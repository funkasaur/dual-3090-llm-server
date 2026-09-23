# Interrupt isolation

> **Read [AUDIT #3](AUDIT.md#3-gpu-interrupts-were-the-wrong-thing-to-optimise)
> alongside this page.** The technique below is sound. The target it was
> originally aimed at — GPU interrupts — turned out to carry almost no traffic
> on this machine, while the interrupts that *do* land on the inference cores
> cannot be moved at all. Both facts are documented here rather than quietly
> dropped.

## The theory

A hardware interrupt preempts whatever is running on the target CPU. The
handler executes, pollutes L1/L2 and possibly L3, and returns. For most
workloads this is irrelevant. For a token-generation loop pinned to a specific
core for specific cache-locality reasons, an interrupt arriving mid-matmul
costs more than the handler's own runtime — it costs the cache lines the
handler evicted.

`irqbalance` makes this worse by design. Its job is to spread interrupts
across all available CPUs for thermal and throughput reasons, which is exactly
the opposite of what a pinned inference core wants.

So the plan is: forbid `irqbalance` from touching CCD0 at all, and hand-pin the
GPU interrupts to one designated core on CCD0 — the "conductor" — so that
interrupt data stays inside CCD0's L3 pool while never landing on a core doing
tensor math.

## The implementation

[`scripts/optimize-ai.sh`](../scripts/optimize-ai.sh). In outline:

```bash
# 1. Wait for the driver to register its interrupts (see AUDIT #1)
# 2. Ban irqbalance from CCD0 entirely, and from the GPU IRQs specifically
# 3. Pin each GPU IRQ to the conductor core, and verify the write took
# 4. Report anything still bound to the inference cores
```

### Banning irqbalance from a die

```ini
[Service]
Environment="IRQBALANCE_BANNED_CPULIST=0-7,16-23"
Environment="IRQBALANCE_ARGS=--banirq=166 --banirq=167"
```

`IRQBALANCE_BANNED_CPULIST` takes a CPU list and is correct for irqbalance
≥ 1.8. The older `IRQBALANCE_BANNED_CPUS` takes a hex mask and is deprecated —
irqbalance 1.9 prints `IRQBALANCE_BANNED_CPUS is discarded, Use
IRQBALANCE_BANNED_CPULIST instead` and ignores it. Check which one your version
wants:

```bash
irqbalance --version
strings "$(command -v irqbalance)" | grep IRQBALANCE_BANNED
```

`--banirq` removes specific IRQs from balancing altogether, so that the manual
affinity set in the next step is not immediately undone.

### Pinning, and verifying

```bash
echo 7 > /proc/irq/166/smp_affinity_list
```

Always read it back:

```bash
cat /proc/irq/166/smp_affinity_list
```

The kernel silently refuses affinity changes on *managed* interrupts. A blind
write looks exactly like a successful one. The script reads back every write
and reports mismatches, which is how finding #3 surfaced in the first place.

Note the distinction between the two affinity files:

| File | Meaning |
|---|---|
| `smp_affinity_list` | The mask you requested |
| `effective_affinity_list` | The single CPU the kernel actually delivers to |

For MSI interrupts these differ routinely — a mask of `0-31` will show an
`effective` of one specific CPU. When auditing, `effective_affinity_list` is
the one that tells you the truth.

## What the counters actually said

The point of `/proc/interrupts` is that it settles arguments. After 8.5 weeks:

```
$ grep -iE 'nvidia|nvme|enp' /proc/interrupts   # summed across all CPUs
 53: total=1199284540   enp42s0     effective=25   <- 2.5GbE NIC, on CCD1. Good.
 54: total=24434220     nvme1q1     effective=0    <- inference core
 55: total=4047786      nvme1q2     effective=1    <- inference core
 56: total=3855576      nvme1q3     effective=2    <- inference core
 57: total=4050823      nvme1q4     effective=3    <- inference core
 58: total=3566644      nvme1q5     effective=4    <- inference core
166: total=13           nvidia      effective=16
167: total=0            nvidia      effective=18
```

Two conclusions, neither of them the expected one:

1. **The GPU interrupts barely fire.** Thirteen, and zero. NVIDIA's driver
   uses mapped doorbells and polling for the hot path; it does not raise an
   interrupt per token. Pinning them is nearly free, and nearly pointless.
2. **The real traffic on the inference cores is NVMe**, and it is bound there
   by the kernel.

## A big total is not a high rate

1.2 billion interrupts from one NIC looks alarming until you divide by the
uptime it accumulated over:

```
1,199,284,540 interrupts / 60.7 days = ~229 per second
```

Measured live rather than inferred, on an otherwise idle machine:

```
$ # hardware interrupts, sampled over 10s
enp42s0    80.7 int/s    cpus: cpu25=807

$ # NET_RX softirqs, sampled over 10s: 2358 total (236/s)
   cpu25   1705   (72%)   <- the NIC's CPU, on CCD1
   cpu8-15, 24-30   648   <- container veth traffic, also CCD1
   cpu3, cpu19        5   (0.2%)  <- inference cores

$ mpstat -P 25
CPU    %usr   %nice    %sys  %iowait    %irq   %soft   %idle
 25    0.99    0.00    0.99     0.00    0.00    0.00   98.02
```

At ~230/s with a handler in the low microseconds, the NIC costs roughly
**0.05% of one core** — and that core is on CCD1. Five softirqs out of 2,358
touched the inference zone, and those were container loopback traffic, not the
NIC. The hard interrupt never goes near CCD0.

**Measure rates, not totals.** `/proc/interrupts` counters are cumulative
since boot, so on a long-lived machine every number looks enormous. Sample
twice and subtract:

```bash
python3 - <<'EOF'
import time
def snap():
    return {p[-1]: sum(int(x) for x in p[1:33])
            for p in (l.split() for l in open('/proc/interrupts'))
            if p and p[0].rstrip(':').isdigit()}
a = snap(); time.sleep(10); b = snap()
for dev in sorted(b, key=lambda d: b[d] - a.get(d, 0), reverse=True)[:10]:
    rate = (b[dev] - a.get(dev, 0)) / 10
    if rate: print(f"{dev:12s} {rate:9.1f} int/s")
EOF
```

### What you can actually tune on a cheap NIC

Not much, as it turns out. This one is an onboard Realtek:

```
$ ethtool -c enp42s0          # interrupt coalescing
netlink error: Operation not supported
$ ethtool -l enp42s0          # RSS / multi-queue
netlink error: Operation not supported
$ grep -c enp42s0 /proc/interrupts
1                             # single IRQ — nothing to spread
```

No coalescing knobs, no receive-side scaling, one interrupt line. The only
lever is *placement*, which the `irqbalance` CCD0 ban already handles.

If a single-queue NIC ever did saturate its one CPU, the fix is **RPS**
(Receive Packet Steering), which spreads softirq *processing* across other
cores while the hardware interrupt stays where it is:

```bash
# Spread NET_RX across CCD1 only (cpus 8-15, 24-31)
echo 'ff00ff00' > /sys/class/net/enp42s0/queues/rx-0/rps_cpus
```

Worth knowing about. Not worth doing at 98% idle.

## Managed interrupts: the part you cannot fix

```bash
$ cat /proc/irq/54/smp_affinity_list
0
```

A single CPU rather than a mask is the signature of a **kernel-managed**
interrupt. The block multi-queue layer allocates one NVMe submission queue per
CPU and binds its interrupt to that CPU, so that completions are handled on the
core that issued the I/O. That binding is created at queue-allocation time and
is not user-modifiable: `irqbalance` skips these, and a write to
`smp_affinity` returns an error.

On this machine, `nvme0q1`–`nvme0q5` and `nvme1q1`–`nvme1q5` sit on CPUs 0-4,
with queues 17-21 on the SMT siblings 16-20. That is the entire inference zone,
on both drives.

**You cannot move them.** What you can do:

1. **Nothing, usually.** Once weights are resident in VRAM, steady-state
   generation does essentially no disk I/O. Those queues are quiet exactly when
   it matters; they spike during model load.
2. **Separate the storage.** If you have more than one NVMe, keeping the model
   store off the drive whose queues share your inference cores means the
   queues that *are* there stay idle. It doesn't remove them; it keeps them
   quiet.
3. **`isolcpus` / `nohz_full`** stop the *scheduler* from placing tasks on
   those cores and reduce timer-tick jitter. They do not relocate managed
   interrupts. Worth doing on its own merits, not as a fix for this.

The script now prints this situation on every run rather than implying a
sterility the hardware does not provide:

```
--- Interrupts still bound to inference CPUs (0-4,16-20) ---
  IRQ 54 (nvme1q1) is pinned to CPU 0
  IRQ 86 (nvme0q1) is pinned to CPU 0
  ...
Managed interrupts above cannot be relocated from userspace.
```

## Checking your own machine

Before pinning anything, find out where the traffic is:

```bash
# Biggest interrupt sources, summed across CPUs
awk 'NR>1 {s=0; for(i=2;i<=NF-2;i++) s+=$i; if (s>100000) print s, $NF}' \
    /proc/interrupts | sort -rn | head -20
```

Then check where each one lands, and whether it can move:

```bash
for i in /proc/irq/[0-9]*; do
  irq=${i##*/}
  printf '%-5s want=%-10s effective=%-4s %s\n' \
    "$irq" "$(cat $i/smp_affinity_list 2>/dev/null)" \
    "$(cat $i/effective_affinity_list 2>/dev/null)" \
    "$(awk -v k="$irq:" '$1==k{print $NF}' /proc/interrupts)"
done | sort -k1 -n
```

A `want=` of a single number means managed and immovable. A `want=` of a range
means you can pin it.

## Watch out for

- **It resets.** Reloading the NVIDIA module, or anything that re-enumerates
  the devices, drops affinity back to the default mask — with no error and no
  failed unit. `optimize-ai.timer` re-asserts it hourly.
- **IRQ numbers are not stable** across reboots or hardware changes. Never
  hardcode them; discover them from `/proc/interrupts` at runtime, which is
  what the script does.
- **`irqbalance --banirq` needs the numbers at service start**, so the
  override file is regenerated on every run.
- **Interrupt counters are cumulative since boot.** A number that looks huge
  may be months of accumulation. Sample twice and subtract if you want a rate.
