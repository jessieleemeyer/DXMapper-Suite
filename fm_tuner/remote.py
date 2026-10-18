"""Remote web page: control and listen to the tuner from a phone or another
PC on the home network (whole-home audio from one dongle).

  http://<this PC>:7377/          the remote page (logo, station, controls)
  /api/status                     compact JSON for the page
  /api/cmd   (POST {"cmd": ...})  a whitelisted subset of the command port
  /api/logo                       the RDS Monitor's logo for the station on air
  /audio.wav                      the tuner's audio as an endless 16-bit WAV

Only loopback and private (home network) addresses are served. Station
details (call, slogan, city, range) come from the DX Mapper's "now hearing"
when it is running next to the tuner; otherwise the page shows the tuner's
own RDS (PS / RadioText).
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import queue
import re
import socket
import struct
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import band, hd_radio
from .settings_dir import APP_ROOT
from .signal_units import format_signal

REMOTE_PORT = 7377
MAPPER_URL = "http://127.0.0.1:7380"
PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "remote.html")
LOGO_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico")
CLIENT_QUEUE = 40          # ~2.7 s of audio blocks per listener

ALLOWED_COMMANDS = re.compile(
    r"^(TUNE \d{2,3}(\.\d)?|UP|DOWN|SEEK( HD)? (UP|DOWN)|STOP|"
    r"PRESET (NEXT|PREV|RECALL \d{1,3})|HD PROGRAM ([1-8]|ANALOG)|LOG)$",
    re.IGNORECASE)


class AudioHub:
    """Fans the tuner's output PCM out to every remote listener."""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self.rate: int | None = None
        self.channels: int | None = None

    @property
    def listeners(self) -> int:
        with self._lock:
            return len(self._clients)

    def publish(self, pcm, rate: int, channels: int) -> None:
        """Called by the audio thread with int16 interleaved samples."""
        with self._lock:
            if (rate, channels) != (self.rate, self.channels):
                self.rate, self.channels = int(rate), int(channels)
                for q in self._clients:      # format changed: reconnect
                    _put(q, None)
                self._clients = []
            clients = list(self._clients)
        if not clients:
            return
        data = pcm.tobytes()
        for q in clients:
            _put(q, data)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=CLIENT_QUEUE)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def close_all(self) -> None:
        with self._lock:
            for q in self._clients:
                _put(q, None)
            self._clients = []


def _put(q: queue.Queue, item) -> None:
    """Never block the audio thread: a slow listener loses its oldest block."""
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
            q.put_nowait(item)
        except (queue.Empty, queue.Full):
            pass


def wav_header(rate: int, channels: int) -> bytes:
    """WAV header with 'unknown' (maximum) lengths for a live stream."""
    block = channels * 2
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block,
                        block, 16) +
            b"data" + struct.pack("<I", 0xFFFFFFFF))


def sibling_dir(*names: str) -> str | None:
    if os.path.isfile(os.path.join(APP_ROOT, "RDS Monitor.py")):
        return APP_ROOT  # one-folder suite: all three programs together
    parent = os.path.dirname(APP_ROOT)
    for name in names:
        path = os.path.join(parent, name)
        if os.path.isdir(path):
            return path
    return None


def logo_file(callsign: str | None) -> str | None:
    """The RDS Monitor's logo for a call (RDS-Monitor/settings/logos)."""
    call = (callsign or "").strip().upper()
    folder = sibling_dir("RDS-Monitor", "RDS Monitor", "RDS_Monitor")
    if not folder or not re.fullmatch(r"[A-Z0-9]{3,8}", call):
        return None
    folder = os.path.join(folder, "settings", "logos")
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    found = [n for n in names if os.path.splitext(n)[0].upper() == call
             and os.path.splitext(n)[1].lower() in LOGO_EXTENSIONS]
    found.sort(key=lambda n: LOGO_EXTENSIONS.index(os.path.splitext(n)[1].lower()))
    for name in found:
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def range_tier(miles, local_mi=40.0, ext_mi=80.0) -> str:
    if miles is None:
        return ""
    return ("Local" if miles <= local_mi else
            "Extended local" if miles <= ext_mi else "DX")


