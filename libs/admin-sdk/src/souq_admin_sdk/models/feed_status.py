from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.feed_state import FeedState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.feed_status_error_counts import FeedStatusErrorCounts


T = TypeVar("T", bound="FeedStatus")


@_attrs_define
class FeedStatus:
    """
    Attributes:
        id (str):
        status (FeedState):
        rows_total (int):
        rows_ok (int):
        rows_rejected (int):
        error_counts (FeedStatusErrorCounts): Rejected rows per reject code (see `FeedRowErrorCode`).
        started_at (datetime.datetime | None | Unset):
        finished_at (datetime.datetime | None | Unset):
        failure_reason (None | str | Unset): Set when `status` is `failed` (whole-file failure, not row errors).
    """

    id: str
    status: FeedState
    rows_total: int
    rows_ok: int
    rows_rejected: int
    error_counts: FeedStatusErrorCounts
    started_at: datetime.datetime | Unset | None = UNSET
    finished_at: datetime.datetime | Unset | None = UNSET
    failure_reason: str | Unset | None = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        status = self.status.value

        rows_total = self.rows_total

        rows_ok = self.rows_ok

        rows_rejected = self.rows_rejected

        error_counts = self.error_counts.to_dict()

        started_at: str | Unset | None
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        elif isinstance(self.started_at, datetime.datetime):
            started_at = self.started_at.isoformat()
        else:
            started_at = self.started_at

        finished_at: str | Unset | None
        if isinstance(self.finished_at, Unset):
            finished_at = UNSET
        elif isinstance(self.finished_at, datetime.datetime):
            finished_at = self.finished_at.isoformat()
        else:
            finished_at = self.finished_at

        failure_reason: str | Unset | None
        if isinstance(self.failure_reason, Unset):
            failure_reason = UNSET
        else:
            failure_reason = self.failure_reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "status": status,
                "rows_total": rows_total,
                "rows_ok": rows_ok,
                "rows_rejected": rows_rejected,
                "error_counts": error_counts,
            }
        )
        if started_at is not UNSET:
            field_dict["started_at"] = started_at
        if finished_at is not UNSET:
            field_dict["finished_at"] = finished_at
        if failure_reason is not UNSET:
            field_dict["failure_reason"] = failure_reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.feed_status_error_counts import FeedStatusErrorCounts

        d = dict(src_dict)
        id = d.pop("id")

        status = FeedState(d.pop("status"))

        rows_total = d.pop("rows_total")

        rows_ok = d.pop("rows_ok")

        rows_rejected = d.pop("rows_rejected")

        error_counts = FeedStatusErrorCounts.from_dict(d.pop("error_counts"))

        def _parse_started_at(data: object) -> datetime.datetime | Unset | None:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                started_at_type_0 = datetime.datetime.fromisoformat(data)

                return started_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))

        def _parse_finished_at(data: object) -> datetime.datetime | Unset | None:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                finished_at_type_0 = datetime.datetime.fromisoformat(data)

                return finished_at_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(datetime.datetime | None | Unset, data)

        finished_at = _parse_finished_at(d.pop("finished_at", UNSET))

        def _parse_failure_reason(data: object) -> str | Unset | None:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        failure_reason = _parse_failure_reason(d.pop("failure_reason", UNSET))

        feed_status = cls(
            id=id,
            status=status,
            rows_total=rows_total,
            rows_ok=rows_ok,
            rows_rejected=rows_rejected,
            error_counts=error_counts,
            started_at=started_at,
            finished_at=finished_at,
            failure_reason=failure_reason,
        )

        feed_status.additional_properties = d
        return feed_status

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
