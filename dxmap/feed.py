"""Port 7375: the RDS Monitor pushes stations here, one JSON object per line.

  {"type": "log", "pi": "1234", "callsign": "WXYZ", "freq_mhz": 92.7, ...}
      -> {"ok": true, "station_id": 12, "new": false}

"type": "log" is the monitor's Log button; "auto" is automatic logging
(merged into the previous log of that station within 10 minutes).
"now" is the station the monitor is decoding right now: shown on the map
as "Now hearing", never logged ({"type": "now", "clear": true} removes it).
"ping" answers {"ok": true}.
"""

import json
import socketserver
import threading
import time

FEED_HOST = "127.0.0.1"
FEED_PORT = 7375


class FeedServer:
    def __init__(self, on_record, host=FEED_HOST, port=FEED_PORT, on_now=None):
        self.on_record = on_record
        self.on_now = on_now
        self.host, self.port = host, port
        self.last_message = None
        self.last_station = None
        self.clients = 0
        self.error = None
        self._server = None
        self._lock = threading.Lock()

    def handle_line(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            return {"ok": False, "error": "not JSON"}
        if not isinstance(msg, dict):
            return {"ok": False, "error": "expected an object"}
        kind = (msg.get("type") or "log").lower()
        if kind == "ping":
            return {"ok": True}
        if kind in ("now", "tuned"):  # tuned: the RDS Monitor retuned
            if self.on_now:
                self.on_now(msg)
            return {"ok": True}
        if kind not in ("log", "auto"):
            return {"ok": False, "error": f"unknown type {kind!r}"}
        try:
            sid, created = self.on_record(msg, auto=(kind == "auto"))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        self.last_message = time.time()
        self.last_station = msg.get("callsign") or msg.get("pi")
        return {"ok": True, "station_id": sid, "new": created}

    def start(self):
        feed = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                with feed._lock:
                    feed.clients += 1
                try:
                    for raw in self.rfile:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        reply = feed.handle_line(line)
                        self.wfile.write((json.dumps(reply) + "\n").encode())
                except (ConnectionError, OSError):
                    pass
                finally:
                    with feed._lock:
                        feed.clients -= 1

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        try:
            self._server = Server((self.host, self.port), Handler)
        except OSError as e:
            self.error = f"port {self.port} unavailable: {e}"
            return False
        threading.Thread(target=self._server.serve_forever,
                         daemon=True).start()
        return True

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
