#!/usr/bin/env bash
#
# optimize-ai.sh — keep GPU hardware interrupts off the inference cores on a
# dual-CCD Ryzen host (tested on a 5950X: CCD0 = cores 0-7, CCD1 = cores 8-15).
#
# What it does:
#   1. Waits for the NVIDIA driver to register its interrupts.
#   2. Tells irqbalance never to place interrupts on CCD0.
#   3. Bans the GPU IRQs from irqbalance entirely and pins them by hand to a
#      single "conductor" core, so NVLink/PCIe interrupt handling never lands
#      on a core that is busy doing matrix math.
#   4. Reports any *kernel-managed* interrupts that are still stuck on the
#      inference cores, because those cannot be moved by any userspace tool.
#
# Run as root. Idempotent — safe to re-run at any time.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Tunables (override via environment)
# ---------------------------------------------------------------------------

# Physical core dedicated to GPU interrupt handling. Must be on the same CCD
# as the inference cores so interrupt data stays in the same L3 pool.
CONDUCTOR_CORE="${CONDUCTOR_CORE:-7}"

# Cores the inference engine owns. Used only for the advisory report at the end.
INFERENCE_CPULIST="${INFERENCE_CPULIST:-0-4,16-20}"

# CPUs irqbalance must never assign interrupts to (all of CCD0 + SMT siblings).
BANNED_CPULIST="${BANNED_CPULIST:-0-7,16-23}"

# How long to wait for NVIDIA IRQs to appear before giving up.
WAIT_SECS="${WAIT_SECS:-90}"

log()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root (try: sudo $0)"

# ---------------------------------------------------------------------------
# 1. Wait for the NVIDIA driver to register its interrupts
# ---------------------------------------------------------------------------
# This is the step the original script was missing. At boot the unit could
# start before the NVIDIA kernel module had bound the cards, /proc/interrupts
# had no "nvidia" lines, and the script exited 1 — leaving the GPU IRQs
# unpinned for the entire uptime of the machine with no visible symptom.

find_nvidia_irqs() {
    awk -F: '/nvidia/ { gsub(/ /, "", $1); print $1 }' /proc/interrupts
}

