"""Reciprocal Rank Fusion: score(d) = sum_i w_i / (k + rank_i(d)), rank 1-based (Cormack et al. 2009).

Expected values are written out by hand so a silent change to the formula (0-based ranks, a different k,
weights applied to the rank instead of the score) shows up as a numeric diff, not just a re-ordering.
"""

from __future__ import annotations

import pytest

from souq_search.fusion import rrf


def as_dict(fused: list[tuple[str, float]]) -> dict[str, float]:
    return dict(fused)


def ids(fused: list[tuple[str, float]]) -> list[str]:
    return [doc for doc, _ in fused]


def test_two_overlapping_lists_hand_computed() -> None:
    fused = rrf([["a", "b", "c"], ["b", "c", "d"]])
    scores = as_dict(fused)
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 62)
    assert scores["d"] == pytest.approx(1 / 63)
    # 0.032522 > 0.032002 > 0.016393 > 0.015873
    assert ids(fused) == ["b", "c", "a", "d"]


def test_default_k_is_60() -> None:
    assert rrf([["x"]]) == rrf([["x"]], k=60)
    assert as_dict(rrf([["x"]]))["x"] == pytest.approx(1 / 61)


def test_custom_k() -> None:
    scores = as_dict(rrf([["a", "b", "c"]], k=1))
    assert scores == pytest.approx({"a": 1 / 2, "b": 1 / 3, "c": 1 / 4})


def test_single_list_preserves_order() -> None:
    ranking = [f"d{i}" for i in range(100)]
    assert ids(rrf([ranking])) == ranking


def test_weights_scale_each_retriever() -> None:
    fused = rrf([["a", "b"], ["b", "a"]], weights=[2.0, 1.0])
    scores = as_dict(fused)
    assert scores["a"] == pytest.approx(2 / 61 + 1 / 62)
    assert scores["b"] == pytest.approx(2 / 62 + 1 / 61)
    assert ids(fused) == ["a", "b"]


def test_weights_can_flip_the_winner() -> None:
    lists = [["a", "b"], ["b", "a"]]
    assert ids(rrf(lists, weights=[1.0, 3.0]))[0] == "b"
    assert ids(rrf(lists, weights=[3.0, 1.0]))[0] == "a"


def test_unit_weights_equal_no_weights() -> None:
    lists = [["a", "b", "c"], ["c", "a"], ["d"]]
    unweighted = as_dict(rrf(lists))
    weighted = as_dict(rrf(lists, weights=[1.0, 1.0, 1.0]))
    assert weighted == pytest.approx(unweighted)


def test_zero_weight_retriever_contributes_nothing() -> None:
    scores = as_dict(rrf([["a", "b"], ["b", "c"]], weights=[1.0, 0.0]))
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 62)
    assert scores.get("c", 0.0) == pytest.approx(0.0)


def test_tie_between_symmetric_documents() -> None:
    fused = rrf([["a", "b"], ["b", "a"]])
    scores = as_dict(fused)
    assert scores["a"] == pytest.approx(scores["b"])
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert set(ids(fused)) == {"a", "b"}


def test_ties_are_deterministic() -> None:
    lists = [["a", "b", "c"], ["c", "b", "a"], ["b", "a", "c"]]
    first = rrf(lists)
    for _ in range(5):
        assert rrf(lists) == first


def test_disjoint_lists_interleave_by_rank() -> None:
    fused = rrf([["a", "b", "c"], ["x", "y", "z"]])
    scores = as_dict(fused)
    assert len(fused) == 6
    for doc, rank in [("a", 1), ("x", 1), ("b", 2), ("y", 2), ("c", 3), ("z", 3)]:
        assert scores[doc] == pytest.approx(1 / (60 + rank))
    order = ids(fused)
    assert set(order[:2]) == {"a", "x"}
    assert set(order[2:4]) == {"b", "y"}
    assert set(order[4:]) == {"c", "z"}


def test_document_in_every_list_beats_single_list_winner() -> None:
    # "m" is 2nd everywhere; "a" is 1st in one list only.
    fused = rrf([["a", "m"], ["b", "m"], ["c", "m"]])
    assert ids(fused)[0] == "m"
    assert as_dict(fused)["m"] == pytest.approx(3 / 62)


@pytest.mark.parametrize("rankings", [[], [[]], [[], [], []]])
def test_empty_inputs(rankings: list[list[str]]) -> None:
    assert rrf(rankings) == []


def test_one_empty_list_is_ignored() -> None:
    fused = rrf([["a", "b"], []])
    assert ids(fused) == ["a", "b"]
    assert as_dict(fused) == pytest.approx({"a": 1 / 61, "b": 1 / 62})


def test_output_sorted_descending_and_unique() -> None:
    lists = [
        [f"d{i}" for i in range(0, 50)],
        [f"d{i}" for i in range(25, 75)],
        [f"d{i}" for i in range(70, 5, -3)],
    ]
    fused = rrf(lists)
    scores = [s for _, s in fused]
    assert scores == sorted(scores, reverse=True)
    assert len(ids(fused)) == len(set(ids(fused)))
    assert set(ids(fused)) == {d for lst in lists for d in lst}


def test_accepts_tuples() -> None:
    # Signature is Sequence[Sequence[str]]: callers pass tuples of hit ids.
    assert ids(rrf((("a", "b"), ("b",)))) == ["b", "a"]


def test_weights_must_match_rankings() -> None:
    with pytest.raises(ValueError, match="weights"):
        rrf([["a"], ["b"]], weights=[1.0])


def test_duplicate_within_one_list_votes_once_at_its_best_rank() -> None:
    # A retriever that returns the same id twice (e.g. two matching nested fields) must not double its vote.
    scores = as_dict(rrf([["a", "b", "a"]]))
    assert scores == pytest.approx({"a": 1 / 61, "b": 1 / 62})
