"""Localhost TCP services exposed by the tuner.

  127.0.0.1:7376  ASCII G RDS stream  (primary)
  127.0.0.1:23    ASCII G RDS stream  (SDR# RDSOutput-compatible port)
  127.0.0.1:7373  JSON-lines telemetry, one status object per line
  127.0.0.1:7374  line-based command channel (see COMMAND_HELP)

All servers accept multiple simultaneous clients. Each client gets its own
sender thread and bounded queue, so a stalled reader is disconnected instead
of blocking the receiver.
"""

from __future__ import annotations

import json
import queue
import socket
import threading

RDS_PORT_PRIMARY = 7376
RDS_PORT_LEGACY = 23
TELEMETRY_PORT = 7373
COMMAND_PORT = 7374

COMMAND_HELP = """Commands (one per line, case-insensitive). Either plain
text or a JSON object such as {"cmd": "tune", "frequency": 101.1}.
Every command gets one JSON reply line.

  TUNE <mhz>          Tune to a channel in the chosen region's band
  UP | NEXT           Step one channel up (wraps at the band edge)
  DOWN | PREV         Step one channel down (wraps at the band edge)
  SEEK UP|DOWN        Start seeking for the next station
  SEEK HD UP|DOWN     Seek for the next station with an HD lock
  STOP                Stop seeking (receiver keeps playing)
  START               Start the receiver
  SHUTDOWN            Stop the receiver and release the dongle
  GAIN <db>           Set manual gain in dB (e.g. GAIN 0.0)
  GAINMODE MANUAL|AUTO
  BIAST ON|OFF        Switch the 4.5 V antenna-port bias-tee
  RDS2 ON|OFF         Decode the RDS2 streams (66.5/71.25/76 kHz)
  DX BANDWIDTH NORMAL|NARROW|DX   Channel filter width (120/90/70 kHz)
  DX SIDE OFF|AUTO|LOW|HIGH       Cut the passband short on the side of a
                                  strong neighbour (AUTO picks it from RF)
  DX CANCELLER ON|OFF             Adjacent-channel canceller
  AUDIO ON|OFF
  STEREO ON|OFF
  SOURCE MAIN|SCA67|SCA92         Listen to the main programme or an
                                  analogue SCA subcarrier
  VOLUME <0.0-5.0>
  LOCAL ON|OFF        Play on this PC's speakers (remote listeners
                      keep their audio either way)
  PRESET LIST         List the stored presets in order
  PRESET SAVE [mhz] [name]
                      Store the current (or given) channel; the name
                      defaults to the station's callsign/PS
  PRESET RECALL <n|name|mhz>
                      Tune to a stored preset
  PRESET RENAME <n|name> <new name>
  PRESET DELETE <n|name|mhz>
  PRESET NEXT|PREV     Tune the next/previous preset (wraps at the ends)
  PRESET UP|DOWN <n|name>
                      Move a preset within the list
  STATUS              Return the current telemetry object
  HELP                Return this text
"""


def format_ascii_g(group, stream: int = 0) -> bytes:
    """Encode a (b1, b2, b3, b4) group as an SDR#-compatible ASCII G record.

    RDS2 streams 1-3 use the same layout with a stream-numbered tag
    (`G1:` .. `G3:`); classic RDS keeps the plain `G:` tag.
    """
    blocks = []
    for block in group:
        text = format(block, "04X") if isinstance(block, int) \
            else str(block).upper()
        blocks.append("0000" if text == "----" else text.rjust(4, "0")[:4])
    tag = "G:" if not stream else f"G{int(stream)}:"
    return (tag + "\r\n" + "".join(blocks) + "\r\n\r\n").encode("ascii")


class _Client:
    def __init__(self, conn: socket.socket, address, maxsize: int = 500):
        self.conn = conn
        self.address = address
        self.queue: queue.Queue[bytes | None] = queue.Queue(maxsize=maxsize)
        self.thread: threading.Thread | None = None


