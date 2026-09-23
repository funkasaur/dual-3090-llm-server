# Veeam guest processing scripts

Stop the stateful containers around the volume snapshot so the image is
application-consistent, then bring them back. Measured downtime on a real run:
**~11 seconds**.

---

## Where these files go — read this first

This trips people up, because the answer is "both machines":

```
  Veeam Backup & Replication server            This Linux host
  (Windows)                                    (Veeam Agent)

  C:\VeeamScripts\pre-freeze.sh    ──upload──►  /var/lib/veeam/scripts/
  C:\VeeamScripts\post-thaw.sh       at job     └─ executed here, as root
         ▲                           runtime
         └── you store and edit them here
```

**You store the scripts on the Veeam backup server.** The job wizard's
`Browse` button opens a file picker onto *the backup server's* local
filesystem — it cannot see this Linux host at all.

**Veeam executes them on the protected machine.** At the start of each job
session it uploads both scripts to `/var/lib/veeam/scripts` on every agent
computer in the job and runs them there as root. That has to be true: nothing
on a Windows server can stop a Docker container on a Linux box without running
something on the Linux box.

So installing these to `/usr/local/bin` on this host does nothing — Veeam will
never look there. This applies to Veeam Agent for Linux managed by Veeam
Backup & Replication, which is how a physical server like this one is
protected.

> Because the files live on a Windows machine, **keep UNIX (LF) line
> endings.** Editing them in Notepad converts to CRLF, and the script then
> fails with `bad interpreter: /bin/bash^M` — a freeze failure that aborts the
> whole job. Use an editor that preserves LF and verify with
> `file pre-freeze.sh` (should say "ASCII text", not "with CRLF line
> terminators"). Veeam also requires the `.sh` extension.

---

## Configuring the job

In the Veeam Backup & Replication console, on the agent backup job:

1. Open the job → **Guest Processing**
2. Tick **Enable application-aware processing**
3. Click **Applications**, select this computer (or its protection group),
   click **Edit**
4. In **Processing Settings**, open the **Scripts** tab and enable script
   execution

There are **two separate pairs** of script fields, and they are not
interchangeable:

| Pair | Runs when | Use for |
|---|---|---|
| Pre-job / post-job | Before and after the whole backup *session* | Notifications, mounting a repo |
| **Pre-freeze / post-thaw** | Immediately before and after the **volume snapshot** | **These scripts** |

Put `pre-freeze.sh` and `post-thaw.sh` in the **pre-freeze / post-thaw**
fields. The job-level pair brackets the entire session, which would leave the
containers down for the full duration of the backup instead of the few seconds
the snapshot takes.

Snapshot scripts require a **volume-level** backup job (entire machine or
selected volumes). File-level jobs only expose the job-level pair.

---

## Tunables

Both scripts read their settings from the environment, so you can override
them without editing the files:

| Variable | Default | Meaning |
|---|---|---|
| `STOP_TIMEOUT` | `60` | Seconds allowed for a clean container shutdown |
| `FAILSAFE_MINUTES` | `30` | Unconditional thaw if the job never completes |
| `HEALTH_TIMEOUT` | `120` | Seconds to wait for health checks after starting |
| `LOG` | `/var/log/veeam-freeze.log` | Shared log for both scripts |

---

## Test before trusting

Run them by hand on the Linux host first — they work standalone, with no
Veeam involvement:

```bash
sudo ./pre-freeze.sh; echo "exit=$?"
docker ps --format '{{.Names}}\t{{.Status}}'   # the three should be gone
sudo ./post-thaw.sh;  echo "exit=$?"
tail -30 /var/log/veeam-freeze.log
```

Then test the failsafe, which is the part you least want to discover is broken
during a genuinely failed backup:

```bash
sudo FAILSAFE_MINUTES=1 ./pre-freeze.sh
systemctl list-timers veeam-failsafe-thaw.timer    # should be armed
# wait ~1 minute WITHOUT running post-thaw
docker ps   # the containers should have come back on their own
```

After a real job, confirm Veeam actually ran them:

```bash
sudo ls -la /var/lib/veeam/scripts/     # Veeam's uploaded copies
tail -40 /var/log/veeam-freeze.log      # timings from the last run
```

---

## What was patched

See [AUDIT #11](../docs/AUDIT.md#11-the-freezethaw-scripts-could-strand-the-stack).

- **Stop order was inverted** — `webui-postgres` went down before
  `open-webui`, so the UI spent the window erroring against a departed
  database. Dependents now stop first and start last.
- **Stop timeout was the Docker default of 10s.** SIGTERM triggers a Postgres
  fast shutdown that still has to finish a checkpoint; a SIGKILL partway
  through produces exactly the inconsistent data directory the stop exists to
  prevent. Now `-t 60`, with a warning if any container exits 137.
- **Nothing thawed the stack if the job died.** `restart: unless-stopped` does
  not restart an explicitly stopped container. `pre-freeze.sh` now arms a
  `systemd-run` transient timer before stopping anything.
- **The failsafe runs an inline `docker start`**, not a call to
  `post-thaw.sh`. Veeam's uploaded copies live under `/var/lib/veeam/scripts`
  only for the job's duration, so a path reference could point at a file that
  no longer exists when the timer fires. The insurance policy must not depend
  on the thing it insures against.
- **Health verification** — `docker start` returning 0 means the container was
  started, not that it works. `post-thaw.sh` waits for health checks to settle.
- **Logging** — neither script logged anything; both now write timings to
  `/var/log/veeam-freeze.log`.

---

## Sources

- [Backup Job and Snapshot Scripts — Veeam Agent Management Guide](https://helpcenter.veeam.com/archive/backup/120/agents/agent_job_guest_scripts.html)
- [Pre-Freeze and Post-Thaw Scripts — Veeam Backup & Replication User Guide](https://helpcenter.veeam.com/docs/vbr/userguide/agent_job_vss_scripts.html)
- [Backup Job Scripts — Veeam Agent for Linux User Guide](https://helpcenter.veeam.com/docs/agentforlinux/userguide/backup_job_script.html)
