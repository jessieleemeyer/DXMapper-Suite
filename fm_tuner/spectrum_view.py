"""Spectrum + waterfall widget for the tuner GUI.

Shows +/-200 kHz around the tuned channel. HD Radio (IBOC) stations put
digital sidebands at roughly +/-129-198 kHz, which appear as flat-topped
shoulders either side of the analog signal; those regions are shaded so they
are easy to read at a glance. The HD status above the plot comes from the
optional HD decoder: only a real decoder lock counts as HD.

The waterfall is a ring-buffer PhotoImage: each frame writes one row and the
canvas shows two offset copies of the image, so scrolling costs one row of
pixel writes instead of a full redraw.
"""

from __future__ import annotations

import tkinter as tk

BG = "#10101c"
GRID = "#2a2a44"
TRACE = "#51e2a8"
MARKER = "#e94560"
HD_BAND = "#1f3a5f"
TEXT = "#cfd8ff"

WATERFALL_ROWS = 96
FLOOR_DB = -95.0
CEIL_DB = -25.0

# Blue -> cyan -> green -> yellow -> red intensity ramp.
_PALETTE_STOPS = [
    (0.00, (8, 8, 24)),
    (0.25, (20, 40, 120)),
    (0.50, (0, 160, 170)),
    (0.70, (80, 220, 90)),
    (0.85, (245, 220, 70)),
    (1.00, (240, 60, 60)),
]


def _build_palette(levels: int = 64) -> list[str]:
    palette = []
    for i in range(levels):
        pos = i / (levels - 1)
        for (p0, c0), (p1, c1) in zip(_PALETTE_STOPS, _PALETTE_STOPS[1:]):
            if p0 <= pos <= p1:
                t = 0.0 if p1 == p0 else (pos - p0) / (p1 - p0)
                rgb = tuple(int(round(a + (b - a) * t))
                            for a, b in zip(c0, c1))
                palette.append("#%02x%02x%02x" % rgb)
                break
        else:
            palette.append("#%02x%02x%02x" % _PALETTE_STOPS[-1][1])
    return palette


PALETTE = _build_palette()


