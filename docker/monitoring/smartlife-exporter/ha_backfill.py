"""Home Assistant long-term statistics -> smartlife_power_watts_history (OpenMetrics, 5-min samples).
15EM plugs: HA reported tenths while hourly mean voltage > 300 V (integration versions varied); those
hours are divided by 10 and hours straddling a switch (130-900 V mean) are dropped."""
import json, time, urllib.request
END = int(time.mktime(time.strptime("2026-09-26 21:07", "%Y-%m-%d %H:%M")))
d = json.load(open("ha_stats.json"))
ids = {m["metric"]["name"]: m["metric"]["device_id"] for m in json.load(urllib.request.urlopen(
    "http://localhost:2090/api/v1/query?query=smartlife_device_topology"))["data"]["result"]}
MAP = {"server": "Server", "smart_15em_plug_in_4": "Office", "smart_15em_plug_in_5": "Desktop", "tv_and_audio": "TV and Audio",
       "docking_station": "Brocade Switch", "laptop": "Optiplex 7070 SFF", "new_plugin_2": "Desk"}
def scaled(p, period):
    P = {r["start"] // 1000: r["mean"] for r in d[period].get(f"sensor.{p}_power", []) if r.get("mean") is not None}
    V = {r["start"] // 1000: r["mean"] for r in d[period].get(f"sensor.{p}_voltage", []) if r.get("mean") is not None}
    return {t: P[t] / (10 if V[t] > 300 else 1) for t in P if t in V and not 130 < V[t] < 900}
series = {}
for p, name in MAP.items():
    fine = scaled(p, "5minute"); cut = min(fine) if fine else END
    pts = {}
    for t, w in scaled(p, "hour").items():
        if t + 3600 <= cut:
            for k in range(12): pts[t + 300 * k] = w
    pts.update(fine)
    series[name] = {t: w for t, w in pts.items() if t < END and 0 <= w < 1900}
# Internet and Den: hourly kWh change -> mean watts
pts = {}
for r in d["hour_energy"]["sensor.internet_and_den_total_energy"]:
    ch = r.get("change")
    if ch is None or not 0 <= ch < 1.8: continue
    t = r["start"] // 1000
    for k in range(12):
        if t + 300 * k < END: pts[t + 300 * k] = ch * 1000
series["Internet and Den"] = pts
with open("ha_backfill.om", "w") as f:
    f.write("# HELP smartlife_power_watts_history Plug power from Home Assistant statistics (5-min / hourly means)\n")
    f.write("# TYPE smartlife_power_watts_history gauge\n")
    for name, pts in series.items():
        lbl = f'device_id="{ids[name]}",instance="smartlife-exporter:9106",job="smartlife",name="{name}"'
        for t in sorted(pts): f.write(f"smartlife_power_watts_history{{{lbl}}} {pts[t]:.2f} {t}\n")
    f.write("# EOF\n")
for name, pts in series.items():
    print(f"{name:18} {len(pts):7} samples  {time.strftime('%Y-%m-%d', time.localtime(min(pts)))} .. {time.strftime('%Y-%m-%d %H:%M', time.localtime(max(pts)))}  kWh {sum(pts.values())/12/1000:.0f}")
