from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.feed_row_error_code import FeedRowErrorCode
from ..types import UNSET, Unset

T = TypeVar("T", bound="FeedRowError")


@_attrs_define
class FeedRowError:
    """
    Attributes:
        row_number (int): 1-based data row number (header excluded).
        code (FeedRowErrorCode):
        message (str):
        field (None | str | Unset):
        raw (None | str | Unset): Raw row text, truncated by the feeds service.
    """

    row_number: int
    code: FeedRowErrorCode
    message: str
    field: str | Unset | None = UNSET
    raw: str | Unset | None = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        row_number = self.row_number

        code = self.code.value

        message = self.message

        field: str | Unset | None
        if isinstance(self.field, Unset):
            field = UNSET
        else:
            field = self.field

        raw: str | Unset | None
        if isinstance(self.raw, Unset):
            raw = UNSET
        else:
            raw = self.raw

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "row_number": row_number,
                "code": code,
                "message": message,
            }
        )
        if field is not UNSET:
            field_dict["field"] = field
        if raw is not UNSET:
            field_dict["raw"] = raw

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        row_number = d.pop("row_number")

        code = FeedRowErrorCode(d.pop("code"))

        message = d.pop("message")

        def _parse_field(data: object) -> str | Unset | None:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        field = _parse_field(d.pop("field", UNSET))

        def _parse_raw(data: object) -> str | Unset | None:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        raw = _parse_raw(d.pop("raw", UNSET))

        feed_row_error = cls(
            row_number=row_number,
            code=code,
            message=message,
            field=field,
            raw=raw,
        )

        feed_row_error.additional_properties = d
        return feed_row_error

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
