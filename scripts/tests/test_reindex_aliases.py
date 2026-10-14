"""Alias choreography of scripts/reindex.py against an in-memory OpenSearch double (no cluster needed)."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from souq_common.embeddings import FakeEmbedder
from souq_scripts.reindex import BUILTIN_GOLDEN_QUERIES, Options, Reindexer, ReindexError, golden_queries
from souq_scripts.templates import load_template

MUTATING = {"create", "put_index_template", "update_aliases", "put_mapping", "put_settings", "bulk",
            "cluster.put_settings"}  # fmt: skip


class NotFoundError(Exception):
    status_code = 404


class FakeIndices:
    def __init__(self, os: FakeOpenSearch) -> None:
        self.os = os

    def exists(self, index: str) -> bool:
        self.os.log("exists", index)
        return index in self.os.indices or any(index in a for a in self.os.aliases.values())

    def put_index_template(self, name: str, body: dict[str, Any]) -> None:
        self.os.log("put_index_template", name)
        self.os.templates[name] = body

    def create(self, index: str, body: dict[str, Any] | None = None) -> None:
        self.os.log("create", index)
        tpl = next((t for t in self.os.templates.values() if index in t["index_patterns"]), None)
        assert tpl is not None, f"no template for {index}"
        t = copy.deepcopy(tpl["template"])
        self.os.indices[index] = {
            "mappings": t["mappings"],
            "settings": {"index": dict(t["settings"]["index"])},
        }
        self.os.aliases[index] = set()
        self.os.docs[index] = {}

    def get_alias(self, name: str) -> dict[str, Any]:
        self.os.log("get_alias", name)
        hit = {i: {"aliases": {name: {}}} for i, a in self.os.aliases.items() if name in a}
        if not hit:
            raise NotFoundError(name)
        return hit

    def update_aliases(self, body: dict[str, Any]) -> None:
        self.os.log("update_aliases", body)
        aliases = copy.deepcopy(self.os.aliases)
        for action in body["actions"]:
            ((op, spec),) = action.items()
            if op == "add":
                aliases[spec["index"]].add(spec["alias"])
            else:
                aliases[spec["index"]].discard(spec["alias"])
        self.os.aliases = aliases  # all-or-nothing, like the real _aliases API

    def get_mapping(self, index: str) -> dict[str, Any]:
        return {index: {"mappings": copy.deepcopy(self.os.indices[index]["mappings"])}}

    def put_mapping(self, index: str, body: dict[str, Any]) -> None:
        self.os.log("put_mapping", index)
        self.os.indices[index]["mappings"]["_meta"] = copy.deepcopy(body["_meta"])

    def get_settings(self, index: str) -> dict[str, Any]:
        return {index: {"settings": copy.deepcopy(self.os.indices[index]["settings"])}}

    def put_settings(self, index: str, body: dict[str, Any]) -> None:
        self.os.log("put_settings", index, body)
        self.os.indices[index]["settings"]["index"].update(body["index"])

    def refresh(self, index: str) -> None:
        self.os.log("refresh", index)


class FakeCluster:
    def __init__(self, os: FakeOpenSearch) -> None:
        self.os = os
        self.persistent: dict[str, Any] = {}

    def get_settings(self, include_defaults: bool = False) -> dict[str, Any]:
        return {"persistent": copy.deepcopy(self.persistent), "transient": {}}

    def put_settings(self, body: dict[str, Any]) -> None:
        self.os.log("cluster.put_settings", body)
        for k, v in body["persistent"].items():
            node = self.persistent
            *parents, leaf = k.split(".")
            for p in parents:
                node = node.setdefault(p, {})
            node[leaf] = v


class FakeOpenSearch:
    def __init__(self) -> None:
        self.indices: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, set[str]] = {}
        self.templates: dict[str, dict[str, Any]] = {}
        self.docs: dict[str, dict[str, dict[str, Any]]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.seq = 0
        self.fail_bulk_after: int | None = None
        self.indices_api = FakeIndices(self)
        self.cluster = FakeCluster(self)

    def log(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def alias(self, name: str) -> set[str]:
        return {i for i, a in self.aliases.items() if name in a}

    def bulk(self, body: list[dict[str, Any]]) -> dict[str, Any]:
        self.log("bulk", len(body))
        if self.fail_bulk_after is not None:
            if self.fail_bulk_after == 0:
                raise ConnectionError("cluster went away")
            self.fail_bulk_after -= 1
        items: list[dict[str, Any]] = []
        it = iter(body)
        for action in it:
            ((op, meta),) = action.items()
            store = self.docs[meta["_index"]]
            if op == "index":
                doc = next(it)
                cur = store.get(meta["_id"])
                if cur and cur["version"] >= meta["version"]:
                    items.append(
                        {"index": {"status": 409, "error": {"type": "version_conflict_engine_exception"}}}
                    )
                    continue
                self.seq += 1
                store[meta["_id"]] = {"version": meta["version"], "source": doc, "seq_no": self.seq}
                items.append({"index": {"status": 201 if cur is None else 200}})
            elif op == "delete":
                cur = store.get(meta["_id"])
                if cur is None or cur["seq_no"] != meta["if_seq_no"]:
                    items.append({"delete": {"status": 409}})
                else:
                    del store[meta["_id"]]
                    items.append({"delete": {"status": 200}})
        return {"items": items, "errors": any(i[next(iter(i))]["status"] >= 300 for i in items)}

    def count(self, index: str) -> dict[str, int]:
        return {"count": len(self.docs[index])}

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        store = self.docs[index]
        if "sort" in body:
            ids = sorted(store)
            if body.get("search_after"):
                ids = [i for i in ids if i > body["search_after"][0]]
            ids = ids[: body["size"]]
            hits = [{"_id": i, "_seq_no": store[i]["seq_no"], "_primary_term": 1, "sort": [i]} for i in ids]
            return {"hits": {"hits": hits}}
        if "knn" in body["query"]:
            hits = [{"_id": i} for i, d in store.items() if "embedding" in d["source"]][:1]
            return {"hits": {"hits": hits}}
        words = body["query"]["multi_match"]["query"].lower().split()
        hits = [
            {"_id": i}
            for i, d in store.items()
            if any(
                w in str(d["source"].get(f, "")).lower().split() for w in words for f in ("title_en", "brand")
            )
        ]
        return {"hits": {"hits": hits[:1]}}


class Client:
    """What reindex.py sees: the opensearch-py surface it uses, backed by FakeOpenSearch's state."""

    def __init__(self, fake: FakeOpenSearch) -> None:
        self.fake = fake
        self.indices = fake.indices_api
        self.cluster = fake.cluster

    def bulk(self, body: list[dict[str, Any]]) -> dict[str, Any]:
        return self.fake.bulk(body)

    def count(self, index: str) -> dict[str, int]:
        return self.fake.count(index)

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.fake.search(index, body)


