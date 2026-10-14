"""FastAPI app: POST /search, GET /healthz, GET /readyz (internal API contract in CLAUDE.md)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from souq_common.budgets import load_budgets
from souq_common.embeddings import get_embedder, resolve_model_dir
from souq_common.settings import get_settings
from souq_common.telemetry import init_telemetry, instrument_fastapi
from souq_search.admission import AdmissionControl
from souq_search.models import Health, Problem, SearchRequest, SearchResponse
from souq_search.opensearch import OpenSearch
from souq_search.pagination import SessionStore
from souq_search.parsing.lexicon import install_catalog_values
from souq_search.parsing.llm import PROMPT_VERSION, LlmQueryParser
from souq_search.parsing.llm_fixtures import AnthropicCaller, LlmTransport
from souq_search.parsing.semantic_cache import SemanticCache
from souq_search.pipeline import BackendUnavailable, EmbedService, InvalidCursor, SearchPipeline
from souq_search.query_log import QueryLogWriter
from souq_search.rerank import OnnxCrossEncoder, Reranker
from souq_search.retrieval.backend import OpenSearchBackend

log = logging.getLogger("souq.search")
PROBLEM_BASE = "https://souq.dev/problems/"
LEXICON_REFRESH_S = 600


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Components:
    pipeline: SearchPipeline
    opensearch: OpenSearch | None = None
    index: str = ""
    redis: Any = None
    query_log: QueryLogWriter | None = None
    checks: dict[str, str] = field(default_factory=dict)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)


async def refresh_lexicon(os_client: OpenSearch, index: str) -> None:
    """Teach the rule parser the brands/colours/sizes that exist in the catalog."""
    brands = await os_client.terms(index, "brand.kw", 5000)
    colours = await os_client.terms(index, "colour", 200)
    sizes = await os_client.terms(index, "size", 500)
    install_catalog_values(brands=brands, colours=set(colours), sizes=set(sizes))


async def _catalog_loop(os_client: OpenSearch, index: str, pipeline: SearchPipeline) -> None:
    while True:
        try:
            await refresh_lexicon(os_client, index)
            coverage = await os_client.coverage(index, ["brand.kw", "colour", "size"])
            coverage["brand"] = coverage.pop("brand.kw", 1.0)
            pipeline.coverage = coverage  # publish whole: requests read it concurrently
        except Exception as exc:
            log.warning("catalog lexicon refresh failed: %s", type(exc).__name__)
        await asyncio.sleep(LEXICON_REFRESH_S)


async def build_components() -> Components:
    settings = get_settings()
    b = load_budgets().search
    checks: dict[str, str] = {}
    index = os.environ.get("SOUQ_SEARCH_INDEX", settings.read_alias)
    os_client = OpenSearch(settings.opensearch_url)

    import redis.asyncio as aioredis

    redis_timeout_s = float(b.redis_timeout_ms) / 1000
    redis = aioredis.from_url(
        settings.redis_url, socket_timeout=redis_timeout_s, socket_connect_timeout=redis_timeout_s
    )

    embed: EmbedService | None = None
    try:
        embed = EmbedService(get_embedder())
        checks["embedder"] = settings.souq_embedder
    except Exception as exc:
        log.error("embedder unavailable, kNN will degrade: %s", exc)
        checks["embedder"] = "unavailable"

    model: OnnxCrossEncoder | None = None
    reranker_dir = resolve_model_dir(settings.souq_reranker_model)
    if (reranker_dir / "model.onnx").exists():
        try:
            model = OnnxCrossEncoder(reranker_dir)
            checks["reranker"] = model.name
        except Exception as exc:
            log.error("reranker failed to load, rerank will degrade: %s", exc)
            checks["reranker"] = "unavailable"
    else:
        log.warning("no reranker at %s; rerank will degrade", reranker_dir)
        checks["reranker"] = "missing"
    reranker = Reranker(model, slots=int(os.environ.get("SOUQ_RERANK_SLOTS", "1")))

    caller = None
    if settings.souq_llm_mode != "replay" and settings.anthropic_api_key:
        caller = AnthropicCaller(settings.anthropic_api_key)
    transport = LlmTransport(settings.souq_llm_mode, caller=caller)
    llm = LlmQueryParser(
        transport,
        settings.souq_llm_model,
        max_output_tokens=int(b.llm_max_output_tokens),
        token_budget=int(b.llm_tokens_per_query),
    )
    checks["llm"] = f"{settings.souq_llm_mode}:{settings.souq_llm_model}"
    cache = None
    if embed is not None:
        cache = SemanticCache(
            redis,
            namespace=f"p{PROMPT_VERSION}_{settings.souq_llm_model}_{settings.souq_embedder}",
            dim=embed.embedder.dim,
            threshold=float(b.semantic_cache_similarity),
            ttl_s=int(b.semantic_cache_ttl_s),
        )

    query_log = None
    if _flag("SOUQ_QUERY_LOG", True):
        query_log = QueryLogWriter(
            settings.database_url,
            batch_size=int(b.query_log_batch),
            flush_ms=float(b.query_log_flush_ms),
            connect_timeout_s=float(b.db_connect_timeout_ms) / 1000,
        )
        query_log.start()

    pipeline = SearchPipeline(
        backend=OpenSearchBackend(os_client, index, str(b.pit_keep_alive)),
        budgets=b,
        embed=embed,
        reranker=reranker,
        llm=llm,
        cache=cache,
        sessions=SessionStore(redis, int(b.pagination_ttl_s)),
        query_log=query_log,
        log_query_text=_flag("SOUQ_QUERY_LOG_TEXT", True),
    )
    comps = Components(
        pipeline=pipeline, opensearch=os_client, index=index, redis=redis, query_log=query_log, checks=checks
    )
    comps.tasks.append(asyncio.create_task(_catalog_loop(os_client, index, pipeline), name="catalog-refresh"))
    return comps


async def warm_up(comps: Components) -> None:
    """Pay ONNX session and connection-pool first-call costs before /readyz can pass.

    A cold process blew the 60-90 ms retrieval budgets on its first requests and answered 503
    (all retrievers timed out), so every deploy or restart would fail its first users.
    """
    timeout_s = 30.0
    try:
        if comps.pipeline.embed is not None:
            await comps.pipeline.embed.embed("warm up", timeout_s)
        if comps.pipeline.reranker.available:
            await comps.pipeline.reranker.score("warm up", ["warm up"] * 30, timeout_s)
        if comps.opensearch is not None:
            await comps.opensearch.index_exists(comps.index, timeout_s)
        comps.checks["warmup"] = "ok"
    except Exception as exc:  # a failed warm-up must not stop the service; requests degrade instead
        comps.checks["warmup"] = f"failed: {type(exc).__name__}"


async def close_components(comps: Components) -> None:
    for task in comps.tasks:
        task.cancel()
    await comps.pipeline.aclose()
    comps.pipeline.reranker.close()
    if comps.pipeline.embed is not None:
        comps.pipeline.embed.close()
    if comps.query_log is not None:
        await comps.query_log.stop()
    if comps.opensearch is not None:
        await comps.opensearch.aclose()
    if comps.redis is not None:
        with contextlib.suppress(Exception):
            await comps.redis.aclose()


def problem(status: int, slug: str, title: str, detail: str | None = None) -> JSONResponse:
    body = Problem(type=PROBLEM_BASE + slug, title=title, status=status, detail=detail)
    return JSONResponse(
        body.model_dump(exclude_none=True), status_code=status, media_type="application/problem+json"
    )


PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": Problem, "description": "Invalid cursor", "content": {"application/problem+json": {}}},
    422: {"model": Problem, "description": "Invalid request", "content": {"application/problem+json": {}}},
    503: {
        "model": Problem,
        "description": "Search backend unavailable",
        "content": {"application/problem+json": {}},
    },
}


def create_app(components: Components | None = None) -> FastAPI:
    """App factory. Pass ``components`` to inject a pipeline (tests); otherwise they are built on startup."""
    init_telemetry("search")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = components is None
        app.state.components = components or await build_components()
        if owned:
            await warm_up(app.state.components)
        try:
            yield
        finally:
            if owned:
                await close_components(app.state.components)

    app = FastAPI(
        title="Souq search (internal)",
        version="1.0.0",
        description="Internal search API. Owns normalisation, parsing, retrieval, fusion, reranking and "
        "pagination. The cursor is opaque to callers.",
        lifespan=lifespan,
    )
    instrument_fastapi(app)
    b = load_budgets().search
    app.add_middleware(
        AdmissionControl, limit=int(b.max_inflight_per_worker), retry_after_s=int(b.shed_retry_after_s)
    )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])
        return problem(422, "validation-error", "Invalid search request", detail)

    @app.exception_handler(InvalidCursor)
    async def _cursor(_: Request, exc: InvalidCursor) -> JSONResponse:
        return problem(400, "invalid-cursor", "Invalid cursor", str(exc))

    @app.exception_handler(BackendUnavailable)
    async def _unavailable(_: Request, exc: BackendUnavailable) -> JSONResponse:
        return problem(503, "search-unavailable", "Search backend unavailable", str(exc))

    @app.post(
        "/search",
        operation_id="search",
        response_model=SearchResponse,
        response_model_exclude_none=True,
        responses=PROBLEM_RESPONSES,
    )
    async def search(req: SearchRequest, request: Request) -> SearchResponse:
        comps: Components = request.app.state.components
        return await comps.pipeline.search(req)

    @app.get("/healthz", operation_id="healthz", response_model=Health)
    async def healthz() -> Health:
        """Liveness: the process is up. Dependency-free on purpose."""
        return Health(status="ok")

    @app.get("/readyz", operation_id="readyz", response_model=Health, responses={503: {"model": Health}})
    async def readyz(request: Request) -> Any:
        comps: Components = request.app.state.components
        checks = dict(comps.checks)
        timeout = float(load_budgets().search.readyz_ms) / 1000
        ready = True
        if comps.opensearch is not None:
            ok = await comps.opensearch.index_exists(comps.index, timeout)
            checks["opensearch"] = "ok" if ok else "unavailable"
            ready = ok
        if comps.redis is not None:
            try:
                await asyncio.wait_for(comps.redis.ping(), timeout)
                checks["redis"] = "ok"
            except Exception:
                checks["redis"] = "unavailable"  # pagination/semantic cache degrade; still serving
        degraded = any(v in ("unavailable", "missing") for k, v in checks.items() if k != "opensearch")
        status = "unavailable" if not ready else ("degraded" if degraded else "ok")
        health = Health(status=status, checks=checks)
        if not ready:
            return JSONResponse(health.model_dump(), status_code=503)
        return health

    return app
