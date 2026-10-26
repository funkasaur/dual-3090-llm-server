#!/usr/bin/env bash
#
# backup.sh — nightly Borg backup of the host config + container persistent data.
#
# Pre-hooks take application-consistent dumps first (Postgres, Qdrant), then
# Borg archives the host. Run from borg-backup.timer; see systemd/.
#
# The repository passphrase is NEVER stored in this file. Put it in a
# root-only file and point BORG_PASSPHRASE_FILE at it:
#
#     install -m 0600 /dev/null /etc/borg/passphrase
#     printf '%s' 'your-passphrase' > /etc/borg/passphrase
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BORG_REPO="${BORG_REPO:-/mnt/storage/borg/system}"
BORG_PASSPHRASE_FILE="${BORG_PASSPHRASE_FILE:-/etc/borg/passphrase}"
LOG="${LOG:-/var/log/borg-backup.log}"

PG_CONTAINER="${PG_CONTAINER:-webui-postgres}"
PG_USER="${PG_USER:-webui_admin}"
PG_DUMP_DIR="${PG_DUMP_DIR:-/var/backup/postgres}"
PG_KEEP_DAYS="${PG_KEEP_DAYS:-14}"

QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
QDRANT_KEEP_SNAPSHOTS="${QDRANT_KEEP_SNAPSHOTS:-3}"
# Host path backing the container's /qdrant/snapshots. Must be a real volume
# mount, or the snapshots are stranded in the container's writable layer and
# never reach the archive. See docker/qdrant/docker-compose.yaml.
QDRANT_SNAPSHOT_DIR="${QDRANT_SNAPSHOT_DIR:-/home/aiuser/qdrant/qdrant_snapshots}"

export BORG_REPO

# Check privileges BEFORE redirecting output. Without this, a non-root run dies
# on the `exec` redirect itself with a bare shell error and no explanation,
# because /var/log/borg-backup.log is not writable. The script also needs root
# for /var/backup, the Borg repository and `docker exec`.
if [[ $EUID -ne 0 ]]; then
    printf 'ERROR: %s must run as root (try: sudo %s)\n' "${0##*/}" "$0" >&2
    exit 1
fi

exec >> "$LOG" 2>&1

log() { printf '[%s] %s\n' "$(date +'%F %T')" "$*"; }

# Always say how the run ended — a silent failure in a nightly job is the
# worst kind. The original script could die under `set -e` with the last log
# line being whatever step happened to be in progress.
on_exit() {
    local rc=$?
    if (( rc == 0 )); then
        log "===== Backup finished OK ====="
    else
        log "===== Backup FAILED (exit ${rc}) ====="
    fi
    exit "$rc"
}
trap on_exit EXIT

log "===== Backup started ====="

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
[[ -r $BORG_PASSPHRASE_FILE ]] || {
    log "FATAL: passphrase file ${BORG_PASSPHRASE_FILE} is missing or unreadable."
    exit 1
}
BORG_PASSPHRASE="$(< "$BORG_PASSPHRASE_FILE")"
export BORG_PASSPHRASE

command -v borg >/dev/null || { log "FATAL: borg not installed."; exit 1; }
command -v jq   >/dev/null || { log "FATAL: jq not installed.";   exit 1; }

# Fail early and loudly if the backup target is not mounted, rather than
# silently creating archives on the root filesystem.
borg info "$BORG_REPO" >/dev/null 2>&1 || {
    log "FATAL: cannot open Borg repo ${BORG_REPO} (not mounted, or wrong passphrase)."
    exit 1
}

# ---------------------------------------------------------------------------
# Pre-hook: PostgreSQL logical dump
# ---------------------------------------------------------------------------
log "Dumping PostgreSQL from container '${PG_CONTAINER}'..."
mkdir -p "$PG_DUMP_DIR"

pg_target="${PG_DUMP_DIR}/pg_dumpall_$(date +%F).sql"
pg_tmp="${pg_target}.partial"

