"""Internal search API models — the contract in CLAUDE.md ("Internal search API"). The generated
``packages/contracts/search-internal/openapi.json`` comes from these, so field names are load-bearing."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Lang = Literal["en", "ar", "auto"]
DetectedLang = Literal["en", "ar"]
ScriptName = Literal["ar", "en", "mixed", "arabizi"]
ParserPath = Literal["rule", "llm", "cache"]
RankSource = Literal["rerank", "fused", "lexical_tail"]
DegradedReason = Literal["rerank", "llm_parse", "knn", "bm25", "semantic_cache"]


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price_min: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    brands: list[str] | None = None
    colours: list[str] | None = None
    sizes: list[str] | None = None
    # Category node as stored in OpenSearch ``category_levels`` (ltree text, e.g. "electronics.audio").
    category: str | None = None

    def is_empty(self) -> bool:
        return not any(
            [
                self.price_min is not None,
                self.price_max is not None,
                self.currency,
                self.brands,
                self.colours,
                self.sizes,
                self.category,
            ]
        )

    def merged_over(self, base: Filters) -> Filters:
        """Fields set on ``self`` win; list fields are unioned (order-preserving)."""
        out = base.model_copy(deep=True)
        for name in ("price_min", "price_max", "currency", "category"):
            value = getattr(self, name)
            if value is not None:
                setattr(out, name, value)
        for name in ("brands", "colours", "sizes"):
            extra = getattr(self, name) or []
            if extra:
                current = list(getattr(out, name) or [])
                current += [v for v in extra if v not in current]
                setattr(out, name, current)
        return out

    def canonical(self) -> dict[str, Any]:
        """Order-insensitive form used in cache/query keys."""
        data = self.model_dump(exclude_none=True)
        for name in ("brands", "colours", "sizes"):
            if name in data:
                data[name] = sorted({str(v).lower() for v in data[name]})
                if not data[name]:
                    del data[name]
        return data


class Cursor(BaseModel):
    """Opaque to the gateway. ``query_key`` names the pagination session (services/search/README.md)."""

    model_config = ConfigDict(extra="forbid")

    query_key: str = Field(min_length=1, max_length=128)
    pit_id: str = Field(max_length=4096)
    window_pos: int = Field(ge=0)
    search_after: list[Any] | None = None


class SearchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: bool = True
    rerank: bool = True
    debug: bool = False


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=512)
    lang: Lang = "auto"
    filters: Filters | None = None
    cursor: Cursor | None = None
    limit: int = Field(default=24, ge=1, le=50)
    options: SearchOptions | None = None


class Price(BaseModel):
    amount: float
    currency: str


class SearchItem(BaseModel):
    sku: str
    title: str
    description: str | None = None
    brand: str  # "" when the catalog has no brand for the product
    price: Price
    category_path: str
    colour: str | None = None
    size: str | None = None
    score: float
    rank_source: RankSource


class FacetBucket(BaseModel):
    value: str
    count: int


class Facet(BaseModel):
    name: str
    buckets: list[FacetBucket]


class ParsedQuery(BaseModel):
    original: str
    normalised: str
    detected_lang: DetectedLang
    script: ScriptName
    rewritten: str | None = None
    filters: Filters
    parser: ParserPath
    unparsed_tokens: list[str]


class SearchResponse(BaseModel):
    items: list[SearchItem]
    facets: list[Facet]
    applied_filters: Filters
    parsed_query: ParsedQuery
    degraded: list[DegradedReason]
    next_cursor: Cursor | None = None
    total_estimate: int
    timings_ms: dict[str, float]
    # Only present with options.debug: per-retriever ranks and scores for offline analysis.
    debug: dict[str, Any] | None = None


class Problem(BaseModel):
    """RFC 9457 problem+json."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None


class Health(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    checks: dict[str, str] = Field(default_factory=dict)
