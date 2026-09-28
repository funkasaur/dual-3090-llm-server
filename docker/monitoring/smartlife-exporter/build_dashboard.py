"""Generates grafana-dashboard.json. Meter colours follow the entity (never rank);
palette validated for Grafana's dark panel surface (#181b1f)."""
import json

TOPO = json.load(open("topology.json"))["devices"]
def members(key, value):   # top-level plugs on a circuit
    return "|".join(n for n, d in TOPO.items() if not d.get("parent") and d.get(key) == value)

DS = {"type": "prometheus", "uid": "efnh5gcimbv28f"}
METERS = [  # billed (top-level) meters, in palette order = stacking order
    ("Office", "#3987e5"), ("Desktop", "#d95926"), ("Server", "#199e70"),
    ("TV and Audio", "#c98500"), ("Internet and Den", "#d55181"), ("NerdQaxe++", "#008300"),
]
CIRCUIT = "#9085e9"            # violet: circuits, distinct from every meter
ESTIMATE = "#8e8d86"           # neutral: modelled history
WARN, CRIT = "#fab219", "#d03b3b"
TOP = 'on(device_id) group_left() smartlife_device_topology{level="top"}'
# Drop samples taken while a plug reported >300 V: those are unscaled (10x) readings
# from before the exporter learned the 15EM plugs' tenths quirk.
def src(sel=""):
    """Best available power per plug: live reading (minus unscaled 15EM samples, V > 300), else Home
    Assistant history (backfilled, Dec 2024 on), else the UPS/flat estimate (30 days before Sep 26 21:07).
    "+ 0" drops the metric name so the three join into one series."""
    return (f"(((smartlife_power_watts{sel} and on(device_id) smartlife_voltage_volts < 300)"
            f" or smartlife_power_watts_history{sel} or smartlife_power_watts_estimate{sel}) + 0)")
W = src()
TOPS = "|".join(n.replace("+", "\\\\+") for n, d in TOPO.items() if not d.get("parent"))
W_TOP = src('{name=~"%s"}' % TOPS)   # billed meters, history-safe
GOOD, WARN_S, CRIT_S = "#0ca30c", "#fab219", "#d03b3b"   # reserved status colours
def kwh(step_s=60):   # energy over the panel range; gaps count as zero
    return f"sum_over_time({W}[$__range:{step_s}s]) * {step_s} / 3.6e6"
KWH = kwh()
AVG30 = f"sum(avg_over_time({W}[30d:5m]) * {TOP})"   # billed watts, 30-day average

_id = 0
def panel(kind, title, x, y, w, h, targets, **kw):
    global _id; _id += 1
    p = dict(id=_id, type=kind, title=title, gridPos=dict(x=x, y=y, w=w, h=h), datasource=DS,
             targets=[dict(datasource=DS, refId=chr(65 + i), **t) for i, t in enumerate(targets)])
    p.update(kw); return p

def fixed(color): return {"mode": "fixed", "fixedColor": color}
def color_overrides(prop="displayName", fmt="{}"):
    return [{"matcher": {"id": "byName", "options": fmt.format(n)},
             "properties": [{"id": "color", "value": fixed(c)}]} for n, c in METERS]
def thresholds(*steps): return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in steps]}

def stat(title, x, w, expr, unit, decimals, desc, size=34, **kw):
    return panel("stat", title, x, 0, w, 5, [dict(expr=expr, instant=True, legendFormat=title)],
                 description=desc,
                 options=dict(colorMode="none", graphMode="none", textMode="value", justifyMode="center", text=dict(valueSize=size),
                              reduceOptions=dict(calcs=["lastNotNull"], fields="", values=False)),
                 fieldConfig=dict(defaults=dict(unit=unit, decimals=decimals, color=fixed("text")), overrides=[]), **kw)

