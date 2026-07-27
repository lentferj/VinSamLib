"""Manual, screenshot-driven smoke test for the M3 UI — not pytest, just a
script that drives the real app in-process (via processEvents (see _qtest_shim.py), no X11
input automation needed) against the real library and dumps PNGs for visual
review. Run with: DISPLAY=:0 python3 tests/manual_ui_smoke.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QModelIndex, Qt, QThreadPool
from PySide6.QtWidgets import QApplication

from vinsamlib import mpc2emu_bridge
from vinsamlib.config import Config
from vinsamlib.ui.main_window import MainWindow

from _qtest_shim import qwait

OUT = Path(tempfile.gettempdir()) / "vinsamlib_manual_smoke"
OUT.mkdir(parents=True, exist_ok=True)


def grab(win, name):
    pix = win.grab()
    path = OUT / name
    pix.save(str(path))
    print("saved", path)


def expand_and_wait(tree, index, ms=2000):
    # index is a *source*-model index (built by walking win._model
    # directly); the tree's own model is now BankFormatFilterProxy
    # (see explorer_pane.py), so it has to be mapped into proxy space
    # before the view will accept it.
    proxy = tree.model()
    if hasattr(proxy, "mapFromSource"):
        index = proxy.mapFromSource(index)
    tree.expand(index)
    qwait(ms)


def main():
    config = Config.load()
    mpc2emu_bridge.install(config)
    config.library_roots = [Path.home() / "Dokumente" / "SYNTHS" / "E4XT" / "ISO-Images"]

    app = QApplication(sys.argv)
    win = MainWindow(config)
    win.resize(1280, 800)
    win.show()
    qwait(300)
    grab(win, "smoke_01_initial.png")

    tree = win._explorer._tree
    model = win._model

    root_index = model.index(0, 0, QModelIndex())
    print("root:", model.data(root_index, Qt.ItemDataRole.DisplayRole))
    expand_and_wait(tree, root_index)
    grab(win, "smoke_02_root_expanded.png")

    if model.rowCount(root_index) == 0:
        print("NO CHILDREN under root — stopping")
        return

    iso_index = None
    for row in range(model.rowCount(root_index)):
        idx = model.index(row, 0, root_index)
        if "Post Industrial" in model.data(idx, Qt.ItemDataRole.DisplayRole):
            iso_index = idx
            break
    if iso_index is None:
        print("Post Industrial disc not found — stopping")
        return
    print("iso:", model.data(iso_index, Qt.ItemDataRole.DisplayRole))
    expand_and_wait(tree, iso_index)
    grab(win, "smoke_03_iso_expanded.png")

    if model.rowCount(iso_index) == 0:
        print("NO CHILDREN under iso — stopping")
        return

    folder_index = model.index(0, 0, iso_index)
    print("folder:", model.data(folder_index, Qt.ItemDataRole.DisplayRole))
    expand_and_wait(tree, folder_index)
    grab(win, "smoke_04_folder_expanded.png")

    if model.rowCount(folder_index) == 0:
        print("NO CHILDREN under folder — stopping")
        return

    # find a bank whose label doesn't look like it errored
    bank_index = None
    for row in range(model.rowCount(folder_index)):
        idx = model.index(row, 0, folder_index)
        label = model.data(idx, Qt.ItemDataRole.DisplayRole)
        print("  bank candidate:", label)
        if "[E4B]" in label:
            bank_index = idx
            break
    if bank_index is None:
        print("NO E4B BANK FOUND — stopping")
        return

    expand_and_wait(tree, bank_index, ms=3000)
    grab(win, "smoke_05_bank_expanded.png")

    if model.rowCount(bank_index) == 0:
        print("NO PRESETS under bank — stopping")
        return

    preset_index = model.index(1, 0, bank_index)   # "kit: Ambi-World" — has real zones
    print("preset:", model.data(preset_index, Qt.ItemDataRole.DisplayRole))
    tree.setCurrentIndex(preset_index)
    qwait(2500)
    grab(win, "smoke_06_preset_selected.png")

    # toggle samples column on and re-grab
    win._toggle_samples_column(True)
    qwait(500)
    grab(win, "smoke_07_samples_shown.png")

    print("done")


if __name__ == "__main__":
    try:
        main()
    finally:
        # Drain the shared thread pool before the interpreter starts
        # tearing down Qt objects: under PySide6/Shiboken, a worker
        # still mid-flight at that point raises a hard "Signal source
        # has been deleted" RuntimeError from its own background
        # thread when it emits (PyQt5 tolerated the same race
        # silently) -- this is what closeEvent() also does for a real
        # run, but this script never gets that far.
        QThreadPool.globalInstance().waitForDone(5000)
