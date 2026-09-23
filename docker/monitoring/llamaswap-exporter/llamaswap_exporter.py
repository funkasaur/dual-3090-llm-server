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


def poll():
    try:
        stats = requests.get(STATS_URL, timeout=5).json()
        total_requests.set(stats.get('total_requests', 0))
        total_input_tokens.set(stats.get('total_input_tokens', 0))
        total_output_tokens.set(stats.get('total_output_tokens', 0))
        total_cache_tokens.set(stats.get('total_cache_tokens', 0))
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
