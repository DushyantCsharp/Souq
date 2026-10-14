"""LLM cost from config/pricing.yaml (the only place prices live)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml

from souq_common.budgets import config_dir


@lru_cache(maxsize=1)
def load_pricing() -> dict[str, Any]:
    with (config_dir() / "pricing.yaml").open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """USD for one request, or None when the model is not on the price sheet."""
    sheet = load_pricing().get("models", {}).get(model)
    if not sheet:
        return None
    in_rate = float(sheet["input_per_mtok"])
    out_rate = float(sheet["output_per_mtok"])
    threshold = sheet.get("long_context_threshold_tokens")
    if threshold is not None and input_tokens > int(threshold):
        in_rate = float(sheet.get("long_context_input_per_mtok", in_rate))
        out_rate = float(sheet.get("long_context_output_per_mtok", out_rate))
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
