#!/usr/bin/env bash
#
# Veeam pre-freeze script — quiesce stateful containers before the snapshot.
#
# Stopping the containers gives an application-consistent image: nothing is
# mid-write when the volume snapshot is taken.
#
# DEPLOYMENT (Veeam Agent for Linux managed by Veeam Backup & Replication):
#   Store this file in a local folder ON THE VEEAM BACKUP SERVER and select it
#   in the backup job wizard. At job runtime Veeam uploads it to
#   /var/lib/veeam/scripts on this machine and runs it there as root. The
#   wizard browses the backup server's filesystem, not this host's, so
#   installing it to /usr/local/bin here will NOT make Veeam find it.
#   See veeam/README.md for the wizard path.
#
# The Veeam server is normally Windows, so mind the line endings: this file
# must keep UNIX (LF) endings or the shebang fails with
# "bad interpreter: /bin/bash^M". Veeam requires .sh format.
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

# Start order is the reverse of the stop order: backing services first.
START_ORDER=(webui-postgres qdrant open-webui)

# Failsafe. If the backup job dies between freeze and thaw, these containers
# stay down indefinitely: `restart: unless-stopped` does NOT restart a
# container that was explicitly stopped. This schedules an unconditional thaw
# as insurance; post-thaw.sh cancels it on the happy path.
#
# The failsafe deliberately runs an inline `docker start` rather than calling
# post-thaw.sh. Veeam uploads these scripts to /var/lib/veeam/scripts for the
# duration of the job, so a path reference here would be pointing at a file
# that may not exist by the time the timer fires. Inlining removes the
# dependency entirely — the insurance policy must not rely on the thing it is
# insuring against.
FAILSAFE_MINUTES="${FAILSAFE_MINUTES:-30}"
FAILSAFE_UNIT="veeam-failsafe-thaw"

LOG="${LOG:-/var/log/veeam-freeze.log}"

log() { printf '[%s] pre-freeze: %s\n' "$(date +'%F %T')" "$*" | tee -a "$LOG"; }

log "=== Freeze starting (PID $$) ==="

# ---------------------------------------------------------------------------
# Arm the failsafe BEFORE stopping anything
# ---------------------------------------------------------------------------
if command -v systemd-run >/dev/null 2>&1; then
    systemctl stop "${FAILSAFE_UNIT}.timer" >/dev/null 2>&1 || true
    systemctl reset-failed "${FAILSAFE_UNIT}.service" >/dev/null 2>&1 || true

    docker_bin="$(command -v docker || echo /usr/bin/docker)"
    if systemd-run --quiet --on-active="${FAILSAFE_MINUTES}min" \
                   --unit="$FAILSAFE_UNIT" \
                   "$docker_bin" start "${START_ORDER[@]}" >/dev/null 2>&1; then
        log "Failsafe armed: unconditional 'docker start ${START_ORDER[*]}' in ${FAILSAFE_MINUTES}m."
    else
        log "WARNING: could not arm failsafe timer; continuing without it."
    fi
else
    log "WARNING: systemd-run unavailable; no failsafe armed."
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
