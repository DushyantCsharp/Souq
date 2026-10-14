from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.invalid_param_location import InvalidParamLocation

T = TypeVar("T", bound="InvalidParam")


@_attrs_define
class InvalidParam:
    """
    Attributes:
        location (InvalidParamLocation):
        name (str):
        detail (str):
    """

    location: InvalidParamLocation
    name: str
    detail: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        location = self.location.value

        name = self.name

        detail = self.detail

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "location": location,
                "name": name,
                "detail": detail,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        location = InvalidParamLocation(d.pop("location"))

        name = d.pop("name")

        detail = d.pop("detail")

        invalid_param = cls(
            location=location,
            name=name,
            detail=detail,
        )

        invalid_param.additional_properties = d
        return invalid_param

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
