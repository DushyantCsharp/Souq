"""LLM record/replay (CLAUDE.md "LLM record/replay fixtures").

``tests/fixtures/llm/<key>.json`` with ``key = sha256(model + "\\n" + canonical_json(request_body))[:16]``
and body ``{"model", "request", "response", "usage": {"input_tokens", "output_tokens"}, "recorded_at"}``.

* ``replay`` — read the fixture; a missing fixture raises :class:`FixtureMissing` (the service turns that
  into ``degraded: ["llm_parse"]``, tests see the exception).
* ``record`` — call the API and write the fixture.
* ``live``   — call the API, write nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from souq_common.embeddings import repo_root

LlmMode = Literal["replay", "record", "live"]


class FixtureMissing(LookupError):
    """Replay mode and no recorded response for this exact request."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fixture_key(model: str, request_body: dict[str, Any]) -> str:
    return hashlib.sha256((model + "\n" + canonical_json(request_body)).encode("utf-8")).hexdigest()[:16]


def fixtures_dir() -> Path:
    env = os.environ.get("SOUQ_LLM_FIXTURES_DIR") or os.environ.get("SOUQ_LLM_FIXTURES")
    return Path(env) if env else repo_root() / "tests" / "fixtures" / "llm"


@dataclass(frozen=True)
class LlmResult:
    response: dict[str, Any]
    input_tokens: int
    output_tokens: int
    source: Literal["fixture", "api"]
    recorded_at: str | None = None

    def text(self) -> str:
        """Concatenated text blocks (structured output puts the JSON in a text block)."""
        blocks = self.response.get("content") or []
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


class ApiCaller(Protocol):
    async def __call__(self, request_body: dict[str, Any], timeout_s: float) -> dict[str, Any]: ...


class FixtureStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or fixtures_dir()

    def path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def load(self, model: str, request_body: dict[str, Any]) -> dict[str, Any] | None:
        path = self.path(fixture_key(model, request_body))
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def save(
        self,
        model: str,
        request_body: dict[str, Any],
        response: dict[str, Any],
        input_tokens: int,
        output_tokens: int,
        recorded_at: str | None = None,
    ) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path(fixture_key(model, request_body))
        payload = {
            "model": model,
            "request": request_body,
            "response": response,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "recorded_at": recorded_at or datetime.now(UTC).isoformat(timespec="seconds"),
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path


class AnthropicCaller:
    """Thin async wrapper over the Anthropic Messages API (``client.messages.create``)."""

    def __init__(self, api_key: str | None) -> None:
        import anthropic

        # No SDK retries: the caller's budget is a hard deadline and a retry would blow it.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)

    async def __call__(self, request_body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        message = await self._client.with_options(timeout=timeout_s).messages.create(**request_body)
        if message.stop_reason == "refusal":
            raise RuntimeError("llm refusal")
        data: dict[str, Any] = message.to_dict()
        return data


class LlmTransport:
    def __init__(
        self, mode: LlmMode, store: FixtureStore | None = None, caller: ApiCaller | None = None
    ) -> None:
        self.mode = mode
        self.store = store or FixtureStore()
        self._caller = caller

    async def create(self, request_body: dict[str, Any], timeout_s: float) -> LlmResult:
        model = str(request_body["model"])
        if self.mode == "replay":
            fixture = self.store.load(model, request_body)
            if fixture is None:
                raise FixtureMissing(fixture_key(model, request_body))
            usage = fixture.get("usage") or {}
            return LlmResult(
                response=fixture["response"],
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                source="fixture",
                recorded_at=fixture.get("recorded_at"),
            )
        if self._caller is None:
            raise RuntimeError(f"SOUQ_LLM_MODE={self.mode} needs an API caller (ANTHROPIC_API_KEY)")
        response = await self._caller(request_body, timeout_s)
        usage = response.get("usage") or {}
        in_tok, out_tok = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        if self.mode == "record":
            self.store.save(model, request_body, response, in_tok, out_tok)
        return LlmResult(response=response, input_tokens=in_tok, output_tokens=out_tok, source="api")
