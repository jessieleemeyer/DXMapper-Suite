"""SoapySDR radios (HackRF and others) through SoapySDR's C library.

Loaded with ctypes like rtlsdr.dll, so it works with any Python version: no
SoapySDR Python bindings needed, just SoapySDR.dll from PothosSDR or
radioconda plus the radio's driver module.

The tuner's DSP runs at IQ_RATE (960 kHz) as 8-bit IQ, the RTL-SDR's format.
A SoapySDR radio is run at a whole multiple of that (a HackRF at 1.92 MHz,
since it is not rated below 2 MHz), low-pass filtered to about 1 MHz and
sliced down to 960 kHz here, so everything after this sees the same stream.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import queue
import threading
import time
from collections import deque

import numpy as np

from .rtlsdr_device import RtlSdrError

SOAPY_SDR_RX = 1
SOAPY_SDR_TIMEOUT = -1
SOAPY_SDR_OVERFLOW = -4
SPEC_PREFIX = "soapy:"
OUT_RATE = 960_000            # receiver.IQ_RATE
PASSBAND_HZ = 440_000.0       # keep +/-440 kHz: about 1 MHz wide
FILTER_TAPS = 63
READ_ELEMS = 65_536             # ~34 ms at 1.92 MHz
READ_QUEUE = 64                # ~2 s of samples waiting to be processed
READ_TIMEOUT_US = 200_000
# Drivers this binding leaves out: RTL dongles have their own direct path,
# the rest are not antennas.
SKIP_DRIVERS = {"rtlsdr", "audio", "remote"}
# Lowest rate each driver is rated for (the HackRF's 2 MHz, less 4 %).
MIN_RATE_HZ = {"hackrf": 1_900_000}
# Radios that take any rate in this span, even when SoapySDR only lists a few
# whole-MHz rates (the HackRF lists 1, 2, ... 20 MHz but accepts 1.92 MHz).
CONTINUOUS_RATES_HZ = {"hackrf": (1_000_000, 20_000_000)}

HACKRF_LNA_MAX, HACKRF_LNA_STEP = 40, 8
HACKRF_VGA_MAX, HACKRF_VGA_STEP = 62, 2
# Auto gain on a radio with no hardware AGC (the HackRF): a fixed middle
# setting instead of 0 dB, which is too weak to hear anything.
AUTO_GAIN_DB = 48.0

INSTALL_HINT = (
    "SoapySDR isn't installed. To use a HackRF (or another SoapySDR radio), "
    "install PothosSDR - see step 4a in INSTALL.txt - then restart the tuner.")


class SoapyError(RtlSdrError):
    """A SoapySDR library or radio failure, with a readable message."""


class Kwargs(ctypes.Structure):
    _fields_ = [("size", ctypes.c_size_t),
                ("keys", ctypes.POINTER(ctypes.c_char_p)),
                ("vals", ctypes.POINTER(ctypes.c_char_p))]


class Range(ctypes.Structure):
    _fields_ = [("minimum", ctypes.c_double), ("maximum", ctypes.c_double),
                ("step", ctypes.c_double)]


_dev, _int, _size = ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t
_SIGNATURES = {
    "SoapySDRDevice_lastError": ([], ctypes.c_char_p),
    "SoapySDRDevice_enumerate": (
        [ctypes.c_void_p, ctypes.POINTER(_size)], ctypes.POINTER(Kwargs)),
    "SoapySDRKwargsList_clear": ([ctypes.POINTER(Kwargs), _size], None),
    "SoapySDRDevice_makeStrArgs": ([ctypes.c_char_p], _dev),
    "SoapySDRDevice_unmake": ([_dev], _int),
    "SoapySDRDevice_getHardwareKey": ([_dev], ctypes.c_void_p),
    "SoapySDR_free": ([ctypes.c_void_p], None),
    "SoapySDRDevice_getSampleRateRange": (
        [_dev, _int, _size, ctypes.POINTER(_size)], ctypes.POINTER(Range)),
    "SoapySDRDevice_setSampleRate": ([_dev, _int, _size, ctypes.c_double], _int),
    "SoapySDRDevice_getSampleRate": ([_dev, _int, _size], ctypes.c_double),
    "SoapySDRDevice_setBandwidth": ([_dev, _int, _size, ctypes.c_double], _int),
    "SoapySDRDevice_setFrequency": (
        [_dev, _int, _size, ctypes.c_double, ctypes.c_void_p], _int),
    "SoapySDRDevice_getFrequency": ([_dev, _int, _size], ctypes.c_double),
    "SoapySDRDevice_setFrequencyCorrection": (
        [_dev, _int, _size, ctypes.c_double], _int),
    "SoapySDRDevice_hasGainMode": ([_dev, _int, _size], ctypes.c_bool),
    "SoapySDRDevice_setGainMode": ([_dev, _int, _size, ctypes.c_bool], _int),
    "SoapySDRDevice_listGains": (
        [_dev, _int, _size, ctypes.POINTER(_size)],
        ctypes.POINTER(ctypes.c_char_p)),
    "SoapySDRStrings_clear": (
        [ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p)), _size], None),
    "SoapySDRDevice_setGain": ([_dev, _int, _size, ctypes.c_double], _int),
    "SoapySDRDevice_getGain": ([_dev, _int, _size], ctypes.c_double),
    "SoapySDRDevice_getGainRange": ([_dev, _int, _size], Range),
    "SoapySDRDevice_setGainElement": (
        [_dev, _int, _size, ctypes.c_char_p, ctypes.c_double], _int),
    "SoapySDRDevice_setupStream": (
        [_dev, _int, ctypes.c_char_p, ctypes.POINTER(_size), _size,
         ctypes.c_void_p], ctypes.c_void_p),
    "SoapySDRDevice_activateStream": (
        [_dev, ctypes.c_void_p, _int, ctypes.c_longlong, _size], _int),
    "SoapySDRDevice_deactivateStream": (
        [_dev, ctypes.c_void_p, _int, ctypes.c_longlong], _int),
    "SoapySDRDevice_closeStream": ([_dev, ctypes.c_void_p], _int),
    "SoapySDRDevice_readStream": (
        [_dev, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), _size,
         ctypes.POINTER(_int), ctypes.POINTER(ctypes.c_longlong),
         ctypes.c_long], _int),
}

_lib_lock = threading.Lock()
_lib: ctypes.CDLL | None = None


def _windows_dirs() -> list[str]:
    home = os.path.expanduser("~")
    dirs = [os.path.dirname(os.path.abspath(sys.argv[0] or "."))]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        dirs.append(os.path.join(base, "PothosSDR", "bin"))
    for root in (os.path.join(home, "radioconda"),
                 os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                              "radioconda"),
                 os.path.join(os.environ.get("LOCALAPPDATA", home),
                              "radioconda")):
        dirs.append(os.path.join(root, "Library", "bin"))
    return dirs


def find_library() -> str | None:
    """Full path of SoapySDR.dll (or the .so elsewhere), or None."""
    env = os.environ.get("SOAPYSDR_DLL")
    if env and os.path.isfile(env):
        return env
    if os.name == "nt":
        for directory in _windows_dirs():
            candidate = os.path.join(directory, "SoapySDR.dll")
            if os.path.isfile(candidate):
                return candidate
        return None
    return ctypes.util.find_library("SoapySDR")


def _prepare_windows(path: str) -> None:
    """Let SoapySDR find its driver modules and their DLLs (hackrf.dll,
    libusb) beside it, as PothosSDR and radioconda lay them out."""
    bin_dir = os.path.dirname(path)
    os.environ.setdefault("SOAPY_SDR_ROOT", os.path.dirname(bin_dir))
    if bin_dir.lower() not in os.environ.get("PATH", "").lower():
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    try:
        os.add_dll_directory(bin_dir)
    except (AttributeError, OSError):
        pass


def _bind(dll: ctypes.CDLL) -> ctypes.CDLL:
    for name, (argtypes, restype) in _SIGNATURES.items():
        try:
            func = getattr(dll, name)
        except AttributeError:
            continue
        func.argtypes = argtypes
        func.restype = restype
    return dll


# SoapySDR prints chatter (VOLK tips, SDRplay service errors when no SDRplay
# is plugged in) to the console. Keep it out of sight; the rest is kept here.
LOG_NOISE = ("sdrplay", "volk")
soapy_log: deque[str] = deque(maxlen=50)
_LOG_HANDLER_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p)


def _on_soapy_log(_level, message) -> None:
    text = (message or b"").decode("utf-8", "replace").strip()
    if text and not any(n in text.lower() for n in LOG_NOISE):
        soapy_log.append(text)


_log_handler = _LOG_HANDLER_TYPE(_on_soapy_log)


def _quiet_logging(dll: ctypes.CDLL) -> None:
    try:
        dll.SoapySDR_registerLogHandler.argtypes = [_LOG_HANDLER_TYPE]
        dll.SoapySDR_registerLogHandler.restype = None
        dll.SoapySDR_registerLogHandler(_log_handler)
    except AttributeError:
        pass


def load_library() -> ctypes.CDLL:
    global _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        path = find_library()
        if not path:
            raise SoapyError(INSTALL_HINT)
        if os.name == "nt":
            _prepare_windows(path)
        try:
            _lib = _bind(ctypes.CDLL(path))
            _quiet_logging(_lib)
        except OSError as exc:
            raise SoapyError(f"{INSTALL_HINT}\n({path}: {exc})") from None
        return _lib


def available() -> bool:
    try:
        load_library()
        return True
    except SoapyError:
        return False


def _kwargs_dict(kw: Kwargs) -> dict[str, str]:
    return {kw.keys[i].decode("utf-8", "replace"):
            kw.vals[i].decode("utf-8", "replace") for i in range(kw.size)}


def enumerate_radios(dll: ctypes.CDLL | None = None) -> list[dict[str, str]]:
    """Every SoapySDR radio plugged in (except RTL-SDRs), as kwargs dicts."""
    dll = dll or load_library()
    length = ctypes.c_size_t(0)
    found = dll.SoapySDRDevice_enumerate(None, ctypes.byref(length))
    radios = []
    try:
        for i in range(length.value):
            kw = _kwargs_dict(found[i])
            if kw.get("driver", "").lower() not in SKIP_DRIVERS:
                radios.append(kw)
    finally:
        if found and length.value:
            dll.SoapySDRKwargsList_clear(found, length.value)
    return radios


def spec_for(kw: dict[str, str]) -> str:
    """The saved setting that reopens this exact radio."""
    keys = [k for k in ("driver", "serial") if kw.get(k)]
    return SPEC_PREFIX + ",".join(f"{k}={kw[k]}" for k in keys)


def label_for(kw: dict[str, str]) -> str:
    label = kw.get("label") or kw.get("product") or kw.get("driver", "SDR")
    serial = kw.get("serial", "")
    if serial and serial[-8:] not in label:
        label += f" SN {serial[-8:]}"
    return label


def pick_rate(ranges: list[tuple[float, float]], minimum: float = 0.0,
              out_rate: int = OUT_RATE) -> int | None:
    """The lowest whole multiple (2x or more) of `out_rate` the radio can
    sample at; plain `out_rate` only as a last resort."""
    for factor in list(range(2, 21)) + [1]:
        rate = out_rate * factor
        if rate < minimum:
            continue
        if any(lo - 1 <= rate <= hi + 1 for lo, hi in ranges):
            return rate
    return None


def lowpass_taps(in_rate: float, passband_hz: float = PASSBAND_HZ,
                 taps: int = FILTER_TAPS) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * passband_hz / in_rate * n) * np.blackman(taps)
    return (h / h.sum()).astype(np.float32)


class Decimator:
    """Low-pass then keep every `factor`-th sample; gapless across blocks."""

    def __init__(self, factor: int, in_rate: float):
        self.factor = int(factor)
        self.taps = lowpass_taps(in_rate)[::-1].copy()
        self._history = np.zeros(len(self.taps) - 1, dtype=np.complex64)
        self._phase = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.factor == 1:
            return x.astype(np.complex64, copy=False)
        data = np.concatenate([self._history, x.astype(np.complex64)])
        n = len(self.taps)
        windows = np.lib.stride_tricks.sliding_window_view(data, n)
        picked = windows[self._phase::self.factor]
        out = picked @ self.taps
        used = self._phase + len(picked) * self.factor
        self._phase = used - len(windows)
        self._history = data[len(data) - (n - 1):].copy()
        return out.astype(np.complex64)


_QUARTER_TURNS = np.array([1, 1j, -1, -1j], dtype=np.complex64)


def to_u8(iq: np.ndarray) -> bytes:
    """Complex samples (full scale +/-1) as the RTL-SDR's interleaved uint8."""
    raw = np.empty(len(iq) * 2, dtype=np.float32)
    raw[0::2] = iq.real
    raw[1::2] = iq.imag
    return np.clip(raw * 127.5 + 127.5, 0, 255).astype(np.uint8).tobytes()


