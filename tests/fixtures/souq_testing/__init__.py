"""Shared test kit for cross-service tests (integration, chaos).

``tests/conftest.py`` puts ``tests/fixtures`` on ``sys.path`` so test modules can ``import souq_testing``
without the repo-wide ``tests`` package name colliding with each service's own ``tests`` directory.
"""

REPO_ROOT_MARKER = "CLAUDE.md"
