"""Translate seeded English product titles to Arabic, with a content-hash cache.

Providers:
  anthropic  Claude via the Message Batches API (50% price, async). Used when ANTHROPIC_API_KEY is
             exported in the environment (``--provider auto``).
             Submitted batch ids are persisted, so a re-run resumes polling instead of paying twice.
  marian     Local Helsinki-NLP/opus-mt-en-ar (MarianMT). Used automatically without an API key.
             Needs the optional extra: ``uv run --package souq-scripts --extra translate ...``.

Every translation is appended to data/cache/translations.jsonl keyed by sha256("en>ar\\n" + source), so
re-runs only translate what is missing and a crash loses at most one batch. Results are then written to
Postgres ``product_texts(lang='ar')`` and to the ``title_ar`` / ``title_ar_provider`` columns of
data/esci/products.parquet, and 100 random pairs go to scripts/seed/translation_check_100.csv (empty
``verdict`` / ``corrected_ar`` columns) for a human spot check.

Which titles: up to ``--limit`` (10k) products, those with an Exact judgment first (the Arabic golden
queries need relevant Arabic-titled documents), the rest in a stable hash order.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from souq_common.settings import get_settings
from souq_scripts.db import connect
from souq_scripts.paths import CACHE_DIR, ESCI_DIR, SEED_DIR

MARIAN_MODEL = "Helsinki-NLP/opus-mt-en-ar"
CACHE_FILE = CACHE_DIR / "translations.jsonl"
BATCH_STATE_FILE = CACHE_DIR / "translation_batches.json"
CHECK_FILE = SEED_DIR / "translation_check_100.csv"
# Higher-quality providers win when both have translated the same text.
PROVIDER_RANK = {"anthropic": 2, "marian": 1}

SYSTEM_PROMPT = (
    "You translate e-commerce product titles from English into Modern Standard Arabic as written on Gulf "
    "marketplaces (amazon.ae, noon). Keep brand names, model numbers, sizes and units exactly as in the "
    "source, in Latin script. Translate the descriptive words naturally; do not add or drop information. "
    "Return only the Arabic title in the title_ar field: no quotes, notes, explanations or English text "
    "other than the names and codes copied from the source."
)

# Structured output leaves the model nowhere to put commentary. Without it, ~25 % of a reviewed sample of
# free-text translations carried the model's own notes ("Wait, I should correct that…", "Note: I kept…")
# into the title — indexed as Arabic product text.
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"title_ar": {"type": "string"}},
    "required": ["title_ar"],
    "additionalProperties": False,
}


def log(msg: str) -> None:
    print(f"[translate] {msg}", file=sys.stderr, flush=True)


def cache_key(text: str, src: str = "en", tgt: str = "ar") -> str:
    return hashlib.sha256(f"{src}>{tgt}\n{text.strip()}".encode()).hexdigest()


_CLAUSE = re.compile(r",| - | – | \| |\(|\[|;")
_LEADING_TAG = re.compile(r"^\s*[(\[][^)\]]*[)\]]\s*")  # "(Refurbished) Apple ..." -> "Apple ..."
HEAD_WORDS = 14
_DANGLING = {
    "for",
    "with",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "by",
    "the",
    "a",
    "an",
    "&",
    "from",
    "at",
    "+",
}


def title_head(title: str) -> str:
    """The part of a marketplace title worth translating: its first clause, at most HEAD_WORDS words.

    ESCI titles run to 200+ characters of keyword stuffing ("..., Compatible with ..., Gift for ...").
    Arabic marketplace titles are short, and MarianMT both slows down and degrades (repetition loops,
    hallucinated words) on long inputs, so every provider translates the same head; the cache is keyed
    on it.
    """
    t = _LEADING_TAG.sub("", " ".join(title.split()))
    m = _CLAUSE.search(t)
    head = t[: m.start()].strip() if m else t
    if len(head.split()) < 3:  # "Apple, iPhone 15 ..." -> the clause is too short to stand alone
        head = _CLAUSE.sub(" ", t)
    words = head.split()[:HEAD_WORDS]
    # A cut mid-phrase ("... Gift Set for") translates to a dangling لـ/مع; end on a content word instead.
    while len(words) > 3 and words[-1].lower().strip(",:;") in _DANGLING:
        words.pop()
    return " ".join(words)


# --- cache -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Translation:
    key: str
    src: str
    tgt: str
    provider: str
    model: str


class TranslationCache:
    """Append-only JSONL. Later lines override earlier ones unless they come from a lower-ranked provider."""

    def __init__(self, path: Path = CACHE_FILE) -> None:
        self.path = path
        self.entries: dict[str, Translation] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._put(Translation(rec["key"], rec["src"], rec["tgt"], rec["provider"], rec["model"]))

    def _put(self, t: Translation) -> None:
        if not t.tgt.strip():  # an empty output (e.g. the model emitted only punctuation) is a miss
            return
        old = self.entries.get(t.key)
        if old is None or PROVIDER_RANK.get(t.provider, 0) >= PROVIDER_RANK.get(old.provider, 0):
            self.entries[t.key] = t

    def get(self, text: str) -> Translation | None:
        """Translation of an exact source text (already a title head)."""
        return self.entries.get(cache_key(text))

    def for_title(self, title: str) -> Translation | None:
        return self.get(title_head(title))

    def add_many(self, items: Iterable[Translation]) -> None:
        items = list(items)
        if not items:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self.path.open("a", encoding="utf-8") as fh:
            for t in items:
                rec = {"key": t.key, "src": t.src, "tgt": t.tgt, "provider": t.provider, "model": t.model,
                       "created_at": now}  # fmt: skip
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._put(t)


# --- providers ---------------------------------------------------------------------------------------


class Provider(Protocol):
    name: str
    model: str

    def translate(self, texts: Sequence[str], cache: TranslationCache, deadline: float) -> int:
        """Translate ``texts`` (all uncached) into ``cache``; return how many were added."""
        ...


def clean_output(text: str) -> str:
    return " ".join(text.strip().strip("\"'«»").rstrip(".").split())


_ARABIC = re.compile(r"[\u0600-\u06FF]")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'.\-]*")
_COMMENTARY = re.compile(r"\b(note|wait|correct(ion)?|translat\w*|i kept|i will|actually)\b|ملاحظة\s*:", re.I)


def arabic_title_ok(src: str, ar: str) -> bool:
    """Reject outputs that are not an Arabic product title: no Arabic, commentary, or English beyond the
    brand/model words copied from the source. A rejected title stays untranslated rather than indexed."""
    if not ar or "\n" in ar or not _ARABIC.search(ar) or _COMMENTARY.search(ar):
        return False
    source_words = {w.lower() for w in _LATIN_WORD.findall(src)}
    foreign = [w for w in _LATIN_WORD.findall(ar) if w.lower() not in source_words]
    return len(foreign) < 2 and len(ar) <= 3 * len(src) + 20


_KEEP_CASE = re.compile(r"\d|[A-Z].*[A-Z]|[a-z][A-Z]")  # codes, acronyms, camelCase: WH-1000XM5, RFID, iPhone


def marian_input(head: str) -> str:
    """Sentence-case a Title Case head and end it with a period.

    opus-mt-en-ar is trained mostly on sentence-case prose (UN/OPUS); Title Case input makes it treat
    every word as a name (garbled output, repetition loops that also run 5x slower). Codes and acronyms
    keep their case so they survive as written.
    """
    words = head.split()
    out = [w if _KEEP_CASE.search(w) else w.lower() for w in words]
    if out and not _KEEP_CASE.search(out[0]):
        out[0] = out[0][:1].upper() + out[0][1:]
    text = " ".join(out)
    return text if text.endswith((".", "!", "?")) else text + "."


class MarianProvider:
    name = "marian"

    def __init__(self, model: str = MARIAN_MODEL, batch_size: int = 64, num_beams: int = 2) -> None:
        self.model = model
        self.batch_size = batch_size
        self.num_beams = num_beams

    def translate(self, texts: Sequence[str], cache: TranslationCache, deadline: float) -> int:
        try:
            import torch
            from transformers import MarianMTModel, MarianTokenizer
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise SystemExit(
                "marian provider needs the optional extra: "
                "uv run --package souq-scripts --extra translate ..."
            ) from exc
        device = (
            "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
        )
        tok = MarianTokenizer.from_pretrained(self.model)
        mdl = MarianMTModel.from_pretrained(self.model).to(device).eval()
        log(f"marian on {device}, batch {self.batch_size}, beams {self.num_beams}")
        # Length-sorted batches waste far less compute on padding.
        ordered = sorted(texts, key=len)
        done, started = 0, time.monotonic()
        for i in range(0, len(ordered), self.batch_size):
            if time.monotonic() > deadline:
                log(f"time budget reached after {done} translations")
                break
            chunk = ordered[i : i + self.batch_size]
            batch = [marian_input(t) for t in chunk]
            enc = tok(batch, return_tensors="pt", padding=True, truncation=True, max_length=64).to(device)
            with torch.inference_mode():
                out = mdl.generate(**enc, num_beams=self.num_beams, max_new_tokens=96, no_repeat_ngram_size=3)
            decoded = tok.batch_decode(out, skip_special_tokens=True)
            cleaned = ((src, clean_output(tgt)) for src, tgt in zip(chunk, decoded, strict=True))
            cache.add_many(
                Translation(cache_key(src), src, tgt, self.name, self.model) for src, tgt in cleaned if tgt
            )
            done += len(chunk)
            if (i // self.batch_size) % 20 == 0:
                log(f"  {done}/{len(ordered)} ({done / max(1e-6, time.monotonic() - started):.1f}/s)")
        return done


class AnthropicBatchProvider:
    """Claude Message Batches. ``client`` is injectable so tests never touch the network."""

    name = "anthropic"

    def __init__(
        self,
        model: str,
        client: Any | None = None,
        state_file: Path = BATCH_STATE_FILE,
        poll_s: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self._client = client
        self.state_file = state_file
        self.poll_s = poll_s
        self.sleep = sleep

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise SystemExit(
                    "anthropic provider needs: uv run --package souq-scripts --extra anthropic"
                ) from exc
            self._client = anthropic.Anthropic()
        return self._client

    def build_requests(self, texts: Sequence[str]) -> list[dict[str, Any]]:
        # custom_id must match ^[a-zA-Z0-9_-]{1,64}$: the first 64 hex chars of the content hash.
        return [
            {
                "custom_id": cache_key(t)[:64],
                "params": {
                    "model": self.model,
                    # Thinking off (allowed at low effort on Haiku 5.5): a one-line translation needs no
                    # deliberation, and it was the source of the "Wait, I should correct…" leaks.
                    "max_tokens": 512,
                    "thinking": {"type": "disabled"},
                    "output_config": {
                        "effort": "low",
                        "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                    },
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": t}],
                },
            }
            for t in texts
        ]

    def _load_state(self) -> list[str]:
        if self.state_file.exists():
            ids: list[str] = json.loads(self.state_file.read_text())["pending"]
            return ids
        return []

    def _save_state(self, pending: list[str]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"pending": pending}, indent=1))

    def collect(self, batch_id: str, by_id: dict[str, str], cache: TranslationCache) -> tuple[int, int]:
        ok = failed = 0
        items = []
        for res in self.client.messages.batches.results(batch_id):
            src = by_id.get(res.custom_id)
            if res.result.type != "succeeded" or src is None:
                failed += 1
                continue
            msg = res.result.message
            if getattr(msg, "stop_reason", None) == "refusal":
                failed += 1
                continue
            raw = next((b.text for b in msg.content if b.type == "text"), "")
            try:
                text = clean_output(str(json.loads(raw).get("title_ar", "")))
            except (ValueError, AttributeError):
                text = ""
            if text and arabic_title_ok(src, text):
                items.append(Translation(cache_key(src), src, text, self.name, self.model))
                ok += 1
            else:
                failed += 1
        cache.add_many(items)
        return ok, failed

    def translate(self, texts: Sequence[str], cache: TranslationCache, deadline: float) -> int:
        by_id = {cache_key(t)[:64]: t for t in texts}
        pending = self._load_state()
        if not pending and texts:
            batch = self.client.messages.batches.create(requests=self.build_requests(texts))
            pending = [batch.id]
            self._save_state(pending)
            log(f"submitted batch {batch.id} with {len(texts)} requests")
        added = 0
        while pending:
            still = []
            for bid in pending:
                batch = self.client.messages.batches.retrieve(bid)
                if batch.processing_status != "ended":
                    still.append(bid)
                    continue
                ok, failed = self.collect(bid, by_id, cache)
                added += ok
                log(f"batch {bid} ended: {ok} translated, {failed} failed/skipped")
            pending = still
            self._save_state(pending)
            if pending:
                if time.monotonic() > deadline:
                    log(f"time budget reached; batches {pending} still running — re-run to collect them")
                    break
                self.sleep(self.poll_s)
        return added


def pick_provider(name: str, model: str | None) -> Provider:
    if name == "auto":
        # Only an exported key counts (what the SDK itself resolves), not one sitting in a .env file:
        # a paid batch should never start because of a file nobody exported on purpose.
        name = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "marian"
    if name == "anthropic":
        return AnthropicBatchProvider(model or get_settings().souq_llm_model)
    return MarianProvider(model or MARIAN_MODEL)


# --- selection + outputs -----------------------------------------------------------------------------


def select_titles(products: pa.Table, judgments: pa.Table | None, limit: int) -> list[tuple[str, str]]:
    exact: set[str] = set()
    if judgments is not None:
        for sku, label in zip(judgments["sku"].to_pylist(), judgments["esci_label"].to_pylist(), strict=True):
            if label == "E":
                exact.add(sku)
    rows = list(zip(products["sku"].to_pylist(), products["title_en"].to_pylist(), strict=True))

    def rank(r: tuple[str, str]) -> tuple[bool, str]:
        return (r[0] not in exact, hashlib.blake2b(r[0].encode(), digest_size=8).hexdigest())

    return sorted(rows, key=rank)[:limit]


def write_parquet_titles(path: Path, cache: TranslationCache) -> int:
    t = pq.read_table(path)
    titles, providers, n = [], [], 0
    for title in t["title_en"].to_pylist():
        hit = cache.for_title(title)
        titles.append(hit.tgt if hit else None)
        providers.append(hit.provider if hit else None)
        n += hit is not None
    t = t.set_column(t.schema.get_field_index("title_ar"), "title_ar", pa.array(titles, pa.string()))
    t = t.set_column(
        t.schema.get_field_index("title_ar_provider"), "title_ar_provider", pa.array(providers, pa.string())
    )
    pq.write_table(t, path)
    return n


def load_postgres(pairs: list[tuple[str, str]], database_url: str | None) -> int:
    with connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO product_texts (sku, lang, title) VALUES (%s, 'ar', %s)
                   ON CONFLICT (sku, lang) DO UPDATE SET title = EXCLUDED.title""",
                pairs,
            )
        conn.commit()
    return len(pairs)


