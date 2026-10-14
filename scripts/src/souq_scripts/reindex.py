"""Versioned OpenSearch indices behind the ``products_read`` / ``products_write`` aliases.

  reindex.py --init v1            create products_v1, point both aliases at it, backfill from Postgres
  reindex.py --to v2              zero-downtime migration to products_v2 (steps below)
  reindex.py --rollback           swap products_read back to the previous index
  reindex.py ... --dry-run        print the plan and validate templates; no mutating calls (CI uses it)

``--to vN`` steps, each idempotent so a crashed run is resumed by re-running the same command:
  1. validate + install the index template, create the index (refresh off while backfilling)
  2. add it to ``products_write``: the indexer dual-writes to every index behind that alias. Then wait
     ``--alias-settle-s`` (> the indexer's 10 s alias refresh) before reading Postgres, so no change that
     the backfill misses can still be in flight to the old index only.
  3. backfill from Postgres ``product_document()`` + embeddings, ``version_type=external`` so a newer live
     write always beats an older backfill copy (409 = fine). Progress is checkpointed in the index's
     mapping ``_meta`` (last SKU), so the backfill resumes where it stopped.
  4. verify doc count == Postgres row count, run golden-query smoke tests (lexical + knn), each must hit
  5. swap ``products_read`` atomically in ONE ``_aliases`` call, then drop the old index from
     ``products_write`` and record when it stopped receiving writes.

``--rollback`` puts the previous index back in ``products_write``, catches it up with rows changed since
it was detached (and prunes deleted SKUs), verifies, swaps ``products_read`` back in one call and detaches
the newer index (marked stale, so a later ``--to`` only needs an incremental catch-up).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from souq_common.arabic import normalise_query
from souq_common.embeddings import Embedder, document_text, get_embedder
from souq_common.settings import get_settings
from souq_scripts.paths import CACHE_DIR, EVALS_DIR
from souq_scripts.templates import (
    available_versions,
    check_all,
    index_name,
    load_template,
    validate_template,
)

# Only mapped fields are sent, None values dropped, vectors rounded to 5 decimals and zero vectors omitted
# (cosinesimil rejects them) — the same document shape services/indexer builds for live events.
DOC_FIELDS: tuple[str, ...] = (
    "sku", "brand", "category_path", "category_levels", "price", "currency", "colour", "size", "attributes",
    "title_en", "title_ar", "description_en", "description_ar", "updated_at", "version",
)  # fmt: skip
EMBED_DECIMALS = 5
# Changes are found by products.updated_at, which is set at transaction start; a long transaction can
# commit after the moment we cut over. Re-sending a few minutes of changes is cheap and idempotent.
CATCH_UP_MARGIN = timedelta(minutes=5)

BUILTIN_GOLDEN_QUERIES: tuple[str, ...] = (
    "headphones", "wireless earbuds", "phone case", "usb c charger", "running shoes", "coffee maker",
    "dress", "backpack", "toy", "book",
    "سماعات", "حذاء", "حقيبة", "هاتف", "قهوة", "فستان",
)  # fmt: skip


class ReindexError(RuntimeError):
    """A precondition or verification failed; the aliases were left as they were before the failing step."""


def log(msg: str) -> None:
    print(f"[reindex] {msg}", file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- sources -----------------------------------------------------------------------------------------


class DocumentSource(Protocol):
    def count(self) -> int: ...

    def batches(
        self, *, after: str | None, size: int, changed_since: datetime | None = None
    ) -> Iterator[list[dict[str, Any]]]: ...

    def existing(self, skus: Sequence[str]) -> set[str]: ...


class PostgresSource:
    """product_document() rows in SKU order (keyset pagination, one short query per batch)."""

    def __init__(self, database_url: str | None = None) -> None:
        from souq_scripts.db import connect

        self._conn = connect(database_url, autocommit=True)

    def count(self) -> int:
        row = self._conn.execute("SELECT count(*) FROM products").fetchone()
        return int(row[0]) if row else 0

    def batches(
        self, *, after: str | None, size: int, changed_since: datetime | None = None
    ) -> Iterator[list[dict[str, Any]]]:
        last = after or ""
        while True:
            if changed_since is None:
                rows = self._conn.execute(
                    "SELECT product_document(sku) FROM products WHERE sku > %s ORDER BY sku LIMIT %s",
                    (last, size),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT product_document(sku) FROM products
                        WHERE sku > %s AND updated_at >= %s ORDER BY sku LIMIT %s""",
                    (last, changed_since, size),
                ).fetchall()
            docs = [r[0] for r in rows if r[0] is not None]
            if not docs:
                return
            yield docs
            last = docs[-1]["sku"]

    def existing(self, skus: Sequence[str]) -> set[str]:
        rows = self._conn.execute("SELECT sku FROM products WHERE sku = ANY(%s)", (list(skus),)).fetchall()
        return {str(r[0]) for r in rows}

    def close(self) -> None:
        self._conn.close()


