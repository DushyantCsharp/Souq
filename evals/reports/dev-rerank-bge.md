# Eval report `dev-rerank-bge` (offline, unknown)

Corpus 8354 docs (index `souq_eval`), embedder `bge-m3`, reranker `bge-reranker-v2-m3`, budget scale x50.0.

| slice | ablation | nDCG@10 | Recall@100 | MRR | zero-result | n |
|---|---|---|---|---|---|---|
| ar | rrf_rerank | 0.3527 | 0.4949 | 0.5743 | 0.0167 | 60 |
| arabizi_mixed | rrf_rerank | 0.5011 | 0.6850 | 0.7165 | 0.0000 | 30 |
| en | rrf_rerank | 0.6154 | 0.7613 | 0.8248 | 0.0000 | 60 |
| all | rrf_rerank | 0.4874 | 0.6395 | 0.7029 | 0.0067 | 150 |

Filter parse (rule): exact match 0.9000 over 80 cases; per field {"price": 0.9375, "currency": 0.9375, "brand": 1.0, "colour": 1.0, "size": 1.0, "category": 1.0}

| stage | p50 ms | p95 ms |
|---|---|---|
| normalise | 0.12 | 0.56 |
| parse | 0.37 | 1.03 |
| embed | 91.56 | 581.95 |
| bm25 | 102.77 | 711.01 |
| knn | 192.0 | 2198.5 |
| fuse | 0.22 | 1.25 |
| rerank | 976.42 | 1747.94 |
| total | 1512.94 | 3245.17 |

LLM: path share 0.0, avg tokens 0.0, USD per 1k queries 0.0 (enabled: False)

Degraded responses per ablation: `{"rrf_rerank": {"knn": 4}}`

Labels: `{"ar": {"provisional": 1215, "provisional_share": 1.0}, "arabizi_mixed": {"esci": 212, "provisional": 391, "provisional_share": 0.6484}, "en": {"esci": 1553, "provisional_share": 0.0}}`

**Warnings**
- ar: 100% of judgments are provisional (translated from ESCI, not yet hand-labelled by the maintainer) — treat this slice's numbers as indicative
- arabizi_mixed: 65% of judgments are provisional (translated from ESCI, not yet hand-labelled by the maintainer) — treat this slice's numbers as indicative
