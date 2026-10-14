# Eval report `20261014T050118Z-offline` (offline, unknown)

Corpus 8354 docs (index `souq_eval`), embedder `bge-m3`, reranker `mmarco-mMiniLMv2-L12-H384-v1`, budget scale x50.0.

| slice | ablation | nDCG@10 | Recall@100 | MRR | zero-result | n |
|---|---|---|---|---|---|---|
| ar | bm25 | 0.0682 | 0.0207 | 0.2167 | 0.6000 | 60 |
| ar | knn | 0.3427 | 0.5041 | 0.5677 | 0.0000 | 60 |
| ar | rrf | 0.3428 | 0.5041 | 0.5657 | 0.0000 | 60 |
| ar | rrf_rerank | 0.3279 | 0.5041 | 0.5709 | 0.0000 | 60 |
| arabizi_mixed | bm25 | 0.2655 | 0.3455 | 0.4661 | 0.3000 | 30 |
| arabizi_mixed | knn | 0.4695 | 0.6934 | 0.6916 | 0.0000 | 30 |
| arabizi_mixed | rrf | 0.4675 | 0.6850 | 0.6638 | 0.0000 | 30 |
| arabizi_mixed | rrf_rerank | 0.4589 | 0.6850 | 0.5951 | 0.0000 | 30 |
| en | bm25 | 0.5595 | 0.5955 | 0.7835 | 0.0000 | 60 |
| en | knn | 0.5578 | 0.7462 | 0.7930 | 0.0000 | 60 |
| en | rrf | 0.5888 | 0.7631 | 0.7553 | 0.0000 | 60 |
| en | rrf_rerank | 0.5873 | 0.7631 | 0.7640 | 0.0000 | 60 |
| all | bm25 | 0.3042 | 0.3156 | 0.4933 | 0.3000 | 150 |
| all | knn | 0.4541 | 0.6388 | 0.6826 | 0.0000 | 150 |
| all | rrf | 0.4662 | 0.6439 | 0.6612 | 0.0000 | 150 |
| all | rrf_rerank | 0.4579 | 0.6439 | 0.6530 | 0.0000 | 150 |

Filter parse (rule): exact match 0.9000 over 80 cases; per field {"price": 0.9375, "currency": 0.9375, "brand": 1.0, "colour": 1.0, "size": 1.0, "category": 1.0}

| stage | p50 ms | p95 ms |
|---|---|---|
| normalise | 0.04 | 0.11 |
| parse | 0.14 | 0.34 |
| embed | 9.54 | 18.52 |
| bm25 | 15.22 | 35.19 |
| knn | 17.23 | 32.56 |
| fuse | 0.07 | 0.13 |
| rerank | 80.95 | 126.01 |
| total | 112.54 | 171.74 |

LLM: path share 0.0, avg tokens 0.0, USD per 1k queries 0.0 (enabled: False)

Degraded responses per ablation: `{"bm25": {}, "knn": {}, "rrf": {}, "rrf_rerank": {}}`

Labels: `{"ar": {"provisional": 1215, "provisional_share": 1.0}, "arabizi_mixed": {"esci": 212, "provisional": 391, "provisional_share": 0.6484}, "en": {"esci": 1553, "provisional_share": 0.0}}`

**Warnings**
- ar: 100% of judgments are provisional (translated from ESCI, not yet hand-labelled by the maintainer) — treat this slice's numbers as indicative
- arabizi_mixed: 65% of judgments are provisional (translated from ESCI, not yet hand-labelled by the maintainer) — treat this slice's numbers as indicative
