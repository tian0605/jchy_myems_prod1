"""Local TLS provider fixture: synthetic 70-meter archives and 15-minute registers."""

import hmac
import json
import os
import ssl
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


STATE = Path("/acceptance/state")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format_, *args):
        # Never log request headers or bodies.
        pass

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"synthetic provider ready")

    def do_POST(self):
        if not hmac.compare_digest(self.headers.get("API_KEY", ""), os.environ["LOCAL_MOCK_KEY"]):
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 4096:
            self.send_error(400)
            return
        payload = json.loads(self.rfile.read(length))
        if self.path == "/WebApi/NH2/AmmeterBasicInfo":
            data = [
                {"ID": index, "Address": f"{index:04}", "AmmeterName": f"Synthetic meter {index:02}",
                 "AreaName": "LOCAL ONLY", "TimesV": 1, "TimesA": 1, "SingleRate": False,
                 "OnlineState": True, "AddDate": "2026-10-01 00:00:00"}
                for index in range(1, 71)
            ]
        elif self.path == "/WebApi/NH2/ElectricQuantity":
            if payload.get("valueType") != "SJZ" or payload.get("dateType") != "mi15":
                self.send_error(400)
                return
            start = datetime.fromisoformat(payload["startTime"])
            end = datetime.fromisoformat(payload["endTime"])
            if not timedelta(0) < end - start <= timedelta(days=1):
                self.send_error(400)
                return
            missing = json.loads((STATE / "scenario.json").read_text())["missing"]
            data = []
            timestamp = start
            while timestamp < end:
                for index in range(1, 71):
                    if missing and index > 66:
                        continue
                    data.append({
                        "AmmeterID": index, "Address": f"{index:04}",
                        "ReadingDate": timestamp.strftime("%Y-%m-%d %H:%M"),
                        "ZYGDNSZ": str(index * 1000 + (timestamp - datetime(2026, 1, 1)).total_seconds() / 900),
                    })
                timestamp += timedelta(minutes=15)
        else:
            self.send_error(404)
            return
        body = json.dumps({"IsSuccess": True, "Code": "0000", "Msg": "synthetic", "Count": len(data), "Data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(STATE / "mock.crt", STATE / "mock.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print("Synthetic HTTPS provider ready", flush=True)
    server.serve_forever()