class SoapyDevice:
    """One open SoapySDR radio, with the same methods the receiver uses on
    RtlSdrDevice. Starts at 0 dB gain like the RTL-SDR does."""

    def __init__(self, spec: str, dll: ctypes.CDLL | None = None):
        self._dll = dll or load_library()
        self._spec = spec
        args = spec[len(SPEC_PREFIX):] if spec.startswith(SPEC_PREFIX) else spec
        self._args = dict(part.split("=", 1) for part in args.split(",")
                          if "=" in part)
        self._driver = self._args.get("driver", "").lower()
        self._dev = self._dll.SoapySDRDevice_makeStrArgs(args.encode())
        if not self._dev:
            raise SoapyError(
                f"Couldn't open the {self._driver or 'SoapySDR'} radio: "
                f"{self._last_error()}. Check it's plugged in, that no other "
                "program (SDR#, SDR++) is using it, and that its USB driver "
                "is installed (see INSTALL.txt step 4a).")
        self._closed = False
        self._stream = None
        self._cancel = threading.Event()
        self._gain_db = 0.0
        self._gain_mode_manual = True
        self._label = self._find_label()
        self._in_rate = OUT_RATE
        self._decimator = Decimator(1, OUT_RATE)
        self._offset_hz = 0.0
        self._mix_phase = 0
        self.on_message = None  # (kind, text) -> None; set by the receiver
        self.overflows = 0
        self.set_gain_db(0.0)

    # -- helpers ---------------------------------------------------------

    def _last_error(self) -> str:
        try:
            raw = self._dll.SoapySDRDevice_lastError()
            return raw.decode("utf-8", "replace") if raw else "unknown error"
        except AttributeError:
            return "unknown error"

    def _check(self, result: int, what: str) -> None:
        if result != 0:
            raise SoapyError(f"{what} failed: {self._last_error()}")

    def _find_label(self) -> str:
        try:
            for kw in enumerate_radios(self._dll):
                if all(kw.get(k) == v for k, v in self._args.items()):
                    return label_for(kw)
        except Exception:
            pass
        return self._driver or "SoapySDR"

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def index(self) -> int:
        return 0

    @property
    def gain_mode_manual(self) -> bool:
        return self._gain_mode_manual

    @property
    def gain_db(self) -> float:
        return self._gain_db

    @property
    def bias_tee(self) -> bool:
        return False

    @property
    def input_rate(self) -> int:
        return int(self._in_rate)

    def bias_tee_supported(self) -> bool:
        return False

    def tuner_type(self) -> str:
        ptr = self._dll.SoapySDRDevice_getHardwareKey(self._dev)
        if not ptr:
            return self._driver
        try:
            return ctypes.string_at(ptr).decode("utf-8", "replace")
        finally:
            self._dll.SoapySDR_free(ptr)

    def name(self) -> str:
        return self._label

    def usb_label(self) -> str:
        return self._label

    # -- configuration -----------------------------------------------------

    def _rate_ranges(self) -> list[tuple[float, float]]:
        length = ctypes.c_size_t(0)
        found = self._dll.SoapySDRDevice_getSampleRateRange(
            self._dev, SOAPY_SDR_RX, 0, ctypes.byref(length))
        try:
            return [(found[i].minimum, found[i].maximum)
                    for i in range(length.value)]
        finally:
            if found:
                self._dll.SoapySDR_free(ctypes.cast(found, ctypes.c_void_p))

    def set_sample_rate(self, rate_hz: int) -> None:
        """`rate_hz` is the rate the tuner wants out; the radio itself runs
        at a whole multiple of it."""
        ranges = self._rate_ranges()
        if self._driver in CONTINUOUS_RATES_HZ:
            ranges.append(CONTINUOUS_RATES_HZ[self._driver])
        rate = pick_rate(ranges, MIN_RATE_HZ.get(self._driver, 0),
                         int(rate_hz))
        if rate is None:
            raise SoapyError(
                f"This radio can't sample at a multiple of "
                f"{rate_hz / 1000:.0f} kHz, so the tuner can't use it yet.")
        self._check(self._dll.SoapySDRDevice_setSampleRate(
            self._dev, SOAPY_SDR_RX, 0, float(rate)), f"Sample rate {rate}")
        self._in_rate = rate
        self._decimator = Decimator(rate // int(rate_hz), rate)
        self._offset_hz = rate / 4 if rate / 4 >= PASSBAND_HZ else 0.0
        self._mix_phase = 0
        # Narrowest analog filter that still passes the ~1 MHz kept; the
        # HackRF rounds this to its 1.75 MHz filter.
        self._dll.SoapySDRDevice_setBandwidth(self._dev, SOAPY_SDR_RX, 0,
                                              float(rate))

    def get_sample_rate(self) -> int:
        return OUT_RATE

    def set_center_freq(self, freq_hz: float) -> None:
        """The radio tunes a quarter of its sample rate above the station so
        its DC spike stays out of the station; _mix() shifts it back."""
        self._check(self._dll.SoapySDRDevice_setFrequency(
            self._dev, SOAPY_SDR_RX, 0, float(freq_hz) + self._offset_hz,
            None), f"Tune to {freq_hz / 1e6:.3f} MHz")

    def get_center_freq(self) -> int:
        return int(self._dll.SoapySDRDevice_getFrequency(
            self._dev, SOAPY_SDR_RX, 0) - self._offset_hz)

    def _mix(self, x: np.ndarray) -> np.ndarray:
        """Move the station from -rate/4 back to 0 Hz (multiply by j**n)."""
        if not self._offset_hz:
            return x
        turns = (np.arange(len(x)) + self._mix_phase) % 4
        self._mix_phase = (self._mix_phase + len(x)) % 4
        return x * _QUARTER_TURNS[turns]

    def set_freq_correction(self, ppm: int) -> None:
        try:
            self._dll.SoapySDRDevice_setFrequencyCorrection(
                self._dev, SOAPY_SDR_RX, 0, float(ppm))
        except AttributeError:
            pass

    def set_gain_mode(self, manual: bool) -> None:
        self._gain_mode_manual = bool(manual)
        if self._dll.SoapySDRDevice_hasGainMode(self._dev, SOAPY_SDR_RX, 0):
            self._dll.SoapySDRDevice_setGainMode(self._dev, SOAPY_SDR_RX, 0,
                                                 not manual)
        elif not manual:
            self.set_gain_db(AUTO_GAIN_DB)

    def set_agc_mode(self, enabled: bool) -> None:
        pass

    def set_bias_tee(self, enabled: bool, gpio: int = 0) -> None:
        if enabled:
            raise SoapyError("Bias-T isn't switched from the tuner on this "
                             "radio.")

    def supported_gains_db(self) -> list[float]:
        if self._driver == "hackrf":
            top = HACKRF_LNA_MAX + HACKRF_VGA_MAX
            return [float(g) for g in range(0, top + 1, HACKRF_VGA_STEP)]
        r = self._dll.SoapySDRDevice_getGainRange(self._dev, SOAPY_SDR_RX, 0)
        step = r.step if r.step > 0 else 1.0
        count = min(int((r.maximum - r.minimum) / step) + 1, 256)
        return [round(r.minimum + i * step, 1) for i in range(count)]

    @staticmethod
    def hackrf_split(gain_db: float) -> tuple[int, int]:
        """HackRF gain as (LNA, VGA): LNA first in its 8 dB steps, the rest
        on the VGA in 2 dB steps. The 14 dB RF amp stays off."""
        total = max(0.0, min(float(gain_db), HACKRF_LNA_MAX + HACKRF_VGA_MAX))
        lna = min(HACKRF_LNA_MAX, int(total // HACKRF_LNA_STEP)
                  * HACKRF_LNA_STEP)
        vga = min(HACKRF_VGA_MAX, int(round((total - lna) / HACKRF_VGA_STEP))
                  * HACKRF_VGA_STEP)
        return lna, vga

    def set_gain_db(self, gain_db: float) -> float:
        if self._driver == "hackrf":
            lna, vga = self.hackrf_split(gain_db)
            set_el = self._dll.SoapySDRDevice_setGainElement
            self._check(set_el(self._dev, SOAPY_SDR_RX, 0, b"AMP", 0.0), "AMP")
            self._check(set_el(self._dev, SOAPY_SDR_RX, 0, b"LNA", float(lna)),
                        "LNA gain")
            self._check(set_el(self._dev, SOAPY_SDR_RX, 0, b"VGA", float(vga)),
                        "VGA gain")
            self._gain_db = float(lna + vga)
        else:
            self._check(self._dll.SoapySDRDevice_setGain(
                self._dev, SOAPY_SDR_RX, 0, float(gain_db)), "Gain")
            self._gain_db = float(self._dll.SoapySDRDevice_getGain(
                self._dev, SOAPY_SDR_RX, 0))
        return self._gain_db

    def get_gain_db(self) -> float:
        if self._driver == "hackrf":
            return self._gain_db
        return float(self._dll.SoapySDRDevice_getGain(
            self._dev, SOAPY_SDR_RX, 0))

    # -- streaming ---------------------------------------------------------

    def reset_buffer(self) -> None:
        pass

    def async_supported(self) -> bool:
        return True

    def read_sync(self, buffer, length: int) -> int:
        raise SoapyError("SoapySDR radios only stream continuously.")

    def _say(self, kind: str, text: str) -> None:
        if self.on_message:
            try:
                self.on_message(kind, text)
            except Exception:
                pass

    def _read_loop(self, stream, chunks: queue.Queue) -> None:
        """Only reads from the radio, so it never falls behind; the
        filtering happens in read_async's thread."""
        flags, time_ns = ctypes.c_int(0), ctypes.c_longlong(0)
        error = None
        try:
            while not self._cancel.is_set():
                buf = np.empty(READ_ELEMS, dtype=np.complex64)
                ptrs = (ctypes.c_void_p * 1)(buf.ctypes.data)
                n = self._dll.SoapySDRDevice_readStream(
                    self._dev, stream, ptrs, READ_ELEMS, ctypes.byref(flags),
                    ctypes.byref(time_ns), READ_TIMEOUT_US)
                if n == SOAPY_SDR_TIMEOUT:
                    continue
                if n == SOAPY_SDR_OVERFLOW:
                    self.overflows += 1
                    if self.overflows in (1, 10, 100, 1000):
                        self._say("warning", f"{self._label} dropped samples "
                                  f"(overflow {self.overflows}): the PC didn't "
                                  "read them fast enough.")
                    continue
                if n < 0:
                    if not self._cancel.is_set():
                        error = n
                    break
                try:
                    chunks.put_nowait(buf[:n])
                except queue.Full:
                    self.overflows += 1
        finally:
            try:
                chunks.put(error, timeout=1)
            except queue.Full:
                pass

    def read_async(self, on_samples, buffer_count: int = 16,
                   buffer_length: int = 131_072) -> None:
        """Stream until cancel_async(), calling on_samples(bytes) with
        `buffer_length` bytes of 960 kHz uint8 IQ each time."""
        self._cancel.clear()
        stream = self._dll.SoapySDRDevice_setupStream(
            self._dev, SOAPY_SDR_RX, b"CF32", None, 0, None)
        if not stream:
            raise SoapyError(f"Couldn't start the stream: {self._last_error()}")
        self._stream = stream
        self.overflows = 0
        out_needed = buffer_length // 2
        pending: list[np.ndarray] = []
        have = 0
        chunks: queue.Queue = queue.Queue(maxsize=READ_QUEUE)
        reader = None
        try:
            self._check(self._dll.SoapySDRDevice_activateStream(
                self._dev, stream, 0, 0, 0), "Start stream")
            reader = threading.Thread(target=self._read_loop,
                                      args=(stream, chunks),
                                      name="soapy-read", daemon=True)
            reader.start()
            started, got, reported = time.monotonic(), 0, False
            while True:
                chunk = chunks.get()
                if chunk is None or isinstance(chunk, int):
                    if isinstance(chunk, int) and not self._cancel.is_set():
                        raise SoapyError(
                            f"The radio stopped sending ({chunk}). It may "
                            "have been unplugged.")
                    break
                got += len(chunk)
                if not reported and time.monotonic() - started >= 5:
                    reported = True
                    rate = got / (time.monotonic() - started)
                    self._say("info", f"{self._label}: {rate / 1e6:.2f} MHz "
                              f"coming in (should be {self._in_rate / 1e6:.2f}"
                              f"), {self.overflows} overflows")
                out = self._decimator.process(self._mix(chunk))
                pending.append(out)
                have += len(out)
                while have >= out_needed:
                    block = np.concatenate(pending)
                    try:
                        on_samples(to_u8(block[:out_needed]))
                    except Exception:
                        pass
                    rest = block[out_needed:]
                    pending, have = [rest], len(rest)
        finally:
            self._cancel.set()
            if reader:
                reader.join(timeout=2)
            self._dll.SoapySDRDevice_deactivateStream(self._dev, stream, 0, 0)
            self._dll.SoapySDRDevice_closeStream(self._dev, stream)
            self._stream = None

    def cancel_async(self) -> None:
        self._cancel.set()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        try:
            self._dll.SoapySDRDevice_unmake(self._dev)
        except Exception:
            pass
        self._dev = None

    def __enter__(self) -> "SoapyDevice":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
