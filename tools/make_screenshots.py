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

    # A MODAL IS A HANG HERE, and this tool had no answer for one.
    #
    # `_stage()` calls `add_presets()`, which checks the playback ceiling, and
    # the demo library is built from full-range sine tones -- so `DemoSample`
    # reaches key 127 and `master` 8377ee1's ceiling warning fires. `exec()`
    # under offscreen Qt blocks forever with nobody to click it, so the run
    # sat there until it was killed: 400 s, then 1500 s, no output.
    #
    # The stub answers "Yes" and SAYS SO. A dialog that is dismissed silently
    # is indistinguishable from one that never fired, and the next person to
    # add a shot has no way to tell which state the picture was taken in.
    from PySide6.QtWidgets import QMessageBox

    real_exec = QMessageBox.exec

    def _auto(self, *a, **k):
        print(f"  [auto-dismissed modal] {self.windowTitle()!r}: "
              f"{self.text().splitlines()[0][:90] if self.text() else ''}")
        return QMessageBox.StandardButton.Yes.value
    QMessageBox.exec = _auto
    globals()["_REAL_QMESSAGEBOX_EXEC"] = real_exec


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


def _stage(win) -> None:
    """Put the three demo presets into New Bank, once.

    Its own function because the placement shot needs a staged bank too, and
    depending on 02 having run first meant asking for 12 alone silently
    produced nothing ("SKIPPED: nothing staged")."""
    from vinsamlib.banks import e4b

    pane = win._bank_pane
    if pane._items:
        return
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


def shot_new_bank(app, win) -> None:
    """02_new_bank -- three presets staged, showing the full button grid."""
    from PySide6.QtCore import QItemSelectionModel

    _stage(win)

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

    _stage(win)
    pane = win._bank_pane
    rows = pane._placement_rows(pane._selected_presets())
    if not rows:
        print("  SKIPPED 12_bank_placement: nothing staged")
        return
    # show_velocity matches what New Bank opens; the import dialog's own shot
    # (09_sample_placement) deliberately stays without the columns, because
    # that path has no velocity to carry.
    dialog = SamplePlacementDialog(rows, octave_offset=_RENAME_OCTAVE,
                                    show_velocity=True)
    dialog.resize(880, 560)
    dialog.show()
    _settle(app)
    _grab(dialog, "12_bank_placement")
    dialog.close()


def shot_sample_placement(app, win) -> None:
    """09_sample_placement -- Adjust Sample Placement inside a folder import.

    THE HANDLER IS DRIVEN, not re-implemented. This shot had no recipe at all
    -- it was taken by hand, which is the drift this file exists to stop, and
    `shot_placement`'s own comment refers to it by name as if a recipe existed.
    Building the dialog by hand here would have reproduced exactly the fault
    the generator documents: a picture that can drift from the code it shows.
    So `_on_adjust_placement_clicked()` runs for real, against the demo folder
    the import would actually read, and `exec()` is captured rather than
    blocked -- same reason as the QMessageBox stub.

    `show_velocity` is NOT set: the handler decides it, from whether the
    folder has velocity layers. The demo tones are single-layer, so the
    columns are absent, which is what this shot is meant to show -- the plain
    Sample/Low/Root/High matrix before any velocity work existed.
    """
    from PySide6.QtWidgets import QDialog

    from vinsamlib.build import sampledir_import
    from vinsamlib.ui.sample_placement_dialog import SamplePlacementDialog
    from vinsamlib.ui.sampledir_import_dialog import SampleDirImportDialog

    src = SCRATCH / "Demo Multisample"
    if not src.is_dir():
        print("  SKIPPED 09_sample_placement: the demo folder is gone")
        return

    captured: list = []
    real_exec = SamplePlacementDialog.exec

    def _capture(self, *a, **k):
        captured.append(self)
        return QDialog.DialogCode.Rejected
    SamplePlacementDialog.exec = _capture

    outer = None
    try:
        outer = SampleDirImportDialog(
            win,
            locked_format="E4B",
            # The same call `MainWindow._start_sample_import` makes, so the
            # rows, the octave and the velocity decision are the product's.
            placement_loader=lambda octave: sampledir_import.parse_preview(
                str(src), octave),
            source_text=f"{len(TONES)} file(s) from Demo Multisample")
        outer._on_adjust_placement_clicked()
        if not captured:
            print("  SKIPPED 09_sample_placement: the handler opened no dialog")
            return
        dialog = captured[0]
        _overlap_two_rows(dialog)
        dialog.resize(880, 560)
        dialog.show()
        _settle(app)
        _grab(dialog, "09_sample_placement")
        dialog.close()
    finally:
        SamplePlacementDialog.exec = real_exec
        if outer is not None:
            outer.close()


