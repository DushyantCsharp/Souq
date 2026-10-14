// k6 load test for the public search API: GET /v1/search on apps/gateway at a constant arrival rate.
//
//   loadtest/run.sh                       # 200 RPS for 5 min against http://host.docker.internal:4000
//   RATE=50 DURATION=1m loadtest/run.sh    # smaller smoke run
//
// Traffic model (shares of RATE, all constant-arrival-rate so a slow server cannot slow the load down):
//   head        75%  EN / AR / Arabizi+mixed / filtered queries, skewed towards popular queries
//   long_tail   15%  natural-language requests the rule parser cannot resolve → LLM-path candidates
//   deep_pages  10%  broad query, then follow next_page_token for DEEP_PAGES pages (one iteration = N requests)
//
// Latency is classified by the path the *server* reports (parsed_query.parser): "llm" → search_latency_llm,
// "rule"/"cache" → search_latency_standard, so a long-tail query the rule parser handles still counts against
// the 250 ms target. Thresholds encode the targets: standard p95 < 250 ms, LLM path p95 < 1.2 s, < 1% errors.
//
// Env: BASE_URL, RATE (total req/s, default 200), DURATION (default 5m), CACHE_MODE (cold|warm), DEEP_PAGES,
//      API_KEY (sent as x-api-key), CLIENT_IPS (synthetic client population, default 4096), SLO_STANDARD_MS,
//      SLO_LLM_MS, RESULTS_DIR (default results).
import { check } from "k6";
import { SharedArray } from "k6/data";
import exec from "k6/execution";
import http from "k6/http";
import { Counter, Rate, Trend } from "k6/metrics";

const BASE_URL = (__ENV.BASE_URL || "http://localhost:4000").replace(/\/+$/, "");
const RATE = Number(__ENV.RATE || 200);
const DURATION = __ENV.DURATION || "5m";
// cold: vary `limit` so most requests miss the gateway response cache (it keys on limit) and exercise the
// search pipeline; warm: fixed limit, realistic repeat-query cache hits. The hit ratio is reported either way.
const CACHE_MODE = __ENV.CACHE_MODE || "cold";
const DEEP_PAGES = Math.max(2, Number(__ENV.DEEP_PAGES || 4));
const API_KEY = __ENV.API_KEY || "";
// The gateway rate-limits per client IP (120/min by default). Real traffic comes from many clients, so spread
// requests over a synthetic population via X-Forwarded-For (the gateway runs with trustProxy).
const CLIENT_IPS = Math.max(1, Number(__ENV.CLIENT_IPS || 4096));
const SLO_STANDARD_MS = Number(__ENV.SLO_STANDARD_MS || 250);
const SLO_LLM_MS = Number(__ENV.SLO_LLM_MS || 1200);
const RESULTS_DIR = __ENV.RESULTS_DIR || "results";

const SHARE = { head: 0.75, long_tail: 0.15, deep_pages: 0.1 };
const HEAD_MIX = [
  ["en", 0.4],
  ["ar", 0.3],
  ["arabizi", 0.15],
  ["filtered", 0.15],
];

const QUERY_FILE = JSON.parse(open("./queries.json"));
const pools = {};
for (const kind of ["en", "ar", "arabizi", "filtered", "long_tail", "browse"]) {
  pools[kind] = new SharedArray(`queries-${kind}`, () =>
    QUERY_FILE[kind].map((entry) => (typeof entry === "string" ? { q: entry } : entry)),
  );
}

const latencyStandard = new Trend("search_latency_standard", true);
const latencyLlm = new Trend("search_latency_llm", true);
const llmPathShare = new Rate("llm_path_share");
const zeroResultRate = new Rate("zero_result_rate");
const cacheHitRate = new Rate("gateway_cache_hit_rate");
const degradedResponses = new Counter("degraded_responses");
const degradedRate = new Rate("degraded_rate");

function scenario(rate, exec_, extra = {}) {
  const r = Math.max(1, Math.round(rate));
  return {
    executor: "constant-arrival-rate",
    exec: exec_,
    rate: r,
    timeUnit: "1s",
    duration: DURATION,
    // Little's law: VUs ≈ rate × latency. Pre-allocate for ~0.75 s so VU start-up never causes drops;
    // maxVUs bounds k6 memory if the server stalls (stalls then surface as dropped_iterations).
    preAllocatedVUs: Math.max(10, Math.ceil(r * 0.75)),
    maxVUs: Math.max(50, r * 3),
    ...extra,
  };
}

