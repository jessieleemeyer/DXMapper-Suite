"""Weak-signal (DX) reception next to strong local stations.

Three tools, all working on the wide IQ the dongle delivers:

* a selectable channel (IF) bandwidth from 56 to 230 kHz;
* an asymmetric passband that keeps the full width on the quiet side but
  cuts hard on the side where a strong 200 kHz neighbour sits, so its
  splatter is rejected before the discriminator;
* an adjacent-channel canceller: the neighbour is selected with its own
  filter, hard-limited to regenerate its constant-envelope FM spectrum
  (including the sidebands that fall inside our passband), fitted to the
  channel signal with a complex least-squares gain and subtracted.

None of this beats the physics of one 8-bit dongle: a neighbour that is
40-60 dB stronger will still raise the noise floor through phase noise and
front-end overload, which only an attenuator or notch fixes. The
`overload` readout is there so that case is visible rather than guessed.
"""
from __future__ import annotations

import numpy as np

CHANNEL_SPACING_HZ = 200_000.0

# Selectable channel (IF) bandwidths, keyed by total width in kHz; the value
# is the half-width in Hz (-6 dB point) used for the symmetric passband.
#   56-80    deep DX: weak station 200 kHz from a strong local (mono, thin)
#   110-130  everyday DXing: clean mono, RDS PI/PS still decodes
#   150-180  clear slots: full mono / light stereo, fast RDS
#   200-230  broadcast standard; needed to see HD Radio (IBOC) sidebands
BANDWIDTH_GROUPS = (
    ("deep DX", ("56", "64", "72", "80")),
    ("DX + RDS", ("110", "120", "130")),
    ("clear slot", ("150", "165", "180")),
    ("broadcast / HD", ("200", "220", "230")),
)
BANDWIDTHS_HZ = {key: float(key) * 500.0
                 for _, keys in BANDWIDTH_GROUPS for key in keys}
DEFAULT_BANDWIDTH = "230"
# Names from the first release, kept so old presets/commands still work.
LEGACY_BANDWIDTHS = {"normal": "230", "narrow": "180", "dx": "130"}


def normalize_bandwidth(value) -> str:
    """Accept '120', 120, 120.0, '120k', '120 kHz' or a legacy name and
    return the BANDWIDTHS_HZ key; ValueError if it is not a choice."""
    text = str(value).strip().lower()
    if text in LEGACY_BANDWIDTHS:
        return LEGACY_BANDWIDTHS[text]
    text = text.replace("khz", "").replace("k", "").strip()
    try:
        khz = float(text)
    except ValueError:
        raise ValueError(f"bandwidth must be one of {', '.join(BANDWIDTHS_HZ)}"
                         f" kHz or {', '.join(LEGACY_BANDWIDTHS)}") from None
    for key in BANDWIDTHS_HZ:
        if abs(float(key) - khz) < 0.5:
            return key
    raise ValueError(f"bandwidth {value!r} kHz is not one of "
                     f"{', '.join(BANDWIDTHS_HZ)}")


def bandwidth_group(key: str) -> str:
    for name, keys in BANDWIDTH_GROUPS:
        if key in keys:
            return name
    return ""


# Edge used on the protected side of an asymmetric passband.
ASYMMETRIC_EDGE_HZ = 60_000.0

SIDES = ("off", "auto", "low", "high")

# Auto side selection: a neighbour must exceed the tuned channel by this
# much to be protected against, with hysteresis so the filter does not
# flip on every read.
AUTO_THRESHOLD_DB = 6.0
AUTO_HYSTERESIS_DB = 3.0

OVERLOAD_CLIP_WARN = 0.001      # fraction of ADC samples at the rails
OVERLOAD_CLIP_HARD = 0.01
OVERLOAD_WIDE_DBFS = -6.0       # whole-band level that leaves no headroom


def passband(bandwidth: str, side: str) -> tuple[float, float]:
    """(low, high) passband edges in Hz relative to the tuned channel."""
    try:
        half = BANDWIDTHS_HZ[normalize_bandwidth(bandwidth)]
    except ValueError:
        half = BANDWIDTHS_HZ[DEFAULT_BANDWIDTH]
    if side == "high":
        return -half, min(half, ASYMMETRIC_EDGE_HZ)
    if side == "low":
        return -min(half, ASYMMETRIC_EDGE_HZ), half
    return -half, half


class BandpassFilter:
    """Complex FIR selecting [low, high] Hz of the IQ, state carried between
    chunks so buffers join seamlessly."""

    def __init__(self, low_hz: float, high_hz: float,
                 sample_rate: float, taps: int = 95):
        from scipy.signal import firwin

        centre = (low_hz + high_hz) / 2.0
        half = (high_hz - low_hz) / 2.0
        nyq = sample_rate / 2.0
        lowpass = firwin(taps, half / nyq, window=("kaiser", 6.0))
        n = np.arange(taps) - (taps - 1) / 2.0
        self.taps = (lowpass * np.exp(2j * np.pi * centre * n / sample_rate)
                     ).astype(np.complex64)
        self.delay = (taps - 1) // 2
        self.low_hz = low_hz
        self.high_hz = high_hz
        self._state = np.zeros(taps - 1, dtype=np.complex64)

    def process(self, iq: np.ndarray) -> np.ndarray:
        from scipy.signal import lfilter

        out, self._state = lfilter(self.taps, [1.0], iq, zi=self._state)
        return out


