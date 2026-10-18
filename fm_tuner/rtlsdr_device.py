"""ctypes binding to rtlsdr.dll (librtlsdr) for Windows.

This keeps the approach used by the original RDS Monitor: no pyrtlsdr, just
the stock RTL-SDR DLL loaded directly.  Download rtlsdr.dll from
https://github.com/rtlsdrblog/rtl-sdr-blog/releases/latest/download/Release.zip
(x64 folder) and put it next to this project, or set the RTLSDR_DLL
environment variable to its full path.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

TUNER_TYPES = {
    0: "Unknown",
    1: "E4000",
    2: "FC0012",
    3: "FC0013",
    4: "FC2580",
    5: "R820T",
    6: "R828D",
}

# Names tried in order.  Windows uses rtlsdr.dll; the others let the module
# load on Linux/macOS for development and code-level testing.
_LIB_NAMES = ["rtlsdr.dll", "librtlsdr.so.0", "librtlsdr.so", "librtlsdr.dylib"]


# void (*rtlsdr_read_async_cb_t)(unsigned char *buf, uint32_t len, void *ctx)
READ_ASYNC_CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_ubyte),
                                 ctypes.c_uint32, ctypes.c_void_p)


class RtlSdrError(RuntimeError):
    """Raised for DLL, device and API failures, with an actionable message."""


def find_library() -> str | None:
    """Locate the RTL-SDR shared library. Returns a full path or None."""
    env = os.environ.get("RTLSDR_DLL")
    if env and os.path.isfile(env):
        return env

    search_dirs: list[str] = [
        str(Path(__file__).resolve().parent),
        str(Path(__file__).resolve().parent.parent),
        os.getcwd(),
    ]
    if getattr(sys, "frozen", False):
        search_dirs.insert(0, os.path.dirname(sys.executable))
    if os.name == "nt":
        search_dirs += [
            os.path.expanduser(r"~\Downloads"),
        ]
        for base in (r"C:\Program Files (x86)", r"C:\Program Files",
                     str(Path(__file__).resolve().parents[2])):
            if os.path.isdir(base):
                try:
                    for name in os.listdir(base):
                        full = os.path.join(base, name)
                        if ("sdr" in name.lower() or "rds" in name.lower()) and os.path.isdir(full):
                            search_dirs.append(full)
                except OSError:
                    pass

    for directory in search_dirs:
        for lib_name in _LIB_NAMES:
            candidate = os.path.join(directory, lib_name)
            if os.path.isfile(candidate):
                return candidate
    return None


def load_library() -> ctypes.CDLL:
    """Load the RTL-SDR library, raising RtlSdrError with guidance."""
    path = find_library()
    errors = []
    if path:
        try:
            return _bind(ctypes.CDLL(path))
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    for lib_name in _LIB_NAMES:
        try:
            return _bind(ctypes.CDLL(lib_name))
        except OSError as exc:
            errors.append(f"{lib_name}: {exc}")

    detail = "\n  ".join(errors) if errors else "no candidates found"
    raise RtlSdrError(
        "rtlsdr.dll could not be loaded.\n"
        "Download it from "
        "https://github.com/rtlsdrblog/rtl-sdr-blog/releases/latest/"
        "download/Release.zip (x64 folder) and copy rtlsdr.dll next to "
        "this application, or set the RTLSDR_DLL environment variable.\n"
        f"Tried:\n  {detail}")


def _bind(dll: ctypes.CDLL) -> ctypes.CDLL:
    """Apply argtypes/restype to the functions we use."""
    p = ctypes.c_void_p
    signatures = {
        "rtlsdr_get_device_count": ([], ctypes.c_uint32),
        "rtlsdr_get_device_name": ([ctypes.c_uint32], ctypes.c_char_p),
        "rtlsdr_get_device_usb_strings": ([ctypes.c_uint32, ctypes.c_char_p,
                                           ctypes.c_char_p, ctypes.c_char_p],
                                          ctypes.c_int),
        "rtlsdr_open": ([ctypes.POINTER(p), ctypes.c_uint32], ctypes.c_int),
        "rtlsdr_close": ([p], ctypes.c_int),
        "rtlsdr_set_sample_rate": ([p, ctypes.c_uint32], ctypes.c_int),
        "rtlsdr_get_sample_rate": ([p], ctypes.c_uint32),
        "rtlsdr_set_center_freq": ([p, ctypes.c_uint32], ctypes.c_int),
        "rtlsdr_get_center_freq": ([p], ctypes.c_uint32),
        "rtlsdr_set_freq_correction": ([p, ctypes.c_int], ctypes.c_int),
        "rtlsdr_set_tuner_gain_mode": ([p, ctypes.c_int], ctypes.c_int),
        "rtlsdr_set_tuner_gain": ([p, ctypes.c_int], ctypes.c_int),
        "rtlsdr_get_tuner_gain": ([p], ctypes.c_int),
        "rtlsdr_get_tuner_gains": ([p, ctypes.POINTER(ctypes.c_int)],
                                   ctypes.c_int),
        "rtlsdr_get_tuner_type": ([p], ctypes.c_int),
        "rtlsdr_set_agc_mode": ([p, ctypes.c_int], ctypes.c_int),
        "rtlsdr_set_bias_tee": ([p, ctypes.c_int], ctypes.c_int),
        "rtlsdr_set_bias_tee_gpio": ([p, ctypes.c_int, ctypes.c_int],
                                     ctypes.c_int),
        "rtlsdr_reset_buffer": ([p], ctypes.c_int),
        "rtlsdr_read_sync": ([p, ctypes.c_void_p, ctypes.c_int,
                              ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
        "rtlsdr_read_async": ([p, READ_ASYNC_CB, ctypes.c_void_p,
                               ctypes.c_uint32, ctypes.c_uint32],
                              ctypes.c_int),
        "rtlsdr_cancel_async": ([p], ctypes.c_int),
    }
    for name, (argtypes, restype) in signatures.items():
        try:
            func = getattr(dll, name)
        except AttributeError:
            continue
        func.argtypes = argtypes
        func.restype = restype
    return dll


class RtlSdrDevice:
    """A single open RTL-SDR device.

    Gain handling follows the project brief: the device is opened with the
    tuner in manual gain mode at 0.0 dB and the RTL2832 digital AGC off, so
    reception never silently starts with automatic or nonzero gain.
    """

    def __init__(self, index: int = 0, dll: ctypes.CDLL | None = None):
        self._dll = dll or load_library()
        self._dev = ctypes.c_void_p()
        self._index = index
        self._closed = True
        self._gain_mode_manual = True
        self._gain_tenths_db = 0
        self._bias_tee = False
        self._async_cb = None

        count = self.device_count(self._dll)
        if count == 0:
            raise RtlSdrError(
                "No RTL-SDR device found. Check that the dongle is plugged "
                "in and that the WinUSB/Zadig driver is installed for it.")
        if index >= count:
            raise RtlSdrError(
                f"RTL-SDR device index {index} requested but only {count} "
                f"device(s) are present.")

        result = self._dll.rtlsdr_open(ctypes.byref(self._dev), index)
        if result != 0:
            raise RtlSdrError(
                f"rtlsdr_open(index={index}) failed with error {result}. "
                "The device may be in use by another program (SDR#, "
                "rtl_tcp, another copy of this tuner).")
        self._closed = False

        # Manual gain at 0.0 dB, digital AGC off — before any streaming.
        self.set_gain_mode(manual=True)
        self.set_gain_db(0.0)
        self.set_agc_mode(False)

    # -- class helpers -----------------------------------------------------

    @staticmethod
    def device_count(dll: ctypes.CDLL | None = None) -> int:
        dll = dll or load_library()
        return int(dll.rtlsdr_get_device_count())

    @staticmethod
    def device_names(dll: ctypes.CDLL | None = None) -> list[str]:
        dll = dll or load_library()
        names = []
        for i in range(int(dll.rtlsdr_get_device_count())):
            raw = dll.rtlsdr_get_device_name(i)
            names.append(raw.decode("utf-8", "replace") if raw else f"Device {i}")
        return names

    # -- properties --------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def index(self) -> int:
        return self._index

    @property
    def gain_mode_manual(self) -> bool:
        return self._gain_mode_manual

    @property
    def gain_db(self) -> float:
        return self._gain_tenths_db / 10.0

    @property
    def bias_tee(self) -> bool:
        return self._bias_tee

    def bias_tee_supported(self) -> bool:
        """True if the loaded DLL exposes bias-tee control."""
        return hasattr(self._dll, "rtlsdr_set_bias_tee") or hasattr(
            self._dll, "rtlsdr_set_bias_tee_gpio")

    def tuner_type(self) -> str:
        try:
            return TUNER_TYPES.get(int(self._dll.rtlsdr_get_tuner_type(self._dev)),
                                   "Unknown")
        except AttributeError:
            return "Unknown"

    def name(self) -> str:
        try:
            raw = self._dll.rtlsdr_get_device_name(self._index)
        except AttributeError:
            return f"RTL-SDR #{self._index}"
        return raw.decode("utf-8", "replace") if raw else f"RTL-SDR #{self._index}"

    def usb_label(self) -> str:
        """The dongle as its USB strings name it, e.g. "RTLSDRBlog Blog V4
        SN 00000001" (the generic chip name when they can't be read)."""
        try:
            bufs = [ctypes.create_string_buffer(256) for _ in range(3)]
            if self._dll.rtlsdr_get_device_usb_strings(self._index, *bufs) == 0:
                maker, product, serial = (
                    b.value.decode("utf-8", "replace").strip() for b in bufs)
                label = " ".join(x for x in (maker, product) if x)
                if label:
                    return label + (f" SN {serial}" if serial else "")
        except (AttributeError, TypeError, ValueError, OSError):
            pass
        return self.name()

    # -- configuration -----------------------------------------------------

    def _check(self, result: int, what: str) -> None:
        if result != 0:
            raise RtlSdrError(f"{what} failed with error {result}")

    def set_sample_rate(self, rate_hz: int) -> None:
        self._check(self._dll.rtlsdr_set_sample_rate(self._dev, int(rate_hz)),
                    f"rtlsdr_set_sample_rate({rate_hz})")

    def get_sample_rate(self) -> int:
        return int(self._dll.rtlsdr_get_sample_rate(self._dev))

    def set_center_freq(self, freq_hz: float) -> None:
        self._check(self._dll.rtlsdr_set_center_freq(self._dev, int(freq_hz)),
                    f"rtlsdr_set_center_freq({int(freq_hz)})")

    def get_center_freq(self) -> int:
        return int(self._dll.rtlsdr_get_center_freq(self._dev))

    def set_freq_correction(self, ppm: int) -> None:
        # Error -2 means "value already set"; treat it as success.
        result = self._dll.rtlsdr_set_freq_correction(self._dev, int(ppm))
        if result not in (0, -2):
            raise RtlSdrError(f"rtlsdr_set_freq_correction({ppm}) failed "
                              f"with error {result}")

    def set_gain_mode(self, manual: bool) -> None:
        self._check(self._dll.rtlsdr_set_tuner_gain_mode(self._dev,
                                                         1 if manual else 0),
                    f"rtlsdr_set_tuner_gain_mode({'manual' if manual else 'auto'})")
        self._gain_mode_manual = bool(manual)

    def set_agc_mode(self, enabled: bool) -> None:
        try:
            self._dll.rtlsdr_set_agc_mode(self._dev, 1 if enabled else 0)
        except AttributeError:
            pass

    def set_bias_tee(self, enabled: bool, gpio: int = 0) -> None:
        """Switch the 4.5 V bias-tee on the antenna port on or off."""
        enabled = bool(enabled)
        value = 1 if enabled else 0
        try:
            func = self._dll.rtlsdr_set_bias_tee
            result = func(self._dev, value)
        except AttributeError:
            try:
                result = self._dll.rtlsdr_set_bias_tee_gpio(self._dev,
                                                            int(gpio), value)
            except AttributeError:
                raise RtlSdrError(
                    "This rtlsdr.dll has no bias-tee support. Use the "
                    "rtl-sdr-blog build of rtlsdr.dll (x64) from "
                    "https://github.com/rtlsdrblog/rtl-sdr-blog/releases"
                ) from None
        self._check(result, f"rtlsdr_set_bias_tee({'on' if enabled else 'off'})")
        self._bias_tee = enabled

    def supported_gains_db(self) -> list[float]:
        """Tuner gain steps in dB, or [] if the tuner does not report them."""
        try:
            count = int(self._dll.rtlsdr_get_tuner_gains(self._dev, None))
        except (AttributeError, ctypes.ArgumentError):
            return []
        if count <= 0:
            return []
        buf = (ctypes.c_int * count)()
        got = int(self._dll.rtlsdr_get_tuner_gains(self._dev, buf))
        return [buf[i] / 10.0 for i in range(max(0, min(got, count)))]

    def set_gain_db(self, gain_db: float) -> float:
        """Set manual tuner gain in dB. Returns the value the tuner accepted."""
        if not self._gain_mode_manual:
            self.set_gain_mode(manual=True)
        tenths = int(round(float(gain_db) * 10))
        self._check(self._dll.rtlsdr_set_tuner_gain(self._dev, tenths),
                    f"rtlsdr_set_tuner_gain({tenths / 10.0:.1f} dB)")
        self._gain_tenths_db = tenths
        try:
            actual = int(self._dll.rtlsdr_get_tuner_gain(self._dev))
            if actual:
                self._gain_tenths_db = actual
        except AttributeError:
            pass
        return self._gain_tenths_db / 10.0

    def get_gain_db(self) -> float:
        try:
            return int(self._dll.rtlsdr_get_tuner_gain(self._dev)) / 10.0
        except AttributeError:
            return self._gain_tenths_db / 10.0

    # -- streaming ---------------------------------------------------------

    def reset_buffer(self) -> None:
        try:
            self._dll.rtlsdr_reset_buffer(self._dev)
        except AttributeError:
            pass

    def read_sync(self, buffer, length: int) -> int:
        """Read raw interleaved uint8 IQ into `buffer`. Returns bytes read."""
        n_read = ctypes.c_int(0)
        result = self._dll.rtlsdr_read_sync(self._dev, buffer, length,
                                            ctypes.byref(n_read))
        if result != 0:
            raise RtlSdrError(
                f"rtlsdr_read_sync failed with error {result}. The dongle may "
                "have been unplugged.")
        return n_read.value

    def async_supported(self) -> bool:
        """True if the DLL can stream continuously via rtlsdr_read_async."""
        return hasattr(self._dll, "rtlsdr_read_async") and hasattr(
            self._dll, "rtlsdr_cancel_async")

    def read_async(self, on_samples, buffer_count: int = 16,
                   buffer_length: int = 262_144) -> None:
        """Stream IQ continuously, calling `on_samples(bytes)` per buffer.

        Blocks until `cancel_async()` is called. Unlike repeated read_sync
        calls, libusb keeps `buffer_count` transfers queued, so samples are
        not lost while the caller is busy demodulating.
        """
        def trampoline(buf, length, _ctx):
            try:
                on_samples(ctypes.string_at(buf, length))
            except Exception:
                pass  # never raise back through libusb's callback

        self._async_cb = READ_ASYNC_CB(trampoline)
        result = self._dll.rtlsdr_read_async(self._dev, self._async_cb, None,
                                             int(buffer_count),
                                             int(buffer_length))
        if result != 0:
            raise RtlSdrError(
                f"rtlsdr_read_async failed with error {result}. The dongle "
                "may have been unplugged.")

    def cancel_async(self) -> None:
        """Stop a running read_async so its thread can return."""
        try:
            self._dll.rtlsdr_cancel_async(self._dev)
        except AttributeError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel_async()
        if self._bias_tee:
            # Never leave 4.5 V on the antenna port after the device is released.
            try:
                self.set_bias_tee(False)
            except Exception:
                pass
        try:
            self._dll.rtlsdr_close(self._dev)
        except Exception:
            pass
        self._dev = ctypes.c_void_p()

    def __enter__(self) -> "RtlSdrDevice":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
