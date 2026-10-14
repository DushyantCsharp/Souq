from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="Filters")


@_attrs_define
class Filters:
    """Same shape in requests (`filters[...]`) and responses (`applied_filters`, `parsed_query.filters`).

    Attributes:
        price_min (float | Unset):
        price_max (float | Unset):
        currency (str | Unset): ISO 4217 code.
        brands (list[str] | Unset):
        colours (list[str] | Unset): Canonical English lowercase colour names (e.g. `red`); Arabic colour words are
            mapped by the parser.
        sizes (list[str] | Unset):
        category (str | Unset): Category path prefix, e.g. `Electronics > Audio`.
    """

    price_min: float | Unset = UNSET
    price_max: float | Unset = UNSET
    currency: str | Unset = UNSET
    brands: list[str] | Unset = UNSET
    colours: list[str] | Unset = UNSET
    sizes: list[str] | Unset = UNSET
    category: str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        price_min = self.price_min

        price_max = self.price_max

        currency = self.currency

        brands: list[str] | Unset = UNSET
        if not isinstance(self.brands, Unset):
            brands = self.brands

        colours: list[str] | Unset = UNSET
        if not isinstance(self.colours, Unset):
            colours = self.colours

        sizes: list[str] | Unset = UNSET
        if not isinstance(self.sizes, Unset):
            sizes = self.sizes

        category = self.category

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if price_min is not UNSET:
            field_dict["price_min"] = price_min
        if price_max is not UNSET:
            field_dict["price_max"] = price_max
        if currency is not UNSET:
            field_dict["currency"] = currency
        if brands is not UNSET:
            field_dict["brands"] = brands
        if colours is not UNSET:
            field_dict["colours"] = colours
        if sizes is not UNSET:
            field_dict["sizes"] = sizes
        if category is not UNSET:
            field_dict["category"] = category

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        price_min = d.pop("price_min", UNSET)

        price_max = d.pop("price_max", UNSET)

        currency = d.pop("currency", UNSET)

        brands = cast(list[str], d.pop("brands", UNSET))

        colours = cast(list[str], d.pop("colours", UNSET))

        sizes = cast(list[str], d.pop("sizes", UNSET))

        category = d.pop("category", UNSET)

        filters = cls(
            price_min=price_min,
            price_max=price_max,
            currency=currency,
            brands=brands,
            colours=colours,
            sizes=sizes,
            category=category,
        )

        return filters
