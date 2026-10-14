"""Plausible AED prices per category, generated with Faker and fully deterministic per (seed, sku).

Prices are drawn log-uniformly inside the category's AED band (retail prices are roughly log-normal: many
cheap items, a long expensive tail) and then snapped to charm price points (14.99, 149.00, 1,299.00).
Seeding per SKU instead of per run means a product keeps its price however the seed set is ordered,
filtered or regenerated.
"""

from __future__ import annotations

import hashlib
import math

from faker import Faker

from souq_scripts.taxonomy import Category

DEFAULT_SEED = 20261013
CURRENCY = "AED"

_faker = Faker()


def _sku_seed(seed: int, sku: str) -> int:
    return int.from_bytes(hashlib.blake2b(f"{seed}:{sku}".encode(), digest_size=8).digest(), "big")


def charm(price: float) -> float:
    """Snap to a retail price point: x.99 under AED 100, else a whole number ending in 9."""
    if price < 100:
        return max(0.99, math.floor(price) + 0.99) if price >= 1 else 0.99
    step = 10 if price < 1000 else 100
    return float(max(step, round(price / step) * step) - 1)


def price_for(category: Category, sku: str, seed: int = DEFAULT_SEED) -> float:
    lo, hi = category.price_aed
    _faker.seed_instance(_sku_seed(seed, sku))
    u = _faker.pyfloat(min_value=math.log(lo), max_value=math.log(hi))
    return round(charm(math.exp(u)), 2)
