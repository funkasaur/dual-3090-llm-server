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

> **Mount the snapshots directory, or the exclusion above silently removes
> Qdrant from your backups entirely.** Qdrant writes to `/qdrant/snapshots`,
> which is *not* covered by a `/qdrant/storage` mount — by default it lands in
> the container's writable layer, where no host backup can see it and a
> `docker compose down` destroys it. This stack had 370 snapshots and 5.6 GB
> stranded that way ([AUDIT #12](AUDIT.md#12-borgs-retention-works-its-pre-hooks-never-clean-up-after-themselves)).
> The Compose file now mounts `./qdrant_snapshots:/qdrant/snapshots`, and
> `backup.sh` warns if that directory is missing or empty.

### Qdrant has no snapshot retention of its own

Worth knowing before you go looking for a setting: self-hosted Qdrant has
**no built-in snapshot cleanup**. Its `snapshots_config` only chooses where
snapshots go (`local` or `s3`), not how long they live. Retention exists only
in Qdrant Cloud ("Days of Retention") and in Private Cloud / Kubernetes via a
`retention` field on `QdrantClusterScheduledSnapshot`.

So for a Docker deployment, pruning through the API — what `backup.sh` does —
is the only option. Left alone, snapshots accumulate forever.

### Pin the Qdrant image version

Qdrant guarantees storage compatibility across **one** minor version only, so
upgrades must step through each minor in turn: `1.17.x -> 1.18.x -> 1.19.x`.
Skipping is unsupported.

That makes `image: qdrant/qdrant:latest` genuinely dangerous on a stateful
deployment: a routine `docker compose pull` can carry you across two minors in
a single step. Pin the tag, bump it one minor at a time, and take a snapshot
before each step.

Verify from the destination, not the source — the only question that matters
is whether the snapshots are *in* an archive:

```bash
borg list ::system-2026-09-23_03:00 | grep -c snapshot
```

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

> **Note the topology.** `/mnt/storage` is a dedicated NVMe, separate from the
> OS drive, so a failed boot disk leaves the image backups intact — which is
> the case bare-metal restore is for. But *both* backup systems live on that
> one drive (Borg 92 GB, Veeam 1.3 TB), so they share a single failure domain
> despite being chosen to fail independently. It is a good local restore tier,
> not an off-site copy. See
> [AUDIT #10a](AUDIT.md#10a-both-backup-systems-share-one-failure-domain).

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

## Keeping both honest

A backup you have never restored is a hypothesis.

### Watch the repository's free space

This is what actually broke the Veeam layer here
([AUDIT #10](AUDIT.md#10-the-veeam-job-had-not-run-in-three-days)). A synthetic
full is built by merging the existing chain into a **new** full file, which must
exist alongside the old one before anything can be pruned. So the volume needs
roughly one full backup's worth of free space, permanently, on top of the chain
itself:

```
chain on disk:  625 GB + 700 GB fulls + incrementals  ≈ 1.3 TB
free space:     405 GB
new full needs: ~700 GB                               -> ERROR_DISK_FULL
```

Retention policy and volume size have to be sized together. A repository that
fits the chain exactly will fail the first time it tries to make a synthetic
full, and the failure arrives as a job error rather than a capacity warning.

Alert on it before it bites:

```bash
# Warn under 1.5x the size of your largest full
df -h /mnt/storage
ls -lS /mnt/storage/<repo>/**/*.vbk | head -1
```

### Check both are still producing

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
