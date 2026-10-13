# Reranker test, 2026-10-05

Does a cross-encoder reranker improve on EmbeddingGemma-300m alone? Tested on two public BEIR datasets,
**SciFact** and **NFCorpus**, 150 seeded test queries each (seed 7). The datasets are not included here;
download them from the BEIR project.

Method:

1. `first_stage.py <dataset>`: embed the corpus with EmbeddingGemma Q8_0 (llama.cpp server on `:7999`, with
   the model's task prefixes), take the top 50 per query, and write `cands.json`. Prints nDCG@10 and recall@50
   for the embedder alone.
2. `rerank.py <model> <dataset>`: rerank those same 50 candidates with one model and print nDCG@10 and cost.
   `RR_DEVICE=cuda` runs on a GPU, and `RR_VRAM_FRACTION` caps its allocator so it can share a card with
   the main model.

Both scripts expect the data under `/work/data/<dataset>/` (BEIR layout), as in the container they ran in.

## Results (nDCG@10, GPU, bf16)

| Model | SciFact | NFCorpus | Change vs embedder only | p50 s/query | Peak VRAM |
|---|---:|---:|---|---:|---:|
| EmbeddingGemma only | 77.57 | 40.71 | — | — | — |
| bge-reranker-v2-m3 | 74.24 | 36.71 | **−3.3 / −4.0** | 0.5 | 1.4-1.7 GB |
| qwen3-reranker-0.6b | 78.90 | 41.68 | +1.3 / +1.0 | 1.2-1.3 | 2.0 GB |
| jina-reranker-v3.5 | 77.71 | 42.00 | +0.1 / +1.3 | 1.1-1.7 | 1.5 GB |
| mxbai-rerank-base-v2 | 79.12 | 40.08 | +1.6 / −0.6 | 0.7-1.2 | 2.5-2.8 GB |

Recall@50 of the first stage: 94.3 (SciFact), 32.8 (NFCorpus).

## Conclusion

The reranker this stack was running (bge-reranker-v2-m3) made results **worse** on both datasets. The best
alternatives gained at most about 1.5 points. That is within what 150 queries can resolve, and it costs
0.5-1.7 s per query plus 1.5-2.8 GB of VRAM on a card the main model needs. So the reranker was removed
and Open WebUI hybrid search turned off.

Limits: two small academic datasets, not this stack's own documents. A reranker could still help a corpus
where the first stage is weak. Measure on your own data before deciding either way.
