"""Manual smoke test for the mpc2emu vintage resample/reduce conversion
feature (Settings hardening + build/convert.py + Convert Options dialog +
Pending pane wiring): drag a real E4B preset into New Bank, send it to
Pending, set conversion options via "Process before building...", Build
Image onto a real EMU3 HD image, and confirm the bank that actually landed
on the image is smaller than what an unconverted assemble() of the same
selection would have produced -- proving the conversion step really ran
through the full GUI-triggered pipeline, not just build/convert.py in
isolation. Same in-process-call approach as every other smoke test here
(no X11 input automation available); the modal ConvertOptionsDialog is
stubbed the same way manual_ui_smoke_pending.py stubs QMessageBox/QInputDialog.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QModelIndex, QThreadPool, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from vinsamlib import mpc2emu_bridge
from vinsamlib.banks import e4b as vs_e4b
from vinsamlib.build import images
from vinsamlib.build.convert import ConversionOptions
from vinsamlib.config import Config
from vinsamlib.ui.convert_options_dialog import ConvertOptionsDialog
from vinsamlib.ui.main_window import MainWindow

from _qtest_shim import qwait

# ImagePane._append_paths() confirms via a real modal QMessageBox before
# writing to the image -- fine for a human, but blocks forever with no
# one to click it in this offscreen harness (same stub manual_ui_smoke_
# pending.py uses for the same reason).
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

OUT = Path(tempfile.gettempdir()) / "vinsamlib_manual_smoke"
OUT.mkdir(parents=True, exist_ok=True)
IMG_DIR = Path.home() / "temp" / "vinsamlib_convert_ui"
IMG_DIR.mkdir(parents=True, exist_ok=True)

E4B_DIR = Path.home() / "Dokumente/SYNTHS/E4XT/E4Bs/Rob.Papen-Techno.Synth.Construction.Yard.E4/Techno Synths RP"

# Fixed choice the dialog would otherwise ask a human for -- mirrors the
# plan's own example (Emax I + 30% key-zone reduce).
_TEST_OPTIONS = ConversionOptions(resample_profile="emax1", reduce_key_zones_pct=30.0)
ConvertOptionsDialog.get_options = staticmethod(lambda parent=None, initial=None: _TEST_OPTIONS)


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
    proxy = tree.model()
    if hasattr(proxy, "mapFromSource"):
        index = proxy.mapFromSource(index)
    tree.expand(index)
    qwait(ms)


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
    bank_idx = next(model.index(r, 0, folder) for r in range(model.rowCount(folder))
                     if "[E4B]" in model.data(model.index(r, 0, folder), Qt.ItemDataRole.DisplayRole))
    expand_and_wait(tree, bank_idx, ms=3000)
    n_presets = min(3, model.rowCount(bank_idx))
    assert n_presets > 0, "no presets found under the test bank -- can't run this smoke test"

    # Drag a few presets into New Bank (same FakeDropEvent trick as the
    # other pane smoke tests -- no real X11 drag available).
    preset_indexes = [model.index(r, 0, bank_idx) for r in range(n_presets)]
    mime = model.mimeData(preset_indexes)
    win._bank_pane.dropEvent(FakeDropEvent(mime))
    win._bank_pane._name_edit.setText("ConvertTestBank")
    qwait(1200)   # let the size-meter worker finish so _last_bytes is set

    # Baseline: what assemble() alone (no conversion) would have produced
    # for this exact selection -- the point of comparison for "did the
    # conversion step actually shrink it".
    from vinsamlib.banks import e4b as banks_e4b
    baseline_selection = [(bank, preset) for bank, preset, _name in win._bank_pane._items]
    baseline_bytes = banks_e4b.assemble(baseline_selection)
    print("baseline (unconverted) assembled size:", len(baseline_bytes))

    win._bank_pane._send_to_pending()
    qwait(200)
    assert [e["name"] for e in win._pending_pane._pending] == ["ConvertTestBank"]

    # "Process before building..." -- ConvertOptionsDialog.get_options is
    # stubbed above to return _TEST_OPTIONS without a real modal dialog.
    assert win._pending_pane._convert_btn.isEnabled(), "convert button should be enabled for an E4B queue"
    win._pending_pane._list.setCurrentRow(0)
    win._pending_pane._show_convert_options()
    qwait(50)
    stored = win._pending_pane._pending[0]["convert_opts"]
    print("convert opts stored on entry:", stored)
    assert stored == _TEST_OPTIONS
    grab(win, "convert_01_options_set.png")

    starter = IMG_DIR / "convert_test.hda"
    starter.unlink(missing_ok=True)
    seed_bank = str(E4B_DIR / "B.007-Dance Organ   RP.e4b")
    images.create_image("emu3_hd_emu", str(starter), [seed_bank], volume_label="CONVTEST", size_mb=32)
    win._image_pane._open_image(str(starter))
    qwait(200)

    win._pending_pane._build_image()
    wait_workers(win._pending_pane)
    while win._image_pane._busy:
        qwait(50)
    qwait(150)
    grab(win, "convert_02_built.png")

    entries = [e for e in win._image_pane._entries if e.name.upper().startswith("CONVERTTEST")]
    print("matching image entries:", [e.name for e in entries])
    assert len(entries) == 1, f"expected exactly one CONVERTTEST bank on the image, found {entries}"

    from vinsamlib.vfs.detect import open_volume
    vol = open_volume(str(starter))
    try:
        built_bytes = vol.read(entries[0])
    finally:
        vol.close()
    print("built (converted) bank size on image:", len(built_bytes), "vs baseline:", len(baseline_bytes))
    assert len(built_bytes) < len(baseline_bytes), (
        "converted bank should be smaller than the unconverted baseline "
        f"(built={len(built_bytes)}, baseline={len(baseline_bytes)})")

    # VinSamLib's own reader must still open what actually landed on the image.
    reopened = vs_e4b.parse_bytes(built_bytes, "ConvertTestBank.e4b")
    print("reopened converted bank -- presets:", len(reopened.presets), "samples:", len(reopened.samples))
    assert len(reopened.presets) == n_presets

    print("\nALL CONVERSION SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
