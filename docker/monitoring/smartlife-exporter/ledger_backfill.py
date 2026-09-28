"""Backfill llamaswap_ledger_{tokens,requests}_total into Prometheus from token-saver's ledger.

Writes the same all-time series the llamaswap-exporter publishes (history table + requests table,
cumulative, 5-minute samples; each hourly history row spread evenly over its hour), ending just before
the exporter's first live sample so the curves join. Load with:
  docker cp ledger.om prometheus:/tmp/ && docker exec prometheus promtool tsdb create-blocks-from openmetrics /tmp/ledger.om /prometheus
Usage: python3 ledger_backfill.py <end_unix_ts> > ledger.om
"""
import sqlite3, sys, bisect
DB = "/home/aiuser/hermes/hermes-data/plugin-data/token-saver/llamaswap.sqlite"
LBL = 'instance="10.0.0.26:9105",job="llama-swap-activity"'
end = int(sys.argv[1])
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
first_req = c.execute("select min(ts) from requests").fetchone()[0]
hist = c.execute("select ts, requests, input_tokens, output_tokens, cache_tokens from history where ts < ? order by ts", (first_req,)).fetchall()
reqs = c.execute("select ts, 1, input_tokens, output_tokens, cache_tokens from requests where ts < ? order by ts", (end,)).fetchall()
start = int(hist[0][0]) // 300 * 300
K = ("requests", "input", "output", "cache")
def cum_at(t):
    tot = [0.0] * 4
    for ts, *v in hist:
        if ts >= t: break
        f = min(1.0, (t - ts) / 3600)
        for i in range(4): tot[i] += v[i] * f
    for ts, *v in reqs:
        if ts > t: break
        for i in range(4): tot[i] += v[i]
    return tot
series = {k: [] for k in K}
t, last = start, [0.0] * 4
while t < end:
    v = cum_at(t)
    v = [max(a, b) for a, b in zip(v, last)]; last = v
    for i, k in enumerate(K): series[k].append((t, v[i]))
    t += 300
print("# TYPE llamaswap_ledger_tokens_total gauge")
for k in K[1:]:
    for ts, v in series[k]: print(f'llamaswap_ledger_tokens_total{{kind="{k}",{LBL}}} {v:.0f} {ts}')
print("# TYPE llamaswap_ledger_requests_total gauge")
for ts, v in series["requests"]: print(f'llamaswap_ledger_requests_total{{store="all",{LBL}}} {v:.0f} {ts}')
print("# EOF")
