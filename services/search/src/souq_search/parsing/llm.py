"""LLM query parser: called only when the rule parser leaves constraint-like tokens.

One Messages API call with a strict JSON schema (structured outputs, ``output_config.format``) on the
cheap fast model from settings (``SOUQ_LLM_MODEL``, default claude-haiku-5-5), thinking off and effort low
so it fits the parse_llm_ms budget. The prompt is fixed text plus the normalised query, so request bodies
are stable and record/replay keys don't drift. Prompt + output stay well under
``budgets.search.llm_tokens_per_query``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from souq_common.telemetry import llm_span, record_llm_usage
from souq_search.models import Filters
from souq_search.parsing import lexicon as lx
from souq_search.parsing.llm_fixtures import LlmTransport
from souq_search.parsing.rules import RuleParse
from souq_search.pricing import cost_usd

# Bump when the prompt or schema changes: it namespaces the semantic cache.
PROMPT_VERSION = "1"

CANONICAL_COLOURS = sorted({c for c in lx.COLOURS.values()})
CURRENCY_CODES = sorted(set(lx.CURRENCIES.values()))

SYSTEM_PROMPT = "\n".join(
    [
        "You turn shopping search queries from a UAE marketplace into search filters. Queries can be English,"
        " Arabic (any dialect), Arabizi (Arabic in Latin letters, digits for letters: 2=ء/ق 3=ع 5=خ 6=ط 7=ح"
        " 9=ق) or a mix of these.",
        "",
        "Fill every field of the JSON schema:",
        "- rewritten_query: what the shopper wants, with the price, brand, colour and size words removed and"
        " obvious misspellings fixed. Write Arabizi in Arabic script; otherwise keep the query's language. Do"
        ' not add products the shopper did not mention. Use "" if nothing is left.',
        '- price_min / price_max: numbers or null. "under/below/less than/up to/اقل من/تحت/ta7t" set'
        ' price_max; "over/above/more than/at least/اكثر من/فوق" set price_min; "between A and B/بين A و B"'
        ' set both; "around X/حوالي X" sets 0.8X to 1.2X rounded to whole numbers. Read number words'
        ' ("two hundred", "مية", "ميتين", "الف"). A vague word such as "cheap" or "رخيص" on its own sets no'
        " price.",
        "- currency: ISO code when the query states one (درهم/dirham/dhs/aed = AED, ريال = SAR,"
        " $/dollar = USD), else null.",
        '- brands: brand names as usually written in English (for example "Nike", "Samsung"); [] if none.',
        "- colours: colours from the allowed list; map Arabic and Arabizi colour words"
        " (احمر/حمراء/a7mar = red). [] if none.",
        '- sizes: clothing or shoe sizes as printed on labels ("42", "XL", "M"); [] if none.',
        "",
        "rule_filters were already extracted by a rule parser. Keep them unless the query clearly"
        " contradicts them.",
    ]
)

_NULLABLE_NUMBER = {"anyOf": [{"type": "number"}, {"type": "null"}]}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rewritten_query": {"type": "string"},
        "price_min": _NULLABLE_NUMBER,
        "price_max": _NULLABLE_NUMBER,
        "currency": {"anyOf": [{"type": "string", "enum": CURRENCY_CODES}, {"type": "null"}]},
        "brands": {"type": "array", "items": {"type": "string"}},
        "colours": {"type": "array", "items": {"type": "string", "enum": CANONICAL_COLOURS}},
        "sizes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["rewritten_query", "price_min", "price_max", "currency", "brands", "colours", "sizes"],
    "additionalProperties": False,
}


class LlmOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rewritten_query: str
    price_min: float | None
    price_max: float | None
    currency: str | None
    brands: list[str]
    colours: list[str]
    sizes: list[str]


@dataclass(frozen=True)
class LlmParse:
    filters: Filters
    rewritten: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    source: str
    recorded_at: str | None = None


class LlmParseError(ValueError):
    """The model answered but not with schema-valid output."""


def _estimate_tokens(text: str) -> int:
    # ~3 chars/token is conservative for mixed Arabic/English; only used to refuse oversize prompts.
    return len(text) // 3 + 1


def _canonical_brand(name: str, lexicon: lx.Lexicon) -> str:
    key = tuple(lx.norm(name).split())
    return lexicon.brands.get(key, name.strip())


def to_filters(out: LlmOutput, lexicon: lx.Lexicon | None = None) -> Filters:
    lex = lexicon or lx.get_lexicon()
    lo, hi = out.price_min, out.price_max
    lo = lo if lo is not None and lo >= 0 else None
    hi = hi if hi is not None and hi > 0 else None
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    brands = list(dict.fromkeys(_canonical_brand(b, lex) for b in out.brands if b.strip()))
    colours = list(dict.fromkeys(lex.resolve_colour(c) for c in out.colours if c in CANONICAL_COLOURS))
    sizes = list(dict.fromkeys(s.strip().upper() for s in out.sizes if s.strip()))
    return Filters(
        price_min=lo,
        price_max=hi,
        currency=out.currency if out.currency in CURRENCY_CODES else None,
        brands=brands or None,
        colours=colours or None,
        sizes=sizes or None,
    )


class LlmQueryParser:
    def __init__(
        self,
        transport: LlmTransport,
        model: str,
        max_output_tokens: int = 300,
        token_budget: int = 1500,
    ) -> None:
        self.transport = transport
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.token_budget = token_budget

    def build_request(self, normalised: str, lang: str, rule: RuleParse) -> dict[str, Any]:
        hints = rule.filters.model_dump(exclude_none=True)
        user = "\n".join(
            [
                f"query: {normalised}",
                f"language: {lang}",
                f"rule_filters: {json.dumps(hints, sort_keys=True, ensure_ascii=False)}",
                f"unparsed: {' '.join(rule.constraint_like)}",
            ]
        )
        if _estimate_tokens(SYSTEM_PROMPT + user) + self.max_output_tokens > self.token_budget:
            raise LlmParseError("prompt over llm_tokens_per_query budget")
        return {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        }

    async def parse(self, normalised: str, lang: str, rule: RuleParse, timeout_s: float) -> LlmParse:
        body = self.build_request(normalised, lang, rule)
        with llm_span(self.model, "chat") as span:
            result = await self.transport.create(body, timeout_s)
            cost = cost_usd(self.model, result.input_tokens, result.output_tokens)
            record_llm_usage(span, result.input_tokens, result.output_tokens, cost)
            try:
                out = LlmOutput.model_validate(json.loads(result.text()))
            except (ValueError, ValidationError) as exc:
                raise LlmParseError("schema-invalid LLM output") from exc
        rewritten = out.rewritten_query.strip() or None
        return LlmParse(
            filters=to_filters(out).merged_over(rule.filters),
            rewritten=rewritten,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=cost,
            source=result.source,
            recorded_at=result.recorded_at,
        )
