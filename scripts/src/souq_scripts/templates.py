"""OpenSearch index templates (infra/opensearch/templates/products_v*.json): load, validate, sync.

The JSON files are the source of truth for the mapping, except the ``souq_arabic_norm`` char-filter rules,
which are owned by ``souq_common.arabic`` (CLAUDE.md rule 4). ``souq-templates --write`` rewrites those
rules from :func:`souq_common.arabic.opensearch_char_filter_mappings`; ``--check`` (CI) fails on drift or on
any deviation from the field contract.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
from pathlib import Path
from typing import Any

from souq_common.arabic import opensearch_char_filter_mappings
from souq_scripts.paths import TEMPLATES_DIR

CHAR_FILTER = "souq_arabic_norm"
EMBEDDING_DIM = 1024
_VERSION = re.compile(r"^v[1-9][0-9]*$")

# Field contract from CLAUDE.md (Shared contracts > OpenSearch): field -> mapping type.
CONTRACT_FIELDS: dict[str, str] = {
    "sku": "keyword",
    "brand": "text",
    "category_path": "keyword",
    "category_levels": "keyword",
    "price": "scaled_float",
    "currency": "keyword",
    "colour": "keyword",
    "size": "keyword",
    "title_en": "text",
    "description_en": "text",
    "title_ar": "text",
    "description_ar": "text",
    "attributes": "flat_object",
    "embedding": "knn_vector",
    "updated_at": "date",
    "version": "long",
}


def index_name(version: str) -> str:
    if not _VERSION.match(version):
        raise ValueError(f"index version must look like v1, v2, ... (got {version!r})")
    return f"products_{version}"


def template_path(version: str, templates_dir: Path = TEMPLATES_DIR) -> Path:
    return templates_dir / f"{index_name(version)}.json"


def available_versions(templates_dir: Path = TEMPLATES_DIR) -> list[str]:
    found = [p.stem.removeprefix("products_") for p in templates_dir.glob("products_v*.json")]
    return sorted((v for v in found if _VERSION.match(v)), key=lambda v: int(v[1:]))


def load_template(version: str, templates_dir: Path = TEMPLATES_DIR) -> dict[str, Any]:
    with template_path(version, templates_dir).open(encoding="utf-8") as fh:
        body: dict[str, Any] = json.load(fh)
    return body


def index_body(template: dict[str, Any]) -> dict[str, Any]:
    """settings + mappings to create an index directly (eval harness, tests) rather than via the template."""
    t = template["template"]
    return {"settings": t["settings"], "mappings": t["mappings"]}


def _get(d: dict[str, Any], *path: str) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def validate_template(body: dict[str, Any], version: str) -> list[str]:
    """Contract violations in a template body (empty list = valid)."""
    errors: list[str] = []
    name = index_name(version)
    if body.get("index_patterns") != [name]:
        errors.append(f"index_patterns must be exactly [{name!r}] so it never applies to other indices")
    settings = _get(body, "template", "settings") or {}
    props = _get(body, "template", "mappings", "properties") or {}
    if _get(settings, "index", "knn") is not True:
        errors.append("settings.index.knn must be true for knn_vector fields")
    for field, ftype in CONTRACT_FIELDS.items():
        actual = _get(props, field, "type")
        if actual != ftype:
            errors.append(f"field {field}: type {actual!r}, contract says {ftype!r}")
    if _get(props, "price", "scaling_factor") != 100:
        errors.append("price must be scaled_float with scaling_factor 100")
    if _get(props, "brand", "fields", "kw", "type") != "keyword":
        errors.append("brand needs a .kw keyword subfield")
    for f in ("title_en", "title_ar"):
        if _get(props, f, "fields", "raw", "type") != "keyword":
            errors.append(f"{f} needs a .raw keyword subfield")
    emb = props.get("embedding", {})
    if emb.get("dimension") != EMBEDDING_DIM:
        errors.append(f"embedding.dimension must be {EMBEDDING_DIM} (bge-m3 dense)")
    method = emb.get("method", {})
    if (method.get("name"), method.get("engine"), method.get("space_type")) != (
        "hnsw",
        "lucene",
        "cosinesimil",
    ):
        errors.append("embedding.method must be hnsw / lucene / cosinesimil")

    analysis = _get(settings, "analysis") or {}
    analyzers = analysis.get("analyzer", {})
    if _get(props, "title_en", "analyzer") != "souq_english" or "souq_english" not in analyzers:
        errors.append("title_en must use the souq_english analyzer")
    for f in ("title_ar", "description_ar"):
        an = _get(props, f, "analyzer")
        if an not in analyzers or CHAR_FILTER not in analyzers[an].get("char_filter", []):
            errors.append(f"{f} analyzer must apply the {CHAR_FILTER} char filter")
    cf = _get(analysis, "char_filter", CHAR_FILTER) or {}
    if cf.get("type") != "mapping":
        errors.append(f"{CHAR_FILTER} must be a mapping char filter")
    elif cf.get("mappings") != opensearch_char_filter_mappings():
        errors.append(f"{CHAR_FILTER} rules drifted from souq_common.arabic (run souq-templates --write)")

    if int(version[1:]) >= 2:
        for f in ("title_en", "title_ar"):
            prefix = _get(props, f, "fields", "prefix") or {}
            an = analyzers.get(prefix.get("analyzer", ""), {})
            if prefix.get("type") != "text" or not any(
                _get(analysis, "filter", flt, "type") == "edge_ngram" for flt in an.get("filter", [])
            ):
                errors.append(f"v2+: {f}.prefix must be a text subfield with an edge_ngram analyzer")
            if not prefix.get("search_analyzer"):
                errors.append(f"v2+: {f}.prefix needs a non-ngram search_analyzer")
    return errors


def validate_superset(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """A newer schema may add fields/subfields but must keep every older field with the same type."""
    errors = []
    old_props = _get(old, "template", "mappings", "properties") or {}
    new_props = _get(new, "template", "mappings", "properties") or {}
    for field, spec in old_props.items():
        if field not in new_props:
            errors.append(f"field {field} removed")
        elif new_props[field].get("type") != spec.get("type"):
            errors.append(f"field {field} changed type {spec.get('type')} -> {new_props[field].get('type')}")
        else:
            for sub in spec.get("fields", {}):
                if sub not in new_props[field].get("fields", {}):
                    errors.append(f"subfield {field}.{sub} removed")
    return errors


def sync_char_filter(path: Path, write: bool) -> bool:
    """True if the file's char-filter rules differ from souq_common (and were rewritten when write=True)."""
    body = json.loads(path.read_text(encoding="utf-8"))
    cf = body["template"]["settings"]["analysis"]["char_filter"][CHAR_FILTER]
    wanted = opensearch_char_filter_mappings()
    if cf.get("mappings") == wanted:
        return False
    if write:
        cf["mappings"] = wanted
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def check_all(templates_dir: Path = TEMPLATES_DIR) -> list[str]:
    errors: list[str] = []
    versions = available_versions(templates_dir)
    if not versions:
        return [f"no products_v*.json templates in {templates_dir}"]
    bodies = {v: load_template(v, templates_dir) for v in versions}
    for v, body in bodies.items():
        errors += [f"products_{v}: {e}" for e in validate_template(body, v)]
    for older, newer in itertools.pairwise(versions):
        errors += [
            f"products_{newer} vs {older}: {e}" for e in validate_superset(bodies[older], bodies[newer])
        ]
    return errors


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="validate templates; exit 1 on any problem (default)"
    )
    mode.add_argument(
        "--write", action="store_true", help="rewrite char-filter rules from souq_common.arabic"
    )
    ap.add_argument("--templates-dir", default=str(TEMPLATES_DIR))
    args = ap.parse_args(argv)
    tdir = Path(args.templates_dir)
    if args.write:
        for v in available_versions(tdir):
            if sync_char_filter(template_path(v, tdir), write=True):
                print(f"updated {template_path(v, tdir)}")
    errors = check_all(tdir)
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    if errors:
        sys.exit(1)
    print(f"templates ok: {', '.join(available_versions(tdir))}")


if __name__ == "__main__":
    main()
