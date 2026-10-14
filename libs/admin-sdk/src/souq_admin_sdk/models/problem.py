from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.invalid_param import InvalidParam


T = TypeVar("T", bound="Problem")


@_attrs_define
class Problem:
    """RFC 9457 problem details.

    Attributes:
        type_ (str): One of `https://souq.dev/problems/{validation-error, invalid-page-token, unauthorized,
            not-found, payload-too-large, unsupported-media-type, rate-limited, upstream-timeout,
            search-unavailable, feeds-unavailable, bad-gateway, internal}`.
        title (str):
        status (int):
        detail (str | Unset):
        instance (str | Unset): Request path
        request_id (str | Unset): Correlates with gateway logs and traces.
        errors (list[InvalidParam] | Unset): Per-parameter validation failures (validation-error only).
    """

    type_: str
    title: str
    status: int
    detail: str | Unset = UNSET
    instance: str | Unset = UNSET
    request_id: str | Unset = UNSET
    errors: list[InvalidParam] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        type_ = self.type_

        title = self.title

        status = self.status

        detail = self.detail

        instance = self.instance

        request_id = self.request_id

        errors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = []
            for errors_item_data in self.errors:
                errors_item = errors_item_data.to_dict()
                errors.append(errors_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "type": type_,
                "title": title,
                "status": status,
            }
        )
        if detail is not UNSET:
            field_dict["detail"] = detail
        if instance is not UNSET:
            field_dict["instance"] = instance
        if request_id is not UNSET:
            field_dict["request_id"] = request_id
        if errors is not UNSET:
            field_dict["errors"] = errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.invalid_param import InvalidParam

        d = dict(src_dict)
        type_ = d.pop("type")

        title = d.pop("title")

        status = d.pop("status")

        detail = d.pop("detail", UNSET)

        instance = d.pop("instance", UNSET)

        request_id = d.pop("request_id", UNSET)

        _errors = d.pop("errors", UNSET)
        errors: list[InvalidParam] | Unset = UNSET
        if _errors is not UNSET:
            errors = []
            for errors_item_data in _errors:
                errors_item = InvalidParam.from_dict(errors_item_data)

                errors.append(errors_item)

        problem = cls(
            type_=type_,
            title=title,
            status=status,
            detail=detail,
            instance=instance,
            request_id=request_id,
            errors=errors,
        )

        problem.additional_properties = d
        return problem

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