class SpectrumView(tk.Frame):
    """Live spectrum trace above a scrolling waterfall."""

    def __init__(self, master, bins: int, span_hz: float,
                 hd_low_hz: float, hd_high_hz: float, **kwargs):
        super().__init__(master, bg=BG, **kwargs)
        self.bins = bins
        self.span_hz = span_hz
        self.hd_low_hz = hd_low_hz
        self.hd_high_hz = hd_high_hz

        self.header = tk.Label(self, text="Spectrum  \u00b1200 kHz",
                               bg=BG, fg=TEXT, font=("Segoe UI", 9, "bold"),
                               anchor="w")
        self.header.pack(fill=tk.X, padx=4, pady=(2, 0))

        self.trace_canvas = tk.Canvas(self, width=bins, height=90,
                                      bg=BG, highlightthickness=0)
        self.trace_canvas.pack(fill=tk.X, padx=4)

        self.waterfall_canvas = tk.Canvas(self, width=bins,
                                          height=WATERFALL_ROWS,
                                          bg=BG, highlightthickness=0)
        self.waterfall_canvas.pack(fill=tk.X, padx=4, pady=(1, 4))

        self._zoom = 1
        self._row = 0
        self._image = tk.PhotoImage(width=bins, height=WATERFALL_ROWS)
        self._top_item = self.waterfall_canvas.create_image(
            0, 0, image=self._image, anchor="nw")
        self._bottom_item = self.waterfall_canvas.create_image(
            0, WATERFALL_ROWS, image=self._image, anchor="nw")
        self._blank()
        self._draw_static()
        self.trace_canvas.bind("<Configure>", self._on_resize)

    # -- drawing -----------------------------------------------------------

    def _on_resize(self, event) -> None:
        zoom = max(1, int(event.width // self.bins))
        if zoom == self._zoom:
            return
        self._zoom = zoom
        self._image = tk.PhotoImage(width=self.bins * zoom,
                                    height=WATERFALL_ROWS)
        self.waterfall_canvas.itemconfig(self._top_item, image=self._image)
        self.waterfall_canvas.itemconfig(self._bottom_item, image=self._image)
        self._row = 0
        self._blank()
        self._draw_static()

    def _blank(self) -> None:
        row = "{" + " ".join([PALETTE[0]] * self.bins * self._zoom) + "}"
        self._image.put(" ".join([row] * WATERFALL_ROWS), to=(0, 0))

    def _x_for(self, offset_hz: float) -> float:
        width = self.bins * self._zoom
        return (offset_hz + self.span_hz / 2) / self.span_hz * width

    def _draw_static(self) -> None:
        canvas = self.trace_canvas
        canvas.delete("static")
        height = 90
        for sign in (-1, 1):
            x0 = self._x_for(sign * self.hd_low_hz)
            x1 = self._x_for(sign * self.hd_high_hz)
            canvas.create_rectangle(min(x0, x1), 0, max(x0, x1), height,
                                    fill=HD_BAND, outline="", tags="static")
        for hz in (-150_000, -100_000, -50_000, 50_000, 100_000, 150_000):
            x = self._x_for(hz)
            canvas.create_line(x, 0, x, height, fill=GRID, tags="static")
            canvas.create_text(x + 2, height - 6, text=f"{hz // 1000:+d}",
                               anchor="w", fill=GRID,
                               font=("Segoe UI", 7), tags="static")
        center = self._x_for(0)
        canvas.create_line(center, 0, center, height, fill=MARKER,
                           dash=(2, 3), tags="static")
        canvas.create_text(center + 3, height - 6, text="0 kHz (tuned)",
                           anchor="w", fill=MARKER, font=("Segoe UI", 7),
                           tags="static")
        for sign in (-1, 1):
            mid = (self._x_for(sign * self.hd_low_hz)
                   + self._x_for(sign * self.hd_high_hz)) / 2
            canvas.create_text(mid, 9, text="HD sideband", fill=TEXT,
                               font=("Segoe UI", 7, "bold"), tags="static")
        canvas.create_text(4, 6, text=f"{CEIL_DB:.0f} dBFS", anchor="w",
                           fill=GRID, font=("Segoe UI", 7), tags="static")
        canvas.create_text(4, height - 6, text=f"{FLOOR_DB:.0f} dBFS",
                           anchor="w", fill=GRID, font=("Segoe UI", 7),
                           tags="static")
        canvas.tag_lower("static")

    def clear(self) -> None:
        self.trace_canvas.delete("trace")
        self._blank()

    def update_spectrum(self, power_db, hd: dict | None = None) -> None:
        """Push one spectrum frame (list of dB values, one per bin)."""
        if not power_db or len(power_db) != self.bins:
            return

        height = 90
        points = []
        for i, value in enumerate(power_db):
            y = self._scale(value)
            points.extend((i * self._zoom, height - y * height))
        self.trace_canvas.delete("trace")
        self.trace_canvas.create_line(*points, fill=TRACE, width=1,
                                      tags="trace")

        colors = [PALETTE[min(len(PALETTE) - 1,
                              max(0, int(self._scale(v) * (len(PALETTE) - 1))))]
                  for v in power_db]
        if self._zoom > 1:
            colors = [c for c in colors for _ in range(self._zoom)]
        row = "{" + " ".join(colors) + "}"
        # Newest row at the top, older rows flowing down.
        self._row = (self._row - 1) % WATERFALL_ROWS
        self._image.put(row, to=(0, self._row))
        self.waterfall_canvas.coords(self._top_item, 0, -(self._row))
        self.waterfall_canvas.coords(self._bottom_item, 0,
                                     WATERFALL_ROWS - self._row)

        verdict = hd_verdict(hd)
        self.header.config(text=f"Spectrum  \u00b1200 kHz    {verdict}")

    @staticmethod
    def _scale(value_db: float) -> float:
        return max(0.0, min(1.0, (value_db - FLOOR_DB) / (CEIL_DB - FLOOR_DB)))


def hd_verdict(hd: dict | None) -> str:
    """One-line HD status from the decoder's telemetry."""
    if not hd or not hd.get("installed"):
        return "HD decoder not installed"
    if hd.get("synced"):
        mer = hd.get("mer_db")
        return "HD locked" + (f" ({hd.get('call')})" if hd.get("call") else "") \
            + (f"  MER {mer:.0f} dB" if mer is not None else "")
    return "HD lost" if hd.get("ever_synced") else "No HD lock"
