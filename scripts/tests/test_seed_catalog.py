from __future__ import annotations

import pytest

from souq_scripts.prices import charm, price_for
from souq_scripts.taxonomy import FALLBACK, TAXONOMY, canonical_colour, classify, extract_size


@pytest.mark.parametrize(
    ("title", "path"),
    [
        ("Sony WH-1000XM5 Wireless Noise Canceling Headphones, Black", "Electronics > Audio > Headphones"),
        ("Spigen Tough Armor Case for iPhone 15 Pro Max", "Electronics > Mobile Phones > Cases & Covers"),
        ("Apple iPhone 15 Pro, 256GB, Black Titanium", "Electronics > Mobile Phones > Smartphones"),
        (
            "Espressione Stainless Steel Espresso and Coffee Maker, 1.5 L",
            "Home & Kitchen > Kitchen > Small Appliances",
        ),
        ("Bicycle Guardians Playing Cards (Pack of 2)", "Toys & Games > Board Games"),
        ("Ant-Man and the Wasp (4K UHD)", "Media > Movies & Music"),
        ("xyzzy plugh", FALLBACK.path),
    ],
)
def test_classify(title: str, path: str) -> None:
    assert classify(title).path == path


@pytest.mark.parametrize(
    ("raw", "canon"),
    [("Space Gray", "grey"), ("Navy Blue/White", "blue"), ("Black/Red", "black"), ("Rose Gold", "pink"),
     ("Off-White", "white"), ("Multicolor", "multicolour"), ("Unknown", None), (None, None)],
)  # fmt: skip
def test_canonical_colour(raw: str | None, canon: str | None) -> None:
    assert canonical_colour(raw) == canon


def test_extract_size() -> None:
    assert extract_size("GUESS Women's Trenchcoat, Black, X-Large", "apparel") == "XL"
    assert extract_size("Mens Polo Shirt Medium", "apparel") == "M"
    assert extract_size("Running Shoe Size 10.5 Wide", "shoe") == "10.5"
    assert extract_size("Large Coffee Mug", "none") is None


def test_prices_are_deterministic_plausible_and_charm_priced() -> None:
    for cat in TAXONOMY:
        lo, hi = cat.price_aed
        prices = [price_for(cat, f"SKU{i}") for i in range(200)]
        assert prices == [price_for(cat, f"SKU{i}") for i in range(200)]
        assert all(0.99 <= p <= hi * 1.06 and p >= min(lo, 100) * 0.9 for p in prices), cat.path
        assert all(round(p * 100) % 100 in (99, 0) for p in prices)
        assert len(set(prices)) > 5, f"{cat.path}: prices should vary"


def test_charm() -> None:
    assert charm(14.2) == 14.99
    assert charm(152) == 149.0
    assert charm(1340) == 1299.0
    assert charm(0.4) == 0.99
