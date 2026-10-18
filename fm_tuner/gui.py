"""Tkinter GUI for the FM Tuner application."""

from __future__ import annotations

import os
import queue
import re
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import band, dx, hd_radio, layout as layout_store, receiver as rx, remote, servers
from .gain_memory import GainMemory
from .signal_units import format_signal
from .mpx_view import MpxView
from .presets import PresetError
from .rtlsdr_device import RtlSdrDevice, RtlSdrError
from .service import TunerService
from .spectrum_view import SpectrumView

BG = "#1a1a2e"
PANEL = "#16213e"
ACCENT = "#0f3460"
BUTTON = "#1a5276"
STOP = "#e94560"
FG = "#e6e6f0"
DIM = "#8a93b8"
OK = "#51e2a8"
HD_CHANNEL_ON = "#1e8449"     # HD channel button that is playing
HD_CHANNELS_PER_ROW = 3
MER_COLOURS = {"good": OK, "fair": "#f1c40f", "weak": STOP}

REFRESH_MS = 200
COLUMN_GAP = 6               # pixels between the two panel columns
DEFAULT_GEOMETRY = "1280x720"

# Signal meter: colour thresholds as a fraction of full scale
# (-70 dBFS = 0.0, -10 dBFS = 1.0), i.e. red below -45 dBFS, yellow below
# -30 dBFS, green above.
METER_YELLOW_AT = 25 / 60
METER_GREEN_AT = 40 / 60
METER_RED = "#e74c3c"
METER_YELLOW = "#f1c40f"
METER_GREEN = "#2ecc71"


