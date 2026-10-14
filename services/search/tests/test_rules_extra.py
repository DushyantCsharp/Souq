"""Rule-parser cases beyond the shared suite: Arabizi, units, brand/price ambiguity, LLM routing."""

from __future__ import annotations

import pytest

from souq_search.parsing.lexicon import install_catalog_values, reset_lexicon
from souq_search.parsing.rules import parse_rules


def test_arabizi_colour_and_price() -> None:
    p = parse_rules("shanta 7amra ta7t 200 dirham", "ar")
    assert p.filters.colours == ["red"]
    assert p.filters.price_max == 200
    assert p.filters.currency == "AED"
    assert p.residual == "shanta"


@pytest.mark.parametrize("text", ["toys for kids from 3 to 5 years", "tv over 55 inch", "laptop under 2 kg"])
def test_measurements_are_not_prices(text: str) -> None:
    p = parse_rules(text, "en")
    assert p.filters.price_min is None
    assert p.filters.price_max is None


def test_multiword_brand_beats_price_operator() -> None:
    p = parse_rules("under armour hoodie under 150 aed", "en")
    assert p.filters.brands == ["Under Armour"]
    assert p.filters.price_max == 150
    assert p.residual == "hoodie"


@pytest.mark.parametrize(
    ("text", "lo", "hi"),
    [
        ("shoes 200-400 aed", 200, 400),
        ("$50", None, 50),
        ("tv 1,500 aed or less", None, 1500),
        ("من 100 الى 300 درهم", 100, 300),
        ("iphone 15 plus", None, None),
        ("shoes 300aed", None, 300),
    ],
)
def test_price_forms(text: str, lo: float | None, hi: float | None) -> None:
    p = parse_rules(text, "en")
    assert (p.filters.price_min, p.filters.price_max) == (lo, hi)


def test_ambiguous_colour_needs_context() -> None:
    assert parse_rules("orange juice", "en").filters.colours is None
    assert parse_rules("orange shirt", "en").filters.colours == ["orange"]
    assert parse_rules("gold watch", "en").filters.colours == ["gold"]


@pytest.mark.parametrize(
    "text",
    [
        "cheap headphones",
        "رخيص سماعات",
        "shoes under two hundred dirhams",
        "tv around 2000 aed",
        "حذاء بحدود ميتين درهم",
    ],
)
def test_constraint_like_routes_to_llm(text: str) -> None:
    assert parse_rules(text, "en").constraint_like


@pytest.mark.parametrize(
    "text",
    [
        "iphone 15 pro max 256gb",
        "kinect xbox one",
        "phone case without logo",
        "توب بدون أكمام",
        "laptop bag fifteen inch",
    ],
)
def test_product_words_do_not_route_to_llm(text: str) -> None:
    assert parse_rules(text, "en").constraint_like == []


def test_catalog_brands_extend_lexicon_but_skip_common_words() -> None:
    try:
        install_catalog_values(brands={"Anker": 40, "Wireless": 99, "Zyx": 1, "Sennheiser": 12})
        assert parse_rules("sennheiser headphones", "en").filters.brands == ["Sennheiser"]
        assert parse_rules("wireless headphones", "en").filters.brands is None
        assert parse_rules("zyx headphones", "en").filters.brands is None  # too few docs
    finally:
        reset_lexicon()


def test_size_forms() -> None:
    assert parse_rules("tshirt xl", "en").filters.sizes == ["XL"]
    assert parse_rules("قميص مقاس كبير", "ar").filters.sizes == ["L"]
    assert parse_rules("sneakers eu 43", "en").filters.sizes == ["43"]
    assert parse_rules("s 42", "en").filters.sizes is None
