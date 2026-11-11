"""Generates packaging/hgs.ico from the same drawing the GUI uses (run once; the .ico is committed)."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QGuiApplication  # noqa: E402

app = QGuiApplication(sys.argv)
from hgs.gui.main_window import app_icon  # noqa: E402

out = Path(__file__).with_name("hgs.ico")
pm = app_icon().pixmap(256, 256)
assert pm.save(str(out), "ICO"), "Qt could not write the ICO file"
print(out)
