import time
import requests
from prometheus_client import start_http_server, Gauge, Info

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
