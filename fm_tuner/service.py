"""Glue between the receiver and the localhost TCP services.

`TunerService` owns an `FMReceiver` plus the ASCII G, telemetry and command
servers, and implements the command grammar documented in
`fm_tuner.servers.COMMAND_HELP`.
"""

from __future__ import annotations

import json
import threading
import time

from . import band, remote, servers
from .presets import PresetError, PresetStore
from .receiver import FMReceiver, ReceiverState
from .rtlsdr_device import RtlSdrError

TELEMETRY_INTERVAL = 0.5


class TunerService:
    def __init__(self, device_index: int = 0,
                 rds_ports=(servers.RDS_PORT_PRIMARY, servers.RDS_PORT_LEGACY),
                 telemetry_port: int = servers.TELEMETRY_PORT,
                 command_port: int = servers.COMMAND_PORT,
                 ppm: int = 0,
                 on_event=None,
                 presets_path: str | None = None):
        self._on_event = on_event
        self.presets = PresetStore(presets_path, on_event=self._event)
        self.receiver = FMReceiver(device_index=device_index,
                                   on_groups=self._publish_groups,
                                   on_rds2_groups=self._publish_rds2_groups,
                                   on_event=self._event,
                                   ppm=ppm)
        self.rds_servers = [
            servers.BroadcastServer(port, name=f"RDS ASCII G {port}",
                                    on_event=self._event)
            for port in rds_ports
        ]
        self.telemetry_server = servers.BroadcastServer(
            telemetry_port, name=f"Telemetry {telemetry_port}",
            on_connect=self._telemetry_greeting, on_event=self._event,
            queue_size=50)
        self.command_server = servers.CommandServer(
            command_port, handler=self.handle_command, on_event=self._event)
        # Remote page for phones / other PCs: off until the GUI switches it on.
        self.audio_hub = remote.AudioHub()
        self.receiver.audio_hub = self.audio_hub
        self.remote = remote.RemoteServer(self, self.audio_hub,
                                          on_event=self._event)
        self._preset_index: int | None = None

        self.groups_sent = 0
        self.rds2_groups_sent = 0
        self.server_errors: list[str] = []
        # Screen rectangle of the tuner window, set by the GUI so the RDS
        # Monitor can dock itself next to it. None when running headless.
        self.window_rect: dict | None = None
        self._telemetry_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start_servers(self) -> list[str]:
        """Bind every server. Returns the list of non-fatal bind errors.

        A failure on the legacy port 23 is reported but does not stop the
        tuner; a failure on the primary RDS, telemetry or command port is
        raised, because the monitor cannot work without them.
        """
        self.server_errors = []
        for index, server in enumerate(self.rds_servers):
            try:
                server.start()
            except OSError as exc:
                if index == 0:
                    raise
                self.server_errors.append(str(exc))
                self._event("warning", str(exc))
        self.telemetry_server.start()
        self.command_server.start()

        self._stop.clear()
        self._telemetry_thread = threading.Thread(
            target=self._telemetry_loop, name="telemetry", daemon=True)
        self._telemetry_thread.start()
        return self.server_errors

    def shutdown(self) -> None:
        self._stop.set()
        if self._telemetry_thread:
            self._telemetry_thread.join(timeout=2.0)
            self._telemetry_thread = None
        self.receiver.stop()
        for server in self.rds_servers:
            server.stop()
        self.telemetry_server.stop()
        self.command_server.stop()
        self.remote.stop()

    # -- publishing --------------------------------------------------------

    def _publish_groups(self, groups) -> None:
        payload = b"".join(servers.format_ascii_g(group) for group in groups)
        if not payload:
            return
        self.groups_sent += len(groups)
        for server in self.rds_servers:
            server.broadcast(payload)

    def _publish_rds2_groups(self, groups) -> None:
        """RDS2 streams go only to the primary port: legacy clients on the
        SDR#-compatible port would choke on the `G1:`..`G3:` records."""
        payload = b"".join(servers.format_ascii_g(group[1:], stream=group[0])
                           for group in groups)
        if not payload or not self.rds_servers:
            return
        self.rds2_groups_sent += len(groups)
        self.rds_servers[0].broadcast(payload)

    def _telemetry_greeting(self) -> bytes:
        return self._telemetry_line()

    def _telemetry_line(self) -> bytes:
        status = self.status()
        return (json.dumps(status) + "\n").encode("utf-8")

    def _telemetry_loop(self) -> None:
        while not self._stop.wait(TELEMETRY_INTERVAL):
            if self.telemetry_server.client_count == 0:
                continue
            self.telemetry_server.broadcast(self._telemetry_line())

    def status(self) -> dict:
        status = self.receiver.telemetry()
        status["rds_clients"] = sum(s.client_count for s in self.rds_servers)
        status["telemetry_clients"] = self.telemetry_server.client_count
        status["groups_sent"] = self.groups_sent
        status["rds2_groups_sent"] = self.rds2_groups_sent
        status["ports"] = {
            "rds": [s.port for s in self.rds_servers],
            "telemetry": self.telemetry_server.port,
            "command": self.command_server.port,
        }
        status["signal_units"] = getattr(self, "signal_units", "s")
        status["server_errors"] = list(self.server_errors)
        status["presets"] = self.presets.list()
        status["window"] = self.window_rect
        status["remote"] = {"enabled": self.remote.running,
                            "port": self.remote.port,
                            "listeners": self.audio_hub.listeners,
                            "error": self.remote.error}
        return status

    # -- commands ----------------------------------------------------------

    def handle_command(self, text: str) -> dict:
        """Execute one command line and return a JSON-serialisable reply."""
        command, args = _parse(text)
        if command is None:
            return {"ok": False, "error": f"could not parse {text!r}"}

        try:
            return self._execute(command, args)
        except band.FrequencyError as exc:
            return {"ok": False, "error": str(exc), "command": command}
        except RtlSdrError as exc:
            return {"ok": False, "error": str(exc), "command": command}
        except PresetError as exc:
            return {"ok": False, "error": str(exc), "command": command}
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "command": command}

    def _execute(self, command: str, args: list[str]) -> dict:
        receiver = self.receiver

        if command in ("help", "?"):
            return {"ok": True, "command": "help", "help": servers.COMMAND_HELP}

        if command == "status":
            return {"ok": True, "command": "status", "status": self.status()}

        if command == "tune":
            if not args:
                raise ValueError("TUNE needs a frequency, e.g. TUNE 101.1")
            freq = receiver.tune(_number(args[0], "frequency"))
            return _ok("tune", frequency_mhz=freq)

        if command in ("up", "next"):
            return _ok("up", frequency_mhz=receiver.step(+1))

        if command in ("down", "prev", "previous"):
            return _ok("down", frequency_mhz=receiver.step(-1))

        if command == "seek":
            words = [a.lower() for a in args]
            hd = "hd" in words
            words = [w for w in words if w != "hd"]
            direction = words[0] if words else "up"
            if direction not in ("up", "down"):
                raise ValueError("SEEK needs UP or DOWN")
            receiver.seek(+1 if direction == "up" else -1, hd=hd)
            return _ok("seek", direction=direction, hd=hd)

        if command == "stop":
            receiver.stop_seek()
            return _ok("stop", frequency_mhz=receiver.frequency)

        if command == "start":
            receiver.start()
            return _ok("start", frequency_mhz=receiver.frequency)

        if command == "shutdown":
            receiver.stop()
            return _ok("shutdown")

        if command == "gain":
            if not args:
                raise ValueError("GAIN needs a value in dB, e.g. GAIN 0.0")
            receiver.set_gain_db(_number(args[0], "gain"))
            return _ok("gain", gain_db=receiver.gain_db,
                       gain_mode="manual")

        if command in ("gainmode", "gain_mode"):
            mode = args[0].lower() if args else ""
            if mode not in ("manual", "auto"):
                raise ValueError("GAINMODE needs MANUAL or AUTO")
            receiver.set_gain_mode(mode == "manual")
            return _ok("gainmode", gain_mode=mode)

        if command in ("biast", "bias_t", "biastee", "bias_tee"):
            receiver.set_bias_tee(_flag(args, "BIAST"))
            return _ok("biast", bias_tee=receiver.bias_tee)

        if command == "rds2":
            receiver.set_rds2(_flag(args, "RDS2"))
            return _ok("rds2", rds2_enabled=receiver.rds2_enabled)

        if command == "dx":
            self._dx_command(args)
            return _ok("dx", dx=receiver.dx_settings)

        if command == "audio":
            receiver.set_audio_enabled(_flag(args, "AUDIO"))
            return _ok("audio", audio_enabled=receiver.audio_enabled)

        if command == "stereo":
            receiver.set_stereo(_flag(args, "STEREO"))
            return _ok("stereo", stereo_mode="stereo" if receiver.stereo_mode
                                             else "mono")

        if command == "source":
            if not args:
                raise ValueError("SOURCE MAIN|SCA67|SCA92")
            receiver.set_audio_source(args[0].lower())
            return _ok("source", audio_source=receiver.audio_source)

        if command == "hd":
            self._hd_command(args)
            return _ok("hd", hd=receiver.hd_status())

        if command in ("local", "localplay", "local_playback"):
            self.receiver.local_playback = _flag(args, "LOCAL")
            return _ok("local", local_playback=self.receiver.local_playback)

        if command == "volume":
            if not args:
                raise ValueError("VOLUME needs a value between 0.0 and 5.0")
            receiver.set_volume(_number(args[0], "volume"))
            return _ok("volume", volume=receiver.volume)

        if command in ("preset", "presets"):
            return self._preset_command(args)

        return {"ok": False, "error": f"unknown command {command!r}",
                "help": servers.COMMAND_HELP}

    def _preset_command(self, args: list[str]) -> dict:
        """PRESET [LIST | SAVE [name] | RECALL key | DELETE key |
        RENAME key name | UP key | DOWN key]."""
        action = args[0].lower() if args else "list"
        rest = args[1:]

        if action in ("list", ""):
            return _ok("presets", presets=self.presets.list())

        if action in ("save", "add", "store"):
            frequency = self.receiver.frequency
            name_parts = list(rest)
            if name_parts and _as_float(name_parts[0]) is not None:
                # A leading number is always meant as the frequency, so an
                # off-band one is an error rather than part of the name.
                frequency = band.validate_any(name_parts.pop(0))
            name = " ".join(name_parts) or self._preset_autoname(frequency)
            item = self.presets.add(frequency, name)
            return _ok("preset save", preset=item,
                       presets=self.presets.list())

        if action in ("next", "prev", "previous"):
            item, freq = self.preset_step(+1 if action == "next" else -1)
            return _ok(f"preset {action}", preset=item, frequency_mhz=freq)

        if not rest:
            raise PresetError(f"PRESET {action.upper()} needs a preset "
                              f"number or name")
        key = " ".join(rest)

        if action in ("recall", "select", "go", "tune"):
            item = self.presets.find(key)
            if item is None:
                raise PresetError(f"no preset matching {key!r}")
            freq = self.receiver.tune(item["frequency_mhz"])
            self._preset_index = self._index_of(item)
            return _ok("preset recall", preset=item, frequency_mhz=freq)

        if action in ("delete", "remove", "del"):
            item = self.presets.remove(key)
            return _ok("preset delete", preset=item,
                       presets=self.presets.list())

        if action == "rename":
            parts = key.split(None, 1)
            if len(parts) < 2:
                raise PresetError("PRESET RENAME needs a preset and a name")
            item = self.presets.rename(parts[0], parts[1])
            return _ok("preset rename", preset=item,
                       presets=self.presets.list())

        if action in ("up", "down"):
            presets = self.presets.move(key, -1 if action == "up" else +1)
            return _ok(f"preset {action}", presets=presets)

        raise PresetError(f"unknown PRESET action {action!r}")

    def preset_step(self, direction: int) -> tuple[dict, float]:
        """Tune the next/previous preset in list order, wrapping at the
        ends. Starts from the preset on air, else the last one recalled."""
        presets = self.presets.list()
        if not presets:
            raise PresetError("no presets saved yet")
        on_air = [i for i, p in enumerate(presets)
                  if abs(p["frequency_mhz"] - self.receiver.frequency) < 0.005]
        if on_air:
            index = on_air[0]
        elif self._preset_index is not None and self._preset_index < len(presets):
            index = self._preset_index
        else:
            index = -1 if direction > 0 else 0
        index = (index + direction) % len(presets)
        item = presets[index]
        freq = self.receiver.tune(item["frequency_mhz"])
        self._preset_index = index
        return item, freq

    def _index_of(self, item: dict) -> int | None:
        for i, p in enumerate(self.presets.list()):
            if p["name"] == item["name"]:
                return i
        return None

    def _preset_autoname(self, frequency_mhz: float) -> str:
        """Name a new preset after the station on air, if it is identified."""
        rds = self.receiver.rds.snapshot()
        for key in ("callsign", "ps", "pi"):
            value = str((rds or {}).get(key) or "").strip()
            if value and value not in ("--", "--------"):
                return value
        return band.fmt(frequency_mhz)

    # -- events ------------------------------------------------------------

    def _event(self, kind: str, message: str) -> None:
        if self._on_event:
            try:
                self._on_event(kind, message)
            except Exception:
                pass

    def _hd_command(self, args: list[str]) -> None:
        """HD AUTO|MANUAL|HDONLY|ANALOG, HD PROGRAM 1-8|ANALOG,
        HD DELAY <seconds 0-10>."""
        if not args:
            raise ValueError("HD needs AUTO, MANUAL, HDONLY, ANALOG, "
                             "PROGRAM <1-8|ANALOG> or DELAY <seconds>")
        key = args[0].lower()
        value = args[1] if len(args) > 1 else ""
        if key in ("program", "programme", "prog"):
            if value.lower() in ("analog", "analogue", "0"):
                self.receiver.set_hd_program(None)
            else:
                self.receiver.set_hd_program(
                    int(_number(value.upper().lstrip("HD"), "programme")) - 1)
        elif key == "delay":
            self.receiver.set_hd_delay(_number(value, "delay"))
        else:
            self.receiver.set_hd_mode(
                {"hdonly": "hd_only", "hd-only": "hd_only",
                 "analogonly": "analog"}.get(key, key))

    def _dx_command(self, args: list[str]) -> None:
        """DX BANDWIDTH <kHz 56..230>, DX SIDE off|auto|low|high,
        DX CANCELLER on|off."""
        if len(args) < 2:
            raise ValueError("DX needs BANDWIDTH, SIDE or CANCELLER "
                             "and a value, e.g. DX SIDE HIGH")
        key, value = args[0].lower(), args[1].lower()
        if key in ("bandwidth", "bw"):
            self.receiver.set_dx(bandwidth=value)
        elif key == "side":
            self.receiver.set_dx(side=value)
        elif key in ("canceller", "canceler", "cancel"):
            self.receiver.set_dx(canceller=_flag([value], "DX CANCELLER"))
        else:
            raise ValueError(f"unknown DX setting {args[0]!r}")


