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

# ---- Power by meter ------------------------------------------------------------
panels.append(panel("timeseries", "Power by meter (stacked = whole home)", 0, 5, 24, 10,
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
    return panel("timeseries", title, x, 15, 8, 8, [dict(expr=f"{real} or on() {hist}", legendFormat="load")],
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

# ---- Energy ------------------------------------------------------------------------
panels.append(panel("timeseries", "Energy by meter", 0, 23, 12, 9,
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
    return panel("bargauge", title, x, 23, 6, 9,
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
panels.append(panel("bargauge", "Inside each meter (average power, selected range)", 0, 32, 24, 12, inside,
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

# ---- Device table (collapsed) --------------------------------------------------------
tbl = panel("table", "All plugs", 0, 45, 24, 10, [
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
               "gridPos": dict(x=0, y=44, w=24, h=1), "panels": [tbl]})

dash = dict(uid="smartlife-power", title="Home power", tags=["smartlife", "power"], timezone="browser",
            refresh="1m", time={"from": "now-24h", "to": "now"}, schemaVersion=39, graphTooltip=1,
            panels=panels, templating=dict(list=[dict(type="textbox", name="rate", label="Rate ($/kWh)",
                                                      query="0.15", current=dict(text="0.15", value="0.15"))]))
json.dump(dash, open("grafana-dashboard.json", "w"), indent=1)
print(len(panels), "panels")
