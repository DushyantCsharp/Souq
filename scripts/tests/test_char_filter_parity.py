"""The real OpenSearch souq_arabic_norm char filter must agree with souq_common.arabic.normalise_arabic."""

from __future__ import annotations

import random
import secrets
from typing import Any

import pytest

from souq_common.arabic import ARABIC_CHAR_MAP, normalise_arabic
from souq_scripts.templates import CHAR_FILTER, index_body, load_template

pytestmark = pytest.mark.integration

TABLE = [
    "الْعَرَبِيَّةُ", "مـــكتبة", "أحمد إبراهيم آمنة ٱلله", "مستشفى على", "مدرسة جميلة", "سؤال رئيس",
    "١٢٣ ۴۵۶ ٣٫٥ ١٬٠٠٠", "کتاب فارسی", "سماعات‌لاسلكية‍", "‏سوني‎ WH-1000XM5", "حِذَاءٌ رِيَاضِيٌّ أَسْوَدُ",
    "Sony Headphones 128GB", "فستان   سهرة",
]  # fmt: skip


def random_corpus(n: int = 80, seed: int = 5) -> list[str]:
    rng = random.Random(seed)
    alphabet = [*ARABIC_CHAR_MAP, *"ابتثجحخدذرزسشصضطظعغفقكلمنهوي ءaZ09- "]
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 24))) for _ in range(n)]


def analyze_char_filter(client: Any, char_filter: dict[str, Any], text: str) -> str:
    resp = client.indices.analyze(body={"tokenizer": "keyword", "char_filter": [char_filter], "text": text})
    return "".join(t["token"] for t in resp["tokens"])


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_opensearch_char_filter_equals_normalise_arabic(opensearch: Any, version: str) -> None:
    cf = load_template(version)["template"]["settings"]["analysis"]["char_filter"][CHAR_FILTER]
    mismatches = [
        (text, got, normalise_arabic(text))
        for text in TABLE + random_corpus()
        if (got := analyze_char_filter(opensearch, cf, text)) != normalise_arabic(text)
    ]
    # A mismatch means the template and souq_common disagree: run `souq-templates --write`, or, if the
    # analyzer cannot express the rule, raise it with the search agent (owner of souq_common.arabic).
    assert mismatches == []


def test_full_template_builds_and_arabic_variants_share_tokens(opensearch: Any) -> None:
    name = f"souq_scripts_test_{secrets.token_hex(4)}"
    opensearch.indices.create(index=name, body=index_body(load_template("v2")))
    try:

        def tokens(field: str, text: str) -> list[str]:
            resp = opensearch.indices.analyze(index=name, body={"field": field, "text": text})
            return [t["token"] for t in resp["tokens"]]

        assert tokens("title_ar", "سَمّاعَات لاسلكيّة") == tokens("title_ar", "سماعات لاسلكيه")
        assert tokens("title_ar", "أحذية") == tokens("title_ar", "احذيه")
        assert tokens("title_ar.raw", "إبراهيم") == tokens("title_ar.raw", "ابراهيم")
        assert "سم" in tokens("title_ar.prefix", "سماعات")
        assert tokens("title_en", "Running Shoes") == tokens("title_en", "running shoe")
    finally:
        opensearch.indices.delete(index=name, ignore_unavailable=True)
