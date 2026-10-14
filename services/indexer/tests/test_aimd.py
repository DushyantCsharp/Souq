"""AIMD bulk-size controller: additive increase on success, multiplicative decrease on rejection
(429 / es_rejected_execution_exception), clamped to [min_size, max_size]; exponential backoff with a cap.
"""

from __future__ import annotations

import itertools
import math

import pytest

from souq_indexer.aimd import AimdController


def test_contract_defaults() -> None:
    c = AimdController()
    assert c.batch_size == 1000
    c.on_rejection()
    assert c.batch_size == 500
    c.on_success()
    assert c.batch_size == 600


def test_batch_size_is_int() -> None:
    c = AimdController(initial=101, decrease_factor=0.5)
    c.on_rejection()
    assert isinstance(c.batch_size, int)
    c.on_success()
    assert isinstance(c.batch_size, int)


def test_halves_on_each_rejection() -> None:
    c = AimdController(initial=1000, min_size=50, max_size=5000)
    seen = []
    for _ in range(3):
        c.on_rejection()
        seen.append(c.batch_size)
    assert seen == [500, 250, 125]
    c.on_rejection()
    # 62.5 — rounding direction is an implementation detail, but it must stay within one unit.
    assert c.batch_size in (62, 63)


def test_floor_at_min_size() -> None:
    c = AimdController(initial=1000, min_size=50, max_size=5000)
    for _ in range(50):
        c.on_rejection()
        assert c.batch_size >= 50
    assert c.batch_size == 50


def test_additive_growth() -> None:
    c = AimdController(initial=1000, min_size=50, max_size=5000, additive_step=100)
    for expected in (1100, 1200, 1300):
        c.on_success()
        assert c.batch_size == expected


def test_ceiling_at_max_size() -> None:
    c = AimdController(initial=4950, min_size=50, max_size=5000, additive_step=100)
    c.on_success()
    assert c.batch_size == 5000
    for _ in range(100):
        c.on_success()
    assert c.batch_size == 5000


def test_recovers_additively_after_collapse() -> None:
    c = AimdController(initial=1000, min_size=50, max_size=5000, additive_step=100)
    for _ in range(10):
        c.on_rejection()
    assert c.batch_size == 50
    c.on_success()
    assert c.batch_size == 150
    c.on_success()
    assert c.batch_size == 250


def test_custom_parameters() -> None:
    c = AimdController(initial=200, min_size=10, max_size=300, additive_step=40, decrease_factor=0.25)
    c.on_rejection()
    assert c.batch_size == 50
    c.on_success()
    assert c.batch_size == 90
    for _ in range(10):
        c.on_success()
    assert c.batch_size == 300
    for _ in range(10):
        c.on_rejection()
    assert c.batch_size == 10


def test_sawtooth_converges_below_capacity() -> None:
    # Simulate a cluster that rejects any batch above 1500 docs: AIMD must oscillate under it and never
    # leave [min, max].
    c = AimdController(initial=1000, min_size=50, max_size=5000, additive_step=100)
    sizes = []
    for _ in range(200):
        if c.batch_size > 1500:
            c.on_rejection()
        else:
            c.on_success()
        sizes.append(c.batch_size)
        assert 50 <= c.batch_size <= 5000
    tail = sizes[-50:]
    assert max(tail) <= 1600
    assert min(tail) >= 750


# --------------------------------------------------------------------------- backoff


def test_backoff_grows_with_consecutive_rejections() -> None:
    c = AimdController()
    delays = [c.backoff_seconds(n) for n in range(1, 6)]
    assert all(d > 0 for d in delays)
    assert all(b >= a for a, b in itertools.pairwise(delays)), delays
    assert delays[-1] > delays[0]


def test_backoff_is_capped() -> None:
    c = AimdController()
    capped = c.backoff_seconds(1_000)
    assert math.isfinite(capped)
    assert capped == pytest.approx(c.backoff_seconds(100))
    for n in range(1, 60):
        assert c.backoff_seconds(n) <= capped + 1e-9
    # A stuck cluster must not park the consumer for longer than a poll interval budget allows.
    assert capped <= 120


def test_backoff_does_not_overflow_on_huge_counts() -> None:
    c = AimdController()
    assert math.isfinite(c.backoff_seconds(10_000))


def test_no_backoff_without_rejections() -> None:
    c = AimdController()
    assert c.backoff_seconds(0) == pytest.approx(0.0)


@pytest.mark.parametrize(("initial", "expected"), [(10, 50), (99_999, 5000)])
def test_initial_is_clamped_to_bounds(initial: int, expected: int) -> None:
    assert AimdController(initial=initial, min_size=50, max_size=5000).batch_size == expected


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"min_size": 0}, "min_size"),
        ({"min_size": 100, "max_size": 50}, "min_size"),
        ({"decrease_factor": 1.0}, "decrease_factor"),
        ({"decrease_factor": 0.0}, "decrease_factor"),
        ({"additive_step": 0}, "additive_step"),
    ],
)
def test_rejects_parameters_that_cannot_converge(kwargs: dict[str, float], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        AimdController(**kwargs)  # type: ignore[arg-type]
