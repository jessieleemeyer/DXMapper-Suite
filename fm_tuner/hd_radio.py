"""HD Radio (NRSC-5 IBOC) decoding through the optional nrsc5 add-on.

The tuner keeps the dongle: its wideband IQ is resampled to nrsc5's native
FM rate and piped in, so nothing else opens the RTL-SDR. nrsc5 is not part
of this program; the user places libnrsc5 and its nrsc5.py wrapper in
settings/hd/ (the HD add-on), and without them HD is reported as not
installed while analog FM carries on as before.
"""

from __future__ import annotations

import collections
import ctypes
import importlib.util
import os
import tempfile
import shutil
import platform
import queue
import zipfile
import threading
import time
from fractions import Fraction

import numpy as np

from .settings_dir import settings_dir

HD_RATE = 744_187.5          # nrsc5 native FM input rate (complex float)
HD_AUDIO_RATE = 44_100       # nrsc5 audio output, stereo int16
HD_DIRNAME = "hd"
# Where hybrid IBOC sidebands sit either side of the analog carrier.
SIDEBAND_LOW_HZ = 129_000.0
SIDEBAND_HIGH_HZ = 198_000.0
HD_PROGRAMS = 8              # HD1-HD8
HD_AUDIO_SECONDS = 4.0       # most audio buffered per programme
HD_AUDIO_STALE_S = 2.0       # no audio for this long counts as lost
HD_MER_GOOD_DB = 10.0        # MER at/above this is solid HD
HD_MER_FAIR_DB = 6.0         # below this HD drops out a lot
HD_AUDIO_BAD_FLAGS = 0b11     # nrsc5 AudioFlags UNAVAILABLE | DECODING_ERROR
LIB_NAMES = {"Windows": "libnrsc5.dll", "Linux": "libnrsc5.so",
             "Darwin": "libnrsc5.dylib"}


