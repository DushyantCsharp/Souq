#!/usr/bin/env python3
"""Fail the release when an image outgrows its size budget in ``ci/image-budgets.json``.

The size checked is the uncompressed single-platform size of a locally loaded image: the sum of its layer
sizes from ``docker history``, i.e. what a node unpacks on pull. ``docker image inspect .Size`` is not used
because its meaning depends on the image store (uncompressed with overlay2, compressed content size with
the containerd snapshotter that Docker Desktop and newer engines default to). Budgets are in MiB.
Images without a budget pass with a notice so a new service does not break the release, but the search
image (ONNX models baked in) must stay budgeted — growth there costs cold-start time on every node.

    check_image_size.py --name search --image souq-scan/search:ci [--budgets ci/image-budgets.json]
    check_image_size.py --name search --size-bytes 1234567          # tests / precomputed sizes
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024
DEFAULT_BUDGETS = Path(__file__).with_name("image-budgets.json")


class BudgetError(ValueError):
    pass


@dataclass(frozen=True)
class Verdict:
    name: str
    size_bytes: int
    budget_mib: float | None

    @property
    def size_mib(self) -> float:
        return self.size_bytes / MIB

    @property
    def ok(self) -> bool:
        return self.budget_mib is None or self.size_mib <= self.budget_mib

    def message(self) -> str:
        if self.budget_mib is None:
            return f"{self.name}: {self.size_mib:.1f} MiB (no budget configured)"
        pct = 100.0 * self.size_mib / self.budget_mib
        state = "within" if self.ok else "OVER"
        return (
            f"{self.name}: {self.size_mib:.1f} MiB / {self.budget_mib:.0f} MiB budget ({pct:.0f}%, {state})"
        )


def load_budgets(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BudgetError(f"cannot read {path}: {exc}") from exc
    images = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images, dict):
        raise BudgetError(f"{path}: expected an object with an 'images' map")
    budgets: dict[str, float] = {}
    for name, spec in images.items():
        limit = spec.get("max_mib") if isinstance(spec, dict) else None
        if isinstance(limit, bool) or not isinstance(limit, int | float) or limit <= 0:
            raise BudgetError(f"{path}: images.{name}.max_mib must be a positive number")
        budgets[name] = float(limit)
    return budgets


def docker_image_size(
    image: str, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> int:
    proc = run(
        ["docker", "history", "--no-trunc", "--human=false", "--format", "{{.Size}}", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise BudgetError(f"docker history {image} failed: {proc.stderr.strip()}")
    try:
        sizes = [int(line) for line in proc.stdout.split()]
    except ValueError as exc:
        raise BudgetError(f"unexpected layer size output for {image}: {proc.stdout!r}") from exc
    if not sizes:
        raise BudgetError(f"docker history {image} returned no layers")
    return sum(sizes)


def check(name: str, size_bytes: int, budgets: dict[str, float]) -> Verdict:
    return Verdict(name=name, size_bytes=size_bytes, budget_mib=budgets.get(name))


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check an image against its size budget.")
    ap.add_argument("--name", required=True, help="budget key, e.g. search")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--image", help="local image reference to inspect")
    src.add_argument("--size-bytes", type=int, help="size to check instead of inspecting an image")
    ap.add_argument("--budgets", type=Path, default=DEFAULT_BUDGETS)
    ap.add_argument("--require-budget", action="store_true", help="fail when the image has no budget")
    args = ap.parse_args(argv)

    try:
        budgets = load_budgets(args.budgets)
        size = args.size_bytes if args.size_bytes is not None else docker_image_size(args.image)
    except BudgetError as exc:
        print(f"check_image_size: {exc}", file=sys.stderr)
        return 2

    verdict = check(args.name, size, budgets)
    print(verdict.message())
    if verdict.budget_mib is None:
        level = "error" if args.require_budget else "notice"
        print(f"::{level} title=image budget::{args.name} has no entry in {args.budgets}", file=sys.stderr)
        return 1 if args.require_budget else 0
    if not verdict.ok:
        print(f"::error title=image size budget exceeded::{verdict.message()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
