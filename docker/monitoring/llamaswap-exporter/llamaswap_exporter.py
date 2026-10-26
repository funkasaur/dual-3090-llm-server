import time
import requests
from prometheus_client import start_http_server, Counter, Gauge, Histogram, Info

STATS_URL = "http://llama-swap-unified:8080/api/metrics/stats"
ACTIVITY_URL = "http://llama-swap-unified:8080/api/metrics/activity"
POLL_INTERVAL = 15
EXPORTER_PORT = 9105

total_requests = Gauge('llamaswap_total_requests', 'Total requests served')
total_input_tokens = Gauge('llamaswap_total_input_tokens', 'Total input/prompt tokens')
total_output_tokens = Gauge('llamaswap_total_output_tokens', 'Total output/generated tokens')
total_cache_tokens = Gauge('llamaswap_total_cache_tokens', 'Total cache tokens')

last_duration_ms = Gauge('llamaswap_last_request_duration_ms', 'Duration of the most recent request in ms')
last_tokens_per_second = Gauge('llamaswap_last_request_tokens_per_second', 'Generation speed of the most recent request')
last_prompt_per_second = Gauge('llamaswap_last_request_prompt_per_second', 'Prompt processing speed of the most recent request')

active_model = Info('llamaswap_active_model', 'Model used by the most recently seen request')

# llama-swap's totals are cumulative since it moved to a persistent store (Sep 25 19:43). The
# token-saver ledger's `history` table holds hourly totals from before that (Apr 2026 on, partly
# estimated). Their sum is an all-time counter; the backfill script writes the same series into
# Prometheus hourly, so live values continue the backfilled curve.
LEDGER = '/ledger/llamaswap.sqlite'
ledger_tokens = Gauge('llamaswap_ledger_tokens_total', 'All-time tokens by kind: token-saver history + llama-swap store', ['kind'])
ledger_requests = Gauge('llamaswap_ledger_requests_total', 'All-time requests: token-saver history + llama-swap store', ['store'])  # labelled: no 0 before the first read
_offset, _offset_at = None, 0

# Per-request performance (2026-10-12): every activity record once, by id. The activity page holds the
# newest 25, so more than 25 requests between two 15 s polls would drop the oldest of them.
DEPTHS = ((8192, '0-8k'), (32768, '8-32k'), (65536, '32-64k'), (131072, '64-128k'))
decode_tps = Histogram('llamaswap_request_decode_tps', 'Generation tokens/s per request (>= 16 output tokens)',
                       ['depth'], buckets=(10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 120, 150, 200))
prefill_tps = Histogram('llamaswap_request_prefill_tps', 'Prompt tokens/s per request (>= 256 new prompt tokens)',
                        buckets=(100, 250, 500, 750, 1000, 1500, 2000, 3000, 5000))
prefill_s = Histogram('llamaswap_request_prefill_seconds', 'New prompt tokens / prompt speed: approx. time to first token',
                      buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40, 80, 160))
duration_s = Histogram('llamaswap_request_duration_seconds', 'Whole request duration',
                       buckets=(1, 2, 5, 10, 20, 40, 60, 120, 300, 600, 1200))
context_tok = Histogram('llamaswap_request_context_tokens', 'Context per request: cached + new prompt tokens',
                        buckets=(1024, 4096, 8192, 16384, 32768, 65536, 98304, 131072, 196608, 262144))
draft_total = Counter('llamaswap_request_draft_tokens', 'Speculative draft tokens proposed')
draft_acc = Counter('llamaswap_request_draft_accepted_tokens', 'Speculative draft tokens accepted')
status_total = Counter('llamaswap_request_status', 'Requests by HTTP status', ['code'])
_seen_id = None
for _lab in [lab for _, lab in DEPTHS] + ['128k+']:  # start labelled series at 0 so rate() sees the first request
    decode_tps.labels(_lab)
status_total.labels('200')


def depth_label(n):
    return next((lab for lim, lab in DEPTHS if n < lim), '128k+')


