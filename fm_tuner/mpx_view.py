"""Baseband (MPX) spectrum widget for the tuner GUI.

Shows the demodulated composite signal from 0 to 100 kHz, which is where the
FM baseband lives: the mono L+R sum below 15 kHz, the 19 kHz stereo pilot, the
38 kHz L-R subcarrier, the 57 kHz RDS subcarrier, the RDS2 subcarriers at
66.5 / 71.25 / 76 kHz, and the 67 kHz and 92 kHz SCA (or DARC) subcarriers.

Everything that can appear in the composite is labelled: each region is
shaded in its own colour behind the trace with a legend band under the ruler,
every carrier has a marker line with its name and frequency written next to
it (labels are packed into staggered rows using the real font metrics so the
close-spaced 66.5/67/71.25/76 kHz labels never overlap), and a status table
under the plot lists each carrier - plus the HD Radio decoder lock - with
what it is, its live level and PRESENT / not seen.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

BG = "#10101c"
GRID = "#2a2a44"
TRACE = "#7fb2ff"
FILL = "#1b2b4d"
LABEL = "#9fb0e0"
TEXT = "#cfd8ff"
PRESENT = "#2ecc71"
ABSENT = "#5c6280"

# Row counts; the row heights come from the real font metrics at runtime so
# the layout holds at any DPI / Tk scaling.
LABEL_ROWS = 3
PLOT_HEIGHT = 150
LEGEND_ROWS = 2
STATUS_ROWS = 5

FLOOR_DB = -80.0
CEIL_DB = 0.0
# A carrier counts as "present" when its summed level sits this far above the
# median of the whole baseband frame (a rough noise-floor estimate).
PRESENT_MARGIN_DB = 10.0

# Every part of the composite, left to right: (low Hz, high Hz, label,
# band colour, legend text colour). Gaps between regions are guard bands.
REGIONS = [
    (0.0, 15_000.0, "L+R audio (mono) 0-15k", "#16304a", "#7fd8ff"),
    (18_000.0, 20_000.0, "19k pilot", "#154a3a", "#59e3b4"),
    (23_000.0, 53_000.0, "L-R stereo difference 23-53k (38k)", "#1c2c52",
     "#8fa8ff"),
    (54_600.0, 59_400.0, "RDS 57k", "#3a2a5f", "#c79bff"),
    (64_100.0, 68_900.0, "RDS2-1 66.5k / SCA 67k", "#4a2f1c", "#ffb27f"),
    (68_900.0, 73_600.0, "RDS2-2 71.25k", "#43351a", "#ffd27f"),
    (73_600.0, 78_400.0, "RDS2-3 76k", "#4a2f1c", "#ffd27f"),
    (78_400.0, 89_000.0, "unused", "#141424", "#6c7290"),
    (89_000.0, 95_000.0, "SCA 92k / DARC", "#3a1f2c", "#ff8fae"),
    (95_000.0, 100_000.0, "noise", "#141424", "#6c7290"),
]

# (key, Hz, short name, marker colour, label row, label side) - keys match
# the receiver status fields so levels can be looked up by name. The side
# says which side of the marker line the text sits on ("w" = to the right).
CARRIERS = [
    ("pilot", 19_000.0, "19k pilot", "#59e3b4", 0, "w"),
    ("stereo", 38_000.0, "38k L-R", "#8fa8ff", 0, "w"),
    ("rds", 57_000.0, "57k RDS", "#c79bff", 0, "w"),
    ("rds2_1", 66_500.0, "66.5k RDS2-1", "#ffd27f", 1, "e"),
    ("sca", 67_000.0, "67k SCA", "#ffb27f", 0, "w"),
    ("rds2_2", 71_250.0, "71.25k RDS2-2", "#ffd27f", 1, "w"),
    ("rds2_3", 76_000.0, "76k RDS2-3", "#ffd27f", 0, "w"),
    ("sca92", 92_000.0, "92k SCA/DARC", "#ff8fae", 1, "e"),
]
# What each carrier is, for the status table.
CARRIER_MEANING = {
    "pilot": "stereo pilot tone",
    "stereo": "stereo difference (DSB-SC)",
    "rds": "RDS data 1187.5 bps",
    "rds2_1": "RDS2 stream 1",
    "sca": "SCA subcarrier audio",
    "rds2_2": "RDS2 stream 2",
    "rds2_3": "RDS2 stream 3",
    "sca92": "SCA audio or DARC data",
}
# Not a baseband carrier - HD Radio (IBOC) sidebands sit at +/-129-198 kHz
# RF, outside the composite - but listed here so the whole picture is in one
# place. PRESENT only when the optional HD decoder locks; level is its MER.
HD_ROW = ("hd", "HD Radio (IBOC)", "HD decoder lock (level = MER)",
          "#7fd8ff")
# Shorter legend text used when the full region label does not fit its band.
SHORT_LEGEND = {
    "L+R audio (mono) 0-15k": "L+R",
    "19k pilot": "pilot",
    "L-R stereo difference 23-53k (38k)": "L-R stereo",
    "RDS 57k": "RDS",
    "RDS2-1 66.5k / SCA 67k": "RDS2-1/SCA",
    "RDS2-2 71.25k": "RDS2-2",
    "RDS2-3 76k": "RDS2-3",
    "SCA 92k / DARC": "SCA/DARC",
}
RULER_STEP_HZ = 10_000.0


class MpxView(tk.Frame):
    """Static-scale spectrum of the demodulated FM baseband."""

    def __init__(self, master, bins: int, span_hz: float, **kwargs):
        super().__init__(master, bg=BG, **kwargs)
        self.bins = bins
        self.span_hz = span_hz
        self._width = bins
        self.levels: dict[str, float | None] = {k: None for k, *_ in CARRIERS}
        self.present: dict[str, bool | None] = {k: None for k, *_ in CARRIERS}
        self.present["hd"] = None

        self.font_label = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self.font_small = tkfont.Font(family="Segoe UI", size=8)
        self.font_small_bold = tkfont.Font(family="Segoe UI", size=8,
                                           weight="bold")
        self.font_mono = tkfont.Font(family="Consolas", size=9)
        self.label_row_h = self.font_label.metrics("linespace") + 2
        self.legend_row_h = self.font_small_bold.metrics("linespace") + 2
        self.status_row_h = self.font_label.metrics("linespace") + 2
        self.ruler_h = self.font_small.metrics("linespace") + 4
        self.height = (LABEL_ROWS * self.label_row_h + PLOT_HEIGHT
                       + self.ruler_h + LEGEND_ROWS * self.legend_row_h
                       + (STATUS_ROWS + 1) * self.status_row_h + 6)

        self.header = tk.Label(self, text="MPX baseband  0-100 kHz",
                               bg=BG, fg=TEXT, font=("Segoe UI", 9, "bold"),
                               anchor="w")
        self.header.pack(fill=tk.X, padx=4, pady=(2, 0))

        self.canvas = tk.Canvas(self, width=bins, height=self.height,
                                bg=BG, highlightthickness=0)
        self.canvas.pack(fill=tk.X, padx=4, pady=(0, 4))
        self.canvas.bind("<Configure>", self._on_resize)
        self._draw_static()
        self._draw_status()

    # -- geometry ----------------------------------------------------------

    @property
    def plot_top(self) -> int:
        return LABEL_ROWS * self.label_row_h

    @property
    def plot_bottom(self) -> int:
        return self.plot_top + PLOT_HEIGHT

    def _on_resize(self, event) -> None:
        if event.width == self._width:
            return
        self._width = max(event.width, 1)
        self._draw_static()
        self._draw_status()

    def _x_for(self, hz: float) -> float:
        return hz / self.span_hz * self._width

    # -- drawing -----------------------------------------------------------

    def _draw_static(self) -> None:
        canvas = self.canvas
        canvas.delete("static")
        top, bottom = self.plot_top, self.plot_bottom
        legend_y = bottom + self.ruler_h

        row_h = self.legend_row_h
        for i, (low, high, label, band_color, text_color) in enumerate(REGIONS):
            x0, x1 = self._x_for(low), self._x_for(high)
            canvas.create_rectangle(x0, top, x1, bottom, fill=band_color,
                                    outline="", tags="static")
            # Alternate legend rows so neighbouring labels never collide;
            # a label that still would not fit its band is left to the
            # status table below.
            y = legend_y + (i % LEGEND_ROWS) * row_h
            canvas.create_rectangle(x0, y + 1, x1, y + row_h - 1,
                                    fill=band_color, outline="",
                                    tags="static")
            for text in (label, SHORT_LEGEND.get(label)):
                if text and self.font_small_bold.measure(text) <= (x1 - x0) - 4:
                    canvas.create_text((x0 + x1) / 2, y + row_h / 2,
                                       text=text, fill=text_color,
                                       font=self.font_small_bold,
                                       tags="static")
                    break

        # Carrier name/frequency labels: each goes in the first row (from its
        # preferred one) where it does not overlap a label already placed.
        occupied: list[list[tuple[float, float]]] = [[] for _ in range(LABEL_ROWS)]
        for key, hz, name, colour, preferred, anchor in CARRIERS:
            x = self._x_for(hz)
            w = self.font_label.measure(name)
            dx = -3 if anchor == "e" else 3
            span = (x + dx - w, x + dx) if anchor == "e" else (x + dx, x + dx + w)
            span = (max(0.0, span[0]), min(float(self._width), span[1]))
            row = preferred
            for candidate in list(range(preferred, LABEL_ROWS)) + list(range(preferred)):
                if all(span[1] + 4 < s0 or span[0] - 4 > s1
                       for s0, s1 in occupied[candidate]):
                    row = candidate
                    break
            occupied[row].append(span)
            label_y = row * self.label_row_h + self.label_row_h / 2
            canvas.create_line(x, label_y + self.label_row_h / 2, x, bottom,
                               fill=colour, dash=(2, 3), tags="static")
            canvas.create_text(x + dx, label_y, text=name, anchor=anchor,
                               fill=colour, font=self.font_label,
                               tags="static")

        canvas.create_line(0, bottom, self._width, bottom, fill=GRID,
                           tags="static")
        ticks = int(self.span_hz // RULER_STEP_HZ)
        for i in range(ticks + 1):
            hz = i * RULER_STEP_HZ
            x = min(self._x_for(hz), self._width - 1)
            canvas.create_line(x, top, x, bottom, fill=GRID, tags="static")
            canvas.create_line(x, bottom, x, bottom + 4, fill=LABEL,
                               tags="static")
            if i == ticks:
                text, anchor = f"{int(hz / 1000)} kHz", "e"
            else:
                text, anchor = f"{int(hz / 1000)}", "w" if i == 0 else "center"
            canvas.create_text(x, bottom + self.ruler_h / 2 + 1, text=text,
                               anchor=anchor, fill=LABEL,
                               font=self.font_small, tags="static")
        canvas.create_text(4, top + 7, text=f"{CEIL_DB:.0f} dB", anchor="w",
                           fill=LABEL, font=self.font_small, tags="static")
        canvas.create_text(4, bottom - 6, text=f"{FLOOR_DB:.0f} dB",
                           anchor="w", fill=LABEL, font=self.font_small,
                           tags="static")
        canvas.tag_lower("static")

    def _draw_status(self) -> None:
        """Table under the plot: every carrier, what it is, live level and
        PRESENT / not seen, two columns of four rows."""
        canvas = self.canvas
        canvas.delete("status")
        rh = self.status_row_h
        y0 = self.height - (STATUS_ROWS + 1) * rh - 2
        col_w = self._width / 2
        for col in range(2):
            x = col * col_w + 4
            canvas.create_text(x, y0 + rh / 2, anchor="w",
                               text="carrier", fill=LABEL,
                               font=self.font_small, tags="status")
            canvas.create_text(x + col_w * 0.36, y0 + rh / 2,
                               anchor="w", text="what it is", fill=LABEL,
                               font=self.font_small, tags="status")
            canvas.create_text(x + col_w * 0.72, y0 + rh / 2,
                               anchor="w", text="level", fill=LABEL,
                               font=self.font_small, tags="status")
            canvas.create_text(x + col_w * 0.86, y0 + rh / 2,
                               anchor="w", text="status", fill=LABEL,
                               font=self.font_small, tags="status")
        canvas.create_line(0, y0 + rh, self._width,
                           y0 + rh, fill=GRID, tags="status")
        entries = [(key, name, CARRIER_MEANING.get(key, ""), colour)
                   for key, _hz, name, colour, _row, _side in CARRIERS]
        entries.append(HD_ROW)
        for i, (key, name, meaning, colour) in enumerate(entries):
            col, row = divmod(i, STATUS_ROWS)
            x = col * col_w + 4
            y = y0 + rh * (row + 1) + rh / 2
            present = self.present.get(key)
            dot = PRESENT if present else ABSENT
            fg = colour if present else LABEL
            font = self.font_label if present else self.font_small
            canvas.create_oval(x, y - 4, x + 8, y + 4, fill=dot, outline="",
                               tags="status")
            canvas.create_text(x + 12, y, anchor="w", text=name, fill=fg,
                               font=font, tags="status")
            canvas.create_text(x + col_w * 0.36, y, anchor="w",
                               text=meaning, fill=fg,
                               font=self.font_small, tags="status")
            canvas.create_text(x + col_w * 0.72, y, anchor="w",
                               text=_db(self.levels.get(key)), fill=fg,
                               font=self.font_mono, tags="status")
            state = ("--" if present is None
                     else "PRESENT" if present else "not seen")
            canvas.create_text(x + col_w * 0.86, y, anchor="w", text=state,
                               fill=PRESENT if present else ABSENT,
                               font=self.font_label, tags="status")

    def clear(self) -> None:
        self.canvas.delete("trace")
        self.levels = {k: None for k, *_ in CARRIERS}
        self.present = {k: None for k, *_ in CARRIERS}
        self.present["hd"] = None
        self._draw_status()
        self.header.config(text="MPX baseband  0-100 kHz")

    def update_spectrum(self, power_db, levels: dict | None = None,
                        stereo: bool | None = None,
                        rds_ready: bool | None = None,
                        hd_detected: bool | None = None) -> None:
        """Push one baseband frame (dB per bin) plus per-carrier levels.

        `levels` maps the CARRIERS keys to measured dB (or None). Presence
        is judged against the frame's median level, except the pilot and RDS
        which use the decoders' own verdicts when given.
        """
        if not power_db or len(power_db) != self.bins:
            return

        step = self._width / self.bins
        bottom = self.plot_bottom
        points = [0.0, float(bottom)]
        for i, value in enumerate(power_db):
            y = bottom - self._scale(value) * (PLOT_HEIGHT - 4)
            points.extend((i * step, y))
        points.extend(((self.bins - 1) * step, float(bottom)))
        self.canvas.delete("trace")
        self.canvas.create_polygon(*points, fill=FILL, outline=TRACE,
                                   width=1, tags="trace")

        floor = sorted(power_db)[len(power_db) // 2]
        levels = levels or {}
        for key, *_ in CARRIERS:
            value = levels.get(key)
            self.levels[key] = value
            self.present[key] = carrier_present(value, floor)
        if stereo is not None:
            self.present["pilot"] = bool(stereo)
            self.present["stereo"] = bool(stereo)
        if rds_ready is not None:
            self.present["rds"] = bool(rds_ready)
        self.present["hd"] = hd_detected
        self.levels["hd"] = levels.get("hd")
        self._draw_status()

        self.header.config(text=header_text(self.levels))

    @staticmethod
    def _scale(value_db: float) -> float:
        return max(0.0, min(1.0, (value_db - FLOOR_DB) / (CEIL_DB - FLOOR_DB)))


def carrier_present(level_db, floor_db) -> bool | None:
    """True when a carrier's level clears the noise floor by the margin."""
    if level_db is None or floor_db is None:
        return None
    return level_db > floor_db + PRESENT_MARGIN_DB


def header_text(levels: dict) -> str:
    return ("MPX baseband  0-100 kHz    "
            f"19k pilot {_db(levels.get('pilot'))}   "
            f"38k stereo {_db(levels.get('stereo'))}   "
            f"57k RDS {_db(levels.get('rds'))}   "
            f"67k SCA {_db(levels.get('sca'))}   "
            f"92k SCA {_db(levels.get('sca92'))}")


def _db(value) -> str:
    return "--" if value is None else f"{value:.0f} dB"