class BroadcastServer:
    """TCP server that fans one byte stream out to every connected client."""

    def __init__(self, port: int, host: str = "127.0.0.1", name: str = "",
                 on_connect=None, on_event=None, queue_size: int = 500):
        self.port = port
        self.host = host
        self.name = name or f"port {port}"
        self._on_connect = on_connect      # -> bytes to send to a new client
        self._on_event = on_event
        self._queue_size = queue_size

        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._clients: list[_Client] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.total_sent = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Bind and start accepting. Raises OSError with a clear message."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            raise OSError(_bind_message(self.host, self.port, exc)) from exc
        sock.listen(8)
        sock.settimeout(0.5)
        self._sock = sock
        self._stop.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name=f"srv-{self.port}", daemon=True)
        self._accept_thread.start()
        self._emit("listening", f"{self.name} listening on "
                                f"{self.host}:{self.port}")

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._accept_thread:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None
        with self._lock:
            clients, self._clients = self._clients, []
        for client in clients:
            self._drop(client)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def client_addresses(self) -> list[str]:
        with self._lock:
            return [f"{c.address[0]}:{c.address[1]}" for c in self._clients]

    # -- sending -----------------------------------------------------------

    def broadcast(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.queue.put_nowait(data)
            except queue.Full:
                self._emit("client_slow",
                           f"{self.name}: dropping slow client "
                           f"{client.address[0]}:{client.address[1]}")
                self._remove(client)
        self.total_sent += len(data)

    # -- internals ---------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set() and self._sock is not None:
            try:
                conn, address = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client = _Client(conn, address, self._queue_size)
            client.thread = threading.Thread(
                target=self._send_loop, args=(client,),
                name=f"srv-{self.port}-client", daemon=True)
            with self._lock:
                self._clients.append(client)
            client.thread.start()
            self._emit("client_connected",
                       f"{self.name}: client {address[0]}:{address[1]} "
                       f"connected")
            if self._on_connect:
                try:
                    greeting = self._on_connect()
                except Exception:
                    greeting = None
                if greeting:
                    try:
                        client.queue.put_nowait(greeting)
                    except queue.Full:
                        pass

    def _send_loop(self, client: _Client) -> None:
        try:
            while not self._stop.is_set():
                try:
                    data = client.queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if data is None:
                    break
                client.conn.sendall(data)
        except OSError:
            pass
        finally:
            self._remove(client)

    def _remove(self, client: _Client) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
            else:
                return
        self._drop(client)
        self._emit("client_disconnected",
                   f"{self.name}: client {client.address[0]}:"
                   f"{client.address[1]} disconnected")

    @staticmethod
    def _drop(client: _Client) -> None:
        try:
            client.queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            client.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.conn.close()
        except OSError:
            pass

    def _emit(self, kind: str, message: str) -> None:
        if self._on_event:
            try:
                self._on_event(kind, message)
            except Exception:
                pass


class CommandServer:
    """Line-based command channel. `handler(text) -> dict` per line."""

    def __init__(self, port: int = COMMAND_PORT, host: str = "127.0.0.1",
                 handler=None, on_event=None):
        self.port = port
        self.host = host
        self.handler = handler
        self._on_event = on_event
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._conns: list[socket.socket] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            raise OSError(_bind_message(self.host, self.port, exc)) from exc
        sock.listen(8)
        sock.settimeout(0.5)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop,
                                        name=f"cmd-{self.port}", daemon=True)
        self._thread.start()
        self._emit("listening",
                   f"Command channel listening on {self.host}:{self.port}")

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        with self._lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _accept_loop(self) -> None:
        while not self._stop.is_set() and self._sock is not None:
            try:
                conn, address = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._conns.append(conn)
            threading.Thread(target=self._client_loop, args=(conn, address),
                             name="cmd-client", daemon=True).start()

    def _client_loop(self, conn: socket.socket, address) -> None:
        conn.settimeout(0.5)
        buffer = b""
        self._emit("client_connected",
                   f"Command client {address[0]}:{address[1]} connected")
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    reply = self._dispatch(text)
                    conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            with self._lock:
                if conn in self._conns:
                    self._conns.remove(conn)
            try:
                conn.close()
            except OSError:
                pass
            self._emit("client_disconnected",
                       f"Command client {address[0]}:{address[1]} disconnected")

    def _dispatch(self, text: str) -> dict:
        if self.handler is None:
            return {"ok": False, "error": "no command handler"}
        try:
            return self.handler(text)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _emit(self, kind: str, message: str) -> None:
        if self._on_event:
            try:
                self._on_event(kind, message)
            except Exception:
                pass


def _bind_message(host: str, port: int, exc: OSError) -> str:
    hint = ""
    if port == RDS_PORT_LEGACY:
        hint = (" Port 23 is the telnet port: it may be taken by the Windows "
                "Telnet Server or by SDR#'s RDSOutput plugin, and binding it "
                "can require running the tuner as Administrator. The tuner "
                "keeps running without it — port 7376 still serves RDS.")
    elif port == TELEMETRY_PORT:
        hint = " Another copy of the tuner may already be running."
    return (f"Could not bind {host}:{port} ({exc}).{hint}")
