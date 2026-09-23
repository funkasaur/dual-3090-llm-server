# The inference engine

## Two pieces, not one

- **`llama-swap`** — the router. One OpenAI-compatible endpoint on port 3060.
  It reads a model catalogue, launches the right backend process when a
  request names a model, proxies to it, and unloads it after a TTL. Nothing
  in the frontend needs to know which model is resident.
- **`ik-llama-server`** — the backend that actually runs the tensors. From
  [`ik_llama.cpp`](https://github.com/ikawrakow/ik_llama.cpp), a fork of
  llama.cpp with additional quantisation types, MTP speculative decoding, and
  the `graph` split mode used here. Plain `llama-server` will reject several
  of the flags below.

This split is worth having. Swapping between a 27B for real work and a 2B for
title generation is a config entry rather than a redeploy, and idle models
give their VRAM back automatically.

## Splitting across two 3090s

```
-sm, --split-mode SPLIT_MODE  how to split the model across multiple GPUs:
    none:  use one GPU only
    graph: split model tensors and computation graph across GPUs
    layer: split layers and KV across GPUs   (default)
```
*(from `ik-llama-server --help`)*

The default `layer` mode gives each card a contiguous range of layers. It
works over plain PCIe because traffic between cards is just the activation
tensor handed from one layer range to the next — but only one GPU is busy at a
time.

`graph` mode splits the tensors themselves and the computation graph across
both devices, so both cards work on the same layer concurrently. That is
tensor parallelism, and it means far more inter-GPU traffic per token — which
is why it wants NVLink. On this machine:

```
$ nvidia-smi nvlink -s
GPU 0: 4 links @ 14.062 GB/s
GPU 1: 4 links @ 14.062 GB/s
```

~56 GB/s aggregate between the cards, versus PCIe 4.0 x16's ~32 GB/s
theoretical — and, more importantly, without crossing the CPU's root complex.

Related tuning flags in this build:

| Flag | Effect |
|---|---|
| `-smf16` / `-smf32` | Precision of inter-GPU exchange. f16 halves the traffic. |
| `-grt`, `--graph-reduce-type` | Type used for the cross-GPU reduction. |
| `-gap`, `--graph-attn-precision` | Flash-attention precision under `-sm graph`. |
| `-sas`, `--scheduler-async` | Async compute-graph evaluation; overlaps work across devices. |

**Without NVLink, benchmark before assuming `graph` wins.** Over PCIe alone the
reduction traffic can cost more than the parallelism gains, and `layer` mode
is often faster. Measure both on your own hardware.

## Measured throughput

27B dense at Q8_0, both cards, `--split-mode graph`, 262k context:

| | |
|---|---|
| Generation | **~46 tok/s** |
| Prompt processing | **~1118 tok/s** |

Read live from the llama-swap metrics endpoint:

```bash
curl -s localhost:9105/metrics | grep -E 'tokens_per_second|prompt_per_second'
```

Prompt processing being ~24x generation speed is the expected shape: prefill is
a compute-bound batched matmul across the whole prompt, while generation is
memory-bandwidth-bound and produces one token per full pass over the weights.
It is also why the threading is asymmetric.

## Threading

```
--threads 5
--threads-batch 10
```

Sized to the inference zone — five physical cores (0-4) and their SMT siblings
(16-20):

- **`--threads 5`** for generation. Latency-bound, one thread per physical
  core, no SMT. Two threads contending for one core's execution units adds
  latency without adding throughput here.
- **`--threads-batch 10`** for prompt processing. Throughput-bound and does
  benefit from SMT, so it uses all ten logical CPUs.

This only works if the container was given all ten. `cpuset: "0-4"` is five
logical CPUs and silently oversubscribes the batch phase 2:1 — see
[AUDIT #2](AUDIT.md#2-cpuset-0-4-was-allocating-half-the-intended-cpu).

Supporting environment:

```yaml
OMP_NUM_THREADS=5
OMP_PROC_BIND=close   # keep threads near each other, in-die
OMP_PLACES=cores      # bind to cores, not hardware threads
```

## Memory and context

```
--ctx-size 262144
--cache-type-k q8_0
--cache-type-v q8_0
--ctx-checkpoints 64
--flash-attn on
```

KV cache at f16 becomes the dominant VRAM consumer long before the weights do
at long context. Quantising both K and V to `q8_0` roughly halves it for very
little measurable quality cost, and is what makes a 262k window fit alongside
27B of Q8 weights in 48GB.

`--flash-attn on` is not optional at this context length — the attention
matrix materialised naively at 262k tokens does not fit in any consumer card.

`--ctx-checkpoints 64` keeps recomputation cheap when the context is edited or
branched, which matters a lot in a chat UI where users regenerate.

## Container configuration

```yaml
ipc: host          # required for CUDA IPC / P2P between the two cards
shm_size: "16gb"   # default 64MB shared memory will not survive this workload
cap_add: [IPC_LOCK]  # lets the engine mlock weights so they are never swapped
```

`ipc: host` is the one people miss. Without it, CUDA peer-to-peer between the
GPUs fails inside the container and `graph` mode falls back to something much
slower, or refuses outright.

## Model lifecycle

```yaml
globalTTL: 300           # unload an idle model after 5 minutes
healthCheckTimeout: 180  # allow 3 minutes for a large model to load
```

Per model, `ttl: 0` pins it in VRAM permanently. Use that for the one model you
actually work in, so you never pay reload cost; leave short TTLs on utility
models that only serve occasional calls.

`healthCheckTimeout` needs to be generous. A 27B Q8 loading across two cards
takes well over a minute from cold page cache, and too tight a timeout makes
llama-swap kill and retry a backend that was loading perfectly well.

## Verifying

```bash
# Which model is resident, and is it healthy
curl -s localhost:3060/v1/models | jq

# Both cards should be busy during generation under -sm graph
nvidia-smi dmon -s um

# Confirm the pinning held
docker inspect -f '{{.HostConfig.CpusetCpus}}' llama-swap-unified
```

If `nvidia-smi dmon` shows only one card working during generation, `graph`
mode is not actually in effect — check for a fallback warning in the container
logs.
