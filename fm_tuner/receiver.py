"""FM receiver engine: RTL-SDR capture, FM demodulation, audio and RDS.

One worker thread owns the dongle. It reads IQ, demodulates to the 240 kHz
MPX composite, and fans the MPX out to:

  * the audio thread (stereo decode, 75 us de-emphasis, WASAPI/DirectSound
    output through sounddevice), and
  * the RDS decoder process (`fm_tuner.rds_decoder`), whose groups are
    published to the ASCII G servers and to the live RDS state.

Tuning, gain, seek and stop are requested from any thread; the worker
applies them between reads so the device is only ever touched from one
thread.
"""

from __future__ import annotations

import functools
import math
import multiprocessing
import queue
import threading
import time
import wave
from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np

from . import band
from . import dx
from . import hd_radio
from .rds_decoder import (RDS2_BACKLOG_LIMIT, RDS_RESET, rds2_streams_fit,
                          rds_worker_process)
from .rds_state import LiveRDSState
from .sca import SCA_FREQUENCIES_HZ, SCADecoder
from .rtlsdr_device import RtlSdrDevice, RtlSdrError
from . import soapy_device
from .soapy_device import SoapyDevice

IQ_RATE = 960_000       # RTL-SDR sample rate
MPX_RATE = 240_000      # after 4x decimation — keeps the 57 kHz RDS carrier
AUDIO_RATE = 48_000     # after a further 5x decimation
READ_BYTES = 131_072    # ~68 ms of IQ per read

CHANNEL_CUTOFF_HZ = 120_000.0   # channel filter edge: -6 dB point
PILOT_HZ = 19_000.0
MPX_WAV_FULL_SCALE = 0.9        # +-75 kHz deviation -> this fraction of int16
MPX_WAV_SCALE = (32767.0 * MPX_WAV_FULL_SCALE
                 / (2.0 * math.pi * 75_000.0 / 240_000.0))
AUDIO_LP_STOP_DB = 70.0        # audio low-pass rejection of the 19 kHz pilot
PILOT_STEREO_THRESHOLD = 0.06   # pilot/MPX amplitude ratio for stereo lock
SEEK_MIN_DBFS = -34.0           # RF level a channel must exceed
SEEK_DWELL_READS = 3            # reads discarded after each seek retune
SEEK_ABOVE_FLOOR_DB = 10.0      # or this far above the quietest channel seen
SEEK_HD_WAIT_S = 8.0            # HD seek: time allowed for an HD lock

SPECTRUM_BINS = 256             # spectrum/waterfall resolution
SPECTRUM_FFT = 2048
SPECTRUM_SPAN_HZ = 400_000.0    # +/-200 kHz: analog channel plus IBOC skirts
# HD Radio (IBOC) digital sidebands sit either side of the analog carrier.

# Baseband (MPX) analyser: mono sum, 19 kHz pilot, 38 kHz stereo subcarrier,
# 57 kHz RDS subcarrier and any SCA/DARC carriers above it.
MPX_BINS = 256
MPX_FFT = 4096
MPX_SPAN_HZ = 100_000.0
MPX_CARRIERS = {
    "pilot": 19_000.0,
    "stereo": 38_000.0,
    "rds": 57_000.0,
    "rds2_1": 66_500.0,
    "sca": 67_000.0,
    "rds2_2": 71_250.0,
    "rds2_3": 76_000.0,
    "sca92": 92_000.0,
}
AUDIO_SOURCES = ("main",) + tuple(SCA_FREQUENCIES_HZ)
MPX_CARRIER_WIDTH_HZ = 1_500.0   # half-width summed around each carrier


class ReceiverState:
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    SEEKING = "seeking"
    ERROR = "error"


@dataclass
class Metrics:
    """Measured receiver values. `None` means "not available", never faked."""

    signal_dbfs: float | None = None
    signal_rms: float | None = None
    pilot_ratio: float | None = None
    stereo: bool | None = None
    clipping: float | None = None
    mpx_deviation: float | None = None
    mpx_pilot_db: float | None = None
    mpx_stereo_db: float | None = None
    mpx_rds_db: float | None = None
    mpx_sca_db: float | None = None
    mpx_sca92_db: float | None = None
    mpx_rds2_db: list | None = None     # 66.5 / 71.25 / 76 kHz carrier levels
    sca_level_db: float | None = None    # decoded SCA subcarrier, when selected
    sca_present: bool | None = None
    reads: int = 0
    audio_underruns: int = 0
    rds_dropped: int = 0                 # MPX chunks the RDS worker never saw
    rds2_skipped: int = 0                # chunks where RDS2 decoding was skipped
    rds_level: float | None = None
    rds_snr_db: float | None = None
    rds_block_hits: float | None = None
    rds_bits: int = 0
    rds2_levels: list | None = None      # streams 1-3 subcarrier levels
    rds2_block_hits: list | None = None
    rds2_groups: int = 0
    wide_dbfs: float | None = None       # whole 960 kHz before channel filter
    neighbour_low_db: float | None = None
    neighbour_high_db: float | None = None
    dx_active_side: str | None = None
    dx_passband_hz: tuple | None = None
    dx_reduction_db: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "signal_dbfs": _round(self.signal_dbfs, 1),
                "signal_rms": _round(self.signal_rms, 2),
                "pilot_ratio": _round(self.pilot_ratio, 4),
                "stereo": self.stereo,
                "clipping": _round(self.clipping, 4),
                "mpx_deviation": _round(self.mpx_deviation, 4),
                "mpx_pilot_db": _round(self.mpx_pilot_db, 1),
                "mpx_stereo_db": _round(self.mpx_stereo_db, 1),
                "mpx_rds_db": _round(self.mpx_rds_db, 1),
                "mpx_sca_db": _round(self.mpx_sca_db, 1),
                "mpx_sca92_db": _round(self.mpx_sca92_db, 1),
                "mpx_rds2_db": ([_round(v, 1) for v in self.mpx_rds2_db]
                                if self.mpx_rds2_db else None),
                "sca_level_db": _round(self.sca_level_db, 1),
                "sca_present": self.sca_present,
                "reads": self.reads,
                "audio_underruns": self.audio_underruns,
                "rds_dropped": self.rds_dropped,
                "rds2_skipped": self.rds2_skipped,
                "rds_level": _round(self.rds_level, 4),
                "rds_snr_db": _round(self.rds_snr_db, 1),
                "rds_block_hits": self.rds_block_hits,
                "rds_bits": self.rds_bits,
                "rds2_levels": ([_round(v, 4) for v in self.rds2_levels]
                                if self.rds2_levels else None),
                "rds2_block_hits": self.rds2_block_hits,
                "rds2_groups": self.rds2_groups,
                "wide_dbfs": _round(self.wide_dbfs, 1),
                "neighbour_low_db": _round(self.neighbour_low_db, 1),
                "neighbour_high_db": _round(self.neighbour_high_db, 1),
                "dx_active_side": self.dx_active_side,
                "dx_passband_hz": (list(self.dx_passband_hz)
                                   if self.dx_passband_hz else None),
                "dx_reduction_db": _round(self.dx_reduction_db, 1),
            }


def _round(value, digits):
    return None if value is None else round(float(value), digits)