def lan_addresses() -> list[str]:
    """This PC's home-network IPv4 address(es), best guess first."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.255.255", 1))   # no packet is sent
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in found:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_private and not addr.is_loopback and ip not in out:
            out.append(ip)
    return out


def _clean(text) -> str:
    return " ".join(str(text or "").split())


class MapperLink:
    """Cached 'now hearing' from the DX Mapper (call, city, miles...)."""

    TTL = 1.5

    def __init__(self, url: str = MAPPER_URL):
        self.url = url
        self._lock = threading.Lock()
        self._at = 0.0
        self._state: dict | None = None

    def state(self) -> dict | None:
        with self._lock:
            if time.time() - self._at < self.TTL:
                return self._state
            self._at = time.time()
        try:
            with urllib.request.urlopen(self.url + "/api/state",
                                        timeout=0.8) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            data = None
        with self._lock:
            self._state = data
        return data

    def log_now(self) -> dict:
        """Log the station the RDS Monitor is hearing, like Log to Map."""
        req = urllib.request.Request(self.url + "/api/now/log", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": "DX Mapper is not running - start "
                                          "the RDS Monitor and DX Mapper to log"}


def build_status(status: dict, mapper_state: dict | None) -> dict:
    """What the remote page shows, from tuner telemetry + DX Mapper."""
    freq = round(float(status.get("frequency_mhz") or 0), 3)
    hd = status.get("hd") or {}
    out = {
        "frequency_mhz": freq,
        "decimals": (status.get("band") or {}).get("decimals", 1),
        "step_mhz": (status.get("band") or {}).get("step_mhz", 0.2),
        "state": status.get("state"),
        "seeking": bool(status.get("seeking")),
        "signal_dbfs": status.get("signal_dbfs"),
        "signal_text": format_signal(status.get("signal_dbfs"),
                                     status.get("signal_units")),
        "stereo": status.get("stereo"),
        "pi": status.get("pi") or None,
        "ps": _clean(status.get("ps")) or None,
        "rt": _clean(status.get("radiotext")) or None,
        "pty": status.get("pty_name") or None,
        "hd_available": (band.plan_key() == "americas"
                         and hd_radio.find_addon() is not None),
        "hd_locked": bool(hd.get("synced")),
        "hd_playing": hd.get("playing"),
        "hd_programs": [p.get("hd") or f"HD{(p.get('program') or 0) + 1}"
                        for p in hd.get("programs") or [] if isinstance(p, dict)],
        "presets": [{"name": p.get("name"), "frequency_mhz": p.get("frequency_mhz")}
                    for p in status.get("presets") or []],
        "audio": bool(status.get("audio_enabled")),
        "callsign": None, "slogan": None, "location": None,
        "now_playing": None, "range": None, "logo": False,
    }
    now = (mapper_state or {}).get("now")
    if now and now.get("freq_mhz") is not None \
            and abs(float(now["freq_mhz"]) - freq) < 0.005 \
            and (not out["pi"] or not now.get("pi")
                 or str(now["pi"]).upper() == str(out["pi"]).upper()):
        settings = (mapper_state or {}).get("settings") or {}
        local = float(settings.get("local_radius_mi") or 40)
        ext = max(local, float(settings.get("extended_radius_mi") or 80))
        out["callsign"] = now.get("callsign") or None
        out["slogan"] = _clean(now.get("slogan")) or None
        out["location"] = now.get("location") or None
        out["now_playing"] = _clean(now.get("now_playing")) or None
        miles = now.get("distance_mi")
        if miles is not None:
            out["range"] = (f"{range_tier(miles, local, ext)} \u00b7 "
                            f"{miles} mi {now.get('compass') or ''}").strip()
    out["logo"] = bool(logo_file(out["callsign"]))
    return out


class RemoteServer:
    """Threaded HTTP server for the remote page (off until switched on)."""

    def __init__(self, service, hub: AudioHub, port: int = REMOTE_PORT,
                 host: str = "0.0.0.0", mapper: MapperLink | None = None,
                 on_event=None):
        self.service = service
        self.hub = hub
        self.port = port
        self.host = host
        self.mapper = mapper or MapperLink()
        self._on_event = on_event
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def urls(self) -> list[str]:
        return [f"http://{ip}:{self.port}/" for ip in lan_addresses()]

    def start(self) -> None:
        if self._httpd:
            return
        try:
            httpd = ThreadingHTTPServer((self.host, self.port),
                                        _handler_class(self))
        except OSError as exc:
            self.error = f"Could not open port {self.port}: {exc}"
            raise OSError(self.error) from exc
        httpd.daemon_threads = True
        self.error = None
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="remote-http", daemon=True)
        self._thread.start()
        self._emit("listening", f"Remote page on port {self.port}: "
                                + (", ".join(self.urls()) or "no network"))

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        self.hub.close_all()
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def page_status(self) -> dict:
        return build_status(self.service.status(), self.mapper.state())

    def command(self, text: str) -> dict:
        text = " ".join(str(text or "").split())
        if not ALLOWED_COMMANDS.match(text):
            return {"ok": False, "error": f"not allowed from the remote: {text!r}"}
        if text.upper() == "LOG":
            return self.log_station()
        return self.service.handle_command(text)

    def log_station(self) -> dict:
        st = self.page_status()
        if not st.get("callsign") and not st.get("pi"):
            return {"ok": False, "error": "No station decoded yet"}
        if not self.mapper.state() or not (self.mapper.state() or {}).get("now"):
            return {"ok": False, "error": "Start the RDS Monitor and DX Mapper to log"}
        if st.get("callsign") is None:
            return {"ok": False, "error": "The RDS Monitor hasn't identified "
                                          "this station yet"}
        reply = self.mapper.log_now()
        if reply.get("ok"):
            reply["message"] = (f"Logged {st['callsign']} "
                                f"{band.fmt(st['frequency_mhz'])} to DX Mapper"
                                + (" (new station)" if reply.get("new") else ""))
            self._emit("remote", "Remote: " + reply["message"])
        return reply

    def _emit(self, kind: str, message: str) -> None:
        if self._on_event:
            try:
                self._on_event(kind, message)
            except Exception:
                pass


def _allowed(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return addr.is_loopback or addr.is_private or addr.is_link_local


def _handler_class(remote: RemoteServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "FMTunerRemote/1.0"

        def log_message(self, *_args):
            pass

        def _send(self, code: int, body, ctype="application/json",
                  headers=None):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _check(self) -> bool:
            if _allowed(self.client_address[0]):
                return True
            self._send(403, {"error": "home network only"})
            return False

        def do_GET(self):
            if not self._check():
                return
            path = self.path.split("?", 1)[0]
            try:
                if path in ("/", "/index.html"):
                    with open(PAGE_FILE, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if path == "/api/status":
                    return self._send(200, remote.page_status())
                if path == "/api/logo":
                    st = remote.page_status()
                    f = logo_file(st.get("callsign"))
                    if not f:
                        return self._send(404, {"error": "no logo"})
                    with open(f, "rb") as fh:
                        return self._send(200, fh.read(),
                                          mimetypes.guess_type(f)[0]
                                          or "application/octet-stream")
                if path == "/audio.wav":
                    return self._audio()
            except (BrokenPipeError, ConnectionResetError):
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._check():
                return
            if self.path.split("?", 1)[0] != "/api/cmd":
                return self._send(404, {"error": "not found"})
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 4096)
                data = json.loads(self.rfile.read(length) or b"{}")
                reply = remote.command(data.get("cmd"))
            except Exception as exc:
                reply = {"ok": False, "error": str(exc)}
            self._send(200, reply)

        def _audio(self):
            hub = remote.hub
            q = hub.subscribe()
            remote._emit("remote", f"Remote listener {self.client_address[0]} connected")
            try:
                deadline = time.time() + 5
                while hub.rate is None and time.time() < deadline:
                    time.sleep(0.1)
                if hub.rate is None:
                    return self._send(503, {"error": "tuner audio is not running"})
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(wav_header(hub.rate, hub.channels))
                while remote.running:
                    try:
                        data = q.get(timeout=5)
                    except queue.Empty:
                        break       # audio stopped: let the page reconnect
                    if data is None:
                        break
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)
                remote._emit("remote", f"Remote listener {self.client_address[0]} left")

    return Handler


def qr_matrix(text: str) -> list[list[bool]]:
    """QR code modules for `text` (vendored segno, pure Python)."""
    import sys
    vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    import segno
    code = segno.make(text, error="m", micro=False)
    return [[bool(v) for v in row] for row in code.matrix]
