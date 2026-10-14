"""Deterministic query parser (the hot path, no LLM): price / currency / brand / colour / size in English,
Arabic and Arabizi.

Works on ``normalise_query`` tokens, consumes the tokens that form a constraint and returns the rest as
``residual``. ``constraint_like`` lists leftovers that still look like constraints ("cheap", "مية", "around",
"without") — the signal that sends the query to the LLM parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from souq_common.arabic import normalise_query, strip_definite_article
from souq_search.models import Filters
from souq_search.parsing import lexicon as lx

_NUM = re.compile(r"^\d+(?:\.\d+)?k?$")
_RANGE_TOKEN = re.compile(r"^(\d+(?:\.\d+)?)[-–](\d+(?:\.\d+)?)$")
# Tokens to split before matching: "300aed", "aed300", "$50", "50$", "و200", "200+".
_GLUED = re.compile(r"^(\$|€|£|<=|>=|<|>|≤|≥|~|و|ب|w)?(\d+(?:\.\d+)?k?)(\$|€|£|\+|[a-zء-ي.]+)?$")
UNIT_WORDS = {
    lx.norm(x)
    for x in [
        "year",
        "years",
        "yr",
        "yrs",
        "month",
        "months",
        "old",
        "age",
        "y",
        "kg",
        "g",
        "gram",
        "grams",
        "lb",
        "lbs",
        "ml",
        "l",
        "liter",
        "litre",
        "liters",
        "cm",
        "mm",
        "m",
        "meter",
        "metre",
        "inch",
        "inches",
        "in",
        '"',
        "ft",
        "feet",
        "gb",
        "tb",
        "mb",
        "mah",
        "w",
        "watt",
        "watts",
        "v",
        "volt",
        "hz",
        "mp",
        "pcs",
        "pieces",
        "piece",
        "pack",
        "seats",
        "seater",
        "person",
        "people",
        "persons",
        "players",
        "hours",
        "hrs",
        "minutes",
        "min",
        "cups",
        "سنه",
        "سنوات",
        "سنين",
        "شهر",
        "اشهر",
        "شهور",
        "كيلو",
        "كجم",
        "جرام",
        "غرام",
        "مل",
        "لتر",
        "سم",
        "مم",
        "متر",
        "انش",
        "بوصه",
        "قيراط",
        "جيجا",
        "تيرا",
        "واط",
        "قطعه",
        "قطع",
        "اشخاص",
        "شخص",
        "ساعه",
        "ساعات",
    ]
}


@dataclass
class RuleParse:
    filters: Filters
    residual: str
    unparsed_tokens: list[str]
    constraint_like: list[str]
    tokens: list[str] = field(default_factory=list)
    matched: list[tuple[str, str]] = field(default_factory=list)  # (field, surface) for debug output


def _split_glued(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for tok in tokens:
        m = _GLUED.match(tok)
        if not m:
            m_range = _RANGE_TOKEN.match(tok)
            out.extend([m_range.group(1), "-", m_range.group(2)] if m_range else [tok])
            continue
        pre, number, post = m.groups()
        if post and post not in lx.CURRENCIES and post != "+":
            # "256gb", "15pro": product text, keep whole
            out.append(tok)
            continue
        out.extend(p for p in (pre, number, post) if p)
    return out


def _to_number(tok: str) -> float | None:
    if not _NUM.match(tok):
        return None
    if tok.endswith("k"):
        return float(tok[:-1]) * 1000
    return float(tok)


class _Parser:
    def __init__(self, tokens: list[str], lexicon: lx.Lexicon) -> None:
        self.t = tokens
        self.lex = lexicon
        self.used = [False] * len(tokens)
        self.price_min: float | None = None
        self.price_max: float | None = None
        self.currency: str | None = None
        self.brands: list[str] = []
        self.colours: list[str] = []
        self.sizes: list[str] = []
        self.matched: list[tuple[str, str]] = []

    # -- helpers -------------------------------------------------------------------------------------------
    def free(self, i: int) -> bool:
        return 0 <= i < len(self.t) and not self.used[i]

    def seq_at(self, i: int, seqs: list[tuple[str, ...]]) -> tuple[str, ...] | None:
        for seq in seqs:
            n = len(seq)
            if (
                i + n <= len(self.t)
                and all(self.free(i + j) for j in range(n))
                and tuple(self.t[i : i + n]) == seq
            ):
                return seq
        return None

    def consume(self, start: int, end: int, what: str) -> None:
        for j in range(start, end):
            self.used[j] = True
        self.matched.append((what, " ".join(self.t[start:end])))

    def currency_at(self, i: int) -> tuple[str, int] | None:
        """Currency starting at i (1 or 2 tokens) -> (code, length)."""
        if not self.free(i):
            return None
        if i + 1 < len(self.t) and self.free(i + 1):
            two = f"{self.t[i]} {self.t[i + 1]}"
            if two in lx.CURRENCIES:
                return lx.CURRENCIES[two], 2
        if self.t[i] in lx.CURRENCIES:
            return lx.CURRENCIES[self.t[i]], 1
        return None

    def number_at(self, i: int) -> float | None:
        if not self.free(i):
            return None
        value = _to_number(self.t[i])
        if value is None:
            return None
        # "under 2 years", "over 55 inch": a measurement, not a price
        if i + 1 < len(self.t) and self.t[i + 1] in UNIT_WORDS:
            return None
        return value

    def consume_leads(self, i: int) -> int:
        """Swallow 'for' / 'price' / 'بسعر' right before a price expression; returns new start index."""
        while i - 1 >= 0 and self.free(i - 1) and self.t[i - 1] in lx.PRICE_LEADS:
            i -= 1
        return i

    def set_currency(self, code: str | None) -> None:
        if code and not self.currency:
            self.currency = code

    # -- matchers ------------------------------------------------------------------------------------------
    def match_brands(self) -> None:
        i = 0
        while i < len(self.t):
            hit = None
            for n in range(min(self.lex.max_brand_len, len(self.t) - i), 0, -1):
                if not all(self.free(i + j) for j in range(n)):
                    continue
                key = tuple(self.t[i : i + n])
                name = self.lex.brands.get(key)
                if name is None and n == 1:
                    name = self.lex.brands.get((strip_definite_article(self.t[i]),))
                if name:
                    hit = (n, name)
                    break
            if hit:
                n, name = hit
                if name not in self.brands:
                    self.brands.append(name)
                self.consume(i, i + n, "brand")
                i += n
            else:
                i += 1

    def _finish_price(self, start: int, end: int, lo: float | None, hi: float | None, cur: str | None) -> int:
        """Record a price match spanning [start, end) plus a trailing currency; returns the end index."""
        tail = self.currency_at(end)
        if tail:
            cur = cur or tail[0]
            end += tail[1]
        start = self.consume_leads(start)
        if lo is not None:
            self.price_min = lo
        if hi is not None:
            self.price_max = hi
        self.set_currency(cur)
        self.consume(start, end, "price")
        return end

    def _has_currency_near(self, i: int) -> bool:
        return self.currency_at(i) is not None

    def match_prices(self) -> None:
        i = 0
        while i < len(self.t):
            if not self.free(i):
                i += 1
                continue
            nxt = self._match_price_at(i)
            i = nxt if nxt is not None else i + 1

    def _match_price_at(self, i: int) -> int | None:
        # Leading currency: "aed 200", "$ 50"
        lead_cur = self.currency_at(i)
        j = i + lead_cur[1] if lead_cur else i
        cur = lead_cur[0] if lead_cur else None

        # Ranges: [between|from|بين] NUM SEP NUM [CUR]. Without a range word ("100-200", "100 to 200") a
        # currency is required so "2-3 seater" or "pack 6 to 12" stay product text.
        start_seq = self.seq_at(j, lx.RANGE_START)
        k = j + len(start_seq) if start_seq else j
        lo = self.number_at(k)
        if lo is not None and k + 2 < len(self.t) and self.free(k + 1) and self.t[k + 1] in lx.RANGE_SEP:
            hi = self.number_at(k + 2)
            if hi is not None and hi >= lo and (start_seq or cur or self._has_currency_near(k + 3)):
                return self._finish_price(i, k + 3, lo, hi, cur)

        # Prefix operators: under/less than/اقل من NUM [CUR]
        for ops, is_max in ((lx.MAX_OPS, True), (lx.MIN_OPS, False)):
            seq = self.seq_at(j, ops)
            if not seq:
                continue
            k = j + len(seq)
            k_cur = self.currency_at(k)  # "under aed 300"
            if k_cur:
                cur = cur or k_cur[0]
                k += k_cur[1]
            value = self.number_at(k)
            if value is None:
                continue
            if seq in lx.WEAK_OPS and not (cur or self._has_currency_near(k + 1)):
                continue
            return self._finish_price(i, k + 1, None if is_max else value, value if is_max else None, cur)

        # Number first: NUM [CUR] [tail] — "300 aed or less", "200 درهم", "200+ aed"
        value = self.number_at(j)
        if value is None:
            return None
        k = j + 1
        k_cur = self.currency_at(k)
        if k_cur:
            cur = cur or k_cur[0]
            k += k_cur[1]
        for tails, is_max in ((lx.MAX_TAILS, True), (lx.MIN_TAILS, False)):
            seq = self.seq_at(k, tails)
            if seq:
                weak = seq in {("max",), ("plus",), ("+",)}
                if weak and not cur:
                    continue
                return self._finish_price(
                    i, k + len(seq), None if is_max else value, value if is_max else None, cur
                )
        if cur:
            # A bare amount with a currency reads as a budget ceiling ("shoes 200 aed").
            return self._finish_price(i, k, None, value, cur)
        return None

    def match_sizes(self) -> None:
        i = 0
        while i < len(self.t):
            if not self.free(i):
                i += 1
                continue
            tok = self.t[i]
            if tok in lx.SIZE_CUES or tok in lx.SHOE_SYSTEM_CUES:
                value, n = self._size_value(i + 1, numeric_only=tok in lx.SHOE_SYSTEM_CUES)
                if value:
                    self.sizes.append(value)
                    self.consume(i, i + 1 + n, "size")
                    i += 1 + n
                    continue
            elif (tok,) in lx.STANDALONE_SIZES:
                self.sizes.append(lx.LETTER_SIZES[(tok,)])
                self.consume(i, i + 1, "size")
            i += 1

    def _size_value(self, i: int, numeric_only: bool = False) -> tuple[str | None, int]:
        if not self.free(i):
            return None, 0
        if lx.SIZE_NUMBER.match(self.t[i]):
            return self.t[i], 1
        if numeric_only:
            return None, 0
        for n in (2, 1):
            key = tuple(self.t[i : i + n])
            if len(key) == n and all(self.free(i + j) for j in range(n)) and key in lx.LETTER_SIZES:
                return lx.LETTER_SIZES[key], n
        return None, 0

    def match_colours(self) -> None:
        i = 0
        while i < len(self.t):
            if not self.free(i):
                i += 1
                continue
            hit = None
            for n in (2, 1):
                key = tuple(self.t[i : i + n])
                if len(key) != n or not all(self.free(i + j) for j in range(n)):
                    continue
                canonical = self.lex.colours.get(key)
                if canonical is None and n == 1:
                    bare = strip_definite_article(self.t[i])
                    # feminine nisba adjectives: ذهبية/رمادية (normalised ذهبيه) agree with ذهبي/رمادي
                    canonical = self.lex.colours.get((bare,)) or (
                        self.lex.colours.get((bare[:-1],)) if bare.endswith("يه") else None
                    )
                if canonical:
                    hit = (n, canonical, key)
                    break
            if not hit:
                i += 1
                continue
            n, canonical, key = hit
            cue = i - 1 >= 0 and self.free(i - 1) and self.t[i - 1] in lx.COLOUR_CUES
            if key in lx.AMBIGUOUS_COLOURS and not (cue or self._near_product_noun(i, n)):
                i += n
                continue
            start = i - 1 if cue else i
            # "in red" consumes "in" only when it is the cue; "اللون الاحمر" likewise
            colour = self.lex.resolve_colour(canonical)
            if colour not in self.colours:
                self.colours.append(colour)
            self.consume(start, i + n, "colour")
            i += n

    def _near_product_noun(self, i: int, n: int) -> bool:
        neighbours = [self.t[j] for j in (i - 2, i - 1, i + n, i + n + 1) if 0 <= j < len(self.t)]
        return any(strip_definite_article(w) in lx.PRODUCT_NOUNS or w in lx.PRODUCT_NOUNS for w in neighbours)

    def match_lone_currency(self) -> None:
        """A stray currency after a matched amount ("under 300 for men aed") still sets the currency. Without
        any amount it stays unconsumed and counts as constraint-like, so the LLM can interpret it."""
        if self.price_min is None and self.price_max is None:
            return
        for i in range(len(self.t)):
            hit = self.currency_at(i)
            if hit:
                self.set_currency(hit[0])
                self.consume(i, i + hit[1], "currency")


def _constraint_like(tokens: list[str], used: list[bool]) -> list[str]:
    """Leftover tokens the LLM could still turn into filters (see lexicon PRICE_WORDS etc.)."""
    free = [(i, strip_definite_article(t)) for i, t in enumerate(tokens) if not used[i]]
    words = {i: t for i, t in free}
    out: list[str] = []
    for i, tok in free:
        raw = tokens[i]
        near = [words.get(j, "") for j in (i - 2, i - 1, i + 1, i + 2)]
        nxt = words.get(i + 1, "")
        if (
            tok in lx.PRICE_WORDS
            or raw in lx.PRICE_WORDS
            or tok in lx.APPROX_WORDS
            or tok in lx.MAGNITUDE_WORDS
        ):
            out.append(raw)
        elif tok in lx.OPERATOR_WORDS and (nxt in lx.EN_NUMBER_WORDS or nxt in lx.MAGNITUDE_WORDS):
            out.append(raw)  # "under two hundred", "اقل من خمسمية" (number given in words)
        elif (
            tok in lx.EN_NUMBER_WORDS
            and tok != "a"
            and any(
                w in lx.MAGNITUDE_WORDS or w in lx.CURRENCIES or w in lx.PRICE_WORDS or w in lx.OPERATOR_WORDS
                for w in near
            )
        ):
            out.append(raw)
        elif tok in lx.CURRENCIES:
            out.append(raw)  # a currency with no amount the rules could read
        elif tok in lx.SIZE_CUES:
            out.append(raw)  # "مقاسات كبيرة": a size cue whose value the rules could not read
    return out


def parse_rules(text: str, lang: str, *, lexicon: lx.Lexicon | None = None) -> RuleParse:
    """Parse ``text`` (raw or normalised; it is normalised here). ``lang`` is the request language hint —
    the vocabulary is matched in every language regardless, because shoppers mix scripts."""
    del lang  # all vocabularies are always active; kept for the contract and future per-language rules
    nq = normalise_query(text)
    tokens = _split_glued(nq.tokens)
    p = _Parser(tokens, lexicon or lx.get_lexicon())
    p.match_brands()
    p.match_prices()
    p.match_sizes()
    p.match_colours()
    p.match_lone_currency()
    if p.price_min is not None and p.price_max is not None and p.price_min > p.price_max:
        p.price_min, p.price_max = p.price_max, p.price_min

    filters = Filters(
        price_min=p.price_min,
        price_max=p.price_max,
        currency=p.currency,
        brands=p.brands or None,
        colours=p.colours or None,
        sizes=p.sizes or None,
    )
    residual_tokens = [tok for tok, used in zip(tokens, p.used, strict=True) if not used]
    return RuleParse(
        filters=filters,
        residual=" ".join(residual_tokens),
        unparsed_tokens=residual_tokens,
        constraint_like=_constraint_like(tokens, p.used),
        tokens=tokens,
        matched=p.matched,
    )
