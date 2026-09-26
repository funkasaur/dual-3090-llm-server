# The inference engine

## Three pieces

- **`llama-swap`** — the router. One OpenAI-compatible endpoint on port 3060.
  It reads a model catalogue, launches the right backend process when a
  request names a model, proxies to it, and unloads it after a TTL. Nothing
  in the frontend needs to know which model or engine is resident.
- **llamAmpere** — the backend for the main model (Qwen3.8-27B at Q8_0) since
  2026-09-26. [`JakeATX/llamAmpere`](https://github.com/JakeATX/llamAmpere) is a
  llama.cpp fork with attention kernels written for Ampere (sm_86). It is built
  here from a pinned commit plus two local patches — see
  [`llamampere/`](../llamampere/README.md) — and mounted into the llama-swap
  container.
- **`ik-llama-server`** — [`ik_llama.cpp`](https://github.com/ikawrakow/ik_llama.cpp),
  shipped inside the llama-swap image. It runs every other model in the
  catalogue, and the previous configuration of the main model is kept as a
  fallback entry (`...-ik`) that one config edit switches back to.

Swapping between a 27B for real work and a 2B for title generation is a config
entry rather than a redeploy, and idle models give their VRAM back
automatically. Swapping *engines* works the same way.

## Why the main model moved to llamAmpere

Measured on this machine, same GGUF, same sampling settings, replaying real
captured agent requests (tool schemas, long histories) against both engines:

| Context | ik_llama, `--split-mode graph` | llamAmpere, `--split-mode tensor` |
|---|---|---|
| 33k | 43-48 tok/s | 64-68 tok/s |
| 108k | 34.5-35.4 | 57.4-57.7 |
| 150k | 34.4-38.6 | 60.4-66.2 |
| 184k | 30.5-32.1 | 59.5-60.6 |
| ~240k | 23.5-24.0 | 50.2-51.0 |

Prefill is 29-40% faster too (1,732 vs 1,340 tok/s cold at 33k; 1,040 vs 780 at
184k).

The gap comes from the cost of each verification pass at depth, not from
speculation: both engines turn one pass into ~2.0-2.7 tokens. ik_llama's pass
grows by ~0.27 ms per 1k tokens of context (42 ms at 33k, 72-76 ms at 150k);
llamAmpere's by ~0.05 ms (35 ms to 41 ms). That growth is attention reading the
KV cache, which is what llamAmpere's fused kernels target. An agent that spends
most of its turns at 100k+ context is exactly where it shows.

Before switching, two things were checked, not assumed:

- **Numerical equivalence.** Identical token ids fed to both engines, the
  next-token top-20 distribution compared at 38 positions, including inside a
  real 108k request: same top token 38/38, symmetric KL mean 0.0005 (short) and
  0.0012 (108k), max 0.0057. That is the same order as Q8_0's own error against
  BF16 (~0.0009), i.e. two equally good computations of the same model.
- **The upstream "wedge" bug** (llamAmpere issue #1: after a prompt-cache
  restore the server processed every request as ~4-6 tokens and answered prompts
  it never saw, silently). A 20-minute stress of the reported trigger shapes —
  181 requests, two long sessions interleaved, exact repeats, continuations,
  dropped streams, cancelled and queued requests, 152 cache restores — produced
  0 failures. The watchdog below covers what a stress test cannot.

## Splitting across two 3090s

Both engines use tensor parallelism: each layer's weights are split, both cards
compute their half at once, and partial results are combined before the next
layer. That needs far more inter-GPU traffic per token than pipeline (`layer`)
splitting, which is why it wants NVLink:

```
$ nvidia-smi nvlink -s
GPU 0: 4 links @ 14.062 GB/s
GPU 1: 4 links @ 14.062 GB/s
```

~56 GB/s aggregate between the cards, without crossing the CPU's root complex.

| | ik_llama `graph` | llamAmpere `tensor` |
|---|---|---|
| What is split | each layer's compute graph | weights **and** KV cache, across a virtual combined device |
| Cross-GPU reduction | ik's own, f16 by default | NCCL all-reduce |
| MTP draft head | runs on one card | own buffers on each card |
| GPU busy while decoding | 72% / 85% (uneven) | 90% / 90% |
| Quantised KV | supported | supported (mainline llama.cpp refuses it in tensor mode; the fork lifted that) |
| Sampling | GPU | CPU (forced by tensor mode; measured, not a bottleneck) |

**`--tensor-split 49.5,50.5`.** With 262k context both cards are nearly full,
and GPU0 also hosts the reranker (~1.7 GB), whose memory grows with request
size. The split moves in coarse ~1.9 GB steps: 50/50 leaves ~0.8 GB free on
GPU0; 49.5/50.5 leaves ~2.5 GB on GPU0 and ~0.7 GB on GPU1 (47/53 runs out of
memory on GPU1). The engine reserves all of its own memory at startup, so the
headroom belongs on the card with the *other* tenant. Verified through a 245k
token cold prefill: peak 22.1 / 23.9 GB, no growth.

## Speculative decoding (MTP)

Qwen3.8 ships a multi-token-prediction head inside the GGUF, used as a built-in
draft model. Every drafted token is verified by the full model, so drafts
change speed, never output.

- **llamAmpere:** `--spec-type draft-mtp --spec-draft-n-max 3 --spec-draft-p-min 0`
  with exact p/q rejection sampling.
- **ik_llama:** `--spec-type mtp:n_max=4,p_min=0.8` — `p_min` stops drafting at
  the first low-confidence token, so `n_max` behaves adaptively.

Things measured here that are worth knowing:

- **`n_max` costs VRAM on this model.** The linear-attention layers carry a
  recurrent state that must be rolled back when drafts are rejected; ik_llama
  pre-allocates one snapshot per draft position (~120 MiB each). An ngram draft
  stage with `n_max=64` would need ~7.8 GB and does not fit.
- **`-mtprot iq4_ks` (ik_llama) did not help at long context.** It shrinks the
  draft pass, but drafting was only 7-9% of each step at 100k+; the verify pass
  dominates. It also re-quantises at every load (219 s on one core, 48 s on
  five) — if you keep it, bake it into the GGUF once with
  `llama-quantize --extra-output-tensor iq4_ks ... COPY`.
- **Draft-accepted tokens report logprob 0** on llamAmpere. Only a reporting gap.

## Reasoning effort

Qwen3.8's chat template renders `reasoning_effort` as the *first line of the
system prompt* (`xhigh` and `low` add an instruction; `medium` adds nothing).
Two consequences:

- **Only `chat_template_kwargs.reasoning_effort` reaches the template** on
  ik_llama. Clients set it per request; the server default is
  `--chat-template-kwargs '{"reasoning_effort":"medium"}'`. A top-level
  `reasoning_effort` and `reasoning_budget` are ignored there.
- **Changing it mid-conversation invalidates the whole prompt cache**, because
  the first tokens change. Pick one effort per session.

On llamAmpere (as upstream llama.cpp) a top-level `reasoning_effort` *overrides*
the kwargs. Hermes sends a top-level `"reasoning_effort": "medium"` on every
request, so an agent asking for `xhigh` via the kwargs would silently have run at
medium. Local patch 0002 makes the kwargs win when both are present — measured
end to end: the xhigh request renders exactly 38 tokens longer, identical to
ik_llama's behaviour. A top-level field on its own still applies.

## Threading

```
--threads 5
--threads-batch 5
```

The container gets `cpuset: "0-4"`: five physical cores, one hardware thread
each, SMT siblings (16-20) left to the host. That is a deliberate choice — see
[AUDIT #2](AUDIT.md#2-cpuset-0-4-was-allocating-half-the-intended-cpu) for the
history. With every layer on the GPUs, the CPU's hot path during generation is
**one thread**: measured on both engines, exactly one core at 100% (the thread
driving the GPUs, sampling, and spin-waiting) and every other thread idle. Wider
CPU allocations only matter for load-time and prompt-cache work.

```yaml
OMP_NUM_THREADS=5
OMP_PROC_BIND=false   # NOT close: see AUDIT #12
```

`OMP_PROC_BIND=close` with `OMP_PLACES=cores` pinned the *entire* server
process — all 44 threads — to core 0, because OpenMP binds the main thread to
the first place at library load and every later thread inherits that mask.
`OMP_PLACES` alone implies binding and does the same. See
[AUDIT #12](AUDIT.md#12-openmp-pinned-the-whole-inference-server-to-one-core).

## Memory and context

```
--ctx-size 262144
--cache-type-k q8_0 --cache-type-v q8_0
--flash-attn on
--ctx-checkpoints 16
--cache-ram 32768          # llamAmpere
```

KV cache at f16 becomes the dominant VRAM consumer long before the weights do
at long context. Only 16 of the 64 layers use attention (the other 48 are
linear attention with a constant-size state), so `q8_0` K/V at 262k is ~8.7 GB.

**Checkpoints and the host-RAM prompt cache.** Because the linear-attention
state cannot be truncated, resuming a conversation from anywhere but its end
needs a saved checkpoint of that state. `--cache-ram` holds whole sessions in
host RAM so switching between conversations restores in seconds instead of
re-prefilling (~2 minutes at 130k).

Upstream llamAmpere stored the MTP draft layer's *entire* KV cache in every
checkpoint (the KV cache's serializer ignores the "partial only" flag the
server passes), so checkpoints grew with context: 375 MiB at 107k, 537 MiB at
184k, and a 130k session filled ~10.7 GB of RAM cache. Local patch 0001 skips
that copy — restore already truncates the draft cache — and checkpoints are a
constant ~150 MiB. Verified: restored output byte-identical to a cold
computation and to the unpatched behaviour. With it, `--cache-ram 32768` holds
about four long sessions. Mainline llama.cpp has the same code path.

## Container configuration

```yaml
ipc: host          # required for CUDA IPC / P2P between the two cards
shm_size: "16gb"   # ignored under ipc: host; harmless
cap_add: [IPC_LOCK]  # only matters if --mlock is used
volumes:
  - ./llamampere/bin:/opt/llamampere:ro
```

`ipc: host` is the one people miss. Without it, CUDA peer-to-peer between the
GPUs fails inside the container and tensor parallelism falls back to something
much slower, or refuses outright.

The llama-swap image already contains everything the llamAmpere build links
against (CUDA 12.9 runtime, cuBLAS, NCCL, libgomp), so it runs inside the same
container. Its entry sets `LD_LIBRARY_PATH=/opt/llamampere:/usr/local/cuda/lib64`
— that replaces the container's value rather than appending, so the CUDA path
must be listed again.

## Model lifecycle

```yaml
globalTTL: 300           # unload an idle model after 5 minutes
healthCheckTimeout: 180  # allow 3 minutes for a large model to load
```

Per model, `ttl: 0` pins it in VRAM permanently. Use that for the one model you
actually work in; leave short TTLs on utility models. llamAmpere loads the 27B
Q8 in ~7 s from page cache; ik_llama took ~60 s with the load-time `-mtprot`
re-quantisation.

## The watchdog

A silently wedged server returns HTTP 200 with wrong answers, which no health
check catches. `llm-watchdog` (root timer, every 60 s) asks the loaded model to
tokenize a fixed string and expects 6 tokens; two consecutive failures unload
the model through llama-swap so the next request reloads it cleanly. It writes
metrics through node-exporter's textfile collector, and two Grafana rules
(`InferenceEngineWedged`, `LlmWatchdogStale`) deliver to Telegram. The failure
path was tested end to end, including the Telegram message. Files:
[`scripts/llm-watchdog.sh`](../scripts/llm-watchdog.sh),
[`systemd/llm-watchdog.*`](../systemd/).

## Verifying

```bash
# Which model is resident, which engine binary, and is it healthy
curl -s localhost:3060/running | jq '.running[] | {model, state, cmd: (.cmd | split(" ")[0])}'

# Both cards should be busy (~90%) during generation
nvidia-smi dmon -s um

# The server may use all five cores, not one (AUDIT #12)
docker exec llama-swap-unified sh -c 'grep Cpus_allowed_list /proc/$(pgrep -f llama-server | head -1)/status'

# Watchdog verdict
cat /var/lib/node_exporter/textfile_collector/llm_watchdog.prom

# Rolling back the main model to ik_llama: swap the two keys in config.yaml
#   Qwen3.8-27B-Q8-Medium-Reason  <->  Qwen3.8-27B-Q8-Medium-Reason-ik
```
