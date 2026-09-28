"""Prometheus exporter for Smart Life (Tuya) devices, via the Smart Life sharing API.

Needs data/auth.json from login.py. Polls each home's device list (one request
per home) every POLL_SECONDS; re-reads device specs (for value scaling) every
SPEC_SECONDS. Refreshed tokens are written back to auth.json.
"""
import json
import logging
import os
import time
import types

from prometheus_client import Counter, Gauge, start_http_server
from tuya_sharing import Manager, SharingTokenListener

from login import AUTH_PATH, CLIENT_ID

PORT = int(os.environ.get("PORT", "9106"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
SPEC_SECONDS = int(os.environ.get("SPEC_SECONDS", "1800"))
TOPOLOGY_PATH = os.environ.get("TOPOLOGY_PATH", "/app/topology.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smartlife")

L = ["device_id", "name"]
info = Gauge("smartlife_device_info", "Device metadata", L + ["category", "product", "home"])
online = Gauge("smartlife_device_online", "1 if the cloud reports the device online", L)
power = Gauge("smartlife_power_watts", "Instantaneous power draw", L)
voltage = Gauge("smartlife_voltage_volts", "Mains voltage", L)
current = Gauge("smartlife_current_amps", "Current draw", L)
energy = Counter("smartlife_energy_kwh", "Energy integrated from power samples (resets on exporter restart)", L)
switch = Gauge("smartlife_switch_on", "Relay state per switch dp", L + ["code"])
dp_value = Gauge("smartlife_dp_value", "Every numeric/boolean dp, scaled", L + ["code", "unit"])
up = Gauge("smartlife_up", "1 if the last poll succeeded")
topology = Gauge("smartlife_device_topology",
                 "1 per device; parent is empty for top-level (billed) meters, children are included in their parent",
                 L + ["parent", "level", "circuit", "planned_circuit"])
circuit_rating = Gauge("smartlife_circuit_rating_amps", "Breaker rating from topology.json")
last_poll = Gauge("smartlife_last_success_timestamp_seconds", "Unix time of the last successful poll")


class TokenSaver(SharingTokenListener):
    def __init__(self, auth):
        self.auth = auth

    def update_token(self, token_info):
        self.auth.update(token_info)
        tmp = AUTH_PATH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.auth, f, indent=1)
        os.replace(tmp, AUTH_PATH)
        log.info("token refreshed")


def load_topology():
    """name -> (parent, circuit, planned_circuit); children inherit the parent's circuits."""
    try:
        with open(TOPOLOGY_PATH) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {}
    circuit_rating.set(cfg.get("circuit_amps", 15))
    devs = cfg.get("devices", {})

    def root(name, seen=()):
        parent = devs.get(name, {}).get("parent")
        return name if not parent or parent in seen else root(parent, seen + (name,))

    out = {}
    for name, d in devs.items():
        top = devs.get(root(name), {})
        out[name] = (d.get("parent") or "", top.get("circuit", ""), top.get("planned_circuit", ""))
    return out


def scale_info(device, code):
    """(divisor, unit) for a dp from the device spec, e.g. cur_power scale 1 -> /10."""
    rng = getattr(device, "status_range", {}).get(code)
    if rng is None:
        return 1, ""
    try:
        v = json.loads(rng.values)
        return 10 ** int(v.get("scale", 0)), v.get("unit", "")
    except (ValueError, TypeError, AttributeError):
        return 1, ""


class Poller:
    def __init__(self, auth):
        self.mgr = Manager(CLIENT_ID, auth["user_code"], auth["terminal_id"],
                           auth["endpoint"], auth, TokenSaver(auth))
        self.specs = {}      # device_id -> CustomerDevice with status_range
        self.homes = {}      # home_id -> name
        self.spec_at = 0
        self.last = {}       # device_id -> (time, watts) for energy integration
        self.topology = {}

    def refresh_specs(self):
        # Not Manager.update_device_cache(): it also fetches HA local-control
        # strategy, whose custom-type endpoint now fails with 1108 and aborts
        # the whole refresh. Only homes and per-device scale specs are needed.
        api, repo = self.mgr.customer_api, self.mgr.device_repository
        self.homes = {h.id: h.name for h in self.mgr.home_repository.query_homes()}
        specs = {}
        for home_id in self.homes:
            resp = api.get("/v1.0/m/life/ha/home/devices", {"homeId": home_id})
            for item in resp.get("result", []) if resp.get("success") else []:
                dev = types.SimpleNamespace(id=item["id"], status_range={})
                try:
                    repo.update_device_specification(dev)
                except Exception as e:
                    log.warning("spec for %s failed: %r", item.get("name"), e)
                specs[dev.id] = dev
        self.specs = specs
        self.spec_at = time.time()
        self.topology = load_topology()
        topology.clear()
        info.clear()
        log.info("specs: %d devices in %d homes", len(self.specs), len(self.homes))

    def poll(self):
        if time.time() - self.spec_at > SPEC_SECONDS:
            self.refresh_specs()
        now = time.time()
        for home_id, home_name in self.homes.items():
            resp = self.mgr.customer_api.get("/v1.0/m/life/ha/home/devices", {"homeId": home_id})
            if not resp.get("success"):
                raise RuntimeError(f"device list failed: {resp.get('code')} {resp.get('msg')}")
            for item in resp["result"]:
                self.export(item, home_name, now)

    def export(self, item, home_name, now):
        dev_id, name = item["id"], item.get("name", item["id"])
        spec = self.specs.get(dev_id)
        if spec is None:          # new device: pick up its spec next cycle
            self.spec_at = 0
            spec = item
        lbl = (dev_id, name)
        info.labels(*lbl, item.get("category", ""), item.get("product_name", ""), home_name).set(1)
        parent, circ, planned = self.topology.get(name, ("", "", ""))
        topology.labels(*lbl, parent, "sub" if parent else "top", circ, planned).set(1)
        is_online = bool(item.get("online"))
        online.labels(*lbl).set(int(is_online))

        # Older plugs (e.g. "Smart 15EM Plug-In") report V and W in tenths while
        # their spec says scale 0; mains voltage over 300 gives them away.
        deci = set()
        for st in item.get("status", []):
            if st.get("code") == "cur_voltage" and isinstance(st.get("value"), (int, float)):
                if st["value"] / scale_info(spec, "cur_voltage")[0] > 300:
                    deci = {"cur_voltage", "cur_power"}

        watts = None
        for st in item.get("status", []):
            code, val = st.get("code"), st.get("value")
            if isinstance(val, bool):
                if code.startswith("switch"):
                    switch.labels(*lbl, code).set(int(val))
                dp_value.labels(*lbl, code, "").set(int(val))
                continue
            if not isinstance(val, (int, float)):
                continue
            div, unit = scale_info(spec, code) if hasattr(spec, "status_range") else (1, "")
            if code in deci:
                div *= 10
            x = val / div
            dp_value.labels(*lbl, code, unit).set(x)
            if code == "cur_power":
                watts = x
                power.labels(*lbl).set(x)
            elif code == "cur_voltage":
                voltage.labels(*lbl).set(x)
            elif code == "cur_current":
                current.labels(*lbl).set(x / 1000 if unit.lower() == "ma" or not unit else x)

        if watts is not None and is_online:
            prev = self.last.get(dev_id)
            if prev and now - prev[0] < 10 * POLL_SECONDS:
                energy.labels(*lbl).inc((prev[1] + watts) / 2 * (now - prev[0]) / 3.6e6)
            self.last[dev_id] = (now, watts)
        else:
            self.last.pop(dev_id, None)


def main():
    while not os.path.exists(AUTH_PATH):
        log.warning("no %s yet; run login.py (see its docstring). Rechecking in 30s.", AUTH_PATH)
        time.sleep(30)
    with open(AUTH_PATH) as f:
        auth = json.load(f)
    poller = Poller(auth)
    start_http_server(PORT)
    log.info("listening on :%d, polling every %ds", PORT, POLL_SECONDS)
    while True:
        try:
            poller.poll()
            up.set(1)
            last_poll.set(time.time())
        except Exception:
            log.exception("poll failed")
            up.set(0)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
