"""Chaos: kill OpenSearch for 60 s in the middle of a feed import.

Runs the real pipeline — feeds importer -> outbox -> outbox-relay -> Redpanda -> indexer -> OpenSearch —
against an isolated compose project (``souq-chaos``: Postgres :55433, Redpanda :29092, OpenSearch :9201),
never the shared ``souq`` stack, and only while holding ``.locks/heavy-docker``.

Asserts after recovery:
  * zero lost SKUs and zero duplicates: OpenSearch doc count == Postgres product count, and every
    document's external ``_version`` equals ``products.version`` (so the newest state won everywhere);
  * nothing was routed to retry topics or the DLQ (an outage is backpressure, not a message failure);
  * consumer lag on ``catalog.changes`` drains to 0 within a bound after OpenSearch is healthy again.

    uv run pytest tests/chaos -m chaos -s        # ~3-4 min; SOUQ_CHAOS_SKUS (default 20000) sizes it
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.chaos, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[2]
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "souq-chaos",
    "-f",
    str(REPO / "tests" / "chaos" / "compose.chaos.yaml"),
]
PG_DSN = "postgresql://souq:souq@localhost:55433/souq"
KAFKA = "localhost:29092"
OS_URL = "http://localhost:9201"
TOPICS = {"catalog.changes": 12, "catalog.retry.30s": 12, "catalog.retry.5m": 12, "catalog.dlq": 3}
N_SKUS = int(os.environ.get("SOUQ_CHAOS_SKUS", "20000"))
N_UPDATES = N_SKUS // 5
OUTAGE_S = float(os.environ.get("SOUQ_CHAOS_OUTAGE_S", "60"))
DRAIN_BOUND_S = float(os.environ.get("SOUQ_CHAOS_DRAIN_BOUND_S", "180"))
HEADER = [
    "sku",
    "title_en",
    "title_ar",
    "brand",
    "category_path",
    "price",
    "currency",
    "colour",
    "attributes",
]


# ----------------------------------------------------------------------------------------- helpers
def _run(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)


def _http(method: str, path: str, body: Any = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        OS_URL + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"{}")


def _os_healthy() -> bool:
    try:
        return _http("GET", "/_cluster/health?wait_for_status=yellow&timeout=1s").get("status") in (
            "yellow",
            "green",
        )
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return False


def _wait(predicate: Any, timeout: float, interval: float = 0.5, what: str = "condition") -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return time.monotonic() - start
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


def _migrate() -> None:
    import psycopg

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        for path in sorted((REPO / "packages" / "db" / "migrations").glob("*.sql")):
            up = re.split(r"^-- Down Migration", path.read_text(encoding="utf-8"), flags=re.M)[0]
            conn.execute(up.replace("-- Up Migration", ""))


def _create_topics() -> None:
    from confluent_kafka.admin import AdminClient, NewTopic  # type: ignore[attr-defined]

    admin = AdminClient({"bootstrap.servers": KAFKA})
    futures = admin.create_topics(
        [NewTopic(t, num_partitions=n, replication_factor=1) for t, n in TOPICS.items()]
    )
    for fut in futures.values():
        with contextlib.suppress(Exception):  # TOPIC_ALREADY_EXISTS on a re-run
            fut.result(30)


def _create_index() -> None:
    template = json.loads((REPO / "infra" / "opensearch" / "templates" / "products_v1.json").read_text())
    body = {
        "settings": template["template"]["settings"],
        "mappings": template["template"]["mappings"],
        "aliases": {"products_read": {}, "products_write": {}},
    }
    with contextlib.suppress(urllib.error.HTTPError):  # already exists on a re-run
        _http("PUT", "/products_v1", body)


def _write_feed(path: Path, skus: range, title: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for i in skus:
            w.writerow(
                [
                    f"CH{i:07d}",
                    f"{title} speaker {i}",
                    f"سماعة {i}",
                    "Acme",
                    "Electronics > Audio > Speakers",
                    f"{100 + i % 900}.00",
                    "AED",
                    "black",
                    json.dumps({"model": f"M{i}"}),
                ]
            )


def _import(path: Path) -> dict[str, Any]:
    import psycopg

    from souq_feeds.importer import run_import

    feed_id = uuid.uuid4()
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO feed_imports (id, filename, format, file_path) VALUES (%s, %s, 'csv', %s)",
            (feed_id, path.name, str(path)),
        )
        outcome = run_import(conn, feed_id, 1000)
    assert outcome is not None
    return {"status": outcome.status, "ok": outcome.counters.ok, "rejected": outcome.counters.rejected}


def _rows_imported() -> int:
    import psycopg

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        row = conn.execute("SELECT coalesce(sum(rows_total), 0) FROM feed_imports").fetchone()
    return int(row[0]) if row else 0


def _pg_versions() -> dict[str, int]:
    import psycopg

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        return {sku: int(v) for sku, v in conn.execute("SELECT sku, version FROM products").fetchall()}


def _os_versions() -> dict[str, int]:
    _http("POST", "/products_read/_refresh")
    out: dict[str, int] = {}
    page = _http(
        "POST", "/products_read/_search?scroll=1m", {"size": 5000, "version": True, "_source": False}
    )
    while page["hits"]["hits"]:
        for hit in page["hits"]["hits"]:
            assert hit["_id"] not in out, f"duplicate document {hit['_id']}"
            out[hit["_id"]] = int(hit["_version"])
        page = _http("POST", "/_search/scroll", {"scroll": "1m", "scroll_id": page["_scroll_id"]})
    return out


def _lag() -> dict[str, int]:
    """Committed-offset lag of group souq-indexer per topic (summed over partitions)."""
    from confluent_kafka import Consumer, TopicPartition

    c = Consumer({"bootstrap.servers": KAFKA, "group.id": "souq-indexer", "enable.auto.commit": False})
    try:
        out: dict[str, int] = {}
        for topic, n in TOPICS.items():
            parts = [TopicPartition(topic, p) for p in range(n)]
            committed = {p.partition: p.offset for p in c.committed(parts, timeout=10)}
            total = 0
            for p in parts:
                low, high = c.get_watermark_offsets(p, timeout=10)
                done = committed.get(p.partition, -1)
                total += high - (done if done >= 0 else low)
            out[topic] = total
        return out
    finally:
        c.close()


def _topic_depth(topic: str) -> int:
    from confluent_kafka import Consumer, TopicPartition

    c = Consumer({"bootstrap.servers": KAFKA, "group.id": f"chaos-probe-{uuid.uuid4().hex[:6]}"})
    try:
        return sum(
            high - low
            for low, high in (
                c.get_watermark_offsets(TopicPartition(topic, p), timeout=10) for p in range(TOPICS[topic])
            )
        )
    finally:
        c.close()


# ----------------------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def chaos_stack(heavy_docker_lock: None) -> Iterator[None]:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    try:
        _run([*COMPOSE, "up", "-d", "--wait"], timeout=900)
    except subprocess.CalledProcessError as exc:
        _run([*COMPOSE, "down", "-v", "--remove-orphans"], timeout=300)
        pytest.fail(f"could not start the souq-chaos stack: {exc.stderr[-2000:]}")
    try:
        _migrate()
        _create_topics()
        _create_index()
        yield
    finally:
        # Only ever the isolated souq-chaos project; frees its memory for the other agents/stacks.
        subprocess.run([*COMPOSE, "down", "-v", "--remove-orphans"], capture_output=True, timeout=300)


@pytest.fixture
def workers(chaos_stack: None, tmp_path: Path) -> Iterator[dict[str, subprocess.Popen[bytes]]]:
    env = {
        **os.environ,
        "DATABASE_URL": PG_DSN,
        "KAFKA_BOOTSTRAP": KAFKA,
        "OPENSEARCH_URL": OS_URL,
        "SOUQ_EMBEDDER": "fake",
        "OTEL_SDK_DISABLED": "true",
        "LOG_LEVEL": "WARNING",
        "INDEXER_HEALTH_PORT": "18103",
        "RELAY_HEALTH_PORT": "18104",
    }
    procs: dict[str, subprocess.Popen[bytes]] = {}
    logs = {}
    for name, module in (("relay", "souq_outbox_relay.main"), ("indexer", "souq_indexer.main")):
        logs[name] = (tmp_path / f"{name}.log").open("wb")
        procs[name] = subprocess.Popen(
            [sys.executable, "-m", module],
            env={**env, "SOUQ_HEALTH_FILE": str(tmp_path / f"{name}.alive")},
            stdout=logs[name],
            stderr=subprocess.STDOUT,
        )
    try:
        yield procs
    finally:
        for p in procs.values():
            p.send_signal(signal.SIGTERM)
        for p in procs.values():
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p.kill()
        for fh in logs.values():
            fh.close()
        for name in procs:
            tail = (tmp_path / f"{name}.log").read_text(errors="replace")[-3000:]
            print(f"\n--- {name} log tail ---\n{tail}")


# ----------------------------------------------------------------------------------------- the test
def test_opensearch_outage_mid_import_loses_and_duplicates_nothing(
    workers: dict[str, subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    feed1 = tmp_path / "feed1.csv"
    feed2 = tmp_path / "feed2.csv"
    _write_feed(feed1, range(N_SKUS), "Classic")
    _write_feed(feed2, range(0, N_SKUS, 5), "Updated")  # 20% of SKUs get a new title -> new version

    results: dict[str, dict[str, Any]] = {}

    def importer() -> None:
        results["feed1"] = _import(feed1)
        results["feed2"] = _import(feed2)

    t = threading.Thread(target=importer, name="chaos-import")
    t.start()

    # Kill OpenSearch once the import is well under way and documents are flowing.
    _wait(lambda: _rows_imported() >= N_SKUS // 4, timeout=120, interval=0.1, what="import progress")
    killed_at = time.monotonic()
    _run([*COMPOSE, "kill", "opensearch"], timeout=60)
    assert not _os_healthy()

    peak_lag = 0
    while time.monotonic() - killed_at < OUTAGE_S:
        for p in workers.values():
            assert p.poll() is None, "a worker died during the outage"
        with contextlib.suppress(Exception):
            peak_lag = max(peak_lag, _lag()["catalog.changes"])
        time.sleep(2)
    t.join(timeout=300)
    assert not t.is_alive(), "feed import did not finish"
    imported_during_outage = results

    _run([*COMPOSE, "start", "opensearch"], timeout=120)
    os_back_after = _wait(_os_healthy, timeout=300, interval=1, what="OpenSearch to come back")
    healthy_at = time.monotonic()

    expected = _pg_versions()

    def converged() -> bool:
        try:
            lag = _lag()
            return lag["catalog.changes"] == 0 and _os_versions() == expected
        except Exception:
            return False

    drain_s = _wait(converged, timeout=DRAIN_BOUND_S, interval=3, what="index to converge on Postgres")

    pg = _pg_versions()
    os_docs = _os_versions()
    missing = sorted(set(pg) - set(os_docs))
    extra = sorted(set(os_docs) - set(pg))
    stale = sorted(s for s in pg if s in os_docs and os_docs[s] != pg[s])
    lag = _lag()
    summary = {
        "skus": len(pg),
        "updated_skus": N_UPDATES,
        "peak_lag_during_outage": peak_lag,
        "opensearch_restart_s": round(os_back_after, 1),
        "drain_after_recovery_s": round(drain_s, 1),
        "final_lag": lag,
        "dlq_depth": _topic_depth("catalog.dlq"),
        "retry_depth": _topic_depth("catalog.retry.30s") + _topic_depth("catalog.retry.5m"),
        "imports": imported_during_outage,
    }
    print("\nCHAOS SUMMARY " + json.dumps(summary))

    assert results["feed1"] == {"status": "completed", "ok": N_SKUS, "rejected": 0}
    assert results["feed2"] == {"status": "completed", "ok": len(range(0, N_SKUS, 5)), "rejected": 0}
    assert len(pg) == N_SKUS
    assert missing == [], f"{len(missing)} SKUs lost, e.g. {missing[:5]}"
    assert extra == [], f"{len(extra)} unexpected documents, e.g. {extra[:5]}"
    assert stale == [], f"{len(stale)} documents at the wrong version, e.g. {stale[:5]}"
    assert all(v >= 3 for v in pg.values())  # insert + en + ar texts (packages/db version trigger)
    assert summary["dlq_depth"] == 0
    assert summary["retry_depth"] == 0
    assert peak_lag > 0, "the outage never held back indexing (kill came too late?)"
    assert all(v == 0 for v in lag.values()), lag
    assert time.monotonic() - healthy_at <= DRAIN_BOUND_S + 30
