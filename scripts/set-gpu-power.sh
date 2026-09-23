#!/usr/bin/env bash
#
# set-gpu-power.sh — enable persistence mode and apply a power cap to every
# NVIDIA GPU in the box.
#
# Why cap at all: 2x RTX 3090 at their 480 W stock ceiling is ~960 W of GPU
# alone, which is enough to push a 1500 VA UPS past its real-world output
# during a long prompt-processing burst. Capping to 270 W each costs only a
# few percent of inference throughput on memory-bandwidth-bound workloads
# while removing ~420 W of transient draw. Tune POWER_LIMIT_W for your PSU
# and UPS, not for benchmarks.
#
set -euo pipefail

POWER_LIMIT_W="${POWER_LIMIT_W:-270}"
WAIT_SECS="${WAIT_SECS:-60}"
NVIDIA_SMI="${NVIDIA_SMI:-/usr/bin/nvidia-smi}"

log()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root"
[[ -x $NVIDIA_SMI ]] || die "${NVIDIA_SMI} not found"

# Wait for the driver instead of a blind `sleep 5`. On a cold boot the driver
# can take longer than five seconds, and the original script would then apply
# nothing at all while still exiting 0.
log "Waiting up to ${WAIT_SECS}s for the NVIDIA driver..."
deadline=$(( SECONDS + WAIT_SECS ))
until "$NVIDIA_SMI" -L >/dev/null 2>&1; do
    (( SECONDS >= deadline )) && die "driver did not become ready within ${WAIT_SECS}s"
    sleep 2
done

"$NVIDIA_SMI" -pm 1 >/dev/null || warn "could not enable persistence mode"
log "Persistence mode enabled."

# Apply per GPU so one unsupported card does not abort the rest, and clamp to
# the range the card actually accepts — nvidia-smi rejects out-of-range values
# outright rather than clamping for you.
rc=0
while IFS=, read -r idx min max; do
    idx="${idx// /}"; min="${min%%.*}"; max="${max%%.*}"
    min="${min// /}"; max="${max// /}"

    # Cards that do not report a configurable range return "N/A" here. Feeding
    # that to (( )) is a fatal arithmetic error under `set -e`, which would
    # abort before the remaining GPUs were touched.
    if [[ ! $min =~ ^[0-9]+$ || ! $max =~ ^[0-9]+$ ]]; then
        warn "GPU ${idx}: no numeric power range reported (min='${min}' max='${max}'); skipping."
        rc=1
        continue
    fi

    target="$POWER_LIMIT_W"
    if (( target < min )); then
        warn "GPU ${idx}: ${target}W below minimum ${min}W; using ${min}W."
        target="$min"
    elif (( target > max )); then
        warn "GPU ${idx}: ${target}W above maximum ${max}W; using ${max}W."
        target="$max"
    fi

    if "$NVIDIA_SMI" -i "$idx" -pl "$target" >/dev/null; then
        log "GPU ${idx}: power limit set to ${target}W (range ${min}-${max}W)."
    else
        warn "GPU ${idx}: failed to set power limit."
        rc=1
    fi
done < <("$NVIDIA_SMI" --query-gpu=index,power.min_limit,power.max_limit \
                       --format=csv,noheader)

exit "$rc"