# --- embedding cache ---------------------------------------------------------------------------------


class CachedEmbedder:
    """Content-addressed document-vector cache in SQLite, keyed by (embedder name, document text).

    Every backfill (init, --to, a rollback rebuild) embeds the same texts, and the index cannot hand its
    vectors back (``embedding`` is excluded from ``_source`` to halve storage), so without this each
    reindex would re-run bge-m3 over the whole catalog (~20 min per 50k docs on a laptop CPU).
    """

    def __init__(self, inner: Embedder, name: str, path: Path) -> None:
        self.inner = inner
        self.dim = inner.dim
        self.name = name
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("CREATE TABLE IF NOT EXISTS vec (key TEXT PRIMARY KEY, v BLOB NOT NULL)")
        self.hits = self.misses = 0

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.name}\n{text}".encode()).hexdigest()

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self.inner.encode_queries(texts)

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        keys = [self._key(t) for t in texts]
        found: dict[str, bytes] = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            marks = ",".join("?" * len(chunk))
            found.update(self._db.execute(f"SELECT key, v FROM vec WHERE key IN ({marks})", chunk).fetchall())
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        missing = [i for i, k in enumerate(keys) if k not in found]
        if missing:
            fresh = np.asarray(self.inner.encode_documents([texts[i] for i in missing]), dtype=np.float32)
            self._db.executemany(
                "INSERT OR REPLACE INTO vec VALUES (?, ?)",
                [(keys[i], fresh[j].tobytes()) for j, i in enumerate(missing)],
            )
            self._db.commit()
            out[missing] = fresh
        for i, k in enumerate(keys):
            if k in found:
                out[i] = np.frombuffer(found[k], dtype=np.float32)
        self.hits += len(texts) - len(missing)
        self.misses += len(missing)
        return out


# --- golden queries ----------------------------------------------------------------------------------


def _queries_from_file(path: Path) -> list[str]:
    out: list[str] = []
    keys = ("q", "query", "text")
    if path.suffix == ".jsonl":
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                out += [rec[k] for k in keys if isinstance(rec, dict) and isinstance(rec.get(k), str)][:1]
    elif path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("queries", data.get("items", [])) if isinstance(data, dict) else data
        for rec in items if isinstance(items, list) else []:
            if isinstance(rec, str):
                out.append(rec)
            elif isinstance(rec, dict):
                out += [rec[k] for k in keys if isinstance(rec.get(k), str)][:1]
    elif path.suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as fh:
            for rec in csv.DictReader(fh):
                out += [rec[k] for k in keys if rec.get(k)][:1]
    else:  # plain text: one query per line, '#' comments
        out = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        out = [q for q in out if not q.startswith("#")]
    return out


def golden_queries_from(path: Path) -> list[str]:
    """Pinned smoke-test queries (any catalog other than the ESCI seed needs its own list)."""
    qs = list(dict.fromkeys(q.strip() for q in _queries_from_file(path) if q.strip()))
    if not qs:
        raise ReindexError(f"no queries in {path}")
    return qs


