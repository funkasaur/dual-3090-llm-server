# Example power backfill

The scripts use the Room A and Plug 1 labels in `topology.json`. Set the dates,
device names, example UPS endpoints, and estimates for your own data before running them.
Copy the same device names into the dashboard builder and any alert examples you use.

## UPS-based estimates

`backfill_estimate.py` writes minute samples as `smartlife_power_watts_estimate`.
In the example, Plug 5 uses 0.991 times the output of the UPS at 192.0.2.114.
Room A uses that value plus a 73.2 W allowance for its other loads. Room B uses
1.031 times the output of the UPS at 192.0.2.62. The other plugs use fixed
estimates from the script's `FLAT` table. Fit those coefficients and allowances
to a period with both UPS and plug measurements.

## Home Assistant history

`ha_pull.py` reads long-term statistics over Home Assistant's websocket API using
credentials in `data/homeassistant.json`. It writes `ha_stats.json`. Use matching
sensor IDs in `ha_pull.py` and `ha_backfill.py`. For example, `sensor.room_a_power`
and `sensor.room_a_voltage` map to Room A, and `sensor.plug_4_power` and
`sensor.plug_4_voltage` map to Plug 4. The example estimates Plug 2 using the
hourly change from `sensor.plug_2_total_energy`.

`ha_backfill.py` writes `smartlife_power_watts_history` using five-minute means
when available and hourly means for earlier history. For 15EM plugs it divides
values by 10 when the mean voltage indicates that Home Assistant recorded
tenths, and drops hours that straddle a change in scaling.

## Import and removal

Load the OpenMetrics files with `promtool tsdb create-blocks-from openmetrics`.
The dashboard prefers live readings, then Home Assistant history, then UPS or
fixed estimates. Prometheus may merge the imported blocks with other data.
To remove only the estimates, use the Prometheus admin API
(`--web.enable-admin-api`) and these commands.

```sh
curl -X POST -g 'http://localhost:2090/api/v1/admin/tsdb/delete_series?match[]=smartlife_power_watts_estimate'
curl -X POST http://localhost:2090/api/v1/admin/tsdb/clean_tombstones
```

To remove the Home Assistant backfill, use `smartlife_power_watts_history` as the
selector instead.

## Optional inference history

The Hermes Agent token-saver plugin can supply older inference totals. To use
its SQLite ledger, enable the example mount in the monitoring Compose file.
For a Prometheus backfill, see the usage in `ledger_backfill.py`. Set its instance
label to the same target as your live llama-swap activity scrape.