panels = []
# ---- Now strip ---------------------------------------------------------------
panels.append(panel("stat", "Home power now", 0, 0, 4, 5,
    [dict(expr=f"sum({W} * {TOP})", legendFormat="now")],
    description="Sum of the top-level (billed) meters. Sub-meters are already inside their parent.",
    timeFrom="6h", hideTimeOverride=True,
    options=dict(colorMode="none", graphMode="none", textMode="value", justifyMode="center", text=dict(valueSize=44),
                 reduceOptions=dict(calcs=["lastNotNull"], fields="", values=False)),
    fieldConfig=dict(defaults=dict(unit="watt", color=fixed("#6da7ec")), overrides=[])))
panels.append(stat("Today", 4, 3, f"sum({KWH} * {TOP})", "kwatth", 1,
                   "Energy since midnight, billed meters.", timeFrom="now/d", hideTimeOverride=True))
panels.append(stat("Cost today", 7, 3, f"sum({KWH} * {TOP}) * $rate", "currencyUSD", 2,
                   "Today's energy x the rate set at the top.", timeFrom="now/d", hideTimeOverride=True))
PROJ = "Last 30 days' average billed power x {h} h x rate."
panels.append(stat("Month est.", 10, 3, f"{AVG30} * 730 / 1000 * $rate", "currencyUSD", 0, PROJ.format(h=730)))
panels.append(stat("6 mo est.", 13, 3, f"{AVG30} * 4380 / 1000 * $rate", "currencyUSD", 0, PROJ.format(h=4380)))
panels.append(stat("Year est.", 16, 3, f"{AVG30} * 8760 / 1000 * $rate", "currencyUSD", 0, PROJ.format(h=8760)))
panels.append(panel("bargauge", "New 15 A circuit (after move)", 19, 0, 5, 5,
    [dict(expr='sum(smartlife_current_amps * on(device_id) group_left() '
               'smartlife_device_topology{level="top",planned_circuit="New 15 A"})', instant=True,
          legendFormat="Server + Desktop + Office")],
    description="Current draw of the meters planned for one 15 A circuit. Amber from 12 A "
                "(80% continuous-load limit), red from 15 A (breaker rating).",
    options=dict(orientation="horizontal", displayMode="basic", showUnfilled=True, valueMode="text", text=dict(valueSize=34),
                 namePlacement="top", reduceOptions=dict(calcs=["lastNotNull"], fields="", values=False)),
    fieldConfig=dict(defaults=dict(unit="amp", decimals=1, min=0, max=16, color={"mode": "thresholds"},
                                   thresholds=thresholds((None, CIRCUIT), (12, WARN), (15, CRIT))), overrides=[])))

# ---- Status strip and always-on baseline ------------------------------------------
panels.append(panel("stat", "Plug status", 0, 5, 14, 4,
    [dict(expr="max by (name) (smartlife_device_online) * (1 + max by (name) (smartlife_switch_on))",
          legendFormat="{{name}}", instant=True)],
    description="ON = online and relay closed; OFF = online, relay open (no power to that outlet); OFFLINE = plug not reachable.",
    options=dict(colorMode="value", graphMode="none", textMode="value_and_name", justifyMode="center", orientation="vertical",
                 text=dict(titleSize=12, valueSize=16), reduceOptions=dict(calcs=["lastNotNull"], fields="", values=False)),
    fieldConfig=dict(defaults=dict(mappings=[{"type": "value", "options": {
            "0": {"text": "OFFLINE", "color": CRIT_S, "index": 0}, "1": {"text": "OFF", "color": WARN_S, "index": 1},
            "2": {"text": "ON", "color": GOOD, "index": 2}}}],
        color={"mode": "fixed", "fixedColor": "text"}), overrides=[]),
    transformations=[{"id": "sortBy", "options": {"sort": [{"field": "Field", "desc": False}]}}]))
