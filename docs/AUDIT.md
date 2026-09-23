# Audit findings

Before publishing, every claim in this repository was checked against the
running machine. Several did not survive. This file records what was found,
with the evidence, because a tuning guide that only documents its successes is
not much use to anyone.

Findings are ordered by how much they mattered.

---

## 1. The GPU interrupt pinning had not run in two months

**Severity: high — the headline optimisation was silently inactive.**

```
$ systemctl status optimize-ai.service
× optimize-ai.service - AI Hardware IRQ Optimizer
     Active: failed (Result: exit-code) since Fri 2026-07-24 16:59:25 EDT; 1 month 30 days ago
   Main PID: 1149 (code=exited, status=1/FAILURE)

$ cat /proc/irq/166/smp_affinity_list
0-31
```

The affinity mask was the kernel default. Nothing had been pinned for the
entire 8.5-week uptime.

**Cause.** The unit was ordered `After=network.target`, which says nothing
about the NVIDIA driver. On a cold boot the script ran before the driver had
registered its interrupts, found no `nvidia` lines in `/proc/interrupts`, and
took its own error path:

```bash
if [ ${#IRQ_ARRAY[@]} -lt 2 ]; then
    echo "❌ Error: Could not detect two NVIDIA GPU IRQs. Is the driver loaded?"
    exit 1
fi
```

It failed correctly and said so — into a journal nobody reads. A `oneshot`
unit that fails at boot leaves no trace in normal operation.

**Fixed by** ordering the unit after `nvidia-persistenced.service`, gating it
on `ConditionPathExistsGlob=/dev/nvidia[0-9]*`, and — the part that actually
matters — making the script *wait* for the interrupts to appear rather than
assuming they are already there.

There is a second-order problem: IRQ affinity is not sticky. Reloading the
NVIDIA module resets it, with no failed unit and no log line. `optimize-ai.timer`
now re-asserts the pinning hourly; the script is idempotent.

### 1a. Half the script worked the whole time

The failure was partial in a way worth understanding, because it explains why
nobody noticed.

The script does two different kinds of work:

| Step | Writes to | Survived? |
|---|---|---|
| Ban `irqbalance` from CCD0 | `/etc/systemd/system/irqbalance.service.d/override.conf` | **Yes** |
| Pin GPU IRQs to the conductor core | `/proc/irq/*/smp_affinity_list` | No |

The first is a **file on disk**. It was written during a successful run some
time before July and has applied on every boot since — `irqbalance` reads it
from the unit override whether or not the script ever runs again.

The second is **runtime kernel state**. It lives only in memory, resets on
every boot, and has to be re-applied by something that actually runs.

