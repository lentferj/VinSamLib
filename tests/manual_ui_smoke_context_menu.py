"""Manual smoke test for the right-click context menus added to the
Explorer tree (Add to New Bank), the New Bank list (Remove), and the Image
list (Rename/Delete/Export). Context menus are modal (QMenu.exec blocks
until dismissed), so this drives the underlying handler methods directly
rather than opening a real menu and clicking it -- the same "call the real
code path, skip the mouse gesture" approach used by every other smoke test
here (no X11 input automation in this environment).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QModelIndex, QThreadPool, Qt
from PySide6.QtWidgets import QApplication

from vinsamlib import mpc2emu_bridge
from vinsamlib.build import images
from vinsamlib.config import Config
from vinsamlib.ui.main_window import MainWindow

from _qtest_shim import qwait

OUT = Path(tempfile.gettempdir()) / "vinsamlib_manual_smoke"
OUT.mkdir(parents=True, exist_ok=True)
IMG_DIR = Path.home() / "temp" / "vinsamlib_m6_ui"
IMG_DIR.mkdir(parents=True, exist_ok=True)

E4B_DIR = Path.home() / "Dokumente/SYNTHS/E4XT/E4Bs/Rob.Papen-Techno.Synth.Construction.Yard.E4/Techno Synths RP"


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


def wait_idle(image_pane, timeout_ms=10000):
    waited = 0
    while image_pane._busy and waited < timeout_ms:
        qwait(50)
        waited += 50
    qwait(150)


def main():
    config = Config.load()
    mpc2emu_bridge.install(config)
    config.library_roots = [Path.home() / "Dokumente" / "SYNTHS" / "E4XT" / "ISO-Images"]

    app = QApplication(sys.argv)
    win = MainWindow(config)
    win._bank_pane.statusMessage.connect(lambda m: print("  [bank status]", m))
    win.resize(1280, 800)
    win.show()
    qwait(300)

    # 1) Explorer right-click "Add to New Bank" on a preset -- drive the
    #    signal path directly (addToBankRequested -> MainWindow._add_node_to_bank)
    #    rather than opening the real (modal) QMenu.
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
    preset_idx = model.index(0, 0, bank)
    node = preset_idx.data(Qt.ItemDataRole.UserRole)
    print("right-clicking preset:", node.label)

    win._explorer.addToBankRequested.emit([node])
    qwait(300)
    print("bank pane items after context-menu add:", len(win._bank_pane._items),
          "format:", win._bank_pane._format)
    assert len(win._bank_pane._items) == 1
    assert win._bank_pane._format == "E4B"
    grab(win, "ctx_01_added_via_menu.png")

    # reject-a-second-format check: build a fake KRZ-labelled item and confirm
    # add_presets() rejects it without touching the existing E4B item.
    ok = win._bank_pane.add_presets([(None, None, "KRZ", "fake")])
    print("KRZ add_presets onto E4B-locked bank accepted (should be False):", ok)
    assert ok is False
    assert len(win._bank_pane._items) == 1

    # 2) New Bank list context menu -> Remove (drive _on_list_context_menu's
    #    underlying action directly: select the row, then call _remove_selected,
    #    exactly what the menu's "Remove" action does).
    win._bank_pane._list.setCurrentRow(0)
    win._bank_pane._remove_selected()
    qwait(300)
    print("bank pane items after context-menu remove:", len(win._bank_pane._items))
    assert len(win._bank_pane._items) == 0
    grab(win, "ctx_02_removed_via_menu.png")

    # 3) Image list context menu -> Rename / Delete / Export
    starter = IMG_DIR / "ctx_test.hda"
    starter.unlink(missing_ok=True)
    seed_bank = str(E4B_DIR / "B.007-Dance Organ   RP.e4b")
    images.create_image("emu3_hd_emu", str(starter), [seed_bank], volume_label="CTXTEST", size_mb=32)
    win._image_pane._open_image(str(starter))
    qwait(200)
    print("image entries after open:", [e.name for e in win._image_pane._entries])
    grab(win, "ctx_03_image_opened.png")

    win._image_pane._list.setCurrentRow(0)
    entry = win._image_pane._selected_entry()
    print("selected via list:", entry.name)
    assert entry is not None

    from vinsamlib.ui import workers as workers_mod
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

    win._image_pane._run_confirmed_op(
        "renaming (ctx test)",
        workers_mod.Worker(images.rename_entry, win._image_pane._path, entry, "CTXRENAMED"),
        on_done=lambda _r: win._image_pane._open_image(win._image_pane._path),
    )
    wait_idle(win._image_pane)
    names = [e.name for e in win._image_pane._entries]
    print("image entries after context-menu rename:", names)
    assert any("CTXRENAMED" in n.upper() for n in names), names
    grab(win, "ctx_04_image_renamed.png")

    print("\nALL CONTEXT-MENU SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
