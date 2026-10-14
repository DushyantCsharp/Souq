"""normalise → parse (rule | semantic cache | LLM) → bm25 ∥ (embed → knn) → RRF → rerank → page.

Every stage runs inside ``stage(name, budget_ms)`` and under ``asyncio.wait_for`` with
``min(stage budget, time left in the request budget)``; budgets come from config/budgets.yaml. A stage
that times out or fails degrades the response (``degraded: [...]``) instead of failing it. Only when both
retrievers fail is there nothing to degrade to (:class:`BackendUnavailable` → 503).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from souq_common.arabic import NormalisedQuery, normalise_query, transliterate_arabizi_tokens
from souq_common.budgets import Budgets
from souq_common.embeddings import Embedder
from souq_common.telemetry import hash_query, metrics, stage
from souq_search.fusion import rrf
from souq_search.models import (
    Cursor,
    DegradedReason,
    Facet,
    Filters,
    ParsedQuery,
    ParserPath,
    Price,
    RankSource,
    SearchItem,
    SearchOptions,
    SearchRequest,
    SearchResponse,
)
from souq_search.pagination import PageSession, SessionStore, new_query_key, query_hash, query_key_matches
from souq_search.parsing.llm import PROMPT_VERSION, LlmQueryParser
from souq_search.parsing.rules import RuleParse, parse_rules
from souq_search.parsing.semantic_cache import SemanticCache
from souq_search.query_log import QueryLogRow, QueryLogWriter
from souq_search.rerank import Reranker
from souq_search.retrieval import queries as q
from souq_search.retrieval.backend import Backend, Hit, RetrievalResult

log = logging.getLogger("souq.search")

ALL_RETRIEVERS = frozenset({"bm25", "knn"})
# OpenSearch calls carry the stage budget as their httpx timeout; asyncio.wait_for is only a backstop.
# When both used the same deadline the wait_for cancellation usually won, tearing requests down inside
# httpx's pool; after a sustained overload the pool was left unusable and every request timed out until
# a restart. Letting httpx time out first releases the connection cleanly.
HTTP_BACKSTOP_GRACE_S = 0.05
DESCRIPTION_CHARS = 300
# A constraint parsed from the query text is a hard filter only when that field is populated on at least
# this share of the catalog; otherwise it is a ranking boost (excluding the 84% of products with no size
# because the shopper typed "size 42" loses most relevant results). Facet filters sent in the request
# are always hard.
HARD_FILTER_MIN_COVERAGE = 0.8
COVERAGE_FIELDS = {"brands": "brand", "colours": "colour", "sizes": "size"}


def split_by_coverage(parsed: Filters, coverage: dict[str, float]) -> tuple[Filters, Filters]:
    """(hard, soft) parts of ``parsed``; unknown coverage counts as dense (hard)."""
    hard = parsed.model_copy(deep=True)
    soft = Filters()
    for name, field_name in COVERAGE_FIELDS.items():
        if getattr(parsed, name) and coverage.get(field_name, 1.0) < HARD_FILTER_MIN_COVERAGE:
            setattr(soft, name, getattr(parsed, name))
            setattr(hard, name, None)
    return hard, soft


class BackendUnavailable(RuntimeError):
    """Both retrievers failed: nothing to degrade to."""


class InvalidCursor(ValueError):
    pass


@dataclass(frozen=True)
class PipelineOptions:
    """Internal superset of the API options; the eval harness uses it for ablations."""

    retrievers: frozenset[str] = ALL_RETRIEVERS
    rerank: bool = True
    llm: bool = True
    semantic_cache: bool = True
    debug: bool = False
    store_session: bool = True
    log_query: bool = True

    @classmethod
    def from_api(cls, options: SearchOptions | None) -> PipelineOptions:
        o = options or SearchOptions()
        return cls(rerank=o.rerank, llm=o.llm, debug=o.debug)


class Deadline:
    def __init__(self, budget_ms: float) -> None:
        self.start = time.perf_counter()
        self.budget_ms = budget_ms

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.start) * 1000

    def extend(self, budget_ms: float) -> None:
        self.budget_ms = max(self.budget_ms, budget_ms)

    def remaining_ms(self) -> float:
        return self.budget_ms - self.elapsed_ms()

    def timeout_s(self, stage_ms: float) -> float:
        """Stage timeout: its own budget, but never past the request deadline (floor 1 ms)."""
        return max(1.0, min(stage_ms, self.remaining_ms())) / 1000


class EmbedService:
    """Embeds on a small dedicated thread pool with an LRU in front (repeat queries are common)."""

    def __init__(self, embedder: Embedder, cache_size: int = 4096, workers: int = 2) -> None:
        self.embedder = embedder
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_size = cache_size
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="embed")

    async def embed(self, text: str, timeout_s: float) -> np.ndarray:
        hit = self._cache.get(text)
        if hit is not None:
            self._cache.move_to_end(text)
            return hit
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, self.embedder.encode_queries, [text])
        # No shield: on timeout the cancellation reaches the executor, so an embed still queued behind
        # others is dropped instead of running later for a request that already degraded. Shielding it
        # let abandoned work pile up under load until every request queued for seconds.
        vecs = await asyncio.wait_for(future, timeout=timeout_s)
        vec: np.ndarray = vecs[0]
        self._cache[text] = vec
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return vec

    def clear(self) -> None:
        self._cache.clear()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


@dataclass
class Parsed:
    original: str
    nq: NormalisedQuery
    detected_lang: str
    display_lang: str
    rule: RuleParse
    filters: Filters
    parser: ParserPath
    rewritten: str | None
    llm_tokens: tuple[int, int] = (0, 0)
    llm_cost: float | None = None
    llm_source: str | None = None
    llm_recorded_at: str | None = None
    key_vector: np.ndarray | None = None


@dataclass
class Plan:
    terms: q.QueryTerms
    embed_text: str
    rerank_query: str
    applied: Filters
    apply_currency: bool
    relaxed: bool = False
    boosted: Filters | None = None


@dataclass
class Ctx:
    req: SearchRequest
    opts: PipelineOptions
    deadline: Deadline
    timings: dict[str, float] = field(default_factory=dict)
    degraded: list[DegradedReason] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)

    def degrade(self, reason: DegradedReason) -> None:
        if reason not in self.degraded:
            self.degraded.append(reason)

    def timed(self, name: str, start: float) -> None:
        self.timings[name] = round((time.perf_counter() - start) * 1000, 3)


def _detected_lang(nq: NormalisedQuery, text: str) -> str:
    if nq.script in ("ar", "arabizi"):
        return "ar"
    if nq.script == "en":
        return "en"
    ar = sum(1 for ch in text if "؀" <= ch <= "ۿ")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return "ar" if ar >= latin else "en"


def llm_request_for_query(llm: LlmQueryParser, q: str, lang: str) -> dict[str, Any] | None:
    """The exact Messages API body the pipeline would send for ``q`` (None if the rule parser suffices).
    Used to author record/replay fixtures; mirrors SearchPipeline._parse."""
    nq = normalise_query(q)
    display = lang if lang != "auto" else _detected_lang(nq, q)
    rule = parse_rules(nq.text, display)
    if not rule.constraint_like:
        return None
    return llm.build_request(nq.text, display, rule)


def _localised(src: dict[str, Any], key: str, lang: str) -> str | None:
    primary, secondary = (f"{key}_ar", f"{key}_en") if lang == "ar" else (f"{key}_en", f"{key}_ar")
    value = src.get(primary) or src.get(secondary)
    return str(value) if value else None


def rerank_text(src: dict[str, Any], lang: str) -> str:
    """Cross-encoder document text: localised title (falls back to the other language) + brand. One title
    only: the second language doubles the sequence length (and the rerank latency) for little gain with a
    multilingual cross-encoder."""
    title = _localised(src, "title", lang) or ""
    brand = src.get("brand") or ""
    return f"{title} | {brand}" if brand and brand.lower() not in title.lower() else title


class SearchPipeline:
    def __init__(
        self,
        *,
        backend: Backend,
        budgets: Budgets,
        embed: EmbedService | None,
        reranker: Reranker,
        llm: LlmQueryParser | None,
        cache: SemanticCache | None,
        sessions: SessionStore,
        query_log: QueryLogWriter | None = None,
        log_query_text: bool = True,
    ) -> None:
        self.backend = backend
        self.b = budgets
        self.embed = embed
        self.reranker = reranker
        self.llm = llm
        self.cache = cache
        self.sessions = sessions
        self.query_log = query_log
        self.log_query_text = log_query_text
        # Share of catalog docs with brand/colour/size, refreshed by the app (see split_by_coverage).
        self.coverage: dict[str, float] = {}
        self._background: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------------------------------- public
    async def search(self, req: SearchRequest, opts: PipelineOptions | None = None) -> SearchResponse:
        opts = opts or PipelineOptions.from_api(req.options)
        ctx = Ctx(req=req, opts=opts, deadline=Deadline(float(self.b.total_standard_ms)))
        start = time.perf_counter()
        api_opts = req.options or SearchOptions()
        if req.cursor is not None:
            # Later pages reuse page 1's parse from the session: normalising is enough to check the cursor
            # (no second LLM call or cache lookup per page).
            nq = normalise_query(req.q)
            qhash = query_hash(nq.text, req.lang, req.filters, api_opts)
            if not query_key_matches(req.cursor.query_key, qhash):
                raise InvalidCursor("cursor does not belong to this query")
            session = await self.sessions.load(req.cursor.query_key)
            if session is not None:
                resp = await self._next_page(ctx, req.cursor, session)
                self._finish(ctx, nq.text, nq.script, resp.parsed_query.parser, resp, start)
                return resp
            log.info("pagination session expired; recomputing", extra={"query_hash": qhash})

        parsed = await self._parse(ctx)
        qhash = query_hash(parsed.nq.text, req.lang, req.filters, api_opts)
        resp = await self._first_page(ctx, parsed, qhash)
        self._finish(ctx, parsed.nq.text, parsed.nq.script, parsed.parser, resp, start)
        return resp

    async def parse(
        self, req: SearchRequest, opts: PipelineOptions | None = None
    ) -> tuple[ParsedQuery, list[str]]:
        """Run only normalise + parse (rule | cache | LLM); used by the filter-parse eval."""
        opts = opts or PipelineOptions.from_api(req.options)
        ctx = Ctx(req=req, opts=opts, deadline=Deadline(float(self.b.total_standard_ms)))
        parsed = await self._parse(ctx)
        return self._parsed_query(parsed), list(ctx.degraded)

    async def aclose(self) -> None:
        for task in list(self._background):
            task.cancel()

    # ------------------------------------------------------------------------------------------- stages
    async def _parse(self, ctx: Ctx) -> Parsed:
        b = self.b
        t = time.perf_counter()
        with stage("normalise", b.normalise_ms):
            nq = normalise_query(ctx.req.q)
        ctx.timed("normalise", t)
        detected = _detected_lang(nq, ctx.req.q)
        display = ctx.req.lang if ctx.req.lang != "auto" else detected

        t = time.perf_counter()
        rule = parse_rules(nq.text, display)  # pure and sub-millisecond; decides the parse budget
        llm_wanted = bool(rule.constraint_like) and ctx.opts.llm and self.llm is not None
        budget = float(b.parse_rule_ms)
        if llm_wanted:
            budget += float(b.embed_ms) + float(b.semantic_cache_ms) + float(b.parse_llm_ms)
        with stage("parse", budget, path="rule") as span:
            parsed = Parsed(
                original=ctx.req.q,
                nq=nq,
                detected_lang=detected,
                display_lang=display,
                rule=rule,
                filters=rule.filters,
                parser="rule",
                rewritten=None,
            )
            if llm_wanted:
                ctx.deadline.extend(float(b.total_llm_ms))
                await self._parse_llm(ctx, parsed)
            span.set_attribute("souq.parse.path", {"cache": "cache_hit"}.get(parsed.parser, parsed.parser))
        ctx.timed("parse", t)
        return parsed

    async def _parse_llm(self, ctx: Ctx, parsed: Parsed) -> None:
        assert self.llm is not None
        b = self.b
        nq = parsed.nq
        m = metrics()
        if self.cache is not None and self.embed is not None and ctx.opts.semantic_cache:
            try:
                parsed.key_vector = await self.embed.embed(nq.text, ctx.deadline.timeout_s(float(b.embed_ms)))
                m.semantic_cache_lookups_total.add(1)
                with stage("semantic_cache", b.semantic_cache_ms):
                    hit = await asyncio.wait_for(
                        self.cache.lookup(parsed.key_vector, nq.text, parsed.display_lang),
                        ctx.deadline.timeout_s(float(b.semantic_cache_ms)),
                    )
                if hit is not None:
                    m.semantic_cache_hits_total.add(1)
                    parsed.parser = "cache"
                    parsed.filters = Filters.model_validate(hit["filters"]).merged_over(parsed.rule.filters)
                    parsed.rewritten = hit.get("rewritten")
                    return
            except Exception as exc:
                log.warning("semantic cache unavailable: %s", type(exc).__name__)
                ctx.degrade("semantic_cache")
        try:
            result = await asyncio.wait_for(
                self.llm.parse(
                    nq.text, parsed.display_lang, parsed.rule, ctx.deadline.timeout_s(float(b.parse_llm_ms))
                ),
                ctx.deadline.timeout_s(float(b.parse_llm_ms)),
            )
        except Exception as exc:
            log.info("llm parse degraded: %s", type(exc).__name__)
            ctx.degrade("llm_parse")
            return
        parsed.parser = "llm"
        parsed.filters = result.filters
        parsed.rewritten = result.rewritten
        parsed.llm_tokens = (result.input_tokens, result.output_tokens)
        parsed.llm_cost = result.cost_usd
        parsed.llm_source = result.source
        parsed.llm_recorded_at = result.recorded_at
        if self.cache is not None and parsed.key_vector is not None and "semantic_cache" not in ctx.degraded:
            payload = {
                "filters": result.filters.model_dump(exclude_none=True),
                "rewritten": result.rewritten,
                "prompt_version": PROMPT_VERSION,
            }
            self._spawn(self.cache.store(parsed.key_vector, nq.text, parsed.display_lang, payload))

    def _plan(self, parsed: Parsed, request_filters: Filters | None, relaxed: bool = False) -> Plan:
        nq = parsed.nq
        base = request_filters or Filters()
        soft = Filters()
        if relaxed:
            applied, text, rewritten = base, nq.text, None
        else:
            hard, soft = split_by_coverage(parsed.filters, self.coverage)
            applied = base.merged_over(hard)
            text, rewritten = (parsed.rewritten or parsed.rule.residual or ""), parsed.rewritten
            if not text.strip():
                # Every word was a constraint ("nike size 42"): rank by the brand/colour words.
                text = " ".join([*(applied.brands or []), *(applied.colours or [])]).lower() or nq.text
        tokens = text.split()
        translit = {} if rewritten else transliterate_arabizi_tokens(tokens)
        terms = q.make_terms(text, translit)
        terms.boosts = q.boost_clauses(soft)
        embed_text = " ".join(translit.get(t, t) for t in tokens) if translit else text
        rerank_query = f"{text} {' '.join(translit.values())}".strip() if translit else text
        return Plan(
            terms=terms,
            embed_text=embed_text or nq.text,
            rerank_query=rerank_query or nq.text,
            applied=applied,
            # A currency read from the query text is informational (prices are in catalog currency);
            # only an explicit request filter restricts by currency.
            apply_currency=bool(request_filters and request_filters.currency),
            relaxed=relaxed,
            boosted=None if soft.is_empty() else soft,
        )

    async def _bm25(self, ctx: Ctx, plan: Plan, clauses: list[dict[str, Any]]) -> RetrievalResult:
        b = self.b
        t = time.perf_counter()
        try:
            with stage("bm25", b.bm25_ms):
                return await asyncio.wait_for(
                    self.backend.bm25(
                        plan.terms,
                        clauses,
                        int(b.candidates_per_retriever),
                        q.facet_aggs(plan.applied),
                        ctx.deadline.timeout_s(float(b.bm25_ms)),
                    ),
                    ctx.deadline.timeout_s(float(b.bm25_ms)) + HTTP_BACKSTOP_GRACE_S,
                )
        finally:
            ctx.timed("bm25", t)

    async def _knn(
        self, ctx: Ctx, plan: Plan, clauses: list[dict[str, Any]], parsed: Parsed
    ) -> RetrievalResult:
        b = self.b
        if self.embed is None:
            raise RuntimeError("no embedder")
        t = time.perf_counter()
        try:
            with stage("embed", b.embed_ms):
                if parsed.key_vector is not None and plan.embed_text == parsed.nq.text:
                    vec = parsed.key_vector
                else:
                    vec = await self.embed.embed(plan.embed_text, ctx.deadline.timeout_s(float(b.embed_ms)))
        finally:
            ctx.timed("embed", t)
        t = time.perf_counter()
        try:
            with stage("knn", b.knn_ms):
                return await asyncio.wait_for(
                    self.backend.knn(
                        [float(x) for x in vec],
                        clauses,
                        int(b.candidates_per_retriever),
                        ctx.deadline.timeout_s(float(b.knn_ms)),
                    ),
                    ctx.deadline.timeout_s(float(b.knn_ms)) + HTTP_BACKSTOP_GRACE_S,
                )
        finally:
            ctx.timed("knn", t)

    async def _retrieve(
        self, ctx: Ctx, plan: Plan, parsed: Parsed
    ) -> tuple[RetrievalResult | None, RetrievalResult | None]:
        clauses = q.filter_clauses(plan.applied, apply_currency=plan.apply_currency)
        use = ctx.opts.retrievers
        bm25_task = asyncio.create_task(self._bm25(ctx, plan, clauses)) if "bm25" in use else None
        knn_task = asyncio.create_task(self._knn(ctx, plan, clauses, parsed)) if "knn" in use else None
        bm25_res: RetrievalResult | None = None
        knn_res: RetrievalResult | None = None
        if bm25_task is not None:
            try:
                bm25_res = await bm25_task
            except Exception as exc:
                log.warning("bm25 degraded: %s", type(exc).__name__)
                ctx.degrade("bm25")
        if knn_task is not None:
            try:
                knn_res = await knn_task
            except Exception as exc:
                log.warning("knn degraded: %s", type(exc).__name__)
                ctx.degrade("knn")
        if bm25_res is None and knn_res is None:
            raise BackendUnavailable("all retrievers failed")
        return bm25_res, knn_res

    async def _rerank(
        self, ctx: Ctx, plan: Plan, window: list[Hit], lang: str
    ) -> tuple[list[Hit], list[float], str]:
        """Returns (ordered window, scores, rank source)."""
        fused_scores = [h.score for h in window]
        if not ctx.opts.rerank or len(window) < 2:
            return window, fused_scores, "fused"
        if not self.reranker.available:
            ctx.degrade("rerank")
            return window, fused_scores, "fused"
        b = self.b
        t = time.perf_counter()
        try:
            with stage("rerank", b.rerank_ms, window=len(window)):
                scores = await self.reranker.score(
                    plan.rerank_query,
                    [rerank_text(h.source, lang) for h in window],
                    ctx.deadline.timeout_s(float(b.rerank_ms)),
                )
        except Exception as exc:
            log.info("rerank degraded: %s", type(exc).__name__)
            ctx.degrade("rerank")
            return window, fused_scores, "fused"
        finally:
            ctx.timed("rerank", t)
        order = sorted(range(len(window)), key=lambda i: (-float(scores[i]), i))
        if str(getattr(b, "rerank_mode", "replace")) == "rrf":
            # The reranker votes alongside the fused order instead of overriding it.
            weight = float(getattr(b, "rerank_rrf_weight", 1.0))
            fused = rrf([[h.sku for h in window], [window[i].sku for i in order]], k=int(b.rrf_k),
                        weights=[1.0, weight])  # fmt: skip
            by_sku = {h.sku: h for h in window}
            return [by_sku[s] for s, _ in fused], [round(sc, 6) for _, sc in fused], "rerank"
        return [window[i] for i in order], [round(float(scores[i]), 6) for i in order], "rerank"

    # ------------------------------------------------------------------------------------------- pages
    async def _first_page(self, ctx: Ctx, parsed: Parsed, qhash: str) -> SearchResponse:
        b = self.b
        req = ctx.req
        plan = self._plan(parsed, req.filters)
        bm25_res, knn_res = await self._retrieve(ctx, plan, parsed)

        no_hits = not (bm25_res and bm25_res.hits) and not (knn_res and knn_res.hits)
        parsed_constraints = not parsed.filters.is_empty() or parsed.rewritten
        if no_hits and parsed_constraints and ctx.deadline.remaining_ms() > float(b.bm25_ms) / 2:
            # Parsed filters can be wrong ("apple juice" -> brand Apple); retry with only the user's own
            # filters and the full query text before returning nothing.
            plan = self._plan(parsed, req.filters, relaxed=True)
            bm25_res, knn_res = await self._retrieve(ctx, plan, parsed)

        t = time.perf_counter()
        with stage("fuse", b.fuse_ms):
            by_sku: dict[str, Hit] = {}
            rankings: list[list[str]] = []
            for res in (bm25_res, knn_res):
                if res is None:
                    continue
                rankings.append([h.sku for h in res.hits])
                for h in res.hits:
                    by_sku.setdefault(h.sku, h)
            fused = rrf(rankings, k=int(b.rrf_k))
        ctx.timed("fuse", t)
        pool = [Hit(sku=s, score=round(sc, 6), source=by_sku[s].source) for s, sc in fused]

        n_window = int(b.rerank_window)
        window, window_scores, window_source = await self._rerank(
            ctx, plan, pool[:n_window], parsed.display_lang
        )
        rest = pool[n_window:]

        bm25_total = bm25_res.total if bm25_res else 0
        bm25_in_pool = len(bm25_res.hits) if bm25_res else 0
        has_tail = bm25_res is not None and bm25_total > bm25_in_pool
        total_estimate = len(pool) + max(0, bm25_total - bm25_in_pool)

        if bm25_res is not None and bm25_res.aggregations:
            facets = q.parse_facets(bm25_res.aggregations)
        else:
            facets = q.facets_from_sources([h.source for h in pool])

        ordered: list[tuple[Hit, float, RankSource]] = [
            (h, s, "rerank" if window_source == "rerank" else "fused")
            for h, s in zip(window, window_scores, strict=True)
        ] + [(h, h.score, "fused") for h in rest]
        page = ordered[: req.limit]
        items = [self._item(h.source, h.sku, s, src, parsed.display_lang) for h, s, src in page]

        parsed_query = self._parsed_query(parsed)
        next_cursor: Cursor | None = None
        if len(ordered) > req.limit or has_tail:
            query_key = new_query_key(qhash)
            next_cursor = Cursor(query_key=query_key, pit_id="", window_pos=len(page))
            if ctx.opts.store_session:
                session = PageSession(
                    window=[h.sku for h in window],
                    window_scores=window_scores,
                    window_source=window_source,
                    fused=[h.sku for h in rest],
                    fused_scores=[h.score for h in rest],
                    has_tail=has_tail,
                    total_estimate=total_estimate,
                    display_lang=parsed.display_lang,
                    terms=plan.terms.terms,
                    translit=plan.terms.translit,
                    applied_filters=plan.applied.model_dump(exclude_none=True),
                    boosts=plan.terms.boosts,
                    apply_currency=plan.apply_currency,
                    facets=[f.model_dump() for f in facets],
                    parsed_query=parsed_query.model_dump(),
                    degraded=list(ctx.degraded),
                )
                try:
                    await self.sessions.save(query_key, session)
                except Exception as exc:
                    # Without a session there is no stable page 2; say so by not offering one.
                    log.warning("pagination session not stored: %s", type(exc).__name__)
                    next_cursor = None

        if ctx.opts.debug:
            ctx.debug.update(
                bm25=[(h.sku, round(h.score, 4)) for h in (bm25_res.hits if bm25_res else [])],
                knn=[(h.sku, round(h.score, 4)) for h in (knn_res.hits if knn_res else [])],
                fused=[(h.sku, h.score) for h in pool],
                window=[(h.sku, s) for h, s in zip(window, window_scores, strict=True)],
                terms=plan.terms.terms,
                translit=plan.terms.translit,
                embed_text_hash=hash_query(plan.embed_text),
                relaxed=plan.relaxed,
                bm25_total=bm25_total,
                boosted=plan.boosted.model_dump(exclude_none=True) if plan.boosted else None,
                llm={
                    "input_tokens": parsed.llm_tokens[0],
                    "output_tokens": parsed.llm_tokens[1],
                    "cost_usd": parsed.llm_cost,
                    "source": parsed.llm_source,
                    "recorded_at": parsed.llm_recorded_at,
                },
            )
        return SearchResponse(
            items=items,
            facets=facets,
            applied_filters=plan.applied,
            parsed_query=parsed_query,
            degraded=list(ctx.degraded),
            next_cursor=next_cursor,
            total_estimate=total_estimate,
            timings_ms=ctx.timings,
            debug=ctx.debug or None,
        )

    async def _next_page(self, ctx: Ctx, cursor: Cursor, session: PageSession) -> SearchResponse:
        b = self.b
        req = ctx.req
        ordered = session.ordered()
        pos = cursor.window_pos
        picked = ordered[pos : pos + req.limit]
        items: list[SearchItem] = []
        t = time.perf_counter()
        sources: dict[str, dict[str, Any]] = {}
        if picked:
            try:
                with stage("fetch", b.bm25_ms):
                    sources = await asyncio.wait_for(
                        self.backend.fetch(
                            [s for s, _, _ in picked], ctx.deadline.timeout_s(float(b.bm25_ms))
                        ),
                        ctx.deadline.timeout_s(float(b.bm25_ms)) + HTTP_BACKSTOP_GRACE_S,
                    )
            except Exception as exc:
                log.warning("page fetch failed: %s", type(exc).__name__)
                raise BackendUnavailable("cannot fetch page") from exc
        ctx.timed("fetch", t)
        for sku, score, src in picked:
            if sku in sources:  # a product deleted since page 1 is skipped, positions still advance
                rank_source: RankSource = "rerank" if src == "rerank" else "fused"
                items.append(self._item(sources[sku], sku, score, rank_source, session.display_lang))
        new_pos = pos + len(picked)
        pit_id = cursor.pit_id or session.pit_id or ""
        search_after = cursor.search_after
        more = new_pos < len(ordered)

        missing = req.limit - len(picked)
        if missing > 0 and session.has_tail:
            t = time.perf_counter()
            phrase = " ".join(session.terms) if len(session.terms) > 1 else None
            terms = q.QueryTerms(
                terms=session.terms, translit=session.translit, phrase=phrase, boosts=session.boosts
            )
            clauses = q.filter_clauses(
                Filters.model_validate(session.applied_filters), apply_currency=session.apply_currency
            )
            try:
                with stage("lexical_tail", b.bm25_ms):
                    tail = await asyncio.wait_for(
                        self.backend.tail(
                            terms,
                            clauses,
                            session.pool(),
                            missing,
                            pit_id or None,
                            search_after,
                            ctx.deadline.timeout_s(float(b.bm25_ms)),
                        ),
                        ctx.deadline.timeout_s(float(b.bm25_ms)) + HTTP_BACKSTOP_GRACE_S,
                    )
                for h in tail.hits:
                    items.append(
                        self._item(h.source, h.sku, round(h.score, 6), "lexical_tail", session.display_lang)
                    )
                new_pos += len(tail.hits)
                pit_id, search_after = tail.pit_id, tail.search_after
                more = not tail.exhausted
            except Exception as exc:
                log.warning("lexical tail degraded: %s", type(exc).__name__)
                ctx.degrade("bm25")
                more = False
            finally:
                ctx.timed("lexical_tail", t)
        elif missing > 0:
            more = False
        else:
            more = more or session.has_tail

        next_cursor = (
            Cursor(query_key=cursor.query_key, pit_id=pit_id, window_pos=new_pos, search_after=search_after)
            if more
            else None
        )
        for reason in session.degraded:
            if reason in ("rerank", "llm_parse", "knn", "bm25", "semantic_cache"):
                ctx.degrade(reason)  # type: ignore[arg-type]
        return SearchResponse(
            items=items,
            facets=[Facet.model_validate(f) for f in session.facets],
            applied_filters=Filters.model_validate(session.applied_filters),
            parsed_query=ParsedQuery.model_validate(session.parsed_query),
            degraded=list(ctx.degraded),
            next_cursor=next_cursor,
            total_estimate=session.total_estimate,
            timings_ms=ctx.timings,
        )

    # ------------------------------------------------------------------------------------------- helpers
    def _parsed_query(self, parsed: Parsed) -> ParsedQuery:
        return ParsedQuery(
            original=parsed.original,
            normalised=parsed.nq.text,
            detected_lang="ar" if parsed.detected_lang == "ar" else "en",
            script=parsed.nq.script,
            rewritten=parsed.rewritten,
            filters=parsed.filters,
            parser=parsed.parser,
            unparsed_tokens=parsed.rule.unparsed_tokens,
        )

    @staticmethod
    def _item(src: dict[str, Any], sku: str, score: float, rank_source: RankSource, lang: str) -> SearchItem:
        desc = _localised(src, "description", lang)
        if desc and len(desc) > DESCRIPTION_CHARS:
            desc = desc[: DESCRIPTION_CHARS - 1].rstrip() + "…"
        price = src.get("price")
        return SearchItem(
            sku=sku,
            title=_localised(src, "title", lang) or sku,
            description=desc,
            brand=str(src.get("brand") or ""),
            price=Price(
                amount=float(price) if price is not None else 0.0, currency=str(src.get("currency") or "AED")
            ),
            category_path=str(src.get("category_path") or ""),
            colour=src.get("colour"),
            size=src.get("size"),
            score=score,
            rank_source=rank_source,
        )

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _finish(
        self, ctx: Ctx, normalised: str, script: str, parser: ParserPath, resp: SearchResponse, start: float
    ) -> None:
        total = round((time.perf_counter() - start) * 1000, 3)
        resp.timings_ms["total"] = total
        m = metrics()
        path = {"rule": "standard", "llm": "llm", "cache": "cache"}[parser]
        m.search_latency.record(total, {"path": path})
        m.search_total.add(1)
        if parser == "llm":
            m.llm_path_total.add(1)
        if not resp.items:
            m.zero_result_total.add(1)
        for reason in resp.degraded:
            m.degraded_total.add(1, {"reason": reason})
        if self.query_log is not None and ctx.opts.log_query:
            self.query_log.enqueue(
                QueryLogRow(
                    query_hash=hash_query(normalised),
                    query_text=normalised if self.log_query_text else None,
                    lang=ctx.req.lang,
                    detected_script=script,
                    parser_path=parser,
                    latency_ms=resp.timings_ms,
                    degraded=list(resp.degraded),
                    result_count=len(resp.items),
                )
            )
