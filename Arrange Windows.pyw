"""Choose which screen each program goes on and arrange them (no console)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import arrange_windows  # noqa: E402

arrange_windows.main()
