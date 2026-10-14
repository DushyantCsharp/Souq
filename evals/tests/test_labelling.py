from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from souq_evals import golden as g
from souq_evals import labelling


def test_export_import_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gdir = tmp_path / "golden"
    shutil.copytree(g.GOLDEN, gdir)
    monkeypatch.setattr(labelling, "load_golden", lambda: g.load_golden(gdir))
    monkeypatch.setattr(labelling, "write_slice", lambda qs, s: g.write_slice(qs, s, gdir))
    first = g.load_golden(gdir)[60]  # first Arabic query
    products = {j.sku: {"title_en": "t", "title_ar": "ع", "brand": "b"} for j in first.judgments}
    out = labelling.export_csv("ar", products, tmp_path / "ar.csv", retrieved={first.id: ["NEW1"]})
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert {"candidate_source", "provisional_grade", "grade"} <= set(rows[0])
    mine = [r for r in rows if r["query_id"] == first.id]
    mine[0]["grade"] = "3"
    extra = next(r for r in mine if r["sku"] == "NEW1")
    extra["grade"] = "1"
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=labelling.COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    assert labelling.import_csv(out) == {"ar": 2}
    updated = next(q for q in g.load_golden(gdir) if q.id == first.id)
    sources = {j.sku: (j.grade, j.label_source) for j in updated.judgments}
    assert sources[mine[0]["sku"]] == (3, "maintainer")
    assert sources["NEW1"] == (1, "maintainer")
