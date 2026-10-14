"""In-process cross-encoder reranker (ONNX int8 via onnxruntime; model choice in models/README.md).

Inference is CPU-bound, so it runs on a dedicated thread pool. ``asyncio.wait_for`` cannot stop a running
thread, so a timed-out inference keeps its slot until it actually finishes; new requests that find every
slot busy degrade immediately (fused order + ``degraded: ["rerank"]``) instead of queueing behind it. That
keeps an overloaded reranker from dragging the whole service past its budget.

Busy slots alone are not enough under sustained overload: every slot stays occupied by inferences that finish
after their deadline, so the reranker burns most of the CPU (measured ~350% of 520% at 30 RPS) producing
scores nobody uses, and starves embedding and retrieval. A circuit breaker sheds reranking for a cool-down
once most recent attempts missed their deadline.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

import numpy as np

from souq_common.embeddings import ort_threads


class ScoresPairs(Protocol):
    def score(self, query: str, docs: list[str]) -> np.ndarray: ...


class OnnxCrossEncoder:
    def __init__(
        self,
        model_dir: Path,
        max_length: int | None = None,
        threads: int | None = None,
        chunk: int | None = None,
    ) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        meta_path = model_dir / "souq_model.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        self.max_length = int(max_length or meta.get("max_length", 128))
        self.name = str(meta.get("name", model_dir.name))
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=self.max_length, strategy="only_second")
        self._tokenizer.no_padding()
        self.pad_id = int(meta.get("pad_id", 1))
        self.chunk = int(chunk or meta.get("chunk", 8))
        self._lock = threading.Lock()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads or ort_threads()
        opts.inter_op_num_threads = 1
        # Spinning intra-op threads busy-wait between runs; with several sessions per process (embedder,
        # reranker, N uvicorn workers) they burn whole cores idle. SOUQ_ORT_SPIN=1 for single-caller
        # batch jobs.
        opts.add_session_config_entry("session.intra_op.allow_spinning", os.environ.get("SOUQ_ORT_SPIN", "0"))
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Variable input shapes (query length, rerank batch) make the CPU arena grow to the largest shape seen
        # and never shrink; under load that OOM-killed uvicorn workers. Allocate per run instead.
        opts.enable_cpu_mem_arena = False
        opts.enable_mem_pattern = False
        self._session = ort.InferenceSession(
            str(model_dir / "model.onnx"), opts, providers=["CPUExecutionProvider"]
        )
        self._inputs = {i.name for i in self._session.get_inputs()}

    def score(self, query: str, docs: list[str]) -> np.ndarray:
        if not docs:
            return np.zeros(0, dtype=np.float32)
        with self._lock:
            encs = self._tokenizer.encode_batch([(query, d) for d in docs])
        # Padding to the longest pair wastes most of the compute on short titles: run length-sorted
        # sub-batches, each padded only to its own longest pair.
        order = sorted(range(len(encs)), key=lambda i: len(encs[i].ids))
        scores = np.zeros(len(encs), dtype=np.float32)
        for start in range(0, len(order), self.chunk):
            idx = order[start : start + self.chunk]
            width = max(len(encs[i].ids) for i in idx)
            ids = np.full((len(idx), width), self.pad_id, dtype=np.int64)
            mask = np.zeros((len(idx), width), dtype=np.int64)
            for row, i in enumerate(idx):
                n = len(encs[i].ids)
                ids[row, :n] = encs[i].ids
                mask[row, :n] = 1
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            logits = np.asarray(
                self._session.run(None, {k: v for k, v in feeds.items() if k in self._inputs})[0]
            )
            scores[idx] = logits[:, 0] if logits.ndim == 2 else logits
        return scores


class RerankUnavailable(RuntimeError):
    pass


class Reranker:
    def __init__(
        self,
        model: ScoresPairs | None,
        slots: int = 1,
        *,
        breaker_window: int = 20,
        breaker_late_ratio: float = 0.5,
        cooldown_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self._slots = threading.BoundedSemaphore(slots)
        self._executor = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="rerank")
        self._outcomes: deque[bool] = deque(maxlen=breaker_window)  # True = finished within its deadline
        self._late_ratio = breaker_late_ratio
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._open_until = 0.0

    @property
    def shedding(self) -> bool:
        return self._clock() < self._open_until

    def _record(self, in_time: bool) -> None:
        self._outcomes.append(in_time)
        if len(self._outcomes) < (self._outcomes.maxlen or 1):
            return
        late = self._outcomes.count(False) / len(self._outcomes)
        if late >= self._late_ratio:
            self._open_until = self._clock() + self._cooldown_s
            self._outcomes.clear()  # after the cool-down, judge the reranker on fresh attempts only

    @property
    def available(self) -> bool:
        return self.model is not None

    def _run(self, query: str, docs: list[str]) -> np.ndarray:
        assert self.model is not None
        try:
            return self.model.score(query, docs)
        finally:
            self._slots.release()

    async def score(self, query: str, docs: list[str], timeout_s: float) -> np.ndarray:
        """Scores for (query, doc) pairs; raises RerankUnavailable / TimeoutError so callers can degrade."""
        if self.model is None:
            raise RerankUnavailable("no reranker model loaded")
        if self.shedding:
            raise RerankUnavailable("reranker shedding load")
        if not self._slots.acquire(blocking=False):
            raise RerankUnavailable("reranker busy")
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(self._executor, self._run, query, docs)
        except BaseException:
            self._slots.release()
            raise
        # shield: on timeout the thread keeps running (and holds its slot) but we stop waiting for it
        try:
            scores = await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
        except TimeoutError:
            self._record(False)
            raise
        self._record(True)
        return scores

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
