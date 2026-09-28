"""Import hourly energy history from Prometheus into HA long-term statistics (before each statistic's first row).
Measured data only (live plug readings + HA-history backfill; no estimate). The last imported hour's sum is set so
the first existing hour's change equals Prometheus' energy for that hour, so the series joins without a jump.
usage: ha_import_stats.py <statistic_id> <prom plug name | BILLED> [rate] [--dry] [--join=<unix hour start>]
(rate -> import kWh*rate as a cost stat; --join re-imports everything before that existing hour, e.g. after a rate change)"""
import sys, os, datetime, urllib.request, urllib.parse; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ha_common import *
DRY = '--dry' in sys.argv
JOIN = next((int(a.split('=')[1]) for a in sys.argv if a.startswith('--join=')), None)   # unix hour to join at (re-imports)
args = [a for a in sys.argv[1:] if not a.startswith('--')]
sid, plug = args[0], args[1]; rate = float(args[2]) if len(args) > 2 else None
BILLED = ['Office','Desktop','Server','TV and Audio','Internet and Den','NerdQaxe++']
sel = '{name=~"%s"}' % '|'.join(n.replace('+','\\\\+') for n in BILLED) if plug == 'BILLED' else '{name="%s"}' % plug
SRC = f"(((smartlife_power_watts{sel} and on(device_id) smartlife_voltage_volts < 300) or smartlife_power_watts_history{sel}) + 0)"
EXPR = f"sum_over_time(sum({SRC})[1h:60s]) * 60 / 3.6e6"
def energy(t0, t1):   # {hour_start: kWh} for hours starting in [t0, t1]
    out = {}
    for a in range(t0, t1 + 1, 3600 * 5000):
        b = min(t1, a + 3600 * 4999)
        q = urllib.parse.urlencode({'query': EXPR, 'start': a + 3600, 'end': b + 3600, 'step': 3600})
        r = json.load(urllib.request.urlopen('http://localhost:2090/api/v1/query_range?' + q, timeout=300))['data']['result']
        for series in r:
            for ts, v in series['values']: out[int(float(ts)) - 3600] = float(v)
    return out
rows = call(type='recorder/statistics_during_period', start_time='2024-01-01T00:00:00Z', statistic_ids=[sid], period='hour', types=['sum']).get(sid, [])
if not rows: sys.exit(f'{sid}: no existing statistics yet')
if JOIN:   # re-import: join at a given existing hour instead of the first row
    rows = [x for x in rows if x['start'] // 1000 >= JOIN]
h0, s0 = rows[0]['start'] // 1000, rows[0]['sum']
E = energy(1735603200, h0)   # 2024-12-31 00:00 UTC .. h0
e0 = E.pop(h0, 0.0)
hours = sorted(h for h in E if h < h0)
if not hours: sys.exit(f'{sid}: nothing to import before {h0}')
scale = rate if rate is not None else 1.0
total = sum(E[h] for h in hours)
end_sum = s0 - e0 * scale                      # sum at the end of hour h0-1
cum, stats = end_sum - total * scale, []
for h in hours:
    cum += E[h] * scale
    stats.append({'start': datetime.datetime.fromtimestamp(h, datetime.timezone.utc).isoformat(), 'state': round(cum, 4), 'sum': round(cum, 4)})
md = {'has_mean': False, 'mean_type': 0, 'has_sum': True, 'name': None, 'source': 'recorder', 'statistic_id': sid,
      'unit_of_measurement': 'USD' if rate is not None else 'kWh', 'unit_class': None if rate is not None else 'energy'}
if DRY:
    print(sid, len(stats), 'rows', stats[0]['start'], '->', stats[-1]['start'], f'total {total:.1f} kWh', f'e(h0)={e0:.3f} s0={s0}'); sys.exit()
r = call(type='recorder/import_statistics', metadata=md, stats=stats)
print(sid, len(stats), 'rows', stats[0]['start'], '->', stats[-1]['start'], f'total {total * scale:.1f}', 'result', r)
