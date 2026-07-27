"""Manual smoke test for the Pending for Image column: send two different
banks from New Bank, verify they queue up in order, reorder them, rename
one, send one back to New Bank via double-click, then Build Image the rest
onto a real EMU3 HD image and confirm it landed with the right content and
in the right order. Same in-process-call approach as every other smoke
test here (no X11 input automation available).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QModelIndex, QThreadPool, Qt
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from vinsamlib import mpc2emu_bridge
from vinsamlib.build import images
from vinsamlib.config import Config
from vinsamlib.ui.main_window import MainWindow

from _qtest_shim import qwait

# Both of these dialogs are real modal QMessageBox/QInputDialog calls --
# fine for a human, but they block forever with no one to click them in
# this offscreen harness.
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
_RENAME_TO = "RenamedPending"
QInputDialog.getText = staticmethod(lambda *a, **k: (_RENAME_TO, True))

OUT = Path(tempfile.gettempdir()) / "vinsamlib_manual_smoke"
OUT.mkdir(parents=True, exist_ok=True)
IMG_DIR = Path.home() / "temp" / "vinsamlib_m6_ui"
IMG_DIR.mkdir(parents=True, exist_ok=True)

E4B_DIR = Path.home() / "Dokumente/SYNTHS/E4XT/E4Bs/Rob.Papen-Techno.Synth.Construction.Yard.E4/Techno Synths RP"


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


def expand_and_wait(tree, index, ms=1500):
    # index is a *source*-model index (built by walking win._model
    # directly); the tree's own model is now BankFormatFilterProxy
    # (see explorer_pane.py), so it has to be mapped into proxy space
    # before the view will accept it.
    proxy = tree.model()
    if hasattr(proxy, "mapFromSource"):
        index = proxy.mapFromSource(index)
    tree.expand(index)
    qwait(ms)


def send_current_bank_to_pending(win, name):
    win._bank_pane._name_edit.setText(name)
    qwait(1200)   # let the size-meter worker finish so _last_bytes is set
    win._bank_pane._send_to_pending()
    qwait(200)


def wait_workers(pane):
    while pane._live_workers:
        qwait(50)
    qwait(150)


def main():
    config = Config.load()
    mpc2emu_bridge.install(config)
    config.library_roots = [Path.home() / "Dokumente" / "SYNTHS" / "E4XT" / "ISO-Images"]

    app = QApplication(sys.argv)
    win = MainWindow(config)
    win._pending_pane.statusMessage.connect(lambda m: print("  [pending status]", m))
    win.resize(1360, 800)
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

    names = [model.data(model.index(r, 0, bank), Qt.ItemDataRole.DisplayRole)
              for r in range(min(3, model.rowCount(bank)))]
    print("presets available:", names)

    # 1) Bank A: preset 0
    mime_a = model.mimeData([model.index(0, 0, bank)])
    win._bank_pane.dropEvent(FakeDropEvent(mime_a))
    send_current_bank_to_pending(win, "BankA")
    print("pending after BankA:", [e["name"] for e in win._pending_pane._pending])
    assert [e["name"] for e in win._pending_pane._pending] == ["BankA"]

    win._bank_pane._clear()
    qwait(100)

    # 2) Bank B: preset 1 (different content)
    mime_b = model.mimeData([model.index(1, 0, bank)])
    win._bank_pane.dropEvent(FakeDropEvent(mime_b))
    send_current_bank_to_pending(win, "BankB")
    print("pending after BankB:", [e["name"] for e in win._pending_pane._pending])
    assert [e["name"] for e in win._pending_pane._pending] == ["BankA", "BankB"]
    grab(win, "pend_01_two_pending.png")

    # 2.5) BankC: 2 presets, to test reordering *within* a pending bank via
    #      the Contents box (distinct from reordering the pending banks
    #      themselves, tested next)
    win._bank_pane._clear()
    qwait(100)
    mime_c = model.mimeData([model.index(0, 0, bank), model.index(1, 0, bank)])
    win._bank_pane.dropEvent(FakeDropEvent(mime_c))
    send_current_bank_to_pending(win, "BankC")
    assert [e["name"] for e in win._pending_pane._pending] == ["BankA", "BankB", "BankC"]

    win._pending_pane._list.setCurrentRow(2)
    qwait(100)
    contents_before = [win._pending_pane._contents_list.item(i).text()
                       for i in range(win._pending_pane._contents_list.count())]
    print("BankC contents before reorder:", contents_before)
    assert len(contents_before) == 2

    contents_model = win._pending_pane._contents_list.model()
    contents_model.moveRow(QModelIndex(), 0, QModelIndex(), 2)
    qwait(150)
    contents_after = [win._pending_pane._contents_list.item(i).text()
                       for i in range(win._pending_pane._contents_list.count())]
    print("BankC contents after reorder:", contents_after)
    assert contents_after == list(reversed(contents_before)), contents_after
    entry_names = [name for _b, _p, name in win._pending_pane._pending[2]["items"]]
    print("BankC pending entry items after reorder:", entry_names)
    assert entry_names == contents_after, "Contents reorder didn't sync into the pending recipe"
    grab(win, "pend_01b_contents_reordered.png")

    win._pending_pane._delete_selected()   # done with BankC, keep the rest of the test as before
    qwait(100)
    assert [e["name"] for e in win._pending_pane._pending] == ["BankA", "BankB"]

    # 3) reorder: move BankA (row 0) after BankB (to the end)
    pending_model = win._pending_pane._list.model()
    pending_model.moveRow(QModelIndex(), 0, QModelIndex(), 2)
    qwait(100)
    order = [e["name"] for e in win._pending_pane._pending]
    print("pending order after reorder:", order)
    assert order == ["BankB", "BankA"], order
    grab(win, "pend_02_reordered.png")

    # 4) rename BankA (now at row 1) via the real _rename_selected (QInputDialog stubbed)
    win._pending_pane._list.setCurrentRow(1)
    win._pending_pane._rename_selected()
    qwait(100)
    order = [e["name"] for e in win._pending_pane._pending]
    print("pending order after rename:", order)
    assert order == ["BankB", _RENAME_TO], order
    grab(win, "pend_03_renamed.png")

    # 5) double-click BankB (row 0) -> sent back to New Bank, removed from pending
    item = win._pending_pane._list.item(0)
    win._pending_pane._list.setCurrentItem(item)
    win._pending_pane._move_selected_to_new_bank()
    qwait(200)
    print("pending after move-back:", [e["name"] for e in win._pending_pane._pending])
    print("New Bank name field:", win._bank_pane._name_edit.text())
    print("New Bank item count:", len(win._bank_pane._items))
    assert [e["name"] for e in win._pending_pane._pending] == [_RENAME_TO]
    assert win._bank_pane._name_edit.text() == "BankB"
    assert len(win._bank_pane._items) == 1
    grab(win, "pend_04_moved_back_to_newbank.png")

    # 6) Build Image with just the renamed pending bank, onto a fresh EMU3 HD image
    starter = IMG_DIR / "pending_test.hda"
    starter.unlink(missing_ok=True)
    seed_bank = str(E4B_DIR / "B.007-Dance Organ   RP.e4b")
    images.create_image("emu3_hd_emu", str(starter), [seed_bank], volume_label="PENDTEST", size_mb=32)
    win._image_pane._open_image(str(starter))
    qwait(200)

    win._pending_pane._build_image()
    wait_workers(win._pending_pane)
    while win._image_pane._busy:
        qwait(50)
    qwait(150)
    grab(win, "pend_05_built.png")

    entries = [e.name for e in win._image_pane._entries]
    print("image entries after Build Image:", entries)
    assert any("RENAMEDPEN" in n.upper() for n in entries), entries
    # Build Image no longer auto-empties the queue -- the recipe stays
    # available to rebuild/tweak/send elsewhere until explicitly cleared.
    print("pending after build (should be unchanged):", len(win._pending_pane._pending))
    assert len(win._pending_pane._pending) == 1

    # 7) Clear button empties it explicitly
    win._pending_pane._clear()
    qwait(100)
    print("pending after Clear:", len(win._pending_pane._pending))
    assert len(win._pending_pane._pending) == 0
    assert win._pending_pane._format is None

    print("\nALL PENDING-QUEUE SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
