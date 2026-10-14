"""Make this directory's helper modules importable (pytest runs with --import-mode=importlib)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Unit tests never export telemetry and always use the deterministic embedder.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("SOUQ_EMBEDDER", "fake")