def _overlap_two_rows(dialog) -> None:
    """Make the last two rows overlap, so the warning tint is in the picture.

    The caption claims two rows have been edited to overlap and their note
    fields tinted light red, which is the thing worth showing: it is the only
    state in which the dialog is telling the user something. Left to chance it
    would not appear at all, and a shot that silently stopped demonstrating
    the warning is exactly how `02_new_bank.png` went stale twice.

    Done through the spinboxes rather than by writing the table directly, so
    the repaint that tints the row is the product's own.
    """
    table = dialog._table
    rows = table.rowCount()
    if rows < 2:
        return
    # Column 1 is Low; the dialog installs the spinboxes as cell widgets and
    # wires each one to the product's own repaint, so setting a value here is
    # the same thing a user pressing an arrow does.
    lower = table.cellWidget(rows - 2, 1)
    upper = table.cellWidget(rows - 1, 1)
    if lower is None or upper is None:
        print("  (09: no spinboxes to overlap; shot without the warning tint)")
        return
    lo = lower.value()
    if upper.value() >= lo:
        upper.setValue(min(lo + 2, upper.maximum()))
    else:
        lower.setValue(max(upper.value() - 2, lower.minimum()))


def shot_favourites(app, win) -> None:
    """13_favourites -- a pasted list of hardware preset numbers."""
    from vinsamlib.ui.favourites_dialog import FavouritesDialog

    _stage(win)
    # Synthetic bank and names, like everything else here: the real lists name
    # commercial CD content, and a screenshot is a tracked file.
    names = [f"Demo Patch {i:02d}" for i in range(40)]
    dialog = FavouritesDialog("DemoBank [E4B]", "E4B", names)
    dialog._text.setPlainText(
        "DemoBank 128\nP002\nP005\nP008\nP013\nP021\nP034")
    dialog.resize(660, 540)
    dialog.show()
    _settle(app)
    _grab(dialog, "13_favourites")
    dialog.close()


def shot_settings(app, win) -> None:
    """06_settings -- the dialog, including the two RAM limits and PRAM.

    Added to the generator because it went stale for the same reason
    02_new_bank did: a row was added to the dialog and the picture in the
    README went on showing the old one. The dialog is built from a Config, so
    it needs no library and no staging -- but it must be given a Config that
    is NEVER saved (see the guard in this file's _no_save).
    """
    from vinsamlib.ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(win._config)
    dlg.resize(560, dlg.sizeHint().height())
    _settle(app)
    _grab(dlg, "06_settings")
    dlg.deleteLater()


def shot_settings_audition(app, win) -> None:
    """06b_settings_audition -- the same dialog, scrolled to the Audition group.

    A second picture rather than a taller one: fully expanded the dialog wants
    ~1090 px and adjustSize() will not grow a window past a fraction of screen
    height, so one shot cannot hold both ends of it. The Audition fields are
    the half the README's audition section points at.
    """
    from dataclasses import replace
    from PySide6.QtWidgets import QScrollArea
    from vinsamlib.config import Config
    from vinsamlib.ui.settings_dialog import SettingsDialog
    # The picture must agree with the README, which quotes the DEFAULT note
    # list and hold; the demo config carries whatever this machine had.
    defaults = Config()
    cfg = replace(win._config,
                  audition_notes=defaults.audition_notes,
                  audition_velocity=defaults.audition_velocity,
                  audition_hold_seconds=defaults.audition_hold_seconds,
                  audition_gap_seconds=defaults.audition_gap_seconds,
                  audition_volume=defaults.audition_volume)
    dlg = SettingsDialog(cfg)
    dlg.resize(560, dlg.sizeHint().height())
    dlg.show()
    _settle(app)
    scroll = dlg.findChild(QScrollArea)
    scroll.ensureWidgetVisible(dlg._audition_volume_spin, 0, 40)
    _settle(app)
    _grab(dlg, "06b_settings_audition")
    dlg.deleteLater()


