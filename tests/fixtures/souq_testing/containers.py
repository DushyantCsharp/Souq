"""Testcontainers for the cross-service suites: Postgres 16, Redpanda, OpenSearch 2.19, Redis Stack.

Images match infra/compose.yaml so nothing extra is pulled on the dev machine. Heaps are deliberately small:
the Docker VM is ~8 GB and shared, and CLAUDE.md forbids more than one OpenSearch testcontainer at a time
(the session-scoped fixtures plus the heavy-docker lock enforce that).
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

POSTGRES_IMAGE = "postgres:16-alpine"
REDPANDA_IMAGE = "redpandadata/redpanda:v24.3.7"
OPENSEARCH_IMAGE = "opensearchproject/opensearch:2.19.1"
REDIS_IMAGE = "redis/redis-stack-server:7.4.0-v3"

# Contract topics (CLAUDE.md "Kafka"). Mirrors infra/redpanda/topics.sh.
TOPICS: dict[str, int] = {
    "catalog.changes": 12,
    "catalog.retry.30s": 12,
    "catalog.retry.5m": 12,
    "catalog.dlq": 3,
}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_until(check: Callable[[], bool], timeout_s: float, what: str, interval_s: float = 0.5) -> None:
    deadline = time.monotonic() + timeout_s
    last_exc: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception as exc:  # startup probes fail in many transient ways; keep the last for the report
            last_exc = exc
        time.sleep(interval_s)
    raise TimeoutError(f"{what} not ready after {timeout_s:.0f}s (last error: {last_exc!r})")


def _container(image: str, name_hint: str, mem_limit: str) -> Any:
    from testcontainers.core.container import DockerContainer

    c = DockerContainer(image)
    # Labels make leftovers attributable; names stay unique so parallel CI jobs on one host do not clash.
    c.with_kwargs(mem_limit=mem_limit, labels={"souq.testkit": name_hint})
    return c


@contextlib.contextmanager
def _cleanup_on_failure(container: Any) -> Iterator[None]:
    """A container whose startup fails is never handed to a fixture finaliser, so stop it here."""
    try:
        yield
    except BaseException:
        with contextlib.suppress(Exception):
            container.stop()
        raise


@dataclass
class Started:
    container: Any
    url: str

    def stop(self) -> None:
        # Best-effort: Ryuk reaps anything left behind.
        with contextlib.suppress(Exception):
            self.container.stop()


# --------------------------------------------------------------------------- postgres


def start_postgres() -> Started:
    import psycopg

    c = _container(POSTGRES_IMAGE, "postgres", "384m")
    c.with_env("POSTGRES_USER", "souq").with_env("POSTGRES_PASSWORD", "souq").with_env("POSTGRES_DB", "souq")
    c.with_command(
        "postgres -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=200"
    )
    c.with_exposed_ports(5432)
    c.start()
    with _cleanup_on_failure(c):
        host, port = c.get_container_host_ip(), c.get_exposed_port(5432)
        url = f"postgresql://souq:souq@{host}:{port}/souq"

        def ready() -> bool:
            with psycopg.connect(url, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            return True

        wait_until(ready, 60, "postgres")
    return Started(c, url)


# --------------------------------------------------------------------------- redpanda


def start_redpanda() -> Started:
    # Kafka clients reconnect to the *advertised* address, which must be host:mapped-port. Binding a free host
    # port up front lets the broker advertise it from the first boot, no restart dance needed.
    port = free_port()
    c = _container(REDPANDA_IMAGE, "redpanda", "1g")
    c.with_bind_ports(9092, port)
    c.with_command(
        "redpanda start --mode=dev-container --smp=1 --memory=512M --reserve-memory=0M --overprovisioned "
        "--kafka-addr=PLAINTEXT://0.0.0.0:9092 "
        f"--advertise-kafka-addr=PLAINTEXT://127.0.0.1:{port}"
    )
    c.start()
    with _cleanup_on_failure(c):

        def ready() -> bool:
            code, out = c.exec(["rpk", "cluster", "health"])
            return code == 0 and re.search(rb"Healthy:\s+true", out) is not None

        wait_until(ready, 90, "redpanda")
        bootstrap = f"127.0.0.1:{port}"
        # From the host, not via `rpk` in the container: the broker advertises the host-mapped port, which is
        # unreachable from inside the container.
        from souq_testing.streams import ensure_topics

        def topics_created() -> bool:
            ensure_topics(bootstrap)
            return True

        wait_until(topics_created, 60, "redpanda topics")
    return Started(c, bootstrap)


# --------------------------------------------------------------------------- opensearch


def opensearch_request(base: str, method: str, path: str, body: Any = None, timeout: float = 10) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def start_opensearch() -> Started:
    import docker.types

    c = _container(OPENSEARCH_IMAGE, "opensearch", "1600m")
    for key, value in {
        "discovery.type": "single-node",
        "DISABLE_SECURITY_PLUGIN": "true",
        "DISABLE_INSTALL_DEMO_CONFIG": "true",
        # 512m spent >10 s in young GC during a backfill on the shared VM; 768m keeps search latency sane.
        "OPENSEARCH_JAVA_OPTS": "-Xms768m -Xmx768m",
        "bootstrap.memory_lock": "false",
        "cluster.routing.allocation.disk.threshold_enabled": "false",
        # Keep the k-NN native memory small; the suite indexes a few hundred 1024-d vectors at most.
        "knn.memory.circuit_breaker.limit": "25%",
    }.items():
        c.with_env(key, value)
    c.with_kwargs(
        mem_limit="1600m",
        labels={"souq.testkit": "opensearch"},
        ulimits=[docker.types.Ulimit(name="nofile", soft=65536, hard=65536)],
    )
    c.with_exposed_ports(9200)
    c.start()
    with _cleanup_on_failure(c):
        url = f"http://{c.get_container_host_ip()}:{c.get_exposed_port(9200)}"

        def ready() -> bool:
            health = opensearch_request(url, "GET", "/_cluster/health?wait_for_status=yellow&timeout=2s")
            return health is not None and health.get("status") in {"yellow", "green"}

        # Generous: on the shared dev VM a cold JVM start competes with other stacks for CPU.
        wait_until(ready, 360, "opensearch", interval_s=2)
    return Started(c, url)


# --------------------------------------------------------------------------- redis


def start_redis() -> Started:
    c = _container(REDIS_IMAGE, "redis", "256m")
    c.with_env("REDIS_ARGS", "--maxmemory 128mb --maxmemory-policy allkeys-lru")
    c.with_exposed_ports(6379)
    c.start()
    with _cleanup_on_failure(c):
        host, port = c.get_container_host_ip(), int(c.get_exposed_port(6379))

        def ready() -> bool:
            with socket.create_connection((host, port), timeout=2) as sock:
                sock.sendall(b"PING\r\n")
                return sock.recv(16).startswith(b"+PONG")

        wait_until(ready, 60, "redis")
    return Started(c, f"redis://{host}:{port}/0")


def http_ok(url: str, timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        return False
