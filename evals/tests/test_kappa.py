from __future__ import annotations

import pytest

from souq_evals.kappa import MIN_KAPPA, MIN_PAIRS, cohen_kappa, judge_gate
from souq_evals.labelling import maintainer_labels


def test_kappa_known_values() -> None:
    assert cohen_kappa([0, 1, 2, 3], [0, 1, 2, 3]) == pytest.approx(1.0)
    # 2x2 textbook example: po = 0.7, pe = 0.5 -> 0.4
    a = [1] * 5 + [0] * 5
    b = [1, 1, 1, 1, 0, 0, 0, 0, 1, 1]
    assert cohen_kappa(a, b, labels=(0, 1)) == pytest.approx((0.7 - 0.5) / 0.5)


def test_gate_refuses_without_maintainer_labels() -> None:
    result = judge_gate({("q", "s"): 3}, {})
    assert not result.allowed
    assert "maintainer labels" in result.reason


def test_judge_refused_until_maintainer_has_labelled_enough() -> None:
    labels = maintainer_labels()
    if len(labels) < MIN_PAIRS:  # true for the initial golden set: the Arabic slice is still provisional
        assert not judge_gate(dict(labels), labels).allowed


def test_gate_thresholds() -> None:
    pairs = [(f"q{i}", "s") for i in range(MIN_PAIRS)]
    human = {p: i % 4 for i, p in enumerate(pairs)}
    assert judge_gate(dict(human), human).allowed
    noisy = {p: (g + 1) % 4 if i % 2 else g for i, (p, g) in enumerate(human.items())}
    result = judge_gate(noisy, human)
    assert not result.allowed
    assert result.kappa is not None
    assert result.kappa < MIN_KAPPA
    assert not judge_gate({pairs[0]: 1}, human).allowed  # judge must cover >= MIN_PAIRS shared pairs
