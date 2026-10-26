#!/usr/bin/env python3
"""gpu-fan-curve: drive the 3090 fans from VRAM / junction temperature, not just the core.

Why: the driver's automatic curve follows the GPU core. Under a long Qwen run at the 270 W cap the
core sits at 55-62 C, so the fans stay at 54-66 %, while the GDDR6X backside memory runs 88-94 C
(peaks of 96 C, Sep 24-25). The driver never sees that number.

Every 2 s, per GPU: target = max(curve(VRAM), curve(junction), curve(core), curve(power)), linear
between the points below. The power floor is for the Strix's zero-RPM mode: the driver keeps the fans
STOPPED until the core reaches ~50-55 C, so at the start of a run the card can draw 210 W with 0 % fans
while the memory heats up ahead of the core (seen 2026-10-01 07:35, GPU 0). Rises apply at once; falls by at most FALL_PER_STEP % per step, so the fans don't hunt. When
every reading is below its curve for 60 s the fans go back to the driver's automatic control.

Temperatures: VRAM + junction from gpu-vram-temps.sh's textfile (written every 2 s); core from NVML.
Fail-safe: textfile older than 15 s or unreadable -> fixed STALE_PCT (GpuTempCollectorStale alerts on
that). On exit (stop, SIGTERM, crash) every fan is handed back to the driver's automatic curve.
Needs root (NVML fan control). Writes gpu_fan_curve.prom next to the temps for Prometheus.
"""
import ctypes as C
import os
import re
import signal
import sys
import time

TEXTFILE = "/var/lib/node_exporter/textfile_collector/gpu_vram_temps.prom"
OUT = "/var/lib/node_exporter/textfile_collector/gpu_fan_curve.prom"
CURVES = {  # (temp C, fan %)
    "vram": [(80, 60), (86, 72), (90, 85), (94, 100)],
    "junction": [(80, 60), (88, 85), (92, 100)],
    "core": [(65, 60), (75, 80), (83, 100)],
    "power": [(120, 40), (200, 50), (270, 60)],  # watts -> fan %; spins up the moment real load starts
}
# Per-GPU head start in C, added to VRAM/junction before the curve. The two Strix cards are stacked
# for NVLink with little room between them; GPU 1 runs 6-10 C hotter VRAM at the same load
# (peaks 94-96 vs 86), so its fans ramp 4 C earlier.
GPU_BIAS = {1: 4}
# 2 s loop to match gpu-vram-temps.timer (2 s since 2026-10-01); falls 1 %/2 s = same 0.5 %/s as before.
# Stale after 15 s (~7 missed samples) -> fixed STALE_PCT.
STEP_S, FALL_PER_STEP, AUTO_AFTER_S, STALE_S, STALE_PCT = 2, 1, 60, 15, 80

nvml = C.CDLL("libnvidia-ml.so.1")


def check(rc, what):
    if rc != 0:
        raise RuntimeError(f"{what}: NVML error {rc}")


