# Backups

Two independent layers, both active, answering different questions:

| | Borg | Veeam |
|---|---|---|
| Scope | Files: `/etc`, `/home`, `/opt`, `/root`, dumps | Whole-machine image |
| Runs | Nightly 03:00, on the host via systemd | From a Windows Veeam B&R server over SMB |
| Consistency | Logical dumps taken first (pre-hooks) | Containers stopped around the snapshot |
| Answers | *"What did this database look like on Tuesday?"* | *"The boot drive died — make the machine exist again."* |
| Restore granularity | Single file, in seconds | Bare metal |

Neither substitutes for the other. Borg cannot rebuild a dead boot drive;
Veeam is a clumsy way to retrieve one `.env` from last week.

> **Check that both are actually running.** These are the two most
> independently-failing things in the stack, and a stalled backup job looks
> exactly like a working one from the inside. See
> [AUDIT #10](AUDIT.md#10-the-veeam-job-had-not-run-in-three-days).

---

## Layer 1 — Borg (file-level, nightly)

Driven by `borg-backup.timer` → `borg-backup.service` →
[`scripts/backup.sh`](../scripts/backup.sh).

### Why pre-hooks are not optional

Copying a running database's files is not a backup. Postgres may have pages
written out of order relative to WAL; Qdrant may be mid-segment-write. The
files copy fine and restore into something that may or may not open.

So the script takes consistent dumps *first*, then archives those:

```
pg_dumpall           ->  /var/backup/postgres/
Qdrant snapshot API  ->  inside the Qdrant volume
                     ->  borg create
```

The live Qdrant storage directory is **excluded** from the archive. Archiving
it would reintroduce exactly the inconsistency the snapshots exist to prevent.

### The passphrase

Never in the script. The original had it inline — which meant it was also
archived *into the repository it protects*, so anyone who could read a backup
could decrypt all of them.

```bash
sudo install -d -m 0700 /etc/borg
printf '%s' 'your-passphrase' | sudo tee /etc/borg/passphrase >/dev/null
sudo chmod 600 /etc/borg/passphrase
```

Store a copy somewhere that is not this machine. A Borg repository without its
passphrase is indistinguishable from random data.

### Failure modes this script guards against

Every one of these was a real gap ([AUDIT #6](AUDIT.md#6-the-backup-script-had-several-quiet-failure-modes)):

| Guard | Without it |
|---|---|
| Dump to `.partial`, rename on success | A failed dump leaves a truncated file with a fresh timestamp that looks valid |
| `borg info` before doing any work | An unmounted `/mnt/storage` means Borg initialises onto the root filesystem |
| `RequiresMountsFor=/mnt/storage` | Same, at the systemd layer |
| Retention on Postgres dumps | Dated dumps accumulate forever and get re-archived nightly |
| Qdrant snapshot pruning | Qdrant never prunes its own snapshots; the volume grows without bound |
| `EXIT` trap logging status | Under `set -e`, success and failure look identical in the log |

The unit runs pinned to CCD1 with `IOSchedulingClass=idle` and `Nice=19`, so a
3am archive cannot compete with an inference request.

### Retention and restore

```bash
borg prune --keep-daily 7 --keep-weekly 4 --keep-monthly 6
borg compact          # without this, pruning frees no actual disk space
```

```bash
export BORG_PASSPHRASE="$(sudo cat /etc/borg/passphrase)"
export BORG_REPO=/mnt/storage/borg/system

borg list                                     # archives
borg extract ::system-2026-09-23_03:00 home/user/docker/open-webui/.env

# Browse an archive as a filesystem — the best way to check a backup is real
mkdir /mnt/restore && borg mount ::system-2026-09-23_03:00 /mnt/restore
```

Postgres, from the logical dump:

```bash
docker exec -i webui-postgres psql -U webui_admin < pg_dumpall_2026-09-23.sql
```

Qdrant goes through the snapshot API, not by copying files back:

```bash
curl -X PUT 'http://localhost:6333/collections/<name>/snapshots/recover' \
  -H 'Content-Type: application/json' \
  -d '{"location":"file:///qdrant/storage/snapshots/<name>/<snapshot>.snapshot"}'
```

---

## Layer 2 — Veeam (image-level)

A Windows Veeam B&R server backs up this host over the network, writing into a
repository on an SMB share exported from the Linux box itself:

```ini
[WindowsShare]
   comment = Network Share for Windows Servers
   path = /mnt/storage
```

Backup chain shape: weekly full (`.vbk`) with daily incrementals (`.vib`).

> **Note the topology.** The Veeam repository lives on `/mnt/storage`, which is
> a filesystem *on the machine being backed up*. It is fine for a fast local
> restore, but it is not a second copy in any meaningful sense — one dead
> drive takes the server and its image backups together. If you only keep one
> off-box copy, make it this one.

### Freezing the stack around the snapshot

Veeam runs a **pre-freeze** script on the guest immediately before the
snapshot and a **post-thaw** script immediately after. This stack uses that
window to stop the three stateful containers outright, which is the bluntest
and most reliable way to get an application-consistent image.

Both are in [`veeam/`](../veeam/). **They are stored on the Veeam backup
server and executed on the Linux host** — the job wizard's `Browse` button
reads the backup server's filesystem, and at job runtime Veeam uploads the
scripts to `/var/lib/veeam/scripts` on the agent machine and runs them there
as root.

Copying them to `/usr/local/bin` on the Linux host achieves nothing; Veeam
never looks there.

Configure at: job → **Guest Processing** → **Enable application-aware
processing** → **Applications** → select the computer → **Edit** →
**Scripts** → the **pre-freeze / post-thaw** fields, not the pre-job/post-job
pair.

> The Veeam server is normally Windows, so keep **LF line endings**. A CRLF
> shebang fails with `bad interpreter: /bin/bash^M`, which aborts the job.

Full details and a test procedure in [`veeam/README.md`](../veeam/README.md).

Measured downtime on a real run: **~11 seconds**.

```
webui-postgres  stopped 08:00:30Z  ->  started 08:00:41Z
qdrant          stopped 08:00:30Z  ->  started 08:00:41Z
```

### What was patched

| | |
|---|---|
| **Stop order** | The original stopped `webui-postgres` *before* `open-webui`, so the UI spent the window erroring against a dead database. Dependents now stop first, and start last. |
| **Stop timeout** | Was the Docker default of 10s. SIGTERM triggers a Postgres fast shutdown that still has to finish a checkpoint; a SIGKILL partway through produces exactly the inconsistent data directory the stop was meant to avoid. Now `-t 60`, and the script warns if any container exits 137. |
| **Failsafe** | If the job died between freeze and thaw, the containers stayed down — `restart: unless-stopped` does **not** restart an explicitly stopped container. `pre-freeze.sh` now arms a `systemd-run` timer that thaws unconditionally after 30 minutes; `post-thaw.sh` cancels it on the happy path. |
| **Health verification** | `docker start` returning success only means the container was started, not that it works. `post-thaw.sh` now waits for health checks to settle, so a broken restore surfaces in the job log rather than the next time someone opens the UI. |
| **Logging** | Neither script logged anything. Both now write to `/var/log/veeam-freeze.log` with timings. |

### An alternative worth considering

Stopping containers is simple and correct, but it is not the only option. Veeam
Agent for Linux supports application-aware processing that can call
`pg_start_backup()` / `pg_stop_backup()` and handle WAL truncation without any
downtime. That is the better answer if the freeze window ever becomes a
problem — but at 11 seconds, it currently is not.

---

## Verify both

A backup you have never restored is a hypothesis.

```bash
# --- Borg ---
systemctl status borg-backup.service       # did last night succeed?
borg list | tail -5
borg check --verify-data                   # full integrity; slow, run monthly
grep -c 'Backup FAILED' /var/log/borg-backup.log

# --- Veeam ---
# Newest file here should be from the last scheduled run, not last week.
ls -lt '/mnt/storage/<repo>/<job name>/'*/*.vbk '/mnt/storage/<repo>/<job name>/'*/*.vib \
  2>/dev/null | head -5

# Did the freeze scripts actually run?
tail -20 /var/log/veeam-freeze.log

# Containers should be up, not left frozen
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'postgres|qdrant|open-webui'
```

Add a `systemd` `OnFailure=` hook to `borg-backup.service` and enable email
alerts on the Veeam job. The theme running through [the audit](AUDIT.md) is
that silent failures stay silent for months — and the backup layer is the
worst possible place to learn that lesson.
