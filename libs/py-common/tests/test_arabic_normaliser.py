"""Table tests for ``souq_common.arabic`` — the single source of Arabic normalisation (CLAUDE.md rule 4).

Every expectation here is derived from the contract in CLAUDE.md, so a failure means the module drifted
from what the OpenSearch char-filter mirrors and what the rule parser / evals assume.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from souq_common.arabic import arabizi_to_arabic, detect_script, normalise_arabic, normalise_query

TASHKEEL = [chr(cp) for cp in range(0x064B, 0x0653)]  # fathatan .. sukun, inclusive
SUPERSCRIPT_ALEF = "ٰ"
TATWEEL = "ـ"
STRIPPED = {*TASHKEEL, SUPERSCRIPT_ALEF, TATWEEL}
FOLDED_AWAY = set("أإآٱىةؤئ") | set("٠١٢٣٤٥٦٧٨٩")

ARABIC_LETTER = re.compile(r"[ء-ي]")
LATIN_LETTER = re.compile(r"[A-Za-z]")


# --------------------------------------------------------------------------- normalise_arabic


@pytest.mark.parametrize("mark", TASHKEEL, ids=[f"U+{ord(m):04X}" for m in TASHKEEL])
def test_each_tashkeel_mark_is_stripped(mark: str) -> None:
    assert normalise_arabic(f"ب{mark}ت") == "بت"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # tashkeel (harakat, shadda, sukun, tanwin)
        ("مُحَمَّد", "محمد"),
        ("سَمَّاعَاتٌ", "سماعات"),
        ("كِتَابًا", "كتابا"),
        ("قَلْب", "قلب"),
        # superscript (dagger) alef U+0670
        ("هٰذا", "هذا"),
        ("رحمٰن", "رحمن"),
        # tatweel U+0640
        ("سـلام", "سلام"),
        ("كتـــاب", "كتاب"),
        # alef variants → bare alef
        ("أحمد", "احمد"),
        ("إسلام", "اسلام"),
        ("آلات", "الات"),
        ("ٱلله", "الله"),
        ("أإآٱ", "اااا"),
        # alef maqsura → ya
        ("مستشفى", "مستشفي"),
        ("على", "علي"),
        # ta marbuta → ha
        ("مدرسة", "مدرسه"),
        ("ساعة ذكية", "ساعه ذكيه"),
        # hamza carriers
        ("مؤمن", "مومن"),
        ("بيئة", "بييه"),
        ("سؤال", "سوال"),
        ("رئيس", "رييس"),
        # Arabic-Indic digits → ASCII
        ("٠١٢٣٤٥٦٧٨٩", "0123456789"),
        ("مقاس ٤٢", "مقاس 42"),
        ("اقل من ٣٠٠ درهم", "اقل من 300 درهم"),
        # several rules in one word
        ("الْمُؤَسَّسَة", "الموسسه"),
        ("أَحْـمَـد", "احمد"),
    ],
)
def test_normalise_arabic_table(raw: str, expected: str) -> None:
    assert normalise_arabic(raw) == expected


@pytest.mark.parametrize(
    "text",
    ["Sony WH-1000XM5", "running shoes 42", "سماعات", "حذاء جري", "", "   "],
)
def test_already_normal_text_is_unchanged(text: str) -> None:
    # Case folding of Latin is not part of the contract, so compare case-insensitively.
    assert normalise_arabic(text).lower() == text.lower()


def test_mixed_script_only_touches_arabic() -> None:
    out = normalise_arabic("سمّاعات Sony ٣ أزرق")
    assert "Sony".lower() in out.lower()
    assert "سماعات" in out
    assert "ازرق" in out
    assert "3" in out


arabic_text = st.text(
    alphabet=st.sampled_from([*"ابتثجحخدذرسشصضطظعغفقكلمنهوي", *STRIPPED, *FOLDED_AWAY, "ء", " ", *"abc123"]),
    max_size=40,
)


@settings(max_examples=300, deadline=None, database=None)
@given(arabic_text)
def test_normalise_arabic_is_idempotent_and_total(text: str) -> None:
    once = normalise_arabic(text)
    assert normalise_arabic(once) == once
    assert not (set(once) & STRIPPED), "tashkeel/tatweel survived normalisation"
    assert not (set(once) & FOLDED_AWAY), "an un-folded alef/ya/ta-marbuta/hamza-carrier/digit survived"


# --------------------------------------------------------------------------- arabizi_to_arabic


def test_arabizi_canonical_example() -> None:
    assert normalise_arabic(arabizi_to_arabic("sama3at")) == "سماعات"


@pytest.mark.parametrize(
    ("word", "letter"),
    [
        ("so2al", "ء"),
        ("3arabi", "ع"),
        ("sama3at", "ع"),
        ("5ubz", "خ"),
        ("7elw", "ح"),
        ("7aqiba", "ح"),
    ],
)
def test_arabizi_digit_letters(word: str, letter: str) -> None:
    out = arabizi_to_arabic(word)
    assert letter in out
    assert not re.search(r"[0-9]", out), f"digit left in {out!r}"
    assert not LATIN_LETTER.search(out), f"latin left in {out!r}"
    assert ARABIC_LETTER.search(out)


# --------------------------------------------------------------------------- detect_script


@pytest.mark.parametrize(
    ("text", "script"),
    [
        ("سماعات لاسلكية", "ar"),
        ("حذاء جري اسود اقل من 300 درهم", "ar"),
        ("مقاس ٤٢", "ar"),
        ("wireless headphones", "en"),
        ("black running shoes under 300 AED", "en"),
        ("nike size 42", "en"),
        ("سماعات sony", "mixed"),
        ("iphone 15 جراب", "mixed"),
        ("sama3at", "arabizi"),
        ("7elw", "arabizi"),
    ],
)
def test_detect_script(text: str, script: str) -> None:
    assert detect_script(text) == script


# --------------------------------------------------------------------------- normalise_query


def test_normalise_query_arabic() -> None:
    nq = normalise_query("سَمّاعات  لاسلكيّة")
    assert nq.script == "ar"
    assert nq.text.split() == ["سماعات", "لاسلكيه"]
    assert nq.tokens == ["سماعات", "لاسلكيه"]
    assert nq.arabizi_ar is None


def test_normalise_query_english_has_no_arabizi_rendering() -> None:
    nq = normalise_query("Wireless Headphones")
    assert nq.script == "en"
    assert [t.lower() for t in nq.tokens] == ["wireless", "headphones"]
    assert nq.arabizi_ar is None


def test_normalise_query_arabizi_carries_arabic_rendering() -> None:
    nq = normalise_query("sama3at")
    assert nq.script == "arabizi"
    assert nq.arabizi_ar is not None
    assert normalise_arabic(nq.arabizi_ar) == "سماعات"


def test_normalise_query_mixed() -> None:
    nq = normalise_query("جِراب iPhone")
    assert nq.script == "mixed"
    assert "جراب" in nq.tokens
    assert "iphone" in [t.lower() for t in nq.tokens]
