# Eval methodology

How the relevance and query-understanding numbers in `evals/reports/*.json` are produced, what they can and
cannot tell you, and how the gates were set. How to run the harness is in [evals/README.md](../evals/README.md).
The report schema is in [CLAUDE.md](../CLAUDE.md) ("Eval report").

## Datasets

| Set | Size | Source | Labels |
|---|---|---|---|
| Golden `en` | 60 queries, 1,553 judgments | ESCI (Amazon Shopping Queries), US locale, `small_version == 1` | ESCI human judgments (`label_source: esci`) |
| Golden `ar` | 60 queries, 1,215 judgments | Arabic translations of *other* ESCI English queries; candidate pool = that English query's ESCI judgments | started as copies of the ESCI judgments (`provisional`); being re-graded by Claude (`claude`), see below |
| Golden `arabizi_mixed` | 30 queries, 603 judgments | 10 naturally misspelled ESCI queries; 10 Arabic script + Latin brand/model; 10 Arabizi | misspelled: ESCI (`esci`, 212 judgments); mixed and Arabizi: `provisional` (391), being re-graded by Claude |
| Filter-parse cases | 80 | hand-written: 40 English, 25 Arabic, 15 Arabizi/mixed (`lang: auto`); 8 need the LLM (prices in words, "around X", budgets) | expected `Filters` written by hand |
| Eval corpus | 8,354 docs | every judged product of the golden set + 5,000 seeded random ESCI products | — |

Judgment counts are from `evals/golden/*.jsonl`. Corpus size is `setup.corpus_docs` in the reports.

- **Golden set construction.** `evals/scripts/build_golden.py` reads the curated query list in
  `evals/golden/sources.yaml` (ESCI query ids, plus the Arabic/Arabizi query text) and pulls graded
  judgments from `data/esci/judgments.parquet`.
- **ESCI seed.** `uv run souq-seed-esci` writes `data/esci/{products,queries,judgments}.parquet`: 50,011
  products, 2,558 queries, 51,713 judgments (`docs/handoffs/data-STATUS.md`). `sku` is the ESCI
  `product_id`. `judgments.parquet` holds `query_id, sku, esci_label (E|S|C|I), gain (3|2|1|0)`. ESCI has
  no category tree, so `category_path` comes from a keyword-rule classifier. Arabic titles (`title_ar`)
  are machine translations with provenance in `title_ar_provider` (full schema:
  `docs/handoffs/search.md`).

### Grade mapping

| ESCI label | Meaning | Grade |
|---|---|---|
| E | Exact | 3 |
| S | Substitute | 2 |
| C | Complement | 1 |
| I | Irrelevant | 0 |

Hand and model labels (`label import`) use the same 0–3 scale.

### Corpus construction

The harness builds its own index (`souq_eval`) from `infra/opensearch/templates/products_v2.json`, the
production template. It contains every judged product of the golden set plus 5,000 distractors drawn from
the rest of the ESCI seed with a fixed RNG seed (`souq_evals.corpus.build_corpus`, `seed=7`), ~8.4k docs
in total. Document embeddings are cached in `models/artifacts/cache/`. The index is rebuilt only when the
corpus fingerprint, the template or the embedder changes. It is force-merged to one segment so HNSW graphs
and BM25 statistics are identical run to run. Offline mode is deterministic given the same artefacts.

Stage budgets are multiplied by `--budget-scale` (default 50), so quality numbers don't move with machine
load. Every degraded response is still counted. Per-stage latency in the report comes from this
relaxed-budget run. It is a relative signal, not a production latency measurement (see
`docs/perf-report.md` for that).

## Ablations

| Ablation | Pipeline | Mode |
|---|---|---|
| `bm25` | lexical only | offline, full |
| `knn` | bge-m3 kNN only | offline, full |
| `rrf` | BM25 ∥ kNN, reciprocal-rank fusion, k = 60 | offline, full |
| `rrf_rerank` | + cross-encoder over the top 30 (`search.rerank_mode` from `config/budgets.yaml`) | offline, full |
| `rrf_rerank_llm` | + LLM query parsing | full only |

The "production ablation" is the most complete one present, in the order `rrf_rerank_llm` > `rrf_rerank` >
`rrf`. Thresholds, the CI gate and the README targets all read that row.

## Metrics

Per slice (`en`, `ar`, `arabizi_mixed`) and pooled (`all`, all 150 queries, so `en` and `ar` weigh 60 each
and `arabizi_mixed` 30). Each metric is an unweighted mean over the slice's queries
(`souq_evals.metrics`).

- **nDCG@10.** DCG = Σᵢ₌₁..₁₀ (2^gᵢ − 1) / log₂(i + 1), with exponential gain, so Exact = 7, Substitute = 3,
  Complement = 1. The ideal DCG uses the query's own judged grades, sorted. Unjudged documents count as 0,
  the standard choice for pooled judgments, which penalises a system for surfacing good products nobody
  judged.
- **Recall@100.** |relevant ∩ top 100| / |relevant|, where *relevant* = grade ≥ 2 (Exact or Substitute).
  Queries with no relevant judgment are left out of the mean.
- **MRR.** 1 / rank of the first relevant (grade ≥ 2) result, 0 if none.
- **Zero-result rate.** Share of queries that return no results at all.
- **Filter-parse exact match.** A case counts only if the predicted `Filters` equal the expected ones on
  all seven fields (`price_min`, `price_max`, `currency`, `brands`, `colours`, `sizes`, `category`) after
  normalisation. Per-field accuracy is reported too, with price as the mean of min and max. Offline
  reports score the rule parser. Full reports score rule + LLM and keep the rule-only score under
  `filter_parse.rule_only`.
