#!/usr/bin/env bash
#
# gpu-vram-temps.sh — export GDDR6X junction and memory temperatures as
# Prometheus metrics via node-exporter's textfile collector.
#
# Why this exists: dcgm-exporter 2.3.2 ships a 15-field default counter set
# that does not include DCGM_FI_DEV_MEMORY_TEMP, so there is no GPU memory
# temperature in Prometheus at all. On a 3090 that is the number that matters —
# GDDR6X runs far hotter than the core and throttles at 110C, so a card
# reporting a comfortable 70C core can be sitting at 100C+ on the backside
# memory modules.
#
# Reads temperatures with `gputemps` (https://github.com/ThomasBaruzier/
# gddr6-core-junction-vram-temps), which needs root because it maps the card's
# PCI BAR directly. Run from a timer, not as a daemon: a short-lived root
# process on a schedule is a smaller thing to trust than a long-lived one.
#
set -euo pipefail

GPUTEMPS="${GPUTEMPS:-/usr/local/bin/gputemps}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}"
OUT="${TEXTFILE_DIR}/gpu_vram_temps.prom"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root (gputemps maps PCI BARs)"
[[ -x $GPUTEMPS ]] || die "${GPUTEMPS} not found or not executable"

mkdir -p "$TEXTFILE_DIR"

json="$("$GPUTEMPS" --json --once 2>/dev/null)" \
    || die "gputemps failed — is the NVIDIA driver loaded?"
[[ -n $json ]] || die "gputemps produced no output"

# Write to a temporary file in the SAME directory and rename into place.
# node-exporter reads this directory on every scrape, and a partially written
# file is a parse error that discards the whole textfile collector output.
# rename(2) within one filesystem is atomic, so a scrape sees either the old
# file or the new one, never half of either.
tmp="$(mktemp "${OUT}.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

printf '%s' "$json" | python3 -c '
import sys, json

d = json.load(sys.stdin)
gpus = d.get("gpus", [])

series = [
    ("gpu_core_temp_celsius",     "core",     "GPU core temperature in Celsius."),
    ("gpu_junction_temp_celsius", "junction", "GPU hotspot/junction temperature in Celsius."),
    ("gpu_vram_temp_celsius",     "vram",     "GDDR6X memory temperature in Celsius."),
]

out = []
for name, key, help_text in series:
    out.append(f"# HELP {name} {help_text}")
    out.append(f"# TYPE {name} gauge")
    for g in gpus:
        if key not in g:
            continue
        # %-formatting, not an f-string. This Python is embedded in a
        # single-quoted shell string, so it cannot contain a single quote; and
        # escaped double quotes inside an f-string EXPRESSION are a syntax
        # error before Python 3.12. %-formatting avoids both.
        out.append("%s{gpu=\"%s\"} %s" % (name, g["index"], g[key]))

# Lets you alert on the collector itself going stale, which is the failure
# mode that otherwise looks exactly like "temperatures are fine".
out.append("# HELP gpu_temps_last_update_timestamp_seconds Unix time of the last successful reading.")
out.append("# TYPE gpu_temps_last_update_timestamp_seconds gauge")
ts = d.get("timestamp", 0)
out.append(f"gpu_temps_last_update_timestamp_seconds {ts}")

print("\n".join(out))
' > "$tmp" || die "failed to convert gputemps JSON"

[[ -s $tmp ]] || die "conversion produced an empty file"

chmod 0644 "$tmp"
mv -f "$tmp" "$OUT"
trap - EXIT
