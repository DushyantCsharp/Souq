"""Repo-wide pytest hooks, loaded for every uv member (the root pyproject.toml is the pytest rootdir).

Kept deliberately tiny: per-suite fixtures belong in each package's tests/conftest.py.
"""

from __future__ import annotations

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # Exit code 5 means "no tests collected". The default `test` task deselects integration/live/chaos, so a
    # package whose tests are all integration tests (or a new package with no tests yet) would otherwise fail
    # `pnpm turbo test` for having nothing to run.
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
        session.exitstatus = pytest.ExitCode.OK
