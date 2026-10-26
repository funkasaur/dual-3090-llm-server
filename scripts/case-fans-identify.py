#!/usr/bin/env python3
"""case-fans-identify: find which motherboard fan header (nct6797 channel) drives which physical fan.

MSI MEG X570 GODLIKE, Nuvoton NCT6797 (hwmon "nct6797"). The board has 9 headers (PUMP_FAN1, CPU_FAN1,
SYS_FAN1-7) but the NCT6797 has 7 channels, so at least two headers sit on a controller Linux does not see.
Channel -> header names are not published for this board, so test instead:
for each channel in CHANNELS, one at a time, set it to 100 % for HOLD_S seconds, print RPM before / at
100 % / after, then restore its exact previous mode (pwmN_enable) and duty (pwmN). Watch the case and
note which fan speeds up.

Only ever RAISES a fan to 100 % (harmless for any fan, the AIO pump included), so all 7 channels are
tested by default. A header that never responds to any channel is on the hidden controller. Previous
settings are restored on exit, Ctrl-C or error.

  sudo case-fans-identify.py              # fan1..fan7, 8 s each, 4 s gap (~90 s)
  sudo case-fans-identify.py 4 5          # only fan4 and fan5
  sudo case-fans-identify.py 6 7 --hold 10   # fan6 and fan7, 10 s each
  sudo case-fans-identify.py --lead 10 --hold 10 --gap 5   # 10 s to get ready, 10 s at 100 %, 5 s between
  sudo case-fans-identify.py 5 --stop --hold 10   # STOP fan5 for 10 s instead (easier to see)
  sudo case-fans-identify.py 5 --pulse            # fan5: 20 % for 5 s, then 100 % for 20 s (feel by hand)
  sudo case-fans-identify.py 3 5 --together --hold 15   # fan3 AND fan5 at 100 % at the same time

--stop sets 0 % instead of 100 % (ARCTIC P14 Pro stops below 5 % PWM), which is far easier to spot in a
case full of fans. The Thermalright TL-M12QW pair on fan6 does not stop: it floors at ~595 rpm (10-21). It is the one mode that lowers a fan, so it is only allowed on STOP_OK channels: the
ones established as case fans (remapped 2026-10-21 after a rewire: fan4 = SYS_FAN2 side P14s + rear P12,
fan5 = SYS_FAN3 bottom P14s, fan6 = SYS_FAN4 GPU Thermalrights).
Never fan1 / fan2 / fan3 (AIO pump, pump-head fan, AIO radiator) or fan7 (the X570 chipset fan).
CPU guard during --stop: a stopped set could still be the 420 mm AIO's radiator fans on SYS_FAN1 (140 mm
fans reach ~2350 rpm like a P14 Pro), so the CPU die temperature (zenpower Tdie) is read every 0.25 s and
the channel is restored at once if it rises CPU_RISE_C or reaches CPU_MAX_C.
Needs root (writes /sys/class/hwmon/*/pwmN*). Read-only without root: --dry-run.
"""
import glob
import os
import sys
import time

CHANNELS, HOLD_S, GAP_S, LEAD_S = [1, 2, 3, 4, 5, 6, 7], 8, 4, 5
STOP_OK = {4, 5, 6}
CPU_RISE_C, CPU_MAX_C = 5, 75
PULSE = ((51, 5, "20 %"), (255, 20, "100 %"))   # (duty/255, seconds, label): low then high


def hwmon():
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        if open(f"{d}/name").read().strip() == "nct6797":
            return d
    sys.exit("nct6797 hwmon not found")


def rd(path):
    try:
        return open(path).read().strip()
    except OSError:
        return "?"


