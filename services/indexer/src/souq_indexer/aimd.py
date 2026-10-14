"""AIMD (additive-increase / multiplicative-decrease) bulk batch sizing.

OpenSearch signals overload with 429 / ``es_rejected_execution_exception`` when its write thread-pool
queue is full. Halving the batch on every rejection and growing it by a fixed step on every clean bulk
converges on the largest batch the cluster can absorb right now, the same way TCP congestion control
probes for bandwidth.
"""

from __future__ import annotations


class AimdController:
    def __init__(
        self,
        initial: int = 1000,
        min_size: int = 50,
        max_size: int = 5000,
        additive_step: int = 100,
        decrease_factor: float = 0.5,
        *,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 30.0,
    ) -> None:
        if min_size < 1 or max_size < min_size:
            raise ValueError("need 1 <= min_size <= max_size")
        if not 0.0 < decrease_factor < 1.0:
            raise ValueError("decrease_factor must be in (0, 1)")
        if additive_step < 1:
            raise ValueError("additive_step must be >= 1")
        self.min_size = min_size
        self.max_size = max_size
        self.additive_step = additive_step
        self.decrease_factor = decrease_factor
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self._size = max(min_size, min(max_size, initial))

    @property
    def batch_size(self) -> int:
        return self._size

    def on_success(self) -> None:
        self._size = min(self.max_size, self._size + self.additive_step)

    def on_rejection(self) -> None:
        self._size = max(self.min_size, int(self._size * self.decrease_factor))

    def backoff_seconds(self, consecutive_rejections: int) -> float:
        """Exponential backoff (no jitter: callers add their own if they need decorrelation)."""
        if consecutive_rejections <= 0:
            return 0.0
        # Cap the exponent too, so a long outage cannot overflow the float.
        exponent = min(consecutive_rejections - 1, 32)
        return float(min(self.backoff_max_s, self.backoff_base_s * (2**exponent)))
