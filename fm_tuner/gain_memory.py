"""Manual gain remembered per frequency (settings/gain_memory.json)."""

from __future__ import annotations

import json
import os

from .settings_dir import settings_file

FILENAME = "gain_memory.json"


class GainMemory:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or settings_file(FILENAME)
        self.gains: dict[str, float] = {}
        self.load()

    @staticmethod
    def _key(freq_mhz: float) -> str:
        freq = round(float(freq_mhz), 3)
        return f"{freq:.1f}" if abs(freq * 10 - round(freq * 10)) < 1e-6 \
            else f"{freq:.2f}"

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
        self.gains = {k: float(v) for k, v in data.items()
                      if isinstance(v, (int, float))} \
            if isinstance(data, dict) else {}

    def get(self, freq_mhz: float) -> float | None:
        return self.gains.get(self._key(freq_mhz))

    def remember(self, freq_mhz: float, gain_db: float) -> None:
        key, value = self._key(freq_mhz), round(float(gain_db), 1)
        if self.gains.get(key) == value:
            return
        self.gains[key] = value
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(dict(sorted(self.gains.items())), handle, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass
