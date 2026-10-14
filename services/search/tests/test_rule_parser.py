"""Rule parser: price / brand / colour / size extraction in English and Arabic.

The rule path is the hot path (no LLM) so these queries are the ones the eval filter-parse set leans on.
Colours are canonical lowercase English whatever the query language (OpenSearch ``colour`` is a keyword).
"""

from __future__ import annotations

from typing import Any

import pytest

from souq_search.parsing.rules import parse_rules


def field(filters: Any, name: str) -> Any:
    """Filters may be a pydantic model or a plain dict depending on the layer; read either."""
    if filters is None:
        return None
    if isinstance(filters, dict):
        return filters.get(name)
    return getattr(filters, name, None)


def lowered(values: Any) -> list[str]:
    return sorted(str(v).lower() for v in (values or []))


def num(value: Any) -> float | None:
    return None if value is None else float(value)


# --------------------------------------------------------------------------- headline queries


def test_english_colour_and_price_ceiling_with_currency() -> None:
    p = parse_rules("black running shoes under 300 AED", "en")
    assert field(p.filters, "colours") == ["black"]
    assert num(field(p.filters, "price_max")) == 300
    assert field(p.filters, "price_min") is None
    assert field(p.filters, "currency") == "AED"
    assert p.residual.split() == ["running", "shoes"]


def test_arabic_colour_and_price_ceiling_with_currency() -> None:
    p = parse_rules("حذاء جري اسود اقل من 300 درهم", "ar")
    assert field(p.filters, "colours") == ["black"]
    assert num(field(p.filters, "price_max")) == 300
    assert field(p.filters, "price_min") is None
    assert field(p.filters, "currency") == "AED"
    assert p.residual.split() == ["حذاء", "جري"]


def test_arabic_with_tashkeel_hamza_and_indic_digits() -> None:
    # Same query typed with diacritics, a hamza'd alef and Arabic-Indic digits.
    p = parse_rules("حِذاء جري أسود أقل من ٣٠٠ درهم", "ar")
    assert field(p.filters, "colours") == ["black"]
    assert num(field(p.filters, "price_max")) == 300
    assert field(p.filters, "currency") == "AED"


@pytest.mark.parametrize(
    ("text", "lang"),
    [("between 100 and 200", "en"), ("headphones between 100 and 200", "en"), ("سماعات بين 100 و 200", "ar")],
)
def test_price_range(text: str, lang: str) -> None:
    p = parse_rules(text, lang)
    assert num(field(p.filters, "price_min")) == 100
    assert num(field(p.filters, "price_max")) == 200


def test_brand_and_size() -> None:
    p = parse_rules("nike size 42", "en")
    assert lowered(field(p.filters, "brands")) == ["nike"]
    assert lowered(field(p.filters, "sizes")) == ["42"]
    assert p.residual.strip() == ""


@pytest.mark.parametrize("text", ["مقاس 42", "حذاء مقاس 42", "حذاء مقاس ٤٢"])
def test_arabic_size(text: str) -> None:
    p = parse_rules(text, "ar")
    assert lowered(field(p.filters, "sizes")) == ["42"]


def test_latin_brand_inside_arabic_query() -> None:
    # Gulf shoppers routinely type brand names in Latin inside Arabic queries.
    p = parse_rules("حذاء nike مقاس 42", "ar")
    assert lowered(field(p.filters, "brands")) == ["nike"]
    assert lowered(field(p.filters, "sizes")) == ["42"]
    assert p.residual.split() == ["حذاء"]


# --------------------------------------------------------------------------- price vocabulary


@pytest.mark.parametrize(
    ("text", "lang", "price_min", "price_max"),
    [
        ("shoes under 300", "en", None, 300),
        ("shoes less than 300", "en", None, 300),
        ("shoes below 300", "en", None, 300),
        ("tv over 2000", "en", 2000, None),
        ("tv more than 2000", "en", 2000, None),
        ("tv above 2000", "en", 2000, None),
        ("حذاء اقل من 300", "ar", None, 300),
        ("تلفزيون اكثر من 2000", "ar", 2000, None),
        ("تلفزيون أكثر من ٢٠٠٠", "ar", 2000, None),
    ],
)
def test_price_bounds(text: str, lang: str, price_min: float | None, price_max: float | None) -> None:
    p = parse_rules(text, lang)
    assert num(field(p.filters, "price_min")) == price_min
    assert num(field(p.filters, "price_max")) == price_max


def test_bare_numbers_are_not_prices() -> None:
    # "iphone 15" must not become a price filter; model numbers are product text.
    p = parse_rules("iphone 15", "en")
    assert field(p.filters, "price_min") is None
    assert field(p.filters, "price_max") is None
    assert "15" in p.residual.split()


# --------------------------------------------------------------------------- colours, ar → canonical en


@pytest.mark.parametrize(
    ("word", "canonical"),
    [
        ("اسود", "black"),
        ("أسود", "black"),
        ("سوداء", "black"),
        ("ابيض", "white"),
        ("أبيض", "white"),
        ("بيضاء", "white"),
        ("احمر", "red"),
        ("أحمر", "red"),
        ("حمراء", "red"),
        ("ازرق", "blue"),
        ("أزرق", "blue"),
        ("زرقاء", "blue"),
        ("اخضر", "green"),
        ("أخضر", "green"),
        ("خضراء", "green"),
    ],
)
def test_arabic_colour_maps_to_canonical_english(word: str, canonical: str) -> None:
    p = parse_rules(f"حقيبة {word}", "ar")
    assert field(p.filters, "colours") == [canonical]
    assert word not in p.residual


@pytest.mark.parametrize(
    ("word", "canonical"),
    [("black", "black"), ("Black", "black"), ("white", "white"), ("red", "red"), ("blue", "blue")],
)
def test_english_colour_is_canonical_lowercase(word: str, canonical: str) -> None:
    p = parse_rules(f"{word} backpack", "en")
    assert field(p.filters, "colours") == [canonical]
    assert p.residual.split() == ["backpack"]


# --------------------------------------------------------------------------- result shape


def test_plain_query_has_no_filters() -> None:
    p = parse_rules("wireless headphones", "en")
    for name in ("price_min", "price_max", "currency", "category"):
        assert field(p.filters, name) is None
    for name in ("brands", "colours", "sizes"):
        assert not field(p.filters, name)
    assert p.residual.split() == ["wireless", "headphones"]


def test_result_shape() -> None:
    p = parse_rules("black running shoes under 300 AED", "en")
    assert isinstance(p.residual, str)
    assert isinstance(p.unparsed_tokens, list)
    assert all(isinstance(t, str) for t in p.unparsed_tokens)
    assert isinstance(p.constraint_like, list)
    assert all(isinstance(t, str) for t in p.constraint_like)


def test_fully_parsed_query_has_no_constraint_like_leftovers() -> None:
    # constraint_like is the signal that sends a query to the LLM parser; a cleanly parsed
    # query must not trip it, or the cheap path would never be taken.
    p = parse_rules("black running shoes under 300 AED", "en")
    assert p.constraint_like == []
