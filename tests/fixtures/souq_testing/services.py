"""Run the pipeline's processes against the test containers.

* outbox relay and indexer run as **subprocesses** of the real console entry points, so they read their
  config exactly as in production (env + budgets.yaml) and can be killed/restarted by chaos tests;
* the search service runs **in-process** under uvicorn in a thread, so tests can reach into
  ``app.state.components`` (e.g. swap the reranker) while still talking HTTP to it;
* ``scripts/reindex.py`` runs as a subprocess, like an operator would run it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from souq_testing.containers import free_port, http_ok, wait_until
from souq_testing.paths import REINDEX_SCRIPT, REPO_ROOT
from souq_testing.streams import os_alias_indices


@dataclass
class ManagedProcess:
    name: str
    argv: list[str]
    env: dict[str, str]
    log_path: Path
    proc: subprocess.Popen[bytes] | None = None
    health_url: str | None = None
    _log_fh: Any = field(default=None, repr=False)

    def start(self, ready_timeout_s: float = 60) -> ManagedProcess:
        self._log_fh = self.log_path.open("ab")
        self.proc = subprocess.Popen(
            self.argv, env=self.env, cwd=REPO_ROOT, stdout=self._log_fh, stderr=subprocess.STDOUT
        )
        if self.health_url:
            url = self.health_url

            def up() -> bool:
                if self.proc is not None and self.proc.poll() is not None:
                    raise RuntimeError(f"{self.name} exited with {self.proc.returncode}:\n{self.tail_log()}")
                return http_ok(url)

            try:
                wait_until(up, ready_timeout_s, f"{self.name} health ({url})")
            except Exception:
                self.stop()
                raise
        return self

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout_s: float = 20) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout_s)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None

    def kill(self) -> None:
        """SIGKILL, for crash tests."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)

    def tail_log(self, lines: int = 60) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "<no log>"


def _python_entry(module: str, func: str = "main") -> list[str]:
    # Same code path as the console script, without depending on the venv's bin/ layout.
    return [sys.executable, "-c", f"import sys; from {module} import {func}; sys.exit({func}())"]


def _health_env(prefix: str, log_dir: Path) -> tuple[dict[str, str], str]:
    port = free_port()
    env = {
        f"{prefix}HEALTH_PORT": str(port),
        "SOUQ_HEALTH_FILE": str(log_dir / f"{prefix.lower().rstrip('_')}.alive"),
    }
    return env, f"http://127.0.0.1:{port}/healthz"


def start_relay(base_env: Mapping[str, str], log_dir: Path) -> ManagedProcess:
    extra, health = _health_env("RELAY_", log_dir)
    env = {**os.environ, **base_env, **extra}
    return ManagedProcess(
        "outbox-relay",
        _python_entry("souq_outbox_relay.main"),
        env,
        log_dir / "outbox-relay.log",
        health_url=health,
    ).start()


def start_indexer(
    base_env: Mapping[str, str], log_dir: Path, group_id: str, alias_refresh_s: float = 2.0
) -> ManagedProcess:
    extra, health = _health_env("INDEXER_", log_dir)
    env = {
        **os.environ,
        **base_env,
        **extra,
        "INDEXER_GROUP_ID": group_id,
        # The production 10 s refresh would make the alias-swap test wait 12 s+ per run; the reindex
        # settle time below is scaled with it so "settle > refresh" still holds.
        "INDEXER_ALIAS_REFRESH_S": str(alias_refresh_s),
    }
    return ManagedProcess(
        "indexer", _python_entry("souq_indexer.main"), env, log_dir / "indexer.log", health_url=health
    ).start()


# --------------------------------------------------------------------------- reindex


# Golden queries the smoke step must find in the test catalog (souq_testing.factories templates).
TEST_GOLDEN_QUERIES: tuple[str, ...] = ("headphones", "running shoes", "backpack", "سماعات", "حذاء")

# The real CLI entry point, with the smoke-test query set pinned to the test catalog: the eval golden set
# targets the seeded ESCI catalog, which the integration suite does not load.
_REINDEX_WRAPPER = (
    "import json, sys\n"
    "import souq_scripts.reindex as r\n"
    "args, golden = json.loads(sys.argv[1]), json.loads(sys.argv[2])\n"
    "r.golden_queries = lambda *a, **k: (golden, 'tests')\n"
    "r.main(args)\n"
)


def run_reindex(
    args: Sequence[str],
    base_env: Mapping[str, str],
    *,
    golden: Sequence[str] = TEST_GOLDEN_QUERIES,
    timeout_s: float = 300,
) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-c", _REINDEX_WRAPPER, json.dumps(list(args)), json.dumps(list(golden))]
    return subprocess.run(
        argv,
        env={**os.environ, **base_env},
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def reindex_script_exists() -> bool:
    return REINDEX_SCRIPT.exists()


# --------------------------------------------------------------------------- search (in-process)


class SearchServer:
    """``souq_search.app:create_app()`` under uvicorn in a background thread."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = dict(env)
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self.app: Any = None
        self._saved_env: dict[str, str | None] = {}

    def start(self, timeout_s: float = 90) -> SearchServer:
        import uvicorn

        # Settings and budgets are lru-cached per process; point them at the containers first.
        for key, value in self.env.items():
            self._saved_env[key] = os.environ.get(key)
            os.environ[key] = value
        from souq_common.budgets import load_budgets
        from souq_common.settings import get_settings

        get_settings.cache_clear()
        load_budgets.cache_clear()
        from souq_search.app import create_app

        self.app = create_app()
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="search-uvicorn", daemon=True)
        self._thread.start()
        wait_until(lambda: bool(self._server.started), timeout_s, "search service startup")
        wait_until(lambda: http_ok(self.base_url + "/healthz"), timeout_s, "search /healthz")
        return self

    @property
    def components(self) -> Any:
        return self.app.state.components

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(30)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def log_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="souq-it-"))


def wait_seconds(seconds: float) -> None:
    time.sleep(seconds)


@dataclass
class Stack:
    env: dict[str, str]
    database_url: str
    kafka_bootstrap: str
    opensearch_url: str
    search: SearchServer
    relay: ManagedProcess
    indexer: ManagedProcess
    logs: Path
    http: Any = field(default_factory=lambda: __import__("httpx").Client(timeout=10))

    def search_request(self, q: str, **body: Any) -> Any:
        payload: dict[str, Any] = {"q": q, **body}
        return self.http.post(self.search.base_url + "/search", json=payload)

    def search_ok(self, q: str, **body: Any) -> dict[str, Any]:
        resp = self.search_request(q, **body)
        assert resp.status_code == 200, f"search {q!r} -> {resp.status_code}: {resp.text[:500]}"
        return resp.json()  # type: ignore[no-any-return]

    def diagnostics(self) -> str:
        return (
            f"--- outbox-relay log ---\n{self.relay.tail_log()}\n"
            f"--- indexer log ---\n{self.indexer.tail_log()}\n"
            f"--- aliases --- read={os_alias_indices(self.opensearch_url, 'products_read')} "
            f"write={os_alias_indices(self.opensearch_url, 'products_write')}"
        )