export const options = {
  scenarios: {
    head: scenario(RATE * SHARE.head, "head"),
    long_tail: scenario(RATE * SHARE.long_tail, "longTail"),
    // Each iteration issues DEEP_PAGES requests, so its iteration rate is divided accordingly.
    deep_pages: scenario((RATE * SHARE.deep_pages) / DEEP_PAGES, "deepPages"),
  },
  thresholds: {
    search_latency_standard: [`p(95)<${SLO_STANDARD_MS}`],
    search_latency_llm: [`p(95)<${SLO_LLM_MS}`],
    http_req_failed: ["rate<0.01"],
    checks: ["rate>0.99"],
    // Dropped iterations mean the arrival rate was not achieved (SUT or load generator saturated): > 1% fails.
    dropped_iterations: [`rate<${Math.max(1, RATE * 0.01)}`],
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max", "count"],
  // One metric series per endpoint, not per unique URL.
  tags: { service: "gateway" },
  discardResponseBodies: false,
  noConnectionReuse: false,
};

// Popular queries dominate real search traffic; squaring a uniform draw gives a cheap head-heavy skew.
function pick(pool) {
  return pool[Math.floor(pool.length * Math.random() ** 2)];
}

function pickHeadKind() {
  let x = Math.random();
  for (const [kind, weight] of HEAD_MIX) {
    if ((x -= weight) <= 0) return kind;
  }
  return "en";
}

function limitFor(iteration) {
  return CACHE_MODE === "warm" ? 24 : 10 + (iteration % 41);
}

function clientIp(iteration) {
  const n = iteration % CLIENT_IPS;
  return `10.${66 + ((n >> 16) & 63)}.${(n >> 8) & 255}.${n & 255}`;
}

function searchUrl(item, limit, pageToken) {
  const params = [`q=${encodeURIComponent(item.q)}`, `limit=${limit}`];
  if (item.lang) params.push(`lang=${item.lang}`);
  for (const [field, value] of Object.entries(item.filters || {})) {
    for (const v of Array.isArray(value) ? value : [value]) {
      params.push(`filters[${field}]=${encodeURIComponent(String(v))}`);
    }
  }
  if (pageToken) params.push(`page_token=${encodeURIComponent(pageToken)}`);
  return `${BASE_URL}/v1/search?${params.join("&")}`;
}

function headers(ip) {
  const h = { Accept: "application/json", "X-Forwarded-For": ip };
  if (API_KEY) h["x-api-key"] = API_KEY;
  return h;
}

function parseBody(res) {
  try {
    return res.json();
  } catch {
    return null;
  }
}

/** Issue one search and record the custom metrics; returns the parsed body (or null). */
function search(item, kind, { limit, pageToken, ip, page, expectedPath }) {
  const res = http.get(searchUrl(item, limit, pageToken), {
    headers: headers(ip),
    tags: { name: "GET /v1/search", kind, page },
  });
  const body = res.status === 200 ? parseBody(res) : null;
  check(res, {
    "status 200": (r) => r.status === 200,
    "valid search response": () =>
      body !== null && Array.isArray(body.items) && body.parsed_query !== undefined && Array.isArray(body.degraded),
  });

  const parser = body && body.parsed_query ? body.parsed_query.parser : undefined;
  const path = parser === "llm" ? "llm" : parser ? "standard" : expectedPath;
  const tags = { kind, parser: parser || "none", page };
  (path === "llm" ? latencyLlm : latencyStandard).add(res.timings.duration, tags);
  if (body) {
    llmPathShare.add(path === "llm", tags);
    zeroResultRate.add(body.items.length === 0 && page === "first", tags);
    degradedRate.add(body.degraded.length > 0, tags);
    for (const reason of body.degraded) degradedResponses.add(1, { reason });
  }
  cacheHitRate.add((res.headers["X-Souq-Cache"] || "").toLowerCase() === "hit", tags);
  return body;
}

export function setup() {
  const res = http.get(`${BASE_URL}/readyz`, { tags: { name: "GET /readyz" } });
  if (res.status !== 200) {
    exec.test.abort(`gateway not ready at ${BASE_URL}/readyz (status ${res.status}): start the apps profile first`);
  }
  return { startedAt: new Date().toISOString() };
}

export function head() {
  const iteration = exec.scenario.iterationInTest;
  const kind = pickHeadKind();
  search(pick(pools[kind]), kind, {
    limit: limitFor(iteration),
    ip: clientIp(iteration),
    page: "first",
    expectedPath: "standard",
  });
}

export function longTail() {
  const iteration = exec.scenario.iterationInTest;
  search(pick(pools.long_tail), "long_tail", {
    limit: limitFor(iteration),
    ip: clientIp(iteration + 1_000_003),
    page: "first",
    expectedPath: "llm",
  });
}

export function deepPages() {
  const iteration = exec.scenario.iterationInTest;
  const item = pick(pools.browse);
  const ip = clientIp(iteration + 2_000_003);
  // Same limit on every page: the page token is bound to q/lang/filters and the cursor to the page size.
  const limit = limitFor(iteration);
  let body = search(item, "browse", { limit, ip, page: "first", expectedPath: "standard" });
  for (let page = 2; page <= DEEP_PAGES && body && body.next_page_token; page++) {
    body = search(item, "browse", {
      limit,
      pageToken: body.next_page_token,
      ip,
      page: "next",
      expectedPath: "standard",
    });
  }
}

// ------------------------------------------------------------------------------------------- summary

function metricRow(data, name, label) {
  const m = data.metrics[name];
  if (!m) return `| ${label} | – | – | – | – | 0 |`;
  const v = m.values;
  const fmt = (x) => (x === undefined ? "–" : `${x.toFixed(1)} ms`);
  return `| ${label} | ${fmt(v.med)} | ${fmt(v["p(95)"])} | ${fmt(v["p(99)"])} | ${fmt(v.max)} | ${v.count ?? 0} |`;
}

function rate(data, name) {
  const m = data.metrics[name];
  return m ? `${(100 * m.values.rate).toFixed(2)}%` : "–";
}

function thresholdLines(data) {
  const lines = [];
  for (const [name, metric] of Object.entries(data.metrics)) {
    for (const [expr, result] of Object.entries(metric.thresholds || {})) {
      lines.push(`| \`${name}\` | \`${expr}\` | ${result.ok ? "PASS" : "**FAIL**"} |`);
    }
  }
  return lines;
}

function markdown(data, startedAt) {
  const reqs = data.metrics.http_reqs ? data.metrics.http_reqs.values : { count: 0, rate: 0 };
  const dropped = data.metrics.dropped_iterations ? data.metrics.dropped_iterations.values.count : 0;
  return [
    `### k6 run ${startedAt}`,
    "",
    `Target \`${BASE_URL}\`, ${RATE} req/s for ${DURATION}, cache mode \`${CACHE_MODE}\`, deep pages ${DEEP_PAGES}.`,
    `Achieved ${reqs.count} requests (${reqs.rate.toFixed(1)} req/s), dropped iterations ${dropped}.`,
    "",
    "| Latency (end-to-end at gateway) | p50 | p95 | p99 | max | n |",
    "|---|---|---|---|---|---|",
    metricRow(data, "search_latency_standard", "standard path (rule/cache)"),
    metricRow(data, "search_latency_llm", "LLM path"),
    metricRow(data, "http_req_duration", "all requests"),
    "",
    `Errors ${rate(data, "http_req_failed")} · checks ${rate(data, "checks")} · LLM-path share ${rate(data, "llm_path_share")} · ` +
      `degraded ${rate(data, "degraded_rate")} · zero-result ${rate(data, "zero_result_rate")} · ` +
      `gateway cache hits ${rate(data, "gateway_cache_hit_rate")}`,
    "",
    "| Metric | Threshold | Result |",
    "|---|---|---|",
    ...thresholdLines(data),
    "",
  ].join("\n");
}

export function handleSummary(data) {
  const startedAt = (data.setup_data && data.setup_data.startedAt) || new Date().toISOString();
  const report = markdown(data, startedAt);
  const stamp = startedAt.replace(/[:.]/g, "-");
  return {
    stdout: `\n${report}\n`,
    [`${RESULTS_DIR}/summary.md`]: report,
    [`${RESULTS_DIR}/summary.json`]: JSON.stringify(data, null, 2),
    [`${RESULTS_DIR}/summary-${stamp}.json`]: JSON.stringify(data),
  };
}