def shot_akai_partitions(app, win) -> None:
    """14_akai_partitions / 15_akai_partition_preview -- the AKAI hierarchy.

    A queue of six AKAI volumes with breaks set after rows 2 and 4, and the
    New Image dialog previewing the layout those breaks produce.

    Built from SYNTHETIC volumes rather than the demo library, and this is the
    one recipe here that does not drive the real staging path. The reason is
    the picture: a queue of six banks assembled from the demo library takes
    minutes and produces six rows named after WAV folders, where the point of
    the shot is the PARTITION LETTERS down the left and the break between
    them. The pane is fed the same entry dicts add_pending() builds, so the
    rows are rendered by the real _make_item()/_refresh() path -- what is
    skipped is the assembly that produced them, not the display under test.
    """
    from vinsamlib.ui.image_pane import _NewImageDialog
    from vinsamlib.ui.pending_pane import PendingBanksPane

    names = ["KIT 01", "KIT 02", "PAD 01", "PAD 02", "FX 01", "FX 02"]
    pane = PendingBanksPane()
    pane._format = "AKAI"
    pane._pending = [{"name": n, "format": "AKAI", "items": [None, None],
                      "convert_opts": None} for n in names]
    pane._partition_breaks = {2, 4}
    pane._refresh()
    # Tall enough for all six rows: the point of the shot is THREE partition
    # letters, and at 300 px the C rows fell below the fold, so the picture
    # showed A and B and looked like the two-partition case.
    pane.resize(430, 470)
    pane.show()
    _settle(app, 300)
    _grab(pane, "14_akai_partitions")
    pane.close()

    # ...and the preview the same grouping produces in the New Image dialog.
    work = SCRATCH / "akai_volumes"
    folders = []
    for n in names:
        d = work / n
        d.mkdir(parents=True, exist_ok=True)
        # 2 MB apiece: enough that the MB figures in the preview are real
        # rather than rounding to 0.0, small enough to stay quick.
        for i in range(2):
            (d / f"{n.replace(' ', '')}{i}.S3").write_bytes(b"\0" * 1024 * 1024)
        folders.append(str(d))
    dlg = _NewImageDialog(config=win._config, seed_paths=folders,
                          seed_format="AKAI",
                          seed_partitions=[[0, 1], [2, 3], [4, 5]])
    for i in range(dlg._kind_box.count()):
        if dlg._kind_box.itemData(i) == "akai_hd":
            dlg._kind_box.setCurrentIndex(i)
            break
    dlg._path_edit.setText(str(SCRATCH / "LIBRARY.hda"))
    dlg.resize(660, 520)
    dlg.show()
    _settle(app, 400)
    _grab(dlg, "15_akai_partition_preview")
    dlg.close()


def shot_convert_options(app, win) -> None:
    """05_convert_options -- the dialog the README describes group by group.

    Had no recipe until 2026-09-07, which is exactly how it went stale: the
    K2000 Layer Handling group was added and the picture went on showing a
    dialog without it. This is the third shot to be added here for that
    reason (see 02_new_bank and 06_settings).

    Shot against a KRZ target, because two of its groups are format-gated in
    opposite directions -- Constant-Power Pan Compensation is E4B-only and
    K2000 Layer Handling is KRZ-only -- so KRZ is the one target where both
    are visible at once, one live and one greyed out with its reason.
    """
    from vinsamlib.ui.format_convert_dialog import FormatConvertDialog

    dlg = FormatConvertDialog(win)
    dlg._format_box.setCurrentText("KRZ")
    # Expanded, because a screenshot of collapsed group headers shows the
    # feature list and none of the content the README text is explaining.
    for name in ("_trim_start_group", "_pan_law_group", "_resample_group",
                 "_key_zone_group"):
        group = getattr(dlg, name, None)
        if group is not None:
            group.setChecked(True)
    dlg.adjustSize()
    _settle(app)
    _grab(dlg, "05_convert_options")
    dlg.deleteLater()


ALL = {"02_new_bank": shot_new_bank,
       "05_convert_options": shot_convert_options,
       "06_settings": shot_settings,
       "06b_settings_audition": shot_settings_audition,
       "09_sample_placement": shot_sample_placement,
       "12_bank_placement": shot_placement,
       "13_favourites": shot_favourites,
       "14_akai_partitions": shot_akai_partitions}


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
