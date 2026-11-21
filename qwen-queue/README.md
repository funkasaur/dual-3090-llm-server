# qwen-queue (moved)

The background queue for the local Qwen now lives in its own repository:
[funkasaur/codasaur-queue](https://github.com/funkasaur/codasaur-queue) (CLI `cdq`, settings `CDQ_*`).

It does what the old copy here did (one job at a time, only when the model is already loaded and idle, an hourly
budget, deadlines, cancel) plus what was added since 2026-10-26: GPU-temperature and UPS holds, per-job run caps and
Prometheus metrics. Live callers (agent jobs, Open WebUI, Home Assistant voice)
still bypass it.

The 2026-10-26 version (`qwenq.py`, `qwen-queue.service`) is in this repository's history.
