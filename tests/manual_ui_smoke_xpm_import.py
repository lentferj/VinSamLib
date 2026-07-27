"""Manual smoke test for XPM import (Config.check_xpm_import_support(),
build/xpm_import.py, ui/xpm_import_dialog.py, File > Import XPM... wiring
in main_window.py): drive the real File > Import XPM... flow against a
real Akai MPC XPM program, confirm the resulting E4B bank is written,
offered for addition to the library, gets scanned, and reopens correctly
through VinSamLib's own e4b.py reader. Same in-process-call approach as
every other smoke test here (no X11 input automation available); the
three modal points this flow hits (file-open, the options dialog,
save-as, and the "add to library?" confirmation) are stubbed the same
way the other smoke tests stub QMessageBox/QInputDialog/QFileDialog.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QCoreApplication, QThreadPool
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from vinsamlib import mpc2emu_bridge
from vinsamlib.banks import e4b as vs_e4b
from vinsamlib.build.xpm_import import XpmImportOptions
from vinsamlib.config import Config
from vinsamlib.ui.main_window import MainWindow
from vinsamlib.ui.xpm_import_dialog import XpmImportDialog

XPM_PATH = str(Path.home() / "Samples/MPC/Roland Alpha Juno 2/43 Floating.Keygroup.xpm")

QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (XPM_PATH, ""))
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)


def main():
    if not Path(XPM_PATH).exists():
        print(f"SKIPPED: {XPM_PATH} not found on this machine")
        return

    config = Config.load()
    mpc2emu_bridge.install(config)
    config.library_roots = []

    dest_dir = Path(tempfile.mkdtemp(prefix="vinsamlib_xpm_import_smoke_"))
    dest_path = str(dest_dir / "Floating.e4b")
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (dest_path, ""))
    XpmImportDialog.get_import_options = staticmethod(
        lambda parent=None, initial=None: XpmImportOptions(target_format="E4B"))

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

    print("dest exists:", Path(dest_path).exists())
    assert Path(dest_path).exists()
    print("library_roots:", win._config.library_roots)
    assert dest_dir in win._config.library_roots, "should have been offered and accepted"

    deadline = time.time() + 20
    while win._scan_worker is not None and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.1)

    bank = vs_e4b.parse(dest_path)
    print("reopened imported bank -- presets:", len(bank.presets), "samples:", len(bank.samples))
    assert len(bank.presets) >= 1

    print("\nALL XPM IMPORT SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
