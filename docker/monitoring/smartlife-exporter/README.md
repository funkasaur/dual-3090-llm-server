# smartlife-exporter

Prometheus exporter (`:9106`, job `smartlife`) for the Smart Life / Tuya plugs, read through
the Smart Life sharing API (the QR-code login Home Assistant's Smart Life integration uses; no
Tuya developer project needed). Polls every 60 s: power, voltage, current, relay state, online.

- **Login (once, or when the token expires):**
  `docker compose run --rm smartlife-exporter python login.py <USER_CODE>`. The user code is in
  the Smart Life app under Me -> Settings -> Account and Security -> User Code. Tokens land in
  `data/auth.json` (gitignored) and refresh themselves.
- **Topology:** `topology.json` says which plug feeds which and which circuit each is on.
  Exported as `smartlife_device_topology`; the dashboard and alerts join on it.
- **Quirks handled:** tuya-device-sharing-sdk 0.2.15's `update_device_cache()` dies on a
  retired endpoint (1108), so specs are fetched directly; "Smart 15EM Plug-In" plugs report V
  and W in tenths while their spec says scale 0 (detected by voltage > 300).
- **Dashboard:** `python3 build_dashboard.py` writes `grafana-dashboard.json`; push it with the
  Grafana API. **Alerts:** `alerts/*.json` (also in `grafana/alert-rules.json`).
- **History:** see `BACKFILL.md`.
