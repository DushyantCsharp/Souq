"""The repo-wide heavy-docker lock (``.locks/heavy-docker``) as a context manager.

The Docker VM is shared with other stacks and cannot host two OpenSearch testcontainers at once, so every
suite that starts containers takes this lock for its whole run. ``mkdir`` is the primitive because the shell
scripts use the same one (``until mkdir .locks/heavy-docker; do sleep 15; done``).
"""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
import sys
import time
from pathlib import Path
from types import FrameType, TracebackType

from souq_testing.paths import REPO_ROOT

LOCK_DIR = REPO_ROOT / ".locks" / "heavy-docker"


class HeavyDockerLock:
    def __init__(self, path: Path = LOCK_DIR, poll_s: float = 15.0, timeout_s: float = 3600.0) -> None:
        self.path = path
        self.poll_s = poll_s
        self.timeout_s = timeout_s
        self.held = False
        self._prev_sigterm: object = None

    @staticmethod
    def disabled() -> bool:
        # CI runners have the machine to themselves; the lock only matters on the shared dev VM.
        return os.environ.get("SOUQ_HEAVY_DOCKER_LOCK", "1").lower() in {"0", "false", "no", "off"}

    def acquire(self) -> None:
        if self.disabled() or self.held:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        announced = False
        while True:
            try:
                self.path.mkdir()
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"waited {self.timeout_s:.0f}s for {self.path}; another heavy Docker job holds it "
                        "(remove the directory only if you are sure nothing is running)"
                    ) from None
                if not announced:
                    print(f"[souq-testing] waiting for {self.path} ...", file=sys.stderr, flush=True)
                    announced = True
                time.sleep(self.poll_s)
        self.held = True
        # A lock leaked by an interrupted run blocks every other agent, so release on SIGTERM and at exit too.
        atexit.register(self.release)
        if hasattr(signal, "SIGTERM"):
            self._prev_sigterm = signal.signal(signal.SIGTERM, self._on_sigterm)

    def _on_sigterm(self, signum: int, frame: FrameType | None) -> None:
        self.release()
        prev = self._prev_sigterm
        if callable(prev):
            prev(signum, frame)
        else:
            raise SystemExit(128 + signum)

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        with contextlib.suppress(FileNotFoundError):
            self.path.rmdir()

    def __enter__(self) -> HeavyDockerLock:
        self.acquire()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()