def observe(records):
    """Record activity entries newer than the last seen id (none on the first poll)."""
    global _seen_id
    ids = [r.get('id', 0) for r in records]
    if _seen_id is None:
        _seen_id = max(ids, default=0)
        return
    for r in sorted(records, key=lambda x: x.get('id', 0)):
        if r.get('id', 0) <= _seen_id:
            continue
        t = r.get('tokens') or {}
        new, cached, out = t.get('input_tokens', 0), t.get('cache_tokens', 0), t.get('output_tokens', 0)
        ctx = new + cached
        status_total.labels(str(r.get('resp_status_code', 0))).inc()
        duration_s.observe(r.get('duration_ms', 0) / 1000)
        context_tok.observe(ctx)
        if out >= 16 and t.get('tokens_per_second', 0) > 0:
            decode_tps.labels(depth_label(ctx)).observe(t['tokens_per_second'])
        if new >= 256 and t.get('prompt_per_second', 0) > 0:
            prefill_tps.observe(t['prompt_per_second'])
        if new > 0 and t.get('prompt_per_second', 0) > 0:
            prefill_s.observe(new / t['prompt_per_second'])
        draft_total.inc(max(t.get('draft_tokens', 0), 0))
        draft_acc.inc(max(t.get('draft_acc_tokens', 0), 0))
    _seen_id = max(_seen_id, max(ids, default=0))
_last_total = 0


def history_offset():
    """Sum of the ledger's pre-store hourly history; re-read every 10 minutes."""
    global _offset, _offset_at
    if _offset is None or time.time() - _offset_at > 600:
        try:
            import sqlite3
            c = sqlite3.connect(f'file:{LEDGER}?mode=ro&immutable=1', uri=True, timeout=5)  # WAL db on a ro mount
            first = c.execute('select min(ts) from requests').fetchone()[0] or time.time()
            row = c.execute('select coalesce(sum(requests),0), coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0),'
                            ' coalesce(sum(cache_tokens),0) from history where ts < ?', (first,)).fetchone()
            c.close()
            _offset, _offset_at = dict(zip(('requests', 'input', 'output', 'cache'), row)), time.time()
        except Exception as e:
            print(f"ledger read failed: {e}", flush=True)
    return _offset


def poll():
    try:
        stats = requests.get(STATS_URL, timeout=5).json()
        total_requests.set(stats.get('total_requests', 0))
        total_input_tokens.set(stats.get('total_input_tokens', 0))
        total_output_tokens.set(stats.get('total_output_tokens', 0))
        total_cache_tokens.set(stats.get('total_cache_tokens', 0))
        off = history_offset()
        global _last_total
        # a store that comes back empty after a llama-swap restart would look like a counter reset
        if off is not None and stats.get('total_requests', 0) >= _last_total:
            _last_total = stats.get('total_requests', 0)
            ledger_requests.labels('all').set(off['requests'] + stats.get('total_requests', 0))
            for kind in ('input', 'output', 'cache'):
                ledger_tokens.labels(kind).set(off[kind] + stats.get(f'total_{kind}_tokens', 0))
    except Exception as e:
        print(f"stats poll failed: {e}", flush=True)

    try:
        activity = requests.get(ACTIVITY_URL, timeout=5).json()
        records = activity.get('data', [])
        observe(records)
        if records:
            latest = records[0]
            active_model.info({'model': latest.get('model', 'unknown')})
            tokens = latest.get('tokens', {})
            last_duration_ms.set(latest.get('duration_ms', 0))
            last_tokens_per_second.set(tokens.get('tokens_per_second', 0))
            last_prompt_per_second.set(tokens.get('prompt_per_second', 0))
    except Exception as e:
        print(f"activity poll failed: {e}", flush=True)



if __name__ == '__main__':
    start_http_server(EXPORTER_PORT)
    print(f"llamaswap-exporter listening on :{EXPORTER_PORT}/metrics", flush=True)
    while True:
        poll()
        time.sleep(POLL_INTERVAL)
