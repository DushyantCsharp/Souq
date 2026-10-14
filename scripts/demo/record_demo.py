"""Record the README demo GIF (docs/demo/souq-demo.gif) from the running local stack.

Every frame comes from the live system. Storefront pages are Playwright screenshots. Terminal frames show
the verbatim output of real ``curl`` calls made during a real load burst. The Grafana frames are screenshots
of the provisioned "Search SLO" dashboard, and the numbers in captions come from the responses of the same
run. Nothing is mocked: if a failure does not happen, the scene is retried, never staged.

Scenes (about 60 s in total):
  1. title card
  2. English query; price/colour filters parsed out (Query interpretation panel)
  3. Arabic query, RTL layout
  4. Arabizi query (Arabic in Latin letters: "sama3at" = headphones)
  5. paging: page 1 -> page 2 -> page token with one character flipped -> "start again" -> page 1
  6. reranker degrade: ~60-100 req/s of varied queries against services/search while the storefront searches
  7. storm shed: ~250 req/s above admission capacity -> real 503 + Retry-After (search and gateway),
     the storefront's "try again" notice, then a normal 200 once the burst is over
  8. Grafana "Search SLO" dashboard (last 15 min) showing both bursts

Prerequisites: the compose stack runs with the ``apps`` and ``obs`` profiles (storefront :3000,
gateway :4000, search :8001, Grafana :3001, Prometheus :9091) and ``curl`` is on PATH. From the repo root:

    uv run --no-project --with playwright python -m playwright install chromium     # once
    uv run --no-project --with playwright --with pillow --with httpx \\
        python scripts/demo/record_demo.py --frames /tmp/souq-demo-frames --out docs/demo/souq-demo.gif

Options:
    --wait-ready PATH   poll every 30 s (up to 90 min) until PATH exists, then capture
    --assemble-only     rebuild the GIF from an existing --frames directory (manifest.json); no capture
    --skip-load         no load bursts (scenes 6-8 are left out)
    --max-mb N          GIF size budget (default 8); the encoder lowers scale/colours until it fits

Load bursts (scenes 6 and 7) are heavy. Each takes the repo's shared lock first (mkdir
``.locks/heavy-docker``, retrying every 15 s) and removes it as soon as the burst ends, also on failure.
A burst sends for at most 11 s with a 3 s per-request timeout, so it is over within 15 s.
Endpoints: SOUQ_DEMO_{STOREFRONT,GATEWAY,SEARCH,GRAFANA,PROMETHEUS}_URL. Frames (PNG) and
manifest.json stay in --frames; only the GIF belongs in the repo.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

REPO = Path(__file__).resolve().parents[2]
STOREFRONT = os.environ.get("SOUQ_DEMO_STOREFRONT_URL", "http://localhost:3000")
GATEWAY = os.environ.get("SOUQ_DEMO_GATEWAY_URL", "http://localhost:4000")
SEARCH = os.environ.get("SOUQ_DEMO_SEARCH_URL", "http://localhost:8001")
GRAFANA = os.environ.get("SOUQ_DEMO_GRAFANA_URL", "http://localhost:3001")
PROMETHEUS = os.environ.get("SOUQ_DEMO_PROMETHEUS_URL", "http://localhost:9091")
LOCK_DIR = REPO / ".locks" / "heavy-docker"
VIEWPORT = {"width": 1280, "height": 800}
TOTAL_SCENES = 8
TARGET_TOTAL_MS = 60_000
BURST_MAX_SEND_S = 11
BURST_REQUEST_TIMEOUT_S = 3.0

EN_QUERY = "black running shoes under 300 AED"
AR_QUERY = "سماعات بلوتوث"
ARABIZI_QUERY = "sama3at bluetooth"
# Storefront queries during load. Each must miss the gateway's response cache, so none repeats.
DEGRADE_QUERIES = [
    "wireless earbuds",
    "noise cancelling headphones",
    "portable bluetooth speaker",
    "gaming headset",
    "smart watch for running",
    "usb c hub",
    "mechanical keyboard",
    "camera tripod",
    "kids headphones",
    "soundbar for tv",
]
STORM_QUERIES = ["laptop backpack", "led desk lamp", "office chair", "yoga mat", "water bottle", "hair dryer"]
GATEWAY_STORM_QUERIES = ["smart watch", "phone case", "usb charger", "travel mug", "coffee grinder"]
# Varied load: English, Arabic and Arabizi, with and without parseable filters.
LOAD_QUERIES = [
    "wireless earbuds",
    "bluetooth speaker",
    "running shoes",
    "black backpack",
    "gaming mouse",
    "usb c charger",
    "smart watch",
    "coffee maker",
    "yoga mat",
    "phone case",
    "laptop stand",
    "red dress",
    "samsung phone",
    "nike shoes size 42",
    "led desk lamp",
    "noise cancelling headphones under 500",
    "office chair",
    "wireless keyboard",
    "hair dryer",
    "water bottle",
    "سماعات بلوتوث",
    "حذاء رياضي",
    "ساعة ذكية",
    "شاحن سريع",
    "حقيبة ظهر",
    "كاميرا",
    "سماعات سوني سوداء",
    "قميص ابيض",
    "sama3at",
    "sa3a thakiya",
    "shanta",
    "7itha2 riyadi",
]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# -- manifest ------------------------------------------------------------------------------------------------


@dataclass
class Frame:
    file: str
    ms: int
    scene: int
    title: str
    caption: str
    flexible: bool = True  # duration may be scaled to hit the 60 s target


@dataclass
class Recorder:
    frames_dir: Path
    frames: list[Frame] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def path(self, name: str) -> Path:
        return self.frames_dir / name

    def add(self, name: str, ms: int, scene: int, title: str, caption: str, *, flexible: bool = True) -> None:
        self.frames.append(Frame(name, ms, scene, title, caption, flexible))

    def note(self, msg: str) -> None:
        log(msg)
        self.notes.append(msg)

    def save(self) -> None:
        data = {
            "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "frames": [f.__dict__ for f in self.frames],
            "notes": self.notes,
        }
        (self.frames_dir / "manifest.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))


# -- shared heavy-docker lock and load bursts ----------------------------------------------------------------


def _try_mkdir(path: Path) -> bool:
    try:
        path.mkdir()
    except FileExistsError:
        return False
    return True


def _rmdir(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.rmdir()


@asynccontextmanager
async def heavy_lock(path: Path = LOCK_DIR) -> AsyncIterator[None]:
    """`until mkdir .locks/heavy-docker; do sleep 15; done` ... `rmdir .locks/heavy-docker`, in Python."""
    waited = False
    while not _try_mkdir(path):
        if not waited:
            log(f"waiting for lock {path} ...")
            waited = True
        await asyncio.sleep(15)
    log(f"lock {path.name} taken")
    try:
        yield
    finally:
        _rmdir(path)
        log(f"lock {path.name} released")


DEGRADED_RE = re.compile(rb'"degraded":\s*\[([^\]]*)\]')


@dataclass
class BurstStats:
    rps: float
    seconds: float
    sent: int = 0
    send_s: float = 0.0
    elapsed_s: float = 0.0
    statuses: Counter[int] = field(default_factory=Counter)
    degraded: Counter[str] = field(default_factory=Counter)
    timeouts: int = 0
    transport_errors: int = 0
    client_dropped: int = 0

    @property
    def ok(self) -> int:
        return self.statuses.get(200, 0)

    @property
    def shed(self) -> int:
        return self.statuses.get(503, 0)

    @property
    def other(self) -> int:
        return sum(n for s, n in self.statuses.items() if s not in (200, 503)) + self.transport_errors

    @property
    def offered_rps(self) -> float:
        return self.sent / self.send_s if self.send_s else 0.0

    def summary(self) -> str:
        parts = [
            f"{self.sent:,} requests in {self.send_s:.1f} s ({self.offered_rps:.0f} req/s)",
            f"200: {self.ok:,}",
            f"503 shed: {self.shed:,}",
            f"timed out (>{BURST_REQUEST_TIMEOUT_S:g} s): {self.timeouts:,}",
        ]
        if self.other:
            parts.append(f"other: {self.other:,}")
        if self.client_dropped:
            parts.append(f"not sent: {self.client_dropped:,}")
        return " | ".join(parts)


async def _burst(url: str, rps: float, seconds: float, queries: Sequence[str]) -> BurstStats:
    """Open-loop load: request i is sent at start + i/rps whether or not earlier ones have answered."""
    import httpx

    stats = BurstStats(rps=rps, seconds=seconds)
    max_inflight = int(rps * BURST_REQUEST_TIMEOUT_S) + 50
    limits = httpx.Limits(max_connections=max_inflight, max_keepalive_connections=max_inflight)
    async with httpx.AsyncClient(timeout=httpx.Timeout(BURST_REQUEST_TIMEOUT_S), limits=limits) as client:
        loop = asyncio.get_running_loop()
        pending: set[asyncio.Task[None]] = set()

        async def one(i: int) -> None:
            body = {"q": queries[i % len(queries)], "lang": "auto", "limit": 24}
            try:
                r = await asyncio.wait_for(client.post(url, json=body), BURST_REQUEST_TIMEOUT_S)
            except TimeoutError:
                stats.timeouts += 1
                return
            except httpx.TimeoutException:
                stats.timeouts += 1
                return
            except httpx.HTTPError:
                stats.transport_errors += 1
                return
            stats.statuses[r.status_code] += 1
            if r.status_code == 200 and (m := DEGRADED_RE.search(r.content)):
                for reason in re.findall(rb'"(\w+)"', m.group(1)):
                    stats.degraded[reason.decode()] += 1

        start = loop.time()
        for i in range(int(rps * seconds)):
            delay = start + i / rps - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if len(pending) >= max_inflight:
                stats.client_dropped += 1
                continue
            task = asyncio.create_task(one(i))
            pending.add(task)
            task.add_done_callback(pending.discard)
            stats.sent += 1
        stats.send_s = loop.time() - start
        if pending:
            await asyncio.wait(pending)
        stats.elapsed_s = loop.time() - start
    return stats


class BackgroundBurst:
    """Runs a burst on its own thread and event loop, so Playwright timing is unaffected."""

    def __init__(self, rps: float, seconds: float, queries: Sequence[str] = LOAD_QUERIES) -> None:
        if seconds > BURST_MAX_SEND_S:
            raise ValueError(f"bursts send for at most {BURST_MAX_SEND_S} s")
        self.rps, self.seconds, self.queries = rps, seconds, queries
        self.result: BurstStats | None = None
        self.error: BaseException | None = None
        self.started_at = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.result = asyncio.run(_burst(f"{SEARCH}/search", self.rps, self.seconds, self.queries))
        except BaseException as exc:
            self.error = exc

    def start(self) -> BackgroundBurst:
        log(f"burst: {self.rps:g} req/s for {self.seconds:g} s -> {SEARCH}/search")
        self.started_at = time.monotonic()
        self._thread.start()
        return self

    def remaining_s(self) -> float:
        return max(0.0, self.started_at + self.seconds - time.monotonic())

    async def join(self) -> BurstStats:
        await asyncio.to_thread(self._thread.join)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        log(f"burst done after {self.result.elapsed_s:.1f} s: {self.result.summary()}")
        log(f"burst degraded reasons in 200s: {dict(self.result.degraded)}")
        return self.result


# -- curl (real subprocess output for the terminal frames) ---------------------------------------------------


@dataclass
class CurlResult:
    display: str
    output: str

    @property
    def status(self) -> int:
        m = re.match(r"HTTP/[\d.]+ (\d{3})", self.output)
        return int(m.group(1)) if m else 0

    @property
    def body(self) -> str:
        return self.output.partition("\n\n")[2]


async def run_curl(args: list[str]) -> CurlResult:
    proc = await asyncio.create_subprocess_exec(
        "curl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return CurlResult(shlex.join(["curl", *args]), out.decode("utf-8", "replace").replace("\r\n", "\n"))


def search_curl_args(q: str, limit: int = 5) -> list[str]:
    body = json.dumps({"q": q, "lang": "en", "limit": limit})
    return [
        "-si",
        "-m",
        "5",
        "-X",
        "POST",
        f"{SEARCH}/search",
        "-H",
        "content-type: application/json",
        "-d",
        body,
    ]


def gateway_curl_args(q: str, limit: int = 5) -> list[str]:
    return ["-si", "-m", "5", f"{GATEWAY}/v1/search?{urlencode({'q': q, 'limit': limit})}"]


# -- terminal frames (real text, rendered by Chromium so every glyph has a font) -----------------------------

TERM_CSS = """
body { margin:0; width:1280px; height:800px; background:#0d1117; overflow:hidden; }
.bar { height:38px; background:#161b22; position:relative; font:15px -apple-system,Helvetica,Arial,sans-serif;
       color:#8b949e; display:flex; align-items:center; justify-content:center; }
.dots { position:absolute; left:14px; top:13px; display:flex; gap:10px; }
.dots i { width:12px; height:12px; border-radius:50%; display:block; }
pre { margin:0; padding:20px 22px; font:16px/23px Menlo,"SF Mono","DejaVu Sans Mono",monospace; color:#e6edf3;
      white-space:pre-wrap; word-break:break-all; }
.comment { color:#8b949e; } .cmd { color:#7ee787; } .bad { color:#ff7b72; } .ok { color:#3fb950; }
.header { color:#c9d1d9; } .accent { color:#d2a8ff; }
"""


def http_lines(result: CurlResult, max_body_chars: int | None = None) -> list[tuple[str, str]]:
    """Style curl -si output; optionally truncate a long body (a marker line says so)."""
    lines: list[tuple[str, str]] = []
    head, _, body = result.output.partition("\n\n")
    for i, line in enumerate(head.splitlines()):
        if i == 0 and line.startswith("HTTP/"):
            lines.append(("ok" if 200 <= result.status < 300 else "bad", line))
        elif line.lower().startswith("retry-after"):
            lines.append(("accent", line))
        else:
            lines.append(("header", line))
    body = body.strip()
    if body:
        lines.append(("body", ""))
        if max_body_chars is not None and len(body) > max_body_chars:
            lines.append(("body", body[:max_body_chars] + " …"))
            lines.append(("comment", f"# body truncated for display ({len(body.encode()):,} bytes in total)"))
        else:
            lines.append(("body", body))
    return lines


async def render_terminal(browser: Any, lines: list[tuple[str, str]], path: Path, title: str) -> None:
    rows = "\n".join(f'<span class="{style}">{html.escape(text)}</span>' for style, text in lines)
    doc = (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{TERM_CSS}</style></head><body>'
        '<div class="bar"><div class="dots"><i style="background:#ff5f56"></i><i style="background:#ffbd2e">'
        f'</i><i style="background:#27c93f"></i></div>{html.escape(title)}</div>'
        f"<pre>{rows}</pre></body></html>"
    )
    page = await browser.new_page(viewport=VIEWPORT)
    try:
        await page.set_content(doc)
        height = await page.evaluate("document.querySelector('pre').getBoundingClientRect().bottom")
        if height > VIEWPORT["height"]:
            raise ValueError(f"terminal frame {path.name} overflows ({height:.0f}px)")
        await page.screenshot(path=str(path))
    finally:
        await page.close()


# -- storefront helpers --------------------------------------------------------------------------------------


async def shot(page: Any, path: Path, *, top: bool = True) -> None:
    if top:
        await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(250)
    await page.screenshot(path=str(path), animations="disabled")


async def wait_results(page: Any) -> None:
    await page.wait_for_load_state("networkidle")
    await page.locator("main#results").wait_for(timeout=15_000)


async def page_state(page: Any) -> dict[str, Any]:
    """What the storefront rendered: degraded reasons, error notice, rank sources, titles, interpretation."""
    return await page.evaluate(
        """() => ({
            dir: document.documentElement.dir,
            degraded: [...document.querySelectorAll('[data-testid=degraded-notice] li')]
                .map(li => li.dataset.reason),
            error: document.querySelector('main#results.notice-error')?.innerText ?? null,
            count: document.querySelector('p.result-count')?.innerText ?? null,
            rankSources: [...document.querySelectorAll('article.product-card')]
                .map(a => (a.dataset.rankSource || '').toLowerCase()),
            titles: [...document.querySelectorAll('h3.product-title')].map(h => h.innerText),
            interpretation: Object.fromEntries([...document.querySelectorAll('details.interpretation dt')]
                .map(dt => {
                    const dd = dt.nextElementSibling;
                    const items = dd ? [...dd.querySelectorAll('li')].map(li => li.innerText.trim()) : [];
                    return [dt.innerText.trim(), items.length ? items.join(' · ') : dd?.innerText.trim()];
                })),
            page: document.querySelector('nav.pagination [aria-current=page]')?.innerText ?? null,
        })"""
    )


async def type_query(page: Any, rec: Recorder, text: str, chunks: Sequence[str], prefix: str) -> list[str]:
    box = page.locator("#q")
    await box.click()
    await box.fill("")
    names: list[str] = []
    for i, chunk in enumerate(chunks):
        await box.press_sequentially(chunk, delay=25)
        name = f"{prefix}_typing{i}.png"
        await page.screenshot(path=str(rec.path(name)), animations="disabled")
        names.append(name)
    assert "".join(chunks) == text
    return names


def _rank(state: dict[str, Any]) -> int:
    return 2 if not state["degraded"] and not state["error"] else (1 if not state["error"] else 0)


async def submit_and_settle_clean(page: Any, rec: Recorder, label: str, out: Path, attempts: int = 6) -> dict:
    """Submit the search box and screenshot the result. If the live answer happens to be degraded or failed,
    wait out the gateway's 5 s degraded-response cache and reload (a real retry). The best real render is
    kept: clean, else degraded, else the error notice."""
    async with page.expect_navigation():
        await page.locator("#q").press("Enter")
    best: tuple[int, dict[str, Any]] | None = None
    for i in range(attempts):
        if i:
            await page.wait_for_timeout(6000)
            await page.reload()
        await wait_results(page)
        state = await page_state(page)
        if best is None or _rank(state) > best[0]:
            await shot(page, out)
            best = (_rank(state), state)
        if best[0] == 2:
            break
        rec.note(f"{label}: attempt {i + 1} rendered degraded={state['degraded']} error={state['error']!r}")
    assert best is not None
    if best[0] < 2:
        rec.note(f"{label}: no clean render in {attempts} attempts; kept the best real one")
    return best[1]


# -- scenes --------------------------------------------------------------------------------------------------


async def scene_title(browser: Any, rec: Recorder) -> None:
    page = await browser.new_page(viewport=VIEWPORT)
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><style>
      body {{ margin:0; width:1280px; height:800px; display:flex; flex-direction:column;
             justify-content:center;
             align-items:center; background:linear-gradient(160deg,#0b3d2e 0%,#0f6b4f 100%); color:#fff;
             font-family:-apple-system,"Helvetica Neue",Arial,sans-serif; text-align:center; }}
      .ar {{ font-size:96px; font-family:"Geeza Pro","Noto Naskh Arabic",serif; margin:0 0 8px; }}
      h1 {{ font-size:46px; margin:0 0 18px; font-weight:700; letter-spacing:-0.5px; }}
      p {{ font-size:24px; margin:6px 0; opacity:.9; }}
      .small {{ font-size:18px; opacity:.7; margin-top:36px; }}
    </style></head><body>
      <div class="ar" dir="rtl" lang="ar">سوق</div>
      <h1>Souq — bilingual catalog search (Arabic / English)</h1>
      <p>BM25 + bge-m3 kNN in parallel → RRF → cross-encoder rerank, each stage under a latency budget</p>
      <p>Arabic normalisation · Arabizi · query understanding · degrade, don't fail</p>
      <p class="small">Recorded live from the local Docker stack on {datetime.now():%Y-%m-%d}:
         storefront → gateway → search, Prometheus + Grafana</p>
    </body></html>"""
    await page.set_content(doc)
    await page.wait_for_timeout(300)
    await page.screenshot(path=str(rec.path("s01_title.png")))
    await page.close()
    rec.add(
        "s01_title.png",
        3500,
        1,
        "Souq",
        "A one-minute tour: English, Arabic and Arabizi search, then three failures handled live.",
    )


async def scene_english(page: Any, rec: Recorder) -> None:
    await page.goto(f"{STOREFRONT}/en", wait_until="networkidle")
    typing = await type_query(page, rec, EN_QUERY, ["black running shoes ", "under 300 AED"], "s02")
    state = await submit_and_settle_clean(page, rec, "english", rec.path("s02_en_results.png"))
    interp = state["interpretation"]
    filters = interp.get("Extracted filters", "?")
    unparsed = interp.get("Unparsed terms", "?").replace(" · ", " ")
    for name in typing:
        rec.add(name, 600, 2, "English query", f"Typing “{EN_QUERY}”", flexible=False)
    rec.add(
        "s02_en_results.png",
        5500,
        2,
        "English query",
        f"The rule parser pulls the constraints out of the text ({filters}); "
        f"only “{unparsed}” is left for BM25 + kNN retrieval. {state['count']}.",
    )


async def scene_arabic(page: Any, rec: Recorder) -> None:
    await page.goto(f"{STOREFRONT}/ar", wait_until="networkidle")
    typing = await type_query(page, rec, AR_QUERY, [AR_QUERY], "s03")
    state = await submit_and_settle_clean(page, rec, "arabic", rec.path("s03_ar_results.png"))
    titles = state["titles"]
    arabic_titles = sum(1 for t in titles if re.search(r"[؀-ۿ]", t))
    await page.evaluate("window.scrollTo(0, 430)")
    await shot(page, rec.path("s03_ar_results_scrolled.png"), top=False)
    for name in typing:
        rec.add(
            name,
            700,
            3,
            "Arabic query",
            "Arabic UI (right-to-left), typing “bluetooth headphones” in Arabic",
            flexible=False,
        )
    rec.add(
        "s03_ar_results.png",
        3500,
        3,
        "Arabic query",
        f'Right-to-left layout (<html dir="{state["dir"]}">), Arabic labels and titles. Tashkeel and '
        "alef / ya / ta-marbuta variants are folded the same way in the query and in the index.",
    )
    rest = (
        "; the rest fall back to English where a product has no Arabic title."
        if arabic_titles < len(titles)
        else "."
    )
    rec.add(
        "s03_ar_results_scrolled.png",
        2500,
        3,
        "Arabic query",
        f"{arabic_titles} of the {len(titles)} titles on this page are in Arabic{rest}",
    )


async def scene_arabizi(page: Any, rec: Recorder) -> None:
    await page.goto(f"{STOREFRONT}/en", wait_until="networkidle")
    typing = await type_query(page, rec, ARABIZI_QUERY, [ARABIZI_QUERY], "s04")
    state = await submit_and_settle_clean(page, rec, "arabizi", rec.path("s04_arabizi_results.png"))
    interp = state["interpretation"]
    for name in typing:
        rec.add(
            name,
            700,
            4,
            "Arabizi query",
            f"Arabic written in Latin letters: “{ARABIZI_QUERY}”",
            flexible=False,
        )
    rec.add(
        "s04_arabizi_results.png",
        5000,
        4,
        "Arabizi query",
        f"Script: {interp.get('Script', '?')}, language: {interp.get('Detected language', '?')}. “sama3at” "
        "(headphones; the digit 3 is the letter ain) is transliterated to Arabic script before BM25, kNN "
        "and rerank.",
    )


async def scroll_nav_to_bottom(page: Any) -> None:
    """Scroll so the pagination nav sits just above the bottom edge, with the last result row above it."""
    await page.evaluate(
        """() => {
            const r = document.querySelector('nav.pagination').getBoundingClientRect();
            window.scrollBy(0, r.bottom - window.innerHeight + 48);
        }"""
    )


def flip_char(token: str) -> tuple[str, int]:
    """Flip one base64url character inside the HMAC part of the token."""
    i = len(token) - 6
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    c = token[i]
    replacement = alphabet[(alphabet.index(c) + 1) % len(alphabet)] if c in alphabet else "A"
    return token[:i] + replacement + token[i + 1 :], i


async def scene_paging(page: Any, rec: Recorder) -> None:
    # Scene 2's query again: the gateway cached it, so page 1 is the same result set.
    await page.goto(f"{STOREFRONT}/en?{urlencode({'q': EN_QUERY})}", wait_until="networkidle")
    await wait_results(page)
    await page.locator("nav.pagination").wait_for()
    await scroll_nav_to_bottom(page)
    next_link = page.locator("nav.pagination a[rel=next]")
    await next_link.hover()
    await shot(page, rec.path("s05_page1_bottom.png"), top=False)

    await next_link.click()
    await page.wait_for_url(lambda u: "after=" in u)
    await wait_results(page)
    await page.locator("nav.pagination [aria-current=page]").wait_for()
    await scroll_nav_to_bottom(page)
    state2 = await page_state(page)
    await shot(page, rec.path("s05_page2_bottom.png"), top=False)

    url = urlparse(page.url)
    params = parse_qs(url.query)
    token = params["after"][0]
    bad, idx = flip_char(token)
    params["after"] = [bad]
    await page.goto(f"{STOREFRONT}{url.path}?{urlencode(params, doseq=True, quote_via=quote)}")
    await page.wait_for_load_state("networkidle")
    await page.locator("main#results").wait_for()
    state_bad = await page_state(page)
    await shot(page, rec.path("s05_tampered.png"))

    await page.get_by_role("link", name="Start again from the first page").click()
    await page.wait_for_url(lambda u: "after=" not in u)
    await wait_results(page)
    state_restart = await page_state(page)
    await shot(page, rec.path("s05_restart.png"))

    rec.add(
        "s05_page1_bottom.png",
        2000,
        5,
        "Paging",
        "Page 1. “Next” carries an opaque, HMAC-signed page token from the gateway "
        "(search cursor + query hash + expiry).",
    )
    rec.add(
        "s05_page2_bottom.png",
        2500,
        5,
        "Paging",
        f"{state2['page']}: the search service resumes the same result set from its pagination snapshot.",
    )
    rec.add(
        "s05_tampered.png",
        3500,
        5,
        "Page retry: tampered token",
        f"The page-2 URL with one character of the token flipped (…{token[idx - 5 : idx + 3]} → "
        f"…{bad[idx - 5 : idx + 3]}): the gateway rejects it (invalid-page-token), "
        "the storefront offers a restart.",
    )
    rec.add(
        "s05_restart.png",
        2500,
        5,
        "Page retry: restart",
        f"“Start again from the first page” re-runs the search from page 1 ({state_restart['count']}).",
    )
    if not state_bad["error"]:
        rec.note(f"paging: the tampered token did not render a notice: {state_bad}")


async def scene_degrade(page: Any, rec: Recorder) -> BurstStats | None:
    queries = iter(DEGRADE_QUERIES)
    for rps in (60, 80, 100):
        stats: BurstStats | None = None
        captured: dict[str, Any] | None = None
        used_q = ""
        async with heavy_lock():
            burst = BackgroundBurst(rps=rps, seconds=BURST_MAX_SEND_S).start()
            try:
                await asyncio.sleep(4)
                while burst.remaining_s() > 2.5:
                    q = next(queries, None)
                    if q is None:
                        break
                    await page.goto(f"{STOREFRONT}/en?{urlencode({'q': q})}", wait_until="networkidle")
                    await page.locator("main#results").wait_for()
                    state = await page_state(page)
                    log(f"degrade: q={q!r} degraded={state['degraded']} error={state['error']!r}")
                    if "rerank" in state["degraded"]:
                        await shot(page, rec.path("s06_degraded.png"))
                        captured, used_q = state, q
                        break
                    await page.wait_for_timeout(500)
            finally:
                stats = await burst.join()
        if captured is not None:
            rr = stats.degraded.get("rerank", 0)
            fused = sum(1 for s in captured["rankSources"] if s == "fused")
            rec.add(
                "s06_degraded.png",
                6500,
                6,
                "Failure 1: reranker skipped under load",
                f"{stats.offered_rps:.0f} req/s of varied queries against services/search: "
                f"{rr:,} of {stats.ok:,} "
                f"answers skipped the over-budget reranker. “{used_q}” still returns results ({fused} of "
                f"{len(captured['rankSources'])} cards in fused order) plus a notice: degrade, don't fail.",
            )
            return stats
        rec.note(f"degrade: no rerank degradation rendered at {rps} req/s; retrying with more load")
    rec.note("degrade: could not capture a degraded storefront render")
    return None


async def scene_storm(browser: Any, page: Any, rec: Recorder) -> BurstStats | None:
    limit = budget_value("max_inflight_per_worker") or "?"
    for attempt, rps in enumerate((250, 300), start=1):
        search_503: CurlResult | None = None
        gateway_503: CurlResult | None = None
        sf_query = ""
        async with heavy_lock():
            burst = BackgroundBurst(rps=rps, seconds=10).start()
            try:
                await asyncio.sleep(2.5)
                for _ in range(30):
                    r = await run_curl(search_curl_args("wireless earbuds"))
                    if r.status == 503:
                        search_503 = r
                        break
                    await asyncio.sleep(0.15)
                for q in GATEWAY_STORM_QUERIES:
                    if burst.remaining_s() < 3:
                        break
                    r = await run_curl(gateway_curl_args(q))
                    log(f"storm: gateway q={q!r} -> {r.status}")
                    if r.status == 503:
                        gateway_503 = r
                        break
                    await asyncio.sleep(0.3)
                for q in STORM_QUERIES:
                    if burst.remaining_s() < 2:
                        break
                    await page.goto(f"{STOREFRONT}/en?{urlencode({'q': q})}", wait_until="networkidle")
                    await page.locator("main#results").wait_for()
                    state = await page_state(page)
                    log(f"storm: storefront q={q!r} error={state['error']!r} degraded={state['degraded']}")
                    if state["error"]:
                        await shot(page, rec.path("s07_storefront_unavailable.png"))
                        sf_query = q
                        break
            finally:
                stats = await burst.join()
        if search_503 is None:
            rec.note(f"storm attempt {attempt}: no 503 captured at {rps} req/s")
            continue

        # Recovery: the burst is over, so the same request is answered normally again.
        await asyncio.sleep(2)
        recovery = await run_curl(search_curl_args("wireless earbuds", limit=3))
        for _ in range(5):
            if recovery.status == 200:
                break
            await asyncio.sleep(1)
            recovery = await run_curl(search_curl_args("wireless earbuds", limit=3))

        lines: list[tuple[str, str]] = [
            ("comment", f"# storm: {stats.offered_rps:.0f} req/s of varied queries -> POST {SEARCH}/search"),
            (
                "comment",
                f"# admission control: more than {limit} in-flight /search per worker -> 503 at once",
            ),
            ("cmd", f"$ {search_503.display}"),
            *http_lines(search_503),
        ]
        if gateway_503 is not None:
            lines += [
                ("body", ""),
                ("comment", "# the public gateway forwards the 503 and its Retry-After"),
                ("cmd", f"$ {gateway_503.display}"),
                *http_lines(gateway_503),
            ]
        await render_terminal(browser, lines, rec.path("s07_terminal_503.png"), "curl during the burst")
        rec.add(
            "s07_terminal_503.png",
            6000,
            7,
            "Failure 2: storm shed",
            f"{stats.offered_rps:.0f} req/s is far above capacity. Instead of queueing until everything "
            "times out, search refuses the excess at once: 503 problem+json with Retry-After: 1"
            + (", forwarded by the gateway." if gateway_503 is not None else "."),
        )
        if sf_query:
            rec.add(
                "s07_storefront_unavailable.png",
                3000,
                7,
                "Failure 2: storm shed",
                f"The storefront during the burst (“{sf_query}”): the gateway's 503 becomes a "
                "“try again in a moment” notice, not an error page.",
            )
        else:
            rec.note("storm: the storefront never rendered the unavailable notice during the burst")

        rec_body: dict[str, Any] = {}
        with suppress(ValueError):
            rec_body = json.loads(recovery.body)
        rlines: list[tuple[str, str]] = [
            ("comment", f"# burst over: {stats.summary()}"),
            ("comment", "# 2 s later, the same request:"),
            ("cmd", f"$ {recovery.display}"),
            *http_lines(recovery, max_body_chars=420),
        ]
        if recovery.status == 200:
            total = rec_body.get("timings_ms", {}).get("total")
            took = f", {total:.0f} ms in search" if isinstance(total, int | float) else ""
            items = len(rec_body.get("items", []))
            rlines.append(
                ("comment", f"# {items} items, degraded: {json.dumps(rec_body.get('degraded'))}{took}")
            )
        await render_terminal(browser, rlines, rec.path("s07_terminal_recovered.png"), "curl after the burst")
        rec.add(
            "s07_terminal_recovered.png",
            4500,
            7,
            "Failure 2: storm shed, then recovery",
            f"The burst: {stats.sent:,} requests, {stats.shed:,} shed with 503, {stats.ok:,} answered. "
            f"Two seconds after it ends the same search gets a normal {recovery.status} again.",
        )
        return stats
    rec.note("storm: could not capture a 503")
    return None


def budget_value(key: str) -> str | None:
    path = REPO / "config" / "budgets.yaml"
    if not path.exists():
        return None
    m = re.search(rf"^\s*{re.escape(key)}:\s*([^\s#]+)", path.read_text(), re.M)
    return m.group(1) if m else None


async def prometheus_value(query: str) -> float:
    import httpx

    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{PROMETHEUS}/api/v1/query", params={"query": query})
        r.raise_for_status()
        result = r.json()["data"]["result"]
        return float(result[0]["value"][1]) if result else 0.0


async def scene_grafana(browser: Any, rec: Recorder) -> None:
    # Metrics reach Grafana by OTLP push (10 s) + Prometheus scrape (10 s): wait until the shed is visible.
    deadline = time.monotonic() + 120
    shed = 0.0
    while time.monotonic() < deadline:
        with suppress(Exception):
            shed = await prometheus_value('sum(increase(degraded_total{reason="shed"}[10m]))')
        if shed > 0:
            break
        await asyncio.sleep(5)
    await asyncio.sleep(25)  # let the last points land so both bursts are complete on the graphs
    page = await browser.new_page(viewport=VIEWPORT)
    base = f"{GRAFANA}/d/souq-search-slo/search-slo?orgId=1&from=now-15m&to=now&kiosk"
    for name, url in (
        ("s08_grafana_top.png", base),
        ("s08_grafana_degraded.png", f"{base}&viewPanel=panel-14"),
    ):
        await page.goto(url, wait_until="load")
        with suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=15_000)
        await page.wait_for_timeout(4000)
        await page.screenshot(path=str(rec.path(name)))
    await page.close()
    rec.add(
        "s08_grafana_top.png",
        4500,
        8,
        "Observability: Grafana “Search SLO”",
        "The last 15 minutes as Prometheus saw them: standard-path latency against the 250 ms SLO, "
        "searches per second.",
    )
    rec.add(
        "s08_grafana_degraded.png",
        5000,
        8,
        "Observability: degraded responses by reason",
        "Both bursts by reason: rerank (and knn) skipped under load, shed for the storm's 503s.",
    )
    if shed <= 0:
        rec.note("grafana: Prometheus showed no shed increase before the capture")


# -- capture and assembly ------------------------------------------------------------------------------------


async def wait_ready(path: Path, timeout_s: float = 90 * 60, every_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while not await asyncio.to_thread(path.exists):
        if time.monotonic() > deadline:
            raise SystemExit(f"{path} did not appear within {timeout_s / 60:.0f} min")
        log(f"waiting for {path} ...")
        await asyncio.sleep(every_s)
    log(f"{path} exists; capturing")


async def capture(frames_dir: Path, skip_load: bool) -> Recorder:
    from playwright.async_api import async_playwright

    rec = Recorder(frames_dir)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport=VIEWPORT, locale="en-US")
            await scene_title(browser, rec)
            await scene_english(page, rec)
            await scene_arabic(page, rec)
            await scene_arabizi(page, rec)
            await scene_paging(page, rec)
            if not skip_load:
                await scene_degrade(page, rec)
                await scene_storm(browser, page, rec)
                await scene_grafana(browser, rec)
        finally:
            rec.save()
            await browser.close()
    return rec


def fit_durations(frames: list[dict[str, Any]], target_ms: int = TARGET_TOTAL_MS) -> None:
    fixed = sum(f["ms"] for f in frames if not f.get("flexible", True))
    flex = sum(f["ms"] for f in frames if f.get("flexible", True))
    if flex <= 0:
        return
    k = max(0.5, (target_ms - fixed) / flex)
    for f in frames:
        if f.get("flexible", True):
            f["ms"] = int(round(f["ms"] * k / 10) * 10)


FONT_CANDIDATES = {
    "sans": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ],
    "bold": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ],
}


def font(kind: str, size: int) -> Any:
    from PIL import ImageFont

    for candidate in FONT_CANDIDATES[kind]:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size)


