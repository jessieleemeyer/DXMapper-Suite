"""Place the FM Tuner, RDS Monitor and DX Mapper windows on your screens.

1 screen : tuner on the left third, RDS Monitor top right, DX Mapper
           bottom right.
2 screens: tuner on display 1; RDS Monitor above DX Mapper on the left
           half of display 2, leaving the right half free.
3+       : one program per screen.
Which program goes on which display can be changed in Arrange Windows; the
choice is kept in settings/arrange.json next to this file. Start Radio also
remembers where you last had each window (per screen setup) and puts it back.
"""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT / "settings" / "arrange.json"

PROGRAMS = (  # (key, name shown, window title starts with)
    ("tuner", "FM Tuner", "FM Tuner"),
    ("rds", "RDS Monitor", "RDS Monitor"),
    ("mapper", "DX Mapper", "DX Mapper"),
)
BROWSERS = ("Chrome", "Edge", "Firefox", "Opera", "Brave")


def default_screens(count):
    """Display index (0 = display 1) for each program."""
    if count <= 1:
        return {"tuner": 0, "rds": 0, "mapper": 0}
    if count == 2:
        return {"tuner": 0, "rds": 1, "mapper": 1}
    return {"tuner": 0, "rds": 1, "mapper": 2}


AREAS = (("full", "Whole screen"), ("left", "Left half"),
         ("right", "Right half"))


def default_areas(count):
    """How much of each display (by index) the programs may use."""
    areas = {i: "full" for i in range(max(count, 1))}
    if count == 2:
        areas[1] = "left"
    return areas


def load_settings():
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")


def screens_for(count, saved=None):
    """Saved per-program display choice for this many screens, else the
    default. Choices are kept per screen count, so unplugging the TV
    doesn't lose the 2-screen setup."""
    chosen = dict(default_screens(count))
    mine = ((saved or {}).get("screens") or {}).get(str(count)) or {}
    for key, _, _ in PROGRAMS:
        n = mine.get(key)
        if isinstance(n, int) and 0 <= n < count:
            chosen[key] = n
    return chosen


def areas_for(count, saved=None):
    """Saved whole/left/right choice per display, else the default."""
    areas = default_areas(count)
    mine = ((saved or {}).get("areas") or {}).get(str(count)) or {}
    for index in list(areas):
        if mine.get(str(index)) in dict(AREAS):
            areas[index] = mine[str(index)]
    return areas


def plan(monitors, chosen, areas=None):
    """{program: (x, y, w, h)} given work areas [(x, y, w, h), ...]."""
    rects = {}
    for index, (mx, my, mw, mh) in enumerate(monitors):
        area = (areas or {}).get(index, "full")
        if area in ("left", "right"):
            mw //= 2
            if area == "right":
                mx += mw
        here = [k for k, _, _ in PROGRAMS if chosen.get(k) == index]
        if len(here) == 1:
            rects[here[0]] = (mx, my, mw, mh)
        elif len(here) == 2:
            if "tuner" in here:
                left = mw // 3
                other = here[1] if here[0] == "tuner" else here[0]
                rects["tuner"] = (mx, my, left, mh)
                rects[other] = (mx + left, my, mw - left, mh)
            elif area != "full":
                top = mh // 2
                rects[here[0]] = (mx, my, mw, top)
                rects[here[1]] = (mx, my + top, mw, mh - top)
            else:
                half = mw // 2
                rects[here[0]] = (mx, my, half, mh)
                rects[here[1]] = (mx + half, my, mw - half, mh)
        elif len(here) == 3:
            left, top = mw // 3, mh // 2
            rects["tuner"] = (mx, my, left, mh)
            rects["rds"] = (mx + left, my, mw - left, top)
            rects["mapper"] = (mx + left, my + top, mw - left, mh - top)
    return rects


def layout_key(monitors):
    """Name for this screen setup, so a remembered layout is only reused on
    the same screens."""
    return ";".join(",".join(str(v) for v in m) for m in monitors)