log "Waiting up to ${WAIT_SECS}s for NVIDIA interrupts to appear..."
deadline=$(( SECONDS + WAIT_SECS ))
IRQS=()
while :; do
    mapfile -t IRQS < <(find_nvidia_irqs)
    (( ${#IRQS[@]} > 0 )) && break
    (( SECONDS >= deadline )) && break
    sleep 2
done

if (( ${#IRQS[@]} == 0 )); then
    die "no NVIDIA interrupts found after ${WAIT_SECS}s. Is the driver loaded? (nvidia-smi)"
fi

log "Found ${#IRQS[@]} NVIDIA IRQ(s): ${IRQS[*]}"

# ---------------------------------------------------------------------------
# 2. Work out which SMT threads belong to the conductor core
# ---------------------------------------------------------------------------
# Derived from sysfs instead of hardcoded, so this keeps working if you move
# the conductor to a different core or run it on a different CPU.

sibfile="/sys/devices/system/cpu/cpu${CONDUCTOR_CORE}/topology/thread_siblings_list"
[[ -r $sibfile ]] || die "cannot read $sibfile — is core ${CONDUCTOR_CORE} present?"

# Read the file directly rather than through `tr -d '\n'`. `read` returns 1 on
# EOF-without-newline even when it has successfully populated the array, and
# under `set -e` that non-zero status kills the script silently — no error, no
# output, just an exit. Reading the sysfs file keeps its trailing newline, so
# read returns 0.
IFS=',' read -r -a CONDUCTOR_THREADS < "$sibfile"
(( ${#CONDUCTOR_THREADS[@]} > 0 )) || die "could not determine SMT siblings for core ${CONDUCTOR_CORE}"

log "Conductor core ${CONDUCTOR_CORE} -> threads: ${CONDUCTOR_THREADS[*]}"

# ---------------------------------------------------------------------------
# 3. Configure irqbalance
# ---------------------------------------------------------------------------
# IRQBALANCE_BANNED_CPULIST is the correct variable for irqbalance >= 1.8.
# The older IRQBALANCE_BANNED_CPUS (hex mask) is deprecated and ignored.

banirq_args=""
for irq in "${IRQS[@]}"; do
    banirq_args+=" --banirq=${irq}"
done

install -d -m 0755 /etc/systemd/system/irqbalance.service.d
cat > /etc/systemd/system/irqbalance.service.d/override.conf <<EOF
# Managed by optimize-ai.sh — regenerated on every run. Do not edit by hand.
[Service]
Environment="IRQBALANCE_BANNED_CPULIST=${BANNED_CPULIST}"
Environment="IRQBALANCE_ARGS=${banirq_args# }"
EOF

log "irqbalance: banned from CPUs ${BANNED_CPULIST}; GPU IRQs excluded from balancing."

systemctl daemon-reload
if systemctl is-enabled --quiet irqbalance 2>/dev/null || systemctl is-active --quiet irqbalance; then
    systemctl restart irqbalance
    log "irqbalance restarted."
else
    warn "irqbalance is not enabled/active — the ban list will apply if you start it later."
fi

# ---------------------------------------------------------------------------
# 4. Pin the GPU IRQs to the conductor core
# ---------------------------------------------------------------------------
# Spread across the conductor's SMT threads round-robin. Each write is read
# back, because the kernel silently refuses affinity changes on managed
# interrupts and a blind write would look like it succeeded.

pin_failures=0
idx=0
for irq in "${IRQS[@]}"; do
    target="${CONDUCTOR_THREADS[$(( idx % ${#CONDUCTOR_THREADS[@]} ))]}"
    idx=$(( idx + 1 ))

    afile="/proc/irq/${irq}/smp_affinity_list"
    if [[ ! -w $afile ]]; then
        warn "IRQ ${irq}: ${afile} is not writable; skipping."
        pin_failures=$(( pin_failures + 1 ))
        continue
    fi

    if ! printf '%s\n' "$target" > "$afile" 2>/dev/null; then
        warn "IRQ ${irq}: kernel refused affinity change (managed interrupt?); left as-is."
        pin_failures=$(( pin_failures + 1 ))
        continue
    fi

    readback="$(cat "$afile")"
    if [[ $readback == "$target" ]]; then
        log "IRQ ${irq} -> CPU ${target} (verified)"
    else
        warn "IRQ ${irq}: wrote '${target}' but kernel reports '${readback}'."
        pin_failures=$(( pin_failures + 1 ))
    fi
done

# ---------------------------------------------------------------------------
# 5. Advisory: what is still hitting the inference cores
# ---------------------------------------------------------------------------
# NVMe submission queues are *managed* interrupts: the kernel binds one queue
# per CPU and neither irqbalance nor a manual write can move them. On this
# class of build they are, by a wide margin, the largest remaining source of
# interrupts on the inference cores. See docs/irq-pinning.md.

expand_cpulist() {
    local part lo hi c
    local -a parts=() out=()
    IFS=',' read -r -a parts <<< "$1"
    for part in "${parts[@]}"; do
        if [[ $part == *-* ]]; then
            lo=${part%-*}; hi=${part#*-}
            for (( c = lo; c <= hi; c++ )); do out+=("$c"); done
        else
            out+=("$part")
        fi
    done
    printf '%s\n' "${out[@]}"
}

mapfile -t inference_cpus < <(expand_cpulist "$INFERENCE_CPULIST")

log ""
log "--- Interrupts still bound to inference CPUs (${INFERENCE_CPULIST}) ---"
found_noise=0
for irqdir in /proc/irq/[0-9]*; do
    irq="${irqdir##*/}"
    [[ -r "${irqdir}/effective_affinity_list" ]] || continue
    eff="$(cat "${irqdir}/effective_affinity_list" 2>/dev/null)" || continue
    # Only flag interrupts hard-bound to a single CPU; a wide mask is not noise.
    [[ $eff =~ ^[0-9]+$ ]] || continue
    for cpu in "${inference_cpus[@]}"; do
        if [[ $eff == "$cpu" ]]; then
            # Skip interrupts that have never fired. Legacy ISA lines (1, 3-15)
            # sit on CPU 0 with no device attached and zero count; listing them
            # buries the entries that actually matter.
            count="$(awk -v i="${irq}:" '$1 == i { s = 0; for (n = 2; n <= NF - 2; n++) s += $n; print s }' /proc/interrupts)"
            [[ -n $count && $count -gt 0 ]] || break

            dev="$(awk -v i="${irq}:" '$1 == i { print $NF }' /proc/interrupts)"
            log "  IRQ ${irq} (${dev:-unknown}) on CPU ${eff} — ${count} interrupts since boot"
            found_noise=1
            break
        fi
    done
done
(( found_noise )) || log "  (none)"
log "Managed interrupts above cannot be relocated from userspace."
log "-------------------------------------------------------------"

log ""
log "Suggested placement for this topology:"
log "  Inference engine   : cpuset '${INFERENCE_CPULIST}', OMP_NUM_THREADS=5"
log "  Web / ingress      : cpuset '5-6,21-22'"
log "  Databases / RAG    : cpuset '8-15,24-31'"

if (( pin_failures > 0 )); then
    warn "${pin_failures} IRQ(s) could not be pinned — see messages above."
    exit 1
fi

log "GPU interrupt pinning complete."
