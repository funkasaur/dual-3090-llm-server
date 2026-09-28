"""One-time Smart Life login. Writes data/auth.json for exporter.py.

Get the user code from the Smart Life app: Me -> gear (Settings) ->
Account and Security -> User Code. Then:

    docker compose run --rm smartlife-exporter python login.py <USER_CODE>

and scan the QR it prints with the Smart Life app (Me -> scan icon, top right).
"""
import json
import os
import sys
import time

import qrcode
from tuya_sharing import LoginControl

# Same public client the Home Assistant Smart Life integration uses; the
# sharing API only issues tokens to registered clients.
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
SCHEMA = "haauthorize"
AUTH_PATH = os.environ.get("AUTH_PATH", "/data/auth.json")


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    user_code = sys.argv[1].strip()
    login = LoginControl()

    resp = login.qr_code(CLIENT_ID, SCHEMA, user_code)
    if not resp.get("success"):
        sys.exit(f"QR request failed: {resp}")
    token = resp["result"]["qrcode"]
    payload = f"tuyaSmart--qrLogin?token={token}"

    qr = qrcode.QRCode(border=2)
    qr.add_data(payload)
    if sys.stdout.isatty():
        qr.print_tty()   # ANSI background blocks: renders in any terminal charset
    else:
        qr.print_ascii(invert=True)
    png = os.path.join(os.path.dirname(AUTH_PATH), "login-qr.png")
    qrcode.make(payload).save(png)
    print(f"\nScan with the Smart Life app within 5 minutes (also saved to {png}).")

    print("Waiting for the scan; in the app, tap Confirm after scanning. Leave this running.", flush=True)
    deadline = time.time() + 300
    last_state = None
    while time.time() < deadline:
        time.sleep(3)
        try:
            ok, info = login.login_result(token, CLIENT_ID, user_code)
        except Exception as e:
            print(f"  poll error (retrying): {e!r}", flush=True)
            continue
        state = "ok" if ok else f"{info.get('code')} {info.get('msg')}"
        if state != last_state:
            print(f"  {time.strftime('%H:%M:%S')} status: {state}", flush=True)
            last_state = state
        if ok:
            info["user_code"] = user_code
            fd = os.open(AUTH_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(info, f, indent=1)
            os.remove(png)
            print(f"Logged in as {info.get('username', info.get('uid'))}; saved {AUTH_PATH}")
            return
    sys.exit("Timed out waiting for the scan; run login.py again.")


if __name__ == "__main__":
    main()