class TunerWindow:

    def __init__(self, service: TunerService, start_frequency: float | None,
                 autostart: bool = True):
        self.service = service
        self.receiver = service.receiver
        self._events: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=500)

        self.root = tk.Tk()
        self.root.title("FM Tuner")
        self.root.configure(bg=BG)
        self.root.minsize(560, 160)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.layout = layout_store.LayoutStore()
        if self.layout.band is None:
            self.layout.band = self._ask_region()
            self.layout.save()
        band.set_plan(self.layout.band)
        if start_frequency is None:
            start_frequency = (self.layout.last_frequency
                               or band.start_mhz())
        if not band.is_valid(start_frequency):
            start_frequency = band.start_mhz()
        if not band.is_valid(self.receiver.frequency):
            self.receiver.tune(start_frequency)
        self._base_scaling = self.root.tk.call("tk", "scaling")
        self._apply_scale()
        self._panels: dict[str, tk.Frame] = {}
        self._panel_vars: dict[str, tk.BooleanVar] = {}
        self._drag: dict | None = None

        self.freq_var = tk.StringVar(value=band.fmt(start_frequency))
        self.state_var = tk.StringVar(value="Stopped")
        self.signal_var = tk.StringVar(value="Signal: --")
        self.detail_var = tk.StringVar(value="")
        self.gain_mode_var = tk.StringVar(value="manual")
        self.gain_var = tk.DoubleVar(value=0.0)
        self.gain_label_var = tk.StringVar(value="0.0 dB")
        self.gain_memory = GainMemory()
        self.gain_memory_var = tk.BooleanVar(value=self.layout.gain_memory)
        self.bias_var = tk.BooleanVar(value=service.receiver.bias_tee)
        self.bias_status_var = tk.StringVar(value="Bias-T off")
        self.audio_var = tk.BooleanVar(value=True)
        self.stereo_var = tk.BooleanVar(value=True)
        # Saved toggle wins unless the command line forced RDS2 on.
        if service.receiver.rds2_enabled or self.layout.rds2:
            service.receiver.set_rds2(True)
        self.rds2_var = tk.BooleanVar(value=service.receiver.rds2_enabled)
        self.rds2_status_var = tk.StringVar(value="RDS2: waiting")
        service.receiver.set_hd_mode(self.layout.hd_mode)
        service.receiver.set_hd_program(self.layout.hd_program)
        self.hd_mode_var = tk.StringVar(value=self.layout.hd_mode)
        self.hd_program_var = tk.StringVar(
            value=_hd_program_label(self.layout.hd_program))
        self.hd_info_var = tk.StringVar(value="")
        dx_now = service.receiver.dx_settings
        self.dx_bandwidth_var = tk.StringVar(
            value=self._bandwidth_label(dx_now["bandwidth"]))
        self.dx_side_var = tk.StringVar(value=dx_now["side"])
        self.dx_canceller_var = tk.BooleanVar(value=dx_now["canceller"])
        self.dx_status_var = tk.StringVar(value="")
        self.overload_var = tk.StringVar(value="OVERLOAD --")
        self.stereo_badge_var = tk.StringVar(value="")
        self.hd_badge_var = tk.StringVar(value="")
        self.volume_var = tk.DoubleVar(value=1.0)
        self.device_var = tk.StringVar(value="")
        if self.layout.radio:
            service.receiver.set_radio(self.layout.radio)
        self.radio_var = tk.StringVar(value="")
        self._radios: list[tuple[str, str]] = []
        self.pi_var = tk.StringVar(value="--")
        self.ps_var = tk.StringVar(value="--------")
        self.rt_var = tk.StringVar(value="")
        self.pty_var = tk.StringVar(value="")
        self.clock_var = tk.StringVar(value="")
        self.record_var = tk.StringVar(value="")
        self.record_fmt_var = tk.StringVar(value=RECORD_FORMATS[0])
        self._record_path = ""
        self.server_var = tk.StringVar(value="")
        self.preset_status_var = tk.StringVar(value="no presets yet")
        self._selected_preset: str | None = None
        self._tuned_mhz: float | None = None

        self._audio_devices: list[dict] = []
        self.remote_var = tk.BooleanVar(value=self.layout.remote)
        self.remote_url_var = tk.StringVar(value="")
        self.remote_info_var = tk.StringVar(value="")
        self._remote_qr_text = None
        self._build()
        self._bind_hotkeys()
        if self.layout.remote:
            self.root.after(300, self._apply_remote)
        self._pump_events()
        self._refresh()

        if autostart:
            self.root.after(150, lambda: self._start(start_frequency))

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        self._build_menu()
        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill=tk.BOTH, expand=True)
        self._columns: list[tk.Frame] = []
        for col in range(layout_store.COLUMNS):
            column = tk.Frame(self.body, bg=BG)
            column.grid(row=0, column=col, sticky="nsew",
                        padx=(0, COLUMN_GAP) if col == 0 else 0)
            self.body.grid_columnconfigure(col, weight=1, uniform="cols")
            self._columns.append(column)
        self.body.grid_rowconfigure(0, weight=1)
        self._drop_marker = tk.Frame(self.body, bg=OK, height=3)
        self._build_tuning()
        self._build_presets()
        self._build_status()
        self._build_spectrum()
        self._build_gain_audio()
        self._build_dx()
        self._build_rds()
        self._build_servers()
        self._build_remote()
        self._build_log()
        self._show_hd_controls()
        try:
            self.root.geometry(self.layout.geometry or DEFAULT_GEOMETRY)
        except tk.TclError:
            self.root.geometry(DEFAULT_GEOMETRY)
        self._apply_layout()

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        view = tk.Menu(menubar, tearoff=0)
        for key, title, _ in layout_store.PANELS:
            var = tk.BooleanVar(value=self.layout.visible.get(key, True))
            self._panel_vars[key] = var
            view.add_checkbutton(
                label=title, variable=var,
                command=lambda k=key: self._toggle_panel(k))
        view.add_separator()
        view.add_command(label="Tuner only (default)",
                         command=self._reset_layout)
        view.add_command(label="Show all panels", command=self._show_all)
        view.add_command(label="Default columns",
                         command=self._default_columns)
        self._topmost_var = tk.BooleanVar(value=self.layout.topmost)
        view.add_checkbutton(label="Always on top",
                             variable=self._topmost_var,
                             command=self._toggle_topmost)
        self._apply_topmost()
        self._hotkeys_var = tk.BooleanVar(value=self.layout.hotkeys)
        view.add_checkbutton(
            label="Arrow keys: \u2191\u2193 tune, \u2190\u2192 presets",
            variable=self._hotkeys_var, command=self._toggle_hotkeys)
        units = tk.Menu(view, tearoff=0)
        self._units_var = tk.StringVar(value=self.layout.signal_units)
        self.service.signal_units = self.layout.signal_units
        for value, text in (("s", "S-units (S1-S9+)"), ("db", "dB (dBFS)")):
            units.add_radiobutton(label=text, value=value,
                                  variable=self._units_var,
                                  command=self._change_signal_units)
        view.add_cascade(label="Signal units (RDS Monitor follows)",
                         menu=units)
        view.add_separator()
        size = tk.Menu(view, tearoff=0)
        self._scale_var = tk.IntVar(value=self.layout.scale)
        for pct in layout_store.SCALES:
            size.add_radiobutton(label=f"{pct}%", value=pct,
                                 variable=self._scale_var,
                                 command=self._change_scale)
        view.add_cascade(label="Size (for QHD / 4K)", menu=size)
        self._layout_menu = tk.Menu(view, tearoff=0)
        view.add_cascade(label="Layout presets", menu=self._layout_menu)
        self._render_layout_menu()
        view.add_separator()
        view.add_command(
            label="Drag a panel by its title bar into either column",
            state="disabled")
        menubar.add_cascade(label="View", menu=view)
        region = tk.Menu(menubar, tearoff=0)
        self._region_var = tk.StringVar(value=band.plan_key())
        for key, plan in band.PLANS.items():
            region.add_radiobutton(label=plan[0], value=key,
                                   variable=self._region_var,
                                   command=self._change_region)
        menubar.add_cascade(label="Region", menu=region)
        self.root.config(menu=menubar)

    def _panel(self, title: str, key: str) -> tk.Frame:
        """A movable, hideable panel; laid out later by `_apply_layout`."""
        outer = tk.Frame(self.body, bg=PANEL, bd=0)
        self._panels[key] = outer
        header = tk.Frame(outer, bg=ACCENT, cursor="fleur")
        header.pack(fill=tk.X)
        label = tk.Label(header, text=(title or key.upper()), bg=ACCENT,
                         fg=FG, font=("Segoe UI", 8, "bold"), anchor="w",
                         cursor="fleur")
        label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8, pady=1)
        tk.Button(header, text="\u00d7", bg=ACCENT, fg=DIM,
                  command=lambda k=key: self._hide_panel(k),
                  relief=tk.FLAT, bd=0, padx=4, pady=0, cursor="hand2",
                  activebackground=BUTTON, activeforeground=FG,
                  font=("Segoe UI", 7)).pack(side=tk.RIGHT)
        for widget in (header, label):
            widget.bind("<ButtonPress-1>", lambda e, k=key: self._drag_start(e, k))
            widget.bind("<B1-Motion>", self._drag_motion)
            widget.bind("<ButtonRelease-1>", self._drag_end)
        return outer

    # -- layout ------------------------------------------------------------

    def _apply_layout(self) -> None:
        """Stack the visible panels of each column top to bottom."""
        for frame in self._panels.values():
            frame.pack_forget()
        for col, column in enumerate(self._columns):
            for key in self.layout.column_order(col):
                frame = self._panels[key]
                frame.pack(in_=column, fill=tk.X, pady=(0, 4))
                frame.lift(column)
        for key, var in self._panel_vars.items():
            var.set(self.layout.visible.get(key, True))

    def _visible_order(self) -> list[str]:
        return [k for k in self.layout.order if self.layout.visible.get(k)]

    def _toggle_topmost(self) -> None:
        self.layout.topmost = bool(self._topmost_var.get())
        self._apply_topmost()
        self.layout.save()

    def _apply_topmost(self) -> None:
        try:
            self.root.attributes("-topmost", self.layout.topmost)
        except tk.TclError:
            pass

    def _default_columns(self) -> None:
        self.layout.reset_columns()
        self._apply_layout()
        self.layout.save()

    def _toggle_panel(self, key: str) -> None:
        self.layout.set_visible(key, self._panel_vars[key].get())
        self._apply_layout()
        self.layout.save()

    def _hide_panel(self, key: str) -> None:
        self._panel_vars[key].set(False)
        self._toggle_panel(key)

    def _reset_layout(self) -> None:
        self.layout.reset()
        self._apply_layout()
        self.layout.save()

    def _show_all(self) -> None:
        for key in layout_store.PANEL_KEYS:
            self.layout.set_visible(key, True)
        self._apply_layout()
        self.layout.save()

    def _render_layout_menu(self) -> None:
        menu = self._layout_menu
        menu.delete(0, tk.END)
        menu.add_command(label="Save current layout as...",
                         command=self._save_layout_preset)
        menu.add_command(label="Delete a layout preset...",
                         command=self._delete_layout_preset,
                         state="normal" if self.layout.presets else "disabled")
        if self.layout.presets:
            menu.add_separator()
            for name in sorted(self.layout.presets):
                menu.add_command(
                    label=name,
                    command=lambda n=name: self._load_layout_preset(n))

    def _save_layout_preset(self) -> None:
        name = simpledialog.askstring("Save layout", "Layout preset name:",
                                      parent=self.root)
        if not name:
            return
        if not self.layout.save_preset(name):
            messagebox.showerror("FM Tuner", "Could not save that layout "
                                 "(empty name or too many presets).")
            return
        self.layout.save()
        self._render_layout_menu()

    def _load_layout_preset(self, name: str) -> None:
        if not self.layout.load_preset(name):
            return
        self._scale_var.set(self.layout.scale)
        self._apply_layout()
        self.layout.save()

    def _delete_layout_preset(self) -> None:
        names = sorted(self.layout.presets)
        name = simpledialog.askstring(
            "Delete layout", "Layout preset to delete:\n" + "\n".join(names),
            parent=self.root)
        if name and self.layout.delete_preset(name.strip()):
            self.layout.save()
            self._render_layout_menu()

    def _change_scale(self) -> None:
        self.layout.set_scale(self._scale_var.get())
        self.layout.save()
        messagebox.showinfo(
            "FM Tuner", f"Size {self.layout.scale}% saved - restart the "
            "tuner to apply it.")

    def _apply_scale(self) -> None:
        self.root.tk.call("tk", "scaling",
                          self._base_scaling * self.layout.scale / 100.0)

    # -- drag & drop between / within the two columns ----------------------

    def _drag_start(self, event, key: str) -> None:
        self._drag = {"key": key, "target": None}
        self._panels[key].configure(highlightthickness=2,
                                    highlightbackground=OK)

    def _drop_target(self, x_root: int, y_root: int) -> tuple[int, int]:
        """(column, index) under the pointer."""
        bx = x_root - self.body.winfo_rootx()
        col = min(layout_store.COLUMNS - 1,
                  max(0, bx * layout_store.COLUMNS
                      // max(1, self.body.winfo_width())))
        key = self._drag["key"]
        index = 0
        for other in self.layout.column_order(col):
            if other == key:
                continue
            frame = self._panels[other]
            if y_root > frame.winfo_rooty() + frame.winfo_height() // 2:
                index += 1
        return col, index

    def _drag_motion(self, event) -> None:
        if not self._drag:
            return
        col, index = self._drop_target(event.x_root, event.y_root)
        self._drag["target"] = (col, index)
        others = [k for k in self.layout.column_order(col)
                  if k != self._drag["key"]]
        if index < len(others):
            y = self._panels[others[index]].winfo_y() - 3
        elif others:
            last = self._panels[others[-1]]
            y = last.winfo_y() + last.winfo_height()
        else:
            y = 0
        half = self.body.winfo_width() / layout_store.COLUMNS
        self._drop_marker.place(x=int(col * half), y=max(0, y),
                                width=int(half) - COLUMN_GAP)
        self._drop_marker.lift()

    def _drag_end(self, _event) -> None:
        if not self._drag:
            return
        key, target = self._drag["key"], self._drag["target"]
        self._drop_marker.place_forget()
        self._panels[key].configure(highlightthickness=0)
        self._drag = None
        if target and self.layout.place(key, *target):
            self._apply_layout()
            self.layout.save()

    def _button(self, master, text, command, color=BUTTON, width=None):
        return tk.Button(master, text=text, command=command, bg=color,
                         fg="white", font=("Segoe UI", 9, "bold"),
                         relief=tk.FLAT, padx=10, cursor="hand2",
                         activebackground=ACCENT, activeforeground="white",
                         width=width)

    def _build_tuning(self) -> None:
        panel = self._panel("TUNING", "tuning")
        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill=tk.X, padx=8, pady=8)

        self.freq_display = tk.Label(row, text="---.- MHz", bg=PANEL, fg=OK,
                                     font=("Segoe UI", 28, "bold"))
        self.freq_display.pack(side=tk.LEFT)
        self._wheel(self.freq_display, self._step)
        self.stereo_label = tk.Label(row, textvariable=self.stereo_badge_var,
                                     bg=PANEL, fg=DIM,
                                     font=("Segoe UI", 16, "bold"), width=7,
                                     anchor="w")
        self.stereo_label.pack(side=tk.LEFT, padx=(14, 0), pady=(10, 0))
        self.hd_label = tk.Label(row, textvariable=self.hd_badge_var,
                                 bg=PANEL, fg=DIM,
                                 font=("Segoe UI", 16, "bold"), width=4,
                                 anchor="w")
        self.hd_label.pack(side=tk.LEFT, padx=(4, 0), pady=(10, 0))

        entry_box = tk.Frame(row, bg=PANEL)
        entry_box.pack(side=tk.RIGHT)
        tk.Label(entry_box, text="Tune to (MHz)", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(anchor="e")
        inner = tk.Frame(entry_box, bg=PANEL)
        inner.pack()
        entry = tk.Entry(inner, textvariable=self.freq_var, width=8,
                         font=("Segoe UI", 12), justify="right",
                         bg=ACCENT, fg=FG, insertbackground=FG,
                         relief=tk.FLAT)
        entry.pack(side=tk.LEFT, ipady=3)
        entry.bind("<Return>", lambda _e: self._tune_entry())
        self._button(inner, "Tune", self._tune_entry).pack(side=tk.LEFT,
                                                           padx=4)

        controls = tk.Frame(panel, bg=PANEL)
        controls.pack(fill=tk.X, padx=8, pady=(0, 8))
        self._step_down_btn = self._button(controls, "", lambda: self._step(-1))
        self._step_down_btn.pack(side=tk.LEFT, padx=2)
        self._step_up_btn = self._button(controls, "", lambda: self._step(+1))
        self._step_up_btn.pack(side=tk.LEFT, padx=2)
        self._label_step_buttons()
        self._button(controls, "\u25bc Seek Down",
                     lambda: self._seek(-1)).pack(side=tk.LEFT, padx=8)
        self._button(controls, "\u25b2 Seek Up",
                     lambda: self._seek(+1)).pack(side=tk.LEFT, padx=2)

        # HD channel buttons: shown only for the channels this station has
        self.hd_channel_row = tk.Frame(panel, bg=PANEL)
        self._hd_channel_buttons = []
        self._hd_channels_shown = None

        hd_row = tk.Frame(panel, bg=PANEL)
        hd_row.pack(fill=tk.X, padx=8, pady=(0, 8))
        self._hd_seek_row = hd_row
        # HD Seek only shows when the HD decoder add-on is installed.
        self._hd_seek_buttons = tk.Frame(hd_row, bg=PANEL)
        self._button(self._hd_seek_buttons, "\u25bc HD Seek Down",
                     lambda: self._seek(-1, hd=True)).pack(side=tk.LEFT,
                                                          padx=2)
        self._button(self._hd_seek_buttons, "\u25b2 HD Seek Up",
                     lambda: self._seek(+1, hd=True)).pack(side=tk.LEFT,
                                                          padx=2)
        self._hd_seek_stop = self._button(hd_row, "\u25a0 Stop",
                                          self._stop_seek, STOP)
        self._hd_seek_stop.pack(side=tk.LEFT, padx=8)
        self.power_button = self._button(hd_row, "Start Receiver",
                                         self._toggle_power, ACCENT)
        self.power_button.pack(side=tk.LEFT, padx=2)

    def _show_hd_controls(self) -> None:
        """HD controls only where they can work: HD Radio exists only in
        the Americas region, and HD Seek / mode / channel need the decoder.
        Add HD decoder... stays visible in the Americas without it."""
        region = _hd_region()
        decoder = region and hd_radio.find_addon() is not None
        if region:
            if not self.hd_info_label.winfo_manager():
                self.hd_info_label.pack(fill=tk.X, padx=8, pady=(0, 4))
            self._hd_add_row.pack(fill=tk.X, padx=8, pady=(0, 2),
                                  before=self.hd_info_label)
        else:
            self._hd_add_row.pack_forget()
            self.hd_mer_label.pack_forget()
            self.hd_info_label.pack_forget()
        if decoder:
            self._hd_seek_buttons.pack(side=tk.LEFT,
                                       before=self._hd_seek_stop)
            self._hd_controls.pack(fill=tk.X, before=self._hd_add_row)
        else:
            self._hd_seek_buttons.pack_forget()
            self._hd_controls.pack_forget()

    def _build_presets(self) -> None:
        panel = self._panel("PRESETS", "presets")
        self._preset_row = tk.Frame(panel, bg=PANEL)
        self._preset_row.pack(fill=tk.X, padx=8, pady=(2, 0))

        actions = tk.Frame(panel, bg=PANEL)
        actions.pack(fill=tk.X, padx=8, pady=(4, 8))
        self._button(actions, "+ Save current",
                     self._save_preset).pack(side=tk.LEFT, padx=2)
        self._button(actions, "Rename",
                     self._rename_preset, ACCENT).pack(side=tk.LEFT, padx=2)
        self._button(actions, "Delete",
                     self._delete_preset, STOP).pack(side=tk.LEFT, padx=2)
        self._button(actions, "\u25c0",
                     lambda: self._move_preset(-1), ACCENT).pack(
            side=tk.LEFT, padx=(12, 2))
        self._button(actions, "\u25b6",
                     lambda: self._move_preset(+1), ACCENT).pack(
            side=tk.LEFT, padx=2)
        tk.Label(actions, textvariable=self.preset_status_var, bg=PANEL,
                 fg=DIM, font=("Segoe UI", 8)).pack(side=tk.RIGHT)
        self._preset_buttons: list[tk.Button] = []
        self._render_presets()

    def _build_status(self) -> None:
        panel = self._panel("SIGNAL", "status")
        tk.Label(panel, textvariable=self.state_var, bg=PANEL, fg=FG,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(
            fill=tk.X, padx=8)
        meter_row = tk.Frame(panel, bg=PANEL)
        meter_row.pack(fill=tk.X, padx=8, pady=(2, 0))
        tk.Label(meter_row, textvariable=self.signal_var, bg=PANEL, fg=FG,
                 font=("Consolas", 9), anchor="w", width=26).pack(side=tk.LEFT)
        self.overload_label = tk.Label(meter_row, textvariable=self.overload_var,
                                       bg=PANEL, fg=DIM,
                                       font=("Consolas", 8, "bold"),
                                       width=18, anchor="e")
        self.overload_label.pack(side=tk.RIGHT, padx=(6, 0))
        self.meter = tk.Canvas(meter_row, height=14, bg="#0b1020",
                               highlightthickness=1,
                               highlightbackground=ACCENT)
        self.meter.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        self.meter_fill = self.meter.create_rectangle(0, 0, 0, 14, width=0,
                                                      fill=METER_RED)
        self.meter_ticks = [
            self.meter.create_line(0, 0, 0, 14, fill=DIM) for _ in range(2)]
        self.meter.bind("<Configure>", lambda _e: self._draw_meter())
        self._meter_level = 0.0
        tk.Label(panel, textvariable=self.detail_var, bg=PANEL, fg=DIM,
                 font=("Consolas", 8), anchor="w", justify="left").pack(
            fill=tk.X, padx=8, pady=(0, 6))

    def _build_spectrum(self) -> None:
        panel = self._panel("RF SPECTRUM / MPX", "spectrum")
        self.spectrum = SpectrumView(panel, bins=rx.SPECTRUM_BINS,
                                     span_hz=rx.SPECTRUM_SPAN_HZ,
                                     hd_low_hz=hd_radio.SIDEBAND_LOW_HZ,
                                     hd_high_hz=hd_radio.SIDEBAND_HIGH_HZ)
        self.spectrum.pack(fill=tk.X, padx=4, pady=(4, 0))
        self.mpx = MpxView(panel, bins=rx.MPX_BINS, span_hz=rx.MPX_SPAN_HZ)
        self.mpx.pack(fill=tk.X, padx=4, pady=(0, 4))

    def _build_dx(self) -> None:
        panel = self._panel("DX / INTERFERENCE", "dx")
        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill=tk.X, padx=8, pady=(4, 0))
        tk.Label(row, text="Bandwidth", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 4))
        labels = [self._bandwidth_label(key) for key in dx.BANDWIDTHS_HZ]
        menu = tk.OptionMenu(row, self.dx_bandwidth_var, *labels,
                             command=lambda _value: self._apply_dx())
        menu.config(bg=PANEL, fg=FG, activebackground=PANEL,
                    activeforeground=FG, highlightthickness=0, bd=0,
                    font=("Segoe UI", 9), width=22, anchor="w")
        menu["menu"].config(bg=PANEL, fg=FG, activebackground=ACCENT,
                            activeforeground=FG, font=("Segoe UI", 9))
        menu.pack(side=tk.LEFT)
        tk.Label(row, text="56-80 deep DX | 110-130 DX+RDS | 150-180 clear slot"
                 " | 200-230 broadcast/HD", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(10, 0))
        row2 = tk.Frame(panel, bg=PANEL)
        row2.pack(fill=tk.X, padx=8, pady=(2, 0))
        tk.Label(row2, text="Protect from", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 4))
        for value, text in (("off", "Off"), ("auto", "Auto"),
                            ("low", "Low side (-200k)"),
                            ("high", "High side (+200k)")):
            tk.Radiobutton(row2, text=text, variable=self.dx_side_var,
                           value=value, command=self._apply_dx, bg=PANEL,
                           fg=FG, selectcolor=ACCENT, activebackground=PANEL,
                           activeforeground=FG).pack(side=tk.LEFT)
        tk.Checkbutton(row2, text="Canceller", variable=self.dx_canceller_var,
                       command=self._apply_dx, bg=PANEL, fg=FG,
                       selectcolor=ACCENT, activebackground=PANEL,
                       activeforeground=FG).pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(panel, textvariable=self.dx_status_var, bg=PANEL, fg=DIM,
                 font=("Consolas", 8), anchor="w", justify="left").pack(
            fill=tk.X, padx=8, pady=(2, 6))

    @staticmethod
    def _bandwidth_label(key) -> str:
        key = dx.normalize_bandwidth(key)
        return f"{key} kHz  ({dx.bandwidth_group(key)})"

    def _apply_dx(self) -> None:
        self.receiver.set_dx(bandwidth=self.dx_bandwidth_var.get().split()[0],
                             side=self.dx_side_var.get(),
                             canceller=self.dx_canceller_var.get())

    def _dx_status(self, status: dict) -> str:
        info = status.get("dx") or {}
        band = info.get("passband_hz")
        band_text = (f"{band[0] / 1e3:+.0f}..{band[1] / 1e3:+.0f} kHz"
                     if band else "--")
        side = info.get("active_side")
        if info.get("side") == "auto":
            side_text = f"auto -> {side or 'none'}"
        else:
            side_text = side or info.get("side") or "off"
        low = _fmt(info.get("neighbour_low_db"), "{:.0f}")
        high = _fmt(info.get("neighbour_high_db"), "{:.0f}")
        text = (f"passband {band_text} | protecting {side_text}"
                f" | neighbours -200k {low} / +200k {high} dB")
        if info.get("canceller"):
            text += (f" | canceller {_fmt(info.get('reduction_db'), '{:.1f} dB')}"
                     if side else " | canceller idle (no protected side)")
        return text

    def _build_gain_audio(self) -> None:
        panel = self._panel("GAIN", "gain")
        radio_row = tk.Frame(panel, bg=PANEL)
        radio_row.pack(fill=tk.X, padx=8, pady=(4, 0))
        tk.Label(radio_row, text="Radio", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 4))
        self.radio_combo = ttk.Combobox(radio_row, textvariable=self.radio_var,
                                        state="readonly", width=30)
        self.radio_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.radio_combo.bind("<<ComboboxSelected>>",
                              lambda _e: self._apply_radio())
        self._button(radio_row, "Find", self._refresh_radios).pack(
            side=tk.LEFT, padx=(4, 0))
        self.radio_hint_var = tk.StringVar(value="")
        tk.Label(panel, textvariable=self.radio_hint_var, bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8), justify=tk.LEFT, anchor="w",
                 wraplength=300).pack(fill=tk.X, padx=8)
        self.root.after(200, self._refresh_radios)
        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill=tk.X, padx=8, pady=4)
        tk.Radiobutton(row, text="Manual", variable=self.gain_mode_var,
                       value="manual", command=self._apply_gain_mode,
                       bg=PANEL, fg=FG, selectcolor=ACCENT,
                       activebackground=PANEL, activeforeground=FG).pack(
            side=tk.LEFT)
        tk.Radiobutton(row, text="Automatic", variable=self.gain_mode_var,
                       value="auto", command=self._apply_gain_mode,
                       bg=PANEL, fg=FG, selectcolor=ACCENT,
                       activebackground=PANEL, activeforeground=FG).pack(
            side=tk.LEFT, padx=(6, 12))
        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill=tk.X, padx=8)
        tk.Checkbutton(row, text="Remember gain per frequency",
                       variable=self.gain_memory_var,
                       command=self._toggle_gain_memory,
                       bg=PANEL, fg=FG, selectcolor=ACCENT,
                       activebackground=PANEL,
                       activeforeground=FG).pack(side=tk.LEFT)
        self._button(row, "Save gain", self._save_gain).pack(
            side=tk.LEFT, padx=(6, 0))
        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill=tk.X, padx=8, pady=(0, 2))
        tk.Label(row, textvariable=self.gain_label_var, bg=PANEL, fg=OK,
                 font=("Consolas", 11, "bold"), width=13).pack(side=tk.LEFT)
        self._button(row, "-", lambda: self._nudge_gain(-1), width=2).pack(
            side=tk.LEFT, padx=2)
        self._button(row, "+", lambda: self._nudge_gain(+1), width=2).pack(
            side=tk.LEFT, padx=2)

        self.gain_scale = tk.Scale(panel, from_=0.0, to=49.6, resolution=0.1,
                                   orient=tk.HORIZONTAL,
                                   variable=self.gain_var,
                                   command=self._on_gain_slider,
                                   bg=PANEL, fg=FG, troughcolor=ACCENT,
                                   highlightthickness=0, showvalue=False)
        self.gain_scale.pack(fill=tk.X, padx=8, pady=(0, 6))
        self._wheel(self.gain_scale, self._wheel_gain)

        bias = self._panel("ANTENNA POWER", "antenna")
        bias_row = tk.Frame(bias, bg=PANEL)
        bias_row.pack(fill=tk.X, padx=8, pady=(4, 8))
        tk.Checkbutton(bias_row, text="Bias-T (4.5 V on antenna port)",
                       variable=self.bias_var, command=self._apply_bias_tee,
                       bg=PANEL, fg=FG, selectcolor=ACCENT,
                       activebackground=PANEL,
                       activeforeground=FG).pack(side=tk.LEFT)
        tk.Label(bias_row, textvariable=self.bias_status_var, bg=PANEL,
                 fg=DIM, font=("Consolas", 8)).pack(side=tk.LEFT, padx=10)

        audio = self._panel("AUDIO", "audio")
        row = tk.Frame(audio, bg=PANEL)
        row.pack(fill=tk.X, padx=8, pady=4)
        tk.Checkbutton(row, text="Audio", variable=self.audio_var,
                       command=self._apply_audio, bg=PANEL, fg=FG,
                       selectcolor=ACCENT, activebackground=PANEL,
                       activeforeground=FG).pack(side=tk.LEFT)
        tk.Checkbutton(row, text="Stereo", variable=self.stereo_var,
                       command=self._apply_stereo, bg=PANEL, fg=FG,
                       selectcolor=ACCENT, activebackground=PANEL,
                       activeforeground=FG).pack(side=tk.LEFT, padx=8)
        tk.Label(row, text="Volume", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(8, 2))
        volume_scale = tk.Scale(
            row, from_=0.0, to=3.0, resolution=0.1,
            orient=tk.HORIZONTAL, variable=self.volume_var,
            command=lambda _v: self.receiver.set_volume(
                self.volume_var.get()),
            bg=PANEL, fg=FG, troughcolor=ACCENT, highlightthickness=0,
            length=140, showvalue=False)
        volume_scale.pack(side=tk.LEFT)
        self._wheel(volume_scale, self._wheel_volume)

        src_row = tk.Frame(audio, bg=PANEL)
        src_row.pack(fill=tk.X, padx=8, pady=(0, 4))
        tk.Label(src_row, text="Listen to", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 6))
        self.source_var = tk.StringVar(value=self.receiver.audio_source)
        for value, text in (("main", "Main programme"),
                            ("sca67", "SCA 67 kHz"),
                            ("sca92", "SCA 92 kHz")):
            tk.Radiobutton(src_row, text=text, value=value,
                           variable=self.source_var,
                           command=self._apply_source, bg=PANEL, fg=FG,
                           selectcolor=ACCENT, activebackground=PANEL,
                           activeforeground=FG).pack(side=tk.LEFT, padx=(0, 6))
        self.sca_status_var = tk.StringVar(value="")
        self.sca_status_label = tk.Label(
            src_row, textvariable=self.sca_status_var, bg=PANEL, fg=DIM,
            font=("Consolas", 9, "bold"))
        self.sca_status_label.pack(side=tk.LEFT, padx=8)

        hd_box = tk.Frame(audio, bg=PANEL)
        hd_box.pack(fill=tk.X)
        self._hd_controls = tk.Frame(hd_box, bg=PANEL)
        self._hd_controls.pack(fill=tk.X)
        hd_row = tk.Frame(self._hd_controls, bg=PANEL)
        hd_row.pack(fill=tk.X, padx=8, pady=(0, 2))
        tk.Label(hd_row, text="HD Radio", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 6))
        hd_row_b = tk.Frame(self._hd_controls, bg=PANEL)
        hd_row_b.pack(fill=tk.X, padx=(62, 8), pady=(0, 2))
        for i, (value, text) in enumerate((
                ("auto", "Auto"), ("manual", "Manual"),
                ("hd_only", "HD only"), ("analog", "Analog only"))):
            tk.Radiobutton(hd_row if i < 2 else hd_row_b, text=text,
                           value=value,
                           variable=self.hd_mode_var,
                           command=self._apply_hd_mode, bg=PANEL, fg=FG,
                           selectcolor=ACCENT, activebackground=PANEL,
                           activeforeground=FG).pack(side=tk.LEFT, padx=(0, 4))
        hd_row2 = tk.Frame(self._hd_controls, bg=PANEL)
        hd_row2.pack(fill=tk.X, padx=8, pady=(0, 2))
        tk.Label(hd_row2, text="Channel", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 4))
        self.hd_program_combo = ttk.Combobox(
            hd_row2, textvariable=self.hd_program_var, state="readonly",
            width=18, values=[_hd_program_label(None)])
        self.hd_program_combo.pack(side=tk.LEFT)
        self.hd_program_combo.bind("<<ComboboxSelected>>",
                                   lambda _e: self._apply_hd_program())
        hd_row3 = self._hd_add_row = tk.Frame(hd_box, bg=PANEL)
        hd_row3.pack(fill=tk.X, padx=8, pady=(0, 2))
        self._button(hd_row3, "Add HD decoder\u2026",
                     self._add_hd_decoder).pack(side=tk.LEFT, padx=6)
        self.hd_mer_label = tk.Label(hd_box, text="", bg=PANEL, fg=FG,
                                     font=("Consolas", 9, "bold"), anchor="w")
        self.hd_info_label = tk.Label(
            hd_box, textvariable=self.hd_info_var, bg=PANEL, fg=FG,
            font=("Consolas", 9), anchor="w", justify="left", wraplength=320)
        self.hd_info_label.pack(fill=tk.X, padx=8, pady=(0, 4))

        row2 = tk.Frame(audio, bg=PANEL)
        row2.pack(fill=tk.X, padx=8, pady=(0, 8))
        tk.Label(row2, text="Output device", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 6))
        self.device_combo = ttk.Combobox(row2, textvariable=self.device_var,
                                         state="readonly", width=44)
        self.device_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.device_combo.bind("<<ComboboxSelected>>",
                               lambda _e: self._apply_audio_device())
        self._button(row2, "Refresh", self._refresh_audio_devices).pack(
            side=tk.LEFT, padx=4)
        self._refresh_audio_devices()

    def _save_hd_settings(self) -> None:
        self.layout.hd_mode = self.receiver.hd_mode
        self.layout.hd_program = self.receiver.hd_program
        self.layout.save()

    def _apply_hd_mode(self) -> None:
        self.receiver.set_hd_mode(self.hd_mode_var.get())
        if (self.receiver.hd_mode in ("auto", "hd_only")
                and self.receiver.hd_program is None):
            self.receiver.set_hd_program(0)
            self.hd_program_var.set(_hd_program_label(0))
        self._save_hd_settings()

    def _apply_hd_program(self) -> None:
        label = self.hd_program_var.get()
        match = re.match(r"HD(\d)", label)
        program = int(match.group(1)) - 1 if match else None
        if program is None and self.receiver.hd_mode != "manual":
            # Picking Analog only makes sense in Manual.
            self.receiver.set_hd_mode("manual")
            self.hd_mode_var.set("manual")
        self.receiver.set_hd_program(program)
        self._save_hd_settings()

    def _add_hd_decoder(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose the HD decoder add-on",
            filetypes=[("HD decoder add-on", "*.zip *.dll *.so *.dylib"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            folder = hd_radio.install_addon(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Add HD decoder", str(exc), parent=self.root)
            return
        self.receiver.reload_hd()
        self._show_hd_controls()
        messagebox.showinfo(
            "Add HD decoder",
            f"HD decoder added to\n{folder}\n\nIt stays there when you "
            "update the FM Tuner.", parent=self.root)

    def _update_hd_channel_buttons(self, known: dict) -> None:
        """Analog plus one button per HD channel found on this frequency,
        with the channel name once it decodes. Hidden without HD."""
        mode = self.receiver.hd_mode
        selected = None if mode == "analog" else self.receiver.hd_program
        chans = [(None, "Analog")]
        for n in sorted(known):
            name = (known[n].get("name") or "").strip()
            if len(name) > 14:
                name = name[:13] + "\u2026"
            chans.append((n, _hd_program_label(n)
                          + (f" \u00b7 {name}" if name else "")))
        shown = (tuple(chans), selected) if known else None
        if shown == self._hd_channels_shown:
            return
        self._hd_channels_shown = shown
        for button in self._hd_channel_buttons:
            button.destroy()
        self._hd_channel_buttons = []
        if not known:
            self.hd_channel_row.pack_forget()
            return
        for i, (n, text) in enumerate(chans):
            button = self._button(
                self.hd_channel_row, text,
                lambda n=n: self._pick_hd_channel(n),
                HD_CHANNEL_ON if n == selected else BUTTON)
            button.grid(row=i // HD_CHANNELS_PER_ROW,
                        column=i % HD_CHANNELS_PER_ROW, padx=2, pady=1,
                        sticky="w")
            self._hd_channel_buttons.append(button)
        self.hd_channel_row.pack(fill=tk.X, padx=8, pady=(0, 8),
                                 before=self._hd_seek_row)

    def _pick_hd_channel(self, program: int | None) -> None:
        label = _hd_program_label(program)
        self.hd_program_var.set(next(
            (x for x in self.hd_program_combo["values"]
             if x.split("  ")[0] == label), label))
        if program is not None and self.receiver.hd_mode == "analog":
            self.receiver.set_hd_mode("manual")
            self.hd_mode_var.set("manual")
        self._apply_hd_program()

    def _update_hd_panel(self, hd: dict) -> None:
        # Only the channels decoded on this frequency are offered.
        known = {p["program"]: p for p in hd.get("programs") or []}
        self._update_hd_channel_buttons(known if hd.get("installed") else {})
        names = [_hd_program_label(None)]
        for n in sorted(known):
            extra = known[n].get("name") or known[n].get("type") or ""
            names.append(_hd_program_label(n) + (f"  {extra}" if extra else ""))
        if tuple(self.hd_program_combo["values"]) != tuple(names):
            self.hd_program_combo["values"] = names
            prog = self.receiver.hd_program
            label = _hd_program_label(prog)
            self.hd_program_var.set(next(
                (x for x in names if x.split("  ")[0] == label), label))
        mer = hd.get("mer_db") if hd.get("synced") else None
        if mer is None or not self.hd_info_label.winfo_manager():
            self.hd_mer_label.pack_forget()
        else:
            self.hd_mer_label.config(
                text=f"MER {mer:.1f} dB  {hd_radio.mer_grade(mer)}",
                fg=MER_COLOURS.get(hd_radio.mer_grade(mer), FG))
            if not self.hd_mer_label.winfo_ismapped():
                self.hd_mer_label.pack(fill=tk.X, padx=8,
                                       before=self.hd_info_label)
        if not hd.get("installed"):
            self.hd_info_var.set(
                (hd.get("error") or "HD decoder not installed")
                + " - analog FM works as normal. See README: HD Radio "
                  "(optional).")
            return
        if not hd.get("synced"):
            self.hd_info_var.set("")  # HD details only while locked
            return
        playing = {"hd": "playing HD", "analog": "playing analog",
                   "silence": "HD audio not ready"}.get(hd.get("playing"), "")
        line = " | ".join(x for x in (
            hd.get("call"), hd.get("slogan"),
            f"BER {hd['ber']:.4f}" if hd.get("ber") is not None else None,
            playing) if x)
        prog = self.receiver.hd_program or 0
        p = known.get(prog) or {}
        song = " - ".join(x for x in (p.get("title"), p.get("artist")) if x)
        if song:
            line += f"\nHD{prog + 1}: {song}"
        if hd.get("alert"):
            line += f"\nALERT: {hd['alert']}"
        self.hd_info_var.set(line)

    def _build_rds(self) -> None:
        panel = self._panel("LIVE RDS", "rds")
        grid = tk.Frame(panel, bg=PANEL)
        grid.pack(fill=tk.X, padx=8, pady=4)
        tk.Label(grid, text="PI", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).grid(row=0, column=0, sticky="w")
        tk.Label(grid, textvariable=self.pi_var, bg=PANEL, fg=FG,
                 font=("Consolas", 14, "bold")).grid(row=1, column=0,
                                                     sticky="w", padx=(0, 20))
        tk.Label(grid, text="PS", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).grid(row=0, column=1, sticky="w")
        tk.Label(grid, textvariable=self.ps_var, bg=PANEL, fg=OK,
                 font=("Consolas", 14, "bold")).grid(row=1, column=1,
                                                     sticky="w")
        tk.Label(grid, textvariable=self.pty_var, bg=PANEL, fg=DIM,
                 font=("Segoe UI", 9)).grid(row=1, column=2, sticky="e",
                                            padx=10)
        grid.columnconfigure(2, weight=1)

        tk.Label(panel, textvariable=self.rt_var, bg=PANEL, fg=FG,
                 font=("Segoe UI", 10), anchor="w", justify="left",
                 wraplength=520).pack(fill=tk.X, padx=8)
        tk.Label(panel, textvariable=self.clock_var, bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8), anchor="w").pack(fill=tk.X, padx=8,
                                                        pady=(0, 6))

        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill=tk.X, padx=8, pady=(0, 6))
        self._button(row, "Record MPX (30 s)",
                     lambda: self._record_mpx(30.0)).pack(side=tk.LEFT)
        self._button(row, "Record MPX (1 min)",
                     lambda: self._record_mpx(60.0)).pack(side=tk.LEFT,
                                                          padx=(6, 0))
        self._button(row, "Record MPX (5 min)",
                     lambda: self._record_mpx(300.0)).pack(side=tk.LEFT,
                                                           padx=(6, 0))
        self._button(row, "Stop", self._stop_record_mpx).pack(side=tk.LEFT,
                                                              padx=(6, 0))
        tk.OptionMenu(row, self.record_fmt_var,
                      *RECORD_FORMATS).pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(row, textvariable=self.record_var, bg=PANEL, fg=DIM,
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=8)

        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill=tk.X, padx=8, pady=(0, 6))
        tk.Checkbutton(row, text="RDS2 streams", variable=self.rds2_var,
                       command=self._apply_rds2, bg=PANEL, fg=FG,
                       selectcolor=ACCENT, activebackground=PANEL,
                       activeforeground=FG).pack(side=tk.LEFT)
        tk.Label(row, textvariable=self.rds2_status_var, bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8), anchor="w").pack(side=tk.LEFT, padx=8)

    def _record_mpx(self, seconds: float) -> None:
        """Capture raw multiplex so a station that will not decode can be
        analysed offline."""
        fmt = "wav" if self.record_fmt_var.get() == RECORD_FORMATS[0] \
            else "f32"
        name = time.strftime(f"mpx_%Y%m%d_%H%M%S.{fmt}")
        # os.access() lies on Windows (it calls System32 writable), so try
        # the folders for real: next to the app first, then the home
        # directory, then temp.
        folders = [os.getcwd(),
                   os.path.dirname(os.path.abspath(__file__)),
                   os.path.expanduser("~"),
                   tempfile.gettempdir()]
        path = None
        for folder in folders:
            candidate = os.path.join(folder, name)
            try:
                with open(candidate, "wb"):
                    pass
                path = candidate
                break
            except OSError:
                continue
        if path is None:
            self.record_var.set("recording failed: nowhere writable")
            return
        try:
            self.receiver.record_mpx(path, seconds, fmt)
        except Exception as exc:
            self.record_var.set(f"recording failed: {exc}")
            return
        self._record_path = path
        self.record_var.set(f"REC 0:00 / {_mmss(seconds)}  {path}")

    def _stop_record_mpx(self) -> None:
        if self.receiver.recording_mpx():
            progress = self.receiver.mpx_recording_progress()
            self.receiver.stop_mpx_recording()
            done = _mmss(progress[0]) if progress else "?"
            self.record_var.set(f"stopped after {done}  {self._record_path}")

    def _update_record_timer(self) -> None:
        progress = self.receiver.mpx_recording_progress()
        if progress is not None:
            elapsed, total = progress
            self.record_var.set(
                f"REC {_mmss(elapsed)} / {_mmss(total)}"
                f"  ({_mmss(total - elapsed)} left)  {self._record_path}")
        elif self.receiver.mpx_recording_finished() \
                and self.record_var.get().startswith("REC "):
            self.record_var.set(
                f"done  {self.receiver.mpx_recording_finished()}")

    def _build_servers(self) -> None:
        panel = self._panel("LOCALHOST SERVICES", "servers")
        tk.Label(panel, textvariable=self.server_var, bg=PANEL, fg=DIM,
                 font=("Consolas", 8), anchor="w", justify="left",
                 wraplength=640).pack(
            fill=tk.X, padx=8, pady=(0, 6))

    def _build_remote(self) -> None:
        panel = self._panel("REMOTE (PHONE / TABLET / PC)", "remote")
        tk.Checkbutton(panel, text="Allow remotes on my home network",
                       variable=self.remote_var, command=self._apply_remote,
                       bg=PANEL, fg=FG, selectcolor=ACCENT,
                       activebackground=PANEL, activeforeground=FG,
                       font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8)
        self.local_play_var = tk.BooleanVar(value=True)
        self.local_play_check = tk.Checkbutton(panel, text="Play on this PC",
                       variable=self.local_play_var,
                       command=lambda: self.service.handle_command(
                           "LOCAL " + ("ON" if self.local_play_var.get()
                                       else "OFF")),
                       bg=PANEL, fg=FG, selectcolor=ACCENT,
                       activebackground=PANEL, activeforeground=FG,
                       font=("Segoe UI", 9))
        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill=tk.X, padx=8, pady=(2, 8))
        self._remote_row = row
        self.remote_qr = tk.Canvas(row, width=1, height=1, bg=PANEL,
                                   highlightthickness=0)
        self.remote_qr.pack(side=tk.LEFT)
        text = tk.Frame(row, bg=PANEL)
        text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0))
        tk.Label(text, textvariable=self.remote_url_var, bg=PANEL, fg=OK,
                 font=("Segoe UI", 12, "bold"), anchor="w",
                 justify="left").pack(fill=tk.X)
        tk.Label(text, textvariable=self.remote_info_var, bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8), anchor="w", justify="left",
                 wraplength=300).pack(fill=tk.X)
        self._show_remote_off()

    def _show_remote_off(self) -> None:
        # Never leave the PC silent once nobody can listen remotely.
        self.local_play_check.pack_forget()
        if not self.local_play_var.get():
            self.local_play_var.set(True)
            self.service.handle_command("LOCAL ON")
        self.remote_url_var.set("")
        self.remote_info_var.set(
            "Tick the box, then scan the QR code with your phone's or "
            "tablet's camera - iPhone, iPad or Android (or type the address "
            "into a browser on another PC or Mac). It's a web page, so any "
            "browser works. The device must be on the same Wi-Fi / home "
            "network.")
        self._draw_qr(None)

    def _apply_remote(self) -> None:
        on = bool(self.remote_var.get())
        self.layout.remote = on
        self.layout.save()
        srv = self.service.remote
        if not on:
            srv.stop()
            self._show_remote_off()
            return
        try:
            srv.start()
        except OSError as exc:
            self.remote_var.set(False)
            self.layout.remote = False
            self.layout.save()
            self.remote_url_var.set("")
            self.remote_info_var.set(str(exc))
            return
        self.local_play_check.pack(anchor="w", padx=8,
                                   before=self._remote_row)
        urls = srv.urls()
        if not urls:
            self.remote_url_var.set(f"port {srv.port}")
            self.remote_info_var.set("No home network found on this PC.")
            self._draw_qr(None)
            return
        self.remote_url_var.set("\n".join(urls))
        self.remote_info_var.set(
            "Scan with your phone's camera. If Windows asks about the "
            "firewall, tick Private networks and click Allow.")
        self._draw_qr(urls[0])

    def _draw_qr(self, text: str | None) -> None:
        if text == self._remote_qr_text:
            return
        self._remote_qr_text = text
        canvas = self.remote_qr
        canvas.delete("all")
        if not text:
            canvas.config(width=1, height=1)
            return
        try:
            matrix = remote.qr_matrix(text)
        except Exception as exc:
            self._log(f"[warning] QR code: {exc}")
            canvas.config(width=1, height=1)
            return
        cell, quiet = 4, 4
        size = (len(matrix) + 2 * quiet) * cell
        canvas.config(width=size, height=size)
        canvas.create_rectangle(0, 0, size, size, fill="white", width=0)
        for y, line in enumerate(matrix):
            for x, dark in enumerate(line):
                if dark:
                    px, py = (x + quiet) * cell, (y + quiet) * cell
                    canvas.create_rectangle(px, py, px + cell, py + cell,
                                            fill="black", width=0)

    def _update_remote(self, status: dict) -> None:
        info = status.get("remote") or {}
        if info.get("enabled") and self._remote_qr_text:
            count = info.get("listeners") or 0
            self.remote_info_var.set(
                ("1 device listening" if count == 1 else
                 f"{count} devices listening" if count else
                 "Scan with your phone's camera. If Windows asks about the "
                 "firewall, tick Private networks and click Allow."))

    # -- hotkeys -----------------------------------------------------------

    # Widgets that use the arrow keys themselves.
    _ARROW_WIDGETS = ("Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox",
                      "Text", "Listbox", "Scale", "TScale")

    def _bind_hotkeys(self) -> None:
        for key, action in (("<Up>", lambda: self._step(+1)),
                            ("<Down>", lambda: self._step(-1)),
                            ("<Left>", lambda: self._preset_step(-1)),
                            ("<Right>", lambda: self._preset_step(+1))):
            self.root.bind_all(key, lambda e, a=action: self._hotkey(e, a))

    def _hotkey(self, event, action):
        if not self._hotkeys_var.get():
            return None
        try:
            if event.widget.winfo_class() in self._ARROW_WIDGETS:
                return None
            if event.widget.winfo_toplevel() is not self.root:
                return None
        except (tk.TclError, AttributeError):
            return None
        action()
        return "break"

    def _change_signal_units(self) -> None:
        self.layout.signal_units = self._units_var.get()
        self.service.signal_units = self.layout.signal_units
        self.layout.save()

    def _toggle_hotkeys(self) -> None:
        self.layout.hotkeys = bool(self._hotkeys_var.get())
        self.layout.save()

    def _preset_step(self, direction: int) -> None:
        try:
            item, freq = self.service.preset_step(direction)
        except (PresetError, band.FrequencyError) as exc:
            self.preset_status_var.set(str(exc))
            return
        self._selected_preset = item["name"]
        self.freq_var.set(band.fmt(freq))
        self._render_presets()

    def _build_log(self) -> None:
        panel = self._panel("LOG", "log")
        self.log = tk.Text(panel, height=6, bg="#0d0d18", fg=DIM,
                           font=("Consolas", 8), relief=tk.FLAT, wrap="word")
        self.log.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.log.configure(state="disabled")

    # -- actions -----------------------------------------------------------

    def event(self, kind: str, message: str) -> None:
        """Thread-safe log sink handed to the service."""
        try:
            self._events.put_nowait((kind, message))
        except queue.Full:
            pass

    def _log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        if float(self.log.index("end-1c").split(".")[0]) > 300:
            self.log.delete("1.0", "100.0")
        self.log.configure(state="disabled")

    def _pump_events(self) -> None:
        while True:
            try:
                kind, message = self._events.get_nowait()
            except queue.Empty:
                break
            self._log(f"[{kind}] {message}")
        self.root.after(250, self._pump_events)

    def _start(self, frequency: float | None = None) -> None:
        try:
            self.receiver.start(frequency)
        except RtlSdrError as exc:
            self._log(f"[error] {exc}")
            messagebox.showerror("Radio", str(exc))

    def _toggle_power(self) -> None:
        if self.receiver.running:
            self.receiver.stop()
            self.spectrum.clear()
            self.mpx.clear()
        else:
            try:
                freq = band.validate(self.freq_var.get())
            except band.FrequencyError:
                freq = self.receiver.frequency
            self._start(freq)

    def _tune_entry(self) -> None:
        try:
            freq = self.receiver.tune(self.freq_var.get())
        except band.FrequencyError as exc:
            messagebox.showerror("Invalid frequency", str(exc))
            self.freq_var.set(band.fmt(self.receiver.frequency))
            return
        self.freq_var.set(band.fmt(freq))

    def _ask_region(self) -> str:
        """First start of a new install: pick the band plan."""
        self.root.withdraw()
        win = tk.Toplevel(self.root, bg=BG)
        win.title("FM Tuner - choose your region")
        win.resizable(False, False)
        choice = tk.StringVar(master=self.root, value=band.DEFAULT_PLAN)
        tk.Label(win, text="Choose your region", bg=BG, fg=FG,
                 font=("Segoe UI", 14, "bold")).pack(padx=20, pady=(16, 4),
                                                     anchor="w")
        tk.Label(win, text="This sets the FM band and the tuning steps.\n"
                           "You can change it later in the Region menu.",
                 bg=BG, fg=DIM, font=("Segoe UI", 9),
                 justify="left").pack(padx=20, anchor="w")
        for key, plan in band.PLANS.items():
            tk.Radiobutton(win, text=plan[0], value=key, variable=choice,
                           bg=BG, fg=FG, selectcolor=ACCENT,
                           activebackground=BG, activeforeground=FG,
                           font=("Segoe UI", 10), anchor="w",
                           cursor="hand2").pack(fill=tk.X, padx=20, pady=2)
        tk.Button(win, text="OK", command=win.destroy, bg=ACCENT, fg=FG,
                  font=("Segoe UI", 10, "bold"), relief=tk.FLAT, padx=24,
                  cursor="hand2").pack(pady=(10, 16))
        win.bind("<Return>", lambda _e: win.destroy())
        win.grab_set()
        win.focus_force()
        self.root.wait_window(win)
        self.root.deiconify()
        return band.set_plan(choice.get())

    def _label_step_buttons(self) -> None:
        step = band.step_label()
        self._step_down_btn.config(text=f"\u25c0 -{step}")
        self._step_up_btn.config(text=f"+{step} \u25b6")

    def _change_region(self) -> None:
        key = band.set_plan(self._region_var.get())
        self.layout.band = key
        self.layout.save()
        self._label_step_buttons()
        freq = self.receiver.frequency
        if not band.is_valid(freq):
            freq = self.receiver.tune(band.start_mhz())
        self.freq_var.set(band.fmt(freq))
        self._show_hd_controls()
        self._log(f"[region] {band.PLANS[key][0]}")

    def _step(self, direction: int) -> None:
        freq = self.receiver.step(direction)
        self.freq_var.set(band.fmt(freq))

    def _seek(self, direction: int, hd: bool = False) -> None:
        try:
            self.receiver.seek(direction, hd=hd)
        except RtlSdrError as exc:
            messagebox.showinfo("HD Seek" if hd else "Seek", str(exc))

    def _stop_seek(self) -> None:
        self.receiver.stop_seek()

    # -- presets -----------------------------------------------------------

    def _render_presets(self) -> None:
        """Rebuild the preset buttons from the store."""
        for button in getattr(self, "_preset_buttons", []):
            button.destroy()
        self._preset_buttons = []
        presets = self.service.presets.list()
        names = {item["name"] for item in presets}
        if self._selected_preset not in names:
            self._selected_preset = None
        if not presets:
            self.preset_status_var.set(
                "no presets yet \u2014 tune a station and hit Save current")
        elif self._selected_preset is None:
            self.preset_status_var.set(
                "click to tune, right-click to select")
        for item in presets:
            label = f"{item['name']}\n{band.fmt(item['frequency_mhz'])}"
            selected = item["name"] == self._selected_preset
            on_air = (self._tuned_mhz is not None and
                      abs(item["frequency_mhz"] - self._tuned_mhz) < 0.005)
            if selected:
                bg, fg = OK, "#10241c"
            elif on_air:
                bg, fg = ACCENT, "white"
            else:
                bg, fg = BUTTON, "white"
            button = tk.Button(
                self._preset_row, text=label,
                command=lambda p=item: self._recall_preset(p),
                bg=bg, fg=fg,
                font=("Segoe UI", 8, "bold"), relief=tk.FLAT, width=9,
                cursor="hand2", activebackground=ACCENT,
                activeforeground="white")
            button.pack(side=tk.LEFT, padx=2, pady=2)
            button.bind("<Button-3>",
                        lambda _e, p=item: self._select_preset(p))
            self._preset_buttons.append(button)

    def _select_preset(self, item: dict) -> None:
        self._selected_preset = item["name"]
        self.preset_status_var.set(f"selected {item['name']}")
        self._render_presets()

    def _recall_preset(self, item: dict) -> None:
        self._select_preset(item)
        self.service._preset_index = self.service._index_of(item)
        try:
            freq = self.receiver.tune(item["frequency_mhz"])
        except band.FrequencyError as exc:
            messagebox.showerror("Preset", str(exc))
            return
        self.freq_var.set(band.fmt(freq))

    def _current_preset_key(self) -> str | None:
        if self._selected_preset is None:
            self.preset_status_var.set("right-click a preset first")
            return None
        return self._selected_preset

    def _save_preset(self) -> None:
        reply = self.service.handle_command("PRESET SAVE")
        if not reply.get("ok"):
            messagebox.showerror("Preset", reply.get("error", "failed"))
            return
        item = reply["preset"]
        self._selected_preset = item["name"]
        self.preset_status_var.set(
            f"saved {item['name']} ({band.fmt(item['frequency_mhz'])})")
        self._render_presets()

    def _rename_preset(self) -> None:
        key = self._current_preset_key()
        if key is None:
            return
        name = simpledialog.askstring("Rename preset", "Preset name:",
                                      initialvalue=key, parent=self.root)
        if not name:
            return
        try:
            item = self.service.presets.rename(key, name)
        except PresetError as exc:
            messagebox.showerror("Preset", str(exc))
            return
        self._selected_preset = item["name"]
        self.preset_status_var.set(f"renamed to {item['name']}")
        self._render_presets()

    def _delete_preset(self) -> None:
        key = self._current_preset_key()
        if key is None:
            return
        if not messagebox.askyesno("Delete preset",
                                   f"Remove preset {key}?"):
            return
        try:
            self.service.presets.remove(key)
        except PresetError as exc:
            messagebox.showerror("Preset", str(exc))
            return
        self._selected_preset = None
        self.preset_status_var.set(f"deleted {key}")
        self._render_presets()

    def _move_preset(self, offset: int) -> None:
        key = self._current_preset_key()
        if key is None:
            return
        try:
            self.service.presets.move(key, offset)
        except PresetError as exc:
            messagebox.showerror("Preset", str(exc))
            return
        self._render_presets()

    def _apply_gain_mode(self) -> None:
        manual = self.gain_mode_var.get() == "manual"
        self.receiver.set_gain_mode(manual)
        self.gain_scale.configure(state="normal" if manual else "disabled")
        if manual:
            if not self._recall_gain():
                self.receiver.set_gain_db(self.gain_var.get())

    def _on_gain_slider(self, _value) -> None:
        if self.gain_mode_var.get() != "manual":
            return
        self.receiver.set_gain_db(self.gain_var.get())

    @staticmethod
    def _wheel(widget: tk.Widget, step) -> None:
        """Mouse wheel over a slider: up/right = +1 step, down = -1."""
        def on_wheel(event):
            if getattr(event, "num", None) in (4, 5):
                step(1 if event.num == 4 else -1)
            elif event.delta:
                step(1 if event.delta > 0 else -1)
            return "break"
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(sequence, on_wheel)

    def _wheel_gain(self, direction: int) -> None:
        if self.gain_mode_var.get() == "manual":
            self._nudge_gain(direction)

    def _wheel_volume(self, direction: int) -> None:
        volume = min(3.0, max(0.0, self.volume_var.get() + 0.1 * direction))
        self.volume_var.set(round(volume, 1))
        self.receiver.set_volume(self.volume_var.get())

    def _toggle_gain_memory(self) -> None:
        self.layout.gain_memory = bool(self.gain_memory_var.get())
        self.layout.save()
        if self.layout.gain_memory:
            self._recall_gain()

    def _save_gain(self) -> bool:
        """Save gain button: keep the slider's gain for this frequency.
        Moving the slider never saves on its own."""
        if not self.gain_memory_var.get():
            messagebox.showinfo(
                "Save gain", "Tick \"Remember gain per frequency\" first.",
                parent=self.root)
            return False
        if self.gain_mode_var.get() != "manual":
            messagebox.showinfo(
                "Save gain", "Gain memory only works on Manual gain.\n"
                "Choose Manual, set the gain, then click Save gain.",
                parent=self.root)
            return False
        freq, gain = self.receiver.frequency, self.gain_var.get()
        self.gain_memory.remember(freq, gain)
        self._log(f"[gain] {band.fmt(freq)} MHz: saved {gain:.1f} dB")
        return True

    def _recall_gain(self) -> bool:
        """Put back this frequency's saved manual gain, if there is one."""
        if (not self.gain_memory_var.get()
                or self.gain_mode_var.get() != "manual"):
            return False
        freq = self.receiver.frequency
        saved = self.gain_memory.get(freq)
        if saved is None:
            return False
        saved = min(saved, float(self.gain_scale.cget("to")))
        if (abs(self.gain_var.get() - saved) > 0.05
                or abs(self.receiver.gain_db - saved) > 0.6):
            self.gain_var.set(saved)
            self.receiver.set_gain_db(saved)
            self._log(f"[gain] {band.fmt(freq)} MHz: remembered "
                      f"{saved:.1f} dB")
        return True

    def _nudge_gain(self, direction: int) -> None:
        gains = self.receiver.supported_gains_db()
        current = self.gain_var.get()
        if gains:
            ordered = sorted(gains)
            if direction > 0:
                target = next((g for g in ordered if g > current + 1e-6),
                              ordered[-1])
            else:
                target = next((g for g in reversed(ordered)
                               if g < current - 1e-6), ordered[0])
        else:
            target = max(0.0, current + direction * 1.0)
        self.gain_var.set(round(target, 1))
        self.gain_mode_var.set("manual")
        self.receiver.set_gain_mode(True)
        self.receiver.set_gain_db(target)

    def _apply_bias_tee(self) -> None:
        enabled = self.bias_var.get()
        if enabled and not messagebox.askokcancel(
                "Bias-T",
                "Switch on 4.5 V DC on the antenna port?\n\n"
                "Only do this with a powered LNA or active antenna "
                "connected. A DC-shorted antenna or splitter can damage "
                "the dongle."):
            self.bias_var.set(False)
            return
        try:
            self.receiver.set_bias_tee(enabled)
        except RtlSdrError as exc:
            self.bias_var.set(False)
            self._log(f"[error] {exc}")
            messagebox.showerror("Bias-T", str(exc))

    def _apply_audio(self) -> None:
        self.receiver.set_audio_enabled(self.audio_var.get())

    def _apply_source(self) -> None:
        self.receiver.set_audio_source(self.source_var.get())

    def _apply_stereo(self) -> None:
        self.receiver.set_stereo(self.stereo_var.get())

    def _apply_rds2(self) -> None:
        self.receiver.set_rds2(self.rds2_var.get())
        self.layout.rds2 = self.rds2_var.get()
        self.layout.save()

    def _rds2_status(self, status: dict) -> str:
        if not status.get("rds2_enabled"):
            return "RDS2: off"
        hits = status.get("rds2_block_hits")
        if not hits:
            return "RDS2: waiting"
        streams = "  ".join(
            f"S{i + 1} {_fmt(h, '{:.0f}')}" for i, h in enumerate(hits))
        groups = status.get("rds2_groups", 0)
        state = ("active" if status.get("rds2_active")
                 else ("idle" if groups else "none heard"))
        return f"RDS2 {state}: sync hits {streams} | groups {groups}"

    def _refresh_audio_devices(self) -> None:
        self._audio_devices = rx.FMReceiver.audio_devices()
        names = ["System default"] + [d["name"] for d in self._audio_devices]
        self.device_combo["values"] = names
        if not self.device_var.get():
            self.device_combo.current(0)

    def _refresh_radios(self) -> None:
        """List the radios plugged in (in the background: SoapySDR can take
        a few seconds to look for them)."""
        self.radio_var.set("Looking for radios...")

        def find():
            radios = rx.FMReceiver.radios()
            hint = rx.FMReceiver.radio_hint(radios)
            try:
                self.root.after(0, lambda: (self._show_radios(radios),
                                            self.radio_hint_var.set(hint)))
            except (tk.TclError, RuntimeError):
                pass

        threading.Thread(target=find, name="find-radios", daemon=True).start()

    def _show_radios(self, radios: list[tuple[str, str]]) -> None:
        current = self.receiver.radio or f"rtl:{self.receiver.device_index}"
        if not any(spec == current for spec, _ in radios):
            label = ("RTL-SDR" if current.startswith("rtl:")
                     else current.split("driver=")[-1].split(",")[0]
                     + " (not found)")
            radios = [(current, label)] + radios
        self._radios = radios
        self.radio_combo["values"] = [label for _, label in radios]
        index = next(i for i, (spec, _) in enumerate(radios)
                     if spec == current)
        self.radio_combo.current(index)

    def _apply_radio(self) -> None:
        index = self.radio_combo.current()
        if not 0 <= index < len(self._radios):
            return
        spec = self._radios[index][0]
        if spec == (self.receiver.radio or f"rtl:{self.receiver.device_index}"):
            return
        self.layout.radio = spec
        self.layout.save()
        was_running = self.receiver.running
        if was_running:
            self.receiver.stop()
            self.spectrum.clear()
            self.mpx.clear()
        self.receiver.set_radio(spec)
        self._log(f"[radio] {self._radios[index][1]}")
        if was_running:
            self._start(self._tuned_mhz or band.validate(self.freq_var.get()))

    def _apply_audio_device(self) -> None:
        index = self.device_combo.current()
        device = None if index <= 0 else self._audio_devices[index - 1]["index"]
        self.receiver.set_audio_device(device)

    # -- periodic refresh --------------------------------------------------

    def _refresh(self) -> None:
        status = self.service.status()
        self.freq_display.config(text=f"{band.fmt(status['frequency_mhz'])} MHz")
        self._update_remote(status)

        # Presets can also be changed over the command port.
        signature = [(p["name"], p["frequency_mhz"])
                     for p in status.get("presets", [])]
        tuned = round(float(status["frequency_mhz"]), 3)
        if (signature != getattr(self, "_preset_signature", None)
                or tuned != self._tuned_mhz):
            self._preset_signature = signature
            tune_changed = tuned != self._tuned_mhz
            self._tuned_mhz = tuned
            self._render_presets()
            if tune_changed and status["state"] != "stopped":
                self._recall_gain()
        was_stopped = getattr(self, "_was_stopped", True)
        self._was_stopped = status["state"] == "stopped"
        if was_stopped and not self._was_stopped:
            self._recall_gain()
            if status["state"] != "stopped" and self.layout.last_frequency != tuned:
                self.layout.last_frequency = tuned
                self.layout.save()

        state = status["state"]
        label = {
            rx.ReceiverState.STOPPED: "Stopped",
            rx.ReceiverState.STARTING: "Starting\u2026",
            rx.ReceiverState.RUNNING: "Receiving",
            rx.ReceiverState.SEEKING: "Seeking\u2026",
            rx.ReceiverState.ERROR: "Error",
        }.get(state, state)
        if status["error"]:
            label = f"Error: {status['error']}"
        self.state_var.set(label)
        self.power_button.config(
            text="Stop Receiver" if self.receiver.running
            else "Start Receiver")

        signal = status["signal_dbfs"]
        stereo = status["stereo"]
        self.signal_var.set(
            f"Signal {format_signal(signal, self.layout.signal_units):>9}  "
            f"{'STEREO' if stereo else 'mono' if stereo is False else '--'}"
            if signal is not None else "Signal: unavailable")
        self._meter_level = _meter_level(signal)
        self._draw_meter()
        if stereo:
            badge, colour = "STEREO", METER_GREEN
        elif stereo is False:
            badge, colour = "MONO", METER_YELLOW
        else:
            badge, colour = "", DIM
        self.stereo_badge_var.set(badge)
        self.stereo_label.config(fg=colour)
        hd = status.get("hd") or {}
        badge, colour = "", DIM
        if hd.get("synced"):
            badge, colour = "HD", METER_YELLOW
            if hd.get("playing") == "hd":
                prog = self.receiver.hd_program or 0
                badge, colour = f"HD{prog + 1}", METER_GREEN
        self.hd_badge_var.set(badge)
        self.hd_label.config(fg=colour)
        self._update_hd_panel(hd)
        self._update_record_timer()
        wide_dbfs = status.get("wide_dbfs")
        overload = status.get("overload", "ok") if wide_dbfs is not None else None
        wide = _fmt(wide_dbfs, "{:.0f} dBFS")
        self.overload_var.set(
            {"ok": f"ADC ok {wide}", "warn": f"ADC near clip {wide}",
             "overload": f"OVERLOAD {wide}"}.get(overload, "ADC --"))
        self.overload_label.config(
            fg={"ok": METER_GREEN, "warn": METER_YELLOW,
                "overload": METER_RED}.get(overload, DIM))
        self.dx_status_var.set(self._dx_status(status))

        self.detail_var.set(
            f"gain {status['gain_mode']}"
            f"{'' if status['gain_db'] is None else f' {status_gain(status)}'}"
            f" | clipping {_fmt(status['clipping'], '{:.2%}')}"
            f" | SNR {_fmt(status['snr_db'], '{:.1f} dB')}"
            f" | RSSI {_fmt(status['rssi_dbm'], '{:.1f} dBm')}\n"
            f"audio {'ready' if status['audio_ready'] else 'not ready'}"
            f" ({status['stereo_mode']}"
            + (f", {status.get('audio_rate')} Hz"
               if status.get("audio_rate") else "")
            + f", underruns {status['audio_underruns']})"
            f" | RDS {'ready' if status['rds_ready'] else 'waiting'}"
            f" | groups {status['groups_decoded']}"
            f" | block errors {_fmt(status['block_error_rate'], '{:.1%}')}"
            f"\nRDS carrier {_fmt(status.get('rds_snr_db'), '{:.1f} dB')}"
            f" | sync hits {_fmt(status.get('rds_block_hits'), '{:.1f}')}/1000"
            f" (noise under 10, decodable 40+)"
            f" | bits {status.get('rds_bits', 0)}"
            f" | dropped {status.get('rds_dropped', 0)}"
            f" | RDS2 skipped {status.get('rds2_skipped', 0)}"
            + (f"\naudio: {status['audio_error']}"
               if status["audio_error"] else ""))

        draw_spectrum = self.layout.visible.get("spectrum", True)
        spectrum = self.receiver.spectrum() if draw_spectrum else None
        if spectrum is not None:
            self.spectrum.update_spectrum(spectrum[1], status.get("hd"))
        mpx = self.receiver.mpx_spectrum() if draw_spectrum else None
        if mpx is not None:
            rds2 = status.get("mpx_rds2_db") or [None, None, None]
            self.mpx.update_spectrum(mpx[1], {
                "pilot": status["mpx_pilot_db"],
                "stereo": status["mpx_stereo_db"],
                "rds": status["mpx_rds_db"],
                "rds2_1": rds2[0], "sca": status["mpx_sca_db"],
                "rds2_2": rds2[1], "rds2_3": rds2[2],
                "sca92": status.get("mpx_sca92_db"),
                "hd": (status.get("hd") or {}).get("mer_db"),
            }, stereo=status["stereo"], rds_ready=status["rds_ready"],
                hd_detected=status.get("hd_detected"))

        source = status.get("audio_source") or "main"
        if self.source_var.get() != source:
            self.source_var.set(source)
        if source == "main":
            self.sca_status_var.set("")
        else:
            level = status.get("sca_level_db")
            present = status.get("sca_present")
            if level is None:
                text, colour = "SCA: measuring...", DIM
            elif present:
                text, colour = f"SCA PRESENT  {level:.0f} dB", METER_GREEN
            else:
                text, colour = f"no SCA carrier  {level:.0f} dB", METER_RED
            self.sca_status_var.set(text)
            self.sca_status_label.configure(fg=colour)

        self.pi_var.set(status["pi"] or "--")
        self.ps_var.set(status["ps"] or "--------")
        self.rt_var.set(status["radiotext"] or "")
        pty = status["pty_name"] or ""
        flags = " ".join(f for f, on in (("TP", status["tp"]),
                                         ("TA", status["ta"])) if on)
        self.pty_var.set(f"{pty} {flags}".strip())
        self.clock_var.set(status["clock_time"] or "")

        if status.get("bias_tee_supported") is False:
            self.bias_status_var.set(
                "not switchable on this radio"
                if str(status.get("radio", "")).startswith("soapy:")
                else "not supported by this rtlsdr.dll")
        else:
            self.bias_status_var.set(
                "4.5 V ON" if status.get("bias_tee") else "off")
        if bool(status.get("bias_tee")) != self.bias_var.get():
            self.bias_var.set(bool(status.get("bias_tee")))
        self.rds2_status_var.set(self._rds2_status(status))

        gains = status.get("supported_gains_db") or []
        top = max(gains) if gains else 49.6
        if abs(float(self.gain_scale.cget("to")) - top) > 0.01:
            self.gain_scale.configure(to=top)
        if self.receiver.running and self.gain_mode_var.get() == "manual":
            self.gain_label_var.set(f"{status['gain_db'] or 0.0:.1f} dB")
        elif self.gain_mode_var.get() == "manual":
            self.gain_label_var.set(f"{self.gain_var.get():.1f} dB")
        elif self.receiver.running and status["gain_db"] is not None:
            self.gain_label_var.set(f"auto {status['gain_db']:.1f} dB")
        else:
            self.gain_label_var.set("auto")

        ports = status["ports"]
        self.server_var.set(
            f"RDS ASCII G : {', '.join(str(p) for p in ports['rds'])}"
            f"   clients {status['rds_clients']}   groups sent "
            f"{status['groups_sent']}\n"
            f"Telemetry   : {ports['telemetry']} (JSON lines)   clients "
            f"{status['telemetry_clients']}\n"
            f"Commands    : {ports['command']}"
            + ("\n" + "\n".join(status["server_errors"])
               if status["server_errors"] else ""))

        self._publish_window_rect()
        self.root.after(REFRESH_MS, self._refresh)

    def _draw_meter(self) -> None:
        width = self.meter.winfo_width()
        height = self.meter.winfo_height()
        if width <= 2:
            return
        level = self._meter_level
        self.meter.coords(self.meter_fill, 0, 0, int(width * level), height)
        self.meter.itemconfigure(self.meter_fill, fill=meter_colour(level))
        for tick, frac in zip(self.meter_ticks,
                              (METER_YELLOW_AT, METER_GREEN_AT)):
            x = int(width * frac)
            self.meter.coords(tick, x, 0, x, height)

    def _publish_window_rect(self) -> None:
        """Tell the service where this window is (outer frame, screen px)."""
        try:
            if self.root.state() == "iconic":
                self.service.window_rect = None
                return
            # geometry() gives the outer (frame) corner on every platform;
            # winfo_rootx/y give the client-area corner.  The difference is
            # the window-manager border / title bar.
            outer_x, outer_y = _geometry_origin(self.root.geometry())
            frame_x = max(0, self.root.winfo_rootx() - outer_x)
            frame_y = max(0, self.root.winfo_rooty() - outer_y)
            self.service.window_rect = {
                "x": outer_x,
                "y": outer_y,
                "width": self.root.winfo_width() + 2 * frame_x,
                "height": self.root.winfo_height() + frame_y + frame_x,
                "screen_width": self.root.winfo_screenwidth(),
                "screen_height": self.root.winfo_screenheight(),
            }
        except tk.TclError:
            self.service.window_rect = None

    # -- shutdown ----------------------------------------------------------

    def _on_close(self) -> None:
        try:
            self.layout.geometry = self.root.geometry()
            self.layout.save()
        except tk.TclError:
            pass
        try:
            self.service.shutdown()
        finally:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def status_gain(status: dict) -> str:
    return f"{status['gain_db']:.1f} dB"