class StreamResampler:
    """Rational-ratio polyphase resampler that keeps its state between
    calls, so a stream cut into any chunk sizes resamples exactly as if it
    were one block (no clicks or phase jumps at chunk edges)."""

    def __init__(self, in_rate: float, out_rate: float, cutoff_hz: float,
                 taps_per_phase: int = 24):
        from scipy.signal import firwin
        ratio = Fraction(out_rate) / Fraction(in_rate)
        self.up, self.down = ratio.numerator, ratio.denominator
        self.taps = taps_per_phase
        h = firwin(self.up * self.taps, cutoff_hz, window=("kaiser", 8.0),
                   fs=float(in_rate) * self.up) * self.up
        self._bank = np.ascontiguousarray(
            h.reshape(self.taps, self.up).T[:, ::-1], dtype=np.float32)
        self.reset()

    def reset(self) -> None:
        self._buf = None
        self._m = (self.taps - 1) * self.up

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x)
        if self._buf is None:
            self._buf = np.zeros(self.taps - 1, dtype=x.dtype)
        buf = np.concatenate((self._buf, x))
        n = max(0, -(-(len(buf) * self.up - self._m) // self.down))
        if n:
            pos = self._m + self.down * np.arange(n, dtype=np.int64)
            idx = (pos // self.up)[:, None] + np.arange(1 - self.taps, 1)
            out = np.einsum("nk,nk->n", buf[idx], self._bank[pos % self.up])
            self._m += n * self.down
        else:
            out = np.zeros(0, dtype=buf.dtype)
        keep = len(buf) - (self.taps - 1)
        self._m -= keep * self.up
        self._buf = buf[keep:]
        return out.astype(x.dtype, copy=False)


def addon_dir() -> str:
    return os.path.join(settings_dir(), HD_DIRNAME)


def find_addon(folder: str | None = None) -> tuple[str, str] | None:
    """(library path, wrapper path) of the HD add-on, or None."""
    folder = folder or os.environ.get("FM_TUNER_HD_DIR") or addon_dir()
    lib = os.path.join(folder, LIB_NAMES.get(platform.system(), ""))
    wrapper = os.path.join(folder, "nrsc5.py")
    if os.path.isfile(lib) and os.path.isfile(wrapper):
        return lib, wrapper
    return None


def install_addon(source: str, folder: str | None = None) -> str:
    """Copy the HD decoder add-on (a ZIP, its folder, or the library file
    itself) into settings/hd. Returns the folder; raises ValueError."""
    folder = folder or addon_dir()
    lib_name = LIB_NAMES.get(platform.system(), "libnrsc5.so")
    wanted = {lib_name, "nrsc5.py"}
    files: dict[str, bytes] = {}
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                base = os.path.basename(info.filename)
                if base in wanted or base.upper().startswith("LICENSE"):
                    files[base] = archive.read(info)
    else:
        src_dir = source if os.path.isdir(source) else os.path.dirname(source)
        for base in os.listdir(src_dir):
            if base in wanted or base.upper().startswith("LICENSE"):
                with open(os.path.join(src_dir, base), "rb") as handle:
                    files[base] = handle.read()
    missing = sorted(wanted - set(files))
    if missing:
        raise ValueError("Not an HD decoder add-on: missing "
                         + " and ".join(missing))
    os.makedirs(folder, exist_ok=True)
    for base, data in files.items():
        with open(os.path.join(folder, base), "wb") as handle:
            handle.write(data)
    return folder


def _load_nrsc5(lib: str, wrapper: str):
    spec = importlib.util.spec_from_file_location("fm_tuner_nrsc5", wrapper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.NRSC5.libnrsc5 is None:
        module.NRSC5.libnrsc5 = ctypes.CDLL(lib)
    return module


def _text(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


class HDDecoder:
    """Feeds wideband IQ to nrsc5 on its own thread and keeps the decoded
    station data and the selected programme's audio."""

    def __init__(self, in_rate: float, folder: str | None = None):
        self.available = False
        self.error: str | None = None
        self.dropped = 0
        self.audio_program: int | None = None
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=24)
        self._stop = threading.Event()
        self._reset_gen = 0
        self._audio: collections.deque = collections.deque()
        self._audio_samples = 0
        self._last_audio = 0.0
        self._clear_state()
        found = find_addon(folder)
        if found is None:
            self.error = "HD decoder not installed"
            return
        try:
            self._nrsc5 = _load_nrsc5(*found)
            self._radio = self._nrsc5.NRSC5(self._on_event)
            self._radio.open_pipe()
            self._radio.set_mode(self._nrsc5.Mode.FM)
            self._radio.start()
        except Exception as exc:
            self.error = f"HD decoder failed to load: {exc}"
            return
        self._resampler = StreamResampler(in_rate, HD_RATE, 300_000.0)
        self.available = True
        self._thread = threading.Thread(target=self._loop, name="fm-hd",
                                        daemon=True)
        self._thread.start()

    # -- called by the receiver --------------------------------------------

    def feed(self, iq: np.ndarray) -> None:
        if not self.available:
            return
        try:
            self._queue.put_nowait((self._reset_gen, iq))
        except queue.Full:
            self.dropped += 1

    def reset(self) -> None:
        """Forget the station after a retune."""
        with self._lock:
            self._reset_gen += 1
            self._clear_state()
            self._clear_audio()

    def set_audio_program(self, program: int | None) -> None:
        with self._lock:
            if program != self.audio_program:
                self.audio_program = program
                self._clear_audio()

    def pop_audio(self) -> np.ndarray | None:
        """All buffered audio of the selected programme, as float stereo
        frames (n, 2) at 44.1 kHz, or None."""
        with self._lock:
            if not self._audio:
                return None
            pcm = np.concatenate(list(self._audio))
            self._clear_audio()
        return pcm.reshape(-1, 2).astype(np.float32) / 32768.0

    def audio_live(self) -> bool:
        """True while the selected programme is producing audio."""
        return (self.synced and self.audio_program is not None
                and time.time() - self._last_audio < HD_AUDIO_STALE_S)

    @property
    def synced(self) -> bool:
        return bool(self._state["synced"])

    def snapshot(self) -> dict:
        with self._lock:
            state = dict(self._state)
            state["programs"] = [dict(p, program=n, hd=f"HD{n + 1}")
                                 for n, p in sorted(self._programs.items())]
            state["counts"] = dict(sorted(self._counts.items()))
            state["log"] = [list(x) for x in list(self._log)[-50:]]
        state.update(installed=self.available, error=self.error,
                     dropped=self.dropped, audio_program=self.audio_program)
        return state

    def close(self) -> None:
        self._drop_images()
        self._stop.set()
        if not self.available:
            return
        self.available = False
        thread = getattr(self, "_thread", None)
        if thread is not None:
            thread.join(timeout=3.0)
        try:
            self._radio.stop()
            self._radio.close()
        except Exception:
            pass

    # -- internals ---------------------------------------------------------

    def _clear_state(self) -> None:
        self._drop_images()
        self._state = {"synced": False, "ever_synced": False,
                       "mer_db": None, "ber": None, "freq_offset_hz": None,
                       "call": None, "facility_id": None, "country": None,
                       "slogan": None, "message": None, "location": None,
                       "logo_path": None, "logos": {}, "art_path": None,
                       "alert": None, "alert_categories": [],
                       "alert_time": None, "traffic_path": None,
                       "weather_path": None}
        self._programs: dict[int, dict] = {}
        self._counts: dict[str, int] = {}
        self._log: collections.deque = collections.deque(maxlen=200)

    def _image_folder(self) -> str:
        """Scratch folder for the images being shown now. Nothing here is
        kept: it is emptied on every retune and removed on exit. The RDS
        Monitor keeps the station logos itself, named CALL-HD#."""
        if getattr(self, "_images", None) is None:
            self._images = tempfile.mkdtemp(prefix="fm_tuner_hd_")
        return self._images

    def _drop_images(self) -> None:
        folder = getattr(self, "_images", None)
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
            self._images = None

    def _write_image(self, name: str, data: bytes) -> str | None:
        ext = ("png" if data[:8] == b"\x89PNG\r\n\x1a\n" else
               "jpg" if data[:2] == b"\xff\xd8" else
               "gif" if data[:4] == b"GIF8" else None)
        if ext is None:
            return None
        path = os.path.join(self._image_folder(), f"{name}.{ext}")
        with open(path + ".tmp", "wb") as handle:
            handle.write(data)
        os.replace(path + ".tmp", path)
        return path

    def _save_image(self, evt) -> None:
        """Station logo (one per HD channel) or album/artist art."""
        kind = getattr(evt.mime, "name", "")
        if kind not in ("STATION_LOGO", "PRIMARY_IMAGE") or not evt.data:
            return
        service = getattr(evt, "service", None)
        number = getattr(service, "number", None)
        hd = f"HD{number}" if isinstance(number, int) and 1 <= number <= 8 \
            else "HD1"
        if kind == "STATION_LOGO":
            path = self._write_image(f"logo_{hd}", bytes(evt.data))
            if path:
                self._state["logos"][hd] = path
                if hd == "HD1":
                    self._state["logo_path"] = path
        else:
            path = self._write_image(f"art_{hd}", bytes(evt.data))
            if path:
                self._state["art_path"] = path
                prog = self._programs.get(int(hd[2:]) - 1)
                if prog is not None:
                    prog["art_path"] = path

    def _save_here(self, evt) -> None:
        """Latest HERE traffic or weather map sent with the station."""
        kind = getattr(evt.image_type, "name", "")
        if kind not in ("TRAFFIC", "WEATHER") or not evt.data:
            return
        path = self._write_image(kind.lower(), bytes(evt.data))
        if path:
            self._state[f"{kind.lower()}_path"] = path

    def _clear_audio(self) -> None:
        self._audio.clear()
        self._audio_samples = 0

    def _program(self, n: int) -> dict:
        return self._programs.setdefault(
            int(n), {"name": None, "type": None, "title": None,
                     "artist": None, "album": None, "genre": None})

    def _loop(self) -> None:
        gen = self._reset_gen
        while not self._stop.is_set():
            try:
                item_gen, iq = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item_gen != self._reset_gen:
                continue
            try:
                if gen != item_gen:
                    gen = item_gen
                    self._resampler.reset()
                    self._radio.reset()
                samples = self._resampler.process(
                    np.asarray(iq, dtype=np.complex64))
                if len(samples):
                    self._radio.pipe_samples_cf32(
                        samples.view(np.float32).tobytes())
            except Exception as exc:
                self.error = f"HD decoder stopped: {exc}"
                self.available = False
                return

    def _on_event(self, evt_type, evt) -> None:
        try:
            self._handle(evt_type.name, evt)
            self._analyse(evt_type.name, evt)
        except Exception:
            pass

    def _analyse(self, name: str, evt) -> None:
        """HD equivalent of the RDS group analyser: a count per kind of
        data received and a short log of what arrived."""
        label, text = None, None
        if name in ("SYNC", "LOST_SYNC"):
            label = "Sync" if name == "SYNC" else "Lost sync"
            text = label
        elif name in ("SIS", "STATION_ID", "STATION_NAME", "STATION_SLOGAN",
                      "STATION_MESSAGE", "STATION_LOCATION"):
            label = "SIS"
            if name == "SIS":
                text = "SIS: " + " / ".join(
                    x for x in (_text(evt.name), _text(evt.slogan)) if x)
        elif name == "ID3":
            label = f"PSD HD{evt.program + 1}"
            song = " - ".join(x for x in (_text(evt.title),
                                          _text(evt.artist)) if x)
            text = f"{label}: {song}" if song else None
        elif name == "AUDIO":
            label = f"Audio HD{evt.program + 1}"
            if _bad_audio(evt):
                label += " dropout"
        elif name == "LOT":
            label = "LOT"
            text = (f"LOT: {getattr(evt.mime, 'name', evt.mime)} "
                    f"{_text(evt.name) or ''}").strip()
        elif name == "EMERGENCY_ALERT":
            label, text = "Alert", f"Alert: {_text(evt.message)}"
        elif name == "HERE_IMAGE":
            label = "HERE " + getattr(evt.image_type, "name", "?").title()
            text = label
        elif name in ("SIG", "AUDIO_SERVICE", "AUDIO_SERVICE_DESCRIPTOR"):
            label = "SIG"
        if label is None:
            return
        with self._lock:
            self._counts[label] = self._counts.get(label, 0) + 1
            if text and (not self._log or self._log[-1][1] != text):
                self._log.append((round(time.time(), 1), text))

    def _handle(self, name: str, evt) -> None:
        if name == "AUDIO":
            if _bad_audio(evt):
                return  # nrsc5 fills dropouts with silence; let Auto use analog
            with self._lock:
                self._program(evt.program)  # audio proves the channel exists
                if evt.program != self.audio_program:
                    return
                pcm = np.frombuffer(evt.data, dtype=np.int16).copy()
                self._audio.append(pcm)
                self._audio_samples += len(pcm)
                limit = int(HD_AUDIO_RATE * 2 * HD_AUDIO_SECONDS)
                while self._audio_samples > limit and len(self._audio) > 1:
                    self._audio_samples -= len(self._audio.popleft())
                self._last_audio = time.time()
            return
        with self._lock:
            state = self._state
            if name == "SYNC":
                state.update(synced=True, ever_synced=True,
                             freq_offset_hz=round(evt.freq_offset))
            elif name == "LOST_SYNC":
                state["synced"] = False
            elif name == "MER":
                state["mer_db"] = round((evt.lower + evt.upper) / 2, 1)
            elif name == "BER":
                state["ber"] = round(evt.cber, 4)
            elif name == "STATION_ID":
                state.update(country=_text(evt.country_code),
                             facility_id=evt.fcc_facility_id)
            elif name == "STATION_NAME":
                state["call"] = _text(evt.name)
            elif name == "STATION_SLOGAN":
                state["slogan"] = _text(evt.slogan)
            elif name == "STATION_MESSAGE":
                state["message"] = _text(evt.message)
            elif name == "STATION_LOCATION":
                state["location"] = [round(evt.latitude, 4),
                                     round(evt.longitude, 4)]
            elif name == "SIS":
                if _text(evt.name):
                    state["call"] = _text(evt.name)
                if _text(evt.slogan):
                    state["slogan"] = _text(evt.slogan)
                if evt.fcc_facility_id:
                    state["facility_id"] = evt.fcc_facility_id
                if _text(evt.message):
                    state["message"] = _text(evt.message)
                if _text(evt.alert):
                    state["alert"] = _text(evt.alert)
                    state["alert_categories"] = [
                        getattr(c, "name", str(c)).replace("_", " ").title()
                        for c in evt.alert_categories or []]
                    state["alert_time"] = time.time()
            elif name in ("AUDIO_SERVICE", "AUDIO_SERVICE_DESCRIPTOR"):
                ptype = getattr(evt.type, "name", None)
                if ptype and ptype != "UNDEFINED":
                    self._program(evt.program)["type"] = (
                        ptype.replace("_", " ").title())
                else:
                    self._program(evt.program)
            elif name == "SIG":
                for service in getattr(evt, "services", None) or []:
                    if (getattr(service.type, "name", "") == "AUDIO"
                            and _text(service.name)):
                        self._program(max(service.number - 1, 0))[
                            "name"] = _text(service.name)
            elif name == "EMERGENCY_ALERT":
                state["alert"] = _text(evt.message)
                state["alert_categories"] = [
                    getattr(c, "name", str(c)).replace("_", " ").title()
                    for c in evt.categories or []]
                state["alert_time"] = time.time()
            elif name == "HERE_IMAGE":
                self._save_here(evt)
            elif name == "LOT":
                self._save_image(evt)
            elif name == "ID3":
                prog = self._program(evt.program)
                for key in ("title", "artist", "album", "genre"):
                    value = _text(getattr(evt, key))
                    if value is not None:
                        prog[key] = value


def mer_grade(mer) -> str:
    """'good' / 'fair' / 'weak' for an HD MER in dB ('' when unknown)."""
    if not isinstance(mer, (int, float)):
        return ""
    if mer >= HD_MER_GOOD_DB:
        return "good"
    return "fair" if mer >= HD_MER_FAIR_DB else "weak"


def _bad_audio(evt) -> bool:
    return bool(int(getattr(evt, "flags", 0) or 0) & HD_AUDIO_BAD_FLAGS)


HD_MODES = ("auto", "manual", "hd_only", "analog")
HD_DELAY_DEFAULT_S = 0.0     # analog plays live, so tuning is instant
HD_DELAY_MAX_S = 10.0
HD_STEADY_S = 3.0            # HD must play this long before Auto goes back
HD_PRIME_S = 0.5             # HD audio buffered before it is played
FADE_S = 0.05


class AudioBlender:
    """Chooses analog, HD or silence for each block, like an HD car radio.

    Analog plays live (no delay line by default, so manual tuning gives
    instant feedback); every switch is a short crossfade.
    Auto drops to analog the moment HD runs dry and only returns once HD
    has been steady for a few seconds, so a fluttering signal does not
    bounce. All audio here is float stereo frames, shape (n, 2)."""

    def __init__(self, rate: int, delay_s: float = HD_DELAY_DEFAULT_S):
        self.rate = int(rate)
        self.set_delay(delay_s)
        self._fade = max(1, int(rate * FADE_S))
        self.reset()

    def set_delay(self, delay_s: float) -> None:
        delay_s = min(max(float(delay_s), 0.0), HD_DELAY_MAX_S)
        self.delay_s = delay_s
        self._delay = int(round(delay_s * self.rate))

    def reset(self) -> None:
        self._analog = np.zeros((0, 2), np.float32)
        self._hd = np.zeros((0, 2), np.float32)
        self._hd_primed = False
        self._hd_since: float | None = None
        self.source = "silence"
        self._last = np.zeros(2, np.float32)

    def push_analog(self, frames: np.ndarray) -> None:
        self._analog = np.concatenate((self._analog, _stereo(frames)))
        excess = len(self._analog) - self._delay - self.rate * 2
        if excess > 0:
            self._analog = self._analog[excess:]

    def push_hd(self, frames: np.ndarray) -> None:
        self._hd = np.concatenate((self._hd, _stereo(frames)))
        excess = len(self._hd) - int(self.rate * HD_DELAY_MAX_S)
        if excess > 0:
            self._hd = self._hd[excess:]

    def clear_hd(self) -> None:
        self._hd = self._hd[:0]
        self._hd_primed = False
        self._hd_since = None

    def pull(self, n: int, mode: str, hd_program: int | None,
             now: float) -> np.ndarray:
        analog = self._take_analog(n)
        if not self._hd_primed and len(self._hd) >= self.rate * HD_PRIME_S:
            self._hd_primed = True
        hd_ready = self._hd_primed and len(self._hd) >= n
        hd = None
        if hd_ready:
            if self._hd_since is None:
                self._hd_since = now
            # HD is consumed in real time even while analog plays, so it
            # stays lined up and a lost signal drains out promptly.
            hd, self._hd = self._hd[:n], self._hd[n:]
        else:
            self._hd_primed = False
            self._hd_since = None
        want = self._choose(mode, hd_program, hd_ready, now)
        if want == "hd":
            out = hd
        elif want == "analog":
            out = analog
        else:
            out = np.zeros((n, 2), np.float32)
        if want != self.source:
            out = out.copy()
            k = min(self._fade, n)
            ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)[:, None]
            out[:k] = self._last * (1 - ramp) + out[:k] * ramp
            self.source = want
        if n:
            self._last = out[-1].copy()
        return out

    def _choose(self, mode: str, hd_program: int | None, hd_ready: bool,
                now: float) -> str:
        if mode == "analog" or (mode == "manual" and hd_program is None):
            return "analog"
        if mode in ("hd_only", "manual"):
            return "hd" if hd_ready else "silence"
        # auto
        if not hd_ready:
            return "analog"
        if self.source == "hd" or now - self._hd_since >= HD_STEADY_S:
            return "hd"
        return "analog"

    def _take_analog(self, n: int) -> np.ndarray:
        """n frames from the delay line; silence while it fills."""
        ready = len(self._analog) - self._delay
        if ready <= 0:
            return np.zeros((n, 2), np.float32)
        take = min(n, ready)
        out = self._analog[:take]
        self._analog = self._analog[take:]
        if take < n:
            out = np.concatenate((np.zeros((n - take, 2), np.float32), out))
        return out


def _stereo(frames: np.ndarray) -> np.ndarray:
    frames = np.asarray(frames, dtype=np.float32)
    if frames.ndim == 1:
        return np.repeat(frames[:, None], 2, axis=1)
    return frames
