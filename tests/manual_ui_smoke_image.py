"""Manual smoke test for M6: the Image column's real UI, driven the same
way as the M3/M4/M5 smoke tests — direct model/widget calls plus manual event pumping (see _qtest_shim.py)
waits rather than simulated mouse events (no X11 input automation here) —
with QWidget.grab() screenshots for visual review. Also exercises the M6
drag-out handle added to BankPane by calling its provider + ImagePane's
dropEvent directly, mirroring how manual_ui_smoke_dnd.py drove BankPane.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QMimeData, QModelIndex, QThreadPool, QUrl, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from vinsamlib import mpc2emu_bridge
from vinsamlib.build import images
from vinsamlib.config import Config
from vinsamlib.ui.main_window import MainWindow

from _qtest_shim import qwait

# The pane's confirm-before-mutating dialog (build/images.py's safety copy
# still applies underneath) is a real modal QMessageBox.question() — fine
# for a human, but it blocks forever with no one to click it in this
# offscreen harness. Auto-answer Yes so the drag-drop append path (the one
# real flow in this test that goes through _confirm()) can proceed.
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

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
    """Poll until the pane's background op finishes, instead of a fixed
    sleep — the safety wrapper's copy-then-replace step scales with image
    size, so a flat wait would either be flaky (too short) or slow (long
    enough for the worst case every time)."""
    waited = 0
    while image_pane._busy and waited < timeout_ms:
        qwait(50)
        waited += 50
    qwait(150)   # let the finished-signal's on_done callback run


def main():
    config = Config.load()
    mpc2emu_bridge.install(config)
    config.library_roots = [Path.home() / "Dokumente" / "SYNTHS" / "E4XT" / "ISO-Images"]

    app = QApplication(sys.argv)
    win = MainWindow(config)
    win._image_pane.statusMessage.connect(lambda m: print("  [status]", m))
    win.resize(1280, 800)
    win.show()
    qwait(300)
    grab(win, "img_00_initial.png")

    # 1) build a starter E4B image directly via the safety wrapper (the
    #    New… dialog itself is a modal QDialog.exec() loop that can't be
    #    driven headlessly here; images.py underneath it is already
    #    covered end-to-end by tests/manual_image_ops_smoke.py, so this
    #    test focuses on the pane's *open/list/append/rename/delete/export*
    #    wiring against a real image).
    starter = IMG_DIR / "ui_test.hda"
    starter.unlink(missing_ok=True)
    seed_bank = str(E4B_DIR / "B.007-Dance Organ   RP.e4b")
    images.create_image("emu3_hd_emu", str(starter), [seed_bank], volume_label="UITEST", size_mb=32)
    print("seed image built:", starter)

    win._image_pane._open_image(str(starter))
    qwait(200)
    grab(win, "img_01_opened.png")
    print("entries after open:", [e.name for e in win._image_pane._entries])
    assert win._image_pane._format == "E4B"
    assert win._image_pane._appendable

    # 2) drive the New Bank column to assemble a real preset selection,
    #    then drag its output onto the Image column via the same
    #    _DragHandle.provider() the real mouse gesture would call.
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
    print("dragging preset:", model.data(preset_idx, Qt.ItemDataRole.DisplayRole))

    mime = model.mimeData([preset_idx])
    win._bank_pane.dropEvent(FakeDropEvent(mime))
    qwait(1500)   # let the size-meter worker finish so _last_bytes is set
    grab(win, "img_02_bankpane_ready.png")
    print("bank pane meter:", win._bank_pane._meter_label.text())

    # "Send to Image Column" now queues into Pending for Image rather than
    # writing immediately -- MainWindow wires BankPane.sendToPendingRequested
    # -> PendingBanksPane.add_pending -> (Build Image) -> ImagePane, so
    # calling each handler in turn exercises that whole real chain.
    win._bank_pane._send_to_pending()
    qwait(200)
    print("pending items:", len(win._pending_pane._pending))
    assert len(win._pending_pane._pending) == 1

    win._pending_pane._build_image()
    while win._pending_pane._live_workers:
        qwait(50)
    qwait(150)
    wait_idle(win._image_pane)
    grab(win, "img_03_after_dragdrop_append.png")
    print("entries after build:", [e.name for e in win._image_pane._entries])
    assert len(win._image_pane._entries) == 2, win._image_pane._entries

    # 3) rename + export + delete through the same code paths the buttons use
    target = win._image_pane._entries[-1]
    win._image_pane._list.setCurrentRow(len(win._image_pane._entries) - 1)
    win._image_pane._run_confirmed_op(
        "renaming (test)",
        __import__("vinsamlib.ui.workers", fromlist=["Worker"]).Worker(
            images.rename_entry, win._image_pane._path, target, "RENAMEDUI"),
        on_done=lambda _r: win._image_pane._open_image(win._image_pane._path),
    )
    wait_idle(win._image_pane)
    grab(win, "img_04_after_rename.png")
    names = [e.name for e in win._image_pane._entries]
    print("entries after rename:", names)
    assert any("RENAMEDUI" in n.upper() for n in names), names

    export_target = next(e for e in win._image_pane._entries if "RENAMEDUI" in e.name.upper())
    export_path = IMG_DIR / "exported_from_ui.e4b"
    export_path.unlink(missing_ok=True)
    from vinsamlib.ui import workers as workers_mod
    w = workers_mod.Worker(images.export_entry, win._image_pane._path, export_target, str(export_path))
    win._image_pane._run_confirmed_op("exporting (test)", w, on_done=lambda _r: None)
    wait_idle(win._image_pane)
    print("export exists:", export_path.exists(), export_path.stat().st_size if export_path.exists() else 0)
    assert export_path.exists()

    to_delete = next(e for e in win._image_pane._entries if "RENAMEDUI" in e.name.upper())
    w2 = workers_mod.Worker(images.delete_entry, win._image_pane._path, to_delete)
    win._image_pane._run_confirmed_op(
        "deleting (test)", w2,
        on_done=lambda _r: win._image_pane._open_image(win._image_pane._path))
    wait_idle(win._image_pane)
    grab(win, "img_05_after_delete.png")
    names = [e.name for e in win._image_pane._entries]
    print("entries after delete:", names)
    assert not any("RENAMEDUI" in n.upper() for n in names), names
    assert len(win._image_pane._entries) == 1, names

    # 4) format-lock rejection: try to drop a KRZ file onto this E4B image
    krz_path = str(Path.home() / "Dokumente/SYNTHS/K2000R/Soundsets/K2KFARM/VOX.KRZ")
    reject_mime = QMimeData()
    reject_mime.setUrls([QUrl.fromLocalFile(krz_path)])
    ev = FakeDropEvent(reject_mime)
    win._image_pane.dropEvent(ev)
    qwait(300)
    print("KRZ-onto-E4B-image drop accepted (should be False):", ev.accepted)
    assert ev.accepted is False
    print("entries unchanged after rejected drop:", len(win._image_pane._entries) == 1)

    print("\nALL M6 UI SMOKE CHECKS PASSED")


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
