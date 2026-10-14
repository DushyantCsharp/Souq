"""Download pinned files from the Hugging Face hub with resume, verifying LFS sha256 and sizes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx

from souq_models.registry import ModelSpec

HF = "https://huggingface.co"
CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def file_index(spec: ModelSpec, client: httpx.Client) -> dict[str, dict[str, Any]]:
    """{path: {"size": int, "sha256": str | None}} for the pinned revision (sha256 only for LFS files)."""
    resp = client.get(f"{HF}/api/models/{spec.repo}/revision/{spec.revision}", params={"blobs": "true"})
    resp.raise_for_status()
    out: dict[str, dict[str, Any]] = {}
    for sib in resp.json().get("siblings", []):
        lfs = sib.get("lfs") or {}
        out[sib["rfilename"]] = {"size": sib.get("size"), "sha256": lfs.get("sha256")}
    return out


def _download(url: str, dest: Path, size: int | None, client: httpx.Client) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.exists() else 0
    if size is not None and have == size:
        return
    headers = {"Range": f"bytes={have}-"} if have else {}
    with client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
        if resp.status_code == 416:  # already complete
            return
        resp.raise_for_status()
        mode = "ab" if resp.status_code == 206 else "wb"
        with dest.open(mode) as fh:
            for chunk in resp.iter_bytes(CHUNK):
                fh.write(chunk)


def fetch(spec: ModelSpec, verify: bool = True) -> Path:
    """Ensure every file of ``spec`` is under ``spec.src_dir`` and matches the hub's sha256/size."""
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        index = file_index(spec, client)
        for rel in spec.files:
            meta = index.get(rel)
            if meta is None:
                raise FileNotFoundError(f"{rel} not in {spec.repo}@{spec.revision}")
            dest = spec.src_dir / rel
            _download(f"{HF}/{spec.repo}/resolve/{spec.revision}/{rel}", dest, meta["size"], client)
            if meta["size"] is not None and dest.stat().st_size != meta["size"]:
                raise OSError(f"{dest}: size {dest.stat().st_size} != {meta['size']}")
            if verify and meta["sha256"] and sha256_file(dest) != meta["sha256"]:
                raise OSError(f"{dest}: sha256 mismatch with {spec.repo}@{spec.revision}")
    return spec.src_dir
