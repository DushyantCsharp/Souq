from __future__ import annotations

import pytest

from souq_scripts.category import category_levels, slugify_category, slugify_label
from souq_scripts.taxonomy import CATEGORIES_BY_LTREE, TAXONOMY

CASES: list[tuple[str, str | None]] = [
    ("Electronics > Audio > Headphones", "electronics.audio.headphones"),
    ("Clothing, Shoes & Jewelry > Men > Shoes", "clothing_shoes_and_jewelry.men.shoes"),
    ("  Home & Kitchen  >  Kitchen  ", "home_and_kitchen.kitchen"),
    ("TV & Video", "tv_and_video"),
    ("Électronique > Audio", "lectronique.audio"),  # ASCII only, same as the SQL function
    ("Toys & Games > 3D Puzzles", "toys_and_games.3d_puzzles"),
    ("Baby>Kids", "baby.kids"),
    ("Electronics >  > Headphones", None),
    ("Electronics >", None),
    (">", None),
    ("", None),
    ("   ", None),
    ("!!! > ???", None),
    ("سماعات > صوت", None),  # nothing ASCII left: rejected rather than guessed
    ("A > B > C > D > E > F > G > H > I > J", "a.b.c.d.e.f.g.h.i.j"),
    ("A > B > C > D > E > F > G > H > I > J > K", None),  # > 10 levels
    ("x" * 255, "x" * 255),
    ("x" * 256, None),
]


@pytest.mark.parametrize(("path", "expected"), CASES)
def test_slugify_category(path: str, expected: str | None) -> None:
    assert slugify_category(path) == expected


def test_labels_are_valid_ltree_labels() -> None:
    for cat in TAXONOMY:
        for label in cat.ltree.split("."):
            assert label
            assert all(c.isascii() and (c.isalnum() or c == "_") for c in label)


def test_slugify_label_never_lowers_non_ascii() -> None:
    # str.lower() would turn "İ" into "i" + combining dot; Postgres translate() leaves it alone.
    assert slugify_label("İstanbul") == "stanbul"


def test_category_levels_are_prefixes() -> None:
    assert category_levels("electronics.audio.headphones") == [
        "electronics",
        "electronics.audio",
        "electronics.audio.headphones",
    ]


def test_taxonomy_ltrees_are_unique() -> None:
    assert len(CATEGORIES_BY_LTREE) == len(TAXONOMY) + 1


@pytest.mark.integration
def test_python_slugify_matches_sql_category_ltree(migrated_db: str) -> None:
    import psycopg

    paths = [p for p, _ in CASES] + [c.path for c in TAXONOMY] + ["Ünïcödé & Co > Größe", "a\tb > c\nd"]
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute(
            "SELECT p, category_ltree(p)::text FROM unnest(%s::text[]) WITH ORDINALITY AS t(p, i) ORDER BY i",
            (paths,),
        ).fetchall()
    assert [(p, slugify_category(p)) for p in paths] == [(p, sql) for p, sql in rows]