class Delay:
    """Fixed integer sample delay with carried state."""

    def __init__(self, samples: int, dtype=np.complex64):
        self.samples = samples
        self._buf = np.zeros(samples, dtype=dtype)

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.samples == 0:
            return x
        joined = np.concatenate((self._buf, x))
        self._buf = joined[-self.samples:]
        return joined[:len(x)]


class AdjacentCanceller:
    """Subtract a strong 200 kHz neighbour's in-band leakage from the
    channel signal.

    process(iq_wide, channel) takes the raw wide IQ and the already
    channel-filtered IQ (both from the same buffer) and returns the cleaned
    channel signal, delayed by one filter length so the two paths line up.
    `reduction_db` is how much channel power the last buffer lost - a
    positive number only when there was correlated interference to remove.
    """

    def __init__(self, neighbour_hz: float, channel_low_hz: float,
                 channel_high_hz: float, sample_rate: float,
                 taps: int = 95):
        self.neighbour_hz = neighbour_hz
        self._select = BandpassFilter(neighbour_hz - 100_000.0,
                                      neighbour_hz + 100_000.0,
                                      sample_rate, taps)
        self._inband = BandpassFilter(channel_low_hz, channel_high_hz,
                                      sample_rate, taps)
        self._align = Delay(self._select.delay)
        self._env = 0.0
        self._gain = 0j
        self.reduction_db = 0.0

    def process(self, iq: np.ndarray, channel: np.ndarray) -> np.ndarray:
        r = self._select.process(iq)
        mag = np.abs(r)
        # FM is constant-envelope: hard-limiting the selected neighbour
        # regenerates the sidebands its selection filter removed, i.e. the
        # part that lands inside our channel.
        env = float(np.mean(mag))
        self._env = env if self._env == 0.0 else 0.8 * self._env + 0.2 * env
        unit = r / np.maximum(mag, 1e-9)
        s = self._inband.process((self._env * unit).astype(np.complex64))
        y = self._align.process(channel)
        power_s = float(np.vdot(s, s).real)
        power_y = float(np.vdot(y, y).real)
        if power_s <= 1e-12 or power_y <= 1e-12:
            self.reduction_db = 0.0
            return y
        gain = np.vdot(s, y) / power_s
        self._gain = gain if self._gain == 0j else (0.7 * self._gain
                                                    + 0.3 * gain)
        out = y - self._gain * s
        power_out = float(np.vdot(out, out).real)
        self.reduction_db = (10.0 * np.log10(power_y / power_out)
                             if power_out > 0 else 0.0)
        if self.reduction_db < 0.0:
            # A fit that adds power means there was nothing correlated to
            # remove; pass the channel through untouched.
            self.reduction_db = 0.0
            self._gain = 0j
            return y
        return out


def neighbour_levels(iq: np.ndarray, sample_rate: float,
                     half_hz: float = 60_000.0, n_fft: int = 4096) -> dict:
    """Mean power in dBFS of the tuned channel and its two 200 kHz
    neighbours, from one FFT of the wide IQ."""
    n = min(n_fft, len(iq))
    if n < 64:
        return {"centre": None, "low": None, "high": None}
    spec = np.fft.fft(iq[:n] * np.hanning(n))
    freqs = np.fft.fftfreq(n, 1.0 / sample_rate)
    power = np.abs(spec) ** 2 / (n * 127.5) ** 2 * (8.0 / 3.0)

    def band(centre):
        sel = np.abs(freqs - centre) <= half_hz
        value = float(power[sel].sum())
        return 10.0 * np.log10(value) if value > 0 else None

    return {"centre": band(0.0), "low": band(-CHANNEL_SPACING_HZ),
            "high": band(CHANNEL_SPACING_HZ)}


def choose_side(levels: dict, current: str | None) -> str | None:
    """Pick which neighbour to protect against ('low', 'high') or None."""
    centre = levels.get("centre")
    low = levels.get("low")
    high = levels.get("high")
    if centre is None:
        return current

    def excess(value):
        return (value - centre) if value is not None else -999.0

    ex_low, ex_high = excess(low), excess(high)
    best, best_ex = ("high", ex_high) if ex_high >= ex_low else ("low", ex_low)
    if current in ("low", "high"):
        cur_ex = ex_low if current == "low" else ex_high
        if cur_ex > AUTO_THRESHOLD_DB - AUTO_HYSTERESIS_DB:
            other_ex = ex_high if current == "low" else ex_low
            if other_ex > cur_ex + AUTO_HYSTERESIS_DB:
                return best
            return current
        return None if best_ex <= AUTO_THRESHOLD_DB else best
    return best if best_ex > AUTO_THRESHOLD_DB else None


def overload_state(clipping: float | None, wide_dbfs: float | None) -> str:
    """'ok', 'warn' or 'overload' from ADC clipping and whole-band level."""
    if clipping is not None and clipping >= OVERLOAD_CLIP_HARD:
        return "overload"
    if clipping is not None and clipping >= OVERLOAD_CLIP_WARN:
        return "warn"
    if wide_dbfs is not None and wide_dbfs >= OVERLOAD_WIDE_DBFS:
        return "warn"
    return "ok"