class FakeSource:
    def __init__(self, n: int) -> None:
        base = datetime(2026, 10, 1, tzinfo=UTC)
        self.rows: dict[str, dict[str, Any]] = {}
        for i in range(n):
            self.put(f"SKU{i:04d}", f"widget {i}", version=2, updated_at=base)
        self.batch_calls: list[dict[str, Any]] = []

    def put(self, sku: str, title: str, *, version: int, updated_at: datetime | None = None) -> None:
        self.rows[sku] = {
            "sku": sku, "title_en": title, "brand": "Acme", "category_path": "electronics.audio",
            "category_levels": ["electronics", "electronics.audio"], "price": 10.5, "currency": "AED",
            "colour": None, "size": None, "attributes": {}, "title_ar": None, "description_en": None,
            "description_ar": None, "version": version,
            "updated_at": (updated_at or datetime.now(UTC)).isoformat(),
        }  # fmt: skip

    def count(self) -> int:
        return len(self.rows)

    def batches(
        self, *, after: str | None, size: int, changed_since: datetime | None = None
    ) -> Iterator[list[dict[str, Any]]]:
        self.batch_calls.append({"after": after, "changed_since": changed_since})
        skus = sorted(s for s in self.rows if s > (after or ""))
        if changed_since is not None:
            skus = [s for s in skus if datetime.fromisoformat(self.rows[s]["updated_at"]) >= changed_since]
        for i in range(0, len(skus), size):
            yield [copy.deepcopy(self.rows[s]) for s in skus[i : i + size]]

    def existing(self, skus: Sequence[str]) -> set[str]:
        return {s for s in skus if s in self.rows}


