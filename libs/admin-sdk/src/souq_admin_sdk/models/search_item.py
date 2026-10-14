from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.search_item_rank_source import SearchItemRankSource
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.money import Money


T = TypeVar("T", bound="SearchItem")


@_attrs_define
class SearchItem:
    """
    Attributes:
        sku (str):
        title (str): Localized by `lang` (falls back to the other language).
        brand (str):
        price (Money):
        category_path (str):
        score (float):
        rank_source (SearchItemRankSource):
        description (None | str | Unset):
        colour (None | str | Unset):
        size (None | str | Unset):
    """

    sku: str
    title: str
    brand: str
    price: Money
    category_path: str
    score: float
    rank_source: SearchItemRankSource
    description: str | Unset | None = UNSET
    colour: str | Unset | None = UNSET
    size: str | Unset | None = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        sku = self.sku

        title = self.title

        brand = self.brand

        price = self.price.to_dict()

        category_path = self.category_path

        score = self.score

        rank_source = self.rank_source.value

        description: str | Unset | None
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        colour: str | Unset | None
        if isinstance(self.colour, Unset):
            colour = UNSET
        else:
            colour = self.colour

        size: str | Unset | None
        if isinstance(self.size, Unset):
            size = UNSET
        else:
            size = self.size

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "sku": sku,
                "title": title,
                "brand": brand,
                "price": price,
                "category_path": category_path,
                "score": score,
                "rank_source": rank_source,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if colour is not UNSET:
            field_dict["colour"] = colour
        if size is not UNSET:
            field_dict["size"] = size

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.money import Money

        d = dict(src_dict)
        sku = d.pop("sku")

        title = d.pop("title")

        brand = d.pop("brand")

        price = Money.from_dict(d.pop("price"))

        category_path = d.pop("category_path")

        score = d.pop("score")

        rank_source = SearchItemRankSource(d.pop("rank_source"))

        def _parse_description(data: object) -> str | Unset | None:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        def _parse_colour(data: object) -> str | Unset | None:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        colour = _parse_colour(d.pop("colour", UNSET))

        def _parse_size(data: object) -> str | Unset | None:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        size = _parse_size(d.pop("size", UNSET))

        search_item = cls(
            sku=sku,
            title=title,
            brand=brand,
            price=price,
            category_path=category_path,
            score=score,
            rank_source=rank_source,
            description=description,
            colour=colour,
            size=size,
        )

        search_item.additional_properties = d
        return search_item

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