def wr(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def cpu_temp():
    """zenpower Tdie in C, or None."""
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        if rd(f"{h}/name") == "zenpower":
            v = rd(f"{h}/temp1_input")
            return int(v) / 1000 if v.isdigit() else None
    return None


def hold_with_guard(hold, guarded):
    """Sleep hold s; if guarded, return early (with a reason) when the CPU heats up."""
    if not guarded:
        time.sleep(hold)
        return None
    start = cpu_temp()
    if start is None:
        return "no CPU temperature reading - not stopping a fan without the guard"
    end = time.time() + hold
    while time.time() < end:
        t = cpu_temp()
        if t is not None and (t >= CPU_MAX_C or t - start >= CPU_RISE_C):
            return f"CPU {start:.0f} -> {t:.0f} C: restored early"
        time.sleep(0.25)
    return f"CPU {start:.0f} -> {cpu_temp():.0f} C"


def main():
    d = hwmon()
    argv = sys.argv[1:]
    hold, gap, lead = HOLD_S, GAP_S, LEAD_S
    for flag in ("--hold", "--gap", "--lead"):
        if flag in argv:
            i = argv.index(flag)
            val = int(argv[i + 1])
            del argv[i:i + 2]
            if not 1 <= val <= 300:
                sys.exit(f"{flag} must be 1-300 seconds")
            if flag == "--hold":
                hold = val
            elif flag == "--gap":
                gap = val
            else:
                lead = val
    chans = [int(a) for a in argv if not a.startswith("-")] or CHANNELS
    if any(not 1 <= n <= 7 for n in chans):
        sys.exit("channels are 1-7")
    stop, pulse, together = "--stop" in argv, "--pulse" in argv, "--together" in argv
    if together and (stop or pulse):
        sys.exit("--together only raises (100 %); it cannot be combined with --stop / --pulse")
    if (stop or pulse) and not set(chans) <= STOP_OK:
        sys.exit(f"--stop / --pulse lower a fan, so only fan{sorted(STOP_OK)} (never the AIO candidates or the chipset fan)")
    duty, label = (0, "0 % (STOP)") if stop else (255, "100 %")
    dry = "--dry-run" in sys.argv
    if not dry and os.geteuid() != 0:
        sys.exit("needs root (or --dry-run)")
    saved = {n: (rd(f"{d}/pwm{n}_enable"), rd(f"{d}/pwm{n}")) for n in chans}
    print(f"{d} ({rd(f'{d}/name')}); saved settings (mode, duty/255): {saved}")
    rpm = lambda n: rd(f"{d}/fan{n}_input")

    def restore():
        for n, (mode, duty) in saved.items():
            if dry or mode == "?":
                continue
            try:
                wr(f"{d}/pwm{n}_enable", mode)
                if mode == "1":           # manual mode: put the exact duty back too
                    wr(f"{d}/pwm{n}", duty)
            except OSError as e:
                print(f"  RESTORE FAILED fan{n}: {e}  (was mode {mode}, duty {duty}; reboot also restores)")

    try:
        if not dry:
            print(f"starting in {lead} s - watch the case")
            time.sleep(lead)
        if together:
            before = {n: rpm(n) for n in chans}
            print(f"{' + '.join(f'fan{n}' for n in chans)}: {before} rpm -> 100 % together for {hold} s ...",
                  end="", flush=True)
            if not dry:
                print(f"\n  START {time.strftime('%H:%M:%S')}", flush=True)
                for n in chans:
                    wr(f"{d}/pwm{n}_enable", 1)
                    wr(f"{d}/pwm{n}", 255)
                time.sleep(hold)
                at = {n: rpm(n) for n in chans}
                restore()
                print(f"  END   {time.strftime('%H:%M:%S')}  at 100 %: {at} rpm", flush=True)
                time.sleep(gap)
                print(f"  after: {({n: rpm(n) for n in chans})} rpm")
            else:
                print(" (dry run, nothing set)")
            chans = []
        for n in chans:
            before = rpm(n)
            print(f"fan{n}: {before} rpm -> " + ("20 % 5 s, then 100 % 20 s" if pulse else f"{label} for {hold} s") + " ...", end="", flush=True)
            if dry:
                print(" (dry run, nothing set)")
                continue
            if (stop or pulse) and cpu_temp() is None:
                print(" skipped: no CPU temperature reading for the guard")
                continue
            wr(f"{d}/pwm{n}_enable", 1)   # manual
            if pulse:
                notes = []
                for pct, secs, lab in PULSE:
                    wr(f"{d}/pwm{n}", pct)
                    print(f"\n  {lab} for {secs} s", end="", flush=True)
                    note = hold_with_guard(secs, pct < 255)
                    notes.append(f"{lab}: {rpm(n)} rpm" + (f" ({note})" if note and "early" in note else ""))
                    if note and "early" in note:
                        break               # guard tripped: skip the rest, restore below
                at, note = " / ".join(notes), None
                print()
            else:
                wr(f"{d}/pwm{n}", duty)
                note = hold_with_guard(hold, stop)
                at = rpm(n)
            wr(f"{d}/pwm{n}_enable", saved[n][0])
            if saved[n][0] == "1":
                wr(f"{d}/pwm{n}", saved[n][1])
            time.sleep(gap)
            print(f" {at}" + ("" if pulse else f" rpm at {label}") + f", back to {rpm(n)} rpm (mode {rd(f'{d}/pwm{n}_enable')})"
                  + (f"  [{note}]" if note else ""))
    finally:
        restore()
    if not dry:
        print("restored:", {n: (rd(f"{d}/pwm{n}_enable"), rd(f"{d}/pwm{n}")) for n in saved})


if __name__ == "__main__":
    main()
