# evals

Relevance and parsing evals for `services/search`. The harness runs the real `SearchPipeline` in-process
against its own OpenSearch index and writes `evals/reports/<run_id>.json` (schema in CLAUDE.md; consumed
by CI) plus a markdown table next to it.

```
uv run --package souq-evals python -m souq_evals --offline --out evals/reports/ci.json   # PR CI
uv run --package souq-evals python -m souq_evals --full                                  # nightly (SOUQ_LLM_MODE decides replay/live)
uv run --package souq-evals python -m souq_evals --offline --no-thresholds --rebuild-index
uv run --package souq-evals python -m souq_evals label export --slice ar [--with-retrieved]
uv run --package souq-evals python -m souq_evals label import evals/labelling/ar_to_label.csv
uv run --package souq-evals python -m souq_evals judge --slice ar    # LLM judge, behind the kappa gate
```

Exit code 0 = thresholds met, 1 = a floor in `evals/thresholds.yaml` missed, 2 = refused / setup error.

## What a run needs

| | offline | full |
|---|---|---|
| OpenSearch (`OPENSEARCH_URL`) | yes — the harness creates index `souq_eval` from `infra/opensearch/templates/products_v2.json` | yes |
| `data/esci/{products,queries,judgments}.parquet` | yes (corpus) | yes |
| embedder | `SOUQ_EMBEDDER=bge-m3` + `models/artifacts/bge-m3-int8` (`fake` works, kNN is then a hash baseline and the report says so) | same |
| reranker | `models/artifacts/reranker-int8` (absent → rerank ablations report fused order, noted in `warnings`) | same |
| LLM | not used; `rrf_rerank_llm` is omitted from the report | replay fixtures in `tests/fixtures/llm/` (or `live`/`record` with `ANTHROPIC_API_KEY`) |
| Postgres / Redis | no | no |

The eval corpus is every judged product of the golden set plus 5,000 seeded random ESCI products
(`--distractors`), ~8.4k docs. Document embeddings are cached in `models/artifacts/cache/`; the index is
rebuilt only when corpus, template or embedder change, and is force-merged to one segment so HNSW and BM25
statistics are identical run to run. Offline mode is deterministic given the same artefacts.

Stage budgets are multiplied by `--budget-scale` (default 50) so quality numbers don't move with machine
load; latency is still measured per stage and every degraded response is counted in `degraded`. Use
`--budget-scale 1` to see production-budget degradation rates.

## Ablations and metrics

`bm25`, `knn`, `rrf` (both, RRF k=60), `rrf_rerank` (+ cross-encoder on the top 30), and in full mode
`rrf_rerank_llm` (+ LLM query parsing). Per slice (`en`, `ar`, `arabizi_mixed`, `all`): nDCG@10 (graded
gains 0-3, unjudged = 0), Recall@100 and MRR (relevant = grade ≥ 2), zero-result rate. Also filter-parse
exact match and per-field accuracy over `filter_parse.jsonl`, per-stage p50/p95 (from the production
ablation), LLM path share, average tokens and USD per 1k queries (prices from `config/pricing.yaml`).

## Golden set (`golden/*.jsonl`, built by `scripts/build_golden.py` from `golden/sources.yaml`)

| slice | n | source | label_source |
|---|---|---|---|
| `en` | 60 | ESCI queries (US, small version) with their human ESCI judgments (E=3, S=2, C=1, I=0) | `esci` |
| `ar` | 60 | our Arabic translations of other ESCI queries; candidate pool = that query's ESCI judgments | `provisional` |
| `arabizi_mixed` | 30 | 10 naturally misspelled ESCI queries (`esci`), 10 Arabic + Latin brand/model, 10 Arabizi (`provisional`) | mixed |

**The Arabic slice must be hand-labelled by the maintainer.** ESCI judgments were made for the English query,
so they are only a provisional starting point for a translated query; every report repeats this as a warning
(`labels.<slice>.provisional_share`) until the labels are confirmed:

1. `python -m souq_evals label export --slice ar --with-retrieved` writes `evals/labelling/ar_to_label.csv`:
   one row per (query, product) with the provisional grade, plus the system's own top-20 results that ESCI
   never judged (`candidate_source: retrieved`) so the pool is not biased towards the English candidates.
2. Fill `grade` (3 exact, 2 substitute, 1 complement, 0 irrelevant; blank = skip) in any spreadsheet.
3. `python -m souq_evals label import evals/labelling/ar_to_label.csv` marks them `label_source: maintainer`
   in `golden/ar.jsonl` (kept across `build_golden.py` rebuilds). Commit the jsonl.

Product titles in Arabic come from the data agent's translation of the ESCI seed (`title_ar`); the report's
`setup.title_ar_coverage` says how much of the corpus has one — with low coverage Arabic BM25 has little to
match and the Arabic slice leans on cross-lingual kNN.

## LLM judge gate

`souq_evals.judge` can grade new (query, product) pairs with the LLM, but only after calibration: it first
grades pairs the maintainer labelled, and Cohen's kappa against the maintainer must be ≥ 0.6 over ≥ 50 pairs
(`souq_evals.kappa.judge_gate`). With fewer than 50 maintainer labels — the state of this repo today — it
refuses without making any call (`judge` exits 2).

## Filter-parse cases (`filter_parse.jsonl`)

80 hand-written cases (40 English, 25 Arabic, 15 Arabizi/mixed) with the expected `Filters`; 8 need the LLM
(prices in words, "around X" → 0.8X–1.2X, budgets). They were written alongside the rule parser, so treat
the rule-only score as a development-set number, not a blind test. In full mode with the committed
replay fixtures the LLM answers are hand-authored (`recorded_at: synthetic`), so `filter_parse` with the
LLM measures the plumbing, not the model; the report says so in `warnings`.

## Thresholds

`thresholds.yaml` holds absolute floors on the best production ablation present (`rrf_rerank_llm` >
`rrf_rerank` > `rrf`) per slice, plus filter-parse exact match. CI also compares with the main-branch
report and blocks an nDCG@10 drop of more than 1 point on any slice.
