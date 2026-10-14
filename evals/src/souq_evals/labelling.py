"""Maintainer labelling workflow (CSV round trip).

    python -m souq_evals label export --slice ar            # -> evals/labelling/ar_to_label.csv
    (fill the `grade` column: 3 exact, 2 substitute, 1 complement, 0 irrelevant; leave blank to skip)
    python -m souq_evals label import evals/labelling/ar_to_label.csv

Imported grades become ``label_source: maintainer`` (or ``--source claude`` for grades written by Claude at
the maintainer's request — kept distinct so a model's labels are never reported as human) and survive
``scripts/build_golden.py`` rebuilds. ``--with-retrieved`` adds the system's own top results that ESCI never
judged, so the pool is not biased towards the English query's candidates.
"""

from __future__ import annotations

import csv
import random
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from souq_evals.golden import GoldenQuery, Judgment, LabelSource, load_golden, write_slice
from souq_evals.paths import LABELLING

COLUMNS = [
    "query_id",
    "query",
    "source_query",
    "sku",
    "title_en",
    "title_ar",
    "brand",
    "candidate_source",
    "provisional_grade",
    "grade",
]


def export_csv(
    slice_name: str,
    products: Mapping[str, Mapping[str, Any]],
    out: Path | None = None,
    retrieved: Mapping[str, list[str]] | None = None,
) -> Path:
    out = out or LABELLING / f"{slice_name}_to_label.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    queries = [q for q in load_golden() if q.slice == slice_name]
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for q in queries:
            judged = {j.sku: j for j in q.judgments}
            rows: list[tuple[str, str, int | None, int | None]] = [
                (
                    j.sku,
                    "esci_judged",
                    # Claude's grades show as provisional: pre-filling `grade` would anchor a human reviewer.
                    j.grade if j.label_source != "maintainer" else None,
                    j.grade if j.label_source == "maintainer" else None,
                )
                for j in q.judgments
            ]
            rows += [(s, "retrieved", None, None) for s in (retrieved or {}).get(q.id, []) if s not in judged]
            for sku, source, provisional, grade in rows:
                p = products.get(sku, {})
                writer.writerow(
                    {
                        "query_id": q.id,
                        "query": q.q,
                        "source_query": q.source_query or "",
                        "sku": sku,
                        "title_en": p.get("title_en") or "",
                        "title_ar": p.get("title_ar") or "",
                        "brand": p.get("brand") or "",
                        "candidate_source": source,
                        "provisional_grade": "" if provisional is None else provisional,
                        "grade": "" if grade is None else grade,
                    }
                )
    return out


def import_csv(path: Path, source: LabelSource = "maintainer") -> dict[str, int]:
    """Apply filled-in grades; returns {slice: labels imported}."""
    golden = {q.id: q for q in load_golden()}
    imported: dict[str, int] = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            grade = (row.get("grade") or "").strip()
            if not grade:
                continue
            value = int(grade)
            if value not in (0, 1, 2, 3):
                raise ValueError(f"grade must be 0-3, got {value} for {row['query_id']}/{row['sku']}")
            q: GoldenQuery = golden[row["query_id"]]
            for j in q.judgments:
                if j.sku == row["sku"]:
                    j.grade, j.label_source = value, source
                    break
            else:
                q.judgments.append(Judgment(sku=row["sku"], grade=value, label_source=source))
            imported[q.slice] = imported.get(q.slice, 0) + 1
    for slice_name in imported:
        write_slice(list(golden.values()), slice_name)
    return imported


def reference_labels(source: LabelSource = "maintainer") -> dict[tuple[str, str], int]:
    return {(q.id, j.sku): j.grade for q in load_golden() for j in q.judgments if j.label_source == source}


def maintainer_labels() -> dict[tuple[str, str], int]:
    return reference_labels("maintainer")


def stratified_sample(
    labels: Mapping[tuple[str, str], int], n: int, seed: int = 0
) -> dict[tuple[str, str], int]:
    """n pairs spread evenly over grades 0-3 (kappa is uninformative if one grade dominates), seeded."""
    rng = random.Random(seed)
    by_grade: dict[int, list[tuple[str, str]]] = {}
    for pair, grade in sorted(labels.items()):
        by_grade.setdefault(grade, []).append(pair)
    for pairs in by_grade.values():
        rng.shuffle(pairs)
    picked: dict[tuple[str, str], int] = {}
    while len(picked) < min(n, len(labels)):
        for grade in sorted(by_grade):
            if by_grade[grade] and len(picked) < n:
                picked[by_grade[grade].pop()] = grade
    return picked


def write_reference_csv(labels: Mapping[tuple[str, str], int], queries: Mapping[str, str], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["query_id", "query", "sku", "reference_grade"])
        for (qid, sku), grade in sorted(labels.items()):
            writer.writerow([qid, queries.get(qid, ""), sku, grade])
    return out
