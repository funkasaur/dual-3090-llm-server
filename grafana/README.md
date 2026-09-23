# Grafana alert rules

Exported from the live Grafana with:

```bash
curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" \
  http://localhost:2000/api/v1/provisioning/alert-rules > grafana/alert-rules.json
```

Grafana owns alerting on this build rather than Prometheus, because Grafana can
deliver notifications on its own and Prometheus rules cannot without an
Alertmanager. The rules live in Grafana's database, so this export is the only
reviewable copy - restore with a POST per rule to the same endpoint.

| Severity | Alert | Expression | Threshold |
|---|---|---|---|
| warning | `CpuDieTemperatureHigh` | `node_hwmon_temp_celsius{chip="pci0000:00_0000:00:18_3",sensor="temp1"}` | > 85 |
| warning | `GpuCoreTemperatureWarning` | `DCGM_FI_DEV_GPU_TEMP` | > 80 |
| critical | `GpuCoreTemperatureCritical` | `DCGM_FI_DEV_GPU_TEMP > 84` | > 0 |
| critical | `GpuExporterDown` | `up{job="dcgm"} == 0` | > 0 |
| warning | `GpuJunctionTemperatureHigh` | `gpu_junction_temp_celsius > 90` | > 0 |
| warning | `GpuPowerCapNotApplied` | `DCGM_FI_DEV_POWER_USAGE > 290` | > 0 |
| warning | `GpuTempCollectorStale` | `time() - gpu_temps_last_update_timestamp_seconds > 300` | > 0 |
| warning | `GpuVramExhausted` | `(DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE)) * 10…` | > 0 |
| warning | `GpuVramTemperatureHigh` | `gpu_vram_temp_celsius > 95` | > 0 |
| critical | `GpuXidErrors` | `increase(DCGM_FI_DEV_XID_ERRORS[15m]) > 0` | > 0 |
| critical | `InferenceEngineDown` | `up{job="llama-swap"} == 0` | > 0 |
| warning | `NodeExporterDown` | `up{job="node"} == 0` | > 0 |
| warning | `UpsBatteryAbnormal` | `upsBaseBatteryStatus != 2` | > 0 |
| warning | `UpsLoadHigh` | `upsAdvanceOutputLoad > 75` | > 0 |
| critical | `UpsOnBattery` | `upsBaseOutputStatus != 2` | > 0 |
| critical | `UpsRuntimeLow` | `upsAdvanceBatteryRunTimeRemaining / 6000 < 5` | > 0 |
| warning | `HostMemoryHigh` | `clamp_min((1 - (node_memory_MemAvailable_bytes{instance="node-exporter:9…` | > 85 |

## Notes

- Metric names from dcgm-exporter are **UPPERCASE**; PromQL is case-sensitive and
  the lowercase form silently matches nothing.
- `gpu_junction_temp_celsius` / `gpu_vram_temp_celsius` come from
  `gpu-vram-temps.timer` on the host, not from dcgm-exporter, which does not
  expose those fields. `GpuTempCollectorStale` alerts if that timer stops.
- CPU sensors on the zenpower chip: `temp1`=Tdie, `temp2`=Tctl, `temp3`=Tccd1
  (inference die), `temp4`=Tccd2 (RAG die).
- Thresholds are set to avoid firing under normal sustained load. A 3090 at 75C
  and a 5950X at 80C are both working normally, not in trouble.