BASE = f"quantile_over_time(0.05, sum({W_TOP})[7d:5m])"
panels.append(panel("stat", "Always-on load", 14, 5, 5, 4, [dict(expr=BASE, instant=True, legendFormat="baseline")],
    description="What never turns off: 5th percentile of whole-home power over the last 7 days.",
    options=dict(colorMode="none", graphMode="none", textMode="value", justifyMode="center", text=dict(valueSize=30),
                 reduceOptions=dict(calcs=["lastNotNull"], fields="", values=False)),
    fieldConfig=dict(defaults=dict(unit="watt", decimals=0, color=fixed("text")), overrides=[])))
panels.append(panel("stat", "Always-on cost / month", 19, 5, 5, 4, [dict(expr=f"{BASE} * 730 / 1000 * $rate", instant=True, legendFormat="baseline")],
    description="The always-on load running for a whole month, at the rate set above.",
    options=dict(colorMode="none", graphMode="none", textMode="value", justifyMode="center", text=dict(valueSize=30),
                 reduceOptions=dict(calcs=["lastNotNull"], fields="", values=False)),
    fieldConfig=dict(defaults=dict(unit="currencyUSD", decimals=0, color=fixed("text")), overrides=[])))

# ---- Power by meter ------------------------------------------------------------
panels.append(panel("timeseries", "Power by meter (stacked = whole home)", 0, 9, 24, 10,
    [dict(expr=src('{name="%s"}' % n), legendFormat=n) for n, _ in METERS],
    description="Live plug readings from Sep 26 21:07; before that, Home Assistant history (hourly, 5-min for the "
                "last 10 days). NerdQaxe++ and LLM Server have no HA history (LLM Server uses its UPS for 30 days).",
    options=dict(legend=dict(displayMode="table", placement="right", calcs=["lastNotNull", "mean", "max"],
                             sortBy="Mean", sortDesc=True),
                 tooltip=dict(mode="multi", sort="none")),
    fieldConfig=dict(defaults=dict(unit="watt", min=0,
        custom=dict(drawStyle="line", lineWidth=0, fillOpacity=80, gradientMode="none", showPoints="never",
                    stacking=dict(mode="normal", group="A"), lineInterpolation="stepAfter",
                    axisSoftMin=0, axisBorderShow=False)),
        overrides=color_overrides())))

# ---- Circuits (small multiples, one series each + UPS-based estimate) --------------
def circuit_panel(title, x, key, value, desc):
    names = members(key, value)
    real = (f'sum(smartlife_current_amps{{name=~"{names}"}} and on(device_id) smartlife_voltage_volts < 300)')
    hist = "sum(%s) / 120" % src('{name=~"%s"}' % names)
    return panel("timeseries", title, x, 19, 8, 8, [dict(expr=f"{real} or on() {hist}", legendFormat="load")],
        description=desc + " Measured current from Sep 26 21:07; before that, power history / 120 V "
                           "(hourly averages hide short peaks). Dashed: 12 A continuous limit, 15 A breaker.",
        options=dict(legend=dict(displayMode="list", placement="bottom", showLegend=False),
                     tooltip=dict(mode="single", sort="none")),
        fieldConfig=dict(defaults=dict(unit="amp", decimals=1, min=0, max=18, color=fixed(CIRCUIT),
            thresholds=thresholds((None, "transparent"), (12, WARN), (15, CRIT)),
            custom=dict(drawStyle="line", lineWidth=1, fillOpacity=12, showPoints="never",
                        thresholdsStyle=dict(mode="dashed"), axisBorderShow=False)), overrides=[]))

panels.append(circuit_panel("Circuit A · Server + Desktop", 0, "circuit", "Circuit A", "Today's circuit."))
panels.append(circuit_panel("Circuit B · Office, TV, Internet", 8, "circuit", "Circuit B", "Today's circuit."))
panels.append(circuit_panel("After move · New 15 A (Server + Desktop + Office)", 16, "planned_circuit", "New 15 A",
                            "The three meters planned for one 15 A circuit."))

