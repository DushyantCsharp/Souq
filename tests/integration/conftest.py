"""The running pipeline for integration tests: Postgres → outbox relay → Redpanda → indexer → OpenSearch,
plus the search service, all started once per session (see tests/conftest.py for the data services).
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import uuid
from collections.abc import Iterator

import pytest
from souq_testing.services import (
    SearchServer,
    Stack,
    log_dir,
    run_reindex,
    start_indexer,
    start_relay,
)
from souq_testing.streams import os_alias_indices

REQUIRED_MODULES = {
    "souq_outbox_relay.main": "outbox relay",
    "souq_indexer.main": "indexer",
    "souq_search.app": "search service",
    "souq_scripts.reindex": "scripts/reindex.py",
    "souq_feeds.cli": "feeds importer",
}


def _missing_components() -> list[str]:
    missing = []
    for module, what in REQUIRED_MODULES.items():
        try:
            found = importlib.util.find_spec(module) is not None
        except ModuleNotFoundError:
            found = False
        if not found:
            missing.append(f"{what} ({module})")
    return missing


@pytest.fixture(scope="session")
def stack(
    service_env: dict[str, str], database_url: str, kafka_bootstrap: str, opensearch_url: str
) -> Iterator[Stack]:
    missing = _missing_components()
    if missing:
        pytest.fail(f"pipeline components not importable: {', '.join(missing)}")

    logs = log_dir()
    if not os_alias_indices(opensearch_url, "products_read"):
        init = run_reindex(["--init", "v1", "--alias-settle-s", "0"], service_env)
        assert init.returncode == 0, f"reindex --init v1 failed:\n{init.stdout}\n{init.stderr}"

    with contextlib.ExitStack() as cleanup:
        # Registered as each one starts, so a later startup failure still stops the earlier processes.
        relay = start_relay(service_env, logs)
        cleanup.callback(relay.stop)
        # A fresh group per session: with auto.offset.reset=earliest it replays anything already on the
        # topic, which external versioning turns into no-ops.
        indexer = start_indexer(service_env, logs, group_id=f"souq-it-{uuid.uuid4().hex[:8]}")
        cleanup.callback(indexer.stop)
        search = SearchServer(service_env).start()
        cleanup.callback(search.stop)
        st = Stack(
            env=service_env,
            database_url=database_url,
            kafka_bootstrap=kafka_bootstrap,
            opensearch_url=opensearch_url,
            search=search,
            relay=relay,
            indexer=indexer,
            logs=logs,
        )
        cleanup.callback(st.http.close)
        yield st


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        st = item.funcargs.get("stack") if hasattr(item, "funcargs") else None
        if isinstance(st, Stack):
            try:
                report.sections.append(("souq pipeline", st.diagnostics()))
            except Exception as exc:  # diagnostics must never mask the real failure
                report.sections.append(("souq pipeline", f"<diagnostics failed: {exc!r}>"))


def unique_token(request: pytest.FixtureRequest) -> str:
    """A letters-only token unique to the test (digits would trip Arabizi detection in the parser)."""
    digest = hashlib.sha256(f"{request.node.nodeid}{uuid.uuid4()}".encode()).digest()
    consonants = "bcdfghjklmnpqrstvwxz"
    return "".join(consonants[b % len(consonants)] for b in digest[:10])


@pytest.fixture
def token(request: pytest.FixtureRequest) -> str:
    return unique_token(request)


@pytest.fixture
def sku_prefix(token: str) -> str:
    return "IT" + token[:6].upper()
