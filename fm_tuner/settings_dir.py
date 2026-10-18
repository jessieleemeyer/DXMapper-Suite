"""Where the tuner keeps its user files: `<FM-Tuner>/settings/`.

Everything the user would want to carry across an upgrade (station presets,
window layout, layout presets) lives in one folder next to the program, so
upgrading is "copy the settings folder into the new version". Files left
in the old locations (working directory, package folder, home) by earlier
builds are moved in on first use. Falls back to the home folder and then
the temp folder if the program folder is not writable.
"""

from __future__ import annotations

import os
import shutil
import tempfile

SETTINGS_DIRNAME = "settings"
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _writable(folder: str) -> bool:
    try:
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, ".write_test")
        with open(probe, "a"):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False


def settings_dir() -> str:
    for folder in (os.path.join(APP_ROOT, SETTINGS_DIRNAME),
                   os.path.join(os.path.expanduser("~"), "FM-Tuner-settings"),
                   os.path.join(tempfile.gettempdir(), "FM-Tuner-settings")):
        if _writable(folder):
            return folder
    return tempfile.gettempdir()


def settings_file(filename: str) -> str:
    """Path of `filename` in the settings folder, migrating an old copy."""
    target = os.path.join(settings_dir(), filename)
    if not os.path.exists(target):
        for old in (os.path.join(os.getcwd(), filename),
                    os.path.join(APP_ROOT, filename),
                    os.path.join(APP_ROOT, "fm_tuner", filename),
                    os.path.join(os.path.expanduser("~"), filename)):
            if os.path.isfile(old) and os.path.abspath(old) != target:
                try:
                    shutil.move(old, target)
                except OSError:
                    continue
                break
    return target