So for two months the machine kept its network and NVMe interrupts off CCD0
exactly as designed, while the GPU pinning the script is named for did
nothing. The 2.5GbE NIC sitting correctly on CPU 25 — see
[#3](#3-gpu-interrupts-were-the-wrong-thing-to-optimise) — is a result of that
surviving config file, not of the script running.

The machine looked healthy because the durable half happened to be the half
doing the useful work. Combined with #3 — the broken half was aimed at
interrupts that fire 13 times in two months — the real cost of a two-month
outage in the headline optimisation was approximately zero. That is luck, not
design.

**Lesson.** A boot-time `oneshot` that can fail needs either a health check or
a timer. Otherwise the only symptom of failure is that your optimisation
quietly isn't one.

And when one script mixes persistent configuration with runtime state, expect
the two to fail apart. The config half will outlive its own script and keep
working; the runtime half vanishes at the next reboot, module reload or failed
unit. Partial success is harder to spot than total failure, because the
surviving half keeps producing evidence that things are fine. If both halves
must live in one script, make it idempotent and re-run it on a timer — which
is what `optimize-ai.timer` now does.

---

## 2. `cpuset: "0-4"` was allocating half the intended CPU

**Severity: high — direct throughput impact on prompt processing.**

On a 5950X the SMT sibling of core *N* is thread *N+16*:

```
$ cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list
0,16
```

So `cpuset: "0-4"` is five *logical* CPUs — one thread from each of five
physical cores, with the siblings left to the rest of the system. The comment
in the Compose file claimed otherwise:

```yaml
cpuset: "0-4"  # Uses 5 Physical Cores (0-4) + SMT siblings   <-- not true
```

This collided with the engine's own threading:

```
--threads 5 --threads-batch 10
```

`--threads-batch 10` asks for ten threads during prompt processing, inside a
cgroup permitted five logical CPUs — a 2:1 oversubscription on exactly the
phase where throughput matters most.

**Fixed** to `cpuset: "0-4,16-20"`, which makes the existing `--threads-batch 10`
correct rather than aspirational.

**Lesson.** Always derive sibling layout from
`/sys/devices/system/cpu/cpu*/topology/thread_siblings_list`. The `N`/`N+1`
assumption is an Intel habit and it is wrong here.

---

## 3. GPU interrupts were the wrong thing to optimise

**Severity: medium — the work was real, the target was not.**

After 8.5 weeks of uptime:

```
$ grep nvidia /proc/interrupts   # summed across all CPUs
166: total=13    nvidia
167: total=0     nvidia
```

Thirteen interrupts. And zero. NVIDIA's driver does its work through mapped
doorbells and polling, not per-token interrupts — so pinning these two lines
is close to a no-op regardless of how carefully it's done.

Meanwhile, on the same machine:

```
53: total=1199284540  enp42s0     -> CPU 25   (CCD1, fine)
54: total=24434220    nvme1q1     -> CPU 0    (inference core)
55: total=4047786     nvme1q2     -> CPU 1    (inference core)
...
```

**The actual interrupt load on the "sterile" inference cores is NVMe.** Each
NVMe submission queue is bound to one CPU, and queues 1-5 on both drives land
precisely on cores 0-4, with queues 17-21 on their SMT siblings 16-20.

Worse, these cannot be moved:

```
$ cat /proc/irq/54/smp_affinity_list
0
```

A single CPU, not a mask. These are kernel-**managed** interrupts. `irqbalance`
will not touch them and a manual write to `smp_affinity` is rejected. No
userspace tool can relocate them — the binding is created by the blk-mq layer
when the queues are allocated.

This does not make the isolation worthless: banning `irqbalance` from CCD0
still keeps *movable* interrupts away, and the 2.5GbE NIC — by far the largest
single source at 1.2 billion interrupts, though see
[the rate measurement](irq-pinning.md#a-big-total-is-not-a-high-rate) for how
little that actually costs — correctly sits on CCD1. But the
mental model of a "completely sterile" inference zone was not accurate, and
the one thing the script worked hardest at was the one thing that barely
mattered.

**Mitigations that actually apply**, in order of practicality:

1. **Accept it.** Once weights are resident in VRAM, steady-state inference
   does almost no disk I/O. The NVMe queues are near-idle during generation;
   they spike during model load.
2. **Move the model store off the drive that shares queues with the inference
   cores**, if you have a second NVMe. Doesn't remove the queues, but keeps
   them quiet when it counts.
3. `isolcpus=` / `nohz_full=` on the kernel command line will stop the
   *scheduler* from placing tasks there. It does **not** move managed
   interrupts. Worth doing for jitter, not for this.

The script now reports this situation instead of implying it doesn't exist:
it lists every single-CPU-bound interrupt sitting on the inference cores and
states plainly that they can't be relocated.

**Lesson.** Measure the interrupt counters before optimising the interrupt
layout. `/proc/interrupts` would have answered this in one command.

---

## 4. Every GPU alert rule was dead config

**Severity: medium — monitoring appeared to exist and did not.**

`gpu-alert.yaml` was carefully written. It was never loaded:

```
$ grep -c 'rule_files' prometheus.yml
0
$ docker exec prometheus ls /etc/prometheus/
prometheus.yml
```

No `rule_files:` stanza, and the file was not mounted into the container.
Zero rules were evaluated. Thermal, VRAM and throttle alerting had never
fired because it had never existed.

**Fixed** by adding the mount and the `rule_files:` stanza. Confirm after a
reload at `/rules` in the Prometheus UI — an empty page there means it is
still not working.

### 4a. The VRAM rule could not have worked anyway

```yaml
expr: (dcgm_fi_dev_fb_used / dcgm_fi_dev_fb_free) * 100 > 95
```

Used divided by **free**, not by total. At 20GB used / 4GB free that evaluates
to 500%, so the alert would have been permanently firing under any real load,
while being incapable of expressing "95% full". Corrected to
`used / (used + free)`.

### 4b. The throttle rule fires on an idle GPU

```yaml
expr: dcgm_fi_dev_clock_throttle_reasons > 0
```

That field is a **bitmask**, and bit 0 (`0x1`) means "GPU is idle". The rule
was true whenever nothing was running. Bit 2 (`0x4`) is "SW power cap", which
is also permanently expected here because the cards are deliberately capped
at 270W.

PromQL has no bitwise AND, but the three benign bits (`0x1|0x2|0x4`) sum to 7,
so `>= 8` is exactly equivalent to "some bit of 0x08 or higher is set" — real
hardware or thermal slowdown only. That is the fix.

### 4c. The temperature threshold was mislabelled

Alerting at 65°C with the description *"close to thermal throttling limits"* —
a 3090 core throttles around 83°C, and 65°C under sustained load is simply
normal. Split into a warning at 80°C and a critical at 84°C.

Added a rule the original was missing entirely: **GDDR6X memory junction
temperature** (`dcgm_fi_dev_memory_temp`). This is the failure mode that
actually degrades 3090s. Memory runs far hotter than the core and throttles at
110°C — a card reading a comfortable 70°C on the core can be sitting at 100°C+
on the backside memory modules.

---

## 5. The backup script had several quiet failure modes

**Severity: medium — a backup that fails silently is worse than none.**

```bash
docker exec webui-postgres pg_dumpall -U webui_admin > /var/backup/postgres/pg_dumpall_$(date +%F).sql
```

- The redirect **creates and truncates the target before `pg_dumpall` runs**.
  If the dump fails halfway, what's left is a partial file with a plausible
  name and a recent timestamp. Now written to `.partial` and renamed only on
  success.
- **Dated dumps were never pruned.** They accumulated indefinitely and were
  re-archived by Borg every night. Retention added (14 days, configurable).
- **Qdrant snapshots were never pruned either.** Every run created one and
  none were deleted; they live inside the storage volume, so the collection
  directory grows forever. Now keeps the newest 3 per collection.
- **The live Qdrant storage directory was being archived** while Qdrant was
  writing to it, which is exactly what the snapshots exist to avoid. The
  storage path is now excluded; the snapshots are what gets backed up.
- **No check that the repository volume was mounted.** If `/mnt/storage` were
  absent, Borg would happily initialise onto the root filesystem. The unit now
  declares `RequiresMountsFor=/mnt/storage` and the script verifies with
  `borg info` before doing any work.
- **No exit status in the log.** Under `set -e` the script died mid-step and
  the last line written was whatever was in progress. An `EXIT` trap now
  always records success or failure.

Also: the backup job ran with no CPU or I/O constraints. It now runs pinned to
CCD1 with `IOSchedulingClass=idle`, so a 3am archive run cannot compete with an
inference request.

---

## 6. Stale duplicate scripts

**Severity: low — but it is how the wrong config gets deployed.**

```
/usr/local/bin/backup.sh          (current)
/usr/local/bin/backup.sh1         (older, uses host postgres, no longer valid)
/usr/local/bin/set-gpu-power.sh   (270W — the one systemd actually runs)
/usr/local/bin/set-gpu-limit.sh1  (292W — orphaned)
```

`backup.sh1` still calls `sudo -u postgres pg_dumpall`, from before Postgres
moved into a container. It would dump the wrong database, or nothing.

Two GPU power scripts disagreeing by 22W, distinguished only by a trailing
`1`, is a foot-gun. Only the canonical versions are in this repo; the
`.sh1` files should be deleted from the host.

---

## 7. Documentation drift

**Severity: low individually — but this is what people copy.**

The prose description of this build, checked against the machine:

| Claim | Reality |
|---|---|
| GPU IRQs pinned to **core 5** (threads 5, 17) | Script pins to **core 7** (threads 7, 23) |
| Inference on "cores 0-4 (**threads 12-16**)" | Threads **0-4, 16-20** |
| Engine is `ik-llama-server` | `llama-swap` **fronting** `ik-llama-server` |
| Backups via **Veeam** | Both run. Borg nightly at 03:00 via systemd (verified succeeding); Veeam image-level from a Windows B&R server (see [#10](#9-the-veeam-job-had-not-run-in-three-days)) |
| "**1350W limit** enforced via `nvidia-smi` power caps" | 1350W is the UPS's rated output (`upsAdvanceIdentLoadPower`). The `nvidia-smi` cap is **270W per card**. Two unrelated numbers |
| `--split-mode graph`: "both cards calculate **identical layers** simultaneously, pooling 48GB" | Self-contradictory — duplicating layers would halve usable VRAM, not pool it. Graph mode splits the compute graph across devices |
| `cloudflared` on CCD0 ingress zone | Runs on CCD1 (`8-15,24-31`), with a comment reading `# Strictly on CCD0` |
| **Intel X520-DA2 10GbE SFP+ over OM4 fiber** to a Brocade ICX 7250-24P | No such card is installed. The active link is the **onboard Realtek Killer E3000 2.5GbE** (`enp42s0`, 2500Mb/s, `Port: Twisted Pair` — copper, not fiber). Two triple-slot Strix 3090s in NVLink leave no free slot, and both GPUs are already down to x8 on AM4's 16 CPU lanes. The X520 is real, but it is in the **Veeam backup server** — its job logs enumerate `Intel(R) Ethernet 10G 2P X520 Adapter` on that host. The card got attributed to the wrong machine |

None of these change what the machine does. All of them would mislead someone
reproducing it.

---

## 8. Grafana allows anonymous access

**Severity: low on a trusted LAN, high the moment it isn't.**

```yaml
- GF_AUTH_ANONYMOUS_ENABLED=true
- GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
```

Fine behind a firewall, and convenient for a wall dashboard. But this stack
also runs a Cloudflare tunnel, and the gap between "LAN only" and "published"
is one dashboard route. If that port is ever exposed, put Cloudflare Access in
front of it. Noted inline in the Compose file.

### 8a. Set the SNMP community string

The `cyberpower` module takes its credentials from `auth.yml`:

```yaml
auths:
  cyberpower_v1:
    version: 1
    community: <your-community-string>
```

`public` is the factory default on essentially every SNMP device, and SNMPv1
community strings cross the wire in cleartext, so a default here is a read
credential for the UPS that anyone on the LAN can guess without trying.

The exposure is modest — read-only access to power telemetry — but it is worth
setting on principle, and it is the kind of default that gets copied forward
onto devices where read access matters more. If the RMCARD205 supports SNMPv3,
use `authPriv` instead; there is a commented example in `auth.example.yml`.

### 8b. Qdrant has no authentication

Qdrant runs with no API key and publishes 6333/6334 to the host. That is the
default and is fine on a trusted LAN, but it means anything that can reach the
host can read or delete every vector collection. The Compose file now carries a
commented `QDRANT__SERVICE__API_KEY` line for anyone whose network is less
friendly.

---

## 9. The Veeam job had not run in three days

**Severity: high — and nobody had noticed, including the operator.**

The working assumption going into this audit was that Borg had been retired and
Veeam was now the only backup. The filesystem said otherwise, in both
directions.

Borg ran successfully six hours before this was written:

```
$ systemctl status borg-backup.service
   Process: 2033170 ExecStart=/usr/local/bin/backup.sh (code=exited, status=0/SUCCESS)
   Finished borg-backup.service - Wed 2026-09-23 03:00:30 EDT
   All archives: 2.20 TB  ->  102.75 GB deduplicated
```

Veeam did not:

```
2026-09-12 05:55   625 GB   ...D2026-09-12T045809_D401.vbk   full
2026-09-13 04:02   1.3 GB   ...D2026-09-13T040023_09BE.vib   incremental
2026-09-19 05:57   700 GB   ...D2026-09-19T045428_168D.vbk   full
2026-09-20 04:02   2.5 GB   ...D2026-09-20T040014_E46A.vib   incremental
                            <- nothing for Sep 21, 22, or 23
```

The established pattern is a weekly full on Saturdays with daily incrementals.
Three consecutive daily runs are missing. Retention removes *old* restore
points, never recent ones, so this is missed or failed runs — and the cause is
on the Windows B&R side, not on this host.

### Root cause: the repository ran out of space

The Veeam job logs name it exactly. On the 19 Sep run:

```
[19.09.2026 04:43:46]  Error  Agent: Failed to process method {Transform.CompileFIB}:
                              There is not enough space on the disk.
[19.09.2026 04:43:46]  Error  Asynchronous request operation has failed.
                              [requestsize = 1056768] [offset = 412453302272]
[19.09.2026 04:43:46]  Error     in c++: Error code: 0x00000070
```

`0x70` is `ERROR_DISK_FULL`, and `Transform.CompileFIB` is the synthetic full
build. Session outcomes across the retained logs:

| When | Mode | Status | Transferred |
|---|---|---|---|
| 12 Sep 05:55 | Retry | Success | 0 B |
| 13 Sep 04:02 | Normal | Success | 656.2 GB |
| **19 Sep 04:43** | Normal | **Failed** | 717.8 GB |
| 19 Sep 05:57 | Retry | Success | 0 B |
| 20 Sep 04:02 | Normal | **Warning** | 716.9 GB |

The arithmetic, measured on the live volume:

```
$ df -h /mnt/storage
/dev/nvme0n1p1  1.8T  1.4T  405G  77% /mnt/storage

full backups on disk:  625 GB (12 Sep) + 700 GB (19 Sep) ≈ 1.3 TB
free space:            405 GB
space a new synthetic full needs:  ~700 GB
```

A synthetic full is built by merging the existing chain into a **new** full
file, which has to exist alongside the old one before anything can be pruned.
That needs roughly one full's worth of free space. There is 405 GB, and a full
is ~700 GB. The job cannot complete and will not be able to until the
repository has headroom.

The job retains `RetainCycles=7` restore points with `RetainDays=30`, and
synthetic fulls on Saturdays. That policy and this volume are incompatible.
Options, in rough order of how well they fit a fixed-size repository:

1. **Switch to forever-forward incremental** — drop synthetic fulls entirely.
   Veeam then merges the oldest increment into the single full each run, so
   only ever one full exists and the ~700 GB of transient headroom stops being
   needed at all. Best fit for a repository that cannot grow.
2. **Reduce `RetainCycles`** until only one full is retained. With the older
   625 GB full pruned, free space goes from 405 GB to ~1 TB, which is
   comfortably enough to build a synthetic full.
3. **Move the repository off this box**, which also fixes
   [10a](#9a-both-backup-systems-share-one-failure-domain).
4. Add capacity — the drive is already 1.8 TB and 77% full, so this only
   defers the question.

**The dangerous part is not the outage. It is the belief.** The operator's
mental model was "Borg is gone, Veeam covers me." The reality was the exact
inverse: the layer assumed dead was the only one working, and the layer assumed
healthy had been silent for three days. Had that belief been acted on — by
disabling the Borg timer — the machine would have had no backups at all, and
nothing would have reported it.

**Lesson.** Backup verification has to be external to the backup. A job that
stops running produces no logs, no alerts and no errors; its failure signature
is an absence, and absences are invisible unless something is specifically
watching for them. Check freshness of the *output*, not health of the process:

```bash
# Alert if the newest restore point is older than ~36h
find /mnt/storage/<repo> -name '*.vib' -o -name '*.vbk' -mmin -2160 | grep -q . \
  || echo "NO RECENT VEEAM RESTORE POINT"
```

### 9a. Both backup systems share one failure domain

`/mnt/storage` is exported over Samba as `[WindowsShare]` and is where the
Windows B&R server writes its repository. It is a dedicated NVMe, separate
from the OS drive:

```
nvme1n1  1.8T  WD_BLACK SN850X  ->  /              (OS, LVM)
nvme0n1  1.8T  WD_BLACK SN850X  ->  /mnt/storage   (backups)
```

That separation is worth having, and it does real work: a failed boot drive
leaves the image backups intact, which is exactly the scenario bare-metal
restore exists for.

The gap is narrower than "backups on the same disk as the system", but it is
still there — **both backup systems live on that one drive**:

```
/mnt/storage/borg/system   92 GB   <- Borg, file-level
/mnt/storage/borg/veeam    1.3 TB  <- Veeam, image-level
```

Two independent backup tools, deliberately chosen to fail independently,
sharing a single point of failure. If `nvme0n1` dies, both layers die in the
same instant and the only surviving copy of anything is the live data on the
OS drive. Every other failure that reaches the machine — theft, fire, a PSU
event taking both drives, ransomware reaching the SMB share, a mistaken `rm`
on `/mnt/storage` — has the same shape.

`veeamimmurepo.service` is running, which helps against the ransomware case
specifically. Nothing here helps against losing the machine. This is a solid
local restore tier and not an off-site copy, which is the one thing the setup
does not have.

---

## 10. The freeze/thaw scripts could strand the stack

**Severity: medium — low probability, high blast radius.**

```bash
# pre-freeze.sh
docker stop webui-postgres open-webui qdrant
# post-thaw.sh
docker start webui-postgres qdrant open-webui
```

They work — container timestamps confirm an ~11 second freeze window on the
Sep 20 run. Three problems nonetheless:

**Stop order is inverted.** `webui-postgres` goes down before `open-webui`, so
the UI spends the shutdown window erroring against a database that has already
left. Stop dependents first; start them last. The *start* order in post-thaw is
already correct, which suggests the stop order was simply not thought about.

**The default 10s stop timeout can defeat the purpose.** `docker stop` sends
SIGTERM, waits, then SIGKILLs. The Postgres image maps SIGTERM to a fast
shutdown, which still has to complete a checkpoint. On a database this size a
SIGKILL partway through is plausible — producing precisely the inconsistent
data directory that stopping the container was meant to prevent. The patched
script uses `-t 60` and warns if any container exits 137 (SIGKILL), so a
too-short timeout reports itself instead of silently degrading the backup.

**Nothing thaws the stack if the job dies.** If Veeam fails, times out, or the
network drops between freeze and thaw, post-thaw never runs — and
`restart: unless-stopped` does **not** restart an explicitly stopped container.
Postgres, Qdrant and the UI stay down until a human notices. `pre-freeze.sh`
now arms a `systemd-run --on-active=30min` transient timer that thaws
unconditionally, and `post-thaw.sh` cancels it on the happy path.

The failsafe runs an inline `docker start`, not a call to `post-thaw.sh`.
Veeam uploads these scripts to `/var/lib/veeam/scripts` only for the duration
of the job session, so a path reference could point at a file that no longer
exists by the time the timer fires. An insurance policy must not depend on the
thing it is insuring against.

Also added: both scripts log to `/var/log/veeam-freeze.log` with timings, and
post-thaw waits for health checks to settle rather than treating "`docker start`
returned 0" as proof the stack works.

---

## 11. Borg's retention works. Its pre-hooks never clean up after themselves

**Severity: high — the vector database was not in the backup at all.**

The suspicion was that Borg was not deleting old archives. It is. The
arithmetic settles it without needing to open the repository:

```
This archive:   136.98 GB      (one night, logical size)
All archives:     2.20 TB      (everything retained, logical size)

2.20 TB / 136.98 GB ~= 16 archives
```

Sixteen is what `--keep-daily 7 --keep-weekly 4 --keep-monthly 6` should
retain once the windows overlap. With 118 nightly runs recorded in the log and
no pruning, that figure would be nearer 16 TB. Deduplicated size confirms it:
101.98 GB on 30 Aug against 102.75 GB on 23 Sep — **0.77 GB of growth in 24
days**, and 92 GB on disk. The repository is stable.

What is not stable is everything the pre-hooks create.

### 11a. Postgres dumps were never pruned

```
$ ls /var/backup/postgres/ | wc -l
117
$ ls /var/backup/postgres/ | head -1
pg_dumpall_2026-05-30.sql
$ du -sh /var/backup/postgres
2.6G
```

Every dump since the day the script was written, still on disk — and each one
re-archived by Borg every night since. Fixed with a `PG_KEEP_DAYS` retention
sweep (14 days by default).

### 11b. Qdrant snapshots were stranded in the container

This is the serious one.

```
$ docker exec qdrant find /qdrant/snapshots -name '*.snapshot' | wc -l
370
$ docker exec qdrant du -sh /qdrant/snapshots
5.6G
```

370 snapshots, never pruned. But the count is the least of it — look at where
they live:

```
$ docker inspect qdrant -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
/home/aiuser/qdrant/qdrant_storage -> /qdrant/storage
```

`/qdrant/storage` is mounted. **`/qdrant/snapshots` is not.** Qdrant writes
snapshots there by default, so all 5.6 GB of them sit in the container's
writable layer, which means:

- **They are not backed up.** Borg archives `/home`, and nothing in `/home`
  contains them. The backup script was faithfully creating snapshots for
  consistency and then archiving everything *except* the snapshots.
- **They do not survive the container.** `docker compose down`, an image
  update, or a `docker rm` discards the writable layer and every snapshot with
  it.
- **They inflate Docker's storage.** The qdrant container's writable layer is
  5.98 GB, against a 6.19 GB virtual size — almost all of it snapshots.

Combined with the earlier fix that (correctly) excludes the live
`qdrant_storage` directory from the archive, the net effect was that **the
vector database had no representation in the backup whatsoever.** The
exclusion was right; the snapshots were supposed to replace it, and they never
arrived.

Fixed by mounting `./qdrant_snapshots:/qdrant/snapshots` in the Compose file,
and by making `backup.sh` count the snapshots visible on the host and warn
loudly when the directory is missing or empty. A backup script that silently
omits a database is worse than one that fails.

### 11c. Orphaned snapshots are never pruned

Retention walks the **live** collection list from the API, so when a collection
is deleted its snapshot directory stops being visited and simply sits there:

```
ORPHANED: hermes_memory (79 snapshots, 161M)
live:     open-webui_files (117 snapshots, 5.1G)
live:     open-webui_knowledge (85 snapshots, 280M)
...
```

`hermes_memory` no longer exists in Qdrant. Its 79 snapshots will never be
touched by any amount of retention tuning.

`backup.sh` now reports these rather than deleting them. That is deliberate:
the snapshots of a dropped collection may be the last copy of something
deleted by accident, and a nightly backup job is the wrong place to make that
decision unattended.

Note also that each snapshot has a `.snapshot.checksum` sidecar, so a naive
file count doubles. Count `*.snapshot` specifically.

**Lesson.** "Did the pre-hook run?" and "did its output reach the archive?"
are different questions. This one answered yes to the first for months. The
only way to catch it is to verify from the destination: list what is actually
*in* an archive, rather than trusting that what you created got picked up.

```bash
borg list ::system-2026-09-23_03:00 | grep -c snapshot
```

---

## Summary

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | GPU IRQ pinning inactive for 2 months | High | Fixed + timer |
| 2 | `cpuset` allocating half the intended CPU | High | Fixed |
| 3 | GPU IRQs are the wrong optimisation target | Medium | Documented, reported by script |
| 4 | All alert rules unloaded; 3 rules also wrong | Medium | Fixed |
| 5 | Silent backup failure modes | Medium | Fixed |
| 6 | Stale duplicate scripts | Low | Excluded; delete from host |
| 7 | Documentation drift | Low | Corrected |
| 8 | Anonymous Grafana; SNMP community string; unauthenticated Qdrant | Low | Documented |
| 9 | Veeam stalled: repository out of space for synthetic fulls | High | **Root cause found — needs capacity** |
| 10 | Freeze/thaw could strand the stack; unsafe stop order and timeout | Medium | Fixed |
| 11 | Qdrant snapshots stranded in container — vector DB absent from backups | High | Fixed (needs volume mount + cleanup) |

The pattern worth taking away: **eight of these eleven were invisible.** The
service that failed, the alerts that never loaded, the cpuset that was half
what it looked like, the pruning that never happened, the pinning that reset
on module reload, the backup job that simply stopped running — all of them
presented as working systems. The things that break loudly get fixed. Build
the check for the things that don't.

The worst of them are the **partial** failures, because they come with
evidence that everything is fine. `optimize-ai.sh` kept its `irqbalance` ban
in place for two months after the script stopped running
([#1a](#1a-half-the-script-worked-the-whole-time)) — interrupts really were
isolated from CCD0, just not by anything still running. Half a system that
works is much harder to notice than none of it.

The general shape: **check the output, not the process.** A backup job that
stopped running produces no errors, only an absence of new restore points. An
alert file that was never loaded produces no alerts, which is indistinguishable
from nothing being wrong. A config file that outlived its script produces
correct behaviour with no cause. In every case the question that finds the
problem is *"when did this last actually produce something?"* — not *"is it
enabled?"*
