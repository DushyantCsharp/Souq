"""The committed replay fixtures must match what the service sends today (else: regenerate them with
services/search/scripts/make_synthetic_fixtures.py, or re-record with a key), and the pipeline must apply
whatever answer a fixture holds. Fixtures may be hand-authored or recorded from the live model, so the
expected answer is read from the fixture itself, not from the authoring file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fakes import FakeBackend, catalog

from souq_common.budgets import load_budgets
from souq_common.embeddings import FakeEmbedder
from souq_search.models import SearchOptions, SearchRequest
from souq_search.pagination import SessionStore
from souq_search.parsing.llm import LlmQueryParser
from souq_search.parsing.llm_fixtures import FixtureStore, LlmResult, LlmTransport
from souq_search.pipeline import EmbedService, SearchPipeline
from souq_search.rerank import Reranker

AUTHORING = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "llm" / "_authoring" / "synthetic.yaml"
)
ENTRIES = yaml.safe_load(AUTHORING.read_text(encoding="utf-8"))


class CapturingTransport(LlmTransport):
    last: LlmResult | None = None

    async def create(self, request_body: dict[str, Any], timeout_s: float) -> LlmResult:
        self.last = await super().create(request_body, timeout_s)
        return self.last


@pytest.mark.parametrize("entry", ENTRIES, ids=[e["q"] for e in ENTRIES])
async def test_fixture_replays_through_the_pipeline(entry: dict[str, object]) -> None:
    b = load_budgets().search
    transport = CapturingTransport("replay", store=FixtureStore())
    llm = LlmQueryParser(
        transport,
        "claude-haiku-5-5",
        int(b.llm_max_output_tokens),
        int(b.llm_tokens_per_query),
    )
    pipeline = SearchPipeline(
        backend=FakeBackend(catalog()),
        budgets=b,
        embed=EmbedService(FakeEmbedder()),
        reranker=Reranker(None),
        llm=llm,
        cache=None,
        sessions=SessionStore(None, 60),
    )
    parsed, degraded = await pipeline.parse(
        SearchRequest(q=str(entry["q"]), lang=entry["lang"], options=SearchOptions(rerank=False))  # type: ignore[arg-type]
    )
    assert degraded == [], "fixture missing: regenerate with scripts/make_synthetic_fixtures.py"
    assert parsed.parser == "llm"
    assert transport.last is not None
    answer = json.loads(transport.last.text())
    assert parsed.rewritten == (answer["rewritten_query"].strip() or None)
    if answer["price_max"] is not None:
        assert parsed.filters.price_max == answer["price_max"]
