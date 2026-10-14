"""Category path slugification: "Electronics > Audio > Headphones" -> "electronics.audio.headphones".

Mirrors the SQL function ``category_ltree(text)`` in packages/db (the one writers use); a parity test runs
both over the same inputs. ASCII-only by design: ltree's notion of a valid non-ASCII label depends on the
database locale, so anything that slugifies to an empty label is rejected rather than guessed at.
"""

from __future__ import annotations

import re

MAX_LEVELS = 10
MAX_LABEL_LEN = 255
SEPARATOR = ">"

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def slugify_label(label: str) -> str:
    # Only ASCII A-Z are lowered (same as the SQL translate()); str.lower() would also fold e.g. "İ"
    # into ASCII "i" + a combining mark and disagree with Postgres.
    s = label.strip().translate(_ASCII_LOWER).replace("&", " and ")
    return _NON_ALNUM.sub("_", s).strip("_")


def slugify_category(path: str) -> str | None:
    """ltree text for a display path, or None if the path is not a valid category."""
    if not path.strip():
        return None
    parts = path.split(SEPARATOR)
    if len(parts) > MAX_LEVELS:
        return None
    labels = [slugify_label(p) for p in parts]
    if any(not lbl or len(lbl) > MAX_LABEL_LEN for lbl in labels):
        return None
    return ".".join(labels)


def category_levels(ltree: str) -> list[str]:
    """Every prefix of an ltree path, shortest first (the index's ``category_levels``)."""
    labels = ltree.split(".")
    return [".".join(labels[: i + 1]) for i in range(len(labels))]
