# llamAmpere build for the dual-3090 box

`Qwen3.8-27B-Q8-Medium-Reason` in llama-swap runs this build of
[JakeATX/llamAmpere](https://github.com/JakeATX/llamAmpere) (a llama.cpp fork with SM86 attention
kernels) instead of ik_llama. The ik_llama entry is kept as `Qwen3.8-27B-Q8-Medium-Reason-ik`.

| | |
|---|---|
| Upstream commit | `2cb16936b` (main, 2026-09-19; includes v0.3.1 and the p/q RNG reseed `207867db`) |
| Local patches | `patches/0001-*` checkpoint size fix, `patches/0002-*` reasoning_effort precedence |
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

## Evidence it is safe

- **Numerics.** Identical token ids into both engines, next-token top-20 compared at 38 positions (short
  prompt and the real 108k request): same top token 38/38, symmetric KL mean 0.0005 / 0.0012, max 0.0057.
  That is the same order as Q8_0's own error against BF16 (0.0009).
- **Silent "wedge" (upstream issue #1).** 20-minute stress, 181 requests, 0 failures: two real sessions
  interleaved (32k-144k), exact repeats with new sampling, continuations carrying `reasoning_content`,
  mid-stream drops, pre-token cancels, queued cancels, 152 RAM-cache restores, 8 evictions. Processed prompt
  size equalled the independently rendered size on every request. The watchdog (below) covers the rest.
- **Patch 0001** (checkpoints): a forced checkpoint restore gave output byte-identical to a cold computation
  and to the unpatched behaviour, with unchanged draft acceptance. Checkpoints went 375 MiB -> 150 MiB at 107k.
- **Patch 0002** (effort): with the server default `medium`, a request carrying top-level `medium` plus
  `chat_template_kwargs` `xhigh` (what Hermes sends when an agent asks for xhigh) now renders xhigh; unpatched it
  silently rendered medium.

## Patches

**0001 — no draft snapshot in prompt checkpoints.** Every prompt checkpoint called
`update_dft(PARTIAL_ONLY)` on the MTP draft context, but `llama_kv_cache::state_write` ignores that flag,
so each checkpoint carried the draft layer's whole KV cache (2,176 B/token: 383 MiB at 184k). It is never
needed: restore already truncates both contexts with `slot.mem.seq_rm()`. Skipped when the draft cache is
partially truncatable. `LLAMA_CKPT_DFT_FULL=1` restores the old behaviour. Mainline llama.cpp has the same
code (`server-context.cpp`, `update_dft(... PARTIAL_ONLY)`), so this is an upstream candidate.

**0002 — `chat_template_kwargs.reasoning_effort` wins over top-level `reasoning_effort`.** Upstream applies
the top-level field last. Hermes sends top-level `"reasoning_effort": "medium"` on every request, which would
override an agent's requested `xhigh`. ik_llama ignores the top-level field; this keeps that behaviour for
clients that set the kwargs, and a top-level field on its own still applies.

## Operating notes

- `--tensor-split 49.5,50.5` at 262144 context: ~22.1 / 23.9 GB, peak unchanged through a 245k cold prefill.
  The split moves in ~1.9 GB steps; 50/50 leaves only ~0.8 GB on GPU0, where the reranker lives.
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
