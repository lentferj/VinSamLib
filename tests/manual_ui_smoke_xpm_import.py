"""Manual smoke test for XPM import (Config.check_xpm_import_support(),
build/xpm_import.py, ui/xpm_import_dialog.py, File > Import XPM... wiring
in main_window.py): drive the real File > Import XPM... flow against a
real Akai MPC XPM program, confirm the resulting E4B bank is written
straight into Config.xpm_imports_dir() (no save dialog, no "add to
library?" prompt -- see MainWindow._on_xpm_imported()), gets scanned, and
reopens correctly through VinSamLib's own e4b.py reader. Same
in-process-call approach as every other smoke test here (no X11 input
automation available); the two modal points this flow still hits
(file-open, the options dialog) are stubbed the same way the other smoke
tests stub QMessageBox/QInputDialog/QFileDialog.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QCoreApplication, QThreadPool
from PySide6.QtWidgets import QApplication, QFileDialog

from vinsamlib import mpc2emu_bridge
from vinsamlib.banks import e4b as vs_e4b
from vinsamlib.build.xpm_import import XpmImportOptions
from vinsamlib.config import Config, xpm_imports_dir
from vinsamlib.ui.main_window import MainWindow
from vinsamlib.ui.xpm_import_dialog import XpmImportDialog

XPM_PATH = str(Path.home() / "Samples/MPC/Roland Alpha Juno 2/43 Floating.Keygroup.xpm")

QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (XPM_PATH, ""))
XpmImportDialog.get_import_options = staticmethod(
    lambda parent=None, initial=None: XpmImportOptions(target_format="E4B"))


def main():
    if not Path(XPM_PATH).exists():
        print(f"SKIPPED: {XPM_PATH} not found on this machine")
        return

    config = Config.load()
    mpc2emu_bridge.install(config)
    config.library_roots = []

    dest = xpm_imports_dir() / f"{Path(XPM_PATH).stem}.e4b"
    dest.unlink(missing_ok=True)   # idempotent re-runs -- see manual_ui_smoke_pending.py's convention

    app = QApplication(sys.argv)
    win = MainWindow(config)
    win.statusBar().messageChanged.connect(lambda m: print("  [status]", m) if m else None)

    ok, reason = config.check_xpm_import_support()
    print("check_xpm_import_support:", ok, reason)
    assert ok, "this smoke test needs a real mpc2emu checkout with parsers/xpm_parser.py"

    win._import_xpm()

    deadline = time.time() + 60
    while win._xpm_import_worker is not None and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.1)
    assert win._xpm_import_worker is None, "import never completed"

    print("dest exists (auto-saved, no dialog):", dest.exists())
    assert dest.exists()
    print("library_roots:", win._config.library_roots)
    assert xpm_imports_dir() in win._config.library_roots, "imports dir should auto-register itself"

    deadline = time.time() + 20
    while win._scan_worker is not None and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.1)

    bank = vs_e4b.parse(str(dest))
    print("reopened imported bank -- presets:", len(bank.presets), "samples:", len(bank.samples))
    assert len(bank.presets) >= 1

    print("\nALL XPM IMPORT SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
