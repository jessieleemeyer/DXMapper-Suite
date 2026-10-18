"""Persistent window layout: panel order and column, visibility, geometry,
UI scale, RDS2 toggle.

Kept in `settings/tuner_layout.json` next to the application (with the
station presets), written on shutdown and whenever a panel is moved or
hidden, so the window comes back exactly as it was left. Missing or corrupt
files fall back to the defaults without raising.
"""

from __future__ import annotations

import json
import os

from .band import PLANS as _BAND_PLANS
from .settings_dir import settings_file

PLANS_KEYS = tuple(_BAND_PLANS)

LAYOUT_FILENAME = "tuner_layout.json"

# (key, title shown in the View menu, visible by default)
PANELS: tuple[tuple[str, str, bool], ...] = (
    ("tuning", "Tuning", True),
    ("presets", "Presets", True),
    ("status", "Signal / status", False),
    ("spectrum", "RF spectrum + MPX", False),
    ("gain", "Gain", False),
    ("antenna", "Antenna power (Bias-T)", False),
    ("dx", "DX / interference filter", False),
    ("audio", "Audio", False),
    ("rds", "Live RDS", True),
    ("servers", "Localhost services", False),
    ("remote", "Remote (phone / tablet / PC)", True),
    ("log", "Log", False),
)
PANEL_KEYS = tuple(key for key, _, _ in PANELS)

# Two columns; panels not listed here start in the left one.
COLUMNS = 2
DEFAULT_RIGHT = ("spectrum", "dx", "rds", "servers", "remote", "log")


def default_path() -> str:
    """`settings/tuner_layout.json` next to the program (see settings_dir)."""
    return settings_file(LAYOUT_FILENAME)


def default_layout() -> dict:
    return {
        "order": list(PANEL_KEYS),
        "visible": {key: shown for key, _, shown in PANELS},
        "geometry": "",
        "rds2": False,
        "hd_mode": "auto",
        "hd_program": 0,
        "hd_delay": 0.0,
        "topmost": False,
        "columns": {k: (1 if k in DEFAULT_RIGHT else 0) for k in PANEL_KEYS},
        "scale": 100,
        "presets": {},
        "last_frequency": None,
        "remote": False,
        "hotkeys": True,
        "signal_units": "s",
        "radio": "",
        "gain_memory": False,
        "band": None,
    }


LAYOUT_PRESET_FIELDS = ("order", "visible", "columns", "scale")
MAX_LAYOUT_PRESETS = 50


SCALES = (100, 125, 150, 175, 200)


