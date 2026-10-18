"""DX Mapper - interactive FM DX log map.

Standalone program. It never opens radio hardware. Stations arrive from the
RDS Monitor's Log button over 127.0.0.1:7375 and from imports (Google Sheets
/ CSV, RDS Spy .spy, .txt). The map opens in its own program window
(pywebview - set up automatically on first start), or in your web browser at
http://127.0.0.1:7380/ when that is not possible. An Excel copy of the log is
kept in settings/DX Log.xlsx.

Double-click "DX Mapper.pyw" or "Start DX Mapper.bat" - no console needed.

    python "DX Mapper.py" [--feed-port 7375] [--web-port 7380]
        [--monitor-dir ..\\RDS-Monitor] [--browser] [--no-browser]
"""

import argparse
import importlib
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SETTINGS_DIR = APP_DIR / "settings"
sys.path.insert(0, str(APP_DIR))
try:  # one-folder suite: hide the console, copy old settings in once
    import suite_start
    suite_start.startup(Path(__file__).resolve().parent)
except ImportError:
    pass

if sys.stdout is None or sys.stderr is None:  # pythonw: no console
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    _log = open(SETTINGS_DIR / "dx_mapper.log", "a", encoding="utf-8",
                buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

from dxmap.excel import ExcelMirror  # noqa: E402
from dxmap.feed import FEED_PORT, FeedServer  # noqa: E402
from dxmap.store import Store  # noqa: E402
from dxmap.web import WEB_PORT, App, serve  # noqa: E402

WEBVIEW_PKG = "pywebview>=5.0,<7"
GUI = [True]


def alert(msg, error=False):
    """Message box (no console needed); also written to the log."""
    print(("[X] " if error else "[!] ") + msg)
    if not GUI[0]:
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        (messagebox.showerror if error else messagebox.showwarning)(
            "DX Mapper", msg, parent=root)
        root.destroy()
    except Exception:
        pass


def find_monitor_dir(explicit=None):
    candidates = [Path(explicit)] if explicit else [
        APP_DIR if (APP_DIR / "RDS Monitor.py").is_file() else APP_DIR / "-",
        APP_DIR.parent / "RDS-Monitor", APP_DIR.parent / "RDS Monitor",
        APP_DIR.parent / "RDS_Monitor"]
    for c in candidates:
        if (c / "RDS Monitor.py").is_file() or (c / "settings").is_dir():
            return c
    return None


# -- program window -----------------------------------------------------------
def _pip_install(pkg):
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        exe = exe.parent / "python.exe"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for extra in ([], ["--user"]):
        try:
            r = subprocess.run([str(exe), "-m", "pip", "install", "-q",
                                "--disable-pip-version-check", *extra, pkg],
                               capture_output=True, text=True, timeout=900,
                               creationflags=flags)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[!] pip: {e}")
            return False
        if r.returncode == 0:
            return True
        print(f"[!] pip install {pkg} {' '.join(extra)}:\n{r.stdout[-1500:]}{r.stderr[-1500:]}")
    return False


def _with_splash(text, work):
    """Run work() in a thread behind a small 'please wait' window."""
    result = [None]
    t = threading.Thread(target=lambda: result.__setitem__(0, work()),
                         daemon=True)
    t.start()
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        root.title("DX Mapper")
        root.resizable(False, False)
        tk.Label(root, text=text, padx=24, pady=14, justify="left").pack()
        bar = ttk.Progressbar(root, mode="indeterminate", length=300)
        bar.pack(padx=24, pady=(0, 18))
        bar.start(12)

        def poll():
            if t.is_alive():
                root.after(200, poll)
            else:
                root.destroy()
        poll()
        root.mainloop()
    except Exception:
        pass
    t.join()
    return result[0]


def load_webview():
    """The pywebview module, installing it on first start; None if not possible."""
    try:
        import webview
        return webview
    except ImportError:
        pass
    if not _with_splash("Setting up the DX Mapper window (first start only).\n"
                        "This needs the internet and takes a minute or two...",
                        lambda: _pip_install(WEBVIEW_PKG)):
        return None
    importlib.invalidate_caches()
    try:
        import site
        user = site.getusersitepackages()
        if user not in sys.path:
            sys.path.append(user)
    except Exception:
        pass
    try:
        import webview
        return webview
    except ImportError:
        return None


def station_title(store):
    n = store.station_count()
    return f"DX Mapper - {n} station{'' if n == 1 else 's'}"


def run_window(webview, url, title=None):
    """The map in its own window; returns when the window is closed."""
    settings = getattr(webview, "settings", None)
    if settings is not None:
        for key in ("ALLOW_DOWNLOADS", "OPEN_EXTERNAL_LINKS_IN_BROWSER"):
            try:
                settings[key] = True
            except Exception:
                pass
    window = webview.create_window(title() if title else "DX Mapper", url,
                                   width=1400, height=900, min_size=(900, 600))
    if title:
        def keep_title():
            shown = None
            while True:
                time.sleep(2)
                try:
                    text = title()
                    if text != shown:
                        window.set_title(text)
                        shown = text
                except Exception:
                    pass
        threading.Thread(target=keep_title, daemon=True).start()
    webview.start(private_mode=False, storage_path=str(SETTINGS_DIR / "window"))


def run_control_panel(url, lines):
    """Small window used when the map is shown in the web browser."""
    import tkinter as tk
    root = tk.Tk()
    root.title("DX Mapper")
    root.resizable(False, False)
    tk.Label(root, text="DX Mapper is running - the map is in your web browser.",
             font=("Segoe UI", 10, "bold"), padx=16, pady=10).pack()
    tk.Label(root, text="\n".join(lines), justify="left", fg="#555",
             padx=16).pack(anchor="w")
    row = tk.Frame(root, pady=12)
    row.pack()
    tk.Button(row, text="Open Map", width=12,
              command=lambda: webbrowser.open(url)).pack(side="left", padx=6)
    tk.Button(row, text="Quit", width=12, command=root.destroy).pack(side="left", padx=6)
    root.mainloop()


def main(argv=None):
    p = argparse.ArgumentParser(description="DX Mapper")
    p.add_argument("--feed-port", type=int, default=FEED_PORT,
                   help="port the RDS Monitor's Log button sends to")
    p.add_argument("--web-port", type=int, default=WEB_PORT,
                   help="port of the map web page")
    p.add_argument("--monitor-dir", default=None,
                   help="RDS Monitor folder (for home location + stations.txt)")
    p.add_argument("--db", default=str(SETTINGS_DIR / "dx_log.db"))
    p.add_argument("--excel", default=str(SETTINGS_DIR / "DX Log.xlsx"),
                   help="Excel copy of the log, kept up to date automatically")
    p.add_argument("--browser", action="store_true",
                   help="show the map in the web browser instead of its own window")
    p.add_argument("--no-browser", action="store_true",
                   help="no window at all (console; Ctrl+C quits)")
    args = p.parse_args(argv)
    GUI[0] = not args.no_browser

    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    store = Store(args.db)
    app = App(store, SETTINGS_DIR / "logger_settings.json",
              monitor_dir=find_monitor_dir(args.monitor_dir))
    try:
        serve(app, port=args.web_port)
    except OSError as e:
        store.close()
        alert(f"DX Mapper is already running (or port {args.web_port} is in "
              f"use), so it was not started again.\n\n({e})", error=True)
        return 1
    app.main_excel = args.excel
    try:
        app.show_logs(app.settings.get("logs_shown") or [app.main_name])
    except Exception as e:
        print(f"[!] could not open the ticked logs ({e}); using {app.main_name}")
    app.excel = ExcelMirror(app.store, app.log_excel(app.log_name), lambda: app.home)
    app.excel.after_save = app.backup_now
    app.store.on_change = app.excel.schedule
    app.excel.schedule()
    feed = FeedServer(app.record, port=args.feed_port, on_now=app.now_hearing)
    app.feed = feed
    if feed.start():
        print(f"[ok] listening for the RDS Monitor on 127.0.0.1:{args.feed_port}")
    else:
        alert(f"{feed.error}\n\nThe RDS Monitor's Log button cannot reach the "
              "map. Close any other DX Mapper and start it again.")
    url = f"http://127.0.0.1:{args.web_port}/"
    lines = [f"Map: {url}", f"Log: {app.store.path}", f"Excel copy: {app.excel.path}"]
    if app.monitor_dir:
        lines.insert(1, f"RDS Monitor folder: {app.monitor_dir}")
    for line in lines:
        print("[ok] " + line)
    app.queue_missing_geo()
    app.queue_missing_wtfda()
    try:
        if args.no_browser:
            print("Running without a window; Ctrl+C quits.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        else:
            webview = None if args.browser else load_webview()
            shown = False
            if webview:
                try:
                    run_window(webview, url, lambda: station_title(app.store))
                    shown = True
                except Exception as e:
                    print(f"[!] program window failed: {e!r}")
            if not shown:
                if not args.browser:
                    alert("The DX Mapper window could not be opened on this PC, "
                          "so the map opens in your web browser instead. Keep "
                          "the small DX Mapper window open while logging.")
                webbrowser.open(url)
                run_control_panel(url, lines)
    finally:
        feed.stop()
        app.excel.stop()
        app.excel.save_now()
        app.close_logs()
    return 0


if __name__ == "__main__":
    sys.exit(main())
