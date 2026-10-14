from __future__ import annotations

from collections.abc import Mapping
from io import BytesIO
from typing import Any, TypeVar

from attrs import define as _attrs_define

from .. import types
from ..models.feed_format import FeedFormat
from ..types import UNSET, File, Unset

T = TypeVar("T", bound="FeedUpload")


@_attrs_define
class FeedUpload:
    """
    Attributes:
        file (File): Feed file. CSV header / JSONL keys are `sku, title_en, title_ar, description_en, description_ar,
            brand, category_path, price, currency, colour, size, attributes`.
        format_ (FeedFormat | Unset):
    """

    file: File
    format_: FeedFormat | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        file = self.file.to_tuple()

        format_: str | Unset = UNSET
        if not isinstance(self.format_, Unset):
            format_ = self.format_.value

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "file": file,
            }
        )
        if format_ is not UNSET:
            field_dict["format"] = format_

        return field_dict

    def to_multipart(self) -> types.RequestFiles:
        files: types.RequestFiles = []

        files.append(("file", self.file.to_tuple()))

        if not isinstance(self.format_, Unset):
            files.append(("format", (None, str(self.format_.value).encode(), "text/plain")))

        return files

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        file = File(payload=BytesIO(d.pop("file")))

        _format_ = d.pop("format", UNSET)
        format_: FeedFormat | Unset
        if isinstance(_format_, Unset):
            format_ = UNSET
        else:
            format_ = FeedFormat(_format_)

        feed_upload = cls(
            file=file,
            format_=format_,
        )

        return feed_upload
