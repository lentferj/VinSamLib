"""Regenerate the README screenshots from a synthetic demo library.

Run:  .venv/bin/python tools/make_screenshots.py [name ...]

    .venv/bin/python tools/make_screenshots.py            # all of them
    .venv/bin/python tools/make_screenshots.py 02_new_bank

WHY THIS EXISTS. `docs/screenshots/02_new_bank.png` went stale twice without
anyone noticing -- it was captured before **Rename Samples…** shipped and
again before **Adjust Placement…** did, so the README showed a four-button
pane while the program had six. The shots had been taken by hand and the
recipe thrown away, which is the whole reason a picture can drift from the
program it documents. Committing the generator is the fix.

NO COMMERCIAL NAMES. Screenshots are tracked files, so nothing from the real
library may appear in one. Everything here is synthesised: the samples are
generated sine tones, the bank is built from them through the program's own
sample-folder import, and every visible name starts with "Demo".

REAL STATE IS NOT TOUCHED. A MainWindow opens the REAL index database, and
this needs a library that actually scans -- the one case the usual
"library_roots = []" rule cannot cover. So `user_data_dir` is redirected into
a scratch directory before MainWindow is constructed and `Config.save` is
replaced with a refusal; the demo library is scanned into a throwaway
index.db and the real one is never opened. Both are asserted at startup
rather than assumed, because the failure is silent and permanent.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SHOTS = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
# A FIXED path, not mkdtemp: this directory's name is visible in the Explorer
# pane of every shot, so a random one would change the picture on every run
# and make the diff meaningless. Removed and rebuilt each time.
SCRATCH = Path(tempfile.gettempdir()) / "vinsamlib_screenshot_work"
# The library is its OWN directory, not a subdirectory of the work area: the
# WAV folders the banks are built from would otherwise be scanned and listed
# as library content, and mpc2emu names each preset after the folder it parsed
# -- which is how the first run of this labelled three presets ".tones",
# ".one_36" and ".one_40" in a screenshot meant to document naming.
DEMO_LIB = Path(tempfile.gettempdir()) / "vinsamlib_demo_library"
RATE = 44100

#: (filename stem, MIDI note). Five zones, matching what the old shot showed,
#: and named so the importer's own key parser places them.
TONES = [("DemoSample-C2", 36), ("DemoSample-E2", 40), ("DemoSample-G2", 43),
         ("DemoSample-C3", 48), ("DemoSample-E3", 52)]


def _sine(path: Path, midi: int, ms: int = 300) -> None:
    freq = 440.0 * 2 ** ((midi - 69) / 12.0)
    n = int(RATE * ms / 1000)
    frames = bytearray()
    for i in range(n):
        # A short fade at both ends, so trim/loop heuristics see something
        # musical rather than a click.
        env = min(1.0, i / 400.0, (n - i) / 400.0)
        frames += struct.pack("<h", int(12000 * env * math.sin(
            2 * math.pi * freq * i / RATE)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))


def _build_library() -> None:
    """A demo library holding one multisampled bank and two single tones."""
    from vinsamlib.build.convert import ConversionOptions
    from vinsamlib.build.sampledir_import import import_sample_dir

    DEMO_LIB.mkdir(parents=True, exist_ok=True)
    # The folder name becomes the PRESET name, so it is the label, not scratch.
    src = SCRATCH / "Demo Multisample"
    src.mkdir(parents=True, exist_ok=True)
    for stem, midi in TONES:
        _sine(src / f"{stem}.wav", midi)

    opts = ConversionOptions(target_format="E4B")
    out = import_sample_dir(str(src), opts, octave_offset=2,
                            bank_name="Demo Multisample",
                            # with_key OFF for E4B: its writer already
                            # appends `_<note><octave>` of its own, so leaving
                            # it on produced "DemoSample-C2_C2".
                            name_base="DemoSample", name_with_key=False)
    shutil.copy(out, DEMO_LIB / "DemoBank.e4b")

    for label, (stem, midi) in zip(("Demo Tone A", "Demo Tone B"), TONES[:2]):
        one = SCRATCH / label
        one.mkdir(parents=True, exist_ok=True)
        _sine(one / f"{stem}.wav", midi)
        out = import_sample_dir(str(one), opts, octave_offset=2,
                                bank_name=label, name_base="DemoSample")
        shutil.copy(out, DEMO_LIB / f"{label.replace(' ', '')}.e4b")


def _isolate() -> None:
    """Redirect the data dir and disarm Config.save BEFORE any window exists.

    Asserted, not assumed: if either patch misses, this scans the user's whole
    library into their real index and can overwrite their config."""
    from vinsamlib import config as cfg

    real_home = cfg.user_data_dir()
    data = SCRATCH / "data"
    data.mkdir(parents=True, exist_ok=True)
    cfg.user_data_dir = lambda: data

    def _refuse(self, *a, **k):
        raise AssertionError("Config.save() during a screenshot run")
    cfg.Config.save = _refuse          # load() still works; only writes die

    from vinsamlib.ui import main_window as mw
    mw.user_data_dir = lambda: data
    assert mw.user_data_dir() != real_home, "data dir was not redirected"
    print(f"  isolated: index db -> {data}")


def _window():
    from PySide6.QtWidgets import QApplication
    from vinsamlib import mpc2emu_bridge
    from vinsamlib.config import Config
    from vinsamlib.ui.main_window import MainWindow

    config = Config.load()
    mpc2emu_bridge.install(config)
    # The tree model takes its roots at CONSTRUCTION, so the demo library has
    # to be in place before the window exists -- setting it afterwards leaves
    # an empty Explorer and a screenshot of nothing.
    config.library_roots = [str(DEMO_LIB)]

    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(config)
    win.resize(1400, 850)
    win.show()
    _settle(app)
    return app, win


def _settle(app, ms: int = 1500) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
    try:
        from _qtest_shim import qwait          # never QTest.qWait -- segfaults
        qwait(ms)
    except Exception:
        for _ in range(40):
            app.processEvents()


def _grab(widget, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{name}.png"
    widget.grab().save(str(path))
    print(f"  wrote {path.relative_to(Path.cwd()) if str(path).startswith(str(Path.cwd())) else path}")


def shot_new_bank(app, win) -> None:
    """02_new_bank -- three presets staged, showing the full button grid."""
    from PySide6.QtCore import QItemSelectionModel
    from vinsamlib.banks import e4b

    pane = win._bank_pane
    items = []
    for fname, label in (("DemoBank.e4b", "Demo Multisample"),
                         ("DemoToneA.e4b", "Demo Tone A"),
                         ("DemoToneB.e4b", "Demo Tone B")):
        path = DEMO_LIB / fname
        bank = e4b.parse_bytes(path.read_bytes(), path.name)
        items.append((bank, bank.presets[0], "E4B", label))
    # add_presets(), not `pane._items = …`: the public path is what sets the
    # "[E4B]" format lock in the header and emits the status line. Assigning
    # the list directly produced a shot of a pane in a state the program never
    # actually reaches.
    pane.add_presets(items)
    pane._name_edit.setText("MyNewBank")
    pane._list.setCurrentRow(0)

    # Expand the library and select a preset, so the Explorer and the Detail
    # pane below it show something -- an empty tree and "Nothing selected."
    # document nothing.
    tree = win._explorer._tree
    model = tree.model()
    # Expand AFTER settling and once per level: the tree populates a bank's
    # children lazily, so a single expandAll() before the scan lands opens a
    # root with nothing under it.
    for _ in range(3):
        _settle(app, 400)
        tree.expandAll()
    root = model.index(0, 0)
    bank_ix = model.index(0, 0, root)
    leaf = model.index(0, 0, bank_ix) if model.rowCount(bank_ix) else bank_ix
    # Go through the SELECTION MODEL, not setCurrentIndex alone -- the Detail
    # pane listens to selectionChanged, and a current index without a
    # selection leaves it reading "Nothing selected."
    tree.selectionModel().setCurrentIndex(
        leaf, QItemSelectionModel.SelectionFlag.ClearAndSelect)
    _settle(app)
    _grab(win, "02_new_bank")


def shot_placement(app, win) -> None:
    """12_bank_placement -- Adjust Placement… over a staged bank."""
    from vinsamlib.ui.bank_pane import _RENAME_OCTAVE
    from vinsamlib.ui.sample_placement_dialog import SamplePlacementDialog

    pane = win._bank_pane
    rows = pane._placement_rows(pane._selected_presets())
    if not rows:
        print("  SKIPPED 12_bank_placement: nothing staged")
        return
    dialog = SamplePlacementDialog(rows, octave_offset=_RENAME_OCTAVE)
    dialog.resize(760, 560)
    dialog.show()
    _settle(app)
    _grab(dialog, "12_bank_placement")
    dialog.close()


ALL = {"02_new_bank": shot_new_bank, "12_bank_placement": shot_placement}


def main(argv: list[str]) -> int:
    wanted = argv or list(ALL)
    unknown = [n for n in wanted if n not in ALL]
    if unknown:
        print(f"unknown shot(s): {unknown}\nknown: {list(ALL)}")
        return 2
    # Clean BEFORE isolating: _isolate creates the throwaway index db inside
    # the work area, and wiping it afterwards deletes that out from under it.
    shutil.rmtree(SCRATCH, ignore_errors=True)
    shutil.rmtree(DEMO_LIB, ignore_errors=True)
    _isolate()
    print("  building the demo library…")
    _build_library()
    app, win = _window()
    try:
        for name in wanted:
            # New Bank has to be staged before the placement dialog can show
            # anything, so 02 always runs first when both were asked for.
            ALL[name](app, win)
    finally:
        win.close()
        shutil.rmtree(SCRATCH, ignore_errors=True)
        shutil.rmtree(DEMO_LIB, ignore_errors=True)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
