from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from souq_common.arabic import ARABIC_CHAR_MAP, normalise_arabic, opensearch_char_filter_mappings
from souq_scripts.templates import (
    CHAR_FILTER,
    CONTRACT_FIELDS,
    available_versions,
    check_all,
    load_template,
    sync_char_filter,
    template_path,
    validate_superset,
    validate_template,
)

ARABIC_SAMPLES = [
    "الْعَرَبِيَّةُ",  # tashkeel
    "مـــكتبة",  # tatweel
    "أحمد إبراهيم آمنة ٱلله",  # alef forms
    "مستشفى على",  # alef maqsura
    "مدرسة جميلة",  # ta marbuta
    "سؤال رئيس",  # hamza on waw / ya
    "١٢٣ ۴۵۶ ٣٫٥ ١٬٠٠٠",  # Arabic-Indic + Persian digits, separators
    "کتاب فارسی",  # keheh, Farsi yeh
    "سماعات‌لاسلكية‍",  # ZWNJ / ZWJ inside words
    "‏سوني‎ WH-1000XM5",  # RLM / LRM around mixed text
    "Sony Headphones 128GB",  # Latin passes through untouched (case preserved)
    "",
]


def emulate_mapping_char_filter(rules: list[str], text: str) -> str:
    """Lucene MappingCharFilter semantics: greedy longest match of rule keys, scanning left to right."""
    mapping: dict[str, str] = {}
    for rule in rules:
        key, _, value = rule.partition("=>")
        decode = lambda s: re.sub(r"\\u([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)  # noqa: E731
        mapping[decode(key.strip())] = decode(value.strip())
    longest = max(map(len, mapping))
    out, i = [], 0
    while i < len(text):
        for n in range(min(longest, len(text) - i), 0, -1):
            if text[i : i + n] in mapping:
                out.append(mapping[text[i : i + n]])
                i += n
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def test_repo_templates_satisfy_the_contract() -> None:
    assert available_versions() == ["v1", "v2"]
    assert check_all() == []


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_char_filter_rules_are_generated_from_souq_common(version: str) -> None:
    analysis = load_template(version)["template"]["settings"]["analysis"]
    assert analysis["char_filter"][CHAR_FILTER]["mappings"] == opensearch_char_filter_mappings()


@pytest.mark.parametrize("text", ARABIC_SAMPLES)
def test_char_filter_rules_mirror_normalise_arabic(text: str) -> None:
    assert emulate_mapping_char_filter(opensearch_char_filter_mappings(), text) == normalise_arabic(text)


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.sampled_from([*ARABIC_CHAR_MAP, *"ابتثجحخدذرزسشصضطظعغفقكلمنهوي ءaZ09-"])))
def test_char_filter_rules_mirror_normalise_arabic_on_any_text(text: str) -> None:
    assert emulate_mapping_char_filter(opensearch_char_filter_mappings(), text) == normalise_arabic(text)


def test_v2_adds_prefix_subfields_and_drops_nothing() -> None:
    v1, v2 = load_template("v1"), load_template("v2")
    assert validate_superset(v1, v2) == []
    props = v2["template"]["mappings"]["properties"]
    for f in ("title_en", "title_ar"):
        assert props[f]["fields"]["prefix"]["type"] == "text"
    assert "prefix" not in v1["template"]["mappings"]["properties"]["title_en"]["fields"]


def test_every_contract_field_is_mapped() -> None:
    props = load_template("v2")["template"]["mappings"]["properties"]
    assert {f: props[f]["type"] for f in CONTRACT_FIELDS} == CONTRACT_FIELDS
    assert props["embedding"]["dimension"] == 1024


def _broken(version: str, mutate: Any) -> list[str]:
    body = copy.deepcopy(load_template(version))
    mutate(body)
    return validate_template(body, version)


def test_validator_catches_contract_breaks() -> None:
    props = lambda b: b["template"]["mappings"]["properties"]  # noqa: E731
    assert any("dimension" in e for e in _broken("v1", lambda b: props(b)["embedding"].update(dimension=768)))
    assert any("price" in e for e in _broken("v1", lambda b: props(b).pop("price")))
    assert any("drifted" in e for e in _broken(
        "v1", lambda b: b["template"]["settings"]["analysis"]["char_filter"][CHAR_FILTER]["mappings"].pop()
    ))  # fmt: skip
    assert any("prefix" in e for e in _broken("v2", lambda b: props(b)["title_ar"]["fields"].pop("prefix")))
    assert any(
        "index_patterns" in e for e in _broken("v1", lambda b: b.update(index_patterns=["products_*"]))
    )


def test_sync_rewrites_drifted_rules(tmp_templates: Path) -> None:
    path = template_path("v1", tmp_templates)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["template"]["settings"]["analysis"]["char_filter"][CHAR_FILTER]["mappings"] = ["\\u0623=>ا"]
    path.write_text(json.dumps(body, ensure_ascii=False))
    assert sync_char_filter(path, write=False) is True
    assert sync_char_filter(path, write=True) is True
    assert sync_char_filter(path, write=False) is False
    assert check_all(tmp_templates) == []
