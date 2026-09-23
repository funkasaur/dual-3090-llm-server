# Backups

Two independent layers, which is deliberate:

- **Borg** — nightly, automated, application-consistent. This is the one that
  runs every day and the one you will actually restore from.
- **Veeam Agent for Linux** — image-level backup of the host for bare-metal
  restore.

They answer different questions. Borg answers *"what did this database look
like on Tuesday?"* Veeam answers *"the boot drive died, make the machine exist
again."* Neither substitutes for the other.

> The prose description of this build credited Veeam with the whole pipeline.
> The nightly automated job is Borg; Veeam runs alongside it. See
> [AUDIT #8](AUDIT.md#8-documentation-drift).

## Why pre-hooks are not optional

Copying a running database's files is not a backup. Postgres may have pages
written out of order relative to WAL; Qdrant may be mid-segment-write. The
files will copy fine and restore into something that may or may not open.

So [`scripts/backup.sh`](../scripts/backup.sh) takes consistent dumps *first*,
then archives those:

```
pg_dumpall  ->  /var/backup/postgres/
Qdrant snapshot API  ->  inside the Qdrant volume
                     ->  borg create
```

The live Qdrant storage directory is **excluded** from the archive. Archiving
it would reintroduce exactly the inconsistency the snapshots exist to prevent.

## The passphrase

Never in the script. The original had it inline — which meant it was also
archived *into the repository it protects*, so anyone who could read a backup
could decrypt all of them.

```bash
sudo install -d -m 0700 /etc/borg
printf '%s' 'your-passphrase' | sudo tee /etc/borg/passphrase >/dev/null
sudo chmod 600 /etc/borg/passphrase
```

Store a copy somewhere that is not this machine. A Borg repository without its
passphrase is indistinguishable from random data — there is no recovery path.

## Failure modes this script guards against

Every one of these was a real gap ([AUDIT #6](AUDIT.md#6-the-backup-script-had-several-quiet-failure-modes)):

| Guard | Without it |
|---|---|
| Dump to `.partial`, rename on success | A failed dump leaves a truncated file with a fresh timestamp that looks like a valid backup |
| `borg info` before doing any work | An unmounted `/mnt/storage` means Borg initialises onto the root filesystem instead |
| `RequiresMountsFor=/mnt/storage` | Same, at the systemd layer |
| Retention on Postgres dumps | Dated dumps accumulate forever and get re-archived nightly |
| Qdrant snapshot pruning | Qdrant never prunes its own snapshots; the storage volume grows without bound |
| `EXIT` trap logging status | Under `set -e` the log's last line is whatever step was in progress — success and failure look identical |

The unit also runs pinned to CCD1 with `IOSchedulingClass=idle` and `Nice=19`,
so a 3am archive run cannot compete with an inference request.

## Schedule

```ini
[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=15min
Persistent=true
```

`Persistent=true` runs a missed backup on next boot, which matters on a machine
that is occasionally down at 3am.

## Retention

```bash
borg prune --keep-daily 7 --keep-weekly 4 --keep-monthly 6
```

Then `borg compact`, without which pruning frees no actual disk space.

## Restoring

Practice this before you need it.

```bash
export BORG_PASSPHRASE="$(sudo cat /etc/borg/passphrase)"
export BORG_REPO=/mnt/storage/borg/system

borg list                                    # archives
borg list ::system-2026-09-23_03:00          # contents of one

# Single file, into the current directory
borg extract ::system-2026-09-23_03:00 home/user/docker/open-webui/.env

# Browse an archive as a filesystem — the best way to check a backup is real
mkdir /mnt/restore && borg mount ::system-2026-09-23_03:00 /mnt/restore
```

Restoring Postgres from the logical dump:

```bash
docker exec -i webui-postgres psql -U webui_admin < pg_dumpall_2026-09-23.sql
```

Restoring a Qdrant collection goes through the snapshot API, not by copying
files back:

```bash
curl -X PUT 'http://localhost:6333/collections/<name>/snapshots/recover' \
  -H 'Content-Type: application/json' \
  -d '{"location":"file:///qdrant/storage/snapshots/<name>/<snapshot>.snapshot"}'
```

## Verify it

A backup you have never restored is a hypothesis.

```bash
borg check --verify-data     # full integrity check; slow, run monthly
borg list | tail -5          # did last night actually happen?
grep -c 'Backup FAILED' /var/log/borg-backup.log
```

Consider a `systemd` `OnFailure=` hook on `borg-backup.service` to notify you,
rather than relying on noticing. The whole theme of
[the audit](AUDIT.md) is that silent failures stay silent for months.
