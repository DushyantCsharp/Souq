from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from souq_scripts.seed.translate import (
    AnthropicBatchProvider,
    Translation,
    TranslationCache,
    arabic_title_ok,
    cache_key,
    marian_input,
    pick_provider,
    select_titles,
    title_head,
)


def test_cache_roundtrip_and_provider_rank(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    cache = TranslationCache(path)
    cache.add_many([Translation(cache_key("red shoe"), "red shoe", "حذاء أحمر", "marian", "m")])
    cache.add_many([Translation(cache_key("red shoe"), "red shoe", "حذاء احمر (claude)", "anthropic", "c")])
    cache.add_many([Translation(cache_key("red shoe"), "red shoe", "worse", "marian", "m")])
    reloaded = TranslationCache(path)
    hit = reloaded.get("red shoe")
    assert hit is not None
    assert hit.provider == "anthropic", "a marian re-run never overrides Claude output"
    assert len(path.read_text().splitlines()) == 3, "append-only"


def test_title_head_and_marian_input() -> None:
    assert (
        title_head("Sony WH-1000XM5 Wireless Headphones, Black, Bluetooth 5.2")
        == "Sony WH-1000XM5 Wireless Headphones"
    )
    assert title_head("(Refurbished) Apple iPhone 8, 64GB") == "Apple iPhone 8"
    assert title_head("Apple, iPhone 15 Pro") == "Apple iPhone 15 Pro"
    assert len(title_head(" ".join(["word"] * 40)).split()) == 14
    assert marian_input("Russian Keyboard Stickers") == "Russian keyboard stickers."
    assert marian_input("Sony WH-1000XM5 Wireless RFID Case") == "Sony WH-1000XM5 wireless RFID case."
    assert marian_input("Apple iPhone 15 Pro") == "Apple iPhone 15 pro."


def test_select_prefers_exact_judged_products() -> None:
    products = pa.table({"sku": ["A", "B", "C", "D"], "title_en": ["a", "b", "c", "d"]})
    judgments = pa.table({"sku": ["C", "D", "B"], "esci_label": ["E", "I", "E"]})
    chosen = select_titles(products, judgments, 2)
    assert {s for s, _ in chosen} == {"B", "C"}


def test_auto_provider_ignores_dotenv_keys(monkeypatch: Any) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert pick_provider("auto", None).name == "marian"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert pick_provider("auto", None).name == "anthropic"


class FakeBatches:
    def __init__(self) -> None:
        self.created: list[list[dict[str, Any]]] = []
        self.polls = 0

    def create(self, requests: list[dict[str, Any]]) -> Any:
        self.created.append(requests)
        return SimpleNamespace(id="msgbatch_1")

    def retrieve(self, batch_id: str) -> Any:
        self.polls += 1
        return SimpleNamespace(processing_status="ended" if self.polls >= 2 else "in_progress")

    def results(self, batch_id: str) -> list[Any]:
        def ok(custom_id: str, text: str) -> Any:
            msg = SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
            )
            return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=msg))

        reqs = self.created[0]
        return [
            ok(reqs[0]["custom_id"], '{"title_ar": " \\"سماعات Sony\\" "}'),
            SimpleNamespace(custom_id=reqs[1]["custom_id"], result=SimpleNamespace(type="errored")),
        ]


def test_anthropic_batch_provider_submits_once_polls_and_caches(tmp_path: Path) -> None:
    batches = FakeBatches()
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    state = tmp_path / "state.json"
    provider = AnthropicBatchProvider(
        "claude-haiku-5-5", client=client, state_file=state, sleep=lambda s: None
    )
    cache = TranslationCache(tmp_path / "t.jsonl")
    added = provider.translate(["Sony headphones", "Broken one"], cache, deadline=float("inf"))

    assert added == 1
    req = batches.created[0][0]
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", req["custom_id"])
    assert req["params"]["model"] == "claude-haiku-5-5"
    assert req["params"]["messages"] == [{"role": "user", "content": "Sony headphones"}]
    hit = cache.get("Sony headphones")
    assert hit is not None
    assert hit.tgt == "سماعات Sony"
    assert req["params"]["thinking"] == {"type": "disabled"}
    assert req["params"]["output_config"]["format"]["type"] == "json_schema"
    assert hit.provider == "anthropic"
    assert cache.get("Broken one") is None
    assert '"pending": []' in state.read_text()


def test_anthropic_batch_provider_resumes_a_submitted_batch(tmp_path: Path) -> None:
    batches = FakeBatches()
    batches.created.append([{"custom_id": cache_key("x")[:64]}, {"custom_id": cache_key("y")[:64]}])
    state = tmp_path / "state.json"
    state.write_text('{"pending": ["msgbatch_1"]}')
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    provider = AnthropicBatchProvider("m", client=client, state_file=state, sleep=lambda s: None)
    provider.translate(["x", "y"], TranslationCache(tmp_path / "t.jsonl"), deadline=float("inf"))
    assert len(batches.created) == 1, "a pending batch is polled, never resubmitted"


def test_empty_outputs_are_cache_misses(tmp_path: Path) -> None:
    cache = TranslationCache(tmp_path / "t.jsonl")
    cache.add_many([Translation(cache_key("x"), "x", "  ", "marian", "m")])
    assert cache.get("x") is None
    assert TranslationCache(tmp_path / "t.jsonl").get("x") is None


def test_title_head_does_not_end_on_a_dangling_word() -> None:
    words = "Bamboo Cutting Board Set Juice Groove Large Thick Kitchen Meat Cheese Board Gift for Kids"
    # the 14-word cut lands on "for"; the head must end on the content word before it
    assert (
        title_head(words)
        == "Bamboo Cutting Board Set Juice Groove Large Thick Kitchen Meat Cheese Board Gift"
    )


def test_arabic_title_validator_rejects_commentary_and_english() -> None:
    src = "Tweezerman Slant Tweezer Stainless Steel"
    assert arabic_title_ok(src, "ملقط Tweezerman مائل من الفولاذ المقاوم للصدأ")
    assert not arabic_title_ok(src, "ملقط مائل… Wait, I should correct that to Tweezerman")
    assert not arabic_title_ok(src, "Note: I kept the brand name. ملقط Tweezerman")
    assert not arabic_title_ok(src, "ملاحظة: أبقيت اسم العلامة")
    assert not arabic_title_ok(src, "Tweezerman slant tweezer")  # no Arabic at all
    assert not arabic_title_ok(src, "")