- **LLM.** `path_share` (queries answered by the LLM or the semantic cache), `avg_tokens` (input + output
  per LLM call), `usd_per_1k_queries` (prices from `config/pricing.yaml`).

No confidence intervals are computed. With 30–60 queries per slice, differences of a point or two between
ablations may be noise (significance not measured).

## Label sources and known biases

Read every relevance number with these in mind. Each report repeats the relevant ones under `warnings`,
and records the label mix per slice under `labels` (`provisional_share`, `claude_share`).

1. **English labels are ESCI's.** Human judgments made by Amazon for Amazon US shoppers and Amazon's
   catalogue, not a Gulf marketplace. They are the only human labels in the set.
2. **Arabic queries derive from English ones.** Each Arabic query is a translation of an ESCI English
   query, not an organic Arabic search, and its candidate pool started as that English query's ESCI pool.
   The labelling export (`label export --with-retrieved`) adds the system's own top-20 unjudged results,
   so the pool is not limited to what the English candidates contained.
3. **Arabic product titles are machine-translated.** Titles only, no Arabic descriptions. The first pass
   used Marian (`Helsinki-NLP/opus-mt-en-ar`) on title heads (first clause, ≤ 14 words). The data agent
   called its quality mediocre, and the 100-pair check sheet (`scripts/seed/translation_check_100.csv`)
   has no verdicts filled in, so translation quality was not measured. Marian is being replaced by
   Claude Haiku 5.5 translation via the Message Batches API; see the translation run for counts. In the
   reports dated 2026-10-14 only 19.55 % of the eval corpus had an Arabic title (`setup.title_ar_coverage`).
   MT output is also more regular than real supplier Arabic (consistent spelling, no dialect, one
   transliteration per brand), which probably flatters lexical matching and the normaliser.
4. **The Arabic BM25 baseline is weak, which makes the Arabic lift target easy to reach.** With few Arabic
   titles, `bm25` returned zero results for 60 % of Arabic queries (nDCG@10 0.0682,
   `evals/reports/final-full.json`). Arabic gains over BM25 therefore mostly measure cross-lingual kNN
   against an almost empty lexical baseline. Treat the Arabic lift as directional. It should shrink as
   title coverage grows.
5. **The Arabic and Arabizi/mixed slices are graded by Claude.** At the maintainer's request, Claude grades
   the `ar` and `arabizi_mixed` candidates (`evals/labelling/*_to_label.csv`), and they are imported with
   `label import --source claude` as `label_source: claude`. These numbers measure **agreement with a
   model's judgement, not a human's**. There is also a shared-model risk: the LLM query parser is Claude,
   and the new Arabic titles are translated by Claude, so the grader may favour text written by its own
   model family (not measured). Before this, the slices carried `provisional` labels copied from the
   English ESCI judgments (100 % of `ar`, 65 % of `arabizi_mixed` in the 2026-10-14 reports). There is
   one label source per slice and no inter-annotator agreement.
6. **The LLM-judge gate measures model–model agreement.** `souq_evals judge` may grade new pairs only
   after Cohen's kappa ≥ 0.6 over ≥ 50 reference pairs (`souq_evals.kappa.judge_gate`). It is calibrated
   against Claude's labels (`--reference claude`), because fewer than 50 maintainer labels exist, so a
   passing kappa shows the judge agrees with another model. It does not show either one agrees with
   shoppers.
7. **Filter-parse cases are a development set.** They were written alongside the rule parser, so the
   rule-only score is optimistic. In full mode with the committed replay fixtures the LLM answers are
   hand-authored (`recorded_at: synthetic`), so rule + LLM numbers measure the plumbing, not the model,
   until fixtures are recorded from the live API.
8. **The corpus is small and easy.** The 5,000 distractors are random, so most are easy negatives, and an
   8.4k-doc index is easier for Recall@100 than the 51,001-doc production index. Hard negatives are only
   the judged Irrelevant/Complement items.

## Thresholds, gates and targets

There are three kinds of number with different purposes.

| Kind | Where | What it is | Enforced by |
|---|---|---|---|
| Regression floors | `evals/thresholds.yaml` | ~3 nDCG points below the first clean offline run (2026-10-13, bge-m3 int8 + mMiniLM int8, `rerank_mode=rrf`: all .458, en .587, ar .328, arabizi_mixed .459). Floors: all nDCG@10 0.43 / Recall@100 0.60 / zero-result ≤ 0.02; en 0.56 / 0.72; ar 0.30; arabizi_mixed 0.43; filter-parse exact 0.85 | harness exit code 1 |
| Relative gate | `ci/eval_compare.py` | nDCG@10 of the production ablation may not drop more than 1.0 point vs the main-branch report on any language slice (`en`, `ar`, `arabizi_mixed`). `all` is shown but not gated, because a regression in one language is exactly what a pooled number averages away | CI (`eval.yml`) |
| Product targets | README "Targets" (from the plan) | nDCG@10 lift over `bm25` ≥ +5 points (en) and ≥ +10 points (ar); Recall@100 ≥ 0.90; zero-result rate ≤ 2 %; filter-parse exact match ≥ 92 % | reported, not gated: `ci/readme_report.py` renders target vs measured |

The floors were taken from measurement so CI catches regressions from where the system is today. The
product targets were set before any measurement and say where it should be. A green CI run does not mean
the targets are met.

When the `ar` and `arabizi_mixed` labels change (provisional → Claude), those slices' numbers move for
reasons unrelated to the code. Re-baseline the floors and the main-branch report in the PR that imports
the labels, and say so in its description.
