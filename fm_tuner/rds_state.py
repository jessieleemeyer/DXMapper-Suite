"""Live RDS field accumulator for the tuner's own display and telemetry.

The RDS Monitor does the full-fidelity decode (RT+, ODA, PTYN, per-station
history). The tuner only needs the currently tuned station's fields, so this
keeps a single station's state and resets it on every retune.
"""

from __future__ import annotations

import datetime
import threading

PTY_NAMES_US = [
    "None", "News", "Information", "Sports", "Talk", "Rock", "Classic Rock",
    "Adult Hits", "Soft Rock", "Top 40", "Country", "Oldies", "Soft",
    "Nostalgia", "Jazz", "Classical", "Rhythm and Blues", "Soft R&B",
    "Foreign Language", "Religious Music", "Religious Talk", "Personality",
    "Public", "College", "Spanish Talk", "Spanish Music", "Hip Hop",
    "Unassigned", "Unassigned", "Weather", "Emergency Test", "Emergency",
]


def pty_name(pty: int | None) -> str:
    if pty is None:
        return ""
    if 0 <= pty < len(PTY_NAMES_US):
        return PTY_NAMES_US[pty]
    return f"PTY {pty}"


class LiveRDSState:
    """Thread-safe accumulator of the tuned station's RDS fields."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._pi = ""
            self._ps_segments: dict[int, tuple[str, str]] = {}
            self._ps = ""
            self._rt_segments: dict[int, str] = {}
            self._rt = ""
            self._rt_ab: int | None = None
            self._ptyn_segments: dict[int, tuple[str, str]] = {}
            self._ptyn = ""
            self._pty: int | None = None
            self._tp = False
            self._ta = False
            self._music: bool | None = None
            self._di: int | None = None
            self._clock: str | None = None
            self._groups = 0
            self._bad_blocks = 0
            self._total_blocks = 0
            self._group_counts: dict[str, int] = {}

    # -- ingest ------------------------------------------------------------

    def add_groups(self, groups) -> None:
        """Feed (b1, b2, b3, b4) hex-string tuples from the decoder."""
        with self._lock:
            for group in groups:
                try:
                    self._add_group(group)
                except Exception:
                    continue

    def _add_group(self, group) -> None:
        b1, b2, b3, b4 = group
        self._groups += 1
        for blk in (b2, b3, b4):
            self._total_blocks += 1
            if blk == "----":
                self._bad_blocks += 1

        if b1 != "----":
            self._pi = b1.upper()
        if b2 == "----":
            return

        b2_val = int(b2, 16)
        group_type = (b2_val >> 12) & 0xF
        version = (b2_val >> 11) & 0x1
        self._pty = (b2_val >> 5) & 0x1F
        self._tp = bool((b2_val >> 10) & 0x1)

        key = f"{group_type}{'A' if version == 0 else 'B'}"
        self._group_counts[key] = self._group_counts.get(key, 0) + 1

        if group_type == 0:
            self._ta = bool((b2_val >> 4) & 0x1)
            self._music = bool((b2_val >> 3) & 0x1)
            self._di = b2_val & 0x7
            if b4 != "----":
                b4_val = int(b4, 16)
                if b4_val != 0:
                    seg = b2_val & 0x3
                    self._ps_segments[seg] = (chr((b4_val >> 8) & 0xFF),
                                              chr(b4_val & 0xFF))
                    self._ps = _assemble_8(self._ps_segments)
        elif group_type == 2 and version == 0:
            ab_flag = (b2_val >> 4) & 0x1
            seg = b2_val & 0xF
            if ab_flag != self._rt_ab:
                self._rt_ab = ab_flag
                self._rt_segments = {}
            if b3 != "----":
                b3_val = int(b3, 16)
                self._rt_segments[seg * 4] = chr((b3_val >> 8) & 0xFF)
                self._rt_segments[seg * 4 + 1] = chr(b3_val & 0xFF)
            if b4 != "----":
                b4_val = int(b4, 16)
                self._rt_segments[seg * 4 + 2] = chr((b4_val >> 8) & 0xFF)
                self._rt_segments[seg * 4 + 3] = chr(b4_val & 0xFF)
            self._rt = _assemble_rt(self._rt_segments)
        elif group_type == 2 and version == 1:
            ab_flag = (b2_val >> 4) & 0x1
            seg = b2_val & 0x7
            if ab_flag != self._rt_ab:
                self._rt_ab = ab_flag
                self._rt_segments = {}
            if b4 != "----":
                b4_val = int(b4, 16)
                self._rt_segments[seg * 2] = chr((b4_val >> 8) & 0xFF)
                self._rt_segments[seg * 2 + 1] = chr(b4_val & 0xFF)
            self._rt = _assemble_rt(self._rt_segments)
        elif group_type == 4 and version == 0:
            if b3 != "----" and b4 != "----":
                self._clock = _decode_clock(int(b3, 16), int(b4, 16))
        elif group_type == 10 and version == 0:
            if b3 != "----" and b4 != "----":
                b3_val = int(b3, 16)
                seg = int(b4, 16) & 0x3
                self._ptyn_segments[seg] = (chr((b3_val >> 8) & 0xFF),
                                            chr(b3_val & 0xFF))
                self._ptyn = _assemble_8(self._ptyn_segments)

    # -- read --------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            error_rate = (self._bad_blocks / self._total_blocks
                          if self._total_blocks else None)
            return {
                "pi": self._pi,
                "ps": self._ps,
                "radiotext": self._rt,
                "ptyn": self._ptyn,
                "pty": self._pty,
                "pty_name": pty_name(self._pty),
                "tp": self._tp,
                "ta": self._ta,
                "music_speech": ("music" if self._music
                                 else "speech" if self._music is False
                                 else None),
                "di": self._di,
                "clock_time": self._clock,
                "groups_decoded": self._groups,
                "block_error_rate": error_rate,
                "group_counts": dict(self._group_counts),
            }

    @property
    def has_data(self) -> bool:
        with self._lock:
            return bool(self._pi)


def _assemble_8(segments: dict[int, tuple[str, str]]) -> str:
    chars = [" "] * 8
    for seg, (c1, c2) in segments.items():
        pos = seg * 2
        if pos < 8:
            chars[pos] = c1
        if pos + 1 < 8:
            chars[pos + 1] = c2
    return "".join(chars).rstrip()


def _assemble_rt(segments: dict[int, str]) -> str:
    if not segments:
        return ""
    max_pos = max(segments)
    chars = [" "] * (max_pos + 1)
    for pos, ch in segments.items():
        chars[pos] = ch
    text = "".join(chars)
    idx = text.find("\r")
    if idx >= 0:
        text = text[:idx]
    return text.rstrip()


def _decode_clock(b3_val: int, b4_val: int) -> str | None:
    mjd = ((b3_val >> 1) << 2) | ((b4_val >> 6) & 0x3)
    hour = (b4_val >> 11) & 0x1F
    minute = b4_val & 0x3F
    lt_mag = (b4_val >> 8) & 0x3
    lt_offset = -lt_mag if (b4_val >> 10) & 0x1 else lt_mag
    if hour > 23 or minute > 59:
        return None
    try:
        date_obj = datetime.date(1858, 11, 17) + datetime.timedelta(days=mjd)
    except (OverflowError, ValueError):
        return None
    sign = "+" if lt_offset >= 0 else "-"
    off = abs(lt_offset)
    return (f"{date_obj.strftime('%a %b %d, %Y')} {hour:02d}:{minute:02d} "
            f"UTC{sign}{off // 2}:{off % 2 * 30:02d}")
