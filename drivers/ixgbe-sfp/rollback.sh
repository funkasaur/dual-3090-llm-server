#!/bin/bash
# Undo prep.sh. Run with sudo. Restores an ixgbe.conf that existed before prep.sh, else removes it.
set -euo pipefail
STATE=/etc/modprobe.d/backups/ixgbe-sfp.last
TS=$(cat "$STATE" 2>/dev/null || true)
[ -n "$TS" ] || { echo "refusing: no $STATE, so prep.sh did not run here; nothing changed"; exit 1; }
rm -f /etc/netplan/70-sfp-10g.yaml
if [ -f "/etc/modprobe.d/backups/ixgbe.conf.bak-sfp-$TS" ]; then
    cp -p "/etc/modprobe.d/backups/ixgbe.conf.bak-sfp-$TS" /etc/modprobe.d/ixgbe.conf
    echo "restored ixgbe.conf from the $TS backup"
else
    rm -f /etc/modprobe.d/ixgbe.conf
fi
rm -f "$STATE"
update-initramfs -u -k "$(uname -r)"
netplan generate && echo "rolled back; netplan generate OK"
