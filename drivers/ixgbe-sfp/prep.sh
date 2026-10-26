#!/bin/bash
# Stage config for an M.2 SFP+ card (Intel 82599, ixgbe) before the card is installed.
# Run with sudo. Backs up first; rollback.sh undoes it. Nothing changes on the live network:
# the netplan entry matches driver ixgbe, which no device uses until the card is in.
# Replace the 192.0.2.x addresses (documentation range) with your own before running.
set -euo pipefail
NETPLAN=/etc/netplan/70-sfp-10g.yaml
STATE=/etc/modprobe.d/backups/ixgbe-sfp.last
[ -e "$NETPLAN" ] && { echo "refusing: $NETPLAN already exists (if an earlier prep.sh made it, run rollback.sh; otherwise move it aside yourself)"; exit 1; }
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p /etc/netplan/backups /etc/modprobe.d/backups
for f in /etc/netplan/*.yaml; do cp -p "$f" "/etc/netplan/backups/$(basename "$f").bak-sfp-$TS"; done
[ -f /etc/modprobe.d/ixgbe.conf ] && cp -p /etc/modprobe.d/ixgbe.conf "/etc/modprobe.d/backups/ixgbe.conf.bak-sfp-$TS"
cp -p "/boot/initrd.img-$(uname -r)" "/boot/initrd.img-$(uname -r).bak-sfp-$TS"
echo "$TS" > "$STATE"
echo "backups: /etc/netplan/backups/*.bak-sfp-$TS, /boot/initrd.img-$(uname -r).bak-sfp-$TS"

# Third-party (non-Intel) module: ixgbe refuses it without this.
echo "options ixgbe allow_unsupported_sfp=1" > /etc/modprobe.d/ixgbe.conf

# Same static address as the onboard-NIC fallback; only one cable is ever connected.
cat > "$NETPLAN" <<'YAML'
network:
  version: 2
  ethernets:
    sfp10g:
      match:
        driver: ixgbe
      addresses: [192.0.2.26/24]
      routes:
        - to: default
          via: 192.0.2.1
          metric: 50
      nameservers:
        addresses: [192.0.2.1]
      optional: true
YAML
chmod 600 "$NETPLAN"

# ixgbe is in the initramfs, so the option must be there too.
update-initramfs -u -k "$(uname -r)"
netplan generate && echo "netplan generate OK"
lsinitramfs "/boot/initrd.img-$(uname -r)" | grep "etc/modprobe.d/ixgbe.conf" >/dev/null \
  && echo "initramfs has ixgbe.conf" || echo "WARN: ixgbe.conf not in initramfs"
