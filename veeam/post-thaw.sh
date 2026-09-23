#!/usr/bin/env bash
#
# Veeam post-thaw script — bring the stateful containers back up after the
# snapshot has been taken.
#
# DEPLOYMENT (Veeam Agent for Linux managed by Veeam Backup & Replication):
#   Store this file in a local folder ON THE VEEAM BACKUP SERVER and select it
#   in the backup job wizard. Veeam uploads it to /var/lib/veeam/scripts on
#   this machine at job runtime and executes it there as root.
#   See veeam/README.md for the wizard path.
#
# Keep UNIX (LF) line endings — the Veeam server is normally Windows, and a
# CRLF shebang fails with "bad interpreter: /bin/bash^M". Veeam requires .sh.
#
# Safe to run when the containers are already up, so it can also be run by
# hand to recover a stranded stack.
#
set -uo pipefail

# PATCH: start order is the reverse of the stop order — backing services
# first, dependents last, so Open WebUI never comes up against a database
# that is not accepting connections yet.
CONTAINERS=(webui-postgres qdrant open-webui)

# Wait for health before reporting success, so Veeam does not mark the job
# complete while the stack is still coming up.
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
FAILSAFE_UNIT="veeam-failsafe-thaw"
LOG="${LOG:-/var/log/veeam-freeze.log}"

log() { printf '[%s] post-thaw: %s\n' "$(date +'%F %T')" "$*" | tee -a "$LOG"; }

log "=== Thaw starting (PID $$) ==="

# Cancel the failsafe armed by pre-freeze; this is the happy path.
if systemctl stop "${FAILSAFE_UNIT}.timer" >/dev/null 2>&1; then
    log "Failsafe timer cancelled."
fi
systemctl reset-failed "${FAILSAFE_UNIT}.service" >/dev/null 2>&1 || true

rc=0
for c in "${CONTAINERS[@]}"; do
    if ! docker inspect "$c" >/dev/null 2>&1; then
        log "WARNING: container '${c}' does not exist; skipping."
        continue
    fi

    if [[ "$(docker inspect -f '{{.State.Running}}' "$c")" == "true" ]]; then
        log "Container '${c}' already running; skipping."
        continue
    fi

    log "Starting ${c}..."
    if docker start "$c" >/dev/null; then
        log "Started ${c}."
    else
        log "ERROR: failed to start ${c}."
        rc=1
    fi
done

# ---------------------------------------------------------------------------
# Verify the stack actually came back
# ---------------------------------------------------------------------------
# A container that starts and immediately crash-loops still reports "started".
# Wait for the health checks to settle so a broken restore surfaces here
# rather than the next time someone opens the UI.
log "Waiting up to ${HEALTH_TIMEOUT}s for health checks..."
deadline=$(( SECONDS + HEALTH_TIMEOUT ))
while :; do
    pending=0
    for c in "${CONTAINERS[@]}"; do
        docker inspect "$c" >/dev/null 2>&1 || continue
        state="$(docker inspect -f '{{.State.Status}}' "$c")"
        health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c")"
        [[ $state == "running" ]] || { pending=1; continue; }
        [[ $health == "starting" ]] && pending=1
    done
    (( pending == 0 )) && break
    (( SECONDS >= deadline )) && { log "WARNING: health checks did not settle in time."; break; }
    sleep 3
done

for c in "${CONTAINERS[@]}"; do
    docker inspect "$c" >/dev/null 2>&1 || continue
    state="$(docker inspect -f '{{.State.Status}}' "$c")"
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$c")"
    log "  ${c}: ${state} (health: ${health})"
    if [[ $state != "running" ]] || [[ $health == "unhealthy" ]]; then
        rc=1
    fi
done

if (( rc == 0 )); then
    log "=== Thaw complete; stack healthy ==="
else
    log "=== Thaw FINISHED WITH ERRORS — check the stack ==="
fi
exit "$rc"