BAND_H = 104
BRAND = (15, 107, 79)


def compose(frames_dir: Path, f: dict[str, Any]) -> Any:
    """Screenshot + caption band below it (the band never covers the UI). Captions are Latin-only:
    Pillow without libraqm cannot shape Arabic."""
    from PIL import Image, ImageDraw

    shot_img = Image.open(frames_dir / f["file"]).convert("RGB")
    w, h = shot_img.size
    img = Image.new("RGB", (w, h + BAND_H), (17, 20, 24))
    img.paste(shot_img, (0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([0, h, w, h + 3], fill=BRAND)
    chip_font, title_font, body_font = font("bold", 20), font("bold", 24), font("sans", 19)
    chip = f"{f['scene']}/{TOTAL_SCENES}"
    cw = d.textlength(chip, font=chip_font) + 22
    d.rounded_rectangle([20, h + 16, 20 + cw, h + 46], radius=8, fill=BRAND)
    d.text((20 + cw / 2, h + 31), chip, font=chip_font, fill="white", anchor="mm")
    d.text((36 + cw, h + 31), f["title"], font=title_font, fill="white", anchor="lm")
    lines: list[str] = []
    cur = ""
    for word in f["caption"].split():
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=body_font) <= w - 40:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > 2:
        raise ValueError(f"caption for {f['file']} needs {len(lines)} lines (max 2): {f['caption']}")
    for i, line in enumerate(lines):
        d.text((20, h + 56 + i * 24), line, font=body_font, fill=(214, 220, 226))
    return img


def assemble(frames_dir: Path, out: Path, max_mb: float) -> None:
    from PIL import Image

    frames = json.loads((frames_dir / "manifest.json").read_text())["frames"]
    fit_durations(frames)
    total = sum(f["ms"] for f in frames)
    composed = [compose(frames_dir, f) for f in frames]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.gif")
    for scale, colours in ((1.0, 128), (0.9, 128), (0.8, 128), (0.75, 96), (0.66, 96), (0.6, 64), (0.5, 64)):
        imgs = []
        for img in composed:
            if scale != 1.0:
                size = (round(img.width * scale), round(img.height * scale))
                img = img.resize(size, Image.Resampling.LANCZOS)
            imgs.append(
                img.quantize(colors=colours, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
            )
        imgs[0].save(
            tmp,
            save_all=True,
            append_images=imgs[1:],
            duration=[f["ms"] for f in frames],
            loop=0,
            optimize=True,
            disposal=1,
        )
        size_mb = tmp.stat().st_size / 1e6
        log(
            f"gif: scale {scale}, {colours} colours -> {size_mb:.2f} MB "
            f"({len(frames)} frames, {total / 1e3:.1f} s)"
        )
        if size_mb <= max_mb:
            shutil.move(tmp, out)
            log(f"wrote {out} ({imgs[0].width}x{imgs[0].height}, {size_mb:.2f} MB, {total / 1e3:.1f} s)")
            return
    tmp.unlink(missing_ok=True)
    raise SystemExit(f"could not get the GIF under {max_mb} MB")


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", type=Path, default=Path(tempfile.gettempdir()) / "souq-demo-frames")
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "demo" / "souq-demo.gif")
    ap.add_argument("--wait-ready", type=Path, default=None)
    ap.add_argument("--assemble-only", action="store_true")
    ap.add_argument("--skip-load", action="store_true", help="no load bursts: scenes 6-8 are left out")
    ap.add_argument("--max-mb", type=float, default=8.0)
    args = ap.parse_args(argv)

    if shutil.which("curl") is None:
        raise SystemExit("curl is required")
    args.frames.mkdir(parents=True, exist_ok=True)
    if not args.assemble_only:
        if args.wait_ready is not None:
            asyncio.run(wait_ready(args.wait_ready))
        started = time.monotonic()
        rec = asyncio.run(capture(args.frames, args.skip_load))
        log(f"captured {len(rec.frames)} frames in {time.monotonic() - started:.0f} s -> {args.frames}")
        for n in rec.notes:
            log(f"note: {n}")
    assemble(args.frames, args.out, args.max_mb)


if __name__ == "__main__":
    sys.exit(main())
