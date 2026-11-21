# Swift 1.5 vs Qwen3.8-27B (unsloth Q8_0), 2026-11-10

Since 2026-11-10 the main entry (`Qwen3.8-27B-Q8-Medium-Reason`) loads ukisai's
[Swift 1.5](https://huggingface.co/ukisai/Swift-1.5-Qwen3.8-27B-GGUF) Q8_0 (Qwen3.8-27B post-trained for shorter
reasoning; 29.0 GB, MTP head included) instead of unsloth's `Qwen3.8-27B-Q8_0.gguf`. Same engine and flags
(llamAmpere cand-0006, `--split-mode tensor`, `--tensor-split 50,50`, MTP n-max 3, q8_0 KV, medium effort,
reasoning budget 24576).

**Verdict:** equal quality and speed; Swift uses fewer tokens on long analysis and ~8% fewer on short reasoning.
The model card claims 58.5% fewer thinking tokens; at medium effort with a 24k budget the gain here is much smaller.

## Quality (2 seeds per arm, blocks A/B/B/A)

| | Qwen3.8-27B | Swift 1.5 |
|---|---|---|
| math/logic (10 items, exact answer) | 20/20 | 20/20 |
| Python functions vs unit tests (4) | 8/8 | 8/8 |
| format following (2) | 4/4 | 4/4 |
| voice tool calls, thinking off (4) | 6/8 | 6/8 |
| tokens math / code / format | 14,485 / 5,773 / 1,162 | 13,363 / 5,690 / 1,122 |

Both arms miss the same tool-call item on both seeds ("who won the game last night" should call web search; both
answer without a tool), so that is base-model behavior.

## Speed (rounds A, B, A, B; 3 warm requests per size per round)

| Context | Qwen3.8-27B t/s (se) | Swift 1.5 t/s (se) | MTP accept |
|---|---|---|---|
| 33k | 101.8 (0.5) | 102.6 (0.8) | 0.71 / 0.72 |
| 108k | 91.1 (1.0) | 89.2 (0.4) | 0.69 / 0.67 |

## Real workload (2 seeds)

| Job | Qwen3.8-27B tokens | Swift 1.5 tokens |
|---|---|---|
| Alert digest on a day's alerts (8.8k-token input) | 2,877 / 3,114 | 2,814 / 4,149 |
| Log digest of 2,000 Grafana log lines (236k-token input) | 2,190 / **6,000** (hit the cap, `finish=length`) | 1,935 / 1,888 |

Same findings in every pair. The long-input digest is where Swift helps: the base model ran into the output cap once.

## How it was switched

- Only the live entry's `--model` path changed. The entry name stays, so every client keeps working.
- **Own disk prompt cache.** The entry got its own `--cache-disk-path` (16 GiB limit). KV cached from other weights
  must never be restored into this model; a shared cache dir would load the old model's KV on a prefix hit.
- Rollback: model path back to the unsloth file and drop the two `--cache-disk-*` lines (the old cache dir is kept).
- Verified after the switch through the router, a queued background job and the voice proxy (114 t/s, MTP 43/51
  accepted on the first direct request).