# ---- Mains voltage and year-over-year -------------------------------------------------
panels.append(panel("timeseries", "Mains voltage by circuit", 0, 27, 12, 8,
    [dict(expr='avg by (circuit) ((smartlife_voltage_volts < 300) * on(device_id) group_left(circuit) '
               'smartlife_device_topology{level="top",circuit!=""})', legendFormat="{{circuit}}")],
    description="Average of the plugs on each circuit. Dashed: ANSI C84.1 range B limits (110 / 127 V), "
                "which the MainsVoltageLow/High alerts use. Normal here is 113-122 V. Live readings only (Sep 26 on).",
    options=dict(legend=dict(displayMode="list", placement="bottom"), tooltip=dict(mode="multi", sort="none")),
    fieldConfig=dict(defaults=dict(unit="volt", decimals=1, min=105, max=130,
        thresholds=thresholds((None, "transparent"), (110, WARN), (127, WARN)),
        custom=dict(drawStyle="line", lineWidth=2, fillOpacity=0, showPoints="never",
                    thresholdsStyle=dict(mode="dashed"), axisBorderShow=False)),
        overrides=[{"matcher": {"id": "byName", "options": "Circuit A"}, "properties": [{"id": "color", "value": fixed(CIRCUIT)}]},
                   {"matcher": {"id": "byName", "options": "Circuit B"}, "properties": [{"id": "color", "value": fixed(ESTIMATE)}]}])))
def yoy(off=""):
    return f"sum by (name) (sum_over_time({W}[$__range:300s]{off}) * 300 / 3.6e6 * {TOP}) * $rate"
panels.append(panel("table", "This month vs the same days last year", 12, 27, 12, 8,
    [dict(expr=yoy(), format="table", instant=True),
     dict(expr=yoy(" offset 1y"), format="table", instant=True)],
    description="Cost so far this calendar month against the same span of days a year earlier. Blank = no history "
                "a year back (Office starts Oct 2025, Desktop Jan 2026, Internet and Den Dec 2025).",
    timeFrom="now/M", hideTimeOverride=True,
    transformations=[{"id": "merge", "options": {}},
        {"id": "organize", "options": {"excludeByName": {"Time": True},
                                       "renameByName": {"name": "Meter", "Value #A": "This month", "Value #B": "Last year"}}},
        {"id": "calculateField", "options": {"mode": "binary", "alias": "Change",
                                             "binary": {"left": "This month", "operator": "-", "right": "Last year"}}},
        {"id": "sortBy", "options": {"sort": [{"field": "This month", "desc": True}]}}],
    fieldConfig=dict(defaults=dict(unit="currencyUSD", decimals=2, custom=dict(align="auto")), overrides=[
        {"matcher": {"id": "byName", "options": "Change"}, "properties": [
            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
            {"id": "color", "value": {"mode": "thresholds"}},
            {"id": "thresholds", "value": thresholds((None, GOOD), (0.005, WARN_S))},
            {"id": "mappings", "value": [{"type": "special", "options": {"match": "nan", "result": {"text": "—", "color": "text"}}},
                                         {"type": "special", "options": {"match": "null", "result": {"text": "—", "color": "text"}}}]}]}])))

# ---- Energy ------------------------------------------------------------------------
panels.append(panel("timeseries", "Energy by meter", 0, 35, 12, 9,
    [dict(expr="sum_over_time(%s[$__interval:1m]) * 60 / 3.6e6" % src('{name="%s"}' % n), legendFormat=n)
     for n, _ in METERS],
    interval="1h", maxDataPoints=48,
    description="kWh per bar. Bars are 1 h on a day view and widen with longer ranges (12 h on 30 days).",
    options=dict(legend=dict(displayMode="list", placement="bottom"), tooltip=dict(mode="multi", sort="none")),
    fieldConfig=dict(defaults=dict(unit="kwatth", decimals=1, min=0,
        custom=dict(drawStyle="bars", fillOpacity=85, lineWidth=0, barAlignment=-1,
                    stacking=dict(mode="normal", group="A"), axisBorderShow=False)),
        overrides=color_overrides())))
