# Eval report `20261014T050146Z-full` (full, unknown)

Corpus 8354 docs (index `souq_eval`), embedder `bge-m3`, reranker `mmarco-mMiniLMv2-L12-H384-v1`, budget scale x50.0.

| slice | ablation | nDCG@10 | Recall@100 | MRR | zero-result | n |
|---|---|---|---|---|---|---|
| ar | bm25 | 0.0682 | 0.0207 | 0.2167 | 0.6000 | 60 |
| ar | knn | 0.3427 | 0.5041 | 0.5677 | 0.0000 | 60 |
| ar | rrf | 0.3428 | 0.5041 | 0.5657 | 0.0000 | 60 |
| ar | rrf_rerank | 0.3279 | 0.5041 | 0.5709 | 0.0000 | 60 |
| ar | rrf_rerank_llm | 0.3288 | 0.5082 | 0.5709 | 0.0000 | 60 |
| arabizi_mixed | bm25 | 0.2655 | 0.3455 | 0.4661 | 0.3000 | 30 |
| arabizi_mixed | knn | 0.4695 | 0.6934 | 0.6916 | 0.0000 | 30 |
| arabizi_mixed | rrf | 0.4675 | 0.6850 | 0.6638 | 0.0000 | 30 |
| arabizi_mixed | rrf_rerank | 0.4589 | 0.6850 | 0.5951 | 0.0000 | 30 |
| arabizi_mixed | rrf_rerank_llm | 0.4589 | 0.6850 | 0.5951 | 0.0000 | 30 |
| en | bm25 | 0.5595 | 0.5955 | 0.7835 | 0.0000 | 60 |
| en | knn | 0.5578 | 0.7462 | 0.7930 | 0.0000 | 60 |
| en | rrf | 0.5888 | 0.7631 | 0.7553 | 0.0000 | 60 |
| en | rrf_rerank | 0.5873 | 0.7631 | 0.7640 | 0.0000 | 60 |
| en | rrf_rerank_llm | 0.5873 | 0.7631 | 0.7640 | 0.0000 | 60 |
| all | bm25 | 0.3042 | 0.3156 | 0.4933 | 0.3000 | 150 |
| all | knn | 0.4541 | 0.6388 | 0.6826 | 0.0000 | 150 |
| all | rrf | 0.4662 | 0.6439 | 0.6612 | 0.0000 | 150 |
| all | rrf_rerank | 0.4579 | 0.6439 | 0.6530 | 0.0000 | 150 |
| all | rrf_rerank_llm | 0.4582 | 0.6456 | 0.6530 | 0.0000 | 150 |

Filter parse (rule+llm): exact match 1.0000 over 80 cases; per field {"price": 1.0, "currency": 1.0, "brand": 1.0, "colour": 1.0, "size": 1.0, "category": 1.0}
Rule parser alone: exact match 0.9000.

| stage | p50 ms | p95 ms |
|---|---|---|
| normalise | 0.04 | 0.07 |
| parse | 0.12 | 0.21 |
| bm25 | 11.35 | 29.88 |
| embed | 9.03 | 13.35 |
| knn | 15.29 | 25.11 |
| fuse | 0.06 | 0.11 |
| rerank | 74.23 | 100.2 |
| total | 100.73 | 137.9 |

LLM: path share 0.0067, avg tokens 450.0, USD per 1k queries 0.000436 (enabled: True)

Degraded responses per ablation: `{"bm25": {}, "knn": {}, "rrf": {}, "rrf_rerank": {}, "rrf_rerank_llm": {}}`

Labels: `{"ar": {"provisional": 1215, "provisional_share": 1.0}, "arabizi_mixed": {"esci": 212, "provisional": 391, "provisional_share": 0.6484}, "en": {"esci": 1553, "provisional_share": 0.0}}`

**Warnings**
- ar: 100% of judgments are provisional (translated from ESCI, not yet hand-labelled by the maintainer) — treat this slice's numbers as indicative
- arabizi_mixed: 65% of judgments are provisional (translated from ESCI, not yet hand-labelled by the maintainer) — treat this slice's numbers as indicative
- LLM path replayed hand-authored synthetic fixtures: llm and rule+llm numbers are not a measurement of the live model
