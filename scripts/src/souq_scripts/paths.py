"""Repository-relative locations shared by the scripts."""

from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pnpm-workspace.yaml").is_file() and (parent / "infra").is_dir():
            return parent
    return Path.cwd()


ROOT = repo_root()
TEMPLATES_DIR = ROOT / "infra" / "opensearch" / "templates"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CACHE_DIR = DATA_DIR / "cache"
ESCI_DIR = DATA_DIR / "esci"
FEEDS_DIR = DATA_DIR / "feeds"
SEED_DIR = ROOT / "scripts" / "seed"
SAMPLES_DIR = SEED_DIR / "samples"
EVALS_DIR = ROOT / "evals"