def cost_panel(title, x, time_from, step_s, desc):
    return panel("bargauge", title, x, 35, 6, 9,
        [dict(expr=f"sort_desc(sum by (name) ({kwh(step_s)} * {TOP})) * $rate", legendFormat="{{name}}", instant=True)],
        description=desc, timeFrom=time_from, hideTimeOverride=True,
        options=dict(orientation="horizontal", displayMode="basic", showUnfilled=False, valueMode="text",
                     text=dict(valueSize=16, titleSize=13), namePlacement="top",
                     reduceOptions=dict(calcs=["lastNotNull"], fields="", values=False)),
        fieldConfig=dict(defaults=dict(unit="currencyUSD", decimals=2, min=0, color=fixed("#6da7ec")),
                         overrides=color_overrides()))
panels.append(cost_panel("Cost by meter · this month", 12, "now/M", 60, "Calendar month so far, billed meters."))
panels.append(cost_panel("Cost by meter · last 12 months", 18, "1y", 300,
    "Includes Home Assistant history; HA recorded nothing Apr 29 - Jun 24 2026, so this undercounts by about two months."))

# ---- Inside each meter -------------------------------------------------------------
AVG = f"avg_over_time({W}[$__range:1m])"
PARENTS = ["Office", "Desktop", "Server"]   # meters that have sub-meters, in palette order
REST = {"Office": "UPS", "Desktop": "Desktop", "Server": "UDM-SE"}   # what the unmetered remainder is
CHILD = {"Desk": "Laptop"}                                            # plug name -> what it powers
inside = []
for par in PARENTS:
    sub = f'on(device_id) group_left(parent) smartlife_device_topology{{level="sub",parent="{par}"}}'
    inside.append(dict(expr=f"sort_desc({AVG} * {sub})", legendFormat=par + " › {{name}}", instant=True))
    inside.append(dict(expr=f'sum({AVG} * on(device_id) group_left() smartlife_device_topology{{name="{par}"}})'
                            f" - sum({AVG} * {sub})", legendFormat=f"{par} › {REST[par]}", instant=True))
panels.append(panel("bargauge", "Inside each meter (average power, selected range)", 0, 44, 24, 12, inside,
    description="Sub-meters, and the unmetered remainder of each parent meter (parent minus its sub-meters). Already counted in the billed totals above.",
    options=dict(orientation="horizontal", displayMode="basic", showUnfilled=False, valueMode="text",
                 text=dict(valueSize=16, titleSize=13), namePlacement="top",
                 reduceOptions=dict(calcs=["lastNotNull"], fields="", values=False)),
    fieldConfig=dict(defaults=dict(unit="watt", decimals=0, min=0),
        overrides=[{"matcher": {"id": "byRegexp", "options": f"^{n} ›.*"},
                    "properties": [{"id": "color", "value": fixed(c)}]} for n, c in METERS]
                 + [{"matcher": {"id": "byName", "options": f"{par} › {REST[par]}"},
                     "properties": [{"id": "color", "value": fixed(ESTIMATE)}]} for par in PARENTS]
                 + [{"matcher": {"id": "byName", "options": f"{par} › {plug}"},
                     "properties": [{"id": "displayName", "value": f"{par} › {label}"}]}
                    for plug, label in CHILD.items() for par in PARENTS if TOPO.get(plug, {}).get("parent") == par])))

# ---- UPS (CyberPower RMCARD over SNMP, job cyberpower_ups) ------------------------------
def ups(expr):   # name the two UPSes after what they power
    return (f'label_replace(label_replace({expr}, "ups", "LLM Server UPS", "instance", "10.0.0.114"),'
            f' "ups", "Desktop UPS", "instance", "10.0.0.62")')
