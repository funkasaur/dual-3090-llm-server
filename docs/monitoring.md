# Observability

`nvidia-smi` in a loop tells you almost nothing useful. It samples the wrong
things at the wrong interval and keeps no history, so it cannot answer the
questions that actually come up: *was that slowdown thermal or was the model
swapped?* *Is the UPS near capacity when both cards spin up?* *Did tokens/sec
regress after that config change?*

## The stack

| Component | Port | Answers |
|---|---|---|
| Prometheus | 2090 | Stores everything (alert rules run in Grafana) |
| Grafana | 2000 | Dashboards and alerting (Telegram) |
| `node-exporter` | 9100 | CPU per core, RAM, disk I/O, per-CCD temps; since 2026-11-15 also processes, tcpstat, interrupts (`--path.procfs=/host/proc`, because Docker masks `/proc/interrupts` in containers; ~5k extra series) |
| `dcgm-exporter` | 2400 | VRAM, utilization, per-GPU watts, throttle reasons |
| `snmp-exporter` | 9116 | UPS load, battery, runtime |
| `llamaswap-exporter` | 9105 | per-request speed, context, cache hits, active model ([standalone repo](https://github.com/funkasaur/llamaswap-exporter)); llama-swap >= v269 counts cache inside `input_tokens` (fixed 2026-11-21) |
| `smartctl-exporter` | 9633 | NVMe wear, spare, media errors, temperature (v0.15.0) |
| `sfp-dom` | (textfile) | 10G SFP+ module temperature and DOM via `ethtool -m` |

All of it pinned to CCD1. Monitoring that perturbs the thing it measures is
worse than no monitoring.

## Per-CCD temperatures: zenpower5

The in-tree `k10temp` driver reports a single aggregate die temperature on
Ryzen, which is useless when the entire point of your configuration is that
the two CCDs are doing different work.

[`zenpower5`](https://github.com/mattkeenan/zenpower5) exposes per-CCD
temperature, plus SVI2 core and SoC voltage, current and power. That lets you
see whether the inference die is running hotter than the database die — and it
is the only way to confirm that a thermal excursion is coming from where you
think it is.

zenpower5 is a newer fork than `zenpower3`: it adds RAPL power and Zen 4/5
support. On a Zen 3 part both read the same SVI2 sensors. The module is still called `zenpower`; this box runs 0.5.0.

```bash
sudo apt install dkms build-essential
git clone https://github.com/mattkeenan/zenpower5 && cd zenpower5
sudo dkms install .
echo zenpower | sudo tee /etc/modules-load.d/zenpower.conf

# k10temp claims the hardware first; it has to go
echo "blacklist k10temp" | sudo tee /etc/modprobe.d/blacklist-k10temp.conf

sensors | grep -A5 zenpower
```

`node-exporter` picks it up automatically through hwmon.

## GPU telemetry

`dcgm-exporter` is worth it over scraping `nvidia-smi` for two metrics in
particular:

- **`dcgm_fi_dev_memory_temp`** — GDDR6X memory junction temperature. This is
  the number that kills 3090s. Memory runs far hotter than the core and
  throttles at 110°C; a card showing a comfortable 70°C core can be sitting at
  100°C+ on the backside modules. If this is routinely high, the thermal pads
  need replacing. `nvidia-smi` does not surface it.
- **`dcgm_fi_dev_clock_throttle_reasons`** — a bitmask explaining *why* clocks
  dropped, which distinguishes "hit the power cap I configured" from "thermal
  emergency". See the alerting note below, because this field is easy to get
  wrong.

## UPS telemetry

Three UPSes, all with RMCARD205 cards: this server, the desktop, and the network/server rack
(a CP2000PFCRM2U).

The CyberPower RMCARD205 speaks SNMP. `snmp-exporter` ships a built-in
`cyberpower` module; you supply only credentials, in `auth.yml`:

```yaml
auths:
  cyberpower_v1:
    version: 1
    community: CHANGE_ME
```

> SNMPv1/v2c community strings cross the wire in cleartext and are effectively
> a shared password. Use SNMPv3 (`authPriv`) if your card supports it — there
> is a commented example in `auth.example.yml`. Never leave it at `public`.

Useful metrics, with values verified against a live RMCARD205:

| Metric | Meaning |
|---|---|
| `upsBaseOutputStatus` | `2` = on mains. Anything else means you are on battery |
| `upsBaseBatteryStatus` | `2` = normal |
| `upsAdvanceOutputLoad` | Load as % of rated capacity |
| `upsAdvanceOutputPower` | Current draw in watts |
| `upsAdvanceIdentLoadPower` | The card's rated output — 1350W here |
| `upsAdvanceBatteryRunTimeRemaining` | TimeTicks (1/100 s) — divide by 6000 for minutes |

Measured on this machine at idle with models unloaded: 163W, 12% load. That
headroom is what justifies the 270W-per-card cap rather than the 390W default (480W maximum) —
see [the power section of the README](../README.md#power).

Test a target without waiting for a scrape:

```bash
curl -s 'http://localhost:9116/snmp?module=cyberpower&auth=cyberpower_v1&target=<UPS_IP>' \
  | grep -E 'upsBaseOutputStatus|upsAdvanceOutputLoad'
```

## dcgm-exporter 4.8.4 and per-container metrics (2026-10-30)

- **dcgm-exporter 2.3.2 → 4.8.4** (`nvcr.io/nvidia/k8s/dcgm-exporter:4.8.4`, mem 1g; it now embeds the DCGM host engine,
  ~420 MiB). Metric list = [`docker/monitoring/dcgm/counters.csv`](../docker/monitoring/dcgm/counters.csv) (upstream default
  minus the driver-version label, plus XID/clock-event counters and `DCGM_FI_DEV_CLOCKS_EVENT_REASONS`).
  Breaking upstream changes handled: label `Hostname` → `hostname` (3 dashboards updated); XID errors carry an `err_msg`
  label, so the alert is now `sum by (gpu) (DCGM_EXP_XID_ERRORS_COUNT) > 0` instead of `increase(DCGM_FI_DEV_XID_ERRORS[15m])`.
  `DCGM_FI_DEV_MEMORY_TEMP` is off again: on the RTX 3090 it reads 0, so the section below still applies.
  `FB_USED` now excludes reserved VRAM (~450 MiB), so used/(used+free) reads a hair lower than before.
- **cAdvisor** for per-container CPU/RAM/network (same day): see [`docker/monitoring/cadvisor/README.md`](../docker/monitoring/cadvisor/README.md).

## GPU memory temperature and fan curve (2026-10-01)

Correction to the section above on this build: dcgm-exporter 2.3.2's default counters do **not**
include memory temperature, so VRAM and junction temperatures come from
[`gpu-vram-temps.sh`](../scripts/gpu-vram-temps.sh) (`gputemps`, reads the card's PCI BAR, needs root; build and install it to `/usr/local/bin/gputemps` from
[gddr6-core-junction-vram-temps](https://github.com/ThomasBaruzier/gddr6-core-junction-vram-temps) per its README.
Reading `/dev/mem` fails when the kernel is in lockdown mode, which Secure Boot usually enables)
as `gpu_vram_temp_celsius` / `gpu_junction_temp_celsius` / `gpu_core_temp_celsius`.

**What was wrong.** The two ROG Strix 3090s sit stacked for NVLink with little room between them. Under a
long Qwen run at the 270 W cap the core reads 55-62 C, so the driver's fan curve (which follows the core)
held the fans at 54-66 %, while GPU 1's VRAM ran 88-94 C for about half the day (peaks 96 C on Sep 24-25;
GPU 0 peaked 86 C). Worse, the Strix zero-RPM mode keeps the fans **stopped** until the core reaches
~50-55 C: GPU 0 was seen drawing 210 W at 0 % fan while its VRAM climbed 52 -> 72 C in about a minute.
`nvidia-smi` shows only the core temperature, so none of this was visible there.

**What runs now**

| Piece | What it does |
|---|---|
| `gpu-vram-temps.timer` | samples every **2 s** (was 30 s), `AccuracySec=500ms`; `LogLevelMax=notice` keeps the ~43k runs/day out of the journal (verified: 0 lines) |
| [`gpu-fan-curve.py`](../scripts/gpu-fan-curve.py) (`gpu-fan-curve.service`, root) | every 2 s per card: fan % = max of curves on VRAM (80->60 %, 86->72, 90->85, 94->100), junction, core and **power** (120 W->40 %, 200->50, 270->60, so fans start with the load, not after zero-RPM lets go). GPU 1 gets a +4 C head start. Rises at once, falls 1 %/2 s, hands back to the driver after 60 s below every curve. Readings > 15 s old -> fixed 80 %. Stop/crash -> fans back to driver auto. `--test` ramps 60/70/80/90 % for 40 s and checks the hand-back |
| [`gpu-watch`](../scripts/gpu-watch) | live terminal table (no sudo): VRAM / junction / core, fan %, RPM per channel, power, curve target. `--once` for one snapshot |
| Alerts | VRAM > 92 C for 1 min (was > 95 / 2 min), junction > 88 / 1 min (was > 90 / 5 min), core critical > 84 / 1 min, core warning > 80 / 2 min, temps stale > 30 s / 1 min (was 300 s / 5 min) |

Hardware facts found on the way: NVML exposes **2 fan channels** per Strix card for its 3 fans (one channel
drives two; both set together, all three spin), range 30-100 %. RPM per channel at 60/70/80/90 %:
~1350 / 1710 / 2100 / 2520. At 30 % the fans stop rather than turn slowly.

### Case fan tests before the 2026-10-21 rewire

On the 2026-10-01 wiring, fan1 ran at 100 % and fan2-5 followed the BIOS SmartFan curve.

**2026-10-01 intake test.** fan3 + fan5 at 100 % for 5 min under steady full load (both GPUs 269 W),
full-load seconds only, against the 3 minutes before: GPU 1 VRAM 86.0 -> **86.0 C (no change)**,
GPU 1 junction about -2 C, GPU 1 core -2 C, board AUXTIN2 51 -> 47 C, GPU 0 and CPU unchanged, GPU 1
fan target 85.2 -> 84.1 %. The intakes cool the core / junction / board, not GPU 1's back-side memory,
which faces the gap between the cards. At the time, the next test was to set the GPU-gap fan to 100 % in the BIOS.

**2026-10-07 BIOS test.** GPU-gap fan at 100 % and rear exhaust on SmartFan (PCIe 1 temperature), set at the 11:13 boot.
The test showed no measurable change. Sustained 240 W+ seconds, modal VRAM before (Oct 6 11:13-12:00, Oct 7 07:00-10:55) vs after
(11:13-11:57, ~15 min of full load): GPU 0 78 -> 78 C, GPU 1 84 -> 84 C (86 next), GPU 1 fan target 79 -> 79 %,
core/junction within 1-2 C. Both headers were on the NUC126, so Linux could not read their RPM.
The PCIe 1 sensor is likely a board temperature that barely moves, so the exhaust may not ramp.
The sample could show a change of 4 C or more. Other adjustments considered were GPU 1 power,
memory clock, and back-side thermal pads.

### Case fans (MSI MEG X570 GODLIKE, NZXT H9 Flow RGB) — remapped 2026-10-21 after a rewire

The board has 9 fan headers (PUMP_FAN1, CPU_FAN1, SYS_FAN1-7) but Linux sees only the Nuvoton
NCT6797D's 7 channels; on MSI's MEG X570 boards the remaining headers sit on an MSI **NUC126**
microcontroller with no Linux driver (LibreHardwareMonitor issue #505 still open; HWiNFO can read it on
Windows). On 2026-10-21 (Arctic Liquid Freezer III Pro 420 pump head flipped, M.2 SFP+ card fitted)
the owner rewired every fan onto PUMP_FAN1, CPU_FAN1 and SYS_FAN1-4; SYS_FAN5-7 are empty. The channels
follow the headers in board order, so every fan is now visible and controllable from Linux.

| nct6797 channel | Header | What | How known |
|---|---|---|---|
| fan1 | PUMP_FAN1 | AIO pump (fixed ~2800 rpm at 100 %) | header order; already at 100 % |
| fan2 | CPU_FAN1 | fan on the AIO pump head (2364 rpm at 100 %; duty follows CPU temp) | burst, by elimination |
| fan3 | SYS_FAN1 | AIO radiator fans, 3x 140 mm (2547 rpm at 100 %) | burst, by elimination |
| fan4 | SYS_FAN2 | 3x P14 Pro side intake + P12 Pro rear exhaust (rated 2500 / 3000; reads 2270-2364 at 100 %, so the tach is a P14) | burst, seen |
| fan5 | SYS_FAN3 | 3x P14 Pro bottom intake (rated 2500; reads 2210-2257 at 100 %) | burst, seen |
| fan6 | SYS_FAN4 | 2x Thermalright TL-M12QW across and between the GPUs (was 1 before 10-21; rated 2000, reads 2257-2280 at 100 %; at 0 % they floor at ~595 rpm, never stop) | burst, seen; `6 --stop` 10-21: fan6 595 rpm floor, fan5 unchanged |
| fan7 | chipset | **X570 chipset fan** (5947 rpm at 100 %; BIOS curve stops it below 50 C) | tested 2026-10-01 |
| — | SYS_FAN5-7 | empty (NUC126, not visible) | by hand |

The 2026-10-01 map (fan3/fan5 = P14 sets, fan6 empty, GPU-gap fan hidden on the NUC126) is out of date.
Note SYS_FAN2 carries four fans on one header; its rpm reading is one fan's tach.

`scripts/case-fans-identify.py` maps channels: raise one at a time to 100 % (default), `--together`
(several at once), `--stop` / `--pulse` (lower, only fan4/5/6, with a CPU-temperature guard), `--hold N`,
`--lead N` (wait before the first channel), `--gap N` (pause between channels), all 1-300 s, `--dry-run`.
Restores each channel's mode; the duty then ramps back on the BIOS curve. Remote run (server in another
room): `sudo case-fans-identify.py 2 3 4 5 6 --lead 60 --hold 10 --gap 10` and count bursts in order.

Install / roll back:

```bash
sudo install -m 755 scripts/gpu-fan-curve.py /usr/local/bin/
sudo install -m 644 systemd/gpu-fan-curve.service systemd/gpu-vram-temps.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gpu-fan-curve.service && sudo systemctl restart gpu-vram-temps.timer
sudo systemctl disable --now gpu-fan-curve     # fans straight back to the driver
```

## Standby monitor (2026-11-15)

Grafana and Prometheus run on this server, so they go quiet with it. A second Prometheus + Grafana on a small
always-on machine scrapes the same targets and takes over alerting when this one dies:
- One `prometheus.yml` for both, so every target is a LAN address both can reach: this server's exporters are
  published on its LAN IP only (node 9100, dcgm 2400, smartctl 9633, cadvisor 8088; commented examples in
  `docker/monitoring/`) with `instance` relabelled to the old Docker names, so history and rules carry on.
  Jobs only one side runs go in `scrape_config_files: [/etc/prometheus/extra/*.yml]`. A timer on the standby
  pulls the file, validates it with `promtool`, reloads, and rolls back on failure.
- The standby Grafana is a copy of this one with every rule **paused**. A watchdog un-pauses them after 3 min of
  failed health checks here (`/api/health` plus fresh rule evaluations) and pauses them again after 5 healthy
  minutes, with a notice each way. A nightly sync copies rules, dashboards, policies, templates and mute timings
  (never contact points). Live failover test passed 2026-11-15.
- Code: [funkasaur/grafana-standby](https://github.com/funkasaur/grafana-standby).
- Noise trim (both sides): `node_systemd_unit_state` keeps active + failed only (in `prometheus.yml`).

## Alerting

**Grafana owns alerting on this build.** The live rules (164 at a 2026-11-15 audit: GPU, CCD, UPS, inference,
watchdog, backups, agents, home power, network) live in Grafana's database and
deliver to Telegram. [`grafana/alert-rules.json`](../grafana/alert-rules.json) is a 36-rule public subset (exported 2026-10-26; the table in [`grafana/README.md`](../grafana/README.md) also lists a few added since):
GPU, CPU, UPS, inference, watchdog, home power, disks and NVMe; the rest watch machines and services that
are not part of this repo.
Prometheus loads **no** rules: the `rule_files:` stanza and the `gpu-alert.yaml` mount are commented out
on purpose, so `/rules` in the Prometheus UI is empty and that is expected.
[`docker/monitoring/gpu-alert.yaml`](../docker/monitoring/gpu-alert.yaml) is kept as a Prometheus-format
reference for anyone running an Alertmanager instead.

**Three things to check before trusting any of it**, all of which were wrong
here before the audit ([#4](AUDIT.md#4-every-gpu-alert-rule-was-dead-config)):

1. **The rules are actually loaded.** In Grafana: Alerting → Alert rules, or
   `GET /api/v1/provisioning/alert-rules`. With Prometheus rules: a
   `rule_files:` stanza in `prometheus.yml` *and* a volume mount into the
   container, confirmed at `/rules` — an empty page means zero rules, which is
   indistinguishable from "nothing is wrong" until something is.
2. **VRAM percentage divides by total.** `used / (used + free)`. Dividing by
   *free* produces values over 100% and an alert that is always firing.
3. **`clock_throttle_reasons` is a bitmask, not a count.** Bit `0x1` is "GPU
   is idle", so `> 0` fires continuously on an idle box. Bit `0x4` is "SW
   power cap", which is permanently expected if you cap your cards. PromQL has
   no bitwise AND, but the benign bits sum to 7, so `>= 8` cleanly isolates
   genuine hardware and thermal slowdowns.

If you use the Prometheus rules instead: they evaluate and display in the UI
without an Alertmanager, but nothing is delivered anywhere until you run one
and add an `alerting:` block to `prometheus.yml`. That is why this build
moved alerting to Grafana, which delivers on its own.

**Datasource errors (2026-10-07).** After a reboot Prometheus stayed down for 25 min, and the rules with
`execErrState: Error` sent alerts named `GpuCoreTemperatureWarning`, `CpuDieTemperatureHigh` and
`CcdTemperatureHigh`, which looked like overheating. Every Prometheus-based rule now treats Error / NoData as
**OK**; one rule, `PrometheusUnreachable` (`vector(1)`, Alerting on error or no data, for 2 m, critical), is the
outage alert. Tested by stopping Prometheus for 3.5 min: only that rule fired, then resolved.

### LLM watchdog

HTTP health checks cannot see a server that answers 200 with the wrong content.
[`llm-watchdog`](../scripts/llm-watchdog.sh) probes the loaded main model's
`/tokenize` every 60 s and writes `llm_watchdog_tokenize_ok`,
`llm_watchdog_model_loaded`, `llm_watchdog_unloads_total` and
`llm_watchdog_last_probe_timestamp_seconds` through the node-exporter textfile
collector (atomically, like `gpu-vram-temps`). `InferenceEngineWedged` fires on
a failed probe or any watchdog unload; `LlmWatchdogStale` fires if the watchdog
itself stops reporting, so a dead watchdog cannot look like a healthy server.
The whole chain — probe, unload, metric, rule, Telegram — was tested by forcing
a failure (`EXPECTED_TOKENS=7 FAILS_TO_ACT=1`).

Since 2026-09-30 `llm_watchdog_unloads_total` counts only unloads llama-swap
answered with 2xx; other answers go to `llm_watchdog_unload_failures_total` and
the unload is retried on the next run. `llm_watchdog_router_up` is 0 when
`/running` could not be read, so the probe was skipped, not passed.
`InferenceEngineDown` (`up{job="llama-swap"} == 0`) is the alert for that case.

## Dashboard starting points

- Import **Grafana dashboard 12239** (NVIDIA DCGM) as a base and add the
  memory-junction temperature panel, which it omits.
- Plot `llamaswap_last_request_tokens_per_second` against
  `dcgm_fi_dev_gpu_temp` on one graph. Thermal throughput loss becomes obvious
  as soon as you can see the two lines together.
- Plot `upsAdvanceOutputPower` alongside per-GPU wattage to see exactly what a
  model load costs at the wall.

## A note on anonymous access

This stack enables anonymous Grafana viewers, which is convenient for a wall
display and fine on a trusted LAN. It also runs a Cloudflare tunnel, and the
distance between "LAN only" and "on the internet" is one dashboard route. If
you expose it, put Cloudflare Access in front.

## Updating the stack's images

Use the newest stable release tag from the project's own registry for image updates.
Most monitoring images and cloudflared received exact tags on 2026-11-15; the SNMP exporter, Wyoming, Open WebUI,
Postgres and llama-swap followed on 2026-11-21. Only the retired `docker/infinity` rollback file still uses `:latest`. What worked on 2026-10-29 (grafana 13.0.1 → 13.3.0, prometheus 3.12.0 → 3.15.1, node-exporter
1.11.1 → 1.12.1, plus open-webui and cloudflared) and again for the 11-15 pins:

1. Tag the running image for rollback: `docker tag $(docker inspect -f '{{.Image}}' <c>) <repo>:pre-update-<date>`.
2. Back up: compose file, `docker logs <c>` into `backups/logs/`, data volumes as a tar in a throwaway
   `alpine` container, then `gzip -t` every archive before going on. Grafana and Open WebUI run DB migrations
   on start, so their volume / `pg_dump` backups are the real rollback, not the image tag.
3. Bump the tag, `docker compose pull <service>`, then `docker compose up -d --no-deps <service>`, one service at a time.
4. Check Prometheus targets and `http://localhost:2090/-/ready` (host port 2090 maps to container port 9090).
   Check Grafana's `/api/health` and alert-rule health with an Editor service-account token, then check
   Open WebUI's `/health/db`.

Rollback: point the compose service at `<repo>:pre-update-<date>` and restore the volume tar.

Gotcha: in a backgrounded shell, `docker run --rm ... tar` can outlive the shell's "completed" notice. Check
for the tar container (`docker ps --filter ancestor=alpine:3`) and `gzip -t` the archive before trusting it.
