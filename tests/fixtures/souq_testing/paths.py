"""Repository paths used by the test kit."""

from __future__ import annotations

from pathlib import Path


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "CLAUDE.md").exists() and (parent / "pnpm-workspace.yaml").exists():
            return parent
    raise RuntimeError("could not locate the Souq repo root from tests/fixtures")


REPO_ROOT = _find_repo_root()
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
FEEDS_FIXTURES_DIR = FIXTURES_DIR / "feeds"
LLM_FIXTURES_DIR = FIXTURES_DIR / "llm"
CONFIG_DIR = REPO_ROOT / "config"
DB_PACKAGE_DIR = REPO_ROOT / "packages" / "db"
REINDEX_SCRIPT = REPO_ROOT / "scripts" / "reindex.py"
