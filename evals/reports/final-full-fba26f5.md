# Eval report `20261014T075812Z-full` (full, fba26f5b52)

Corpus 8781 docs (index `souq_eval`), embedder `bge-m3`, reranker `mmarco-mMiniLMv2-L12-H384-v1`, budget scale x50.0.

| slice | ablation | nDCG@10 | Recall@100 | MRR | zero-result | n |
|---|---|---|---|---|---|---|
| ar | bm25 | 0.2364 | 0.0894 | 0.5283 | 0.2333 | 60 |
| ar | knn | 0.4436 | 0.5666 | 0.7397 | 0.0000 | 60 |
| ar | rrf | 0.4663 | 0.5729 | 0.7525 | 0.0000 | 60 |
| ar | rrf_rerank | 0.4572 | 0.5729 | 0.7330 | 0.0000 | 60 |
| ar | rrf_rerank_llm | 0.4596 | 0.5775 | 0.7330 | 0.0000 | 60 |
| arabizi_mixed | bm25 | 0.3606 | 0.3641 | 0.6056 | 0.2667 | 30 |
| arabizi_mixed | knn | 0.5821 | 0.7164 | 0.7897 | 0.0000 | 30 |
| arabizi_mixed | rrf | 0.5810 | 0.7186 | 0.7794 | 0.0000 | 30 |
| arabizi_mixed | rrf_rerank | 0.5879 | 0.7186 | 0.7794 | 0.0000 | 30 |
| arabizi_mixed | rrf_rerank_llm | 0.5879 | 0.7186 | 0.7794 | 0.0000 | 30 |
| en | bm25 | 0.5522 | 0.5955 | 0.7626 | 0.0000 | 60 |
| en | knn | 0.5611 | 0.7560 | 0.7817 | 0.0000 | 60 |
| en | rrf | 0.5952 | 0.7766 | 0.7853 | 0.0000 | 60 |
| en | rrf_rerank | 0.6102 | 0.7766 | 0.8442 | 0.0000 | 60 |
| en | rrf_rerank_llm | 0.6102 | 0.7766 | 0.8442 | 0.0000 | 60 |
| all | bm25 | 0.3875 | 0.3468 | 0.6375 | 0.1467 | 150 |
| all | knn | 0.5183 | 0.6723 | 0.7665 | 0.0000 | 150 |
| all | rrf | 0.5408 | 0.6835 | 0.7710 | 0.0000 | 150 |
| all | rrf_rerank | 0.5445 | 0.6835 | 0.7868 | 0.0000 | 150 |
| all | rrf_rerank_llm | 0.5455 | 0.6854 | 0.7868 | 0.0000 | 150 |

Filter parse (rule+llm): exact match 1.0000 over 80 cases; per field {"price": 1.0, "currency": 1.0, "brand": 1.0, "colour": 1.0, "size": 1.0, "category": 1.0}
Rule parser alone: exact match 0.9000.

| stage | p50 ms | p95 ms |
|---|---|---|
| normalise | 0.04 | 0.05 |
| parse | 0.12 | 0.19 |
| bm25 | 11.74 | 27.2 |
| embed | 9.43 | 16.87 |
| knn | 18.64 | 28.18 |
| fuse | 0.07 | 0.11 |
| rerank | 88.02 | 165.92 |
| total | 119.45 | 210.39 |

LLM: path share 0.0067, avg tokens 1398.0, USD per 1k queries 0.0011 (enabled: True)

Degraded responses per ablation: `{"bm25": {}, "knn": {}, "rrf": {}, "rrf_rerank": {}, "rrf_rerank_llm": {}}`

Labels: `{"ar": {"claude": 2078, "provisional_share": 0.0, "claude_share": 1.0}, "arabizi_mixed": {"esci": 212, "claude": 707, "provisional_share": 0.0, "claude_share": 0.7693}, "en": {"esci": 1553, "provisional_share": 0.0, "claude_share": 0.0}}`

**Warnings**
- ar: 100% of judgments were graded by Claude (a model) at the maintainer's request, not by a human — they measure agreement with a model's judgement
- arabizi_mixed: 77% of judgments were graded by Claude (a model) at the maintainer's request, not by a human — they measure agreement with a model's judgement