# Dump to a .partial file and only rename on success. Redirecting straight to
# the final path leaves a truncated file that looks like a valid backup if
# pg_dumpall fails halfway through.
if docker exec "$PG_CONTAINER" pg_dumpall -U "$PG_USER" > "$pg_tmp"; then
    mv -f "$pg_tmp" "$pg_target"
    log "Postgres dump complete: ${pg_target} ($(du -h "$pg_target" | cut -f1))"
else
    rm -f "$pg_tmp"
    log "FATAL: pg_dumpall failed."
    exit 1
fi

# Retention: dated dumps otherwise accumulate forever and get re-archived by
# Borg every single night.
find "$PG_DUMP_DIR" -maxdepth 1 -name 'pg_dumpall_*.sql' -mtime "+${PG_KEEP_DAYS}" -print -delete \
    | while read -r f; do log "Pruned old dump: ${f}"; done

# ---------------------------------------------------------------------------
# Pre-hook: Qdrant snapshots
# ---------------------------------------------------------------------------
# Snapshots give a consistent copy. The live segment files under
# qdrant_storage/ are NOT safe to archive directly while Qdrant is writing.
log "Snapshotting Qdrant collections..."

collections="$(curl -sf --max-time 30 "${QDRANT_URL}/collections" | jq -r '.result.collections[].name' || true)"

if [[ -z $collections ]]; then
    log "WARNING: no Qdrant collections found, or Qdrant unreachable at ${QDRANT_URL}."
