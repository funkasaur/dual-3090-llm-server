#!/usr/bin/env bash
#
# llm-watchdog.sh — catch a silently wedged llama-server and force a clean reload.
#
# Why: llamAmpere issue #1 reported a server that, after a prompt-cache restore, processed every request as
# ~4-6 tokens and answered prompts it never saw, with no error anywhere. The signature was /tokenize returning
# nothing. This probes /tokenize with a fixed string; two consecutive wrong answers unload the model through
# llama-swap (the next request reloads it) and flip a metric that Grafana alerts on.
#
# Runs as root from llm-watchdog.timer, because node-exporter's textfile directory is root-owned (same pattern
# as gpu-vram-temps). Only talks to llama-swap on localhost.
set -uo pipefail

SWAP="${SWAP:-http://127.0.0.1:3060}"
MODEL="${MODEL:-Qwen3.8-27B-Q8-Medium-Reason}"
PROBE_TEXT="${PROBE_TEXT:-Hello world this is a test}"
EXPECTED_TOKENS="${EXPECTED_TOKENS:-6}"
FAILS_TO_ACT="${FAILS_TO_ACT:-2}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}"
STATE_DIR="${STATE_DIR:-/var/lib/llm-watchdog}"
OUT="${TEXTFILE_DIR}/llm_watchdog.prom"

mkdir -p "$STATE_DIR" "$TEXTFILE_DIR"
fails=$(cat "$STATE_DIR/fails" 2>/dev/null || echo 0)
unloads=$(cat "$STATE_DIR/unloads" 2>/dev/null || echo 0)
loaded=0
ok=1          # "not loaded" is not a failure; only a loaded, ready model is probed
tokens=-1

state=$(curl -fsS --max-time 5 "$SWAP/running" 2>/dev/null \
        | python3 -c "import sys,json; r=[x for x in json.load(sys.stdin).get('running',[]) if x.get('model')=='$MODEL']; print(r[0]['state'] if r else 'absent')" 2>/dev/null \
        || echo "error")

if [ "$state" = "ready" ]; then
    loaded=1
    tokens=$(curl -fsS --max-time 15 -H 'Content-Type: application/json' \
                  -d "{\"content\": \"$PROBE_TEXT\"}" "$SWAP/upstream/$MODEL/tokenize" 2>/dev/null \
             | python3 -c "import sys,json; print(len(json.load(sys.stdin)['tokens']))" 2>/dev/null \
             || echo -1)
    if [ "$tokens" = "$EXPECTED_TOKENS" ]; then
        fails=0
    else
        ok=0
        fails=$((fails + 1))
        echo "probe FAILED ($fails/$FAILS_TO_ACT): /tokenize returned $tokens tokens, expected $EXPECTED_TOKENS"
        if [ "$fails" -ge "$FAILS_TO_ACT" ]; then
            code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X POST "$SWAP/api/models/unload/$MODEL")
            unloads=$((unloads + 1))
            fails=0
            echo "UNLOADED $MODEL (llama-swap HTTP $code); next request reloads it. unloads total: $unloads"
        fi
    fi
elif [ "$state" = "error" ]; then
    echo "llama-swap /running unreachable; skipping probe"
fi

echo "$fails" > "$STATE_DIR/fails"
echo "$unloads" > "$STATE_DIR/unloads"

# Atomic write: a partial file is a parse error that discards node-exporter's whole textfile output.
tmp="$(mktemp "${OUT}.XXXXXX")"
cat > "$tmp" <<EOF
# HELP llm_watchdog_tokenize_ok 1 if the loaded model tokenized the probe correctly (or is not loaded), 0 if not.
# TYPE llm_watchdog_tokenize_ok gauge
llm_watchdog_tokenize_ok{model="$MODEL"} $ok
# HELP llm_watchdog_model_loaded 1 if the watched model was loaded and ready at probe time.
# TYPE llm_watchdog_model_loaded gauge
llm_watchdog_model_loaded{model="$MODEL"} $loaded
# HELP llm_watchdog_unloads_total Times the watchdog force-unloaded the model after consecutive failed probes.
# TYPE llm_watchdog_unloads_total counter
llm_watchdog_unloads_total{model="$MODEL"} $unloads
# HELP llm_watchdog_last_probe_timestamp_seconds When the watchdog last ran.
# TYPE llm_watchdog_last_probe_timestamp_seconds gauge
llm_watchdog_last_probe_timestamp_seconds $(date +%s)
EOF
chmod 644 "$tmp"
mv -f "$tmp" "$OUT"
