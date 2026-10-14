from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.facet import Facet
    from ..models.filters import Filters
    from ..models.parsed_query import ParsedQuery
    from ..models.search_item import SearchItem


T = TypeVar("T", bound="SearchResponse")


@_attrs_define
class SearchResponse:
    """
    Attributes:
        items (list[SearchItem]):
        facets (list[Facet]):
        applied_filters (Filters): Same shape in requests (`filters[...]`) and responses (`applied_filters`,
            `parsed_query.filters`).
        parsed_query (ParsedQuery): How the query text was understood.
        degraded (list[str]): Components that failed open for this response. Known values: `rerank`, `llm_parse`, `knn`,
            `bm25`, `semantic_cache`. Treat unknown values as informational.
        next_page_token (None | str): Opaque token for the next page; `null` on the last page.
        total_estimate (int):
    """

    items: list[SearchItem]
    facets: list[Facet]
    applied_filters: Filters
    parsed_query: ParsedQuery
    degraded: list[str]
    next_page_token: str | None
    total_estimate: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        facets = []
        for facets_item_data in self.facets:
            facets_item = facets_item_data.to_dict()
            facets.append(facets_item)

        applied_filters = self.applied_filters.to_dict()

        parsed_query = self.parsed_query.to_dict()

        degraded = self.degraded

        next_page_token: str | None
        next_page_token = self.next_page_token

        total_estimate = self.total_estimate

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "items": items,
                "facets": facets,
                "applied_filters": applied_filters,
                "parsed_query": parsed_query,
                "degraded": degraded,
                "next_page_token": next_page_token,
                "total_estimate": total_estimate,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.facet import Facet
        from ..models.filters import Filters
        from ..models.parsed_query import ParsedQuery
        from ..models.search_item import SearchItem

        d = dict(src_dict)
        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = SearchItem.from_dict(items_item_data)

            items.append(items_item)

        facets = []
        _facets = d.pop("facets")
        for facets_item_data in _facets:
            facets_item = Facet.from_dict(facets_item_data)

            facets.append(facets_item)

        applied_filters = Filters.from_dict(d.pop("applied_filters"))

        parsed_query = ParsedQuery.from_dict(d.pop("parsed_query"))

        degraded = cast(list[str], d.pop("degraded"))

        def _parse_next_page_token(data: object) -> str | None:
            if data is None:
                return data
            return cast(None | str, data)

        next_page_token = _parse_next_page_token(d.pop("next_page_token"))

        total_estimate = d.pop("total_estimate")

        search_response = cls(
            items=items,
            facets=facets,
            applied_filters=applied_filters,
            parsed_query=parsed_query,
            degraded=degraded,
            next_page_token=next_page_token,
            total_estimate=total_estimate,
        )

        search_response.additional_properties = d
        return search_response

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
