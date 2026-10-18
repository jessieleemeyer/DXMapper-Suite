"""Start the FM Tuner, RDS Monitor and DX Mapper together (no console)."""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = Path(sys.executable)
PYW = PY.with_name("pythonw.exe") if PY.with_name("pythonw.exe").exists() else PY

PARTS = [  # (old folder, script, seconds to wait before the next one)
    ("FM-Tuner", "FM_Tuner.py", 3),
    ("RDS-Monitor", "RDS Monitor.py", 2),
    ("DX-Mapper", "DX Mapper.py", 0),
]


def main():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        sys.path.insert(0, str(ROOT))
        import suite_start
        suite_start.migrate(ROOT)
    except Exception:
        pass
    missing = []
    for folder, script, pause in PARTS:
        path = ROOT / script  # one-folder suite
        if not path.exists():
            path = ROOT / folder / script
        if not path.exists():
            missing.append(script)
            continue
        subprocess.Popen([str(PYW), str(path)], cwd=str(path.parent),
                         creationflags=flags, close_fds=True)
        time.sleep(pause)
    aw = settings = None
    try:
        sys.path.insert(0, str(ROOT))
        import arrange_windows as aw
        settings = aw.load_settings()
        if settings.get("on_start", True):
            aw.arrange(wait_s=60, use_last=settings.get("remember", True))
    except Exception:
        pass
    if missing:
        try:
            import tkinter
            from tkinter import messagebox
            r = tkinter.Tk()
            r.withdraw()
            messagebox.showwarning(
                "Start Radio", "Not found next to Start Radio:\n\n"
                + "\n".join(missing))
            r.destroy()
        except Exception:
            pass
    if aw and settings is not None and settings.get("remember", True):
        try:
            aw.remember()  # until the programs are closed
        except Exception:
            pass


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
