# Dual-CCD thread placement

## Why this matters on a 5950X

The Ryzen 9 5950X is not a 16-core CPU in any meaningful scheduling sense. It
is two 8-core dies — CCD0 and CCD1 — each with its own 32MB L3 cache, joined
through the I/O die. Cores within a CCD share L3. Cores across CCDs do not.

A thread that migrates from core 3 to core 11 keeps running, but its working
set does not follow it. Every line has to be re-fetched across the Infinity
Fabric. For a token-generation loop that is repeatedly sweeping the same
weights and KV cache, that migration is expensive and it is invisible in
`top`.

The Linux scheduler is cache-aware but it is not clairvoyant. Left alone it
will spread a Docker container's threads wherever there is idle capacity — and
on a box that is simultaneously running Postgres, a vector database, an
embedding model and a document parser, "wherever there is idle capacity" means
"across both dies, constantly".

So: partition along the physical boundary and pin everything.

## Reading your own topology

Do not copy the numbers below. Derive them.

```bash
lscpu -e=CPU,CORE,SOCKET,CACHE
```

The last column is `L1d:L1i:L2:L3`. **Group by the L3 value** — that is your
CCD map. On this machine:

```
CPU CORE  L1d:L1i:L2:L3
  0    0  0:0:0:0        <- L3 #0  = CCD0
  ...
  7    7  7:7:7:0
  8    8  8:8:8:1        <- L3 #1  = CCD1
  ...
 15   15  15:15:15:1
 16    0  0:0:0:0        <- SMT sibling of core 0, back on CCD0
```

Note rows 16-31: CPU 16 has `CORE 0`. It is the second thread of physical
core 0, not a separate core.

### The SMT sibling rule

```bash
cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list
# 0,16
```

**On this CPU the sibling of core N is thread N+16.** Not `N+1`. This is the
single most common way to get a `cpuset` wrong — see
[AUDIT #2](AUDIT.md#2-cpuset-0-4-was-allocating-half-the-intended-cpu), where
`cpuset: "0-4"` was silently providing five logical CPUs to an engine
configured for ten.

Generate the correct string rather than typing it:

```bash
# All logical CPUs belonging to physical cores 0-4
for c in 0 1 2 3 4; do
  cat /sys/devices/system/cpu/cpu$c/topology/thread_siblings_list
done | tr ',' '\n' | sort -n | uniq | paste -sd,
# 0,1,2,3,4,16,17,18,19,20
```

## The layout

### CCD0 — cores 0-7, low latency

| Zone | Physical | `cpuset` | Runs |
|---|---|---|---|
| Inference | 0-4 | `0-4,16-20` | `llama-swap` / `ik-llama-server` |
| Web & ingress | 5-6 | `5-6,21-22` | `open-webui` |
| GPU conductor | 7 | `7,23` | GPU hardware interrupts (no containers) |

Five physical cores for inference, sized to match `--threads 5` for generation
and `--threads-batch 10` for prompt processing. Generation is latency-bound and
gets one thread per physical core with no SMT contention; prompt processing is
throughput-bound and uses all ten logical CPUs.

The web zone sits on the same die as the engine so a request arriving through
Open WebUI reaches the backend without crossing the fabric.

Core 7 handles GPU interrupts and runs nothing else — see
[irq-pinning.md](irq-pinning.md), including the important caveat that this
turned out to matter much less than expected.

### CCD1 — cores 8-15, throughput

| `cpuset` | Runs |
|---|---|
| `8-15,24-31` | postgres, qdrant, infinity-embed, infinity-rerank, tika, cloudflared, monitoring stack, agents |

Everything here is cache-hostile: vector search walks large indexes, chunking
streams text, Postgres does random I/O, Tika allocates aggressively in a JVM.
These workloads will evict anything they are allowed to share a cache with.
Giving them their own die means they can saturate L3 and I/O without touching
the inference working set.

## Applying it

In Compose:

```yaml
services:
  llama-swap:
    cpuset: "0-4,16-20"
```

For a process outside Docker:

```bash
taskset -c 0-4,16-20 ./my-inference-binary
```

For a systemd unit:

```ini
[Service]
CPUAffinity=0-4 16-20
```

## Verifying it

Claims about pinning should be checked, not assumed:

```bash
# What each container actually got
docker inspect -f '{{.Name}} {{.HostConfig.CpusetCpus}}' $(docker ps -q)

# What a running process is actually allowed
taskset -cp $(pgrep -f ik-llama-server | head -1)

# Where the work is landing, live
htop   # press F2 -> Display options -> enable "Detailed CPU time",
       # then watch cores 0-4/16-20 during generation
```

If load appears on CCD1 during pure generation, something is not pinned.

## Tuning notes

- **Five cores is a starting point, not a law.** If your models are MoE or you
  run heavy CPU offload, more cores may help. If you are fully GPU-resident
  with `-ngl 99`, the CPU is mostly orchestrating and you may do just as well
  with four, freeing one for the rest of the system.
- **Do not pin to core 0 if you can avoid it** on systems that route a lot of
  kernel housekeeping there. Here the inference zone does include core 0,
  which is a deliberate trade for keeping all five cores contiguous within one
  L3 pool.
- **`isolcpus=` is the stronger version of this.** Adding
  `isolcpus=0-4,16-20 nohz_full=0-4,16-20` to the kernel command line removes
  those CPUs from the general scheduler entirely. It reduces jitter further
  but it will *not* move kernel-managed interrupts — see
  [AUDIT #3](AUDIT.md#3-gpu-interrupts-were-the-wrong-thing-to-optimise).
- **Verify after every Compose change.** A `cpuset` typo does not throw an
  error. It just quietly gives you a different machine.
