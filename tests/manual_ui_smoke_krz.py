"""Manual smoke test for the KRZ path through the M3 UI (floppy image -> bank -> program)."""
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
    pix.save(str(OUT / name))
    print("saved", name)


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
    config.library_roots = [Path.home() / "disk-image" / "Monotanz-144" / "Woodwind"]

    app = QApplication(sys.argv)
    win = MainWindow(config)
    win.resize(1280, 800)
    win._toggle_samples_column(True)
    win.show()
    qwait(300)

    tree = win._explorer._tree
    model = win._model
    root_index = model.index(0, 0, QModelIndex())
    expand_and_wait(tree, root_index)
    grab(win, "krz_01_root.png")

    img_index = None
    for row in range(model.rowCount(root_index)):
        idx = model.index(row, 0, root_index)
        if "bassclar" in model.data(idx, Qt.ItemDataRole.DisplayRole):
            img_index = idx
            break
    if img_index is None:
        print("bassclar floppy not found")
        return
    expand_and_wait(tree, img_index)
    grab(win, "krz_02_floppy.png")

    if model.rowCount(img_index) == 0:
        print("no bank on floppy")
        return
    bank_index = model.index(0, 0, img_index)
    print("bank:", model.data(bank_index, Qt.ItemDataRole.DisplayRole))
    expand_and_wait(tree, bank_index, ms=1500)
    grab(win, "krz_03_bank.png")

    if model.rowCount(bank_index) == 0:
        print("no programs in bank")
        return
    prog_index = model.index(0, 0, bank_index)
    print("program:", model.data(prog_index, Qt.ItemDataRole.DisplayRole))
    tree.setCurrentIndex(prog_index)
    qwait(1500)
    grab(win, "krz_04_program_selected.png")
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