def _ok(command: str, **fields) -> dict:
    reply = {"ok": True, "command": command, "timestamp": time.time()}
    reply.update(fields)
    return reply


def _as_float(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _number(text: str, what: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        raise ValueError(f"{text!r} is not a valid {what}")


def _flag(args: list[str], name: str) -> bool:
    value = args[0].lower() if args else ""
    if value in ("on", "1", "true", "yes", "enable", "enabled"):
        return True
    if value in ("off", "0", "false", "no", "disable", "disabled"):
        return False
    raise ValueError(f"{name} needs ON or OFF")


def _parse(text: str) -> tuple[str | None, list[str]]:
    """Accept either 'TUNE 101.1' or {"cmd": "tune", "frequency": 101.1}."""
    text = text.strip()
    if not text:
        return None, []
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None, []
        command = str(data.get("cmd") or data.get("command") or "").lower()
        if not command:
            return None, []
        args = []
        for key in ("action", "preset", "frequency", "frequency_mhz",
                    "value", "gain_db", "direction", "mode", "state",
                    "volume", "name", "arg"):
            if key in data and data[key] is not None:
                args.append(str(data[key]))
        return command, args
    parts = text.split()
    return parts[0].lower(), parts[1:]


__all__ = ["TunerService", "ReceiverState"]
