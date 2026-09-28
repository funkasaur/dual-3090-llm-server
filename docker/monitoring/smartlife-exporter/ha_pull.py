import json, websocket, time
c = json.load(open('/home/aiuser/monitoring/smartlife-exporter/data/homeassistant.json'))
url = c['url'].rstrip('/').replace('http', 'ws', 1) + '/api/websocket'
ws = websocket.create_connection(url, timeout=120)
ws.recv(); ws.send(json.dumps({'type': 'auth', 'access_token': c['token']})); assert json.loads(ws.recv())['type'] == 'auth_ok'
mid = 0
def call(**msg):
    global mid; mid += 1; msg['id'] = mid; ws.send(json.dumps(msg))
    while True:
        r = json.loads(ws.recv())
        if r.get('id') == mid:
            if not r.get('success'): raise RuntimeError(r)
            return r['result']
IDS = ['sensor.server_power', 'sensor.smart_15em_plug_in_4_power', 'sensor.smart_15em_plug_in_5_power', 'sensor.tv_and_audio_power',
       'sensor.docking_station_power', 'sensor.laptop_power', 'sensor.new_plugin_2_power',
       'sensor.server_voltage', 'sensor.smart_15em_plug_in_4_voltage', 'sensor.smart_15em_plug_in_5_voltage', 'sensor.tv_and_audio_voltage',
       'sensor.docking_station_voltage', 'sensor.laptop_voltage', 'sensor.new_plugin_2_voltage']
out = {}
for period in ('hour', '5minute'):
    out[period] = call(type='recorder/statistics_during_period', start_time='2025-01-01T00:00:00Z', end_time='2026-09-27T01:07:00Z',
                       statistic_ids=IDS, period=period, types=['mean', 'min', 'max'])
    out[period + '_energy'] = call(type='recorder/statistics_during_period', start_time='2025-01-01T00:00:00Z', end_time='2026-09-27T01:07:00Z',
                       statistic_ids=['sensor.internet_and_den_total_energy'], period=period, types=['change', 'sum'])
json.dump(out, open('ha_stats.json', 'w'))
for period in ('hour', '5minute', 'hour_energy'):
    for k, v in out[period].items():
        print(period, f'{k:40}', len(v), time.strftime('%Y-%m-%d %H:%M', time.localtime(v[0]['start'] / 1000)), '..', time.strftime('%Y-%m-%d %H:%M', time.localtime(v[-1]['start'] / 1000)), 'first mean:', v[0].get('mean', v[0].get('change')))
