"""Arabic / Arabizi normalisation — the ONLY normalisation module in the repo (CLAUDE.md rule 4).

Everything here is a pure character mapping (no stemming, no Unicode compatibility folding) so that the
OpenSearch ``souq_arabic_norm`` mapping char filter can mirror it exactly: index templates should be
generated from :data:`ARABIC_CHAR_MAP` / :func:`opensearch_char_filter_mappings`, and a test asserts that
the analyzer and :func:`normalise_arabic` agree.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

Script = Literal["ar", "en", "mixed", "arabizi"]

# --- Character map -----------------------------------------------------------------------------------

_TASHKEEL = [chr(c) for c in range(0x064B, 0x0653)] + ["ٰ"]  # fathatan..sukun, superscript alef
_TATWEEL = "ـ"
# Invisible bidi/joiner marks that RTL keyboards and copy-paste insert inside words; they split tokens
# in the analyzer if left in place.
_INVISIBLE = ["‌", "‍", "‎", "‏", "؜"]

ARABIC_CHAR_MAP: dict[str, str] = {
    **dict.fromkeys(_TASHKEEL, ""),
    _TATWEEL: "",
    **dict.fromkeys(_INVISIBLE, ""),
    # alef forms -> bare alef
    "أ": "ا",  # أ
    "إ": "ا",  # إ
    "آ": "ا",  # آ
    "ٱ": "ا",  # ٱ
    # ya / alef maqsura (incl. Farsi yeh typed on Persian keyboards) -> ya
    "ى": "ي",  # ى
    "ی": "ي",  # ی
    "ک": "ك",  # ک keheh -> kaf
    "ة": "ه",  # ة ta marbuta -> ha
    "ؤ": "و",  # ؤ -> و
    "ئ": "ي",  # ئ -> ي
    # Arabic-Indic and Extended (Persian) Arabic-Indic digits -> ASCII
    **{chr(0x0660 + i): str(i) for i in range(10)},
    **{chr(0x06F0 + i): str(i) for i in range(10)},
    "٫": ".",  # Arabic decimal separator
    "٬": ",",  # Arabic thousands separator
}

_TRANSLATE = str.maketrans(ARABIC_CHAR_MAP)


def opensearch_char_filter_mappings() -> list[str]:
    """Rules for an OpenSearch ``mapping`` char filter equivalent to :func:`normalise_arabic`."""
    return [f"\\u{ord(src):04X}=>{dst}" for src, dst in ARABIC_CHAR_MAP.items()]


def normalise_arabic(text: str) -> str:
    """Strip tashkeel/tatweel, unify alef/ya/ta-marbuta/hamza carriers, map Arabic-Indic digits to ASCII.

    Non-Arabic characters pass through untouched (case is not changed) so this is safe on mixed text.
    """
    return text.translate(_TRANSLATE)


# --- Script detection ---------------------------------------------------------------------------------

_AR_LETTER = re.compile(r"[ء-يٱ-ۓۺ-ۿ]")
_LATIN_LETTER = re.compile(r"[A-Za-zÀ-ɏ]")
# An Arabizi digit standing in for a letter: a single 2/3/5/6/7/8/9 (optionally with an apostrophe for
# the dotted variants) that is glued to latin letters, e.g. "a7mar", "3ala", "ma2", "5ara". Multi-digit
# numbers ("s23", "128gb") are model numbers, not letters.
_ARABIZI_DIGIT = re.compile(
    r"(?:(?<=[a-z])|^)(?<![0-9])[2356789]'?(?![0-9])(?=[a-z])"  # leading/medial: 3ala, a7mar, 5ara
    r"|(?<=[a-z]{3})[23]$"  # trailing hamza/ain: azra2, nisa2
)
_TECH_TOKEN = re.compile(
    r"^(?:\d+(?:gb|tb|mb|k|g|d|m|mm|cm|ml|l|kg|in|inch|w|v|mah|hz|x|xl|pcs|pc|p|s|th|nd|rd|st)|\d+in\d+"
    r"|[a-z]{1,3}\d{1,2}|i[3579]|mp[34]|ps[2-5]|x[1-9]|m[1-4]|g[1-9]|s[1-9]|a[1-9]|z[1-9]|v[1-9])$"
)

# Common romanised Arabic shopping words -> Arabic script. Written Arabic omits short vowels and Arabizi
# spells long vowels inconsistently, so letter rules alone can't recover "sama3at" -> سماعات; known words
# are looked up first and the rules only handle the rest. Also lets digit-free Arabizi ("shanta hamra")
# be detected as Arabizi.
ARABIZI_WORDS: dict[str, str] = {
    "sama3at": "سماعات",
    "sama3a": "سماعة",
    "sama3ah": "سماعة",
    "shanta": "شنطة",
    "shantat": "شنط",
    "shant": "شنط",
    "jazma": "جزمة",
    "gazma": "جزمة",
    "fustan": "فستان",
    "fostan": "فستان",
    "qamis": "قميص",
    "2amis": "قميص",
    "bantalon": "بنطلون",
    "bantaloon": "بنطلون",
    "jakeet": "جاكيت",
    "hidhaa": "حذاء",
    "7itha": "حذاء",
    "jawal": "جوال",
    "jawwal": "جوال",
    "mobail": "موبايل",
    "mobayl": "موبايل",
    "sa3a": "ساعة",
    "sa3ah": "ساعة",
    "saa3a": "ساعة",
    "sa3at": "ساعات",
    "nadara": "نظارة",
    "nadhara": "نظارة",
    "nadarat": "نظارات",
    "tawla": "طاولة",
    "kursi": "كرسي",
    "korsi": "كرسي",
    "sareer": "سرير",
    "sarir": "سرير",
    "matbakh": "مطبخ",
    "3abaya": "عباية",
    "abaya": "عباية",
    "sheila": "شيلة",
    "tarha": "طرحة",
    "khatim": "خاتم",
    "5atim": "خاتم",
    "3otor": "عطور",
    "3itr": "عطر",
    "ahmar": "احمر",
    "a7mar": "احمر",
    "hamra": "حمراء",
    "7amra": "حمراء",
    "aswad": "اسود",
    "sawda": "سوداء",
    "abyad": "ابيض",
    "bayda": "بيضاء",
    "akhdar": "اخضر",
    "a5dar": "اخضر",
    "khadra": "خضراء",
    "5adra": "خضراء",
    "azra2": "ازرق",
    "azraq": "ازرق",
    "zar2a": "زرقاء",
    "zarqa": "زرقاء",
    "asfar": "اصفر",
    "safra": "صفراء",
    "bonni": "بني",
    "bunni": "بني",
    "rasasi": "رصاصي",
    "rmadi": "رمادي",
    "wardi": "وردي",
    "zahri": "زهري",
    "banafsaji": "بنفسجي",
    "dahabi": "ذهبي",
    "fiddi": "فضي",
    "kohli": "كحلي",
    "kbir": "كبير",
    "kabir": "كبير",
    "kbeer": "كبير",
    "sghir": "صغير",
    "saghir": "صغير",
    "zghir": "صغير",
    "rkhis": "رخيص",
    "rakhees": "رخيص",
    "ra5ees": "رخيص",
    "ar5as": "ارخص",
    "arkhas": "ارخص",
    "jadid": "جديد",
    "jdeed": "جديد",
    "gedid": "جديد",
    "rijali": "رجالي",
    "rejali": "رجالي",
    "nisa2i": "نسائي",
    "nisai": "نسائي",
    "atfal": "اطفال",
    "wlad": "ولاد",
    "banat": "بنات",
    "aqal": "اقل",
    "a2al": "اقل",
    "akal": "اقل",
    "ta7t": "تحت",
    "tahet": "تحت",
    "fo2": "فوق",
    "fawq": "فوق",
    "dirham": "درهم",
    "darahem": "دراهم",
    "drahem": "دراهم",
    "ma3": "مع",
    "bidun": "بدون",
    "bdoon": "بدون",
    "3ala": "على",
    "hdeye": "هدية",
    "hadiyya": "هدية",
    "7elw": "حلو",
    "7ilw": "حلو",
    "7elwa": "حلوة",
    "mithl": "مثل",
    "zay": "زي",
    "ghali": "غالي",
    "3ard": "عرض",
    "takhfid": "تخفيض",
    "7awali": "حوالي",
    "ta2riban": "تقريبا",
    "miya": "مية",
    "mit": "مية",
    "alf": "الف",
    "ma2as": "مقاس",
    "maqas": "مقاس",
}
ARABIZI_LEXICON: frozenset[str] = frozenset(ARABIZI_WORDS)

_TOKEN_SPLIT = re.compile(r"[^\w'$%&.+\-؀-ۿ]+", re.UNICODE)


def _is_arabizi_token(tok: str) -> bool:
    tok = tok.lower()
    if not _LATIN_LETTER.search(tok):
        return False
    if tok in ARABIZI_LEXICON:
        return True
    if _TECH_TOKEN.match(tok):
        return False
    return bool(_ARABIZI_DIGIT.search(tok))


def detect_script(text: str) -> Script:
    """``ar`` / ``en`` / ``mixed`` (Arabic + Latin letters) / ``arabizi`` (Latin-script Arabic)."""
    has_ar = bool(_AR_LETTER.search(text))
    has_latin = bool(_LATIN_LETTER.search(text))
    if has_ar and has_latin:
        return "mixed"
    if has_ar:
        return "ar"
    if has_latin and any(_is_arabizi_token(t) for t in _TOKEN_SPLIT.split(text) if t):
        return "arabizi"
    return "en"


# --- Arabizi -> Arabic transliteration ----------------------------------------------------------------

# Longest match first. Apostrophe variants ("3'") are the dotted letters.
_ARABIZI_MULTI: list[tuple[str, str]] = [
    ("3'", "غ"),
    ("7'", "خ"),
    ("6'", "ظ"),
    ("9'", "ض"),
    ("sh", "ش"),
    ("ch", "ش"),
    ("kh", "خ"),
    ("gh", "غ"),
    ("th", "ث"),
    ("dh", "ذ"),
    ("aa", "ا"),
    ("ee", "ي"),
    ("ii", "ي"),
    ("ei", "ي"),
    ("ai", "ي"),
    ("ay", "ي"),
    ("oo", "و"),
    ("ou", "و"),
    ("uu", "و"),
    ("ow", "و"),
    ("aw", "و"),
]
_ARABIZI_SINGLE: dict[str, str] = {
    "2": "ء",
    "3": "ع",
    "5": "خ",
    "7": "ح",
    "6": "ط",
    "9": "ق",
    "8": "ق",
    "b": "ب",
    "p": "ب",
    "t": "ت",
    "j": "ج",
    "g": "ج",
    "h": "ه",
    "d": "د",
    "r": "ر",
    "z": "ز",
    "s": "س",
    "f": "ف",
    "v": "ف",
    "q": "ق",
    "k": "ك",
    "c": "ك",
    "l": "ل",
    "m": "م",
    "n": "ن",
    "w": "و",
    "y": "ي",
    "x": "كس",
}
_VOWELS = set("aeiou")


def _transliterate_word(word: str) -> str:
    known = ARABIZI_WORDS.get(word)
    if known:
        return known
    suffix = ""
    if len(word) > 4 and word.endswith("at") and word[-3] not in _VOWELS:
        # feminine plural -aat (sa3at, nadarat) is written ات
        word, suffix = word[:-2], "ات"
    out: list[str] = []
    i, n = 0, len(word)
    while i < n:
        for src, dst in _ARABIZI_MULTI:
            if word.startswith(src, i):
                out.append(dst)
                i += len(src)
                break
        else:
            ch = word[i]
            if ch in _VOWELS:
                if i == 0:
                    # word-initial vowel is written with an alef seat
                    out.append("ا")
                elif i == n - 1:
                    # final -a/-e is usually ta marbuta (normalised to ه); final -i/-o/-u is a long vowel
                    out.append({"a": "ه", "e": "ه", "i": "ي", "o": "و", "u": "و"}[ch])
                elif ch == "i":
                    out.append("ي")
                # other medial short vowels are not written in Arabic script
            elif ch in _ARABIZI_SINGLE:
                out.append(_ARABIZI_SINGLE[ch])
            elif ch == "'":
                pass
            else:
                out.append(ch)
            i += 1
    return "".join(out) + suffix


def arabizi_to_arabic(text: str) -> str:
    """Best-effort Latin-script Arabic -> Arabic script (2→ء 3→ع 5→خ 7→ح 3'→غ 6→ط 9→ق + letter map).

    Short vowels are dropped as in written Arabic, so the output is approximate; callers should match it
    with some fuzziness. Non-letter tokens (numbers, punctuation) are kept as is.
    """
    words = re.split(r"(\s+)", text.lower())
    return "".join(w if w.isspace() or not _LATIN_LETTER.search(w) else _transliterate_word(w) for w in words)


def is_arabizi_token(token: str) -> bool:
    """True for a Latin token that reads as romanised Arabic ("a7mar", "shanta"), not a model number."""
    return _is_arabizi_token(token)


def transliterate_arabizi_tokens(tokens: list[str]) -> dict[str, str]:
    """Normalised Arabic rendering for each Arabizi-looking token (others are left out of the map)."""
    return {t: normalise_arabic(_transliterate_word(t.lower())) for t in tokens if _is_arabizi_token(t)}


# --- Query normalisation ------------------------------------------------------------------------------

_PUNCT = re.compile(r"[،؛؟۔!?\"“”«»()\[\]{}<>،؛؟:;,/\\|*_~^`=#@]+")
_SPACES = re.compile(r"\s+")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_DEFINITE = re.compile(r"^(?:وال|بال|كال|فال|لل|ال)(?=\w{2,})")


@dataclass(frozen=True)
class NormalisedQuery:
    text: str
    script: Script
    tokens: list[str] = field(default_factory=list)
    arabizi_ar: str | None = None


def strip_definite_article(token: str) -> str:
    """Drop a leading ال (and its و/ب/ك/ف/ل clitic forms) — used for lexicon lookups, not for indexing."""
    return _DEFINITE.sub("", token)


def normalise_query(text: str) -> NormalisedQuery:
    """Normalise a raw user query: NFC, Arabic char map, lowercase Latin, punctuation → spaces.

    ``arabizi_ar`` holds the Arabic-script rendering of the Arabizi tokens (other Latin tokens such as
    brand names are kept as typed) when the query is Arabizi or mixed with Arabizi words.
    """
    script = detect_script(text)
    t = normalise_arabic(unicodedata.normalize("NFC", text)).lower()
    t = _THOUSANDS.sub("", t)  # "1,000" is one number, not two tokens
    t = _PUNCT.sub(" ", t)
    t = _SPACES.sub(" ", t).strip()
    tokens = [tok.strip(".-'") for tok in t.split(" ")]
    tokens = [tok for tok in tokens if tok]
    arabizi_ar: str | None = None
    if script in ("arabizi", "mixed") and any(_is_arabizi_token(tok) for tok in tokens):
        arabizi_ar = normalise_arabic(
            " ".join(_transliterate_word(tok) if _is_arabizi_token(tok) else tok for tok in tokens)
        )
    return NormalisedQuery(text=" ".join(tokens), script=script, tokens=tokens, arabizi_ar=arabizi_ar)


__all__ = [
    "ARABIC_CHAR_MAP",
    "ARABIZI_LEXICON",
    "ARABIZI_WORDS",
    "NormalisedQuery",
    "Script",
    "arabizi_to_arabic",
    "detect_script",
    "is_arabizi_token",
    "normalise_arabic",
    "normalise_query",
    "opensearch_char_filter_mappings",
    "strip_definite_article",
    "transliterate_arabizi_tokens",
]
