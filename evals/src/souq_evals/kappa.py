"""Cohen's kappa and the LLM-judge gate.

An LLM judge may label new queries only after it agrees with the maintainer: kappa >= 0.6 measured on at
least 50 (query, product) pairs that the maintainer labelled by hand (``label_source: maintainer``). Without
enough maintainer labels the gate refuses — there is nothing to calibrate against.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

MIN_PAIRS = 50
MIN_KAPPA = 0.6
Pair = tuple[str, str]  # (query id, sku)


def cohen_kappa(a: Sequence[int], b: Sequence[int], labels: Sequence[int] = (0, 1, 2, 3)) -> float:
    """Unweighted Cohen's kappa for two raters over the same items."""
    if len(a) != len(b):
        raise ValueError("raters must label the same items")
    n = len(a)
    if n == 0:
        raise ValueError("no items")
    observed = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[label] * cb[label] for label in labels) / (n * n)
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    kappa: float | None
    n_pairs: int
    reason: str


def judge_gate(
    judge_labels: Mapping[Pair, int],
    maintainer_labels: Mapping[Pair, int],
    min_pairs: int = MIN_PAIRS,
    min_kappa: float = MIN_KAPPA,
) -> GateResult:
    shared = sorted(set(judge_labels) & set(maintainer_labels))
    if len(maintainer_labels) < min_pairs:
        return GateResult(
            False,
            None,
            len(shared),
            f"only {len(maintainer_labels)} maintainer labels; need {min_pairs} to calibrate the judge",
        )
    if len(shared) < min_pairs:
        return GateResult(
            False, None, len(shared), f"judge labelled {len(shared)} of the {min_pairs} required pairs"
        )
    kappa = cohen_kappa([judge_labels[p] for p in shared], [maintainer_labels[p] for p in shared])
    if kappa < min_kappa:
        return GateResult(False, round(kappa, 4), len(shared), f"kappa {kappa:.3f} < {min_kappa}")
    return GateResult(True, round(kappa, 4), len(shared), "ok")
