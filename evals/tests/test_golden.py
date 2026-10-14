"""Integrity of the committed golden set and filter-parse cases."""

from __future__ import annotations

from collections import Counter

from souq_evals.golden import load_golden
from souq_evals.harness import load_filter_cases


def test_slice_sizes_and_unique_ids() -> None:
    golden = load_golden()
    assert Counter(q.slice for q in golden) == {"en": 60, "ar": 60, "arabizi_mixed": 30}
    assert len({q.id for q in golden}) == 150


def test_every_query_has_graded_relevant_judgments() -> None:
    for q in load_golden():
        assert q.judgments, q.id
        assert all(j.grade in (0, 1, 2, 3) for j in q.judgments)
        assert q.relevant(), f"{q.id} has no grade>=2 judgment"


def test_arabic_slice_is_flagged_provisional_until_hand_labelled() -> None:
    for q in load_golden():
        sources = set(q.label_sources())
        if q.slice == "ar" or q.kind in ("mixed", "arabizi"):
            assert sources <= {"provisional", "maintainer", "claude", "llm_judge"}
        elif q.kind == "misspelled":
            # ESCI's human labels stay on the pairs ESCI judged; Claude only graded the extra retrieved pairs.
            assert "esci" in sources
            assert sources <= {"esci", "maintainer", "claude"}
        else:
            assert sources <= {"esci", "maintainer"}


def test_filter_cases() -> None:
    cases = load_filter_cases()
    assert len(cases) == 80
    assert len({c["id"] for c in cases}) == 80
    for c in cases:
        assert set(c["expected"]) == {
            "price_min",
            "price_max",
            "currency",
            "brands",
            "colours",
            "sizes",
            "category",
        }
        assert c["lang"] in ("en", "ar", "auto")
