"""Push grafana-dashboard.json to Grafana (uid smartlife-power), keeping the live $rate.

Token: a Grafana service-account token with Editor role, in data/grafana.json as
{"url": "http://localhost:2000", "token": "glsa_..."} (chmod 600; data/ is gitignored).
Usage: python3 build_dashboard.py && python3 push_dashboard.py
"""
import json, urllib.request

cfg = json.load(open("data/grafana.json"))
url, hdr = cfg.get("url", "http://localhost:2000").rstrip("/"), {
    "Authorization": "Bearer " + cfg["token"], "Content-Type": "application/json"}

def call(path, body=None):
    req = urllib.request.Request(url + path, headers=hdr, data=json.dumps(body).encode() if body else None,
                                 method="POST" if body else "GET")
    return json.load(urllib.request.urlopen(req, timeout=30))

dash = json.load(open("grafana-dashboard.json"))
live = call("/api/dashboards/uid/smartlife-power")
json.dump(live, open(f"grafana-dashboard.live.bak-{live['meta']['version']}.json", "w"))   # backup of what is replaced
for v in live["dashboard"].get("templating", {}).get("list", []):   # keep the rate set in the UI
    if v.get("name") == "rate":
        for mine in dash["templating"]["list"]:
            if mine["name"] == "rate":
                mine.update(query=v.get("query"), current=v.get("current"))
dash["version"] = live["dashboard"]["version"]
r = call("/api/dashboards/db", {"dashboard": dash, "folderUid": live["meta"].get("folderUid"), "overwrite": False,
                                "message": "build_dashboard.py"})
print(r.get("status"), "version", r.get("version"), url + r.get("url", ""))
