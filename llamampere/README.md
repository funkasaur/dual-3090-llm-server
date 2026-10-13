# llamAmpere build for the dual-3090 box

`Qwen3.8-27B-Q8-Medium-Reason` in llama-swap runs this build of
[JakeATX/llamAmpere](https://github.com/JakeATX/llamAmpere) (a llama.cpp fork with SM86 attention
kernels) instead of ik_llama. The ik_llama entry is kept as `Qwen3.8-27B-Q8-Medium-Reason-ik`.

| | |
|---|---|
| Upstream commit | `d5b0ba7` (tag v0.5.2, 2026-10-11; since 2026-10-11, before that `2cb16936b`) |
| Local patches | `patches/0002-*` reasoning_effort precedence, `patches/0003-*` CUDA graph cache cap from `GGML_CUDA_MAX_GRAPHS` (old 0001 is upstream as `dc2b130`) |
| Built by | `./build.sh` (CUDA 12.9.1 devel, sm_86, NCCL, `-Wl,--allow-shlib-undefined`) |
| Runs in | `llama-swap-unified`, mounted at `/opt/llamampere` (see [`docker/llama-swap/docker-compose.yaml`](../docker/llama-swap/docker-compose.yaml)) |
| Model | unsloth `Qwen3.8-27B-Q8_0.gguf`, unchanged |

## Why

Measured 2026-09-25/26 on the same GGUF, same sampling, replaying real captured agent requests:

| Context | ik_llama (graph split) | llamAmpere (tensor split) |
|---|---|---|
| 33k | 43-48 t/s | 64-68 t/s |
| 108k | 34.5-35.4 | 57.4-57.7 |
| 150k | 34.4-38.6 | 60.4-66.2 |
| 184k | 30.5-32.1 | 59.5-60.6 |
| ~240k | 23.5-24.0 | 50.2-51.0 |

Prefill is +29-40%. Both engines use one CPU core while decoding; llamAmpere keeps both GPUs ~90% busy
(ik: 72% / 85%). The gain is per-verification-pass cost at depth: ik's pass grows ~0.27 ms per 1k tokens of
context, llamAmpere's ~0.05 ms.

## v0.5.2 (2026-10-11)

Same GGUF, same flags, same prompts, against the previous build (`2cb16936b` + 0001 + 0002):

| Gate | Previous | v0.5.2 + 0002 |
|---|---|---|
| Numerics (identical token ids) | — | short 20/20 same top-1, KL 0.0000; 108k 8/8 top-1, sym-KL mean 0.0010, max 0.0044 |
| Decode t/s at 33k / 108k / 150k / 240k | 77.6 / 68.9 / 61.2 / 58.3 | 84.2 / 75.9 / 67.4 / 56.2 (240k cold −4%, warm +10%) |
| Prefill | unchanged | unchanged |
| VRAM per card | — | −1.6 GB |
| 10-min stress (switches, continuations, repeats, drops, cancels) | 28 ops, 0 failed | 28 ops, 0 failed |

## Patch 0003: CUDA graph cache cap (2026-10-13)

ggml-cuda caches at most 64 CUDA graphs per card (LRU, `max_cuda_graphs` in `common.cuh`). With
`--split-mode tensor` and an MTP drafter, each decode round runs ~130 graph segments per card (one per stretch
between all-reduces), so every graph is evicted before it is used again and nearly every kernel launches
eagerly. The patch reads the cap from `GGML_CUDA_MAX_GRAPHS` (unset or ≤0 = 64, upstream behaviour); the
llama-swap entry sets 512.

| Check | Cap 64 | Cap 512 |
|---|---|---|
| Graph evictions (cold + warm 33k request) | 240,844 | 0 |
| Graph replays | 18,552 | 252,638 |
| Decode t/s 33k / 108k / 150k (10 warm requests per cell) | 84.5 / 74.4 / 69.2 | 88.2 / 77.4 / 71.8 (+3.8 to +4.4%) |
| Numerics | — | 20/20 and 8/8 top-1, sym-KL 0.0000 (bit-identical) |
| VRAM per card | — | +84 MiB |
| Stress | 35 ops, 0 failed | 35 ops, 0 failed |

## Evidence it is safe (original switch, 2026-09-26)

- **Numerics.** Identical token ids into both engines, next-token top-20 compared at 38 positions (short
  prompt and the real 108k request): same top token 38/38, symmetric KL mean 0.0005 / 0.0012, max 0.0057.
  That is the same order as Q8_0's own error against BF16 (0.0009).