UPS_Y = 56
def ups_stat(title, x, expr, unit, desc, **fc):
    return panel("stat", title, x, UPS_Y, 6, 5, [dict(expr=ups(expr), instant=True, legendFormat="{{ups}}")],
                 description=desc,
                 options=dict(colorMode="background", graphMode="none", textMode="value_and_name", justifyMode="center",
                              orientation="vertical", text=dict(titleSize=12, valueSize=22),
                              reduceOptions=dict(calcs=["lastNotNull"], fields="", values=False)),
                 fieldConfig=dict(defaults=dict(unit=unit, **fc), overrides=[]))
panels.append(ups_stat("UPS status", 0, "upsBaseOutputStatus", "none",
    "Output status from the UPS network card. Anything but Online means the UPS is not passing mains through.",
    mappings=[{"type": "value", "options": {"2": {"text": "Online", "color": GOOD}, "3": {"text": "ON BATTERY", "color": CRIT_S},
               "4": {"text": "Boost", "color": WARN_S}, "10": {"text": "Buck", "color": WARN_S}, "11": {"text": "OVERLOAD", "color": CRIT_S},
               "6": {"text": "Off", "color": CRIT_S}}}], color={"mode": "thresholds"}, thresholds=thresholds((None, WARN_S))))
panels.append(ups_stat("UPS battery charge", 6, "upsAdvanceBatteryCapacity", "percent",
    "Battery charge. Below 100% after an outage while it recharges.", decimals=0, color={"mode": "thresholds"},
    thresholds=thresholds((None, CRIT_S), (50, WARN_S), (90, GOOD))))
panels.append(ups_stat("UPS runtime at current load", 12, "upsAdvanceBatteryRunTimeRemaining / 100", "s",
    "The UPS's own estimate of battery runtime at the present load.", decimals=0, color={"mode": "thresholds"},
    thresholds=thresholds((None, CRIT_S), (600, WARN_S), (1200, GOOD))))
panels.append(ups_stat("UPS battery health", 18, "upsAdvanceBatteryReplaceIndicator", "none",
    "The UPS's self-test verdict on the battery.",
    mappings=[{"type": "value", "options": {"1": {"text": "OK", "color": GOOD}, "2": {"text": "REPLACE", "color": CRIT_S}}}],
    color={"mode": "thresholds"}, thresholds=thresholds((None, WARN_S))))
panels.append(panel("timeseries", "UPS output power", 0, UPS_Y + 5, 12, 8,
    [dict(expr=ups("upsAdvanceOutputPower"), legendFormat="{{ups}}")],
    description="Watts leaving each UPS. LLM Server UPS sits on the Office plug; Desktop UPS on the Desktop plug.",
    fieldConfig=dict(defaults=dict(unit="watt", min=0, custom=dict(fillOpacity=10, lineWidth=2)), overrides=[
        {"matcher": {"id": "byName", "options": "LLM Server UPS"}, "properties": [{"id": "color", "value": fixed(dict(METERS)["Office"])}]},
        {"matcher": {"id": "byName", "options": "Desktop UPS"}, "properties": [{"id": "color", "value": fixed(dict(METERS)["Desktop"])}]}]),
    options=dict(legend=dict(displayMode="list", placement="bottom"), tooltip=dict(mode="multi"))))
panels.append(panel("timeseries", "UPS input voltage", 12, UPS_Y + 5, 12, 8,
    [dict(expr=ups("upsAdvanceInputLineVoltage / 10"), legendFormat="{{ups}}")],
    description="Mains voltage as each UPS sees it. The dashed lines are the 110 V / 127 V alert limits.",
    fieldConfig=dict(defaults=dict(unit="volt", decimals=1, custom=dict(lineWidth=2, thresholdsStyle=dict(mode="dashed")),
                                   thresholds=thresholds((None, "transparent"), (110, WARN), (127, CRIT))), overrides=[
        {"matcher": {"id": "byName", "options": "LLM Server UPS"}, "properties": [{"id": "color", "value": fixed(dict(METERS)["Office"])}]},
        {"matcher": {"id": "byName", "options": "Desktop UPS"}, "properties": [{"id": "color", "value": fixed(dict(METERS)["Desktop"])}]}]),
    options=dict(legend=dict(displayMode="list", placement="bottom"), tooltip=dict(mode="multi"))))

