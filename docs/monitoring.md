# Observability

`nvidia-smi` in a loop tells you almost nothing useful. It samples the wrong
things at the wrong interval and keeps no history, so it cannot answer the
questions that actually come up: *was that slowdown thermal or was the model
swapped?* *Is the UPS near capacity when both cards spin up?* *Did tokens/sec
regress after that config change?*

## The stack

| Component | Port | Answers |
|---|---|---|
| Prometheus | 2090 | Stores everything, evaluates alert rules |
| Grafana | 2000 | Dashboards |
| `node-exporter` | 9100 | CPU per core, RAM, disk I/O, per-CCD temps |
| `dcgm-exporter` | 2400 | VRAM, utilisation, per-GPU watts, throttle reasons |
| `snmp-exporter` | 9116 | UPS load, battery, runtime |
| `llamaswap-exporter` | 9105 | tokens/sec, cache hits, active model |

All of it pinned to CCD1. Monitoring that perturbs the thing it measures is
worse than no monitoring.

## Per-CCD temperatures: zenpower3

The in-tree `k10temp` driver reports a single aggregate die temperature on
Ryzen, which is useless when the entire point of your configuration is that
the two CCDs are doing different work.

[`zenpower3`](https://github.com/ocerman/zenpower3) exposes per-CCD
temperature, per-core power and current. That lets you see whether the
inference die is running hotter than the database die — and it is the only way
to confirm that a thermal excursion is coming from where you think it is.

```bash
sudo apt install dkms build-essential
git clone https://github.com/ocerman/zenpower3 && cd zenpower3
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
headroom is what justifies the 270W-per-card cap rather than the stock 480W —
see [the power section of the README](../README.md#power).

Test a target without waiting for a scrape:

```bash
curl -s 'http://localhost:9116/snmp?module=cyberpower&auth=cyberpower_v1&target=<UPS_IP>' \
  | grep -E 'upsBaseOutputStatus|upsAdvanceOutputLoad'
```

## Alerting

Rules are in [`docker/monitoring/gpu-alert.yaml`](../docker/monitoring/gpu-alert.yaml).
They cover core and memory temperature, VRAM exhaustion, real throttling,
exporter and engine liveness, and UPS state.

**Three things to check before trusting any of it**, all of which were wrong
here before the audit ([#4](AUDIT.md#4-every-gpu-alert-rule-was-dead-config)):

1. **The rules are actually loaded.** A `rule_files:` stanza in
   `prometheus.yml` *and* a volume mount into the container. Confirm at
   `/rules` in the Prometheus UI — an empty page means zero rules, which is
   indistinguishable from "nothing is wrong" until something is.
2. **VRAM percentage divides by total.** `used / (used + free)`. Dividing by
   *free* produces values over 100% and an alert that is always firing.
3. **`clock_throttle_reasons` is a bitmask, not a count.** Bit `0x1` is "GPU
   is idle", so `> 0` fires continuously on an idle box. Bit `0x4` is "SW
   power cap", which is permanently expected if you cap your cards. PromQL has
   no bitwise AND, but the benign bits sum to 7, so `>= 8` cleanly isolates
   genuine hardware and thermal slowdowns.

Rules evaluate and display in the UI without an Alertmanager, but nothing is
delivered anywhere until you run one. There is a commented `alerting:` block in
`prometheus.yml`.

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