def curve(points, t):
    if t is None or t < points[0][0]:
        return 0
    for (t0, f0), (t1, f1) in zip(points, points[1:]):
        if t <= t1:
            return f0 + (f1 - f0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def read_textfile():
    """{gpu index: {"vram": C, "junction": C}} or None if stale/unreadable."""
    try:
        if time.time() - os.path.getmtime(TEXTFILE) > STALE_S:
            return None
        out = {}
        for m in re.finditer(r'gpu_(vram|junction)_temp_celsius\{gpu="(\d+)"\} (\S+)', open(TEXTFILE).read()):
            out.setdefault(int(m[2]), {})[m[1]] = float(m[3])
        return out or None
    except OSError:
        return None


class Gpu:
    def __init__(self, i):
        self.i, self.h = i, C.c_void_p()
        check(nvml.nvmlDeviceGetHandleByIndex_v2(i, C.byref(self.h)), f"handle {i}")
        n = C.c_uint()
        check(nvml.nvmlDeviceGetNumFans(self.h, C.byref(n)), "fans")
        self.fans, self.pct, self.cool_since = n.value, None, None  # pct None = driver auto

    def core(self):
        t = C.c_uint()
        return float(t.value) if nvml.nvmlDeviceGetTemperature(self.h, 0, C.byref(t)) == 0 else None

    def power(self):
        mw = C.c_uint()
        return mw.value / 1000 if nvml.nvmlDeviceGetPowerUsage(self.h, C.byref(mw)) == 0 else None

    def set(self, pct):
        pct = int(round(max(30, min(100, pct))))
        if pct != self.pct:
            for f in range(self.fans):
                check(nvml.nvmlDeviceSetFanSpeed_v2(self.h, f, C.c_uint(pct)), f"set gpu{self.i} fan{f}")
            self.pct = pct

    def auto(self):
        if self.pct is not None:
            for f in range(self.fans):
                nvml.nvmlDeviceSetDefaultFanSpeed_v2(self.h, f)
            self.pct = None


def write_prom(gpus, stale):
    lines = ["# HELP gpu_fan_curve_target_percent Fan % set by gpu-fan-curve; -1 = driver automatic.",
             "# TYPE gpu_fan_curve_target_percent gauge"]
    lines += [f'gpu_fan_curve_target_percent{{gpu="{g.i}"}} {-1 if g.pct is None else g.pct}' for g in gpus]
    lines += ["# HELP gpu_fan_curve_stale 1 when temps were stale and the fixed fail-safe speed is in use.",
              "# TYPE gpu_fan_curve_stale gauge", f"gpu_fan_curve_stale {int(stale)}",
              "# TYPE gpu_fan_curve_last_run_timestamp_seconds gauge",
              f"gpu_fan_curve_last_run_timestamp_seconds {time.time():.0f}"]
    tmp = OUT + ".tmp"
    open(tmp, "w").write("\n".join(lines) + "\n")
    os.replace(tmp, OUT)


def rpm(g, f):
    class R(C.Structure):
        _fields_ = [("version", C.c_uint), ("fan", C.c_uint), ("speed", C.c_uint)]
    r = R(version=C.sizeof(R) | (1 << 24), fan=f)
    return r.speed if nvml.nvmlDeviceGetFanSpeedRPM(g.h, C.byref(r)) == 0 else None


def policy(g, f):
    p = C.c_uint()  # 0 = driver automatic (temperature-controlled), 1 = manual
    return p.value if nvml.nvmlDeviceGetFanControlPolicy_v2(g.h, f, C.byref(p)) == 0 else None


def selftest(gpus, steps=(60, 70, 80, 90), hold=10):
    """--test: ramp every fan through steps (hold s each, 40 s total), print RPM at each step,
    hand back to the driver, confirm every channel is on driver auto again."""
    def show(tag):
        for g in gpus:
            print(f"  {tag:8} gpu{g.i}: " + "; ".join(
                f"fan{f} {rpm(g, f)} rpm policy {policy(g, f)}" for f in range(g.fans)), flush=True)
    show("before")
    try:
        for pct in steps:
            for g in gpus:
                g.set(pct)
            print(f"-- {pct}% for {hold} s", flush=True)
            time.sleep(hold)
            show(f"at {pct}%")
    finally:
        for g in gpus:
            g.auto()
    time.sleep(5)
    show("restored")
    bad = [g.i for g in gpus for f in range(g.fans) if policy(g, f) != 0]
    print("RESULT:", "FAIL - not back on driver auto: gpu" + str(bad) if bad else "OK - fans responded and are back on driver auto")
    return 1 if bad else 0


def main():
    check(nvml.nvmlInit_v2(), "init")
    n = C.c_uint()
    check(nvml.nvmlDeviceGetCount_v2(C.byref(n)), "count")
    gpus = [Gpu(i) for i in range(n.value)]
    if "--test" in sys.argv:
        return selftest(gpus)

    def restore(*_):
        for g in gpus:
            g.auto()
        print("fans handed back to the driver", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, restore)
    signal.signal(signal.SIGINT, restore)
    try:
        while True:
            temps = read_textfile()
            for g in gpus:
                if temps is None:
                    g.set(STALE_PCT)
                    continue
                t = temps.get(g.i, {})
                b = GPU_BIAS.get(g.i, 0)
                vram, junction = (t.get(k) + b if t.get(k) is not None else None for k in ("vram", "junction"))
                want = max(curve(CURVES["vram"], vram), curve(CURVES["junction"], junction),
                           curve(CURVES["core"], g.core()), curve(CURVES["power"], g.power()))
                now = time.time()
                if want == 0:  # everything below its curve
                    g.cool_since = g.cool_since or now
                    if g.pct is not None and now - g.cool_since >= AUTO_AFTER_S:
                        g.auto()
                        print(f"gpu{g.i}: cool for {AUTO_AFTER_S}s -> driver auto", flush=True)
                    elif g.pct is not None:  # ease down while waiting to hand back (never up)
                        g.set(g.pct - FALL_PER_STEP)
                    continue
                g.cool_since = None
                new = want if g.pct is None or want >= g.pct else max(want, g.pct - FALL_PER_STEP)
                if g.pct is None or abs(new - g.pct) >= 1:
                    print(f"gpu{g.i}: vram {t.get('vram')} junction {t.get('junction')} -> fan {new:.0f}%", flush=True)
                g.set(new)
            write_prom(gpus, temps is None)
            time.sleep(STEP_S)
    finally:
        for g in gpus:
            g.auto()


if __name__ == "__main__":
    sys.exit(main())
