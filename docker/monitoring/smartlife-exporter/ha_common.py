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
RAW = {'server': 'server', 'smart_15em_plug_in_4': 'office', 'smart_15em_plug_in_5': 'desktop',
       'smart_15em_plug_in_6': 'nerdqaxe', 'smart_15em_plug_in_7': 'llm_server', 'tv_and_audio': 'tv_and_audio'}
REPL = {f'sensor.{r}_{k}': f'sensor.{s}_{k}_corrected' for r, s in RAW.items() for k in ('power', 'voltage')}
PAT = re.compile(r'(?<![\w.])(' + '|'.join(re.escape(k) for k in REPL) + r')(?![\w])')
def fix(text): return PAT.sub(lambda m: REPL[m.group(1)], text)
