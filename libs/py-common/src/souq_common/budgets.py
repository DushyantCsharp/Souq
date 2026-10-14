"""Latency budgets loaded from config/budgets.yaml — the only place budgets are defined."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


class Budgets:
    """Read-only attribute view over the YAML tree: ``load_budgets().search.rerank_ms``."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        try:
            value = self._data[name]
        except KeyError as exc:
            raise AttributeError(f"no budget named {name!r}") from exc
        return Budgets(value) if isinstance(value, dict) else value

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)


def config_dir() -> Path:
    env = os.environ.get("SOUQ_CONFIG_DIR")
    if env:
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "budgets.yaml"
        if candidate.exists():
            return candidate.parent
    raise FileNotFoundError("config/budgets.yaml not found; set SOUQ_CONFIG_DIR")


@lru_cache(maxsize=1)
def load_budgets() -> Budgets:
    with (config_dir() / "budgets.yaml").open(encoding="utf-8") as fh:
        return Budgets(yaml.safe_load(fh))
