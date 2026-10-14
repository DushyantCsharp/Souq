"""Record/replay contract (CLAUDE.md): key derivation, replay misses, record writes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from souq_search.parsing.llm import OUTPUT_SCHEMA, LlmOutput, LlmParseError, LlmQueryParser, to_filters
from souq_search.parsing.llm_fixtures import (
    FixtureMissing,
    FixtureStore,
    LlmTransport,
    canonical_json,
    fixture_key,
)
from souq_search.parsing.rules import parse_rules


def test_fixture_key_matches_contract() -> None:
    body = {"model": "m", "messages": [{"role": "user", "content": "سماعات"}], "max_tokens": 10}
    expected = hashlib.sha256(("m\n" + canonical_json(body)).encode()).hexdigest()[:16]
    assert fixture_key("m", body) == expected
    assert canonical_json({"b": 1, "a": "ع"}) == '{"a":"ع","b":1}'


async def test_replay_missing_fixture_raises(tmp_path: Path) -> None:
    transport = LlmTransport("replay", store=FixtureStore(tmp_path))
    with pytest.raises(FixtureMissing):
        await transport.create({"model": "m", "messages": []}, 1.0)


async def test_record_writes_fixture_then_replays(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    async def caller(body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        calls.append(body)
        return {"content": [{"type": "text", "text": "{}"}], "usage": {"input_tokens": 7, "output_tokens": 3}}

    store = FixtureStore(tmp_path)
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    rec = await LlmTransport("record", store=store, caller=caller).create(body, 1.0)
    assert rec.source == "api"
    assert calls
    saved = json.loads(next(iter(store.directory.glob("*.json"))).read_text())
    assert set(saved) == {"model", "request", "response", "usage", "recorded_at"}
    replayed = await LlmTransport("replay", store=store).create(body, 1.0)
    assert replayed.source == "fixture"
    assert replayed.input_tokens == 7


def test_request_is_strict_schema_and_within_token_budget() -> None:
    parser = LlmQueryParser(
        LlmTransport("replay"), "claude-haiku-5-5", max_output_tokens=300, token_budget=1500
    )
    body = parser.build_request("cheap nike shoes", "en", parse_rules("cheap nike shoes", "en"))
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] is OUTPUT_SCHEMA
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    assert set(OUTPUT_SCHEMA["required"]) == set(OUTPUT_SCHEMA["properties"])
    assert "temperature" not in body  # Haiku 5.5 rejects non-default sampling parameters
    assert len(body["system"]) // 3 + 300 < 1500


def test_oversize_prompt_is_refused() -> None:
    parser = LlmQueryParser(LlmTransport("replay"), "m", max_output_tokens=300, token_budget=400)
    with pytest.raises(LlmParseError):
        parser.build_request("x", "en", parse_rules("x", "en"))


def test_llm_output_is_sanitised() -> None:
    out = LlmOutput(
        rewritten_query="shoes",
        price_min=300,
        price_max=100,
        currency="XXX",
        brands=["nike", " "],
        colours=["red", "chartreuse"],
        sizes=["xl"],
    )
    f = to_filters(out)
    assert (f.price_min, f.price_max) == (100, 300)
    assert f.currency is None
    assert f.brands == ["Nike"]
    assert f.colours == ["red"]
    assert f.sizes == ["XL"]
