"""LLM relevance judge for labelling new queries — only usable once it agrees with the maintainer.

The gate (souq_evals.kappa.judge_gate) runs first: the judge grades the maintainer-labelled pairs, Cohen's
kappa is computed against the maintainer's grades, and below 0.6 (or with fewer than 50 maintainer labels)
the judge refuses to label anything.
"""

from __future__ import annotations

import json
from typing import Any

from souq_evals.kappa import GateResult, Pair, judge_gate
from souq_search.parsing.llm_fixtures import LlmTransport

JUDGE_SYSTEM = (
    "You grade how well a product matches an e-commerce search query (English, Arabic or Arabizi). "
    "3 = exact match, 2 = substitute (close alternative), 1 = complement (useful with it), 0 = irrelevant. "
    "Answer with the grade only, in the JSON schema."
)
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"grade": {"type": "integer", "enum": [0, 1, 2, 3]}},
    "required": ["grade"],
    "additionalProperties": False,
}


class LlmJudge:
    def __init__(self, transport: LlmTransport, model: str) -> None:
        self.transport = transport
        self.model = model

    def request(self, query: str, product: dict[str, Any]) -> dict[str, Any]:
        doc = " | ".join(str(product.get(k) or "") for k in ("title_en", "title_ar", "brand"))
        return {
            "model": self.model,
            "max_tokens": 50,
            "system": JUDGE_SYSTEM,
            "messages": [{"role": "user", "content": f"query: {query}\nproduct: {doc}"}],
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        }

    async def grade(self, query: str, product: dict[str, Any]) -> int:
        result = await self.transport.create(self.request(query, product), timeout_s=30.0)
        return int(json.loads(result.text())["grade"])


async def calibrate(
    judge: LlmJudge | None,
    maintainer: dict[Pair, int],
    queries: dict[str, str],
    products: dict[str, dict[str, Any]],
) -> GateResult:
    """Grade the maintainer-labelled pairs with the judge and apply the kappa gate. With too few
    maintainer labels the gate refuses before making any LLM call."""
    precheck = judge_gate({}, maintainer)
    if precheck.n_pairs == 0 and "maintainer labels" in precheck.reason:
        return precheck
    if judge is None:
        return GateResult(False, None, 0, "no judge configured")
    labels: dict[Pair, int] = {}
    for qid, sku in sorted(maintainer):
        if qid in queries and sku in products:
            labels[(qid, sku)] = await judge.grade(queries[qid], products[sku])
    return judge_gate(labels, maintainer)
