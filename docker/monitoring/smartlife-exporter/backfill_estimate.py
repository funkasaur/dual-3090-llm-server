"""30-day estimate of per-plug power before the smartlife exporter existed.
UPS-backed: Plug 5 = 0.991 x UPS 192.0.2.114; Room A = that + 73.2 W; Room B = 1.031 x UPS 192.0.2.62.
Flat (tonight's average): the rest. Ratios/averages measured 2026-09-26 21:10-22:00."""
import json, time, urllib.request, urllib.parse
END = int(time.mktime(time.strptime("2026-09-26 21:06", "%Y-%m-%d %H:%M")))   # first real sample 21:07
START = (int(time.time()) - 30 * 86400 + 3600) // 60 * 60
def rng(q, s, e, step=60):
    out = {}
    for a in range(s, e, 10000 * step):
        b = min(a + 9999 * step, e)
        u = "http://localhost:2090/api/v1/query_range?" + urllib.parse.urlencode(dict(query=q, start=a, end=b, step=step))
        r = json.load(urllib.request.urlopen(u))["data"]["result"]
        if r: out.update({int(float(t)): float(v) for t, v in r[0]["values"]})
    return out
u114 = rng('avg_over_time(upsAdvanceOutputPower{instance="192.0.2.114"}[1m])', START, END)
u62 = rng('avg_over_time(upsAdvanceOutputPower{instance="192.0.2.62"}[1m])', START, END)
ids = {m["metric"]["name"]: m["metric"]["device_id"] for m in json.load(urllib.request.urlopen(
    "http://localhost:2090/api/v1/query?query=smartlife_device_topology"))["data"]["result"]}
FLAT = {"Room C": 168.9, "Plug 6": 56.1, "Plug 7": 35.0, "Plug 4": 30.3,
        "Plug 1": 84.3, "Plug 2": 7.2, "Plug 3": 0.0}
ts = range(START, END + 1, 60)
series = {n: {t: v for t in ts} for n, v in FLAT.items()}
series["Plug 5"] = {t: 0.991 * u114[t] for t in ts if t in u114}
series["Room A"] = {t: 0.991 * u114[t] + 73.2 for t in ts if t in u114}
series["Room B"] = {t: 1.031 * u62[t] for t in ts if t in u62}
with open("backfill.om", "w") as f:
    f.write("# HELP smartlife_power_watts_estimate Modelled plug power before the exporter existed (UPS-scaled or flat average)\n")
    f.write("# TYPE smartlife_power_watts_estimate gauge\n")
    for n, pts in series.items():
        lbl = f'device_id="{ids[n]}",instance="smartlife-exporter:9106",job="smartlife",name="{n}"'
        for t in sorted(pts):
            f.write(f"smartlife_power_watts_estimate{{{lbl}}} {pts[t]:.1f} {t}\n")
    f.write("# EOF\n")
print({n: len(p) for n, p in series.items()}, time.ctime(START), "->", time.ctime(END))
day = time.mktime(time.strptime("2026-09-26", "%Y-%m-%d"))
top = ["Room A", "Room B", "Room C", "Plug 1", "Plug 2", "Plug 3"]
print("today 00:00-21:06 kWh:", round(sum(v for n in top for t, v in series[n].items() if t >= day) / 60 / 1000, 2))
print("30d kWh:", round(sum(v for n in top for v in series[n].values()) / 60 / 1000, 1))
