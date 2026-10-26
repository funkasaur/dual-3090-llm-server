#!/bin/bash
# Undo prep.sh. Run with sudo.
set -euo pipefail
rm -f /etc/netplan/70-sfp-10g.yaml /etc/modprobe.d/ixgbe.conf
update-initramfs -u -k "$(uname -r)"
netplan generate && echo "rolled back; netplan generate OK"
