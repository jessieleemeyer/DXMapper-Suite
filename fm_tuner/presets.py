"""Persistent, dynamic tuner presets.

Presets are an ordered list of {name, frequency_mhz} entries kept in a JSON
file in the `settings` folder next to the application, so they survive restarts and can be edited by
hand. The store is safe to call from the GUI and the command server at the
same time, and never raises on a missing or corrupt file: a broken file is
kept aside as `<name>.bad` and an empty list is used.
"""

from __future__ import annotations

import json
import os
import threading

from . import band
from .settings_dir import settings_file

PRESETS_FILENAME = "tuner_presets.json"
MAX_PRESETS = 100
MAX_NAME_LENGTH = 40


class PresetError(ValueError):
    """A preset could not be added, renamed or recalled."""


def default_path() -> str:
    """`settings/tuner_presets.json` next to the program (see settings_dir)."""
    return settings_file(PRESETS_FILENAME)


class PresetStore:
    def __init__(self, path: str | None = None, on_event=None):
        self.path = path or default_path()
        self._on_event = on_event
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> list[dict]:
        with self._lock:
            self._items = []
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except FileNotFoundError:
                return []
            except (OSError, json.JSONDecodeError) as exc:
                self._event("warning",
                            f"Could not read {self.path}: {exc}")
                self._quarantine()
                return []
            entries = data.get("presets") if isinstance(data, dict) else data
            if not isinstance(entries, list):
                return []
            for entry in entries[:MAX_PRESETS]:
                item = _clean(entry)
                if item is not None:
                    self._items.append(item)
            return list(self._items)

    def save(self) -> None:
        with self._lock:
            payload = {"presets": list(self._items)}
            temp = f"{self.path}.tmp"
            try:
                with open(temp, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2)
                os.replace(temp, self.path)
            except OSError as exc:
                self._event("warning",
                            f"Could not save presets to {self.path}: {exc}")

    def _quarantine(self) -> None:
        try:
            os.replace(self.path, self.path + ".bad")
        except OSError:
            pass

    # -- queries -----------------------------------------------------------

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._items]

    def find(self, key) -> dict | None:
        """Look a preset up by 1-based index, name or frequency."""
        with self._lock:
            if isinstance(key, int) or (isinstance(key, str)
                                        and key.strip().isdigit()):
                index = int(key) - 1
                if 0 <= index < len(self._items):
                    return dict(self._items[index])
                return None
            text = str(key).strip()
            for item in self._items:
                if item["name"].lower() == text.lower():
                    return dict(item)
            try:
                freq = round(float(text), 3)
            except ValueError:
                return None
            for item in self._items:
                if abs(item["frequency_mhz"] - freq) < 0.005:
                    return dict(item)
            return None

    # -- mutation ----------------------------------------------------------

    def add(self, frequency_mhz: float, name: str = "") -> dict:
        """Store a preset, replacing any existing one on that frequency."""
        freq = band.validate_any(float(frequency_mhz))
        label = _clean_name(name) or band.fmt(freq)
        with self._lock:
            if len(self._items) >= MAX_PRESETS:
                raise PresetError(f"only {MAX_PRESETS} presets can be stored")
            item = {"name": label, "frequency_mhz": freq}
            for index, existing in enumerate(self._items):
                if abs(existing["frequency_mhz"] - freq) < 0.005:
                    self._items[index] = item
                    break
            else:
                self._items.append(item)
            self.save()
            return dict(item)

    def rename(self, key, name: str) -> dict:
        label = _clean_name(name)
        if not label:
            raise PresetError("a preset needs a name")
        with self._lock:
            index = self._index_of(key)
            self._items[index]["name"] = label
            self.save()
            return dict(self._items[index])

    def remove(self, key) -> dict:
        with self._lock:
            index = self._index_of(key)
            item = self._items.pop(index)
            self.save()
            return item

    def move(self, key, offset: int) -> list[dict]:
        """Shift a preset up (-1) or down (+1) in the list."""
        with self._lock:
            index = self._index_of(key)
            target = max(0, min(len(self._items) - 1, index + offset))
            if target != index:
                self._items.insert(target, self._items.pop(index))
                self.save()
            return self.list()

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self.save()

    def _index_of(self, key) -> int:
        item = self.find(key)
        if item is None:
            raise PresetError(f"no preset matching {key!r}")
        for index, existing in enumerate(self._items):
            if (existing["name"] == item["name"]
                    and existing["frequency_mhz"] == item["frequency_mhz"]):
                return index
        raise PresetError(f"no preset matching {key!r}")

    def _event(self, kind: str, message: str) -> None:
        if self._on_event:
            try:
                self._on_event(kind, message)
            except Exception:
                pass


def _clean_name(name) -> str:
    text = " ".join(str(name or "").split())
    return text[:MAX_NAME_LENGTH]


def _clean(entry) -> dict | None:
    if not isinstance(entry, dict):
        return None
    try:
        freq = band.validate_any(float(entry.get("frequency_mhz")))
    except (TypeError, ValueError, band.FrequencyError):
        return None
    name = _clean_name(entry.get("name")) or band.fmt(freq)
    return {"name": name, "frequency_mhz": freq}


__all__ = ["PresetStore", "PresetError", "default_path", "PRESETS_FILENAME"]
