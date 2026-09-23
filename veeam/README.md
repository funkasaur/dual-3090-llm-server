# Veeam guest processing scripts

Veeam runs **pre-freeze** on the guest immediately before taking the snapshot,
and **post-thaw** immediately after. This stack uses that window to stop the
three stateful containers outright — the bluntest and most reliable way to get
an application-consistent image.

Measured downtime on a real run: **~11 seconds**.

## Install

These go on the **Linux guest being backed up**, not on the Veeam server:

```bash
sudo install -m 0755 pre-freeze.sh post-thaw.sh /usr/local/bin/
```

Then point the backup job's guest processing settings at
`/usr/local/bin/pre-freeze.sh` and `/usr/local/bin/post-thaw.sh`.

Veeam treats a non-zero exit from pre-freeze as a failed freeze and aborts the
job, which is the correct behaviour — an image taken over a half-stopped
database is worse than no image.

## Tunables

Both scripts read their settings from the environment:

| Variable | Default | Meaning |
|---|---|---|
| `STOP_TIMEOUT` | `60` | Seconds to allow for a clean container shutdown |
| `FAILSAFE_MINUTES` | `30` | Unconditional thaw if the job never completes |
| `HEALTH_TIMEOUT` | `120` | Seconds to wait for health checks after starting |
| `LOG` | `/var/log/veeam-freeze.log` | Shared log for both scripts |

## Test before trusting

Run them by hand, in order, and watch the log:

```bash
sudo /usr/local/bin/pre-freeze.sh; echo "exit=$?"
docker ps --format '{{.Names}}\t{{.Status}}'   # the three should be gone
sudo /usr/local/bin/post-thaw.sh;  echo "exit=$?"
tail -30 /var/log/veeam-freeze.log
```

Then test the failsafe, which is the part you most want to work and least want
to discover is broken:

```bash
sudo FAILSAFE_MINUTES=1 /usr/local/bin/pre-freeze.sh
systemctl list-timers veeam-failsafe-thaw.timer    # should be armed
# wait ~1 minute without running post-thaw
docker ps   # the containers should have come back on their own
```

## What was patched

See [AUDIT #11](../docs/AUDIT.md#11-the-freezethaw-scripts-could-strand-the-stack).
In short: the stop order was inverted, the 10-second default stop timeout risked
SIGKILLing Postgres mid-checkpoint, and nothing brought the stack back if the
backup job died between freeze and thaw.
