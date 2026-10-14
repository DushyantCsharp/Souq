"""Build evals/golden/*.jsonl from golden/sources.yaml and the ESCI seed (data/esci/*.parquet).

    uv run --package souq-evals python evals/scripts/build_golden.py

Existing maintainer labels are preserved: a judgment already marked ``label_source: maintainer`` keeps its
grade when the golden set is rebuilt.
"""

from __future__ import annotations

from collections import defaultdict

import pyarrow.parquet as pq
import yaml

from souq_evals.golden import SLICE_LANG, GoldenQuery, Judgment, load_golden, write_slice
from souq_evals.paths import ESCI, GOLDEN


def main() -> int:
    sources = yaml.safe_load((GOLDEN / "sources.yaml").read_text(encoding="utf-8"))
    queries = {
        int(r["query_id"]): str(r["query"]) for r in pq.read_table(ESCI / "queries.parquet").to_pylist()
    }
    judged: dict[int, dict[str, int]] = defaultdict(dict)
    for r in pq.read_table(ESCI / "judgments.parquet").to_pylist():
        judged[int(r["query_id"])][str(r["sku"])] = int(r["gain"])
    previous = {
        (q.id, j.sku): j for q in load_golden() for j in q.judgments if j.label_source == "maintainer"
    }
    out: list[GoldenQuery] = []
    for slice_name, entries in sources.items():
        for entry in entries:
            qid = int(entry["esci"])
            kind = entry.get("kind", "native" if slice_name == "en" else "translated")
            text = entry.get("q") or queries[qid]
            provisional = slice_name == "ar" or kind in ("mixed", "arabizi")
            gid = f"{slice_name}-{qid}"
            judgments = []
            for sku, grade in sorted(judged[qid].items()):
                kept = previous.get((gid, sku))
                judgments.append(
                    kept
                    or Judgment(sku=sku, grade=grade, label_source="provisional" if provisional else "esci")
                )
            out.append(
                GoldenQuery(
                    id=gid,
                    slice=slice_name,
                    q=text,
                    lang=SLICE_LANG[slice_name],
                    kind=kind,
                    source_query_id=qid,
                    source_query=queries[qid],
                    judgments=judgments,
                )
            )
    for slice_name in sources:
        path = write_slice(out, slice_name)
        n = sum(1 for q in out if q.slice == slice_name)
        print(f"{path}: {n} queries, {sum(len(q.judgments) for q in out if q.slice == slice_name)} judgments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