@pytest.fixture
def env() -> tuple[FakeOpenSearch, FakeSource, Reindexer, list[float]]:
    fake = FakeOpenSearch()
    source = FakeSource(23)
    sleeps: list[float] = []
    r = Reindexer(
        Client(fake),
        source,
        embedder_factory=FakeEmbedder,
        embedder_name="fake",
        read_alias="products_read",
        write_alias="products_write",
        options=Options(
            batch_size=5, checkpoint_every=1, alias_settle_s=12, sleep=sleeps.append, golden=["widget"]
        ),
    )
    return fake, source, r, sleeps


def alias_calls(fake: FakeOpenSearch) -> list[list[dict[str, Any]]]:
    return [args[0]["actions"] for name, args in fake.calls if name == "update_aliases"]


def test_init_creates_index_points_both_aliases_and_backfills(env: Any) -> None:
    fake, _source, r, sleeps = env
    r.init("v1")
    assert fake.alias("products_read") == {"products_v1"} == fake.alias("products_write")
    assert len(fake.docs["products_v1"]) == 23
    doc = fake.docs["products_v1"]["SKU0003"]
    assert doc["version"] == 2
    assert len(doc["source"]["embedding"]) == 1024
    assert "colour" not in doc["source"], "None fields are dropped like the indexer does"
    assert fake.cluster.persistent["action"]["auto_create_index"] == "-products_read,-products_write,+*"
    assert r.meta("products_v1")["backfill"]["done"] is True
    assert fake.indices["products_v1"]["settings"]["index"]["refresh_interval"] == "1s"
    assert sleeps == [], "init needs no settle wait: the index is already live in both aliases"


def test_init_is_rerunnable_and_resends_rows(env: Any) -> None:
    fake, source, r, _ = env
    r.init("v1")
    source.put("SKU9999", "late widget", version=2)
    r.init("v1")
    assert "SKU9999" in fake.docs["products_v1"]
    assert len(alias_calls(fake)) == 1, "aliases are only created once"


def test_init_refuses_concrete_index_named_like_alias(env: Any) -> None:
    fake, _, r, _ = env
    fake.indices["products_write"] = {"mappings": {}, "settings": {"index": {}}}
    fake.aliases["products_write"] = set()
    with pytest.raises(ReindexError, match="concrete index named 'products_write'"):
        r.init("v1")


