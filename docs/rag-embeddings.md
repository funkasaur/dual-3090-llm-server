# RAG embeddings: Infinity/nomic → EmbeddingGemma

On 2026-10-05 Open WebUI's embedder moved from Infinity (`nomic-embed-text-v1.5` on CPU, plus
`bge-reranker-v2-m3` on GPU 0) to **EmbeddingGemma-300m Q8_0 on llama.cpp** (`docker/gemma-embed/`), and the
reranker was removed. Tested on Open WebUI 0.11.3.

| | Before | After |
|---|---|---|
| Embedder | Infinity, nomic-embed-text-v1.5, CPU | llama.cpp `llama-server --embedding`, EmbeddingGemma-300m Q8_0, CPU (4 threads, 2 GB cap) |
| Reranker | bge-reranker-v2-m3 on GPU 0 (~1.7 GB VRAM, grows with request size) | none ([it lowered nDCG@10](reranker-test/README.md)) |
| Dimensions | 768 | 768 (same size, **different space**: every vector must be rebuilt) |
| RAM | stopping Infinity freed ~9 GB | ~135 MB resident at idle (2 GB cap) |

Same dimensions is the trap: nothing errors if old and new vectors are mixed in one collection. Retrieval just
gets quietly worse. Plan the migration as a full re-embed and check it per tenant.

## Prefixes

EmbeddingGemma is trained with task prefixes. Open WebUI (0.11.x) only takes them from the environment:

```yaml
- "RAG_EMBEDDING_QUERY_PREFIX=task: search result | query: "
- "RAG_EMBEDDING_CONTENT_PREFIX=title: none | text: "
```

Quote the whole item. Unquoted, the `|` is a YAML error and the trailing space is lost.

## Migration

1. **Back up first:** a `pg_dump` of the Open WebUI database and a Qdrant snapshot of each collection you will
   touch (`docs/backups.md`).
2. **Start `gemma-embed`** and check it returns 768-dim vectors from inside the Open WebUI container's network
   (`curl http://gemma-embed:7997/v1/embeddings -d '{"input":"x"}'`).
3. **Point Open WebUI at it:** `RAG_EMBEDDING_OPENAI_API_BASE_URL=http://gemma-embed:7997/v1`, the model
   name, the two prefixes. Remove the reranker variables. Then also check **Admin → Documents**: settings saved
   there live in the database and override the environment. Clear the reranker engine, URL and model there (or
   with `POST /api/v1/retrieval/config/update`). With hybrid search off they are unused, but turning hybrid
   search on later would call a reranker that no longer exists.
4. **Reindex knowledge bases** (Admin → Documents → Reindex). This covers **knowledge bases only**.
5. **Re-embed chat-attached files separately.** Reindex leaves them on the old vectors, silently.
   `POST /api/v1/retrieval/process/file` returns early when a file already has chunks, so for each file:
   save its Qdrant points (backup), delete them, then call `process/file`.
6. **Other tenants:** anything else writing to the same Qdrant with the old embedder (for example a mem0
   memory store) needs its own config change and re-embed. mem0 has no prefix option, so it embeds plain text
   both ways.
7. **Verify per tenant, not per collection:** for a sample of points, re-embed the stored text with the new
   model (plus the right prefix) and compare cosine similarity with the stored vector. Expect ≥0.999. Old
   vectors score far lower.
8. Stop Infinity once nothing calls it (watch its logs for a day).

If CPU re-embedding is slow (≈3 chunks/s here), run a temporary second `gemma-embed` on a GPU for the bulk job
(35-60 chunks/s) and remove it afterwards.

## Tika 4

`apache/tika:latest-full` moved from Tika 3 to 4 without notice on 2026-09-03. Open WebUI defaults to the
Tika 3 API, so every PDF/docx/xlsx upload from then on failed. The only symptom was files with 0 chunks. Fix:
pin the image (`docker/tika/`) and set `TIKA_SERVER_VERSION=4` (or Admin → Documents → Tika server version).

## Rollback

1. Restore the Open WebUI compose file and the database dump (or put the old URL, model and reranker settings
   back in Admin → Documents).
2. Start Infinity again (`docker/infinity/`).
3. Restore the Qdrant snapshots taken in step 1. Re-embedding back to nomic is the slow alternative.
