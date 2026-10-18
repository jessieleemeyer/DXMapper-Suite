"""Shared start-up for the one-folder DXMapper Suite.

startup(root) is called by Start Radio and by FM_Tuner.py, RDS Monitor.py
and DX Mapper.py when they are double-clicked on their own:
- hides the console window Windows opens for a double-clicked .py file
  (not when started from a Command Prompt or PowerShell window);
- the first time, copies the settings from the old FM-Tuner, RDS-Monitor
  and DX-Mapper folders into the one shared settings folder. Nothing is
  moved or deleted, so the old folders still work if you go back.
"""

import datetime
import os
import shutil
from pathlib import Path

OLD_FOLDERS = ("FM-Tuner", "RDS-Monitor", "DX-Mapper")
MARKER = ".copied_from_old_folders"
OLD_LOOSE_FILES = ("monitor_settings.json", "station_count.txt")


def is_suite_folder(root):
    root = Path(root)
    return all((root / f).is_file()
               for f in ("FM_Tuner.py", "RDS Monitor.py", "DX Mapper.py"))


def _copy_missing(src, dst, copied):
    for path in sorted(Path(src).rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        target = Path(dst) / path.relative_to(src)
        if target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(str(target.relative_to(Path(dst).parent)))
        except OSError:
            pass


def migrate(root):
    """Copy old per-program settings into root/settings once. Returns the
    list of files copied (empty when there was nothing to do)."""
    root = Path(root)
    settings = root / "settings"
    marker = settings / MARKER
    if not is_suite_folder(root) or marker.exists():
        return []
    copied = []
    for name in OLD_FOLDERS:
        old = root / name
        if not old.is_dir():
            continue
        if (old / "settings").is_dir():
            _copy_missing(old / "settings", settings, copied)
        for loose in OLD_LOOSE_FILES:
            src = old / loose
            if src.is_file() and not (settings / loose).exists():
                settings.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, settings / loose)
                copied.append(f"settings/{loose}")
        if name == "FM-Tuner":
            for dll in old.glob("*.dll"):
                if not (root / dll.name).exists():
                    try:
                        shutil.copy2(dll, root / dll.name)
                        copied.append(dll.name)
                    except OSError:
                        pass
        if name == "RDS-Monitor" and (old / "reports").is_dir():
            _copy_missing(old / "reports", root / "reports", copied)
    try:
        settings.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"Settings copied from the old program folders on "
            f"{datetime.datetime.now():%Y-%m-%d %H:%M}.\n"
            "The old FM-Tuner, RDS-Monitor and DX-Mapper folders were left as they "
            "were.\n\n" + "\n".join(copied) + "\n", encoding="utf-8")
    except OSError:
        pass
    return copied


def hide_console():
    """Hide the console of a double-clicked .py (Windows only)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        hwnd = k32.GetConsoleWindow()
        if not hwnd:
            return
        pids = (wintypes.DWORD * 16)()
        n = k32.GetConsoleProcessList(pids, 16)
        names = []
        for pid in pids[:min(n, 16)]:
            h = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not h:
                return
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(520)
            ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            k32.CloseHandle(h)
            if not ok:
                return
            names.append(os.path.basename(buf.value).lower())
        if names and all(n.startswith(("python", "py.", "pyw")) for n in names):
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def startup(root):
    hide_console()
    try:
        migrate(root)
    except Exception:
        pass
