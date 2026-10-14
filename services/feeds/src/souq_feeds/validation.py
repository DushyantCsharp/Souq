"""Supplier feed row validation (Pydantic v2) with the CLAUDE.md reject codes.

Each rule raises ``PydanticCustomError`` whose *type* is the reject code, so a failed row maps onto
``feed_row_errors.code`` without string matching. A row is rejected with one code — the most
fundamental problem first (see ``CODE_PRIORITY``) — so per-code counts add up to ``rows_rejected``.

The limits mirror the Postgres constraints in packages/db (sku pattern/length, numeric(12,2),
``^[A-Z]{3}$``, ltree category), so a row that validates here does not fail in the database.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, model_validator
from pydantic_core import PydanticCustomError

RejectCode = Literal[
    "missing_field",
    "invalid_price",
    "invalid_currency",
    "invalid_json",
    "invalid_category",
    "too_long",
    "duplicate_sku_in_feed",
    "malformed_row",
]

REJECT_CODES: Final[frozenset[str]] = frozenset(RejectCode.__args__)  # type: ignore[attr-defined]

# When a row has several problems, report the most fundamental one.
CODE_PRIORITY: Final[tuple[str, ...]] = (
    "malformed_row",
    "missing_field",
    "too_long",
    "invalid_json",
    "invalid_category",
    "invalid_price",
    "invalid_currency",
    "duplicate_sku_in_feed",
)

FEED_COLUMNS: Final[tuple[str, ...]] = (
    "sku",
    "title_en",
    "title_ar",
    "description_en",
    "description_ar",
    "brand",
    "category_path",
    "price",
    "currency",
    "colour",
    "size",
    "attributes",
)
REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("sku", "brand", "category_path", "price", "currency")
TITLE_COLUMNS: Final[tuple[str, ...]] = ("title_en", "title_ar")

MAX_LEN: Final[dict[str, int]] = {
    "sku": 64,
    "title_en": 512,
    "title_ar": 512,
    "description_en": 10_000,
    "description_ar": 10_000,
    "brand": 200,
    "category_path": 1_000,
    "colour": 64,
    "size": 64,
}
MAX_ATTRIBUTES_BYTES: Final[int] = 16_384
MAX_PRICE: Final[Decimal] = Decimal("9999999999.99")  # numeric(12, 2)

_SKU_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CCY_RE = re.compile(r"[A-Z]{3}")

# Active ISO 4217 codes. A 3-letter shape check alone would accept "ABC" or "USS".
# fmt: off
ISO_4217: Final[frozenset[str]] = frozenset((
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM", "BBD", "BDT", "BGN",
    "BHD", "BIF", "BMD", "BND", "BOB", "BRL", "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF",
    "CLP", "CNY", "COP", "CRC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP", "ERN", "ETB",
    "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD", "HNL", "HTG",
    "HUF", "IDR", "ILS", "INR", "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF",
    "KPW", "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD", "MAD", "MDL", "MGA",
    "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR", "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO",
    "NOK", "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG", "QAR", "RON", "RSD",
    "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP", "SLE", "SOS", "SRD", "SSP", "STN",
    "SVC", "SYP", "SZL", "THB", "TJS", "TMT", "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX",
    "USD", "UYU", "UZS", "VES", "VND", "VUV", "WST", "XAF", "XCD", "XOF", "XPF", "YER", "ZAR", "ZMW",
    "ZWG",
))
# fmt: on

# Category slug rules, mirroring category_ltree() in packages/db (the database stays authoritative:
# the importer re-checks with the SQL function before writing).
MAX_CATEGORY_LEVELS: Final[int] = 10
MAX_CATEGORY_LABEL: Final[int] = 255
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _slug_label(label: str) -> str:
    # Only ASCII A-Z are lowered (like the SQL translate()); str.lower() would fold some non-ASCII letters.
    return _NON_ALNUM.sub("_", label.strip().translate(_ASCII_LOWER).replace("&", " and ")).strip("_")


def category_slug(path: str) -> str | None:
    if not path.strip():
        return None
    parts = path.split(">")
    if len(parts) > MAX_CATEGORY_LEVELS:
        return None
    labels = [
        _NON_ALNUM.sub("_", p.strip().translate(_ASCII_LOWER).replace("&", " and ")).strip("_") for p in parts
    ]
    if any(not label or len(label) > MAX_CATEGORY_LABEL for label in labels):
        return None
    return ".".join(labels)


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _text(field: str, *, required: bool) -> BeforeValidator:
    limit = MAX_LEN[field]

    def check(value: Any) -> str | None:
        value = _blank_to_none(value)
        if value is None:
            if required:
                raise PydanticCustomError("missing_field", f"{field} is required")
            return None
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise PydanticCustomError("malformed_row", f"{field} must be a string")
        text = str(value)
        if len(text) > limit:
            raise PydanticCustomError("too_long", f"{field} is {len(text)} characters (max {limit})")
        return text

    return BeforeValidator(check)


def _sku(value: Any) -> str:
    value = _blank_to_none(value)
    if value is None:
        raise PydanticCustomError("missing_field", "sku is required")
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise PydanticCustomError("malformed_row", "sku must be a string")
    sku = str(value)
    if len(sku) > MAX_LEN["sku"]:
        raise PydanticCustomError("too_long", f"sku is {len(sku)} characters (max {MAX_LEN['sku']})")
    if not _SKU_RE.fullmatch(sku):
        raise PydanticCustomError(
            "malformed_row", "sku may only contain A-Z a-z 0-9 . _ - (starting alphanumeric)"
        )
    return sku


def _price(value: Any) -> Decimal:
    value = _blank_to_none(value)
    if value is None:
        raise PydanticCustomError("missing_field", "price is required")
    if isinstance(value, bool):
        raise PydanticCustomError("invalid_price", "price must be a number")
    try:
        # str() first: float inputs (JSONL) must not leak binary-float noise into numeric(12,2).
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PydanticCustomError("invalid_price", f"price {str(value)[:40]!r} is not a number") from None
    if not price.is_finite():
        raise PydanticCustomError("invalid_price", "price must be finite")
    if price <= 0:
        raise PydanticCustomError("invalid_price", "price must be greater than 0")
    if price > MAX_PRICE:
        raise PydanticCustomError("invalid_price", f"price exceeds {MAX_PRICE}")
    exponent = price.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise PydanticCustomError("invalid_price", "price has more than 2 decimal places")
    return price.quantize(Decimal("0.01"))


def _currency(value: Any) -> str:
    value = _blank_to_none(value)
    if value is None:
        raise PydanticCustomError("missing_field", "currency is required")
    code = str(value).upper()
    if not _CCY_RE.fullmatch(code) or code not in ISO_4217:
        raise PydanticCustomError("invalid_currency", f"currency {str(value)[:20]!r} is not an ISO 4217 code")
    return code


def _category(value: Any) -> str:
    value = _blank_to_none(value)
    if value is None:
        raise PydanticCustomError("missing_field", "category_path is required")
    if not isinstance(value, str):
        raise PydanticCustomError("invalid_category", "category_path must be a string")
    if len(value) > MAX_LEN["category_path"]:
        raise PydanticCustomError("too_long", f"category_path is {len(value)} characters")
    if category_slug(value) is None:
        raise PydanticCustomError(
            "invalid_category",
            "category_path must be 1-10 '>'-separated levels, each with at least one ASCII letter or digit",
        )
    # Canonical display form; the database stores category_ltree(<this>).
    return " > ".join(p.strip() for p in value.split(">"))


def _attributes(value: Any) -> dict[str, Any]:
    value = _blank_to_none(value)
    if value is None:
        return {}
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_ATTRIBUTES_BYTES:
            raise PydanticCustomError("too_long", f"attributes exceed {MAX_ATTRIBUTES_BYTES} bytes")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PydanticCustomError("invalid_json", f"attributes is not valid JSON: {exc.msg}") from None
    if not isinstance(value, dict):
        raise PydanticCustomError("invalid_json", "attributes must be a JSON object")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_ATTRIBUTES_BYTES:
        raise PydanticCustomError("too_long", f"attributes exceed {MAX_ATTRIBUTES_BYTES} bytes")
    return value


def _colour(value: Any) -> Any:
    value = _blank_to_none(value)
    # Index contract: colour is canonical lowercase English.
    return value.lower() if isinstance(value, str) else value


class FeedRow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    sku: Annotated[str, BeforeValidator(_sku)]
    title_en: Annotated[str | None, _text("title_en", required=False)] = None
    title_ar: Annotated[str | None, _text("title_ar", required=False)] = None
    description_en: Annotated[str | None, _text("description_en", required=False)] = None
    description_ar: Annotated[str | None, _text("description_ar", required=False)] = None
    brand: Annotated[str, _text("brand", required=True)]
    category_path: Annotated[str, BeforeValidator(_category)]
    price: Annotated[Decimal, BeforeValidator(_price)]
    currency: Annotated[str, BeforeValidator(_currency)]
    colour: Annotated[str | None, _text("colour", required=False), BeforeValidator(_colour)] = None
    size: Annotated[str | None, _text("size", required=False)] = None
    attributes: Annotated[dict[str, Any], BeforeValidator(_attributes)] = {}

    @model_validator(mode="after")
    def _needs_a_title(self) -> FeedRow:
        if not self.title_en and not self.title_ar:
            raise PydanticCustomError("missing_field", "title_en or title_ar is required")
        return self


class RowRejection(Exception):
    def __init__(self, code: str, field: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


def _pydantic_code(err: Any) -> str:
    kind = str(err["type"])
    if kind in REJECT_CODES:
        return kind
    if kind == "missing":
        return "missing_field"
    if kind in ("string_too_long", "too_long"):
        return "too_long"
    return "malformed_row"


def validate_row(data: dict[str, Any]) -> FeedRow:
    """Validate one parsed row or raise ``RowRejection`` with the highest-priority problem."""
    try:
        return FeedRow.model_validate(data)
    except ValidationError as exc:
        problems = []
        for err in exc.errors(include_url=False, include_input=False):
            code = _pydantic_code(err)
            loc = err.get("loc") or ()
            # The only model-level rule is "needs a title"; report it as title_en, like the feed manifests.
            field = str(loc[0]) if loc else ("title_en" if code == "missing_field" else None)
            problems.append((CODE_PRIORITY.index(code), code, field, str(err["msg"])))
        problems.sort(key=lambda p: p[0])
        _, code, field, message = problems[0]
        if len(problems) > 1:
            message += f" (+{len(problems) - 1} more problem(s))"
        raise RowRejection(code, field, message) from None