class LayoutStore:
    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        self.order: list[str] = []
        self.visible: dict[str, bool] = {}
        self.geometry = ""
        self.rds2 = False
        self.hd_mode = "auto"
        self.hd_program: int | None = 0
        self.hd_delay = 0.0
        self.topmost = False
        self.columns: dict[str, int] = {}
        self.scale = 100
        self.presets: dict[str, dict] = {}
        self.last_frequency: float | None = None
        self.remote = False
        self.hotkeys = True
        self.signal_units = "s"
        self.radio = ""
        self.gain_memory = False
        self.band: str | None = None
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        data = {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
        self._apply(data)

    def save(self) -> bool:
        payload = {"order": self.order, "visible": self.visible,
                   "geometry": self.geometry, "rds2": self.rds2,
                   "hd_mode": self.hd_mode, "hd_program": self.hd_program,
                   "hd_delay": self.hd_delay,
                   "topmost": self.topmost, "columns": self.columns,
                   "scale": self.scale, "presets": self.presets,
                   "last_frequency": self.last_frequency,
                   "remote": self.remote, "hotkeys": self.hotkeys,
                   "signal_units": self.signal_units,
                   "radio": self.radio,
                   "gain_memory": self.gain_memory,
                   "band": self.band}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self.path)
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    def reset(self) -> None:
        """Back to the default layout; saved layout presets are kept."""
        presets = self.presets
        self._apply({})
        self.presets = presets

    def _apply(self, data: dict) -> None:
        defaults = default_layout()
        order = data.get("order")
        cleaned = []
        if isinstance(order, list):
            for key in order:
                if key in PANEL_KEYS and key not in cleaned:
                    cleaned.append(key)
        # Panels added in a newer build slot in at their default position.
        for key in PANEL_KEYS:
            if key not in cleaned:
                cleaned.insert(min(len(cleaned), PANEL_KEYS.index(key)), key)
        self.order = cleaned
        visible = data.get("visible")
        self.visible = dict(defaults["visible"])
        if isinstance(visible, dict):
            for key, shown in visible.items():
                if key in PANEL_KEYS:
                    self.visible[key] = bool(shown)
        geometry = data.get("geometry")
        self.geometry = geometry if isinstance(geometry, str) else ""
        self.rds2 = bool(data.get("rds2", defaults["rds2"]))
        mode = data.get("hd_mode")
        self.hd_mode = (mode if mode in ("auto", "manual", "hd_only", "analog")
                        else defaults["hd_mode"])
        prog = data.get("hd_program", defaults["hd_program"])
        self.hd_program = (prog if prog is None or (isinstance(prog, int)
                                                    and 0 <= prog < 8)
                           else defaults["hd_program"])
        delay = data.get("hd_delay")
        self.hd_delay = (min(max(float(delay), 0.0), 10.0)
                         if isinstance(delay, (int, float))
                         else defaults["hd_delay"])
        self.topmost = bool(data.get("topmost", False))
        self.remote = bool(data.get("remote", False))
        self.hotkeys = bool(data.get("hotkeys", True))
        self.signal_units = (data.get("signal_units")
                             if data.get("signal_units") in ("s", "db") else "s")
        self.gain_memory = bool(data.get("gain_memory", False))
        # A new install has no band yet (the tuner asks); an existing one
        # from before regions existed stays on Americas.
        self.band = (data["band"] if data.get("band") in PLANS_KEYS
                     else ("americas" if data else None))
        radio = data.get("radio")
        self.radio = radio if isinstance(radio, str) else ""
        last = data.get("last_frequency")
        self.last_frequency = (float(last) if isinstance(last, (int, float))
                               and 65.0 <= last <= 108.5 else None)
        self.columns = dict(defaults["columns"])
        columns = data.get("columns")
        if isinstance(columns, dict):
            for key, col in columns.items():
                if key in PANEL_KEYS and isinstance(col, int) \
                        and 0 <= col < COLUMNS:
                    self.columns[key] = col
        scale = data.get("scale", defaults["scale"])
        self.scale = scale if scale in SCALES else 100
        self.presets = {}
        presets = data.get("presets")
        if isinstance(presets, dict):
            for name, snap in presets.items():
                if isinstance(name, str) and name.strip() \
                        and isinstance(snap, dict):
                    self.presets[name.strip()] = snap

    # -- edits -------------------------------------------------------------

    def set_visible(self, key: str, shown: bool) -> None:
        if key in PANEL_KEYS:
            self.visible[key] = bool(shown)

    def move(self, key: str, offset: int) -> bool:
        """Shift `key` by `offset` places; returns whether anything moved."""
        if key not in self.order:
            return False
        index = self.order.index(key)
        target = max(0, min(len(self.order) - 1, index + offset))
        if target == index:
            return False
        self.order.pop(index)
        self.order.insert(target, key)
        return True

    def move_to(self, key: str, target_index: int) -> bool:
        if key not in self.order:
            return False
        index = self.order.index(key)
        target = max(0, min(len(self.order) - 1, target_index))
        if target == index:
            return False
        self.order.pop(index)
        self.order.insert(target, key)
        return True

    def set_column(self, key: str, column: int) -> None:
        if key in PANEL_KEYS and 0 <= int(column) < COLUMNS:
            self.columns[key] = int(column)

    def column_order(self, column: int) -> list[str]:
        """Visible panels of `column`, top to bottom."""
        return [k for k in self.order
                if self.visible.get(k) and self.columns.get(k, 0) == column]

    def place(self, key: str, column: int, index: int) -> bool:
        """Drop `key` into `column` at `index` among that column's panels."""
        if key not in self.order or not 0 <= column < COLUMNS:
            return False
        before = (self.columns.get(key), self.column_order(column))
        self.set_column(key, column)
        others = [k for k in self.column_order(column) if k != key]
        index = max(0, min(len(others), index))
        self.order.remove(key)
        if index >= len(others):
            # after the last panel of this column
            anchor = self.order.index(others[-1]) + 1 if others else len(self.order)
        else:
            anchor = self.order.index(others[index])
        self.order.insert(anchor, key)
        return before != (column, self.column_order(column))

    def reset_columns(self) -> None:
        self.columns = dict(default_layout()["columns"])

    def set_scale(self, scale: int) -> None:
        if scale in SCALES:
            self.scale = scale

    # -- layout presets ----------------------------------------------------

    def snapshot(self) -> dict:
        return {"order": list(self.order), "visible": dict(self.visible),
                "columns": dict(self.columns), "scale": self.scale}

    def save_preset(self, name: str) -> bool:
        name = name.strip()
        if not name or (name not in self.presets
                        and len(self.presets) >= MAX_LAYOUT_PRESETS):
            return False
        self.presets[name] = self.snapshot()
        return True

    def load_preset(self, name: str) -> bool:
        snap = self.presets.get(name)
        if snap is None:
            return False
        keep = {"geometry": self.geometry, "rds2": self.rds2,
                "hd_mode": self.hd_mode, "hd_program": self.hd_program,
                "hd_delay": self.hd_delay,
                "topmost": self.topmost, "presets": self.presets,
                "signal_units": self.signal_units, "radio": self.radio,
                "gain_memory": self.gain_memory, "band": self.band,
                "remote": self.remote, "hotkeys": self.hotkeys,
                "last_frequency": self.last_frequency}
        self._apply({**keep, **{k: snap.get(k) for k in LAYOUT_PRESET_FIELDS}})
        return True

    def delete_preset(self, name: str) -> bool:
        return self.presets.pop(name, None) is not None