def test_migrate_dual_writes_then_backfills_then_swaps_atomically(env: Any) -> None:
    fake, _source, r, sleeps = env
    r.init("v1")
    fake.calls.clear()
    r.migrate_to("v2")

    names = [n for n, _ in fake.calls]
    first_write_alias = names.index("update_aliases")
    first_bulk = names.index("bulk")
    assert names.index("create") < first_write_alias < first_bulk, "dual-write must be on before backfill"
    assert sleeps == [12], "settle wait between joining the write alias and reading Postgres"

    calls = alias_calls(fake)
    assert calls[0] == [{"add": {"index": "products_v2", "alias": "products_write"}}]
    # The read swap is ONE _aliases call holding both the remove and the add.
    assert calls[1] == [
        {"remove": {"index": "products_v1", "alias": "products_read"}},
        {"add": {"index": "products_v2", "alias": "products_read"}},
    ]
    assert calls[2] == [{"remove": {"index": "products_v1", "alias": "products_write"}}]
    assert fake.alias("products_read") == {"products_v2"} == fake.alias("products_write")
    assert len(fake.docs["products_v2"]) == 23
    assert r.meta("products_v2")["previous_index"] == "products_v1"
    assert r.meta("products_v1")["detached_at"]
    # Refresh was disabled for the bulk load and restored from the template afterwards.
    assert fake.indices["products_v2"]["settings"]["index"]["refresh_interval"] == "1s"


def test_live_write_newer_than_backfill_wins(env: Any) -> None:
    fake, _source, r, _ = env
    r.init("v1")
    r.ensure_index("v2")
    r.add_to_write_alias("products_v2")
    # The indexer (dual-writing) already stored version 5 of SKU0001; Postgres snapshot said version 2.
    meta = {"_index": "products_v2", "_id": "SKU0001", "version": 5, "version_type": "external"}
    fake.bulk([{"index": meta}, {"sku": "SKU0001", "title_en": "newer", "version": 5}])
    stats = r.backfill("products_v2")
    assert stats.conflicts == 1
    assert stats.indexed == 22
    assert fake.docs["products_v2"]["SKU0001"]["source"]["title_en"] == "newer"


def test_crashed_backfill_resumes_from_checkpoint(env: Any) -> None:
    fake, source, r, _ = env
    r.init("v1")
    fake.fail_bulk_after = 2  # third bulk of the v2 backfill dies
    with pytest.raises(ConnectionError):
        r.migrate_to("v2")
    assert fake.alias("products_read") == {"products_v1"}, "reads never moved"
    assert fake.alias("products_write") == {"products_v1", "products_v2"}
    checkpoint = r.meta("products_v2")["backfill"]
    assert checkpoint["last_sku"] == "SKU0009"
    assert checkpoint["done"] is False

    fake.fail_bulk_after = None
    source.batch_calls.clear()
    r.migrate_to("v2")
    assert source.batch_calls[0]["after"] == "SKU0009"
    assert fake.alias("products_read") == {"products_v2"}
    assert len(fake.docs["products_v2"]) == 23


def test_count_mismatch_blocks_the_swap(env: Any) -> None:
    fake, _source, r, _sleeps = env
    r.init("v1")
    r.source.count = lambda: 24  # type: ignore[method-assign]
    with pytest.raises(ReindexError, match="Postgres has 24"):
        r.migrate_to("v2")
    assert fake.alias("products_read") == {"products_v1"}


def test_golden_query_without_hits_blocks_the_swap(env: Any) -> None:
    fake, _, r, _ = env
    r.init("v1")
    r.opt.golden = ["widget", "unobtainium"]
    with pytest.raises(ReindexError, match="unobtainium"):
        r.migrate_to("v2")
    assert fake.alias("products_read") == {"products_v1"}


def test_second_run_is_a_noop(env: Any) -> None:
    fake, _, r, _ = env
    r.init("v1")
    r.migrate_to("v2")
    fake.calls.clear()
    r.migrate_to("v2")
    assert not [n for n, _ in fake.calls if n in MUTATING]


