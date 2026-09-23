#!/usr/bin/env bash
#
# Veeam pre-freeze script — quiesce stateful containers before the snapshot.
#
# Veeam runs this on the guest immediately before taking the snapshot, and
# runs post-thaw.sh immediately after. Stopping the containers gives a
# crash-consistent-free, application-consistent image: nothing is mid-write
# when the snapshot is taken.
#
# Install on the Linux guest (NOT the Veeam server) as:
#   /usr/local/bin/pre-freeze.sh   (mode 0755, root-owned)
# and reference that path in the job's guest processing settings.
#
# Veeam treats a non-zero exit as a failed freeze, so exit codes matter here.
#
set -uo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# PATCH: order reversed. The original stopped webui-postgres BEFORE open-webui,
# so Open WebUI spent the shutdown window throwing database connection errors
# at any active user. Dependents must go down first.
CONTAINERS=(open-webui webui-postgres qdrant)

# PATCH: was the docker default of 10s. `docker stop` sends SIGTERM, waits,
# then SIGKILLs. The Postgres image maps SIGTERM to a fast shutdown, which
# still has to finish a checkpoint — and a SIGKILL partway through leaves
# exactly the inconsistent data directory this script exists to prevent.
STOP_TIMEOUT="${STOP_TIMEOUT:-60}"

# Failsafe. If the backup job dies between freeze and thaw, these containers
# stay down indefinitely: `restart: unless-stopped` does NOT restart a
# container that was explicitly stopped. This schedules an unconditional thaw
# as insurance; post-thaw.sh cancels it on the happy path.
FAILSAFE_MINUTES="${FAILSAFE_MINUTES:-30}"
FAILSAFE_UNIT="veeam-failsafe-thaw"
POST_THAW="${POST_THAW:-/usr/local/bin/post-thaw.sh}"

LOG="${LOG:-/var/log/veeam-freeze.log}"

log() { printf '[%s] pre-freeze: %s\n' "$(date +'%F %T')" "$*" | tee -a "$LOG"; }

log "=== Freeze starting (PID $$) ==="

# ---------------------------------------------------------------------------
# Arm the failsafe BEFORE stopping anything
# ---------------------------------------------------------------------------
if command -v systemd-run >/dev/null 2>&1 && [[ -x $POST_THAW ]]; then
    systemctl stop "${FAILSAFE_UNIT}.timer" >/dev/null 2>&1 || true
    if systemd-run --quiet --on-active="${FAILSAFE_MINUTES}min" \
                   --unit="$FAILSAFE_UNIT" "$POST_THAW" >/dev/null 2>&1; then
        log "Failsafe armed: unconditional thaw in ${FAILSAFE_MINUTES}m."
    else
        log "WARNING: could not arm failsafe timer; continuing without it."
    fi
else
    log "WARNING: systemd-run or ${POST_THAW} unavailable; no failsafe armed."
fi

# ---------------------------------------------------------------------------
# Stop the containers
# ---------------------------------------------------------------------------
rc=0
for c in "${CONTAINERS[@]}"; do
    if ! docker inspect "$c" >/dev/null 2>&1; then
        log "WARNING: container '${c}' does not exist; skipping."
        continue
    fi

    if [[ "$(docker inspect -f '{{.State.Running}}' "$c")" != "true" ]]; then
        log "Container '${c}' already stopped; skipping."
        continue
    fi

    log "Stopping ${c} (timeout ${STOP_TIMEOUT}s)..."
    start=$SECONDS
    if docker stop -t "$STOP_TIMEOUT" "$c" >/dev/null; then
        log "Stopped ${c} in $(( SECONDS - start ))s."
    else
        log "ERROR: failed to stop ${c}."
        rc=1
    fi
done

# Report whether anything was killed rather than shut down cleanly. Exit code
# 137 is SIGKILL, which means the stop timeout was too short and the snapshot
# may not be application-consistent after all.
for c in "${CONTAINERS[@]}"; do
    docker inspect "$c" >/dev/null 2>&1 || continue
    code="$(docker inspect -f '{{.State.ExitCode}}' "$c" 2>/dev/null)"
    if [[ $code == "137" ]]; then
        log "WARNING: ${c} exited 137 (SIGKILL) — raise STOP_TIMEOUT."
    fi
done

if (( rc == 0 )); then
    log "=== Freeze complete; safe to snapshot ==="
else
    log "=== Freeze FAILED — Veeam will abort the job ==="
fi
exit "$rc"
