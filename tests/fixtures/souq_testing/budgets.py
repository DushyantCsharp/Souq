"""Optional latency-budget scaling for integration runs on an overloaded machine.

The suite runs against the real ``config/budgets.yaml`` by default. On a shared dev VM under heavy load,
``SOUQ_IT_BUDGET_SCALE=4`` (say) runs the stack — and the tests' own budget assertions — against a copy of
``config/`` with the ``search`` request-path ``*_ms`` budgets multiplied, so a slow neighbour does not
masquerade as a stage timeout. Pipeline cadences (relay poll, indexer linger) are left alone: scaling
those would only slow the pipeline down. Relative checks ("degraded within the rerank budget") stay
meaningful because both sides read the same scaled file. Never set in CI.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from souq_testing.paths import CONFIG_DIR


def budget_scale() -> float:
    raw = os.environ.get("SOUQ_IT_BUDGET_SCALE", "1")
    try:
        scale = float(raw)
    except ValueError as exc:
        raise ValueError(f"SOUQ_IT_BUDGET_SCALE must be a number, got {raw!r}") from exc
    if scale < 1:
        raise ValueError("SOUQ_IT_BUDGET_SCALE must be >= 1 (budgets are only ever relaxed)")
    return scale


def _scale(node: Any, scale: float) -> Any:
    if isinstance(node, dict):
        return {
            k: (v * scale if isinstance(v, int | float) and str(k).endswith("_ms") else _scale(v, scale))
            for k, v in node.items()
        }
    return node


def config_dir_for_tests() -> Path:
    """``config/`` itself, or a temp copy with scaled ``*_ms`` budgets."""
    scale = budget_scale()
    if scale == 1:
        return CONFIG_DIR
    out = Path(tempfile.mkdtemp(prefix="souq-it-config-"))
    shutil.copytree(CONFIG_DIR, out, dirs_exist_ok=True)
    data = yaml.safe_load((CONFIG_DIR / "budgets.yaml").read_text(encoding="utf-8"))
    data["search"] = _scale(data["search"], scale)
    (out / "budgets.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return out
