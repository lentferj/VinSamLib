"""Manual smoke test for XPM import (Config.check_xpm_import_support(),
build/xpm_import.py, ui/xpm_import_dialog.py, File > Import XPM... wiring
in main_window.py): drive the real File > Import XPM... flow against a
real Akai MPC XPM program and confirm it lands in "New Bank" as a single
program/preset -- no save dialog, no library folder, no Pending entry of
its own. An XPM always holds exactly one preset (mpc2emu's own
parse_xpm() appends exactly one Preset, never more, per its own
docstring), so it's treated exactly like dragging one preset in from
Explorer: MainWindow._on_xpm_imported() re-reads the converted temp
file's bytes but labels the result with the ORIGINAL xpm path (not the
throwaway temp path) so BankPane's own duplicate-check correctly
recognizes re-importing the same XPM as the same preset, then hands it
to BankPane.add_presets(). Same in-process-call approach as every other
smoke test here (no X11 input automation available); the two modal
points this flow hits (file-open, the options dialog) are stubbed the
same way the other smoke tests stub QMessageBox/QInputDialog/QFileDialog.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QCoreApplication, QThreadPool
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from vinsamlib import mpc2emu_bridge
from vinsamlib.banks.e4b import E4BFile, E4BPreset
from vinsamlib.build.convert import ConversionOptions
from vinsamlib.config import Config
from vinsamlib.ui.main_window import MainWindow
from vinsamlib.ui.xpm_import_dialog import XpmImportDialog

XPM_PATH = str(Path.home() / "Samples/MPC/Roland Alpha Juno 2/43 Floating.Keygroup.xpm")

QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (XPM_PATH, ""))
XpmImportDialog.get_import_options = staticmethod(
    lambda parent=None, initial=None, title="Import XPM", warning_text=None,
    locked_format=None: ConversionOptions(target_format=locked_format or "E4B"))
# BankPane's duplicate check defaults to "prompt before skipping" -- the
# re-import check below deliberately exercises that path, so answering
# "No" (don't add the duplicate) here is what makes it deterministic.
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)


def _wait_import(win, timeout=60):
    deadline = time.time() + timeout
    while win._xpm_import_worker is not None and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.1)
    assert win._xpm_import_worker is None, "import never completed"


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

    assert not win._bank_pane._items, "New Bank should start empty"
    assert not win._pending_pane._pending, "Pending should start (and stay) empty"

    win._import_xpm()
    _wait_import(win)

    items = win._bank_pane._items
    print("New Bank items after import:", [(name, type(b).__name__, type(p).__name__)
                                            for b, p, name in items])
    assert len(items) == 1, "the imported XPM should land as exactly one New Bank item"
    bank, preset, name = items[0]
    assert isinstance(bank, E4BFile) and isinstance(preset, E4BPreset), (
        "must be VinSamLib's own reader objects, not mpc2emu's Bank/Preset "
        "-- assemble() only knows how to handle these")
    assert not win._pending_pane._pending, "should not have created a Pending entry"

    # Re-importing the SAME xpm should be caught as a duplicate (not a
    # second, distinct New Bank item) -- this is what labeling the parsed
    # bank with the original xpm_path (not the temp path) is for.
    win._import_xpm()
    _wait_import(win)
    print("New Bank items after re-import:", len(win._bank_pane._items))
    assert len(win._bank_pane._items) == 1, "re-importing the same XPM should be deduped, not doubled"

    # No library folder was created or registered anywhere for any of this.
    assert win._config.library_roots == []

    print("\nALL XPM IMPORT SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