def write_check_sheet(
    rows: list[tuple[str, str, Translation]], path: Path, n: int = 100, seed: int = 100
) -> None:
    sample = random.Random(seed).sample(rows, min(n, len(rows)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["sku", "title_en", "translated_source", "title_ar", "provider", "verdict", "corrected_ar"]
        )
        for sku, en, t in sorted(sample, key=lambda r: r[0]):
            w.writerow([sku, en, t.src, t.tgt, t.provider, "", ""])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=["auto", "anthropic", "marian"], default="auto")
    ap.add_argument(
        "--model", default=None, help="override model id (default: SOUQ_LLM_MODEL / opus-mt-en-ar)"
    )
    ap.add_argument("--limit", type=int, default=10_000)
    ap.add_argument("--time-budget-s", type=float, default=1200)
    ap.add_argument("--esci-dir", default=str(ESCI_DIR))
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--no-load", action="store_true", help="skip writing Arabic titles to Postgres")
    ap.add_argument(
        "--cached-only", action="store_true", help="publish cached translations; translate nothing"
    )
    args = ap.parse_args(argv)

    esci = Path(args.esci_dir)
    products = pq.read_table(esci / "products.parquet", columns=["sku", "title_en"])
    jpath = esci / "judgments.parquet"
    judgments = pq.read_table(jpath, columns=["sku", "esci_label"]) if jpath.exists() else None
    chosen = select_titles(products, judgments, args.limit)
    cache = TranslationCache()
    todo = sorted({title_head(title) for _, title in chosen if cache.for_title(title) is None})
    provider = pick_provider(args.provider, args.model)
    log(
        f"{len(chosen)} titles selected, {len(chosen) - len(todo)} cached, "
        f"{len(todo)} to translate via {provider.name}"
    )
    started = time.monotonic()
    if todo and not args.cached_only:
        provider.translate(todo, cache, deadline=started + args.time_budget_s)
    translated = [(sku, en, t) for sku, en in chosen if (t := cache.for_title(en)) is not None]
    log(
        f"{len(translated)}/{len(chosen)} selected titles have Arabic "
        f"({time.monotonic() - started:.0f}s this run)"
    )

    n_parquet = write_parquet_titles(esci / "products.parquet", cache)
    log(f"products.parquet: {n_parquet} rows with title_ar")
    if not args.no_load:
        # Same set as the parquet: every product whose title head has a cached translation.
        everything = zip(products["sku"].to_pylist(), products["title_en"].to_pylist(), strict=True)
        pairs = [(sku, t.tgt) for sku, en in everything if (t := cache.for_title(en)) is not None]
        n_loaded = load_postgres(pairs, args.database_url)
        log(f"Postgres: upserted {n_loaded} Arabic titles")
    if translated:
        write_check_sheet(translated, CHECK_FILE)
        log(f"wrote {CHECK_FILE}")


if __name__ == "__main__":
    main()
