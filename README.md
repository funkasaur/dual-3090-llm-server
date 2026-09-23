# Dual-3090 LLM Inference Server

A bare-metal, self-hosted LLM inference server built on a dual-CCD AMD Ryzen 9 5950X
and two NVLink-bridged RTX 3090s — documented down to the interrupt level.

Most self-hosting guides stop at `docker run`. The interesting problems on this
class of machine are not in the container: they are in how the Linux host
schedules threads across two Core Complex Dies, where hardware interrupts land,
and how much power the whole thing is allowed to pull before the UPS gives up.
This repository documents that layer, with the working configuration and the
measurements behind it.

It also documents what **didn't** work. The optimisations here were audited
against the running machine before publication, and several of them turned out
to be inactive, misconfigured, or aimed at the wrong target.
[`docs/AUDIT.md`](docs/AUDIT.md) is the honest version, and it is probably the
most useful file here.

---

## Contents

| | |
|---|---|
| [Hardware](#hardware) | What the box is |
| [`docs/cpu-topology.md`](docs/cpu-topology.md) | Dual-CCD thread placement, and how to derive it yourself |
| [`docs/irq-pinning.md`](docs/irq-pinning.md) | Interrupt isolation — including why the obvious target is the wrong one |
| [`docs/inference.md`](docs/inference.md) | llama-swap + ik-llama-server, NVLink, measured throughput |
| [`docs/monitoring.md`](docs/monitoring.md) | Prometheus / Grafana / DCGM / SNMP |
| [`docs/backups.md`](docs/backups.md) | Two layers: Borg file-level, Veeam image-level |
| [`docs/veeam-linux-repository.md`](docs/veeam-linux-repository.md) | Using the Linux box as a Veeam repository, and why the SMB workaround costs you |
| [`docs/AUDIT.md`](docs/AUDIT.md) | **Findings from auditing this setup against the live machine** |
| [`scripts/`](scripts/) | Host scripts (patched — see AUDIT) |
| [`systemd/`](systemd/) | Units for the above |
| [`docker/`](docker/) | Compose files for the whole stack, secrets parameterised |
| [`veeam/`](veeam/) | Guest pre-freeze / post-thaw scripts (patched) |
| [`grafana/`](grafana/) | Exported alert rules — Grafana owns alerting here |

---

## Hardware

| Component | Part |
|---|---|
| CPU | AMD Ryzen 9 5950X — 16C/32T, two CCDs, 32MB L3 per CCD |
| GPUs | 2x ASUS ROG Strix RTX 3090, NVLink bridged (4 links, ~14 GB/s each) |
| Memory | 64GB DDR4-3600 (G.SKILL Trident Z) |
| Motherboard | MSI MEG X570 GODLIKE |
| Cooling | Arctic Liquid Freezer III Pro 420, GPUs repasted |
| PSU | Corsair HX1500i (ATX 3.1) |
| UPS | CyberPower OL1500RTXL2U, RMCARD205 management card |
| Networking | Onboard Realtek Killer E3000 2.5GbE (RJ45), linked at 2500Mb/s |
| Chassis | NZXT H9 Flow (2025) |
| OS | Ubuntu 24.04.4 LTS, kernel 6.8 |

> **No expansion slots remain.** Two triple-slot ROG Strix 3090s bridged with
> NVLink physically occupy everything, and AM4 only has 16 CPU PCIe lanes to
> begin with — both cards run at **x8** (`current_link_width: 8`, max 16) once
> the second slot is populated. There is nowhere to put a 10GbE NIC, so the
> box uses the onboard 2.5GbE. In practice that is not the bottleneck: model
> weights live on local NVMe, and 2.5Gb is ample for API traffic and the
> Cloudflare tunnel.
>
> If you are planning a similar build and want 10GbE, budget for it on a
> platform with more lanes (Threadripper / EPYC / SP3) or accept a single GPU.

---

## The core idea

The 5950X is two 8-core dies that do not share L3 cache. Anything crossing
between them pays a latency penalty through the I/O die. So the machine is
partitioned along that physical boundary and nothing is allowed to drift across
it:

```
CCD0 — cores 0-7  (L3 pool #0)        CCD1 — cores 8-15 (L3 pool #1)
├─ 0-4  inference  (+SMT 16-20)       └─ 8-15 everything else (+SMT 24-31)
├─ 5-6  web/ingress (+SMT 21-22)         postgres, qdrant, embeddings,
└─ 7    GPU interrupts (+SMT 23)         reranker, tika, agents, monitoring
```

Inference gets five physical cores and an uncontended slice of L3. The RAG
stack — vector search, chunking, database writes, document parsing — is
cache-hostile and I/O-heavy by nature, so it is exiled to the other die where
it can thrash as hard as it likes without evicting a single line of the
inference working set.

This is enforced with `cpuset` in Compose, verifiable at any time:

```bash
docker inspect -f '{{.Name}} {{.HostConfig.CpusetCpus}}' $(docker ps -q)
```

> **The SMT trap.** On this CPU the sibling of core *N* is thread *N+16*, not
> *N+1*. `cpuset: "0-4"` gives you five *logical* CPUs — half of what you think
> you're getting. The correct string for five physical cores is `"0-4,16-20"`.
> This repo shipped with that bug; see [AUDIT #2](docs/AUDIT.md).

---

## Inference

`llama-swap` fronts `ik-llama-server`. llama-swap is the router: it exposes one
OpenAI-compatible endpoint, launches the right backend process on demand, and
unloads it after a TTL. `ik-llama-server` does the actual tensor work.

Both cards are driven with `--split-mode graph` over the NVLink bridge, pooling
48GB of VRAM so a 27B dense model at Q8 fits with a very large context.

Measured on this machine, 27B dense at Q8_0, 262k context:

| | |
|---|---|
| Generation | **~46 tok/s** |
| Prompt processing | **~1118 tok/s** |

See [`docs/inference.md`](docs/inference.md) for the full configuration.

---

## Service stack

All of it is in [`docker/`](docker/), secrets parameterised, one directory per unit.

- **Frontend** — Open WebUI (CCD0, next to the engine)
- **Data** — PostgreSQL (chat history), Qdrant (vectors)
- **RAG** — Infinity embeddings (CPU) + reranker (GPU), Apache Tika extraction
- **Ingress** — `cloudflared` Zero Trust tunnel, no inbound ports opened
- **Observability** — Prometheus, Grafana, node-exporter, dcgm-exporter,
  snmp-exporter, plus a small custom exporter for llama-swap tokens/sec
- **Backups** — two layers: Borg nightly with Postgres/Qdrant pre-hooks, plus
  Veeam image-level backup with guest freeze/thaw scripts

---

## Power

Two 3090s at their stock 480W ceiling is ~960W of GPU alone, which a 1500VA
UPS will not enjoy. Both cards are capped to **270W** via `nvidia-smi`, which
costs only a few percent on memory-bandwidth-bound inference and removes ~420W
of transient draw.

The UPS reports its own rated capacity as 1350W. Measured draw at idle with
models unloaded is 163W (12% load), leaving comfortable headroom under load.

> The commonly repeated "1350W limit enforced via nvidia-smi" is a conflation of
> two different numbers: 1350W is the UPS's rated output, and the `nvidia-smi`
> cap is 270W per card. Nothing enforces a system-wide wattage ceiling.

---

## Using this

Nothing here is a turnkey installer, and you should not run it as one — the
core pinning values are specific to a 16-core dual-CCD Ryzen. Read
[`docs/cpu-topology.md`](docs/cpu-topology.md) first and derive your own layout
from `lscpu -e=CPU,CORE,CACHE`.

```bash
git clone https://github.com/<you>/dual-3090-llm-server
cd dual-3090-llm-server

# Host scripts
sudo install -m 0755 scripts/*.sh /usr/local/bin/
sudo install -m 0644 systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now optimize-ai.service optimize-ai.timer nvidia-power-limit.service

# Shared container network
docker network create ai-net

# Per-service: copy .env.example to .env and fill it in first
cd docker/open-webui && cp .env.example .env && $EDITOR .env && docker compose up -d
```

**Every `.env.example` needs real values before anything will start.** No
credentials are committed to this repository, and `.gitignore` is set up to
keep it that way.

---

## Contributing

Corrections are especially welcome. If something here is wrong — and given the
audit results, more of it may be — open an issue. Measurements beat reasoning
about cache hierarchies, and a counter-example with `perf` output will always
win the argument.

## License

MIT — see [LICENSE](LICENSE).
