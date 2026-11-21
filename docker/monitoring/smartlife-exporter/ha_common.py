# needs websocket-client + requests (pip install websocket-client requests). Uses data/homeassistant.json.
import json, re, requests, websocket
c = json.load(open('/home/aiuser/monitoring/smartlife-exporter/data/homeassistant.json'))
H = {'Authorization': 'Bearer ' + c['token']}; B = c['url'].rstrip('/')
ws = websocket.create_connection(B.replace('http', 'ws', 1) + '/api/websocket', timeout=120)
ws.recv(); ws.send(json.dumps({'type': 'auth', 'access_token': c['token']})); ws.recv()
_mid = 0
def call(**m):
    global _mid; _mid += 1; m['id'] = _mid; ws.send(json.dumps(m))
    while True:
        r = json.loads(ws.recv())
        if r.get('id') == _mid: return r.get('result') if r.get('success') else {'_error': r.get('error')}
# Example: raw HA plug entity id -> name of its corrected sensor (sensor.<name>_power_corrected). Replace with yours.
RAW = {'plug_1_raw': 'plug_1', 'plug_4_raw': 'plug_4', 'plug_5_raw': 'plug_5',
       'plug_6_raw': 'plug_6', 'plug_7_raw': 'plug_7', 'room_b_raw': 'room_b'}
REPL = {f'sensor.{r}_{k}': f'sensor.{s}_{k}_corrected' for r, s in RAW.items() for k in ('power', 'voltage')}
PAT = re.compile(r'(?<![\w.])(' + '|'.join(re.escape(k) for k in REPL) + r')(?![\w])')
def fix(text): return PAT.sub(lambda m: REPL[m.group(1)], text)