def _on_screen(rect, monitors):
    x, y, w, h = rect[:4]
    cx, cy = x + w // 2, y + min(h, 40) // 2
    return w > 50 and h > 50 and any(
        mx <= cx < mx + mw and my <= cy < my + mh
        for mx, my, mw, mh in monitors)


def last_layout(monitors, saved=None):
    """{program: (x, y, w, h, maximized)} remembered for these screens,
    leaving out anything that would be off-screen."""
    mine = ((saved or {}).get("last") or {}).get(layout_key(monitors)) or {}
    out = {}
    for key, _, _ in PROGRAMS:
        r = mine.get(key)
        if (isinstance(r, list) and len(r) == 5
                and all(isinstance(v, int) for v in r[:4])
                and _on_screen(r, monitors)):
            out[key] = (r[0], r[1], r[2], r[3], bool(r[4]))
    return out


def update_last(saved, monitors, rects):
    """Store the windows' current places; True when something changed.
    Programs that are closed keep their last place."""
    mine = saved.setdefault("last", {}).setdefault(layout_key(monitors), {})
    changed = False
    for key, r in rects.items():
        r = [int(r[0]), int(r[1]), int(r[2]), int(r[3]), bool(r[4])]
        if _on_screen(r, monitors) and mine.get(key) != r:
            mine[key] = r
            changed = True
    return changed


def match_title(title):
    """Program key for a window title, or None (browser tabs don't count)."""
    if any(b in title for b in BROWSERS):
        return None
    for key, _, prefix in PROGRAMS:
        if title == prefix or title.startswith(prefix + " "):
            return key
    return None


# --- Windows only -------------------------------------------------------

def _user32():
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    return ctypes.windll.user32