# ---- LLM server power cost ----------------------------------------------------------------------
LLM_Y = UPS_Y + 13
LLMW = f'sum({src(chr(123) + "name=" + chr(34) + "LLM Server" + chr(34) + chr(125))})'
GPUW = "sum(DCGM_FI_DEV_POWER_USAGE)"
LLM_KWH = f"(sum_over_time({LLMW}[$__range:1m]) * 60 / 3.6e6)"
TOK = lambda kinds: f'sum(increase(llamaswap_ledger_tokens_total{{kind=~"{kinds}"}}[$__range]))'   # all-time series: token-saver ledger + llama-swap store
def llm_stat(title, x, w, expr, unit, decimals, desc):
    return panel("stat", title, x, LLM_Y, w, 5, [dict(expr=expr, instant=True, legendFormat=title)], description=desc,
                 options=dict(colorMode="none", graphMode="none", textMode="value", justifyMode="center", text=dict(valueSize=28),
                              reduceOptions=dict(calcs=["lastNotNull"], fields="", values=False)),
                 fieldConfig=dict(defaults=dict(unit=unit, decimals=decimals, color=fixed("text")), overrides=[]))
LLM30 = f"avg_over_time({LLMW}[30d:5m])"   # same 30-day basis as the home projections at the top
LLM_PROJ = "LLM Server plug, last 30 days' average power x {h} h x rate. Inside Office, so already part of Office's cost."
panels.append(llm_stat("LLM cost / day", 0, 3, f"{LLM30} * 24 / 1000 * $rate", "currencyUSD", 2, LLM_PROJ.format(h=24)))
panels.append(llm_stat("LLM cost / month", 3, 3, f"{LLM30} * 730 / 1000 * $rate", "currencyUSD", 0, LLM_PROJ.format(h=730)))
panels.append(llm_stat("LLM cost / year", 6, 3, f"{LLM30} * 8760 / 1000 * $rate", "currencyUSD", 0, LLM_PROJ.format(h=8760)))
panels.append(llm_stat("LLM energy (range)", 9, 3, LLM_KWH, "kwatth", 1, "LLM Server plug energy over the selected range."))
panels.append(llm_stat("Tokens (range)", 12, 3, TOK("input|output"), "short", 1,
    "Prompt (uncached) + generated tokens served by llama-swap over the selected range (token-saver ledger + llama-swap store; hourly and partly estimated before Sep 25)."))
panels.append(llm_stat("$ / 1M tokens", 15, 3, f"{LLM_KWH} * $rate / (({TOK('input|output')} > 0) / 1e6)", "currencyUSD", 3,
    "LLM Server electricity over the range / (prompt + generated tokens) in millions. Idle power counts, so busy periods look cheaper."))
panels.append(llm_stat("$ / 1M generated", 18, 3, f"{LLM_KWH} * $rate / (({TOK('output')} > 0) / 1e6)", "currencyUSD", 2,
    "LLM Server electricity over the range / generated tokens in millions."))
panels.append(llm_stat("GPU share", 21, 3,
    f"avg_over_time({GPUW}[$__range:1m]) / avg_over_time({LLMW}[$__range:1m])", "percentunit", 0,
    "Both RTX 3090s (DCGM board power) as a share of the LLM Server plug. The rest is CPU, RAM, fans, PSU losses."))
