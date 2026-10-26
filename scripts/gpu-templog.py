#!/usr/bin/env python3
# gpu-templog: 1 s CSV of GPU VRAM/junction/core, GPU power, CPU Tdie, NCT6797 temps, case-fan rpm+duty
# (fan4 side+rear, fan5 bottom, fan6 GPU Thermalrights since the 2026-10-21 rewire; fan3/fan5 before).
# Read-only. Used for the 2026-10-01 case-fan tests. Usage: gpu-templog.py <seconds> <out.csv>
import ctypes as C, re, time, sys, os
DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 900
OUT = sys.argv[2]
NCT, ZEN = "/sys/class/hwmon/hwmon2", "/sys/class/hwmon/hwmon3"
TXT = "/var/lib/node_exporter/textfile_collector/gpu_vram_temps.prom"
nv = C.CDLL("libnvidia-ml.so.1"); nv.nvmlInit_v2()
hs = []
for i in range(2):
    h = C.c_void_p(); nv.nvmlDeviceGetHandleByIndex_v2(i, C.byref(h)); hs.append(h)
def rd(p):
    try: return open(p).read().strip()
    except OSError: return ""
cols = ["t","fan4","fan5","fan6","pwm4","pwm5","pwm6","g0_vram","g1_vram","g0_junc","g1_junc","g0_core","g1_core","g0_w","g1_w","cpu_tdie","systin","auxtin0","auxtin2","chip_virt"]
with open(OUT, "w") as f:
    f.write(",".join(cols) + "\n")
    end = time.time() + DUR
    while time.time() < end:
        t = open(TXT).read() if os.path.exists(TXT) else ""
        g = {(m[1], m[2]): m[3] for m in re.finditer(r'gpu_(\w+)_temp_celsius\{gpu="(\d)"\} (\S+)', t)}
        w = []
        for h in hs:
            mw = C.c_uint(); nv.nvmlDeviceGetPowerUsage(h, C.byref(mw)); w.append(f"{mw.value/1000:.0f}")
        row = [time.strftime("%H:%M:%S"), *(rd(f"{NCT}/fan{n}_input") for n in (4, 5, 6)), *(rd(f"{NCT}/pwm{n}") for n in (4, 5, 6)),
               g.get(("vram","0"),""), g.get(("vram","1"),""), g.get(("junction","0"),""), g.get(("junction","1"),""),
               g.get(("core","0"),""), g.get(("core","1"),""), *w,
               str(int(rd(f"{ZEN}/temp1_input") or 0)/1000), *(str(int(rd(f"{NCT}/temp{i}_input") or 0)/1000) for i in (1,3,5,7))]
        f.write(",".join(row) + "\n"); f.flush()
        time.sleep(1)