def test_crash_between_swap_and_detach_is_finished_on_rerun(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _, r, _ = env
    r.init("v1")

    def boom(keep: str) -> list[str]:
        raise ConnectionError("died after the swap")

    with monkeypatch.context() as m:
        m.setattr(r, "detach_from_write_alias", boom)
        with pytest.raises(ConnectionError):
            r.migrate_to("v2")
    assert fake.alias("products_read") == {"products_v2"}
    assert fake.alias("products_write") == {"products_v1", "products_v2"}
    r.migrate_to("v2")
    assert fake.alias("products_write") == {"products_v2"}
    assert r.meta("products_v2")["previous_index"] == "products_v1"


def test_rollback_catches_up_prunes_and_swaps_back(env: Any) -> None:
    fake, source, r, _ = env
    r.init("v1")
    r.migrate_to("v2")
    # While v1 was detached: one update, one delete, one insert (the indexer only wrote them to v2).
    later = datetime.now(UTC) + timedelta(seconds=1)
    source.put("SKU0002", "widget 2 renamed", version=3, updated_at=later)
    del source.rows["SKU0004"]
    source.put("SKU0500", "new widget", version=2, updated_at=later)
    for sku in ("SKU0002", "SKU0500"):
        fake.bulk([{"index": {"_index": "products_v2", "_id": sku, "version": source.rows[sku]["version"],
                              "version_type": "external"}}, source.rows[sku]])  # fmt: skip
    del fake.docs["products_v2"]["SKU0004"]
    fake.calls.clear()
    source.batch_calls.clear()

    r.rollback()

    assert source.batch_calls[0]["changed_since"] is not None, "incremental catch-up, not a full rebuild"
    v1 = fake.docs["products_v1"]
    assert v1["SKU0002"]["source"]["title_en"] == "widget 2 renamed"
    assert "SKU0500" in v1
    assert "SKU0004" not in v1
    swap = alias_calls(fake)[1]
    assert swap == [
        {"remove": {"index": "products_v2", "alias": "products_read"}},
        {"add": {"index": "products_v1", "alias": "products_read"}},
    ]
    assert fake.alias("products_read") == {"products_v1"} == fake.alias("products_write")
    assert r.meta("products_v2")["detached_at"], "v2 is now stale and remembers since when"


def test_forward_again_after_rollback_only_catches_up(env: Any) -> None:
    fake, source, r, _ = env
    r.init("v1")
    r.migrate_to("v2")
    r.rollback()
    source.batch_calls.clear()
    r.migrate_to("v2")
    assert source.batch_calls[0]["changed_since"] is not None
    assert fake.alias("products_read") == {"products_v2"}


def test_rollback_falls_back_to_full_backfill_when_catch_up_is_not_enough(env: Any) -> None:
    fake, source, r, _ = env
    r.init("v1")
    r.migrate_to("v2")
    # Rows that reached Postgres with an old updated_at and no outbox event (e.g. a bulk seed):
    # an incremental catch-up cannot see them, the count check notices, and a full backfill fixes it.
    source.put("SKU0777", "bulk loaded", version=2, updated_at=datetime(2020, 1, 1, tzinfo=UTC))
    meta = {"_index": "products_v2", "_id": "SKU0777", "version": 2, "version_type": "external"}
    fake.bulk([{"index": meta}, source.rows["SKU0777"]])
    r.rollback()
    assert "SKU0777" in fake.docs["products_v1"]


def test_forward_again_catches_up_rows_written_straight_to_postgres(env: Any) -> None:
    """A bulk load that bypasses the outbox while v2 is live never reaches v2; leaving the write alias later
    must not hide it. Catch-up starts from the earlier of "detached" and "last verified complete"."""
    fake, source, r, _ = env
    r.init("v1")
    r.migrate_to("v2")
    now = datetime.now(UTC)
    # Timeline: v1 last synced and left the write alias 2 h ago, v2 was last verified complete 1 h ago.
    two_h = (now - timedelta(hours=2)).isoformat()
    r.update_meta("products_v1", detached_at=two_h, backfill={"finished_at": two_h})
    r.update_meta("products_v2", backfill={"finished_at": (now - timedelta(hours=1)).isoformat()})
    # An update (not an insert, so counts still match), well over the 5-minute catch-up margin before v2
    # leaves the write alias, written to Postgres only.
    source.put("SKU0003", "translated directly", version=9, updated_at=now - timedelta(minutes=30))
    r.rollback()
    assert fake.docs["products_v1"]["SKU0003"]["source"]["title_en"] == "translated directly"
    r.migrate_to("v2")
    assert fake.docs["products_v2"]["SKU0003"]["source"]["title_en"] == "translated directly"


def test_full_flag_rebuilds_instead_of_catching_up(env: Any) -> None:
    _, source, r, _ = env
    r.init("v1")
    r.migrate_to("v2")
    r.rollback()
    source.batch_calls.clear()
    r.opt.full = True
    r.migrate_to("v2")
    assert source.batch_calls[0]["changed_since"] is None, "--full ignores the catch-up watermark"


def test_embedder_change_is_refused(env: Any) -> None:
    _, _, r, _ = env
    r.init("v1")
    r.embedder_name = "bge-m3"
    with pytest.raises(ReindexError, match="mixing embedders"):
        r.backfill("products_v1")


def test_plan_is_read_only(env: Any) -> None:
    fake, _, r, _ = env
    r.init("v1")
    fake.calls.clear()
    steps = r.plan("to", "v2")
    assert not [n for n, _ in fake.calls if n in MUTATING]
    assert any("create index products_v2" in s for s in steps)
    assert any("atomically swap" in s for s in steps)


def test_template_used_for_creation_is_the_repo_template(env: Any) -> None:
    fake, _, r, _ = env
    r.init("v1")
    assert fake.templates["products_v1"] == load_template("v1")


def test_golden_queries_prefers_eval_golden_set(tmp_path: Path) -> None:
    assert golden_queries(tmp_path) == (list(BUILTIN_GOLDEN_QUERIES), "built-in")
    golden = tmp_path / "golden"
    golden.mkdir()
    for slice_name, n in (("en", 60), ("ar", 60)):
        lines = [{"q": f"{slice_name} query {i}", "slice": slice_name} for i in range(n)]
        (golden / f"{slice_name}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    qs, src = golden_queries(tmp_path, limit=10)
    assert "ar.jsonl" in src
    assert "en.jsonl" in src
    assert len(qs) == 10
    assert {q.split()[0] for q in qs} == {"ar", "en"}, "every slice is sampled"


def test_smoke_query_adds_arabizi_rendering() -> None:
    from souq_scripts.reindex import smoke_query_text

    assert smoke_query_text("Headphones") == "headphones"
    assert "شنط" in smoke_query_text("shanta hamra")


def test_embedding_cache_reuses_vectors(tmp_path: Path) -> None:
    from souq_scripts.reindex import CachedEmbedder

    calls: list[int] = []

    class Counting(FakeEmbedder):
        def encode_documents(self, texts: list[str]) -> Any:
            calls.append(len(texts))
            return super().encode_documents(texts)

    path = tmp_path / "emb.sqlite"
    first = CachedEmbedder(Counting(), "fake", path).encode_documents(["red shoe", "blue bag"])
    again = CachedEmbedder(Counting(), "fake", path)
    second = again.encode_documents(["blue bag", "red shoe", "green hat"])
    assert calls == [2, 1], "only the unseen text is embedded on the second run"
    assert (second[0] == first[1]).all()
    assert (second[1] == first[0]).all()
    assert (again.hits, again.misses) == (2, 1)
    other = CachedEmbedder(Counting(), "bge-m3", path)
    other.encode_documents(["red shoe"])
    assert calls[-1] == 1, "vectors are never shared across embedders"


def test_golden_file_pins_queries(tmp_path: Path) -> None:
    from souq_scripts.reindex import golden_queries_from

    txt = tmp_path / "q.txt"
    txt.write_text("# smoke queries\nwidget\n\nwidget\nسماعات\n", encoding="utf-8")
    assert golden_queries_from(txt) == ["widget", "سماعات"]
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n")
    with pytest.raises(ReindexError):
        golden_queries_from(empty)
