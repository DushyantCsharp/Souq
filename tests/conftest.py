"""Shared pytest wiring for cross-service suites under ``tests/`` (integration, chaos).

* LLM calls replay recorded fixtures (``SOUQ_LLM_MODE=replay``, CLAUDE.md rule 7) unless ``--run-live``.
* ``live`` tests are skipped unless ``--run-live`` is passed (and need ANTHROPIC_API_KEY).
* Everything under ``tests/integration`` is auto-marked ``integration`` so the default unit run skips it.
* Session-scoped containers (Postgres 16, Redpanda, OpenSearch 2.19, Redis Stack) are started lazily, once,
  while holding the repo's heavy-docker lock for the whole session.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from souq_testing.paths import CONFIG_DIR, LLM_FIXTURES_DIR  # noqa: E402

# --------------------------------------------------------------------------- options / env


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("souq")
    # ValueError: another conftest in the same session already registered the option.
    with contextlib.suppress(ValueError):
        group.addoption(
            "--run-live",
            action="store_true",
            default=False,
            help="run tests marked `live` (real LLM/API calls; needs ANTHROPIC_API_KEY)",
        )


def _run_live(config: pytest.Config) -> bool:
    return bool(config.getoption("--run-live", default=False))


def pytest_configure(config: pytest.Config) -> None:
    for line in (
        "integration: needs containers (testcontainers or the compose stack)",
        "live: calls a real external LLM/API; never in default CI",
        "chaos: fault-injection tests (kills/pauses containers)",
        "slow: > 10 s",
    ):
        config.addinivalue_line("markers", line)
    # Set before any service module reads souq_common.settings (lru-cached), and inherited by subprocesses.
    if not _run_live(config):
        os.environ["SOUQ_LLM_MODE"] = "replay"
        # No key in replay mode: a code path that tries to go live fails loudly instead of spending money.
        os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.setdefault("SOUQ_EMBEDDER", "fake")
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")
    from souq_testing.budgets import budget_scale, config_dir_for_tests

    if budget_scale() == 1:
        os.environ.setdefault("SOUQ_CONFIG_DIR", str(CONFIG_DIR))
    else:
        # A scaled copy of config/ for an overloaded dev VM (see souq_testing.budgets).
        os.environ["SOUQ_CONFIG_DIR"] = str(config_dir_for_tests())


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    integration_dir = Path(__file__).resolve().parent / "integration"
    skip_live = pytest.mark.skip(reason="live test: pass --run-live (and set ANTHROPIC_API_KEY)")
    for item in items:
        if integration_dir in item.path.resolve().parents:
            item.add_marker(pytest.mark.integration)
        if "live" in item.keywords and not _run_live(config):
            item.add_marker(skip_live)


# --------------------------------------------------------------------------- LLM mode fixtures


def _clear_settings_cache() -> None:
    try:
        from souq_common.settings import get_settings
    except ImportError:
        return
    get_settings.cache_clear()


@pytest.fixture
def llm_replay(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Force replay mode for one test (even under --run-live); yields the fixture directory."""
    monkeypatch.setenv("SOUQ_LLM_MODE", "replay")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _clear_settings_cache()
    yield LLM_FIXTURES_DIR
    _clear_settings_cache()


@pytest.fixture
def llm_live(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """For `live` tests: switch to live mode; skips without --run-live or without an API key."""
    if not _run_live(request.config):
        pytest.skip("live LLM test: pass --run-live")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("live LLM test: ANTHROPIC_API_KEY is not set")
    monkeypatch.setenv("SOUQ_LLM_MODE", "live")
    _clear_settings_cache()
    yield
    _clear_settings_cache()


# --------------------------------------------------------------------------- containers / services


def external_services() -> bool:
    """CI runs the suite against GitHub service containers instead of testcontainers."""
    return os.environ.get("SOUQ_TEST_USE_EXTERNAL_SERVICES", "").lower() in {"1", "true", "yes"}


def _external(var: str) -> str:
    value = os.environ.get(var)
    if not value:
        pytest.skip(f"SOUQ_TEST_USE_EXTERNAL_SERVICES=1 but {var} is not set")
    return value


@pytest.fixture(scope="session")
def external_mode() -> bool:
    """True when services are shared, externally managed instances: no container control (pause/kill)."""
    return external_services()


@pytest.fixture(scope="session")
def heavy_docker_lock() -> Iterator[None]:
    if external_services():
        yield
        return
    from souq_testing.locks import HeavyDockerLock

    with HeavyDockerLock():
        yield


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def docker_ready(heavy_docker_lock: None) -> None:
    if not external_services() and not _docker_available():
        pytest.skip("Docker is not reachable; integration containers cannot start")


@pytest.fixture(scope="session")
def pg_container(docker_ready: None) -> Iterator[str]:
    """Postgres 16 URL (bare testcontainer, or the external instance)."""
    if external_services():
        yield _external("DATABASE_URL")
        return
    from souq_testing.containers import start_postgres

    started = start_postgres()
    try:
        yield started.url
    finally:
        started.stop()


@pytest.fixture(scope="session")
def database_url(pg_container: str) -> str:
    """Postgres with the release migrations applied via packages/db's node-pg-migrate runner."""
    from souq_testing.db import migrate_up, schema_present

    if not (external_services() and schema_present(pg_container)):
        migrate_up(pg_container)
    return pg_container


@pytest.fixture(scope="session")
def kafka_bootstrap(docker_ready: None) -> Iterator[str]:
    """Redpanda with the contract topics (catalog.changes, retry.30s, retry.5m, dlq)."""
    if external_services():
        from souq_testing.streams import ensure_topics

        bootstrap = _external("KAFKA_BOOTSTRAP")
        ensure_topics(bootstrap)
        yield bootstrap
        return
    from souq_testing.containers import start_redpanda

    started = start_redpanda()
    try:
        yield started.url
    finally:
        started.stop()


@pytest.fixture(scope="session")
def opensearch_url(docker_ready: None) -> Iterator[str]:
    if external_services():
        yield _external("OPENSEARCH_URL")
        return
    from souq_testing.containers import start_opensearch

    started = start_opensearch()
    try:
        yield started.url
    finally:
        started.stop()


@pytest.fixture(scope="session")
def redis_url(docker_ready: None) -> Iterator[str]:
    if external_services():
        yield _external("REDIS_URL")
        return
    from souq_testing.containers import start_redis

    started = start_redis()
    try:
        yield started.url
    finally:
        started.stop()


@pytest.fixture(scope="session")
def service_env(
    database_url: str, kafka_bootstrap: str, opensearch_url: str, redis_url: str
) -> dict[str, str]:
    """Environment for in-process/subprocess services pointed at the session's data services."""
    return {
        "DATABASE_URL": database_url,
        "KAFKA_BOOTSTRAP": kafka_bootstrap,
        "OPENSEARCH_URL": opensearch_url,
        "REDIS_URL": redis_url,
        "SOUQ_EMBEDDER": "fake",
        "SOUQ_LLM_MODE": os.environ.get("SOUQ_LLM_MODE", "replay"),
        "SOUQ_CONFIG_DIR": os.environ.get("SOUQ_CONFIG_DIR", str(CONFIG_DIR)),
        "OTEL_SDK_DISABLED": "true",
        "PYTHONUNBUFFERED": "1",
    }
