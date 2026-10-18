"""FM broadcast band plans (regions) used by the tuner.

The region is chosen on first start and saved in settings:

  americas  87.7-108.1 MHz, 0.2 MHz steps on odd tenths (87.7, 87.9 ... 108.1)
  europe    87.5-108.0 MHz, 0.1 MHz steps (most of the world)
  japan     76.0-95.0 MHz, 0.1 MHz steps
  oirt      65.90-74.00 MHz, 0.03 MHz steps (Eastern Europe OIRT band)

Frequencies are handled internally as integer kHz so that grid arithmetic
is exact.
"""

from __future__ import annotations

# key: (menu title, min kHz, max kHz, step kHz, decimals shown, first-run kHz)
PLANS: dict[str, tuple[str, int, int, int, int, int]] = {
    "americas": ("Americas (87.7-108.1 MHz, 0.2 MHz on odd)",
                 87_700, 108_100, 200, 1, 101_100),
    "europe": ("Europe / most of the world (87.5-108.0 MHz, 0.1 MHz)",
               87_500, 108_000, 100, 1, 100_000),
    "japan": ("Japan (76.0-95.0 MHz, 0.1 MHz)",
              76_000, 95_000, 100, 1, 80_000),
    "oirt": ("Eastern Europe OIRT (65.90-74.00 MHz, 0.03 MHz)",
             65_900, 74_000, 30, 2, 69_800),
}
DEFAULT_PLAN = "americas"
_plan = DEFAULT_PLAN


class FrequencyError(ValueError):
    """Raised when a frequency is outside the band or off the channel grid."""


def set_plan(key: str) -> str:
    """Switch the active region; unknown keys fall back to Americas."""
    global _plan
    _plan = key if key in PLANS else DEFAULT_PLAN
    return _plan


def plan_key() -> str:
    return _plan


def _limits(key: str | None = None) -> tuple[int, int, int, int]:
    _, lo, hi, step_khz, decimals, _ = PLANS[key or _plan]
    return lo, hi, step_khz, decimals


def min_mhz() -> float:
    return _limits()[0] / 1000.0


def max_mhz() -> float:
    return _limits()[1] / 1000.0


def step_mhz() -> float:
    return _limits()[2] / 1000.0


def decimals() -> int:
    return _limits()[3]


def start_mhz() -> float:
    """A sensible first-run frequency inside the active band."""
    return PLANS[_plan][5] / 1000.0


def info() -> dict:
    """Band description sent with the tuner status."""
    lo, hi, step_khz, dec = _limits()
    return {"plan": _plan, "min_mhz": lo / 1000.0, "max_mhz": hi / 1000.0,
            "step_mhz": step_khz / 1000.0, "decimals": dec}


def fmt(freq_mhz: float) -> str:
    """Frequency as text with the active region's decimals (101.1, 66.44)."""
    return f"{float(freq_mhz):.{decimals()}f}"


def step_label() -> str:
    """Step size for button labels: 0.2, 0.1 or 0.03."""
    return f"{step_mhz():g}"


def to_khz(freq_mhz: float) -> int:
    return int(round(float(freq_mhz) * 1000))


def channels_khz() -> list[int]:
    lo, hi, step_khz, _ = _limits()
    return list(range(lo, hi + 1, step_khz))


def channels_mhz() -> list[float]:
    """All valid channels in MHz."""
    return [k / 1000.0 for k in channels_khz()]


def is_valid(freq_mhz: float, key: str | None = None) -> bool:
    try:
        validate(freq_mhz, key)
    except FrequencyError:
        return False
    return True


def validate(freq_mhz: float, key: str | None = None) -> float:
    """Validate a frequency against the band plan and return it normalized.

    Raises FrequencyError with a message suitable for display to the user.
    """
    try:
        value = float(freq_mhz)
    except (TypeError, ValueError):
        raise FrequencyError(f"{freq_mhz!r} is not a number")

    lo, hi, step_khz, dec = _limits(key)
    khz = to_khz(value)
    if abs(value * 1000 - khz) > 0.01:
        raise FrequencyError(
            f"{value} MHz is not on the {step_khz / 1000:g} MHz channel grid "
            f"(e.g. {lo / 1000:.{dec}f})")
    if khz < lo or khz > hi:
        raise FrequencyError(
            f"{value} MHz is outside the {lo / 1000:.{dec}f}-"
            f"{hi / 1000:.{dec}f} MHz band (change it in the Region menu)")
    off = (khz - lo) % step_khz
    if off:
        below = khz - off
        raise FrequencyError(
            f"{value} MHz is not a channel: channels are spaced "
            f"{step_khz / 1000:g} MHz apart starting at {lo / 1000:.{dec}f} MHz "
            f"(e.g. {below / 1000:.{dec}f} or "
            f"{(below + step_khz) / 1000:.{dec}f})")
    return khz / 1000.0


def validate_any(freq_mhz: float) -> float:
    """Valid in the active region or any other one (presets survive a
    region change). Raises the active region's error otherwise."""
    try:
        return validate(freq_mhz)
    except FrequencyError as exc:
        for key in PLANS:
            if is_valid(freq_mhz, key):
                return validate(freq_mhz, key)
        raise exc


def step(freq_mhz: float, direction: int) -> float:
    """Return the next channel up (direction=+1) or down (-1), wrapping."""
    lo, hi, step_khz, _ = _limits()
    khz = to_khz(freq_mhz)
    if khz < lo or khz > hi:
        return (lo if direction > 0 else hi) / 1000.0
    off = (khz - lo) % step_khz
    if off:
        # Snap an off-grid value onto the grid before stepping.
        khz = khz - off + (step_khz if direction > 0 else 0)
        return min(hi, khz) / 1000.0
    khz += step_khz * (1 if direction > 0 else -1)
    if khz > hi:
        khz = lo
    elif khz < lo:
        khz = hi
    return khz / 1000.0


def nearest(freq_mhz: float) -> float:
    """Snap any frequency to the closest valid channel."""
    lo, hi, step_khz, _ = _limits()
    khz = max(lo, min(hi, to_khz(freq_mhz)))
    n = round((khz - lo) / step_khz)
    return min(hi, lo + n * step_khz) / 1000.0