else
    while read -r collection; do
        [[ -n $collection ]] || continue
        log "  Snapshotting: ${collection}"
        if ! curl -sf --max-time 300 -X POST \
                "${QDRANT_URL}/collections/${collection}/snapshots" >/dev/null; then
            log "  WARNING: snapshot failed for ${collection}"
            continue
        fi

        # Keep only the newest N snapshots per collection. Qdrant never prunes
        # these itself, so they grow without bound inside the storage volume.
        mapfile -t old < <(
            curl -sf --max-time 30 "${QDRANT_URL}/collections/${collection}/snapshots" \
              | jq -r '.result | sort_by(.creation_time) | reverse | .['"${QDRANT_KEEP_SNAPSHOTS}"':] | .[].name'
        )
        for snap in "${old[@]}"; do
            [[ -n $snap ]] || continue
            log "  Pruning old snapshot: ${snap}"
            curl -sf --max-time 60 -X DELETE \
                "${QDRANT_URL}/collections/${collection}/snapshots/${snap}" >/dev/null || true
        done
    done <<< "$collections"
    log "Qdrant snapshots complete."

    # Fail loudly if the snapshots are not reachable on the host. A silent
    # miss here means the vector database is not in the backup at all, which
    # is exactly the failure this pre-hook exists to prevent.
    if [[ -d $QDRANT_SNAPSHOT_DIR ]]; then
        count=$(find "$QDRANT_SNAPSHOT_DIR" -name '*.snapshot' 2>/dev/null | wc -l)
        log "Snapshots visible on host: ${count} in ${QDRANT_SNAPSHOT_DIR}"
        (( count > 0 )) || log "WARNING: snapshot directory is empty — is /qdrant/snapshots mounted?"

        # Report directories belonging to collections that no longer exist.
        # Pruning above walks the LIVE collection list, so when a collection is
        # deleted its snapshots stop being pruned and simply sit there forever.
        #
        # Deliberately only a warning: those snapshots may be the last copy of
        # a collection someone dropped by accident, and a backup script is the
        # wrong place to make that call automatically.
        for dir in "$QDRANT_SNAPSHOT_DIR"/*/; do
            [[ -d $dir ]] || continue
            name="$(basename "$dir")"
            [[ $name == tmp ]] && continue
            if ! grep -qxF "$name" <<< "$collections"; then
                n=$(find "$dir" -name '*.snapshot' | wc -l)
                sz=$(du -sh "$dir" | cut -f1)
                log "WARNING: orphaned snapshots for deleted collection '${name}' (${n} files, ${sz}) — not pruned."
            fi
        done
    else
        log "WARNING: ${QDRANT_SNAPSHOT_DIR} does not exist. Qdrant snapshots are"
        log "WARNING: stranded inside the container and are NOT being backed up."
    fi
fi

# ---------------------------------------------------------------------------
# Borg archive
# ---------------------------------------------------------------------------
log "Running Borg create..."

# Rebuildable bulk is left out: --exclude-caches skips every dir holding a
# CACHEDIR.TAG (Rust target/, ~/.cargo/registry, uv .venv, mypy/ruff/pytest
# caches); the rest are downloaded toolchains and package caches that a
# reinstall fetches again.

borg create \
    --verbose \
    --stats \
    --compression lz4 \
    --one-file-system \
    "::system-{now:%Y-%m-%d_%H:%M}" \
    /etc \
    /home \
    /root \
    /opt \
    /var/backup \
    /usr/local/bin \
    --exclude 'sh:/home/*/.cache' \
    --exclude 'sh:/home/*/.local/share/Trash' \
    --exclude 'sh:**/node_modules' \
    --exclude 'sh:**/__pycache__' \
    --exclude '/home/*/qdrant/qdrant_storage' \
    --exclude-caches \
    --exclude 'sh:/home/*/.rustup/toolchains' \
    --exclude 'sh:/home/*/.npm/_cacache' \
    --exclude '/var/lib/docker' \
    --exclude /mnt \
    --exclude /proc \
    --exclude /sys \
    --exclude /dev \
    --exclude /run \
    --exclude /tmp \
    --exclude /var/cache \
    --exclude /var/tmp \
    || create_rc=$?

# rc 1 is a borg warning, almost always "file changed while we backed it up" on a
# live log or session file. The archive is written, so carry on to prune, compact
# and the offsite sync instead of skipping them; rc 2+ is a real error and still
# fails the run.
case ${create_rc:-0} in
    0) log "Borg create complete." ;;
    1) log "WARNING: borg create finished with warnings (rc 1, see above); archive kept, continuing." ;;
    *) log "FATAL: borg create failed (rc ${create_rc})."; exit "$create_rc" ;;
esac

# ---------------------------------------------------------------------------
# Prune + compact
# ---------------------------------------------------------------------------
log "Pruning old archives..."
borg prune --list --glob-archives 'system-*' \
    --keep-daily 7 --keep-weekly 4 --keep-monthly 6
log "Prune complete."

log "Compacting repository..."
borg compact
log "Compact complete."
# ---------------------------------------------------------------------------
# Offsite (optional): mirror the repository to an rclone remote
# ---------------------------------------------------------------------------
# The repository is already encrypted and deduplicated, so this uploads only the
# segments that changed tonight, and the remote holds only ciphertext. `borg
# with-lock` keeps any other borg process out while rclone reads it. Set
# RCLONE_DEST (e.g. gdrive:llm-server/borg) to enable; give root its own rclone
# config, since a shared one can end up root-owned after a token refresh. Google
# Drive keeps files that `sync` removes in its trash for 30 days, which is the net
# if a damaged local repository is ever mirrored up.
RCLONE="${RCLONE:-rclone}"
RCLONE_DEST="${RCLONE_DEST:-}"
if [[ -z $RCLONE_DEST ]]; then
    log "Offsite sync not configured (RCLONE_DEST unset); skipping."
elif command -v "$RCLONE" >/dev/null && "$RCLONE" listremotes 2>/dev/null | grep -qx "${RCLONE_DEST%%:*}:"; then
    log "Syncing repository to ${RCLONE_DEST}..."
    borg with-lock "$BORG_REPO" "$RCLONE" sync "$BORG_REPO" "$RCLONE_DEST" \
        --transfers 4 --checkers 8 --retries 3 --drive-chunk-size 64M --stats-one-line --stats 0 -v
    log "Offsite sync complete."
else
    log "WARNING: no rclone remote '${RCLONE_DEST%%:*}' for this user; repository NOT copied offsite."
fi
