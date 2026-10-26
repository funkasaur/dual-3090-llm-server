#!/bin/bash
# Stage config for the Vogzone M.2 SFP+ (Intel 82599, ixgbe) before the card is installed.
# Run with sudo. Backs up first; rollback.sh undoes it. Nothing changes on the live network:
# the netplan entry matches driver ixgbe, which no device uses until the card is in.
set -euo pipefail
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p /etc/netplan/backups /etc/modprobe.d/backups
for f in /etc/netplan/*.yaml; do cp -p "$f" "/etc/netplan/backups/$(basename "$f").bak-sfp-$TS"; done
[ -f /etc/modprobe.d/ixgbe.conf ] && cp -p /etc/modprobe.d/ixgbe.conf "/etc/modprobe.d/backups/ixgbe.conf.bak-sfp-$TS"
cp -p "/boot/initrd.img-$(uname -r)" "/boot/initrd.img-$(uname -r).bak-sfp-$TS"
echo "backups: /etc/netplan/backups/*.bak-sfp-$TS, /boot/initrd.img-$(uname -r).bak-sfp-$TS"

# Ubiquiti-coded module: ixgbe refuses non-Intel SFP+ without this.
echo "options ixgbe allow_unsupported_sfp=1" > /etc/modprobe.d/ixgbe.conf

# Same static address as the enp42s0 fallback; only one cable is ever connected.
cat > /etc/netplan/70-sfp-10g.yaml <<'EOF'
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
EOF
chmod 600 /etc/netplan/70-sfp-10g.yaml

# ixgbe is in the initramfs, so the option must be there too.
update-initramfs -u -k "$(uname -r)"
netplan generate && echo "netplan generate OK"
lsinitramfs "/boot/initrd.img-$(uname -r)" | grep "etc/modprobe.d/ixgbe.conf" >/dev/null \
  && echo "initramfs has ixgbe.conf" || echo "WARN: ixgbe.conf not in initramfs"