def golden_queries(evals_dir: Path = EVALS_DIR, limit: int = 24) -> tuple[list[str], str]:
    """Smoke-test queries from the eval golden set if one exists, else a built-in bilingual list.

    Reads every slice file (evals/golden/*.jsonl, or golden*.{jsonl,json,csv} anywhere under evals/) and
    samples evenly across them, so each language slice is represented.
    """
    candidates = sorted(
        {
            p
            for pattern in ("golden/*.jsonl", "**/golden*.jsonl", "**/golden*.json", "**/golden*.csv")
            for p in evals_dir.glob(pattern)
            if "reports" not in p.parts and p.is_file()
        }
    )
    qs: list[str] = []
    used: list[str] = []
    for path in candidates:
        try:
            found = [q.strip() for q in _queries_from_file(path) if q.strip()]
        except (OSError, ValueError, KeyError):
            continue
        if found:
            qs += found
            used.append(path.name)
    qs = list(dict.fromkeys(qs))
    if not qs:
        return list(BUILTIN_GOLDEN_QUERIES), "built-in"
    step = max(1, len(qs) // limit)
    return qs[::step][:limit], f"{evals_dir}/{{{','.join(used)}}}"


def smoke_query_text(q: str) -> str:
    """The query as the search service sees it: normalised, plus the Arabic rendering of Arabizi words."""
    nq = normalise_query(q)
    return " ".join(t for t in (nq.text, nq.arabizi_ar) if t)


# --- the reindexer -----------------------------------------------------------------------------------


@dataclass
class BulkStats:
    indexed: int = 0
    conflicts: int = 0  # 409: a newer version is already in the index (live write won), which is success
    retried: int = 0


@dataclass
class Options:
    batch_size: int = 250
    checkpoint_every: int = 10
    alias_settle_s: float = 12.0
    count_tolerance: int = 0
    max_retries: int = 5
    full: bool = False  # rebuild the target from Postgres instead of an incremental catch-up
    sleep: Callable[[float], None] = time.sleep
    golden: Sequence[str] = field(default_factory=lambda: list(BUILTIN_GOLDEN_QUERIES))


def _not_found(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 404 or type(exc).__name__ == "NotFoundError"


class Reindexer:
    def __init__(
        self,
        client: Any,
        source: DocumentSource,
        *,
        embedder_factory: Callable[[], Embedder] = get_embedder,
        embedder_name: str | None = None,
        read_alias: str | None = None,
        write_alias: str | None = None,
        options: Options | None = None,
    ) -> None:
        settings = get_settings()
        self.client = client
        self.source = source
        self._embedder_factory = embedder_factory
        self._embedder: Embedder | None = None
        self.embedder_name = embedder_name or settings.souq_embedder
        self.read_alias = read_alias or settings.read_alias
        self.write_alias = write_alias or settings.write_alias
        self.opt = options or Options()

    # -- state ----------------------------------------------------------------------------------------

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = self._embedder_factory()
        return self._embedder

    def alias_targets(self, alias: str) -> set[str]:
        try:
            resp = self.client.indices.get_alias(name=alias)
        except Exception as exc:
            if _not_found(exc):
                return set()
            raise
        return set(resp)

    def exists(self, index: str) -> bool:
        return bool(self.client.indices.exists(index=index))

    def meta(self, index: str) -> dict[str, Any]:
        resp = self.client.indices.get_mapping(index=index)
        souq: dict[str, Any] = resp[index]["mappings"].get("_meta", {}).get("souq", {})
        return souq

    def update_meta(self, index: str, **changes: Any) -> dict[str, Any]:
        # Mapping _meta is replaced wholesale on put_mapping, so read-modify-write the whole object.
        resp = self.client.indices.get_mapping(index=index)
        meta = dict(resp[index]["mappings"].get("_meta", {}))
        souq = dict(meta.get("souq", {}))
        for k, v in changes.items():
            if isinstance(v, dict) and isinstance(souq.get(k), dict):
                souq[k] = {**souq[k], **v}
            else:
                souq[k] = v
        meta["souq"] = souq
        self.client.indices.put_mapping(index=index, body={"_meta": meta})
        return souq

    def describe(self) -> str:
        return f"{self.read_alias} -> {sorted(self.alias_targets(self.read_alias)) or '-'}, " + (
            f"{self.write_alias} -> {sorted(self.alias_targets(self.write_alias)) or '-'}"
        )

    # -- building blocks ------------------------------------------------------------------------------

    def ensure_index(self, version: str) -> str:
        name = index_name(version)
        template = load_template(version)
        errors = validate_template(template, version)
        if errors:
            raise ReindexError(f"template {name} invalid: {'; '.join(errors)}")
        self.client.indices.put_index_template(name=name, body=template)
        if not self.exists(name):
            log(f"creating index {name}")
            self.client.indices.create(index=name)
            self.update_meta(name, created_at=now_iso())
        return name

    def guard_alias_names(self) -> None:
        # An indexer that starts before --init would auto-create a concrete index named like the alias,
        # after which the alias can never be created. Refuse loudly instead of half-initialising.
        for alias in (self.read_alias, self.write_alias):
            if self.exists(alias) and not self.alias_targets(alias):
                raise ReindexError(
                    f"a concrete index named {alias!r} exists (auto-created by an early writer?); "
                    f"delete it, then re-run --init"
                )

    def block_alias_autocreate(self) -> None:
        settings = self.client.cluster.get_settings(include_defaults=False)
        current = settings.get("persistent", {}).get("action", {}).get("auto_create_index")
        value = f"-{self.read_alias},-{self.write_alias},+*"
        if current == value:
            return
        if current not in (None, "true", True):
            log(f"leaving custom action.auto_create_index={current!r} untouched")
            return
        self.client.cluster.put_settings(body={"persistent": {"action.auto_create_index": value}})

    def _doc(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {k: raw[k] for k in DOC_FIELDS if raw.get(k) is not None}

    def _embed(self, docs: list[dict[str, Any]]) -> None:
        vectors = np.asarray(
            self.embedder.encode_documents([document_text(d) for d in docs]), dtype=np.float32
        )
        if vectors.shape != (len(docs), self.embedder.dim):
            raise ReindexError(
                f"embedder returned {vectors.shape}, expected ({len(docs)}, {self.embedder.dim})"
            )
        norms = np.linalg.norm(vectors, axis=1)
        for doc, vec, norm in zip(docs, np.round(vectors, EMBED_DECIMALS), norms, strict=True):
            if norm > 0 and np.isfinite(norm):
                doc["embedding"] = vec.tolist()

    def bulk_index(self, index: str, docs: list[dict[str, Any]], stats: BulkStats) -> None:
        pending = docs
        for attempt in range(self.opt.max_retries + 1):
            body: list[dict[str, Any]] = []
            for d in pending:
                body.append(
                    {
                        "index": {
                            "_index": index,
                            "_id": d["sku"],
                            "version": d["version"],
                            "version_type": "external",
                        }
                    }
                )
                body.append(d)
            resp = self.client.bulk(body=body)
            retry: list[dict[str, Any]] = []
            failures: list[str] = []
            for doc, item in zip(pending, resp["items"], strict=True):
                result = item.get("index", {})
                status = int(result.get("status", 500))
                if 200 <= status < 300:
                    stats.indexed += 1
                elif status == 409:
                    stats.conflicts += 1
                elif status == 429 or status >= 500:
                    retry.append(doc)
                else:
                    failures.append(f"{doc['sku']}: {status} {result.get('error')}")
            if failures:
                raise ReindexError(f"{len(failures)} documents rejected by {index}, e.g. {failures[:3]}")
            if not retry:
                return
            stats.retried += len(retry)
            pending = retry
            self.opt.sleep(min(30.0, 0.5 * 2**attempt))
        raise ReindexError(
            f"{len(pending)} documents still rejected (429/5xx) after {self.opt.max_retries} retries"
        )

    def backfill(self, index: str, *, changed_since: datetime | None = None) -> BulkStats:
        """Full (resumable) backfill, or an incremental catch-up when ``changed_since`` is given."""
        souq = self.meta(index)
        recorded = souq.get("backfill", {}).get("embedder")
        if recorded and recorded != self.embedder_name:
            raise ReindexError(
                f"{index} holds {recorded!r} vectors but SOUQ_EMBEDDER={self.embedder_name!r}; "
                "mixing embedders breaks knn — use a fresh index version"
            )
        incremental = changed_since is not None
        after = None if incremental else souq.get("backfill", {}).get("last_sku")
        stats = BulkStats()
        base = 0 if incremental else int(souq.get("backfill", {}).get("indexed", 0))
        if after:
            log(f"resuming backfill of {index} after sku {after!r} ({base} docs already indexed)")
        since = f" changed since {changed_since.isoformat()}" if changed_since else ""
        log(f"backfilling {index} from Postgres{since} (embedder={self.embedder_name})")
        started = time.monotonic()
        for n, batch in enumerate(
            self.source.batches(after=after, size=self.opt.batch_size, changed_since=changed_since), 1
        ):
            docs = [self._doc(d) for d in batch]
            self._embed(docs)
            self.bulk_index(index, docs, stats)
            if not incremental and n % self.opt.checkpoint_every == 0:
                self.update_meta(
                    index,
                    backfill={"done": False, "last_sku": docs[-1]["sku"], "indexed": base + stats.indexed,
                              "embedder": self.embedder_name},
                )  # fmt: skip
                rate = (stats.indexed + stats.conflicts) / max(1e-6, time.monotonic() - started)
                log(f"  {base + stats.indexed} docs ({rate:.0f}/s), last sku {docs[-1]['sku']}")
        log(
            f"backfill done: {stats.indexed} indexed, {stats.conflicts} already newer, "
            f"{stats.retried} retried"
        )
        return stats

    def prune(self, index: str) -> int:
        """Delete docs whose SKU no longer exists in Postgres (deletes an index missed while detached).

        Conditional on the seq_no seen during the scan, so a doc re-created by a live write meanwhile
        survives (its delete comes back 409 and is ignored).
        """
        deleted = 0
        after: list[Any] | None = None
        while True:
            body: dict[str, Any] = {"size": 1000, "_source": False, "seq_no_primary_term": True,
                                    "sort": [{"sku": "asc"}], "query": {"match_all": {}}}  # fmt: skip
            if after:
                body["search_after"] = after
            hits = self.client.search(index=index, body=body)["hits"]["hits"]
            if not hits:
                return deleted
            alive = self.source.existing([h["_id"] for h in hits])
            gone = [h for h in hits if h["_id"] not in alive]
            if gone:
                actions = [
                    {"delete": {"_index": index, "_id": h["_id"], "if_seq_no": h["_seq_no"],
                                "if_primary_term": h["_primary_term"]}}
                    for h in gone
                ]  # fmt: skip
                resp = self.client.bulk(body=actions)
                deleted += sum(1 for it in resp["items"] if 200 <= int(it["delete"]["status"]) < 300)
            after = hits[-1]["sort"]

    def verify_count(self, index: str) -> int:
        for attempt in range(3):
            self.client.indices.refresh(index=index)
            in_index = int(self.client.count(index=index)["count"])
            in_db = self.source.count()
            if abs(in_index - in_db) <= self.opt.count_tolerance:
                log(f"count ok: {index}={in_index}, postgres={in_db}")
                return in_index
            log(f"count mismatch {index}={in_index} postgres={in_db} (attempt {attempt + 1}/3)")
            self.opt.sleep(2.0)
        raise ReindexError(f"{index} has {in_index} docs but Postgres has {in_db}; not swapping")

    def smoke(self, index: str) -> None:
        if self.source.count() == 0:
            log("catalog is empty; skipping golden-query smoke test")
            return
        failures = []
        for q in self.opt.golden:
            fields = ["title_en^3", "title_ar^3", "brand^2", "description_en", "description_ar"]
            body = {"size": 1, "query": {"multi_match": {"query": smoke_query_text(q), "fields": fields}}}
            if not self.client.search(index=index, body=body)["hits"]["hits"]:
                failures.append(q)
        vec = np.asarray(self.embedder.encode_queries([self.opt.golden[0]]), dtype=np.float32)[0]
        knn = {"size": 1, "query": {"knn": {"embedding": {"vector": vec.tolist(), "k": 5}}}}
        if not self.client.search(index=index, body=knn)["hits"]["hits"]:
            failures.append(f"knn:{self.opt.golden[0]}")
        if failures:
            raise ReindexError(f"golden queries returned no hits on {index}: {failures}")
        log(f"smoke ok: {len(self.opt.golden)} golden queries + knn returned hits on {index}")

    def add_to_write_alias(self, index: str) -> bool:
        if index in self.alias_targets(self.write_alias):
            return False
        log(f"adding {index} to {self.write_alias} (dual-write on)")
        self.client.indices.update_aliases(
            body={"actions": [{"add": {"index": index, "alias": self.write_alias}}]}
        )
        return True

    def swap_read_alias(self, target: str) -> list[str]:
        current = sorted(self.alias_targets(self.read_alias))
        if current == [target]:
            return []
        actions: list[dict[str, Any]] = [
            {"remove": {"index": i, "alias": self.read_alias}} for i in current if i != target
        ]
        actions.append({"add": {"index": target, "alias": self.read_alias}})
        # One _aliases call: readers see either the old index or the new one, never neither or both.
        self.client.indices.update_aliases(body={"actions": actions})
        log(f"{self.read_alias}: {current or '-'} -> {target}")
        return [i for i in current if i != target]

    def detach_from_write_alias(self, keep: str) -> list[str]:
        others = sorted(self.alias_targets(self.write_alias) - {keep})
        if others:
            self.client.indices.update_aliases(
                body={"actions": [{"remove": {"index": i, "alias": self.write_alias}} for i in others]}
            )
            for i in others:
                # From now on this index misses writes; a rollback must catch it up from this moment.
                self.update_meta(i, detached_at=now_iso())
            log(f"{self.write_alias}: removed {others} (dual-write off)")
        return others

    def _settle(self) -> None:
        if self.opt.alias_settle_s > 0:
            log(
                f"waiting {self.opt.alias_settle_s:.0f}s for writers to pick up the {self.write_alias} change"
            )
            self.opt.sleep(self.opt.alias_settle_s)

    def _template_refresh_interval(self, index: str) -> str:
        # Restore from the template, not from the live setting: a crashed run leaves "-1" behind.
        version = index.removeprefix("products_")
        value = load_template(version)["template"]["settings"].get("index", {}).get("refresh_interval", "1s")
        return str(value)

    def _counts_match(self, index: str) -> bool:
        self.client.indices.refresh(index=index)
        return (
            abs(int(self.client.count(index=index)["count"]) - self.source.count())
            <= self.opt.count_tolerance
        )

    def _bring_up_to_date(self, index: str, *, force_full: bool = False) -> None:
        """Make ``index`` (already in the write alias) complete: full/resumed backfill or catch-up + prune."""
        souq = self.meta(index)
        detached_at = souq.get("detached_at")
        done = bool(souq.get("backfill", {}).get("done"))
        if done and not detached_at and not force_full:
            log(f"{index}: backfill already complete")
            return
        if force_full and done:
            self.update_meta(index, backfill={"done": False, "last_sku": None, "indexed": 0})
        self._settle()
        self.client.indices.put_settings(index=index, body={"index": {"refresh_interval": "-1"}})
        try:
            if detached_at and done and not force_full:
                # Catch up from the earlier of "left the write alias" and "last verified complete": rows
                # written straight to Postgres (seeds, bulk translation loads) never pass through the
                # indexer, so being in the write alias does not mean an index received them.
                marks = [datetime.fromisoformat(detached_at)]
                finished_at = souq.get("backfill", {}).get("finished_at")
                if finished_at:
                    marks.append(datetime.fromisoformat(finished_at))
                since = min(marks) - CATCH_UP_MARGIN
                self.backfill(index, changed_since=since)
                pruned = self.prune(index)
                log(f"pruned {pruned} docs deleted from Postgres while {index} was detached")
                if not self._counts_match(index):
                    # The index was not complete when it was detached (e.g. rows bulk-loaded without
                    # outbox events), so an incremental catch-up cannot fix it: rebuild it.
                    log(f"{index} still differs from Postgres after catch-up; running a full backfill")
                    self.update_meta(index, backfill={"done": False, "last_sku": None, "indexed": 0})
                    self.backfill(index)
            else:
                self.backfill(index)
        finally:
            self.client.indices.put_settings(
                index=index, body={"index": {"refresh_interval": self._template_refresh_interval(index)}}
            )
        self.update_meta(
            index,
            detached_at=None,
            backfill={
                "done": True,
                "last_sku": None,
                "embedder": self.embedder_name,
                "finished_at": now_iso(),
            },
        )

    # -- commands -------------------------------------------------------------------------------------

    def init(self, version: str, *, backfill: bool = True) -> None:
        self.guard_alias_names()
        name = self.ensure_index(version)
        self.block_alias_autocreate()
        actions = []
        if not self.alias_targets(self.read_alias):
            actions.append({"add": {"index": name, "alias": self.read_alias}})
        if not self.alias_targets(self.write_alias):
            actions.append({"add": {"index": name, "alias": self.write_alias}})
        if actions:
            self.client.indices.update_aliases(body={"actions": actions})
        state = self.describe()
        if name not in self.alias_targets(self.read_alias):
            log(f"aliases already initialised ({state}); use --to {version} to migrate")
            return
        if backfill:
            self.update_meta(name, promoted_at=self.meta(name).get("promoted_at") or now_iso())
            # The index is live in both aliases already, so no settle wait is needed: anything the
            # backfill misses is being written to it by the indexer.
            # --init is the bootstrap ("make this index match Postgres"), so it always re-sends every row;
            # external versioning turns unchanged rows into cheap 409s.
            settle, self.opt.alias_settle_s = self.opt.alias_settle_s, 0.0
            try:
                self._bring_up_to_date(name, force_full=True)
            finally:
                self.opt.alias_settle_s = settle
            self.verify_count(name)
        log(f"init done: {self.describe()}")

    def migrate_to(self, version: str) -> None:
        target = index_name(version)
        read = self.alias_targets(self.read_alias)
        if not read or not self.alias_targets(self.write_alias):
            raise ReindexError(f"aliases missing ({self.describe()}); run --init first")
        if read == {target} and self.alias_targets(self.write_alias) == {target}:
            log(f"nothing to do: {self.describe()}")
            return
        self.ensure_index(version)
        self.add_to_write_alias(target)
        self._bring_up_to_date(target, force_full=self.opt.full)
        self.verify_count(target)
        self.smoke(target)
        previous = sorted(read - {target})
        if previous:
            # Recorded before the swap so a crash between the two still leaves a rollback pointer.
            self.update_meta(target, previous_index=previous[0])
        if self.swap_read_alias(target):
            self.update_meta(target, promoted_at=now_iso())
        self.detach_from_write_alias(keep=target)
        log(f"migrated: {self.describe()}")

    def rollback(self, to_version: str | None = None) -> None:
        read = sorted(self.alias_targets(self.read_alias))
        if len(read) != 1:
            raise ReindexError(f"expected exactly one index behind {self.read_alias}, found {read}")
        current = read[0]
        previous = index_name(to_version) if to_version else self.meta(current).get("previous_index")
        if not previous:
            raise ReindexError(f"{current} records no previous index; pass --to <version>")
        if previous == current:
            log(f"{self.read_alias} already on {current}")
            return
        if not self.exists(previous):
            raise ReindexError(f"previous index {previous} no longer exists")
        log(f"rolling back {self.read_alias}: {current} -> {previous}")
        self.add_to_write_alias(previous)
        self._bring_up_to_date(previous, force_full=self.opt.full)
        self.verify_count(previous)
        self.smoke(previous)
        self.swap_read_alias(previous)
        self.update_meta(previous, promoted_at=now_iso(), previous_index=current)
        self.detach_from_write_alias(keep=previous)
        log(f"rolled back: {self.describe()}")

    def plan(self, mode: str, version: str | None) -> list[str]:
        """Human-readable steps the command would take from the current cluster state (read-only)."""
        read, write = self.alias_targets(self.read_alias), self.alias_targets(self.write_alias)
        steps = [
            f"current: {self.read_alias} -> {sorted(read) or '-'}, "
            f"{self.write_alias} -> {sorted(write) or '-'}"
        ]
        if mode == "init" and version:
            name = index_name(version)
            steps.append(f"put template {name}" + ("" if self.exists(name) else f"; create index {name}"))
            if not read or not write:
                steps.append(f"point missing aliases at {name}; backfill {name} from Postgres; verify count")
            else:
                steps.append("aliases already initialised: no alias change")
        elif mode == "to" and version:
            name = index_name(version)
            if read == {name} and write == {name}:
                return [*steps, "nothing to do"]
            steps.append(f"put template {name}" + ("" if self.exists(name) else f"; create index {name}"))
            if name not in write:
                steps.append(f"add {name} to {self.write_alias}; wait {self.opt.alias_settle_s:.0f}s")
            souq = self.meta(name) if self.exists(name) else {}
            if souq.get("detached_at"):
                steps.append(
                    f"catch up {name}: rows changed since {souq['detached_at']} - 5 min, prune deletes"
                )
            elif not souq.get("backfill", {}).get("done"):
                after = souq.get("backfill", {}).get("last_sku")
                steps.append(
                    f"backfill {name} from Postgres" + (f" resuming after {after!r}" if after else "")
                )
            steps += [
                f"verify count {name} == Postgres; smoke {len(self.opt.golden)} golden queries + knn",
                f"atomically swap {self.read_alias}: {sorted(read)} -> {name}",
                f"remove {sorted(write - {name})} from {self.write_alias}",
            ]
        elif mode == "rollback":
            cur = sorted(read)
            prev = (
                index_name(version) if version else (self.meta(cur[0]).get("previous_index") if cur else None)
            )
            steps += [
                f"add {prev} to {self.write_alias}; catch up rows changed since detach; prune deletes",
                f"verify + smoke {prev}; atomically swap {self.read_alias}: {cur} -> {prev}",
                f"remove {cur} from {self.write_alias} (marked stale)",
            ]
        return steps


# --- CLI ---------------------------------------------------------------------------------------------


def make_client(url: str) -> Any:
    from opensearchpy import OpenSearch

    return OpenSearch(url, timeout=60, max_retries=3, retry_on_timeout=True, http_compress=True)


def dry_run(args: argparse.Namespace, mode: str, version: str | None) -> int:
    errors = check_all()
    for e in errors:
        log(f"template error: {e}")
    if errors:
        return 1
    log("templates valid")
    client = make_client(args.opensearch_url)
    try:
        reachable = bool(client.ping())
    except Exception:
        reachable = False
    if not reachable:
        log(f"OpenSearch at {args.opensearch_url} unreachable: validated templates offline only")
        return 0

    class _NoSource:
        def count(self) -> int:
            return 0

        def batches(self, **_: Any) -> Iterator[list[dict[str, Any]]]:
            return iter(())

        def existing(self, skus: Sequence[str]) -> set[str]:
            return set(skus)

    reindexer = Reindexer(client, _NoSource(), options=Options(golden=args.golden))
    if mode == "auto":
        initialised = reindexer.alias_targets(reindexer.read_alias)
        mode, version = ("to", version) if initialised else ("init", available_versions()[0])
        log(f"plan for: --{mode} {version}")
    for step in reindexer.plan(mode, version):
        log(f"plan: {step}")
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cmd = ap.add_mutually_exclusive_group()
    cmd.add_argument(
        "--init", metavar="VERSION", help="create products_<VERSION> and point both aliases at it"
    )
    cmd.add_argument("--to", metavar="VERSION", help="migrate the aliases to products_<VERSION>")
    cmd.add_argument("--rollback", action="store_true", help="swap products_read back to the previous index")
    ap.add_argument("--rollback-to", metavar="VERSION", help="explicit rollback target (default: recorded)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and validate templates only")
    ap.add_argument("--no-backfill", action="store_true", help="--init: create index + aliases only")
    ap.add_argument("--opensearch-url", default=get_settings().opensearch_url)
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--batch-size", type=int, default=250)
    ap.add_argument("--alias-settle-s", type=float, default=12.0, help="> indexer alias refresh (10 s)")
    ap.add_argument("--count-tolerance", type=int, default=0)
    ap.add_argument("--no-embedding-cache", action="store_true", help="always re-embed documents")
    ap.add_argument("--full", action="store_true", help="--to/--rollback: rebuild the target from Postgres")
    ap.add_argument(
        "--golden-file",
        type=Path,
        help="smoke-test queries: .txt (one per line), .jsonl/.json/.csv with a q/query/text field",
    )
    args = ap.parse_args(argv)
    if args.golden_file:
        args.golden, golden_src = golden_queries_from(args.golden_file), str(args.golden_file)
    else:
        args.golden, golden_src = golden_queries()

    if not (args.init or args.to or args.rollback):
        if not args.dry_run:
            ap.error("one of --init, --to, --rollback is required (or --dry-run alone to validate + plan)")
        # Bare --dry-run (CI): plan the natural next step for whatever state the cluster is in.
        latest = available_versions()[-1]
        sys.exit(dry_run(args, "auto", latest))
    mode = "init" if args.init else "to" if args.to else "rollback"
    version = args.init or args.to or args.rollback_to
    if args.dry_run:
        sys.exit(dry_run(args, mode, version))

    log(f"golden queries: {len(args.golden)} from {golden_src}")
    source = PostgresSource(args.database_url)
    name = get_settings().souq_embedder

    def embedder() -> Embedder:
        inner = get_embedder()
        if args.no_embedding_cache:
            return inner
        return CachedEmbedder(inner, name, CACHE_DIR / f"embeddings-{name}.sqlite")

    reindexer = Reindexer(
        make_client(args.opensearch_url),
        source,
        embedder_factory=embedder,
        embedder_name=name,
        options=Options(
            batch_size=args.batch_size,
            alias_settle_s=args.alias_settle_s,
            count_tolerance=args.count_tolerance,
            full=args.full,
            golden=args.golden,
        ),
    )
    try:
        if mode == "init":
            reindexer.init(args.init, backfill=not args.no_backfill)
        elif mode == "to":
            reindexer.migrate_to(args.to)
        else:
            reindexer.rollback(args.rollback_to)
    except ReindexError as exc:
        log(f"FAILED: {exc}")
        sys.exit(1)
    finally:
        with contextlib.suppress(Exception):
            source.close()


if __name__ == "__main__":
    main()
