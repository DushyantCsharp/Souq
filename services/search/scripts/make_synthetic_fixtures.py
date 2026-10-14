"""Write LLM replay fixtures for the hand-authored answers in tests/fixtures/llm/_authoring/synthetic.yaml.

Request bodies come from souq_search.pipeline.llm_request_for_query, i.e. exactly what the service sends,
so replay keys match. Fixtures are marked ``"recorded_at": "synthetic"``; token usage is an estimate
(~4 chars/token for the English prompt, ~3 for the query line and the JSON answer).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from souq_common.budgets import load_budgets
from souq_common.settings import get_settings
from souq_search.parsing.llm import LlmQueryParser
from souq_search.parsing.llm_fixtures import FixtureStore, LlmTransport, fixtures_dir
from souq_search.pipeline import llm_request_for_query

AUTHORING = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "llm" / "_authoring" / "synthetic.yaml"
)


def main() -> int:
    b = load_budgets().search
    model = get_settings().souq_llm_model
    parser = LlmQueryParser(
        LlmTransport("replay"), model, int(b.llm_max_output_tokens), int(b.llm_tokens_per_query)
    )
    store = FixtureStore(fixtures_dir())
    written = set()
    for entry in yaml.safe_load(AUTHORING.read_text(encoding="utf-8")):
        body = llm_request_for_query(parser, entry["q"], entry["lang"])
        if body is None:
            raise SystemExit(f"rule parser handles {entry['q']!r}; the LLM is never called for it")
        text = json.dumps(entry["answer"], ensure_ascii=False)
        user = body["messages"][0]["content"]
        in_tok = len(body["system"]) // 4 + len(user) // 3 + 12
        out_tok = len(text) // 3 + 1
        key = hashlib.sha256(text.encode()).hexdigest()[:12]
        response = {
            "id": f"msg_synthetic_{key}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        }
        path = store.save(model, body, response, in_tok, out_tok, recorded_at="synthetic")
        written.add(path.name)
        print(f"{path.name}  {entry['q']}")
    for stale in store.directory.glob("*.json"):
        if stale.name not in written and json.loads(stale.read_text()).get("recorded_at") == "synthetic":
            stale.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
