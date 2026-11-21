# M.2 SFP+ card (Intel 82599, `ixgbe`)

- `prep.sh` stages the module option and a netplan file before the card goes in (backs up first; the new
  interface is `optional` so a missing card can't hold up boot).
- `rollback.sh` undoes it.

## After the cutover (2026-10-27)

Once the SFP+ port is the only cable, flip which port is required. Otherwise `systemd-networkd-wait-online`
waits its full 2-minute timeout on every boot for the unplugged onboard port. `/etc/netplan/70-sfp-10g.yaml`
(addresses are documentation placeholders):

```yaml
network:
  version: 2
  ethernets:
    sfp10g:            # required: boot waits for this one
      match:
        driver: ixgbe
      addresses: [192.0.2.26/24]
      routes:
        - to: default
          via: 192.0.2.1
          metric: 50
      nameservers:
        addresses: [192.0.2.1]
    # old onboard copper port, no cable since the fiber move; don't hold up boot for it
    enp42s0:
      optional: true
```

`sudo netplan generate && /usr/lib/systemd/systemd-networkd-wait-online` should then return at once (0.008 s here).