def monitors():
    """Work areas (taskbar excluded), in Windows display-number order."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes
    user32 = _user32()

    class MONITORINFOEX(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def each(hmon, _hdc, _rect, _data):
        info = MONITORINFOEX()
        info.cbSize = ctypes.sizeof(MONITORINFOEX)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcWork
            m = re.search(r"(\d+)$", info.szDevice)
            number = int(m.group(1)) if m else 99
            found.append((number, (r.left, r.top, r.right - r.left,
                                   r.bottom - r.top)))
        return 1

    user32.EnumDisplayMonitors(None, None, proc(each), 0)
    found.sort(key=lambda item: (item[0], item[1][0]))
    return [area for _, area in found]


def find_windows():
    """{program: hwnd} for the visible program windows."""
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes
    user32 = _user32()
    found = {}
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _data):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                key = match_title(buf.value)
                if key and key not in found:
                    found[key] = hwnd
        return True

    user32.EnumWindows(proc(each), 0)
    return found


def window_rects():
    """{program: (x, y, w, h, maximized)} of the open, not minimised
    program windows."""
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes
    user32 = _user32()
    out = {}
    for key, hwnd in find_windows().items():
        if user32.IsIconic(hwnd):
            continue
        r = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(r)):
            out[key] = (r.left, r.top, r.right - r.left, r.bottom - r.top,
                        bool(user32.IsZoomed(hwnd)))
    return out


def remember(poll_s=5.0):
    """Keep noting where the windows are until all three are closed, so
    the next Start Radio puts them back there."""
    areas = monitors()
    if not areas:
        return
    seen = False
    while True:
        rects = window_rects()
        if rects:
            seen = True
            saved = load_settings()
            if update_last(saved, areas, rects):
                save_settings(saved)
        elif seen and not find_windows():
            return
        time.sleep(poll_s)


def arrange(wait_s=0.0, use_last=False):
    """Move the program windows; returns the names of the ones placed.
    wait_s: keep looking that long for windows that haven't opened yet.
    use_last: put windows back where they were last time (if known)."""
    areas = monitors()
    if not areas:
        return []
    saved = load_settings()
    rects = {k: tuple(v) + (False,) for k, v in plan(
        areas, screens_for(len(areas), saved),
        areas_for(len(areas), saved)).items()}
    if use_last:
        rects.update(last_layout(areas, saved))
    user32 = _user32()
    placed = set()
    deadline = time.time() + wait_s
    while True:
        for key, hwnd in find_windows().items():
            if key in placed or key not in rects:
                continue
            x, y, w, h, maximized = rects[key]
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE (un-maximise first)
            user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0014)
            if maximized:
                user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE on that screen
            placed.add(key)
        if len(placed) == len(PROGRAMS) or time.time() >= deadline:
            break
        time.sleep(1.0)
    return [name for key, name, _ in PROGRAMS if key in placed]


# --- the Arrange Windows window ------------------------------------------

def main():
    import tkinter as tk
    from tkinter import messagebox, ttk
    root = tk.Tk()
    root.title("Arrange Windows")
    root.resizable(False, False)
    areas = monitors()
    if not areas:
        messagebox.showinfo("Arrange Windows",
                            "Arranging windows works on Windows only.")
        root.destroy()
        return
    saved = load_settings()
    chosen = screens_for(len(areas), saved)
    labels = [f"Display {i + 1}  ({w}\u00d7{h})"
              for i, (_, _, w, h) in enumerate(areas)]
    frame = ttk.Frame(root, padding=12)
    frame.pack()
    ttk.Label(frame, text=f"{len(areas)} screen(s) found. "
              "Pick where each program goes:").grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
    picks = {}
    for row, (key, name, _) in enumerate(PROGRAMS, start=1):
        ttk.Label(frame, text=name).grid(row=row, column=0, sticky="w",
                                         padx=(0, 10), pady=2)
        var = tk.StringVar(value=labels[chosen[key]])
        ttk.Combobox(frame, textvariable=var, values=labels, width=26,
                     state="readonly").grid(row=row, column=1, pady=2)
        picks[key] = var
    area_names = dict(AREAS)
    area_keys = {name: key for key, name in AREAS}
    current = areas_for(len(areas), saved)
    uses = {}
    row = 4
    for index, label in enumerate(labels):
        ttk.Label(frame, text=f"Use of display {index + 1}").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=2)
        var = tk.StringVar(value=area_names[current[index]])
        ttk.Combobox(frame, textvariable=var, width=26, state="readonly",
                     values=[name for _, name in AREAS]).grid(
            row=row, column=1, pady=2)
        uses[index] = var
        row += 1
    on_start = tk.BooleanVar(value=saved.get("on_start", True))
    ttk.Checkbutton(frame, text="Arrange every time Start Radio runs",
                    variable=on_start).grid(row=row, column=0, columnspan=2,
                                            sticky="w", pady=(8, 0))
    row += 1
    use_last = tk.BooleanVar(value=saved.get("remember", True))
    ttk.Checkbutton(frame, text="  \u2514 Put windows back where I last had them",
                    variable=use_last).grid(row=row, column=0, columnspan=2,
                                            sticky="w")
    status = tk.StringVar()

    def store():
        data = load_settings()
        data.setdefault("screens", {})[str(len(areas))] = {
            key: labels.index(var.get()) for key, var in picks.items()}
        data.setdefault("areas", {})[str(len(areas))] = {
            str(i): area_keys[var.get()] for i, var in uses.items()}
        data["on_start"] = bool(on_start.get())
        data["remember"] = bool(use_last.get())
        save_settings(data)

    def do_arrange():
        store()
        data = load_settings()
        data.get("last", {}).pop(layout_key(areas), None)
        save_settings(data)
        done = arrange()
        status.set(("Placed: " + ", ".join(done)) if done else
                   "None of the three programs is open.")

    buttons = ttk.Frame(frame)
    buttons.grid(row=row + 1, column=0, columnspan=2, pady=(10, 0), sticky="e")
    ttk.Button(buttons, text="Arrange now", command=do_arrange).pack(
        side="left", padx=(0, 6))
    ttk.Button(buttons, text="Save and close",
               command=lambda: (store(), root.destroy())).pack(side="left")
    ttk.Label(frame, textvariable=status).grid(row=row + 2, column=0,
                                               columnspan=2, sticky="w",
                                               pady=(8, 0))
    root.mainloop()


if __name__ == "__main__":
    main()