panels.append(panel("timeseries", "LLM Server: wall power vs GPUs", 0, LLM_Y + 5, 24, 8,
    [dict(expr=LLMW, legendFormat="LLM Server plug"), dict(expr=GPUW, legendFormat="GPUs (2x 3090)"),
     dict(expr=f"{LLMW} - {GPUW}", legendFormat="Rest of system")],
    description="Plug power at the wall against GPU board power. The gap is the rest of the machine plus PSU losses.",
    fieldConfig=dict(defaults=dict(unit="watt", min=0, custom=dict(lineWidth=2, fillOpacity=0)), overrides=[
        {"matcher": {"id": "byName", "options": "LLM Server plug"}, "properties": [{"id": "color", "value": fixed(dict(METERS)["Office"])}]},
        {"matcher": {"id": "byName", "options": "GPUs (2x 3090)"}, "properties": [{"id": "color", "value": fixed(CIRCUIT)}]},
        {"matcher": {"id": "byName", "options": "Rest of system"}, "properties": [{"id": "color", "value": fixed(ESTIMATE)}]}]),
    options=dict(legend=dict(displayMode="list", placement="bottom"), tooltip=dict(mode="multi"))))
DETAILS_Y = LLM_Y + 13

# ---- Device table (collapsed) --------------------------------------------------------
tbl = panel("table", "All plugs", 0, DETAILS_Y + 1, 24, 10, [
    dict(expr=f"{W} * on(device_id) group_left(parent, level, circuit) smartlife_device_topology", format="table", instant=True),
    dict(expr="smartlife_voltage_volts", format="table", instant=True),
    dict(expr="smartlife_current_amps", format="table", instant=True),
    dict(expr="smartlife_device_online", format="table", instant=True)],
    transformations=[{"id": "merge", "options": {}},
        {"id": "organize", "options": {
            "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True, "device_id": True},
            "indexByName": {"name": 0, "level": 1, "parent": 2, "circuit": 3, "Value #A": 4, "Value #B": 5, "Value #C": 6, "Value #D": 7},
            "renameByName": {"name": "Plug", "level": "Level", "parent": "Inside", "circuit": "Circuit",
                             "Value #A": "Watts", "Value #B": "Volts", "Value #C": "Amps", "Value #D": "Online"}}},
        {"id": "sortBy", "options": {"sort": [{"field": "Watts", "desc": True}]}}],
    fieldConfig=dict(defaults=dict(custom=dict(align="auto")), overrides=[
        {"matcher": {"id": "byName", "options": "Watts"}, "properties": [{"id": "unit", "value": "watt"}, {"id": "decimals", "value": 1}]},
        {"matcher": {"id": "byName", "options": "Volts"}, "properties": [{"id": "unit", "value": "volt"}, {"id": "decimals", "value": 1}]},
        {"matcher": {"id": "byName", "options": "Amps"}, "properties": [{"id": "unit", "value": "amp"}, {"id": "decimals", "value": 2}]},
        {"matcher": {"id": "byName", "options": "Online"}, "properties": [{"id": "mappings", "value": [
            {"type": "value", "options": {"1": {"text": "yes"}, "0": {"text": "OFFLINE", "color": CRIT}}}]},
            {"id": "custom.cellOptions", "value": {"type": "color-text"}}]}]))
panels.append({"id": 99, "type": "row", "title": "Plug details", "collapsed": True,
               "gridPos": dict(x=0, y=DETAILS_Y, w=24, h=1), "panels": [tbl]})

dash = dict(uid="smartlife-power", title="Home power", tags=["smartlife", "power"], timezone="browser",
            refresh="1m", time={"from": "now-24h", "to": "now"}, schemaVersion=39, graphTooltip=1,
            panels=panels, templating=dict(list=[dict(type="textbox", name="rate", label="Rate ($/kWh)",
                                                      query="0.15", current=dict(text="0.15", value="0.15"))]))
json.dump(dash, open("grafana-dashboard.json", "w"), indent=1)
print(len(panels), "panels")
