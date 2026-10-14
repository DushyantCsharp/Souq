"""Golden queries with graded judgments (0-3) and per-judgment label provenance."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from souq_evals.paths import GOLDEN, SLICES

# "claude": graded in-session by Claude (a model), at the maintainer's request — not a human label.
LabelSource = Literal["esci", "provisional", "maintainer", "claude", "llm_judge"]
SLICE_LANG = {"en": "en", "ar": "ar", "arabizi_mixed": "auto"}
RELEVANT_GRADE = 2  # Exact or Substitute counts as relevant for Recall@k and MRR


@dataclass
class Judgment:
    sku: str
    grade: int
    label_source: LabelSource


@dataclass
class GoldenQuery:
    id: str
    slice: str
    q: str
    lang: str
    kind: str
    source_query_id: int | None
    source_query: str | None
    judgments: list[Judgment] = field(default_factory=list)

    def grades(self) -> dict[str, int]:
        return {j.sku: j.grade for j in self.judgments}

    def relevant(self) -> set[str]:
        return {j.sku for j in self.judgments if j.grade >= RELEVANT_GRADE}

    def label_sources(self) -> Counter[str]:
        return Counter(j.label_source for j in self.judgments)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GoldenQuery:
        raw: list[dict[str, Any]] = d.pop("judgments", [])
        q = cls(**d)
        q.judgments = [Judgment(**j) for j in raw]
        return q


def slice_path(slice_name: str, directory: Path = GOLDEN) -> Path:
    return directory / f"{slice_name}.jsonl"


def load_golden(directory: Path = GOLDEN, slices: tuple[str, ...] = SLICES) -> list[GoldenQuery]:
    out: list[GoldenQuery] = []
    for name in slices:
        path = slice_path(name, directory)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(GoldenQuery.from_dict(json.loads(line)))
    return out


def write_slice(queries: list[GoldenQuery], slice_name: str, directory: Path = GOLDEN) -> Path:
    path = slice_path(slice_name, directory)
    path.write_text("".join(q.to_json() + "\n" for q in queries if q.slice == slice_name), encoding="utf-8")
    return path
