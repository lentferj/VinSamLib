"""Manual smoke test for XPM import (Config.check_xpm_import_support(),
build/xpm_import.py, ui/xpm_import_dialog.py, File > Import XPM... wiring
in main_window.py): drive the real File > Import XPM... flow against a
real Akai MPC XPM program and confirm it lands directly in "Pending for
Image" as a one-preset bank recipe -- no save dialog, no library folder
at all. An XPM always holds exactly one preset (mpc2emu's own
parse_xpm() appends exactly one Preset, never more, per its own
docstring), so there's nothing to pick from and nowhere else it needs to
go first; MainWindow._on_xpm_imported() re-reads the converted temp file
with VinSamLib's own e4b.py/krz.py reader to get a real (bank, preset)
pair, same shape a drag from Explorer would produce. Same in-process-call
approach as every other smoke test here (no X11 input automation
available); the two modal points this flow hits (file-open, the options
dialog) are stubbed the same way the other smoke tests stub
QMessageBox/QInputDialog/QFileDialog.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QCoreApplication, QThreadPool
from PySide6.QtWidgets import QApplication, QFileDialog

from vinsamlib import mpc2emu_bridge
from vinsamlib.build.xpm_import import XpmImportOptions
from vinsamlib.config import Config
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

    app = QApplication(sys.argv)
    win = MainWindow(config)
    win.statusBar().messageChanged.connect(lambda m: print("  [status]", m) if m else None)

    ok, reason = config.check_xpm_import_support()
    print("check_xpm_import_support:", ok, reason)
    assert ok, "this smoke test needs a real mpc2emu checkout with parsers/xpm_parser.py"

    assert not win._pending_pane._pending, "pending queue should start empty"

    win._import_xpm()

    deadline = time.time() + 60
    while win._xpm_import_worker is not None and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.1)
    assert win._xpm_import_worker is None, "import never completed"

    pending = win._pending_pane._pending
    print("pending queue after import:", [(e["name"], e["format"], len(e["items"])) for e in pending])
    assert len(pending) == 1, "the imported XPM should land as exactly one pending entry"
    entry = pending[0]
    assert entry["format"] == "E4B"
    assert len(entry["items"]) == 1, "an XPM always holds exactly one preset"

    bank, preset, name = entry["items"][0]
    print("pending entry's bank/preset:", type(bank).__name__, type(preset).__name__, name)
    from vinsamlib.banks.e4b import E4BFile, E4BPreset
    assert isinstance(bank, E4BFile) and isinstance(preset, E4BPreset), (
        "must be VinSamLib's own reader objects, not mpc2emu's Bank/Preset "
        "-- assemble()/Pending's own machinery only knows how to handle these")

    # No library folder was created or registered anywhere for this.
    assert win._config.library_roots == []

    print("\nALL XPM IMPORT SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
