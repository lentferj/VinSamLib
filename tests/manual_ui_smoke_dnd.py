"""Manual smoke test for M5: drag-and-drop into the New Bank column.
Drives the real model.mimeData()/BankPane.dropEvent() code paths directly
(a fake drop event standing in for real mouse-drag simulation, which needs
X11 input tools not available here) rather than a person dragging a mouse.
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


class FakeDropEvent:
    def __init__(self, mime):
        self._mime = mime
        self.accepted = None

    def mimeData(self):
        return self._mime

    def acceptProposedAction(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


def grab(win, name):
    win.grab().save(str(OUT / name))
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
    config.library_roots = [Path.home() / "Dokumente" / "SYNTHS" / "E4XT" / "ISO-Images"]

    app = QApplication(sys.argv)
    win = MainWindow(config)
    win.resize(1280, 800)
    win.show()
    qwait(300)

    tree = win._explorer._tree
    model = win._model

    root = model.index(0, 0, QModelIndex())
    expand_and_wait(tree, root)
    iso = next(model.index(r, 0, root) for r in range(model.rowCount(root))
               if "Post Industrial" in model.data(model.index(r, 0, root), Qt.ItemDataRole.DisplayRole))
    expand_and_wait(tree, iso)
    folder = model.index(0, 0, iso)
    expand_and_wait(tree, folder)
    bank = next(model.index(r, 0, folder) for r in range(model.rowCount(folder))
                if "[E4B]" in model.data(model.index(r, 0, folder), Qt.ItemDataRole.DisplayRole))
    expand_and_wait(tree, bank, ms=3000)

    n = model.rowCount(bank)
    print("presets available:", n)
    preset_indexes = [model.index(i, 0, bank) for i in range(min(3, n))]
    for idx in preset_indexes:
        print(" ", model.data(idx, Qt.ItemDataRole.DisplayRole))

    # 1) drag a single preset in
    mime = model.mimeData([preset_indexes[0]])
    assert mime is not None, "mimeData() returned None for a preset index"
    ev = FakeDropEvent(mime)
    win._bank_pane.dropEvent(ev)
    print("drop1 accepted:", ev.accepted, "items:", len(win._bank_pane._items),
          "format:", win._bank_pane._format)
    qwait(800)
    grab(win, "dnd_01_one_preset.png")

    # 2) drag two more presets in (multi-select style: separate mimeData calls)
    for idx in preset_indexes[1:]:
        mime = model.mimeData([idx])
        ev = FakeDropEvent(mime)
        win._bank_pane.dropEvent(ev)
    qwait(1200)
    grab(win, "dnd_02_three_presets.png")
    print("items now:", len(win._bank_pane._items), "meter:", win._bank_pane._meter_label.text())

    # 3) try to remove one
    win._bank_pane._list.setCurrentRow(0)
    win._bank_pane._remove_selected()
    qwait(800)
    grab(win, "dnd_03_after_remove.png")
    print("items after remove:", len(win._bank_pane._items))

    # 4) verify assemble() actually works on the accumulated selection
    from vinsamlib.banks import e4b as e4b_mod
    selections = [(b, p) for b, p, _n in win._bank_pane._items]
    data = e4b_mod.assemble(selections)
    print("assembled bytes:", len(data), "starts with FORM/E4B0:",
          data[:4] == b"FORM" and data[8:12] == b"E4B0")

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
