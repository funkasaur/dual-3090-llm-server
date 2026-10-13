# First stage: EmbeddingGemma (llama.cpp server on :7999) -> top-50 candidates per sampled query.
# Writes data/<ds>/cands.json: {qid: [doc ids]} for 150 seeded queries that have test qrels.
import json, math, random, sys, time, urllib.request
import numpy as np

ds = sys.argv[1]
D = f"/work/data/{ds}"
N_Q, TOP = 150, 50
corpus = [json.loads(l) for l in open(f"{D}/corpus.jsonl")]
queries = {d["_id"]: d["text"] for d in map(json.loads, open(f"{D}/queries.jsonl"))}
qrels = {}
for l in list(open(f"{D}/qrels/test.tsv"))[1:]:
    q, d, s = l.rstrip("\n").split("\t")
    if int(s) > 0:
        qrels.setdefault(q, {})[d] = int(s)
qids = sorted(q for q in qrels if q in queries)
random.Random(7).shuffle(qids)
qids = qids[:N_Q]


def emb(texts):
    b = json.dumps({"input": texts}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:7999/v1/embeddings", data=b, headers={"Content-Type": "application/json"}), timeout=900))
    v = np.array([d["embedding"] for d in sorted(r["data"], key=lambda d: d["index"])], dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


t0 = time.time()
docs = [f"title: {d.get('title') or 'none'} | text: {d['text'][:5000]}" for d in corpus]
Dm = np.vstack([emb(docs[i:i + 4]) for i in range(0, len(docs), 4)])
Q = np.vstack([emb([f"task: search result | query: {queries[q]}" for q in qids[i:i + 8]]) for i in range(0, len(qids), 8)])
ids = [d["_id"] for d in corpus]
cands, nd, rec = {}, [], []
for qi, q in enumerate(qids):
    top = np.argsort(-(Dm @ Q[qi]))[:TOP]
    cands[q] = [ids[j] for j in top]
    rel = qrels[q]
    dcg = sum(rel.get(ids[j], 0) / math.log2(r + 2) for r, j in enumerate(top[:10]))
    ideal = sorted(rel.values(), reverse=True)[:10]
    nd.append(dcg / sum(g / math.log2(r + 2) for r, g in enumerate(ideal)))
    rec.append(sum(1 for j in top if ids[j] in rel) / len(rel))
json.dump({"qids": qids, "cands": cands}, open(f"{D}/cands.json", "w"))
print(json.dumps({"dataset": ds, "model": "embeddinggemma-only", "queries": len(qids),
                  "ndcg10": round(100 * float(np.mean(nd)), 2), "recall50": round(100 * float(np.mean(rec)), 2),
                  "first_stage_s": round(time.time() - t0)}))
