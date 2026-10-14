from enum import StrEnum


class FeedRowErrorCode(StrEnum):
    DUPLICATE_SKU_IN_FEED = "duplicate_sku_in_feed"
    INVALID_CATEGORY = "invalid_category"
    INVALID_CURRENCY = "invalid_currency"
    INVALID_JSON = "invalid_json"
    INVALID_PRICE = "invalid_price"
    MALFORMED_ROW = "malformed_row"
    MISSING_FIELD = "missing_field"
    TOO_LONG = "too_long"

    def __str__(self) -> str:
        return str(self.value)
