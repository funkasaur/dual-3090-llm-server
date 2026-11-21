# cAdvisor: per-container metrics (2026-10-30)

- Service `cadvisor` in [`../docker-compose.yaml`](../docker-compose.yaml): `ghcr.io/google/cadvisor:v0.60.6`, no host
  port (cAdvisor's UI has no auth; Prometheus reads it over `ai-net`), cpuset 8-15,24-31, 256m (uses ~25 MiB).
  Most metric groups are disabled (`--disable_metrics=...`); CPU, memory and network per container are what's left.
- **Version note:** gcr.io/cadvisor stops at v0.52.x and cannot read Docker 29's containerd image store (driver "overlayfs"):
  it only exports root-cgroup series and logs `failed to identify the read-write layer ID`. Snapshotter support landed in
  v0.54; images moved to ghcr.io.
- Prometheus job `cadvisor` (15 s). Series carry `name` = container name (`--docker_only`, labels not stored).
- Grafana dashboard **Containers** (`uid containers`): [`grafana/dashboards/containers.json`](../../../grafana/dashboards/containers.json),
  top containers by CPU/RAM/network/disk I/O with a container picker. Alert `CadvisorDown` if the job goes unscraped.
