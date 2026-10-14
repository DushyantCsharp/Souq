# Eval report `dev-rerank-rrf` (offline, unknown)

Corpus 8354 docs (index `souq_eval`), embedder `bge-m3`, reranker `mmarco-mMiniLMv2-L12-H384-v1`, budget scale x50.0.

| slice | ablation | nDCG@10 | Recall@100 | MRR | zero-result | n |
|---|---|---|---|---|---|---|
| ar | rrf_rerank | 0.3279 | 0.5041 | 0.5709 | 0.0000 | 60 |
| arabizi_mixed | rrf_rerank | 0.4589 | 0.6850 | 0.5951 | 0.0000 | 30 |
| en | rrf_rerank | 0.5873 | 0.7631 | 0.7640 | 0.0000 | 60 |
| all | rrf_rerank | 0.4579 | 0.6439 | 0.6530 | 0.0000 | 150 |

Filter parse (rule): exact match 0.9000 over 80 cases; per field {"price": 0.9375, "currency": 0.9375, "brand": 1.0, "colour": 1.0, "size": 1.0, "category": 1.0}

| stage | p50 ms | p95 ms |
|---|---|---|
| normalise | 0.09 | 0.21 |
| parse | 0.34 | 1.03 |
| embed | 30.95 | 110.63 |
| bm25 | 58.24 | 249.27 |
| knn | 64.92 | 395.67 |
| fuse | 0.19 | 0.45 |
| rerank | 199.15 | 473.47 |
| total | 315.19 | 910.32 |

LLM: path share 0.0, avg tokens 0.0, USD per 1k queries 0.0 (enabled: False)

Degraded responses per ablation: `{"rrf_rerank": {}}`

Labels: `{"ar": {"provisional": 1215, "provisional_share": 1.0}, "arabizi_mixed": {"esci": 212, "provisional": 391, "provisional_share": 0.6484}, "en": {"esci": 1553, "provisional_share": 0.0}}`

**Warnings**
- ar: 100% of judgments are provisional (translated from ESCI, not yet hand-labelled by the maintainer) — treat this slice's numbers as indicative
- arabizi_mixed: 65% of judgments are provisional (translated from ESCI, not yet hand-labelled by the maintainer) — treat this slice's numbers as indicative
