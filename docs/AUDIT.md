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

**Lesson.** A boot-time `oneshot` that can fail needs either a health check or
a timer. Otherwise the only symptom of failure is that your optimisation
quietly isn't one.

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
still keeps *movable* interrupts away, and the 10GbE NIC — by far the largest
single source at 1.2 billion interrupts — correctly sits on CCD1. But the
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
| Backups via **Veeam** | Nightly automated pipeline is **Borg**; Veeam Agent is installed and running alongside it |
| "**1350W limit** enforced via `nvidia-smi` power caps" | 1350W is the UPS's rated output (`upsAdvanceIdentLoadPower`). The `nvidia-smi` cap is **270W per card**. Two unrelated numbers |
| `--split-mode graph`: "both cards calculate **identical layers** simultaneously, pooling 48GB" | Self-contradictory — duplicating layers would halve usable VRAM, not pool it. Graph mode splits the compute graph across devices |
| `cloudflared` on CCD0 ingress zone | Runs on CCD1 (`8-15,24-31`), with a comment reading `# Strictly on CCD0` |

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

The pattern worth taking away: **five of these eight were invisible.** The
service that failed, the alerts that never loaded, the cpuset that was half
what it looked like, the pruning that never happened, the pinning that reset
on module reload — all of them presented as working systems. The things that
break loudly get fixed. Build the check for the things that don't.
