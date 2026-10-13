# Rerank the first-stage top-50 with one model; prints nDCG@10 and CPU cost per query.
import json, math, os, resource, sys, time

import torch

torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "4")))
# RR_DEVICE=cuda runs on the one visible GPU; RR_VRAM_FRACTION caps this process's allocator (share of total VRAM).
DEV = os.environ.get("RR_DEVICE", "cpu")
if DEV == "cuda":
    torch.cuda.set_per_process_memory_fraction(float(os.environ.get("RR_VRAM_FRACTION", "0.083")), 0)
name, ds = sys.argv[1], sys.argv[2]
D = f"/work/data/{ds}"
corpus = {d["_id"]: d for d in map(json.loads, open(f"{D}/corpus.jsonl"))}
queries = {d["_id"]: d["text"] for d in map(json.loads, open(f"{D}/queries.jsonl"))}
qrels = {}
for l in list(open(f"{D}/qrels/test.tsv"))[1:]:
    q, d, s = l.rstrip("\n").split("\t")
    if int(s) > 0:
        qrels.setdefault(q, {})[d] = int(s)
c = json.load(open(f"{D}/cands.json"))
qids, cands = c["qids"], c["cands"]


def text(did):
    d = corpus[did]
    return (f"{d.get('title') or ''}\n{d['text']}").strip()[:5000]


DT = torch.bfloat16
if name == "bge-reranker-v2-m3":
    from sentence_transformers import CrossEncoder
    m = CrossEncoder("BAAI/bge-reranker-v2-m3", max_length=1024, device=DEV, model_kwargs={"torch_dtype": DT})
    score = lambda q, docs: list(m.predict([(q, x) for x in docs], batch_size=8))
elif name == "qwen3-reranker-0.6b":
    from sentence_transformers import CrossEncoder
    m = CrossEncoder("Qwen/Qwen3-Reranker-0.6B", max_length=1024, device=DEV, model_kwargs={"torch_dtype": DT})
    score = lambda q, docs: list(m.predict([(q, x) for x in docs], batch_size=4))  # 8 overran a 2 GB GPU cap
elif name == "jina-reranker-v3.5":
    from transformers import AutoModel
    m = AutoModel.from_pretrained("jinaai/jina-reranker-v3.5", dtype=DT, trust_remote_code=True).eval().to(DEV)
    if os.environ.get("RR_JINA_BLOCK"):  # GPU: score the 50 docs in blocks of this many tokens, not one 60k-token pass
        m._ensure_tokenizer(); m._tokenizer.model_max_length = int(os.environ["RR_JINA_BLOCK"])

    def score(q, docs):
        s = [0.0] * len(docs)
        for r in m.rerank(q, docs):
            s[r["index"]] = float(r["relevance_score"])
        return s
elif name == "mxbai-rerank-base-v2":
    from mxbai_rerank import MxbaiRerankV2
    m = MxbaiRerankV2("mixedbread-ai/mxbai-rerank-base-v2", torch_dtype=DT, device=DEV)

    def score(q, docs):
        s = [0.0] * len(docs)
        for r in m.rank(q, docs, return_documents=False, top_k=len(docs), batch_size=2):  # 8 built a 2.5 GB logits block
            s[r.index] = float(r.score)
        return s
else:
    raise SystemExit(f"unknown model {name}")

nd, per_q = [], []
cpu0, wall0 = time.process_time(), time.time()
with torch.inference_mode():
    for q in qids:
        docs = [text(d) for d in cands[q]]
        a = time.time()
        s = score(queries[q], docs)
        per_q.append(time.time() - a)
        order = [cands[q][i] for i in sorted(range(len(docs)), key=lambda i: -s[i])]
        rel = qrels[q]
        dcg = sum(rel.get(d, 0) / math.log2(r + 2) for r, d in enumerate(order[:10]))
        ideal = sorted(rel.values(), reverse=True)[:10]
        nd.append(dcg / sum(g / math.log2(r + 2) for r, g in enumerate(ideal)))
per_q.sort()
print(json.dumps({"dataset": ds, "model": name, "queries": len(qids), "ndcg10": round(100 * sum(nd) / len(nd), 2),
                  "s_per_query_p50": round(per_q[len(per_q) // 2], 2), "s_per_query_p95": round(per_q[int(.95 * len(per_q))], 2),
                  "cpu_s_per_query": round((time.process_time() - cpu0) / len(qids), 2),
                  "peak_rss_gb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 2),
                  "wall_s": round(time.time() - wall0), "device": DEV,
                  "peak_vram_gb": round(torch.cuda.max_memory_reserved() / 1e9, 2) if DEV == "cuda" else None}))
