# Host scripts

These run on the host, not in a container. Install with:

```bash
sudo install -m 0755 scripts/*.sh /usr/local/bin/
sudo install -m 0644 systemd/*    /etc/systemd/system/
sudo systemctl daemon-reload
```

| Script | Unit | Purpose |
|---|---|---|
| `optimize-ai.sh` | `optimize-ai.service` + `.timer` | Pin GPU interrupts off the inference cores; report immovable ones |
| `set-gpu-power.sh` | `nvidia-power-limit.service` | Persistence mode + per-GPU power cap |
| `backup.sh` | `borg-backup.service` + `.timer` | Nightly Borg archive with Postgres/Qdrant pre-hooks |

All three are idempotent and safe to re-run.

## Before enabling

Every script has tunables at the top, overridable from the environment or the
unit file. **The defaults assume a 16-core dual-CCD Ryzen** — read
[`../docs/cpu-topology.md`](../docs/cpu-topology.md) and derive your own core
numbers before enabling anything.

`backup.sh` additionally needs a passphrase file before it will run at all:

```bash
sudo install -d -m 0700 /etc/borg
printf '%s' 'your-passphrase' | sudo tee /etc/borg/passphrase >/dev/null
sudo chmod 600 /etc/borg/passphrase
```

## Dry runs

```bash
sudo POWER_LIMIT_W=300 set-gpu-power.sh     # override the cap for one run
sudo optimize-ai.sh                         # prints what it pinned and what it couldn't
sudo systemctl start borg-backup.service && journalctl -fu borg-backup
```

## What changed from the originals

Each script carries a `PATCH:` comment where behaviour differs from the
version that was running on the source machine. The reasoning for every change
is in [`../docs/AUDIT.md`](../docs/AUDIT.md) — the short version is that the
IRQ script had not successfully run in two months, and the backup script had
several ways to fail without saying so.
