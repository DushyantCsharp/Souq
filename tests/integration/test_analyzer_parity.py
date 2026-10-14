"""CLAUDE.md rule 4: OpenSearch's ``souq_arabic_norm`` char filter mirrors ``souq_common.arabic``.

Checked against the live index (``_analyze``), so a template that drifted from ``ARABIC_CHAR_MAP``, or an
OpenSearch mapping-rule quirk (escaping, multi-char rules), fails here rather than as silent recall loss.
"""

from __future__ import annotations

import pytest
from souq_testing.containers import opensearch_request
from souq_testing.services import Stack

from souq_common.arabic import ARABIC_CHAR_MAP, normalise_arabic

pytestmark = pytest.mark.integration

SAMPLES = [
    "مُحَمَّد",
    "سَمَّاعَاتٌ لاسلكيّة",
    "أحمد إسلام آلات ٱلله",
    "مستشفى على",
    "مدرسة ساعة ذكية",
    "مؤمن بيئة سؤال رئيس",
    "مقاس ٤٢ اقل من ٣٠٠ درهم",
    "كتـــاب هٰذا",
    "حذاء جري اسود",
    "Sony WH-1000XM5 سماعات",
]


def char_filter(stack: Stack, text: str) -> str:
    body = {"char_filter": ["souq_arabic_norm"], "tokenizer": "keyword", "text": text}
    tokens = opensearch_request(stack.opensearch_url, "POST", "/products_read/_analyze", body)["tokens"]
    return "".join(t["token"] for t in tokens)


def analyze(stack: Stack, analyzer: str, text: str) -> list[str]:
    body = {"analyzer": analyzer, "text": text}
    tokens = opensearch_request(stack.opensearch_url, "POST", "/products_read/_analyze", body)["tokens"]
    return [t["token"] for t in tokens]


@pytest.mark.parametrize(
    "src", sorted(ARABIC_CHAR_MAP), ids=[f"U+{ord(c):04X}" for c in sorted(ARABIC_CHAR_MAP)]
)
def test_every_mapped_character(stack: Stack, src: str) -> None:
    text = f"ب{src}ت"
    assert char_filter(stack, text) == normalise_arabic(text)


@pytest.mark.parametrize("text", SAMPLES)
def test_char_filter_matches_normaliser(stack: Stack, text: str) -> None:
    assert char_filter(stack, text) == normalise_arabic(text)


@pytest.mark.parametrize("text", SAMPLES)
def test_arabic_analyzer_is_invariant_to_normalisation(stack: Stack, text: str) -> None:
    # Indexing raw text and querying normalised text (or the reverse) must produce the same terms.
    assert analyze(stack, "souq_arabic", text) == analyze(stack, "souq_arabic", normalise_arabic(text))
