# qwen-queue

Bounded queue for background work on the local Qwen (llama-swap). Design and rules: the docstring at the top
of [`qwenq.py`](qwenq.py). Worker: `systemctl --user status qwen-queue` (unit symlinked from this folder).
CLI: `~/bin/qwenq` (symlink). DB: `~/.local/share/qwen-queue/queue.db`.

    qwenq submit --kind test --prompt "Say hi" --wait     # or a JSON chat body on stdin / --file
    qwenq list | get <id> | cancel <id> | stats [--hours 24]
    journalctl --user -u qwen-queue -f

Tested 2026-09-30: completes a job; waits while live callers use the model (106 s behind a live agent run); never loads
a model that isn't already loaded (a Gemma job expired instead of evicting Qwen); cancel mid-run frees the
slot in ~2 s; a deadline stops a running job; a job waiting on an unloaded model doesn't block others.
Live callers (agent jobs, Open WebUI, Home Assistant voice) bypass the queue. As of 2026-10-26 the nightly
check's explanation is the only regular job kind; an earlier shadow-ensemble experiment (~10k jobs) stopped
2026-10-07.
