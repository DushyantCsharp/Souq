# Eval report `dev-offline-2` (offline, unknown)

Corpus 8354 docs (index `souq_eval`), embedder `bge-m3`, reranker `mmarco-mMiniLMv2-L12-H384-v1`, budget scale x10.0.

| slice | ablation | nDCG@10 | Recall@100 | MRR | zero-result | n |
|---|---|---|---|---|---|---|
| ar | bm25 | 0.0682 | 0.0207 | 0.2167 | 0.6000 | 60 |
| ar | knn | 0.3427 | 0.5041 | 0.5677 | 0.0000 | 60 |
| ar | rrf | 0.3428 | 0.5041 | 0.5657 | 0.0000 | 60 |
| ar | rrf_rerank | 0.2306 | 0.3461 | 0.4478 | 0.2333 | 60 |
| arabizi_mixed | bm25 | 0.2655 | 0.3455 | 0.4661 | 0.3000 | 30 |
| arabizi_mixed | knn | 0.4647 | 0.6639 | 0.6864 | 0.0667 | 30 |
| arabizi_mixed | rrf | 0.4675 | 0.6825 | 0.6633 | 0.0333 | 30 |
| arabizi_mixed | rrf_rerank | 0.4236 | 0.6428 | 0.5754 | 0.1000 | 30 |
| en | bm25 | 0.5595 | 0.5955 | 0.7835 | 0.0000 | 60 |
| en | knn | 0.5578 | 0.7462 | 0.7930 | 0.0000 | 60 |
| en | rrf | 0.5814 | 0.7454 | 0.7469 | 0.0167 | 60 |
| en | rrf_rerank | 0.5516 | 0.6482 | 0.7526 | 0.0833 | 60 |
| all | bm25 | 0.3042 | 0.3156 | 0.4933 | 0.3000 | 150 |
| all | knn | 0.4531 | 0.6329 | 0.6816 | 0.0133 | 150 |
| all | rrf | 0.4632 | 0.6363 | 0.6577 | 0.0133 | 150 |
| all | rrf_rerank | 0.3976 | 0.5263 | 0.5953 | 0.1467 | 150 |

Filter parse (rule): exact match 0.9000 over 80 cases; per field {"price": 0.9375, "currency": 0.9375, "brand": 1.0, "colour": 1.0, "size": 1.0, "category": 1.0}

| stage | p50 ms | p95 ms |
|---|---|---|
| normalise | 0.09 | 0.89 |
| parse | 0.37 | 1.14 |
| embed | 0.05 | 0.21 |
| knn | 189.57 | 608.13 |
| bm25 | 72.97 | 563.44 |
| fuse | 0.17 | 0.82 |
| rerank | 209.43 | 660.27 |
| total | 436.59 | 1175.42 |

LLM: path share 0.0, avg tokens 0.0, USD per 1k queries 0.0 (enabled: False)

Degraded responses per ablation: `{"bm25": {}, "knn": {"unavailable": 2}, "rrf": {"unavailable": 1, "knn": 2}, "rrf_rerank": {"knn": 39, "unavailable": 6}}`

Labels: `{"ar": {"provisional": 1215, "provisional_share": 1.0}, "arabizi_mixed": {"esci": 212, "provisional": 391, "provisional_share": 0.6484}, "en": {"esci": 1553, "provisional_share": 0.0}}`

**Warnings**
- ar: 100% of judgments are provisional (translated from ESCI, not yet hand-labelled by the maintainer) — treat this slice's numbers as indicative
- arabizi_mixed: 65% of judgments are provisional (translated from ESCI, not yet hand-labelled by the maintainer) — treat this slice's numbers as indicative