RECORD_FORMATS = ("WAV 16-bit MPX (RDS Spy / SDR#)", "float32 raw (.f32)")


def _mmss(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _fmt(value, spec: str) -> str:
    return "unavailable" if value is None else spec.format(value)


def _meter_level(dbfs: float | None) -> float:
    """Map signal dBFS onto 0..1 for the meter (-70 dBFS empty, -10 full)."""
    if dbfs is None:
        return 0.0
    return max(0.0, min(1.0, (dbfs + 70.0) / 60.0))


def meter_colour(level: float) -> str:
    """Red below the weak threshold, yellow in between, green when good."""
    if level < METER_YELLOW_AT:
        return METER_RED
    if level < METER_GREEN_AT:
        return METER_YELLOW
    return METER_GREEN


def _geometry_origin(geometry: str) -> tuple[int, int]:
    """Parse the +X+Y part of a Tk geometry string ('WxH+X+Y')."""
    match = re.search(r"([+-]-?\d+)([+-]-?\d+)$", geometry)
    if not match:
        return 0, 0
    return int(match.group(1).lstrip("+")), int(match.group(2).lstrip("+"))


def run(service: TunerService, frequency: float | None,
        autostart: bool = True):
    window = TunerWindow(service, frequency, autostart=autostart)
    service._on_event = window.event
    window.run()
    return window


__all__ = ["TunerWindow", "run", "RtlSdrDevice", "servers"]


def _hd_region() -> bool:
    """HD Radio (IBOC) is only on the air in the Americas band plan."""
    return band.plan_key() == "americas"


def _hd_program_label(program: int | None) -> str:
    return "Analog" if program is None else f"HD{program + 1}"
