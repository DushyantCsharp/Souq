from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.parsed_query_parser import ParsedQueryParser
from ..models.parsed_query_script import ParsedQueryScript
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.filters import Filters


T = TypeVar("T", bound="ParsedQuery")


@_attrs_define
class ParsedQuery:
    """How the query text was understood.

    Attributes:
        original (str):
        normalised (str):
        detected_lang (str):
        script (ParsedQueryScript):
        filters (Filters): Same shape in requests (`filters[...]`) and responses (`applied_filters`,
            `parsed_query.filters`).
        parser (ParsedQueryParser):
        unparsed_tokens (list[str]):
        rewritten (None | str | Unset): Retrieval text after filter phrases were removed or rewritten.
    """

    original: str
    normalised: str
    detected_lang: str
    script: ParsedQueryScript
    filters: Filters
    parser: ParsedQueryParser
    unparsed_tokens: list[str]
    rewritten: str | Unset | None = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        original = self.original

        normalised = self.normalised

        detected_lang = self.detected_lang

        script = self.script.value

        filters = self.filters.to_dict()

        parser = self.parser.value

        unparsed_tokens = self.unparsed_tokens

        rewritten: str | Unset | None
        if isinstance(self.rewritten, Unset):
            rewritten = UNSET
        else:
            rewritten = self.rewritten

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "original": original,
                "normalised": normalised,
                "detected_lang": detected_lang,
                "script": script,
                "filters": filters,
                "parser": parser,
                "unparsed_tokens": unparsed_tokens,
            }
        )
        if rewritten is not UNSET:
            field_dict["rewritten"] = rewritten

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.filters import Filters

        d = dict(src_dict)
        original = d.pop("original")

        normalised = d.pop("normalised")

        detected_lang = d.pop("detected_lang")

        script = ParsedQueryScript(d.pop("script"))

        filters = Filters.from_dict(d.pop("filters"))

        parser = ParsedQueryParser(d.pop("parser"))

        unparsed_tokens = cast(list[str], d.pop("unparsed_tokens"))

        def _parse_rewritten(data: object) -> str | Unset | None:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        rewritten = _parse_rewritten(d.pop("rewritten", UNSET))

        parsed_query = cls(
            original=original,
            normalised=normalised,
            detected_lang=detected_lang,
            script=script,
            filters=filters,
            parser=parser,
            unparsed_tokens=unparsed_tokens,
            rewritten=rewritten,
        )

        parsed_query.additional_properties = d
        return parsed_query

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