- **Silent "wedge" (upstream issue #1).** 20-minute stress, 181 requests, 0 failures: two real sessions
  interleaved (32k-144k), exact repeats with new sampling, continuations carrying `reasoning_content`,
  mid-stream drops, pre-token cancels, queued cancels, 152 RAM-cache restores, 8 evictions. Processed prompt
  size equalled the independently rendered size on every request. The watchdog (below) covers the rest.
- **Patch 0001** (checkpoints, now upstream): a forced checkpoint restore gave output byte-identical to a cold
  computation and to the unpatched behaviour, with unchanged draft acceptance. Checkpoints went 375 MiB -> 150 MiB at 107k.
- **Patch 0002** (effort): with the server default `medium`, a request carrying top-level `medium` plus
  `chat_template_kwargs` `xhigh` (what Hermes sends when an agent asks for xhigh) now renders xhigh; unpatched it
  silently rendered medium.

## Patches

**0001 — no draft snapshot in prompt checkpoints** (upstream since `dc2b130`, so dropped from `patches/` at v0.5.2). Every prompt checkpoint called
`update_dft(PARTIAL_ONLY)` on the MTP draft context, but `llama_kv_cache::state_write` ignores that flag,
so each checkpoint carried the draft layer's whole KV cache (2,176 B/token: 383 MiB at 184k). It is never
needed: restore already truncates both contexts with `slot.mem.seq_rm()`. Skipped when the draft cache is
partially truncatable. `LLAMA_CKPT_DFT_FULL=1` restores the old behaviour. Mainline llama.cpp has the same
code (`server-context.cpp`, `update_dft(... PARTIAL_ONLY)`), so this is an upstream candidate.

**0002 — `chat_template_kwargs.reasoning_effort` wins over top-level `reasoning_effort`.** Upstream applies
the top-level field last. Hermes sends top-level `"reasoning_effort": "medium"` on every request, which would
override an agent's requested `xhigh`. ik_llama ignores the top-level field; this keeps that behaviour for
clients that set the kwargs, and a top-level field on its own still applies.

**0003 — `GGML_CUDA_MAX_GRAPHS`.** See above. Without the variable the build behaves exactly like upstream.

## Operating notes

- `--tensor-split 50,50` at 262144 context (since 2026-10-13): ~20.5 / 20.5 GB on v0.5.2, flat through a 240k
  request. The old 49.5,50.5 left room for a reranker on GPU0 but put a weight step more on GPU1, so GPU0
  waited at every sync; 50,50 is +5-6% decode at 108-150k. See [docs/inference.md](../docs/inference.md).
- `--cache-ram 32768`: a ~130k session is ~7 GB with patch 0001, so this holds ~4 long sessions before an
  eviction forces a cold re-prefill (~2 min at 130k). Switching away from a long session costs up to ~6 s to
  save its state.
- Draft-accepted tokens report logprob 0 (upstream reporting gap). Vision is not part of this entry.
- `/completion` with `n_probs` works; the ik entry returns HTTP 500 on partial-UTF-8 tokens in `n_probs`.

## Watchdog

[`scripts/llm-watchdog.sh`](../scripts/llm-watchdog.sh) with
[`systemd/llm-watchdog.{service,timer}`](../systemd/) — root timer every 60 s: if the model is loaded,
`/tokenize` a fixed string and expect 6 tokens; two consecutive failures unload the model through
llama-swap. Metrics go to node-exporter's textfile collector; the Grafana rules `InferenceEngineWedged`
and `LlmWatchdogStale` (in [`grafana/alert-rules.json`](../grafana/alert-rules.json)) deliver to Telegram.
The failure path was tested end to end, including the Telegram message.

## Rollback

In llama-swap's `config.yaml`, swap the two keys back (`Qwen3.8-27B-Q8-Medium-Reason` <-> `...-ik`) or restore
`config.yaml.bak-pre-llamampere-*`. llama-swap reloads on save. Agent clients that record a model version
can tag rows with the engine to keep the two separable.

## Rebuilding

`./build.sh` rebuilds from the pinned commit plus `patches/`. The previous `bin/` is kept as `bin.old`. To move
to a newer upstream, change `COMMIT`, rebuild, and re-run the numerical and stress checks before switching.