class FMReceiver:
    def __init__(self, device_index: int = 0,
                 on_groups=None,
                 on_event=None,
                 ppm: int = 0,
                 on_rds2_groups=None,
                 rds2: bool = False):
        self.device_index = device_index
        # "" or "rtl:N" = RTL-SDR number N; "soapy:driver=hackrf,..." = a
        # SoapySDR radio. Set with set_radio() while stopped.
        self.radio = ""
        self._on_groups = on_groups          # called with [(b1,b2,b3,b4), ...]
        self._on_rds2_groups = on_rds2_groups  # [(stream, b1,b2,b3,b4), ...]
        # Shared with the decoder process so RDS2 can be toggled live.
        self._rds2_flag = multiprocessing.Value("b", 1 if rds2 else 0)
        self._last_rds2_group_time = 0.0
        self._on_event = on_event            # called with (kind, message)
        self.ppm = ppm

        self.rds = LiveRDSState()
        self.metrics = Metrics()

        self._device: RtlSdrDevice | None = None
        self._dongle: str | None = None
        self._state = ReceiverState.STOPPED
        self._error: str | None = None
        self._lock = threading.RLock()

        self._frequency = 101.1
        self._pending_freq: float | None = None
        self._pending_gain: float | None = None
        self._pending_gain_mode: bool | None = None
        self._gain_db = 0.0
        self._gain_manual = True
        self._agc_gain_db: float | None = None   # gain reported while auto
        self._agc_poll_at = 0.0
        self._supported_gains: list[float] = []
        self._bias_tee = False
        self._pending_bias_tee: bool | None = None
        self._bias_tee_supported: bool | None = None

        self._seek_dir = 0
        self._seek_start_freq: float | None = None
        self._seek_result: str | None = None
        self._seek_hd = False
        self._seek_fresh = False
        self._seek_floor: float | None = None
        self._seek_hd_until: float | None = None

        self._stop_flag = threading.Event()
        self._rx_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._iq_queue: queue.Queue = queue.Queue(maxsize=8)
        self._async_active = False
        self._mpx_recorder = None      # open file while capturing MPX
        self._mpx_record_left = 0      # samples still to capture
        self._mpx_record_path = None
        self._mpx_record_total = 0
        self._mpx_record_fmt = "wav"
        self._mpx_record_done = None   # path of the last finished capture
        self._dropped_buffers = 0

        self.audio_enabled = True
        self.stereo_mode = True
        self.audio_source = "main"     # "main" | "sca67" | "sca92"
        self.volume = 1.0
        self.hd_mode = "auto"          # see hd_radio.HD_MODES
        self.hd_program: int | None = 0   # 0 = HD1; None = analog (Manual)
        self.hd_delay_s = hd_radio.HD_DELAY_DEFAULT_S
        self.hd_source = "analog"      # what is playing: analog|hd|silence
        self._hd: hd_radio.HDDecoder | None = None
        self._hd_gen = 0               # bumped on retune / programme change
        self._audio_device: int | None = None
        self._audio_queue: queue.Queue | None = None
        # Remote listeners (fm_tuner.remote.AudioHub), set by the service.
        self.audio_hub = None
        # False = silent on this PC while remote listeners still get audio.
        self.local_playback = True
        self._audio_thread: threading.Thread | None = None
        self._audio_gen = 0
        self._audio_ready = False
        self._audio_rate: int | None = None
        self._audio_error: str | None = None

        self._rds_in: multiprocessing.Queue | None = None
        self._rds2_in: list[multiprocessing.Queue] = []
        self._rds_out: multiprocessing.Queue | None = None
        self._rds_stop: multiprocessing.Event | None = None
        self._rds_procs: list[multiprocessing.Process] = []
        self._rds2_dropped = 0               # chunks the RDS2 queues refused
        self._rds2_skip_counts = [0, 0, 0]
        self._rds2_group_totals = [0, 0, 0]
        self._rds_pump: threading.Thread | None = None
        self._last_group_time = 0.0

        self._spectrum_lock = threading.Lock()
        self._spectrum: np.ndarray | None = None      # dB, SPECTRUM_BINS wide
        self._spectrum_offsets = _spectrum_offsets()  # Hz from channel centre
        self._mpx_spectrum: np.ndarray | None = None  # dB, MPX_BINS wide
        self._mpx_freqs = _mpx_frequencies()          # Hz, baseband

        self._tuned_at = 0.0

        # DX / interference settings, read by the DSP loop each buffer.
        self._dx_bandwidth = dx.DEFAULT_BANDWIDTH
        self._dx_side = "off"
        self._dx_canceller = False

    # -- public state ------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def frequency(self) -> float:
        return self._frequency

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def running(self) -> bool:
        return self._state in (ReceiverState.RUNNING, ReceiverState.SEEKING)

    def supported_gains_db(self) -> list[float]:
        return list(self._supported_gains)

    # -- lifecycle ---------------------------------------------------------

    def start(self, frequency_mhz: float | None = None) -> None:
        """Open the dongle and begin receiving. Raises RtlSdrError on failure."""
        with self._lock:
            if self.running:
                if frequency_mhz is not None:
                    self.tune(frequency_mhz)
                return
            if frequency_mhz is not None:
                self._frequency = band.validate(frequency_mhz)

            self._state = ReceiverState.STARTING
            self._error = None
            self._stop_flag.clear()

            try:
                device = self._open_device()
                device.set_sample_rate(IQ_RATE)
                if self.ppm:
                    device.set_freq_correction(self.ppm)
                device.set_center_freq(self._frequency * 1e6)
                device.set_gain_mode(manual=self._gain_manual)
                if self._gain_manual:
                    self._gain_db = device.set_gain_db(self._gain_db)
                self._supported_gains = device.supported_gains_db()
                self._bias_tee_supported = device.bias_tee_supported()
                if self._bias_tee:
                    try:
                        device.set_bias_tee(True)
                    except RtlSdrError as exc:
                        self._bias_tee = False
                        self._emit("warning", str(exc))
                device.reset_buffer()
            except RtlSdrError as exc:
                self._state = ReceiverState.ERROR
                self._error = str(exc)
                raise

            self._device = device
            self._dongle = device.usb_label()
            self._tuned_at = time.time()
            self.rds.reset()
            self._start_rds_process()
            self._start_hd()
            if self.audio_enabled:
                self._start_audio_thread()

            while not self._iq_queue.empty():
                try:
                    self._iq_queue.get_nowait()
                except queue.Empty:
                    break
            self._async_active = device.async_supported()
            if self._async_active:
                self._reader_thread = threading.Thread(
                    target=self._reader_loop, args=(device,),
                    name="fm-usb", daemon=True)
                self._reader_thread.start()
            else:
                self._reader_thread = None
                self._emit("warning",
                           "This rtlsdr.dll has no rtlsdr_read_async; "
                           "falling back to gapped synchronous reads.")

            self._rx_thread = threading.Thread(target=self._rx_loop,
                                               name="fm-rx", daemon=True)
            self._rx_thread.start()
            self._state = ReceiverState.RUNNING
            self._emit("started", f"Receiving {band.fmt(self._frequency)} MHz")

    def _open_device(self):
        if self.radio.startswith(soapy_device.SPEC_PREFIX):
            device = SoapyDevice(self.radio)
            device.on_message = self._emit
            return device
        return RtlSdrDevice(self.device_index)

    def set_radio(self, spec: str | None) -> None:
        """Pick the radio the next start() opens (see `radio`)."""
        spec = (spec or "").strip()
        if spec.startswith("rtl:"):
            try:
                self.device_index = max(0, int(spec[4:]))
            except ValueError:
                spec = ""
        self.radio = spec

    @staticmethod
    def radio_hint(radios: list[tuple[str, str]]) -> str:
        """Why no HackRF/SoapySDR radio is listed, or "" when one is."""
        if any(spec.startswith("soapy:") for spec, _ in radios):
            return ""
        if not soapy_device.available():
            return ("No HackRF listed: SoapySDR isn't installed. Install "
                    "PothosSDR (INSTALL.txt step 4a), then restart the tuner.")
        return ("SoapySDR is installed but found no HackRF. Check the USB "
                "cable and driver (INSTALL.txt step 4a, b), then press Find.")

    @staticmethod
    def radios() -> list[tuple[str, str]]:
        """(setting, label) for every radio plugged in: RTL-SDRs first, then
        SoapySDR radios such as a HackRF when SoapySDR is installed."""
        found = []
        try:
            for i, name in enumerate(RtlSdrDevice.device_names()):
                found.append((f"rtl:{i}", f"RTL-SDR {i + 1}: {name}"))
        except (RtlSdrError, OSError):
            pass
        if soapy_device.available():
            try:
                for kw in soapy_device.enumerate_radios():
                    found.append((soapy_device.spec_for(kw),
                                  soapy_device.label_for(kw)))
            except Exception:
                pass
        return found

    def stop(self) -> None:
        """Stop reception, audio and RDS, and release the dongle."""
        with self._lock:
            if self._state == ReceiverState.STOPPED:
                return
            self._seek_dir = 0
            self._stop_flag.set()
            rx = self._rx_thread
            reader = self._reader_thread
            device = self._device
            self._rx_thread = None
            self._reader_thread = None

        self._async_active = False
        if device is not None:
            device.cancel_async()
        if reader and reader.is_alive():
            reader.join(timeout=3.0)
        if rx and rx.is_alive():
            rx.join(timeout=3.0)

        self._stop_audio_thread()
        self._stop_rds_process()
        self._stop_hd()

        with self._lock:
            if self._device:
                self._device.close()
                self._device = None
            if self._state != ReceiverState.ERROR:
                self._state = ReceiverState.STOPPED
            self._audio_ready = False
        self._emit("stopped", "Receiver stopped")

    # -- tuning ------------------------------------------------------------

    def tune(self, frequency_mhz: float) -> float:
        """Tune to a validated channel. Raises band.FrequencyError if invalid."""
        freq = band.validate(frequency_mhz)
        with self._lock:
            self._seek_dir = 0
            self._frequency = freq
            if self.running:
                self._pending_freq = freq
            else:
                self._tuned_at = time.time()
                self.rds.reset()
        self._emit("tuned", f"{band.fmt(freq)} MHz")
        return freq

    def step(self, direction: int) -> float:
        """Move one channel (region step) up (+1) or down (-1), wrapping the band."""
        return self.tune(band.step(self._frequency, direction))

    def seek(self, direction: int, hd: bool = False) -> None:
        """Start a background seek up (+1) or down (-1). With hd=True it
        only stops on a station where the HD decoder locks."""
        with self._lock:
            if not self.running:
                raise RtlSdrError("Start the receiver before seeking.")
            if hd and self._hd is None:
                raise RtlSdrError("HD Seek needs the HD decoder. Add it with "
                                  "Add HD decoder\u2026 in the Audio panel.")
            self._seek_dir = 1 if direction > 0 else -1
            self._seek_hd = bool(hd)
            self._seek_fresh = True
            self._seek_floor = None
            self._seek_hd_until = None
            self._seek_start_freq = self._frequency
            self._seek_result = None
            self._state = ReceiverState.SEEKING
        what = "HD seeking" if hd else "Seeking"
        self._emit("seek", f"{what} {'up' if direction > 0 else 'down'}")

    def stop_seek(self) -> None:
        """Stop an in-progress seek and stay on the current channel."""
        with self._lock:
            if self._seek_dir:
                self._seek_dir = 0
                self._seek_result = "stopped"
                if self._state == ReceiverState.SEEKING:
                    self._state = ReceiverState.RUNNING
        self._emit("seek_stop", "Seek stopped")

    # -- gain --------------------------------------------------------------

    def set_gain_db(self, gain_db: float) -> None:
        with self._lock:
            self._gain_manual = True
            self._gain_db = float(gain_db)
            self._pending_gain_mode = True
            self._pending_gain = float(gain_db)
            if not self.running and self._device:
                self._gain_db = self._device.set_gain_db(gain_db)

    def set_gain_mode(self, manual: bool) -> None:
        with self._lock:
            self._gain_manual = bool(manual)
            self._pending_gain_mode = bool(manual)
            if manual:
                self._pending_gain = self._gain_db

    @property
    def gain_db(self) -> float:
        return self._gain_db

    @property
    def gain_manual(self) -> bool:
        return self._gain_manual

    # -- bias tee ----------------------------------------------------------

    @property
    def rds2_enabled(self) -> bool:
        return bool(self._rds2_flag.value)

    def set_rds2(self, enabled: bool) -> None:
        self._rds2_flag.value = 1 if enabled else 0
        if not enabled:
            with self.metrics.lock:
                self.metrics.rds2_levels = None
                self.metrics.rds2_block_hits = None
        self._emit("info", f"RDS2 streams {'on' if enabled else 'off'}")

    # -- DX / interference ---------------------------------------------

    def set_dx(self, bandwidth: str | None = None, side: str | None = None,
               canceller: bool | None = None) -> None:
        """Channel bandwidth in kHz (a dx.BANDWIDTHS_HZ key), which strong
        neighbour to protect against ('off', 'auto', 'low', 'high') and
        whether the adjacent-channel canceller runs."""
        if bandwidth is not None:
            self._dx_bandwidth = dx.normalize_bandwidth(bandwidth)
        if side is not None:
            if side not in dx.SIDES:
                raise ValueError(f"side must be one of {', '.join(dx.SIDES)}")
            self._dx_side = side
        if canceller is not None:
            self._dx_canceller = bool(canceller)
        self._emit("info", f"DX filter {self._dx_bandwidth} kHz, protect "
                           f"{self._dx_side}, canceller "
                           f"{'on' if self._dx_canceller else 'off'}")

    @property
    def dx_settings(self) -> dict:
        return {"bandwidth": self._dx_bandwidth, "side": self._dx_side,
                "canceller": self._dx_canceller}

    def set_bias_tee(self, enabled: bool) -> None:
        """Switch the dongle's 4.5 V antenna-port bias-tee on or off."""
        enabled = bool(enabled)
        with self._lock:
            self._bias_tee = enabled
            if self.running:
                self._pending_bias_tee = enabled
                return
            device = self._device
        if device is not None:
            try:
                device.set_bias_tee(enabled)
            except RtlSdrError:
                self._bias_tee = False
                raise
        self._emit("bias_tee", f"Bias-T {'on' if enabled else 'off'}")

    @property
    def bias_tee(self) -> bool:
        return self._bias_tee

    @property
    def bias_tee_supported(self) -> bool | None:
        return self._bias_tee_supported

    # -- audio -------------------------------------------------------------

    @staticmethod
    def audio_devices() -> list[dict]:
        """Windows output devices as {'index', 'name', 'default'} dicts."""
        try:
            import sounddevice as sd
        except Exception:
            return []
        try:
            default_out = sd.default.device[1]
        except Exception:
            default_out = None
        devices = []
        for idx, info in enumerate(sd.query_devices()):
            if info.get("max_output_channels", 0) <= 0:
                continue
            try:
                api = sd.query_hostapis(info["hostapi"])["name"]
            except Exception:
                api = ""
            devices.append({
                "index": idx,
                "name": f"{info['name']} ({api})" if api else info["name"],
                "default": idx == default_out,
            })
        return devices

    def set_audio_device(self, index: int | None) -> None:
        self._audio_device = index
        if self.audio_enabled and self.running:
            self._start_audio_thread()

    def set_audio_enabled(self, enabled: bool) -> None:
        self.audio_enabled = bool(enabled)
        if not enabled:
            self._stop_audio_thread()
        elif self.running:
            self._start_audio_thread()

    def set_stereo(self, stereo: bool) -> None:
        if bool(stereo) == self.stereo_mode:
            return
        self.stereo_mode = bool(stereo)
        if self.audio_enabled and self.running:
            self._start_audio_thread()

    def set_audio_source(self, source: str) -> None:
        """Play the main programme or an SCA subcarrier ("sca67"/"sca92")."""
        source = str(source).lower()
        if source not in AUDIO_SOURCES:
            raise ValueError(f"audio source must be one of {AUDIO_SOURCES}")
        if source == self.audio_source:
            return
        self.audio_source = source
        with self.metrics.lock:
            self.metrics.sca_level_db = None
            self.metrics.sca_present = None
        self._emit("audio", "Audio source: " + (
            "main programme" if source == "main"
            else f"SCA {SCA_FREQUENCIES_HZ[source] / 1000:g} kHz"))

    def set_hd_mode(self, mode: str) -> None:
        if mode not in hd_radio.HD_MODES:
            raise ValueError("HD mode must be " + "|".join(hd_radio.HD_MODES))
        self.hd_mode = mode
        self._sync_hd_program()

    def set_hd_program(self, program: int | None) -> None:
        """0-7 for HD1-HD8, or None for analog."""
        if program is not None and not 0 <= int(program) < hd_radio.HD_PROGRAMS:
            raise ValueError("HD programme must be 1-8")
        self.hd_program = None if program is None else int(program)
        self._sync_hd_program()

    def set_hd_delay(self, seconds: float) -> None:
        self.hd_delay_s = min(max(float(seconds), 0.0),
                              hd_radio.HD_DELAY_MAX_S)
        self._hd_gen += 1

    def reload_hd(self) -> None:
        """Pick up a newly added HD decoder without restarting."""
        self._stop_hd()
        if self.running:
            self._start_hd()

    def hd_status(self) -> dict:
        hd = self._hd
        if hd is None:
            found = hd_radio.find_addon() is not None
            state = {"installed": False, "synced": False,
                     "error": None if found else "HD decoder not installed",
                     "programs": []}
        else:
            state = hd.snapshot()
        state.update(mode=self.hd_mode, program=self.hd_program,
                     delay_s=self.hd_delay_s, playing=self.hd_source)
        return state

    def _hd_audio_program(self) -> int | None:
        if self.hd_mode == "analog":
            return None
        if self.hd_mode == "auto":
            return self.hd_program if self.hd_program is not None else 0
        return self.hd_program

    def _sync_hd_program(self) -> None:
        if self._hd is not None:
            program = self._hd_audio_program()
            if program != self._hd.audio_program:
                self._hd.set_audio_program(program)
                self._hd_gen += 1

    def _start_hd(self) -> None:
        if self._hd is not None:
            return
        decoder = hd_radio.HDDecoder(IQ_RATE)
        if not decoder.available:
            if decoder.error and "not installed" not in decoder.error:
                self._emit("warning", decoder.error)
            return
        self._hd = decoder
        self._sync_hd_program()
        self._emit("hd", "HD decoder ready")

    def _stop_hd(self) -> None:
        decoder, self._hd = self._hd, None
        if decoder is not None:
            decoder.close()
        self.hd_source = "analog"

    def _blend_hd(self, chunk: np.ndarray, channels: int, out_rate: int,
                  state: dict) -> np.ndarray:
        """Run one block of analog audio through the HD blender."""
        decoder = self._hd
        if decoder is None or self.hd_mode == "analog":
            state.clear()
            self.hd_source = "analog"
            return chunk
        if state.get("rate") != out_rate or "blender" not in state:
            state.update(rate=out_rate, gen=None,
                         blender=hd_radio.AudioBlender(out_rate,
                                                       self.hd_delay_s),
                         resampler=hd_radio.StreamResampler(
                             hd_radio.HD_AUDIO_RATE, out_rate, 20_000.0))
        blender = state["blender"]
        if state["gen"] != self._hd_gen:
            state["gen"] = self._hd_gen
            blender.set_delay(self.hd_delay_s)
            blender.reset()
            state["resampler"].reset()
            decoder.pop_audio()
        frames = chunk.astype(np.float32) / 32767.0
        frames = frames.reshape(-1, 2) if channels == 2 else frames
        blender.push_analog(frames)
        hd = decoder.pop_audio()
        if hd is not None and len(hd):
            both = state["resampler"].process(
                (hd[:, 0] + 1j * hd[:, 1]).astype(np.complex64))
            hd = np.column_stack((both.real, both.imag)) * min(self.volume,
                                                               2.0)
            blender.push_hd(hd)
        n = len(frames)
        out = np.clip(blender.pull(n, self.hd_mode, self.hd_program,
                                   time.time()), -1.0, 1.0)
        self.hd_source = blender.source
        if channels == 2:
            return (out.reshape(-1) * 32767).astype(np.int16)
        return (out.mean(axis=1) * 32767).astype(np.int16)

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(5.0, float(volume)))

    # -- telemetry ---------------------------------------------------------

    def telemetry(self) -> dict:
        """A full receiver snapshot. Unmeasurable values are null."""
        metrics = self.metrics.snapshot()
        rds = self.rds.snapshot()
        hd = self.hd_status()
        rds_ready = bool(rds["pi"]) and (time.time() - self._last_group_time) < 10
        data = {
            "type": "status",
            "timestamp": time.time(),
            "frequency_mhz": round(self._frequency, 3),
            "state": self._state,
            "error": self._error,
            "device": (self._device.name() if self._device else None),
            "tuner_type": (self._device.tuner_type() if self._device else None),
            "dongle": self._dongle if self._device else None,
            "radio": self.radio or f"rtl:{self.device_index}",
            "gain_mode": "manual" if self._gain_manual else "auto",
            "gain_db": (round(self._gain_db, 1) if self._gain_manual
                        else self._agc_gain_db),
            "supported_gains_db": self._supported_gains,
            "bias_tee": self._bias_tee,
            "bias_tee_supported": self._bias_tee_supported,
            "signal_dbfs": metrics["signal_dbfs"],
            "signal_rms": metrics["signal_rms"],
            # The RTL-SDR reports no calibrated noise floor or absolute RSSI,
            # so SNR and dBm are published as unavailable rather than invented.
            "snr_db": None,
            "rssi_dbm": None,
            "clipping": metrics["clipping"],
            "stereo": metrics["stereo"],
            "stereo_mode": "stereo" if self.stereo_mode else "mono",
            "pilot_ratio": metrics["pilot_ratio"],
            "mpx_deviation": metrics["mpx_deviation"],
            "mpx_pilot_db": metrics["mpx_pilot_db"],
            "mpx_stereo_db": metrics["mpx_stereo_db"],
            "mpx_rds_db": metrics["mpx_rds_db"],
            "mpx_sca_db": metrics["mpx_sca_db"],
            "mpx_sca92_db": metrics["mpx_sca92_db"],
            "mpx_rds2_db": metrics["mpx_rds2_db"],
            "audio_source": self.audio_source,
            "sca_level_db": metrics["sca_level_db"],
            "sca_present": metrics["sca_present"],
            "audio_enabled": self.audio_enabled,
            "audio_ready": self._audio_ready,
            "audio_rate": self._audio_rate,
            "audio_device": self._audio_device,
            "audio_error": self._audio_error,
            "audio_underruns": metrics["audio_underruns"],
            "rds_dropped": metrics["rds_dropped"],
            "rds2_skipped": metrics["rds2_skipped"],
            "volume": self.volume,
            "local_playback": self.local_playback,
            "rds_ready": rds_ready,
            "rds_level": metrics["rds_level"],
            "rds_snr_db": metrics["rds_snr_db"],
            "rds_block_hits": metrics["rds_block_hits"],
            "rds_bits": metrics["rds_bits"],
            "rds2_enabled": self.rds2_enabled,
            "rds2_levels": metrics["rds2_levels"],
            "rds2_block_hits": metrics["rds2_block_hits"],
            "rds2_groups": metrics["rds2_groups"],
            "rds2_active": (time.time() - self._last_rds2_group_time) < 10,
            "hd_detected": hd["synced"] if hd["installed"] else None,
            "hd": hd,
            "wide_dbfs": metrics["wide_dbfs"],
            "overload": dx.overload_state(metrics["clipping"],
                                          metrics["wide_dbfs"]),
            "dx": {
                "bandwidth": self._dx_bandwidth,
                "side": self._dx_side,
                "canceller": self._dx_canceller,
                "active_side": metrics["dx_active_side"],
                "passband_hz": metrics["dx_passband_hz"],
                "reduction_db": metrics["dx_reduction_db"],
                "neighbour_low_db": metrics["neighbour_low_db"],
                "neighbour_high_db": metrics["neighbour_high_db"],
            },
            "seeking": bool(self._seek_dir),
            "seek_result": self._seek_result,
            "reads": metrics["reads"],
            "band": band.info(),
        }
        data.update({k: rds[k] for k in
                     ("pi", "ps", "radiotext", "ptyn", "pty", "pty_name",
                      "tp", "ta", "music_speech", "di", "clock_time",
                      "groups_decoded", "block_error_rate")})
        return data

    # -- internals ---------------------------------------------------------

    def _emit(self, kind: str, message: str) -> None:
        if self._on_event:
            try:
                self._on_event(kind, message)
            except Exception:
                pass

    def _poll_agc_gain(self, device: RtlSdrDevice) -> None:
        """In auto mode, read back the gain the tuner AGC is applying."""
        now = time.monotonic()
        if self._gain_manual or now < self._agc_poll_at:
            if self._gain_manual:
                self._agc_gain_db = None
            return
        self._agc_poll_at = now + 0.5
        try:
            value = device.get_gain_db()
        except RtlSdrError:
            return
        self._agc_gain_db = (round(float(value), 1)
                             if isinstance(value, (int, float)) else None)

    def _apply_pending(self, device: RtlSdrDevice) -> bool:
        """Apply queued tune/gain requests. Returns True if retuned."""
        retuned = False
        with self._lock:
            freq = self._pending_freq
            gain = self._pending_gain
            gain_mode = self._pending_gain_mode
            bias_tee = self._pending_bias_tee
            self._pending_freq = None
            self._pending_gain = None
            self._pending_gain_mode = None
            self._pending_bias_tee = None

        if bias_tee is not None:
            try:
                device.set_bias_tee(bias_tee)
                self._emit("bias_tee", f"Bias-T {'on' if bias_tee else 'off'}")
            except RtlSdrError as exc:
                self._bias_tee = False
                self._emit("warning", str(exc))

        if gain_mode is not None:
            try:
                device.set_gain_mode(manual=gain_mode)
                device.set_agc_mode(not gain_mode)
            except RtlSdrError as exc:
                self._emit("warning", str(exc))
        if gain is not None:
            try:
                self._gain_db = device.set_gain_db(gain)
            except RtlSdrError as exc:
                self._emit("warning", str(exc))
        if freq is not None:
            try:
                device.set_center_freq(freq * 1e6)
                if not self._async_active:
                    device.reset_buffer()
                self.rds.reset()
                self._reset_rds_decoders()
                if self._hd is not None:
                    self._hd.reset()
                self._hd_gen += 1
                self._tuned_at = time.time()
                retuned = True
            except RtlSdrError as exc:
                self._emit("warning", str(exc))
        return retuned

    def _reader_loop(self, device: RtlSdrDevice) -> None:
        """Pull IQ from the dongle continuously and hand it to the DSP loop."""
        def on_buffer(data: bytes) -> None:
            if self._stop_flag.is_set():
                device.cancel_async()
                return
            try:
                self._iq_queue.put_nowait(data)
            except queue.Full:
                # The DSP loop is behind; drop the oldest buffer so the
                # newest audio still arrives promptly.
                try:
                    self._iq_queue.get_nowait()
                    self._iq_queue.put_nowait(data)
                except queue.Empty:
                    pass
                self._dropped_buffers += 1
                if self._dropped_buffers == 10:
                    self._emit("warning", "The PC isn't keeping up with the "
                               "radio, so samples are being dropped (choppy "
                               "audio, missing RDS).")

        try:
            device.read_async(on_buffer, buffer_count=16,
                              buffer_length=READ_BYTES)
        except RtlSdrError as exc:
            if not self._stop_flag.is_set():
                self._fail(str(exc))

    def _next_iq_buffer(self, device: RtlSdrDevice, sync_buf) -> bytes | None:
        """Return the next raw IQ buffer, from the stream or a sync read."""
        if self._async_active:
            try:
                return self._iq_queue.get(timeout=0.5)
            except queue.Empty:
                return None
        n_read = device.read_sync(sync_buf, READ_BYTES)
        return bytes(memoryview(sync_buf)[:n_read]) if n_read >= 4 else None

    def _rx_loop(self) -> None:
        device = self._device
        assert device is not None
        sync_buf = (__import__("ctypes").c_ubyte * READ_BYTES)()
        settle_reads = 0
        seek_checked = 0
        decim = IQ_RATE // MPX_RATE
        last_sample = None          # carried so the discriminator is gapless
        channel = None              # built from the DX settings on first read
        channel_cfg = None
        canceller = None
        active_side = None

        try:
            while not self._stop_flag.is_set():
                if self._apply_pending(device):
                    settle_reads = 2
                self._poll_agc_gain(device)

                try:
                    data = self._next_iq_buffer(device, sync_buf)
                except RtlSdrError as exc:
                    self._fail(str(exc))
                    return
                if data is None:
                    continue

                raw = np.frombuffer(data, dtype=np.uint8)
                if raw.size < 4:
                    continue
                i = raw[0::2].astype(np.float32) - 127.5
                q = raw[1::2].astype(np.float32) - 127.5
                iq = i + 1j * q

                clipping = float(np.mean((raw == 0) | (raw == 255)))
                self._update_spectrum(iq)
                wide_rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))

                levels = dx.neighbour_levels(iq, IQ_RATE)
                side_setting = self._dx_side
                if side_setting == "auto":
                    active_side = dx.choose_side(levels, active_side)
                elif side_setting in ("low", "high"):
                    active_side = side_setting
                else:
                    active_side = None
                cfg = (self._dx_bandwidth, active_side, self._dx_canceller)
                if cfg != channel_cfg:
                    channel_cfg = cfg
                    low, high = dx.passband(self._dx_bandwidth,
                                            active_side or "off")
                    channel = dx.BandpassFilter(low, high, IQ_RATE)
                    if self._dx_canceller and active_side:
                        neighbour = (dx.CHANNEL_SPACING_HZ
                                     if active_side == "high"
                                     else -dx.CHANNEL_SPACING_HZ)
                        canceller = dx.AdjacentCanceller(neighbour, low, high,
                                                         IQ_RATE)
                    else:
                        canceller = None
                    last_sample = None
                wide_iq = iq
                if self._hd is not None:
                    self._hd.feed(wide_iq)
                iq = channel.process(wide_iq)
                if canceller is not None:
                    iq = canceller.process(wide_iq, iq)
                # Level of the selected channel only, so the meter and seek
                # do not count neighbouring stations.
                rf_rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))
                # Prepending the previous buffer's last sample keeps the
                # differential discriminator continuous across buffers.
                if last_sample is not None:
                    demod = np.angle(iq * np.conj(
                        np.concatenate(([last_sample], iq[:-1]))))
                else:
                    demod = np.angle(iq[1:] * np.conj(iq[:-1]))
                last_sample = iq[-1]
                # Boxcar-average each group of `decim` samples before
                # decimating, so noise above 120 kHz does not alias onto
                # the pilot, stereo and 57 kHz RDS subcarriers.
                usable = (len(demod) // decim) * decim
                mpx = demod[:usable].reshape(-1, decim).mean(axis=1)
                pilot_ratio = _pilot_ratio(mpx)
                self._update_mpx_spectrum(mpx)

                with self.metrics.lock:
                    self.metrics.signal_rms = rf_rms
                    self.metrics.signal_dbfs = (20 * math.log10(rf_rms / 127.5)
                                                if rf_rms > 0 else None)
                    self.metrics.clipping = clipping
                    self.metrics.wide_dbfs = (20 * math.log10(wide_rms / 127.5)
                                              if wide_rms > 0 else None)
                    self.metrics.neighbour_low_db = levels["low"]
                    self.metrics.neighbour_high_db = levels["high"]
                    self.metrics.dx_active_side = active_side
                    self.metrics.dx_passband_hz = (channel.low_hz,
                                                   channel.high_hz)
                    self.metrics.dx_reduction_db = (
                        canceller.reduction_db if canceller else None)
                    self.metrics.pilot_ratio = pilot_ratio
                    self.metrics.stereo = (pilot_ratio > PILOT_STEREO_THRESHOLD
                                           if pilot_ratio is not None else None)
                    self.metrics.mpx_deviation = float(np.sqrt(np.mean(mpx ** 2)))
                    self.metrics.reads += 1

                if settle_reads > 0:
                    settle_reads -= 1
                    continue

                if self._seek_dir:
                    seek_checked = self._seek_step(device, rf_rms, pilot_ratio,
                                                   seek_checked)
                    settle_reads = SEEK_DWELL_READS
                    continue
                seek_checked = 0

                if self.audio_enabled and self._audio_queue is not None:
                    try:
                        self._audio_queue.put_nowait(mpx.copy())
                    except queue.Full:
                        with self.metrics.lock:
                            self.metrics.audio_underruns += 1
                if self._rds_in is not None:
                    try:
                        self._rds_in.put_nowait(mpx.copy())
                    except Exception:
                        # RDS backlog: drop rather than delay audio
                        with self.metrics.lock:
                            self.metrics.rds_dropped += 1
                if self._rds2_in and self._rds2_flag.value:
                    for q in self._rds2_in:
                        try:
                            q.put_nowait(mpx.copy())
                        except Exception:
                            with self.metrics.lock:
                                self._rds2_dropped += 1
                                self.metrics.rds2_skipped += 1
                if self._mpx_recorder is not None:
                    self._write_mpx_recording(mpx)
        except Exception as exc:  # unexpected DSP/runtime failure
            self._fail(f"Receive loop error: {exc}")

    def _seek_step(self, device: RtlSdrDevice, rf_rms: float,
                   pilot_ratio: float | None, checked: int) -> int:
        """Evaluate the current channel during a seek and advance if empty.

        The channel seek started on is skipped, so Seek always moves on.
        A channel counts as a station when it is well above the quietest
        channel passed so far (works at any gain), or when it is strong and
        carries a stereo pilot. HD seek then waits for an HD lock."""
        if self._seek_fresh:
            self._seek_fresh = False
            return self._seek_advance(device, checked)

        if self._seek_hd_until is not None:
            hd = self._hd
            if hd is not None and hd.synced:
                return self._seek_finish(
                    f"found HD {band.fmt(self._frequency)} MHz",
                    f"HD station found at {band.fmt(self._frequency)} MHz")
            if time.time() < self._seek_hd_until:
                return checked
            self._seek_hd_until = None
            return self._seek_advance(device, checked)

        dbfs = 20 * math.log10(rf_rms / 127.5) if rf_rms > 0 else -99.0
        floor = self._seek_floor
        above_floor = floor is not None and dbfs - floor >= SEEK_ABOVE_FLOOR_DB
        piloted = (dbfs >= SEEK_MIN_DBFS and pilot_ratio is not None
                   and pilot_ratio > PILOT_STEREO_THRESHOLD)
        self._seek_floor = dbfs if floor is None else min(floor, dbfs)
        if above_floor or piloted:
            if not self._seek_hd:
                return self._seek_finish(
                    f"found {band.fmt(self._frequency)} MHz",
                    f"Station found at {band.fmt(self._frequency)} MHz")
            if self._hd is None:
                return self._seek_finish("HD decoder not running",
                                         "HD seek stopped: no HD decoder")
            if self._hd.synced:
                return self._seek_finish(
                    f"found HD {band.fmt(self._frequency)} MHz",
                    f"HD station found at {band.fmt(self._frequency)} MHz")
            self._seek_hd_until = time.time() + SEEK_HD_WAIT_S
            self._emit("seek_progress",
                       f"{band.fmt(self._frequency)} MHz - checking for HD")
            return checked
        return self._seek_advance(device, checked)

    def _seek_finish(self, result: str, message: str) -> int:
        with self._lock:
            self._seek_dir = 0
            self._seek_hd_until = None
            self._seek_result = result
            if self._state == ReceiverState.SEEKING:
                self._state = ReceiverState.RUNNING
        self._emit("seek_done", message)
        return 0

    def _seek_advance(self, device: RtlSdrDevice, checked: int) -> int:
        checked += 1
        if checked > len(band.channels_khz()):
            with self._lock:
                if self._seek_start_freq is not None:
                    self._frequency = self._seek_start_freq
                    self._pending_freq = self._seek_start_freq
            return self._seek_finish(
                "no station found",
                "No HD station found" if self._seek_hd else "No station found")

        with self._lock:
            direction = self._seek_dir
            self._frequency = band.step(self._frequency, direction)
            next_freq = self._frequency
        try:
            device.set_center_freq(next_freq * 1e6)
            if not self._async_active:
                device.reset_buffer()
        except RtlSdrError as exc:
            self._fail(str(exc))
        self._drain_iq_queue()
        self.rds.reset()
        if self._hd is not None:
            self._hd.reset()
        self._hd_gen += 1
        self._tuned_at = time.time()
        self._emit("seek_progress", f"{band.fmt(next_freq)} MHz")
        return checked

    def _drain_iq_queue(self) -> None:
        """Drop IQ captured before a seek retune so it is not measured."""
        try:
            while True:
                self._iq_queue.get_nowait()
        except (queue.Empty, AttributeError):
            pass

    # -- spectrum / HD detection -------------------------------------------

    def _update_spectrum(self, iq: np.ndarray) -> None:
        """Recompute the baseband spectrum."""
        if len(iq) < SPECTRUM_FFT:
            return
        segment = iq[:SPECTRUM_FFT] * np.hanning(SPECTRUM_FFT)
        spectrum = np.fft.fftshift(np.abs(np.fft.fft(segment)))
        power_db = 20 * np.log10(spectrum / (SPECTRUM_FFT * 127.5) + 1e-12)

        bin_hz = IQ_RATE / SPECTRUM_FFT
        half = int(round((SPECTRUM_SPAN_HZ / 2) / bin_hz))
        center = SPECTRUM_FFT // 2
        band_db = power_db[center - half:center + half]
        if band_db.size < SPECTRUM_BINS:
            return
        # Peak-hold decimation to the display width so narrow IBOC skirts
        # survive the downsample.
        group = band_db.size // SPECTRUM_BINS
        trimmed = band_db[:group * SPECTRUM_BINS].reshape(SPECTRUM_BINS, group)
        display = trimmed.max(axis=1)

        with self._spectrum_lock:
            self._spectrum = display

    def spectrum(self) -> tuple[list[float], list[float]] | None:
        """Latest (offsets_hz, power_db) pair for the spectrum/waterfall."""
        with self._spectrum_lock:
            if self._spectrum is None:
                return None
            return self._spectrum_offsets, [float(v) for v in self._spectrum]

    # -- MPX (baseband) analyser -------------------------------------------

    def _update_mpx_spectrum(self, mpx: np.ndarray) -> None:
        """Recompute the demodulated baseband spectrum and carrier levels."""
        if len(mpx) < MPX_FFT:
            return
        segment = mpx[:MPX_FFT] - float(np.mean(mpx[:MPX_FFT]))
        spectrum = np.abs(np.fft.rfft(segment * np.hanning(MPX_FFT)))
        # Normalise so full-scale deviation sits near 0 dB.
        power_db = 20 * np.log10(spectrum / (MPX_FFT / 4) + 1e-12)

        bin_hz = MPX_RATE / MPX_FFT
        top = int(round(MPX_SPAN_HZ / bin_hz))
        if top < MPX_BINS:
            return
        # Bin edges spanning exactly 0..MPX_SPAN_HZ, so the displayed
        # frequency of a carrier matches where it really is; truncating to
        # a whole number of FFT bins per column would stretch the axis.
        edges = np.linspace(0, top, MPX_BINS + 1).astype(int)
        display = np.array([power_db[edges[i]:max(edges[i + 1], edges[i] + 1)].max()
                            for i in range(MPX_BINS)])

        levels = {}
        for name, hz in MPX_CARRIERS.items():
            lo = int(round((hz - MPX_CARRIER_WIDTH_HZ) / bin_hz))
            hi = int(round((hz + MPX_CARRIER_WIDTH_HZ) / bin_hz))
            window = power_db[max(0, lo):hi + 1]
            levels[name] = float(window.max()) if window.size else None

        with self._spectrum_lock:
            self._mpx_spectrum = display
        with self.metrics.lock:
            self.metrics.mpx_pilot_db = levels["pilot"]
            self.metrics.mpx_stereo_db = levels["stereo"]
            self.metrics.mpx_rds_db = levels["rds"]
            self.metrics.mpx_sca_db = levels["sca"]
            self.metrics.mpx_sca92_db = levels["sca92"]
            self.metrics.mpx_rds2_db = [levels["rds2_1"], levels["rds2_2"],
                                        levels["rds2_3"]]

    def mpx_spectrum(self) -> tuple[list[float], list[float]] | None:
        """Latest (frequencies_hz, power_db) pair for the MPX display."""
        with self._spectrum_lock:
            if self._mpx_spectrum is None:
                return None
            return self._mpx_freqs, [float(v) for v in self._mpx_spectrum]

    def _fail(self, message: str) -> None:
        with self._lock:
            self._error = message
            self._state = ReceiverState.ERROR
            self._seek_dir = 0
        self._emit("error", message)

    # -- MPX recording -----------------------------------------------------

    def record_mpx(self, path: str, seconds: float = 10.0,
                   fmt: str = "wav") -> str:
        """Capture the composite MPX to `path` at MPX_RATE, for analysis.

        `fmt` "wav": 16-bit mono PCM, +-75 kHz deviation scaled to
        MPX_WAV_FULL_SCALE of full scale - a true MPX recording that RDS
        Spy, SDR# and audio tools open directly. "f32": raw float32
        radians, exactly what the RDS decoder is fed.
        """
        if fmt not in ("wav", "f32"):
            raise ValueError(f"unknown MPX format {fmt!r}")
        self.stop_mpx_recording()
        if fmt == "wav":
            writer = wave.open(path, "wb")
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(MPX_RATE)
            self._mpx_recorder = writer
        else:
            self._mpx_recorder = open(path, "wb")  # noqa: SIM115
        self._mpx_record_fmt = fmt
        self._mpx_record_left = int(seconds * MPX_RATE)
        self._mpx_record_total = self._mpx_record_left
        self._mpx_record_path = path
        self._mpx_record_done = None
        self._emit("info", f"Recording {seconds:.0f} s of MPX to {path}")
        return path

    def _write_mpx_recording(self, mpx) -> None:
        try:
            take = mpx[:self._mpx_record_left]
            if self._mpx_record_fmt == "wav":
                pcm = np.clip(take * MPX_WAV_SCALE, -32767, 32767)
                self._mpx_recorder.writeframes(
                    pcm.astype(np.int16).tobytes())
            else:
                self._mpx_recorder.write(take.astype(np.float32).tobytes())
            self._mpx_record_left -= len(take)
            if self._mpx_record_left <= 0:
                path = self._mpx_record_path
                self.stop_mpx_recording()
                self._mpx_record_done = path
                self._emit("info", f"MPX recording finished: {path}")
        except Exception as exc:
            self.stop_mpx_recording()
            self._emit("warning", f"MPX recording failed: {exc}")

    def stop_mpx_recording(self) -> None:
        if self._mpx_recorder is not None:
            try:
                self._mpx_recorder.close()
            except Exception:
                pass
        self._mpx_recorder = None
        self._mpx_record_left = 0
        self._mpx_record_path = None

    def recording_mpx(self) -> bool:
        return self._mpx_recorder is not None

    def mpx_recording_progress(self) -> tuple[float, float] | None:
        """(seconds elapsed, seconds total) of the capture in progress."""
        if self._mpx_recorder is None or not self._mpx_record_total:
            return None
        done = self._mpx_record_total - self._mpx_record_left
        return done / MPX_RATE, self._mpx_record_total / MPX_RATE

    def mpx_recording_finished(self) -> str | None:
        """Path of the most recently completed capture, or None."""
        return self._mpx_record_done

    # -- RDS process -------------------------------------------------------

    def _reset_rds_decoders(self) -> None:
        """Tell every decoder process the station changed."""
        for q in [getattr(self, "_rds_in", None)] + list(getattr(self, "_rds2_in", [])):
            if q is None:
                continue
            try:
                q.put_nowait(RDS_RESET)
            except Exception:
                pass

    def _start_rds_process(self) -> None:
        """One decoder process per stream: classic RDS plus RDS2 streams 1-3.

        Each has its own input queue so a slow RDS2 decoder can only lose
        its own chunks, never classic RDS's.
        """
        self._rds_out = multiprocessing.Queue(maxsize=200)
        self._rds_stop = multiprocessing.Event()
        self._rds_in = multiprocessing.Queue(maxsize=60)
        streams = [(0, self._rds_in, None)]
        self._rds2_in = []
        if rds2_streams_fit(MPX_RATE):
            for s in (1, 2, 3):
                q = multiprocessing.Queue(maxsize=RDS2_BACKLOG_LIMIT + 4)
                self._rds2_in.append(q)
                streams.append((s, q, self._rds2_flag))
        self._rds_procs = []
        for stream, q, flag in streams:
            proc = multiprocessing.Process(
                target=rds_worker_process,
                args=(q, self._rds_out, MPX_RATE, self._rds_stop, flag, stream),
                name=f"fm-rds{stream}", daemon=True)
            proc.start()
            self._rds_procs.append(proc)
        self._rds2_dropped = 0
        self._rds2_skip_counts = [0, 0, 0]
        self._rds2_group_totals = [0, 0, 0]
        self._rds_pump = threading.Thread(target=self._pump_rds,
                                          name="fm-rds-pump", daemon=True)
        self._rds_pump.start()

    def _pump_rds(self) -> None:
        while not self._stop_flag.is_set():
            try:
                payload = self._rds_out.get(timeout=0.5)
            except Exception:
                continue
            if not payload:
                continue
            groups = payload.get("groups") or []
            stream = payload.get("stream") or 0
            if stream:
                idx = stream - 1
                with self.metrics.lock:
                    levels = list(self.metrics.rds2_levels or [0.0, 0.0, 0.0])
                    hits = list(self.metrics.rds2_block_hits or [None] * 3)
                    levels[idx] = payload.get("rds_level") or 0.0
                    hits[idx] = payload.get("block_hits")
                    self.metrics.rds2_levels = levels
                    self.metrics.rds2_block_hits = hits
                    self._rds2_group_totals[idx] = payload.get("total_groups") or 0
                    self._rds2_skip_counts[idx] = payload.get("skipped") or 0
                    self.metrics.rds2_groups = sum(self._rds2_group_totals)
                    self.metrics.rds2_skipped = (
                        self._rds2_dropped + sum(self._rds2_skip_counts))
                if groups:
                    self._last_rds2_group_time = time.time()
                    if self._on_rds2_groups:
                        try:
                            self._on_rds2_groups(
                                [(stream,) + tuple(g) for g in groups])
                        except Exception:
                            pass
                continue
            with self.metrics.lock:
                self.metrics.rds_level = payload.get("rds_level")
                self.metrics.rds_snr_db = payload.get("symbol_snr_db")
                self.metrics.rds_block_hits = payload.get("block_hits")
                self.metrics.rds_bits = payload.get("total_bits") or 0
            if not groups:
                continue
            self._last_group_time = time.time()
            self.rds.add_groups(groups)
            if self._on_groups:
                try:
                    self._on_groups(groups)
                except Exception:
                    pass

    def _stop_rds_process(self) -> None:
        if self._rds_stop is not None:
            self._rds_stop.set()
        for proc in self._rds_procs:
            proc.join(timeout=3.0)
            if proc.is_alive():
                proc.terminate()
        self._rds_procs = []
        if self._rds_pump is not None:
            self._rds_pump.join(timeout=2.0)
            self._rds_pump = None
        self._rds_in = self._rds_out = None
        self._rds2_in = []
        self._rds_stop = None

    # -- audio -------------------------------------------------------------

    def _stop_audio_thread(self) -> None:
        self._audio_gen += 1
        thread = self._audio_thread
        self._audio_thread = None
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self._audio_queue = None
        self._audio_ready = False

    def _start_audio_thread(self) -> None:
        self._stop_audio_thread()
        self._audio_gen += 1
        generation = self._audio_gen
        self._audio_queue = queue.Queue(maxsize=30)
        self._audio_error = None
        self._audio_thread = threading.Thread(
            target=self._audio_loop, args=(generation,),
            name="fm-audio", daemon=True)
        self._audio_thread.start()

    def _publish_audio(self, chunk, rate: int, channels: int) -> None:
        hub = self.audio_hub
        if hub is not None:
            try:
                hub.publish(chunk, rate, channels)
            except Exception:
                pass

    def _audio_loop(self, generation: int) -> None:
        try:
            import sounddevice as sd
            from scipy.signal import (butter, firwin, kaiserord, lfilter,
                                      lfilter_zi, resample_poly)
        except Exception as exc:
            self._audio_error = (
                f"Audio unavailable: {exc}. Install with "
                f"'python -m pip install sounddevice scipy'.")
            self._emit("warning", self._audio_error)
            return

        sr = MPX_RATE
        decim = sr // AUDIO_RATE
        # Audio low-pass: flat to 15 kHz, >70 dB down by the 19 kHz pilot so
        # neither the pilot nor the 38 kHz-mixed RDS products reach the
        # output or alias through the 48 kHz decimation.
        taps, beta = kaiserord(AUDIO_LP_STOP_DB, 4000 / (sr / 2))
        lp_b = firwin(taps | 1, 17000 / (sr / 2), window=("kaiser", beta))
        lp_a = [1.0]
        lp_zi = lfilter_zi(lp_b, lp_a)
        lr_zi = lfilter_zi(lp_b, lp_a)
        stereo_ref = StereoReference(sr)
        # 75 us de-emphasis (North America)
        de_b, de_a = butter(1, 2122 / (sr / 2), btype="low")
        de_l_zi = lfilter_zi(de_b, de_a)
        de_r_zi = lfilter_zi(de_b, de_a)

        stereo = self.stereo_mode
        channels = 2 if stereo else 1
        chunk_frames = 2048
        sca_decoders: dict[str, SCADecoder] = {}

        try:
            stream, out_rate, channels = _open_output_stream(
                sd, self._audio_device, channels, chunk_frames)
        except Exception as exc:
            self._audio_error = (
                f"Could not open audio output device "
                f"{self._audio_device if self._audio_device is not None else 'default'}"
                f": {exc}")
            self._emit("warning", self._audio_error)
            if self.audio_hub is None:
                return
            # Keep producing audio for remote listeners with no local device.
            stream, out_rate = None, AUDIO_RATE
        stereo = stereo and channels == 2
        chunk_size = chunk_frames * channels
        resample = None
        if out_rate != AUDIO_RATE:
            ratio = Fraction(int(out_rate), AUDIO_RATE).limit_denominator(1000)
            up, down = ratio.numerator, ratio.denominator
            resample = functools.partial(resample_poly, up=up, down=down)
            self._emit("audio",
                       f"Audio device wants {out_rate} Hz; resampling "
                       f"48000 -> {out_rate}")

        self._audio_ready = stream is not None
        self._audio_rate = out_rate
        write_buffer = np.zeros(0, dtype=np.int16)
        prebuffered = False
        prebuffer_samples = int(out_rate * 0.5) * channels
        running_rms = 0.01
        hd_state: dict = {}

        try:
            while (not self._stop_flag.is_set()
                   and generation == self._audio_gen):
                try:
                    mpx = self._audio_queue.get(timeout=0.5)
                except (queue.Empty, AttributeError):
                    # The IQ reader delivers MPX every ~68 ms; blocking
                    # writes pace the stream, so never pad with silence
                    # here — that would outrun the device and bury the
                    # programme audio.
                    if prebuffered:
                        with self.metrics.lock:
                            self.metrics.audio_underruns += 1
                    continue
                if mpx is None or len(mpx) < 100:
                    continue

                mpx = mpx - np.mean(mpx)
                source = self.audio_source
                if source != "main":
                    decoder = sca_decoders.get(source)
                    if decoder is None:
                        decoder = SCADecoder(sr, SCA_FREQUENCIES_HZ[source])
                        sca_decoders[source] = decoder
                    mono = decoder.process(mpx)
                    with self.metrics.lock:
                        self.metrics.sca_level_db = decoder.level_db
                        self.metrics.sca_present = decoder.present
                    mono = mono[::decim]
                    if resample is not None:
                        mono = resample(mono)
                    batch_rms = max(float(np.sqrt(np.mean(mono ** 2))), 1e-10)
                    running_rms = _track_level(running_rms, batch_rms)
                    gain = _agc_gain(running_rms, self.volume)
                    mono = np.clip(mono * gain, -1.0, 1.0)
                    pcm = (mono * 32767).astype(np.int16)
                    if channels == 2:
                        chunk = np.repeat(pcm, 2)
                    else:
                        chunk = pcm
                    self._publish_audio(chunk, out_rate, channels)
                    write_buffer = np.concatenate([write_buffer, chunk])
                    if not prebuffered:
                        if len(write_buffer) < prebuffer_samples:
                            continue
                        prebuffered = True
                    if stream is None or not self.local_playback:
                        write_buffer = write_buffer[:0]
                    while len(write_buffer) >= chunk_size:
                        stream.write(write_buffer[:chunk_size].tobytes())
                        write_buffer = write_buffer[chunk_size:]
                    continue
                if stereo:
                    mpx, ref_38k, pilot_amp = stereo_ref.process(mpx)
                l_plus_r, lp_zi = lfilter(lp_b, lp_a, mpx, zi=lp_zi)

                if stereo:
                    # Mixing down the subcarrier halves it, hence the x2.
                    product, lr_zi = lfilter(lp_b, lp_a, mpx * ref_38k,
                                             zi=lr_zi)
                    mpx_rms = float(np.sqrt(np.mean(mpx * mpx))) + 1e-12
                    locked = pilot_amp / mpx_rms > PILOT_STEREO_THRESHOLD
                    l_minus_r = 2.0 * product if locked else np.zeros_like(product)
                    left = (l_plus_r + l_minus_r) * 0.5
                    right = (l_plus_r - l_minus_r) * 0.5
                    left, de_l_zi = lfilter(de_b, de_a, left, zi=de_l_zi)
                    right, de_r_zi = lfilter(de_b, de_a, right, zi=de_r_zi)
                    left = left[::decim]
                    right = right[::decim]
                    if resample is not None:
                        left = resample(left)
                        right = resample(right)
                    length = min(len(left), len(right))
                    left, right = left[:length], right[:length]
                    batch_rms = max(float(np.sqrt(np.mean(left ** 2))),
                                    float(np.sqrt(np.mean(right ** 2))), 1e-10)
                    running_rms = _track_level(running_rms, batch_rms)
                    gain = _agc_gain(running_rms, self.volume)
                    left = np.clip(left * gain, -1.0, 1.0)
                    right = np.clip(right * gain, -1.0, 1.0)
                    chunk = np.empty(len(left) * 2, dtype=np.int16)
                    chunk[0::2] = (left * 32767).astype(np.int16)
                    chunk[1::2] = (right * 32767).astype(np.int16)
                else:
                    mono, de_l_zi = lfilter(de_b, de_a, l_plus_r, zi=de_l_zi)
                    mono = mono[::decim]
                    if resample is not None:
                        mono = resample(mono)
                    batch_rms = max(float(np.sqrt(np.mean(mono ** 2))), 1e-10)
                    running_rms = _track_level(running_rms, batch_rms)
                    gain = _agc_gain(running_rms, self.volume)
                    mono = np.clip(mono * gain, -1.0, 1.0)
                    chunk = (mono * 32767).astype(np.int16)

                chunk = self._blend_hd(chunk, channels, out_rate, hd_state)
                self._publish_audio(chunk, out_rate, channels)
                write_buffer = np.concatenate([write_buffer, chunk])
                if not prebuffered:
                    if len(write_buffer) < prebuffer_samples:
                        continue
                    prebuffered = True
                if stream is None or not self.local_playback:
                    write_buffer = write_buffer[:0]
                while len(write_buffer) >= chunk_size:
                    stream.write(write_buffer[:chunk_size].tobytes())
                    write_buffer = write_buffer[chunk_size:]
        except Exception as exc:
            self._audio_error = f"Audio output stopped: {exc}"
            self._emit("warning", self._audio_error)
        finally:
            self._audio_ready = False
            self._audio_rate = None
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


# Audio levelling. A fast AGC follows the programme's own dynamics and
# makes the output pump, so the level is tracked slowly and the gain it
# can apply is bounded.
AGC_TARGET_RMS = 0.15
AGC_ATTACK = 0.05        # per ~68 ms block when the signal gets louder
AGC_RELEASE = 0.01       # slower still when it gets quieter
AGC_MIN_GAIN = 0.5
AGC_MAX_GAIN = 60.0


def _track_level(running_rms: float, batch_rms: float) -> float:
    """Smooth the measured level, rising faster than it falls."""
    alpha = AGC_ATTACK if batch_rms > running_rms else AGC_RELEASE
    return (1.0 - alpha) * running_rms + alpha * batch_rms


def _agc_gain(running_rms: float, volume: float) -> float:
    gain = AGC_TARGET_RMS / max(running_rms, 1e-6)
    return min(max(gain, AGC_MIN_GAIN), AGC_MAX_GAIN) * volume


def _open_output_stream(sd, device, channels: int, chunk_frames: int):
    """Open an output stream, falling back to what the device accepts.

    Windows MME/DirectSound endpoints often refuse 48 kHz or stereo and
    raise "Invalid sample rate", so the device's own default rate, the
    usual 44.1/48 kHz pair, mono and the system default device are tried
    in turn. Returns (stream, samplerate, channels).
    """
    rates: list[int] = [AUDIO_RATE]
    devices: list[int | None] = [device]
    if device is not None:
        devices.append(None)
    for candidate in devices:
        try:
            info = sd.query_devices(candidate, "output")
            default_rate = int(round(float(info["default_samplerate"])))
        except Exception:
            default_rate = 0
        if default_rate and default_rate not in rates:
            rates.append(default_rate)
    for rate in (44_100, 48_000, 32_000, 22_050, 16_000):
        if rate not in rates:
            rates.append(rate)

    last_error: Exception = RuntimeError("no audio device")
    for candidate in devices:
        for count in ([channels, 1] if channels > 1 else [1]):
            for rate in rates:
                for blocksize in (chunk_frames, 0):
                    try:
                        stream = sd.RawOutputStream(
                            samplerate=rate, channels=count, dtype="int16",
                            blocksize=blocksize, device=candidate,
                            latency="high")
                        stream.start()
                        return stream, rate, count
                    except Exception as exc:
                        last_error = exc
    raise last_error


def _spectrum_offsets() -> list[float]:
    """Frequency offset in Hz of each displayed spectrum bin."""
    half = SPECTRUM_SPAN_HZ / 2
    step = SPECTRUM_SPAN_HZ / SPECTRUM_BINS
    return [-half + step * (i + 0.5) for i in range(SPECTRUM_BINS)]


def _mpx_frequencies() -> list[float]:
    """Centre frequency in Hz of each displayed MPX bin."""
    step = MPX_SPAN_HZ / MPX_BINS
    return [step * (i + 0.5) for i in range(MPX_BINS)]


class ChannelFilter:
    """Complex low-pass that selects the tuned channel from the wide IQ.

    The dongle delivers 960 kHz of spectrum, so without this the FM
    discriminator sees every station within +/-480 kHz and the strongest
    one captures the audio - an adjacent 200 kHz neighbour plays on the
    wrong channel. The passband covers the +/-100 kHz analogue deviation
    (plus a margin for dongle clock error) and is well down by 200 kHz.
    """

    def __init__(self, sample_rate: float = IQ_RATE, taps: int = 95):
        from scipy.signal import firwin

        nyq = sample_rate / 2.0
        self.taps = firwin(taps, CHANNEL_CUTOFF_HZ / nyq,
                           window=("kaiser", 6.0)).astype(np.float32)
        self._state = np.zeros(taps - 1, dtype=np.complex64)

    def process(self, iq: np.ndarray) -> np.ndarray:
        from scipy.signal import lfilter

        out, self._state = lfilter(self.taps, [1.0], iq, zi=self._state)
        return out


class StereoReference:
    """Pilot-locked 38 kHz reference for recovering the L-R subcarrier.

    The 19 kHz pilot is isolated with a linear-phase FIR, so the multiplex
    can be delayed by the same group delay and stay phase-aligned with it.
    A quarter-cycle fractional delay of the pilot supplies its quadrature
    component, and sin(2t) = 2 sin(t) cos(t) then yields a unit-amplitude,
    DC-free reference at 38 kHz. Filter state is carried between chunks so
    there is no discontinuity at buffer boundaries.
    """

    def __init__(self, sample_rate: float = MPX_RATE, taps: int = 129):
        from scipy.signal import firwin

        nyq = sample_rate / 2.0
        self.taps = firwin(taps, [18_500 / nyq, 19_500 / nyq],
                           pass_zero=False)
        self.delay = (taps - 1) // 2
        self.quarter = sample_rate / (4.0 * PILOT_HZ)
        self._bp_state = np.zeros(taps - 1)
        self._mpx_tail = np.zeros(self.delay)
        self._pilot_tail = np.zeros(int(np.ceil(self.quarter)) + 2)
        self._amp_sq = 0.0

    def process(self, mpx: np.ndarray):
        """Return (aligned mpx, sin(2t) reference, pilot amplitude)."""
        from scipy.signal import lfilter

        pilot, self._bp_state = lfilter(self.taps, [1.0], mpx,
                                        zi=self._bp_state)

        padded = np.concatenate([self._mpx_tail, mpx])
        aligned = padded[:len(mpx)]
        self._mpx_tail = padded[len(mpx):]

        # Fractional (quarter cycle) delay of the pilot: sin(t - pi/2) = -cos(t)
        history = np.concatenate([self._pilot_tail, pilot])
        offset = len(self._pilot_tail) - self.quarter
        idx = np.arange(len(pilot)) + offset
        base = np.floor(idx).astype(int)
        frac = idx - base
        quad = history[base] * (1.0 - frac) + history[base + 1] * frac
        self._pilot_tail = history[-len(self._pilot_tail):]

        # Mean of sin^2 is 1/2, so twice the mean power is the amplitude^2.
        amp_sq = 2.0 * float(np.mean(pilot * pilot))
        self._amp_sq = (0.7 * self._amp_sq + 0.3 * amp_sq
                        if self._amp_sq > 0 else amp_sq)
        amplitude = math.sqrt(max(self._amp_sq, 0.0))
        if self._amp_sq <= 1e-12:
            return aligned, np.zeros_like(aligned), 0.0
        # sin(2t) = 2 sin(t) cos(t), with cos(t) = -quad / amplitude.
        # Normalise by the instantaneous envelope (sin^2 + cos^2) rather
        # than the block average: a pilot carrying amplitude modulation
        # (programme-rate AM from the station's processing or multipath)
        # would otherwise modulate the L-R product and intermodulate the
        # audio. This is a hard limiter on the recovered pilot.
        env_sq = pilot * pilot + quad * quad
        floor = 0.01 * self._amp_sq
        reference = -2.0 * pilot * quad / np.maximum(env_sq, floor)
        return aligned, reference, amplitude


def _pilot_ratio(mpx: np.ndarray) -> float | None:
    """Amplitude of the 19 kHz pilot relative to the whole MPX signal."""
    n = 4096
    if len(mpx) < n:
        return None
    block = mpx[:n] - np.mean(mpx[:n])
    rms = float(np.sqrt(np.mean(block * block))) + 1e-12
    taper = np.hanning(n)
    spectrum = np.abs(np.fft.rfft(block * taper))
    bin_hz = MPX_RATE / n
    center = int(round(PILOT_HZ / bin_hz))
    window = spectrum[max(0, center - 2):center + 3]
    if window.size == 0:
        return None
    # Peak of a windowed sinusoid of amplitude A is A * sum(taper) / 2.
    amplitude = 2.0 * float(np.max(window)) / float(np.sum(taper))
    return amplitude / rms
