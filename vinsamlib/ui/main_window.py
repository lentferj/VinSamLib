"""
MainWindow: menu bar, status bar, and the five-section splitter (Explorer+
Detail, Samples, New Bank, Pending for Image, Image) — see the M3 plan for
why the window's shape was fixed from day one, with New Bank (M5) and Image
(M6) filled in as their milestones landed, and Pending for Image added
later as a staging queue between the two: New Bank hands over named recipes
rather than writing to a real image immediately, Pending holds/reorders
them, and only its own "Build Image →" ever touches the real file.

Also owns the library index (M4): a background scan populates it on
startup and after "Add Library Folder…", with progress in the status bar;
the Explorer's search box queries it directly whenever the user types.
"""

from __future__ import annotations  # noqa: I001

from pathlib import Path
import os
import time
from typing import Optional

from PySide6.QtCore import QObject, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QInputDialog,
    QMainWindow,  # noqa: F401, RUF100
    QMessageBox,
    QSplitter,
)

from . import workers
from .bank_pane import BankPane
from .explorer_pane import ExplorerPane
from .format_convert_dialog import FormatConvertDialog
from .image_pane import ImagePane
from .models import LibraryTreeModel, human_size
from .pending_pane import PendingBanksPane
from .samples_pane import SamplesPane
from .sampledir_import_dialog import SampleDirImportDialog
from . import models
from .favourites_dialog import FavouritesDialog
from .settings_dialog import SettingsDialog
from ..banks import e4b, eiii, krz
from ..build import calllog
from ..build import convert, foreign_import, sampledir_import, xpm_import
from ..build import project
from ..config import Config, user_data_dir
from ..index.db import IndexDB
from ..index.scanner import scan


#: Appended to a New Bank row that mpc2emu produced, as opposed to one taken
#: verbatim off a disc. It marks PROVENANCE, and it is display only -- the
#: written program name comes from the preset object itself (bank_pane's
#: assemble drops the label), so this cannot reach the media or be truncated
#: into it.
#:
#: It used to be added by ONE of the four import paths. A New Bank holding a
#: converted E4B preset next to an imported .gig showed the first marked and
#: the second not, which reads as a difference in what they are rather than
#: an oversight -- both came through the same converter.
_VIA_MPC2EMU = " (mpc2emu)"


def _via_mpc2emu(name: str) -> str:
    """`name` marked as mpc2emu's output, without doubling the mark."""
    name = (name or "").strip()
    if not name:
        return _VIA_MPC2EMU.strip()  # not " (mpc2emu)" with a leading gap
    return name if name.endswith(_VIA_MPC2EMU.strip()) else f"{name}{_VIA_MPC2EMU}"


class _ProgressRelay(QObject):
    """Carries a worker thread's progress across to the GUI thread.

    `render()` calls its hook on whatever thread it is on. Touching a widget
    from there is the kind of thing that works for weeks and then crashes, so
    the hook emits this signal instead: the relay lives in the GUI thread, Qt
    queues the emission, and the slot runs where the widget is.
    """

    tick = Signal(int, str)


class _NamedNode:
    """The one thing the audition dialog needs from a selection: a label.

    New Bank rows are not TreeNodes, so this lets both routes land in the same
    ``_on_audition_ready`` without the dialog knowing where the row came from.
    """

    __slots__ = ("label",)

    def __init__(self, label: str):
        self.label = label


class MainWindow(QMainWindow):
    def __init__(self, config: Config):
        super().__init__()
        self._config = config
        self.setWindowTitle("VinSamLib")
        # A folder dragged from the file manager onto ANY part of this window
        # is added to the library. On the window rather than on the tree: the
        # tree is DragOnly and its viewport would swallow nothing, but the
        # panes beside it are where a drop often lands, and "the browser
        # window" is what a person aims at.
        self.setAcceptDrops(True)
        # 1500x940 rather than the old 1280x800 (+17%): five columns, and the
        # placement editor's piano, were arriving cramped on a first run.
        # CLAMPED to the screen, because a fixed size larger than the display
        # gives a window whose buttons sit off the bottom edge and cannot be
        # reached -- availableGeometry() already excludes panels and docks.
        # The size this window was last closed at, or the default on a fresh
        # install. Size only, not position -- a window restored onto a monitor
        # that is no longer attached cannot be reached.
        want_w = config.window_width or 1500
        want_h = config.window_height or 940
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            want_w = min(want_w, max(900, avail.width() - 40))
            want_h = min(want_h, max(600, avail.height() - 40))
        self.resize(want_w, want_h)

        self._index_db = IndexDB(user_data_dir() / "index.db")
        if self._index_db.migrated:
            self.statusBar().showMessage(
                "Library index rebuilt — sizes and search results will "
                "appear after a rescan",
                8000,
            )
        #: Whether this batch's ceiling findings were attributable to a
        #: preset. False means the Narrow button is not offered, rather
        #: than offered and silently doing nothing.
        self._ceiling_narrowable = False
        self._scan_worker: workers.Worker | None = None
        self._xpm_import_worker: workers.Worker | None = None
        self._sample_dir_import_worker: workers.Worker | None = None
        self._preset_convert_worker: workers.Worker | None = None
        self._preset_convert_queue: list = []
        self._preset_convert_opts: Optional[convert.ConversionOptions] = None  # noqa: UP045
        # Accumulated across a whole queue, reported once when it drains --
        # converting 20 presets at once must not mean 20 modal warnings.
        self._preset_convert_risks: list = []
        self._favourites_worker: workers.Worker | None = None
        # Audition: one worker at a time, and a generation counter so a second
        # right-click during a render cannot deliver the first one's audio.
        self._audition_worker: workers.Worker | None = None
        self._audition_gen: int = 0
        #: The most recent dialog (kept for tests and for the common case) and
        #: every live one. A non-modal dialog is parented to this window, but
        #: its Python wrapper still has to stay alive or its QAudioSink's
        #: finished handler can fire into a collected object.
        self._audition_dialog = None
        self._audition_dialogs: list = []
        # The convert-first import queue: soundfont-style sources and MPC
        # containers, whichever route they arrived by.
        self._import_worker: workers.Worker | None = None
        self._import_queue: list = []
        self._import_opts: Optional[convert.ConversionOptions] = None  # noqa: UP045
        self._import_risks: list = []

        # Before the tree model and the Explorer exist, and before any scan:
        # both decide whether the soundfont-style formats are rows at all,
        # from worker threads that have no Config of their own.
        foreign_import.set_available(config.check_foreign_import_support()[0])
        foreign_import.set_firmware_available(config.check_firmware_import_support()[0])

        self._model = LibraryTreeModel(
            list(config.library_roots), index_db=self._index_db
        )
        self._model.statusMessage.connect(
            lambda msg: self.statusBar().showMessage(msg, 6000)
        )

        self._explorer = ExplorerPane(self._model, self._index_db)
        self._samples = SamplesPane()
        self._samples.setVisible(False)
        self._explorer.selectionChanged.connect(self._samples.show_node)

        self._bank_pane = BankPane(self._config)
        self._bank_pane.statusMessage.connect(
            lambda msg: self.statusBar().showMessage(msg, 6000)
        )
        self._explorer.addToBankRequested.connect(self._add_node_to_bank)
        self._bank_pane.redoLastImportRequested.connect(self._redo_last_import)
        # Asked live, not stored: New Bank's format lock changes as it is
        # filled and cleared, and the Explorer's menu has to reflect the
        # lock at the moment of the right-click.
        self._explorer.locked_format = lambda: self._bank_pane.format
        self._explorer.addFavouritesRequested.connect(self._add_favourites)
        self._explorer.importXpmRequested.connect(self._import_xpm)
        self._explorer.convertPresetRequested.connect(self._convert_preset_via_mpc2emu)
        self._explorer.importForeignRequested.connect(self._import_requests)
        self._bank_pane.importRequested.connect(self._import_requests)
        self._bank_pane.auditionStagedRequested.connect(self._audition_staged)
        self._bank_pane.ceilingZonesAdded.connect(self._on_ceiling_zones_added)
        self._explorer.removeLibraryRootRequested.connect(self._remove_library_root)
        self._explorer.auditionRequested.connect(self._audition_node)

        self._pending_pane = PendingBanksPane()
        self._pending_pane.statusMessage.connect(
            lambda msg: self.statusBar().showMessage(msg, 6000)
        )
        self._bank_pane.sendToPendingRequested.connect(self._pending_pane.add_pending)
        self._pending_pane.moveToNewBankRequested.connect(self._bank_pane.load_pending)

        self._image_pane = ImagePane(config)
        self._image_pane.statusMessage.connect(
            lambda msg: self.statusBar().showMessage(msg, 6000)
        )
        self._pending_pane.buildRequested.connect(self._image_pane.receive_bank_files)

        # Minimum widths so dragging one handle can't crush a neighboring
        # column all the way to zero -- QSplitter's default
        # childrenCollapsible=True lets any pane vanish once its neighbor's
        # growth passes it, which is also what made dragging feel "steppy"
        # (the collapse threshold, not a smooth width all the way down).
        self._explorer.setMinimumWidth(200)
        self._samples.setMinimumWidth(150)
        self._bank_pane.setMinimumWidth(180)
        self._pending_pane.setMinimumWidth(180)
        self._image_pane.setMinimumWidth(180)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._explorer)
        splitter.addWidget(self._samples)
        splitter.addWidget(self._bank_pane)
        splitter.addWidget(self._pending_pane)
        splitter.addWidget(self._image_pane)
        splitter.setSizes([300, 280, 260, 260, 260])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 1)
        splitter.setStretchFactor(3, 1)
        splitter.setStretchFactor(4, 1)
        self._splitter = splitter
        self.setCentralWidget(splitter)

        self._build_menu()

        if self._model.is_empty():
            self.statusBar().showMessage(
                "Add a library folder to get started — File ▸ Add Library Folder…"
            )
        else:
            self.statusBar().showMessage("Ready")
            self._start_scan(list(config.library_roots))

    def start_background_work(self) -> None:
        """Called by app.py once the window is up.

        Recovery is OFFERED before the timer is armed, so a user who says yes
        does not race the first autosave overwriting what they are being
        offered.
        """
        self._offer_recovery()
        self._start_autosave()
        # Armed from the stored setting rather than at import time, so the
        # switch survives a restart -- which is the whole point of it being
        # in config.toml: a problem worth recording is rarely reproduced in
        # the same session it was noticed in.
        calllog.set_enabled(bool(getattr(self._config, "debug_mpc2emu_log", False)))
        if calllog.is_enabled():
            self.statusBar().showMessage(
                "Debug: recording mpc2emu calls into saved projects", 8000
            )

    def closeEvent(self, event) -> None:
        # Give in-flight background workers (tree fetches, a scan) a bounded
        # window to finish before the widget tree they'd signal back into
        # starts getting torn down — under PySide6/Shiboken, a worker still
        # mid-flight at that point raises a hard "Signal source has been
        # deleted" RuntimeError from its own background thread (PyQt5
        # tolerated the same race silently). Bounded rather than unbounded
        # so quitting never hangs on a slow scan.
        QThreadPool.globalInstance().waitForDone(3000)
        self._remember_window_size()
        self._index_db.close()
        # A CLEAN EXIT REMOVES THE RECOVERY FILE, and that is the whole
        # mechanism: nothing records a crash, because a crash is precisely
        # the case where nothing gets the chance to record anything. The
        # file still being there at startup is the signal.
        project.clear_autosave()
        super().closeEvent(event)

    def _remember_window_size(self) -> None:
        """Persist the size so the next start matches this one.

        WRAPPED, and that is not defensive habit. Config.save() writes the
        file that also holds `library_roots`, which this project has lost
        twice to an incidental save -- so save() refuses to write an empty
        library unless told to, and a refusal here must not stop the window
        closing. The screenshot tool replaces save() with something that
        RAISES, precisely to prove it is never called; that tool also closes
        the window, so an unguarded call would break it.

        Not saved while maximised or full-screen: storing 3440x1414 would
        make the next ordinary start fill the screen, which is not what the
        user chose -- normalGeometry() keeps the restored size instead.
        """
        try:
            size = self.normalGeometry().size()
            if size.width() < 400 or size.height() < 300:
                return  # a size nobody could have meant
            self._config.window_width = size.width()
            self._config.window_height = size.height()
            self._config.save()
        except Exception:  # noqa: BLE001, S110
            pass  # never let remembering a size block a quit

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        add_action = QAction("Add Library Folder…", self)
        add_action.triggered.connect(self._add_library_folder)
        file_menu.addAction(add_action)

        remove_action = QAction("Remove Library Folder…", self)
        remove_action.triggered.connect(lambda: self._remove_library_folder())  # noqa: PLW0108, RUF100
        file_menu.addAction(remove_action)

        rescan_action = QAction("Rescan Library", self)
        rescan_action.triggered.connect(
            lambda: self._start_scan(list(self._config.library_roots))
        )
        file_menu.addAction(rescan_action)

        file_menu.addSeparator()

        save_project_action = QAction("Save Project…", self)
        save_project_action.triggered.connect(self._save_project)
        file_menu.addAction(save_project_action)

        load_project_action = QAction("Load Project…", self)
        load_project_action.triggered.connect(self._load_project)
        file_menu.addAction(load_project_action)

        file_menu.addSeparator()

        import_xpm_action = QAction("Import MPC Program…", self)
        import_xpm_action.triggered.connect(lambda: self._import_xpm())  # noqa: PLW0108, RUF100
        xpm_ok, xpm_reason = self._config.check_xpm_import_support()
        import_xpm_action.setEnabled(xpm_ok)
        import_xpm_action.setToolTip(
            xpm_reason if xpm_ok else f"Unavailable: {xpm_reason}"
        )
        file_menu.addAction(import_xpm_action)

        import_sample_dir_action = QAction("Import Sample Folder…", self)
        import_sample_dir_action.triggered.connect(self._import_sample_dir)
        sd_ok, sd_reason = self._config.check_sample_dir_import_support()
        import_sample_dir_action.setEnabled(sd_ok)
        import_sample_dir_action.setToolTip(
            sd_reason if sd_ok else f"Unavailable: {sd_reason}"
        )
        file_menu.addAction(import_sample_dir_action)

        # Its own action rather than one dialog doing both: a file dialog can
        # select directories or files, never both, and one folder holding
        # several instruments is exactly when the folder action is no use.
        import_samples_action = QAction("Import Samples…", self)
        import_samples_action.triggered.connect(self._import_sample_files)
        import_samples_action.setEnabled(sd_ok)
        import_samples_action.setToolTip(
            "Pick individual sample files — for a folder that holds more than "
            "one instrument"
            if sd_ok
            else f"Unavailable: {sd_reason}"
        )
        file_menu.addAction(import_samples_action)

        import_foreign_action = QAction("Import Instrument…", self)
        import_foreign_action.triggered.connect(self._import_foreign_file)
        fi_ok, fi_reason = self._config.check_foreign_import_support()
        import_foreign_action.setEnabled(fi_ok)
        import_foreign_action.setToolTip(
            "SoundFont, SFZ, EXS24, TAL-Sampler or GigaSampler — read and "
            "converted to a hardware bank, never written back"
            if fi_ok
            else f"Unavailable: {fi_reason}"
        )
        file_menu.addAction(import_foreign_action)

        file_menu.addSeparator()

        settings_action = QAction("Settings…", self)
        settings_action.triggered.connect(self._show_settings)
        file_menu.addAction(settings_action)

        file_menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("&View")
        samples_action = QAction("Show Samples Column", self, checkable=True)
        samples_action.toggled.connect(self._toggle_samples_column)
        view_menu.addAction(samples_action)

        view_menu.addSeparator()

        dupe_check_action = QAction("Check for Duplicate Presets", self, checkable=True)
        dupe_check_action.setChecked(True)
        dupe_check_action.toggled.connect(self._toggle_dupe_check)
        view_menu.addAction(dupe_check_action)

        self._ceiling_warn_action = QAction(
            "Warn About Zones Above the E4XT's Rate Ceiling", self, checkable=True
        )
        self._ceiling_warn_action.setChecked(
            bool(getattr(self._config, "warn_playback_ceiling", True))
        )
        self._ceiling_warn_action.setStatusTip(
            "An E4XT runs off the end of a sample above an absolute playback "
            "rate. Commercial banks are often authored past it on keys nobody "
            "plays, so this can be turned off."
        )
        self._ceiling_warn_action.toggled.connect(self._set_ceiling_warning)
        view_menu.addAction(self._ceiling_warn_action)

        view_menu.addSeparator()
        # Auto-play comes BEFORE the report toggle, and the order is the
        # argument: what you hear, then whether you are shown the paperwork
        # about it. Both are audition behaviour and nothing else lives in this
        # group, so neither is buried among the view options above.
        self._audition_autoplay_action = QAction(
            "Play Auditions Automatically", self, checkable=True
        )
        self._audition_autoplay_action.setChecked(
            bool(getattr(self._config, "audition_auto_play", True))
        )
        self._audition_autoplay_action.setStatusTip(
            "On: an audition starts playing as soon as it is ready. Off: it "
            "waits for you to press Play, which is worth it when auditioning a "
            "list of presets and only wanting to hear one."
        )
        self._audition_autoplay_action.toggled.connect(self._set_audition_auto_play)
        view_menu.addAction(self._audition_autoplay_action)

        self._audition_report_action = QAction(
            "Show Audition Report", self, checkable=True
        )
        self._audition_report_action.setChecked(bool(self._config.audition_show_report))
        self._audition_report_action.setStatusTip(
            "Off: an audition plays straight away behind a small notice that "
            "closes itself. The report still opens when nothing can play it."
        )
        self._audition_report_action.toggled.connect(self._set_audition_show_report)
        view_menu.addAction(self._audition_report_action)

        # A NAMED, DISABLED ACTION -- the idiom this project already uses for
        # a fact the user cannot act on from here (see explorer_pane's
        # refusals). It sits under the audition entry because that is the
        # feature it changes, and it is a statement rather than a control:
        # the engine is chosen at import and cannot be switched from a menu.
        from ..audition import render as _render_mod  # noqa: PLC0415, RUF100

        renderer_action = QAction(
            f"Audition renderer: {_render_mod.renderer_description()}", self
        )
        renderer_action.setEnabled(False)
        renderer_action.setStatusTip(
            "numpy is an accelerator, never a requirement. Set "
            "VINSAMLIB_NO_NUMPY=1 to force the pure-Python path."
        )
        view_menu.addAction(renderer_action)

        view_menu.addSeparator()

        dupe_prompt_action = QAction(
            "Prompt Before Skipping Duplicates", self, checkable=True
        )
        dupe_prompt_action.setChecked(True)
        dupe_prompt_action.toggled.connect(self._toggle_dupe_prompt)
        view_menu.addAction(dupe_prompt_action)

        # "Prompt before skipping" only means anything while the duplicate
        # check itself is on.
        dupe_check_action.toggled.connect(dupe_prompt_action.setEnabled)

        help_menu = self.menuBar().addMenu("&Help")
        about_action = QAction("About VinSamLib", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _start_dir(self, setting: str) -> str:
        """Where a file dialog should open. Each kind of import remembers its
        own directory: samples live with your samples, MPC programs with your
        MPC backup, and neither is where you last added a library folder --
        which is what all of them used to fall back to."""
        remembered = getattr(self._config, setting, None)
        if remembered is not None and Path(remembered).is_dir():
            return str(remembered)
        if self._config.last_library_dir is not None:
            return str(self._config.last_library_dir)
        return ""

    def _remember_dir(self, setting: str, chosen: str) -> None:
        """Remember what the choice sat *in*, so the next dialog opens beside
        it: the sibling instrument folder, the next program of the same
        backup. The parent either way -- for a picked file that is the folder
        holding it, for a picked folder it is where its siblings are, which is
        what File > Add Library Folder… has always stored. Same shape as
        ImagePane._remember_dir."""
        directory = Path(chosen).parent
        if getattr(self._config, setting, None) != directory:
            setattr(self._config, setting, directory)
            self._config.save()

    def _add_library_folder(self) -> None:
        start_dir = (
            str(self._config.last_library_dir) if self._config.last_library_dir else ""
        )
        path = QFileDialog.getExistingDirectory(
            self,
            "Add Library Folder",
            start_dir,
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not path:
            return
        self._add_library_roots([Path(path)])

    def _add_library_roots(self, paths) -> int:
        """Add folders as library roots, by their full path. Returns how many.

        The one place a root is added, so the menu and a drop from the file
        manager cannot come to disagree about what counts as addable.

        EVERY REFUSAL IS NAMED. Dropping four folders and watching three
        appear says nothing about the fourth, and "nothing happened" is the
        answer this window is worst at giving.

        Overlap is refused rather than silently allowed. A folder inside an
        existing root, or one that swallows existing roots, would be walked
        twice: the same bank appears under two rows, the index holds it
        twice, and a scan pays for it twice -- which reads as a duplicate
        bug rather than as a thing the user asked for. Compared through
        `os.path.realpath`, so a symlink to a root is recognised as the root.
        """
        roots = list(self._config.library_roots)
        real = {os.path.realpath(r): r for r in roots}
        added, notes = [], []
        for raw in paths:
            p = Path(raw)
            if not p.is_dir():
                notes.append(f"{p.name}: not a folder")
                continue
            rp = os.path.realpath(p)
            if rp in real:
                notes.append(f"{p.name}: already in the library")
                continue
            inside = next(
                (
                    r
                    for k, r in real.items()
                    if rp.startswith(k.rstrip(os.sep) + os.sep)
                ),
                None,
            )
            if inside is not None:
                notes.append(f"{p.name}: already inside {Path(inside).name}")
                continue
            swallows = [
                r for k, r in real.items() if k.startswith(rp.rstrip(os.sep) + os.sep)
            ]
            if swallows:
                notes.append(
                    f"{p.name}: would contain "
                    + ", ".join(Path(s).name for s in swallows)
                )
                continue
            # Absolute, as the user asked -- but NOT resolved: a deliberate
            # symlink into a library is a path they chose, and rewriting it
            # to its target would quietly change what their config says.
            p = p.absolute()
            self._config.library_roots.append(p)
            real[rp] = p
            added.append(p)
        if added:
            self._config.last_library_dir = added[-1].parent
            self._config.save()
            for p in added:
                self._model.add_root(p)
            self._start_scan(added)
        what = (
            f"Added {len(added)} folder(s) to the library" if added else "Nothing added"
        )
        if notes:
            what += " — " + "; ".join(notes)
        self.statusBar().showMessage(what, 12000)
        return len(added)

    # -- folders dropped from a file manager ------------------------------------

    @staticmethod
    def _dropped_dirs(mime) -> list:
        """Local directories in a drag's payload, in order, deduplicated.

        Static and mime-only so the decision can be checked without a real
        drag, which cannot be synthesised reliably offscreen."""
        out, seen = [], set()
        if mime is None or not mime.hasUrls():
            return out
        for url in mime.urls():
            local = url.toLocalFile()
            if not local:
                continue  # a non-file URL: nothing to add
            path = Path(local)
            if not path.is_dir():
                continue
            key = os.path.realpath(path)
            if key in seen:
                continue
            seen.add(key)
            out.append(path)
        return out

    def dragEnterEvent(self, event) -> None:
        # Accept ONLY when there is a folder in the payload. Accepting
        # anything with urls and then refusing at the drop gives the cursor a
        # copy sign over a window that will not take it.
        if self._dropped_dirs(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        # Fires on every mouse move; same decision, no extra work beyond the
        # is_dir() stat the enter already paid for.
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        dirs = self._dropped_dirs(event.mimeData())
        if not dirs:
            event.ignore()
            return
        event.acceptProposedAction()
        self._add_library_roots(dirs)

    def _remove_library_folder(self) -> None:
        """File > Remove Library Folder…: picks a folder from the current
        list first. Explorer's own right-click "Remove ... from Library"
        on a root row (see importXpmRequested's sibling signal,
        removeLibraryRootRequested) already knows which one and skips
        straight to _remove_library_root()."""
        if not self._config.library_roots:
            self.statusBar().showMessage("No library folders to remove")
            return
        items = [str(p) for p in self._config.library_roots]
        choice, ok = QInputDialog.getItem(
            self, "Remove Library Folder", "Folder to remove:", items, 0, False
        )
        if not ok or not choice:
            return
        self._remove_library_root(Path(choice))

    def _remove_library_root(self, path: Path) -> None:
        if (
            QMessageBox.question(
                self,
                "Remove Library Folder",
                f"Remove {path} from your library?\n\n"
                "This only stops VinSamLib from tracking it -- no files on disk "
                "are touched, and you can add it back any time.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._config.library_roots.remove(path)
        # The only place an empty library is a decision rather than an
        # accident -- the user just answered a confirmation to make it so.
        self._config.save(allow_empty_library=True)
        self._model.remove_root(path)
        self._index_db.forget_containers_under(str(path))
        self.statusBar().showMessage(f"Removed {path} from your library", 6000)

    def _show_settings(self) -> None:
        dialog = SettingsDialog(self._config, self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            self._bank_pane.refresh_size_limits()
            self._start_autosave()
        if dialog.path_changed:
            self.statusBar().showMessage(
                "mpc2emu path updated — restart VinSamLib to apply", 8000
            )

    # -- audition -----------------------------------------------------------------

    def _audition_options(self):
        """Config + the negotiated device rate -> ``AuditionOptions``.

        The render rate is decided HERE and passed into the renderer, so
        render rate and sink rate are never decided in two places.
        """
        from ..audition import AuditionOptions, parse_notes  # noqa: PLC0415, RUF100
        from .audition_player import negotiate_format  # noqa: PLC0415, RUF100

        notes = parse_notes(self._config.audition_notes)
        got = negotiate_format()
        rate, channels = got if got else (44100, 2)
        return AuditionOptions(
            notes=tuple(notes),
            velocity=max(1, min(127, int(self._config.audition_velocity))),
            hold_seconds=max(0.05, float(self._config.audition_hold_seconds)),
            gap_seconds=max(0.0, float(self._config.audition_gap_seconds)),
            render_rate=int(rate),
            channels=int(channels),
        )

    def _audition_node(self, node) -> None:
        """Explorer node -> a background render, then the dialog."""
        from ..audition import render_node  # noqa: PLC0415, RUF100

        try:
            opts = self._audition_options()
        except ValueError as ex:
            # Near-unreachable: Settings validates the note list live through
            # this same parser. A status message rather than a modal keeps a
            # new modal raise-site out of the tree for a case that is already
            # guarded at the field.
            self.statusBar().showMessage(f"Audition settings: {ex}", 10000)
            return
        self._silence_previous_auditions()
        self._audition_gen += 1
        gen = self._audition_gen
        # A repeat of the same preset with the same settings is already
        # rendered. Looked up HERE, on the GUI thread, so it costs no worker
        # and no progress window -- it just plays.
        from ..audition import cached_render, render_cache_key  # noqa: PLC0415, RUF100

        hit = cached_render(render_cache_key(node.kind, node.payload, opts))
        if hit is not None:
            self.statusBar().showMessage(
                f"Audition of {node.label} (already rendered)", 4000
            )
            self._on_audition_ready(gen, hit, node)
            return
        relay = self._begin_audition_progress(gen, node.label)
        self.statusBar().showMessage(f"Rendering audition of {node.label}…", 0)
        w = workers.Worker(render_node, node.payload, node.kind, opts, progress=relay)
        w.signals.finished.connect(
            lambda r, g=gen, n=node: self._on_audition_ready(g, r, n)
        )
        w.signals.error.connect(lambda msg, g=gen: self._on_audition_error(g, msg))
        w.signals.finished.connect(lambda *_: self._audition_worker_done(w))
        w.signals.error.connect(lambda *_: self._audition_worker_done(w))
        self._audition_worker = w
        workers.run(w)

    def _audition_staged(self, bank, preset_obj, name: str, edits=None) -> None:
        """New Bank row -> a background render of the preset AS STAGED."""
        from ..audition import render_staged  # noqa: PLC0415, RUF100

        try:
            opts = self._audition_options()
        except ValueError as ex:
            self.statusBar().showMessage(f"Audition settings: {ex}", 10000)
            return
        self._silence_previous_auditions()
        self._audition_gen += 1
        gen = self._audition_gen
        relay = self._begin_audition_progress(gen, name)
        self.statusBar().showMessage(f"Rendering audition of {name}…", 0)
        w = workers.Worker(
            render_staged, bank, preset_obj, opts, name, edits, progress=relay
        )
        w.signals.finished.connect(
            lambda r, g=gen, n=name: self._on_audition_ready(g, r, _NamedNode(n))
        )
        w.signals.error.connect(lambda msg, g=gen: self._on_audition_error(g, msg))
        w.signals.finished.connect(lambda *_: self._audition_worker_done(w))
        w.signals.error.connect(lambda *_: self._audition_worker_done(w))
        self._audition_worker = w
        workers.run(w)

    def _silence_previous_auditions(self) -> None:
        """Stop whatever an earlier audition is still playing.

        Nothing did this, and two complaints came out of it that looked
        unrelated. An audition can be twenty seconds long -- a three-note
        render of a preset with long release tails measured 20.3 s -- so
        asking for the next one while the last is still sounding left both
        playing at once, and each further click added another. That is the
        "stuck, keeps playing".

        The second complaint was the same fault wearing a different face:
        with the report window switched back ON, a new audition opened a
        report whose own player was idle -- correctly showing "Play" -- while
        the sound still coming out belonged to a NOTICE from an earlier,
        report-disabled audition. The button was right; it was describing a
        different player.

        A NOTICE is closed, because it exists only to carry the sound and a
        silent one says "Auditioning ..." about nothing. A REPORT window is
        left open and merely silenced: someone may be reading it, and its
        transport will show "Play" because that is now true of it.
        """
        from .audition_dialog import AuditionNotice  # noqa: PLC0415, RUF100

        for window in list(self._audition_dialogs):
            player = getattr(window, "_player", None)
            if player is not None:
                try:
                    player.stop()
                except RuntimeError:
                    pass  # already gone; nothing to silence
            if isinstance(window, AuditionNotice):
                window.close()
            else:
                # Silencing a window without telling it leaves the transport
                # claiming "Replay" over nothing -- the exact mismatch
                # between button and reality that this whole fix is about.
                sync = getattr(window, "_sync_transport", None)
                if sync is not None:
                    try:
                        sync()
                    except RuntimeError:
                        pass

    def _begin_audition_progress(self, gen: int, label: str):
        """Show the progress window at once and return the hook `render()`
        should call. The window is up BEFORE the work starts, which is the
        whole point -- rendering a big multisample used to look like nothing
        happening followed by a window."""
        from .audition_dialog import AuditionProgress  # noqa: PLC0415, RUF100

        dialog = AuditionProgress(label, parent=self)
        relay = _ProgressRelay(self)
        relay.tick.connect(
            lambda pct, text, d=dialog, g=gen: (
                d.set_progress(pct, text)
                if g == self._audition_gen and d.isVisible()
                else None
            )
        )
        dialog.cancelled.connect(lambda g=gen: self._cancel_audition(g))
        self._audition_progress = dialog
        self._audition_relay = relay  # held: a collected relay emits to
        dialog.show()  # nothing, silently
        return relay.tick.emit

    def _cancel_audition(self, gen: int) -> None:
        """Stop caring about a render in flight.

        The worker is not killed -- nothing here can safely interrupt a
        parse mid-way -- so the generation counter is bumped instead and its
        result is dropped on arrival, exactly as a superseded one is. The
        cost is a thread finishing work nobody wants, which is finite and
        invisible, against the alternative of leaving the user no way out.
        """
        if gen == self._audition_gen:
            self._audition_gen += 1
        self._end_audition_progress()
        self.statusBar().showMessage("Audition cancelled", 4000)

    def _end_audition_progress(self) -> None:
        dialog = getattr(self, "_audition_progress", None)
        self._audition_progress = None
        self._audition_relay = None
        if dialog is not None:
            dialog.close()

    def _audition_worker_done(self, w) -> None:
        if self._audition_worker is w:
            self._audition_worker = None

    def _set_audition_auto_play(self, on: bool) -> None:
        """The one writer for auto-play, so the menu tick, the checkbox in the
        report window and the file cannot drift apart."""
        on = bool(on)
        if getattr(self._config, "audition_auto_play", True) == on:
            return
        self._config.audition_auto_play = on
        if self._audition_autoplay_action.isChecked() != on:
            self._audition_autoplay_action.setChecked(on)
        self._config.save()
        self.statusBar().showMessage(
            "Auditions will play automatically"
            if on
            else "Auditions will wait for you to press Play — View ▸ Play "
            "Auditions Automatically brings it back",
            6000,
        )

    def _set_audition_show_report(self, on: bool) -> None:
        """The one writer for this setting, so the menu tick, the checkbox in
        the report window and the file cannot drift apart."""
        on = bool(on)
        if self._config.audition_show_report == on:
            return
        self._config.audition_show_report = on
        if self._audition_report_action.isChecked() != on:
            self._audition_report_action.setChecked(on)
        self._config.save()
        self.statusBar().showMessage(
            "Audition will show its report each time"
            if on
            else "Audition will play straight away — View ▸ Show Audition Report "
            "brings it back",
            6000,
        )

    def _on_audition_ready(self, gen: int, rendering, node) -> None:
        if gen != self._audition_gen:
            # A later request superseded this one, or it was cancelled;
            # delivering it would play the wrong preset with no sign that it
            # had. The progress window belongs to whatever replaced it, so it
            # is NOT taken down here.
            return
        self._end_audition_progress()
        self.statusBar().clearMessage()
        if not self._config.audition_show_report:
            from .audition_player import check_playback  # noqa: PLC0415, RUF100

            # Opting out of the REPORT must not become opting out of the
            # audition. With nothing to play through, the notice would show a
            # name and fall silent, so the full window is shown instead --
            # it holds Save as WAV, which is the only way to hear it then.
            #
            # Same reasoning for auto-play being OFF: the notice is a
            # "this is playing right now" window, and it has no Play button,
            # so honouring the setting there would mean a window that says
            # "Auditioning <name>…" and then sits silent forever. The report
            # window has a transport, so it is the one that can be quiet on
            # purpose. Falling back to it keeps the two settings independent,
            # which is the whole reason they are separate.
            if (
                getattr(self._config, "audition_auto_play", True)
                and check_playback()[0]
                and self._show_audition_notice(rendering, node)
            ):
                return
        self._show_audition_report(rendering, node)

    def _show_audition_notice(self, rendering, node) -> bool:
        """The small "Auditioning <name>" window. False if it would not play,
        so the caller can fall back to the report."""
        from .audition_dialog import AuditionNotice  # noqa: PLC0415, RUF100

        notice = AuditionNotice(
            rendering,
            node.label,
            parent=self,
            volume=int(getattr(self._config, "audition_volume", 100)),
        )
        # Same ownership rule as the report window: held in a LIST, because a
        # second audition must not drop the first one's Python wrapper while
        # its audio is still running.
        self._audition_dialogs.append(notice)
        notice.finished.connect(lambda *_, d=notice: self._forget_audition_dialog(d))
        notice.reportRequested.connect(
            lambda player, r=rendering, n=node: self._show_audition_report(
                r, n, player=player
            )
        )
        notice.show()
        if not notice.start():
            notice.close()
            return False
        return True

    def _show_audition_report(self, rendering, node, player=None) -> None:
        from .audition_dialog import AuditionDialog  # noqa: PLC0415, RUF100

        # `player` arrives still PLAYING when this was reached from the
        # notice's "Show report": the report describes the sound, so reading
        # it must not silence it.
        dialog = AuditionDialog(
            rendering,
            title=f"Audition — {node.label}",
            parent=self,
            show_report_default=bool(self._config.audition_show_report),
            auto_play_default=bool(getattr(self._config, "audition_auto_play", True)),
            volume=int(getattr(self._config, "audition_volume", 100)),
            player=player,
        )
        # Held so the QAudioSink and its QBuffer are not collected mid-note,
        # and held in a LIST so a second audition cannot drop the first
        # dialog's Python wrapper while its audio is still playing.
        self._audition_dialog = dialog
        self._audition_dialogs.append(dialog)
        dialog.showReportChanged.connect(self._set_audition_show_report)
        dialog.autoPlayChanged.connect(self._set_audition_auto_play)
        dialog.finished.connect(lambda *_, d=dialog: self._forget_audition_dialog(d))
        dialog.show()

    def _forget_audition_dialog(self, dialog) -> None:
        try:
            self._audition_dialogs.remove(dialog)
        except ValueError:
            pass

    def _on_audition_error(self, gen: int, message: str) -> None:
        if gen != self._audition_gen:
            return
        self._end_audition_progress()
        self.statusBar().clearMessage()
        QMessageBox.warning(self, "Audition", workers.last_error_line(message))

    # -- XPM import ---------------------------------------------------------------

    def _import_xpm(
        self,
        path: Optional[str] = None,  # noqa: UP045
        preset_index: Optional[int] = None,  # noqa: UP045
    ) -> None:  # noqa: RUF100, UP045
        """path: pre-chosen (e.g. Explorer's "Import…" on an MPC row/hit --
        see importXpmRequested) or None to prompt with a file picker
        (File > Import MPC Program…).

        preset_index: one program out of a project (.xpj), or None for
        everything the file holds -- which for a .xpm or .xty is its single
        program anyway, and for a project is all of them at once."""
        if self._xpm_import_worker is not None or self._import_worker is not None:
            # Both, since an MPC program now also reaches the shared
            # convert-first queue (a drag onto New Bank) -- two importers
            # writing into New Bank at once would interleave their presets.
            self.statusBar().showMessage("An import is already running")
            return
        if not path:
            path, _filter = QFileDialog.getOpenFileName(
                self,
                "Import MPC Program",
                self._start_dir("last_program_dir"),
                "Akai MPC programs (*.xpm *.xty *.xpj)",
                options=QFileDialog.Option.DontUseNativeDialog,
            )
            if not path:
                return
            self._remember_dir("last_program_dir", path)
        self._remember_import(lambda p=path, i=preset_index: self._import_xpm(p, i))
        opts = FormatConvertDialog.get_import_options(
            self,
            locked_format=self._bank_pane.format,
            bank_loader=lambda: xpm_import.load_samples_for_test(
                path, None, preset_index
            ),
            source_text=path,
        )
        opts = self._with_pending_shrink(opts)
        if opts is None:
            return
        self.statusBar().showMessage(f"Importing {Path(path).name}…")
        risks: list = []
        w = workers.Worker(xpm_import.import_xpm, path, opts, None, risks, preset_index)
        w.signals.finished.connect(
            lambda tmp_path, p=path, r=risks, i=preset_index: self._on_xpm_imported(
                tmp_path, p, opts, r, i
            )
        )
        w.signals.error.connect(self._on_xpm_import_error)
        w.signals.finished.connect(lambda *_: setattr(self, "_xpm_import_worker", None))
        w.signals.error.connect(lambda *_: setattr(self, "_xpm_import_worker", None))
        self._xpm_import_worker = w
        workers.run(w)

    def _on_xpm_imported(
        self,
        tmp_path: str,
        xpm_path: str,
        opts: convert.ConversionOptions,
        risks: Optional[list] = None,  # noqa: UP045
        preset_index: Optional[int] = None,  # noqa: UP045
    ) -> None:  # noqa: RUF100, UP045
        # No save dialog, no library folder at all -- what came out is one
        # or more programs, and they belong in New Bank the same way
        # dragging presets in from Explorer does, not as a "bank" of their
        # own in Pending for Image. A .xpm or .xty always yields exactly
        # one; only a whole-project import yields several (one per keygroup
        # track -- mpc2emu 8e20612).
        #
        # Read the just-converted temp file's bytes but label the result
        # with the ORIGINAL xpm_path, not the (freshly, uniquely,
        # per-import) generated temp path -- BankPane's duplicate check
        # keys on bank.path + preset index/id (see bank_pane.py's
        # _preset_key()), and every import of the *same* source XPM
        # should be recognized as the same preset, not a new one each time
        # just because its throwaway temp file happened to land somewhere
        # else. index/id are already stable within a freshly written bank.
        #
        # A single program out of a project needs the program in that label
        # too: each such import writes a ONE-preset bank, so every program
        # of the same project would otherwise come back as index 0 of the
        # same path and the second one would be silently swallowed as a
        # duplicate of the first.
        label_path = xpm_path if preset_index is None else f"{xpm_path}#{preset_index}"
        pairs = self._read_back_converted_presets(tmp_path, opts, label_path=label_path)
        if not pairs:
            return
        # preset.name is mpc2emu's own E4B-format preset name -- truncated
        # to 16 chars (a real hardware limit; see xpm_parser.py's
        # _safe_name()), so several distinctly-named XPMs sharing a long
        # common prefix (e.g. "Bass-Pulse-Bass 1d"/"2a"/"3b") all collapse
        # to the same displayed name ("Bass-Pulse-Bass") if that's what's
        # used here. The still-distinguishing original filename is what
        # the user actually named these files by, so it's what New
        # Bank's list should show -- this only affects the display label
        # passed around VinSamLib's own UI, not the real (already
        # 16-char-truncated, same as any hardware bank) name baked into
        # preset_obj itself, which is unaffected.
        # NOT run through unique_name() -- unlike preset conversion below,
        # re-importing the same XPM is already correctly content-deduped
        # (same xpm_path -> same identity -> "already present, skipped"),
        # so pre-uniquifying the name here would show a misleading "(2)"
        # on that skip message for what's actually a plain duplicate, not
        # a second distinct item.
        stem = Path(xpm_path).stem
        if preset_index is None and len(pairs) == 1:
            names = [_via_mpc2emu(stem or pairs[0][1].name.strip() or "Imported XPM")]
        else:
            # Anything out of a project: the filename is shared by every
            # program in it and so can't tell them apart, while the program
            # names genuinely do (they come from the tracks, not the file).
            # This is the one case where the preset's own name is the better
            # label -- the file's is only the fallback.
            names = [
                _via_mpc2emu(
                    p.name.strip() or f"{stem} {_program_number(preset_index, i)}"
                )
                for i, (_bank, p) in enumerate(pairs)
            ]
        self._bank_pane.add_presets(
            [
                (bank, preset, opts.target_format, name)
                for (bank, preset), name in zip(pairs, names)
            ],  # noqa: B905, RUF100
            # A conversion: mpc2emu's writer has already reported any zone
            # over the rate ceiling, and two boxes about one fact is worse
            # than none.
            #
            # Say WHICH import these are, so the ceiling findings it reports
            # can be attributed to them rather than to whatever happens to be
            # last in the pane. Recorded per add, so a drag-drop landing
            # mid-import cannot join this batch.
            risk_batch=True,
            check_ceiling=False,
        )
        self._warn_polyphony(risks or [], "Import MPC Program")

    def _warn_polyphony(self, risks: list, title: str) -> None:
        """Per-note voice budget, reported after a conversion rather than
        before it: the count only becomes final once mono reduction and the
        zone reducer have run (see build/convert.py's polyphony_risk()).
        The import still lands in New Bank either way -- an over-budget
        preset is playable, it just won't sound every layer, and only the
        user can decide whether that matters for this material.

        ALSO WHERE THE IMPORT PATH'S CEILING FINDINGS GET AN IDENTITY. An
        import passes `check_ceiling=False` because mpc2emu's writer has
        already reported, so its findings reach here with nothing tying them
        to a staged preset -- and a finding that cannot say which preset it is
        about is REFUSED by the Narrow button rather than applied to every
        staged one, which is what a review of this branch found the old
        fallback did. `take_risk_batch()` is what the import staged, recorded
        as each item went in.

        THE BATCH IS ALWAYS TAKEN, and the flag always set, even with no
        risks: `take_risk_batch()` clears what it returns, so skipping it
        would let one import's staged items be counted against the next
        import's findings. And `_ceiling_narrowable` is assigned rather than
        only assigned-when-truthy -- it gates the Narrow button, and a stale
        True from a previous batch would offer a button on a box whose
        findings cannot be acted on.
        """
        batch = self._bank_pane.take_risk_batch()
        tagged = self._bank_pane.tag_ceiling_findings(risks, batch)
        self._ceiling_narrowable = bool(risks) and bool(tagged)
        if not risks:
            return
        # When an import staged multiple presets, ceiling findings cannot be
        # attributed to one preset, so the Narrow button is not offered. Say
        # why, so the absence of the button does not read as a bug.
        ceiling_risks = [r for r in risks if r.get("code") == self._CEILING_CODE]
        if ceiling_risks and not tagged:
            self.statusBar().showMessage(
                f"{len(ceiling_risks)} zone(s) above the rate ceiling, but "
                f"the import staged {len(batch)} presets -- narrowing needs "
                f"a single preset to attribute zones to. Import presets "
                f"one at a time to narrow their zones.",
                12000,
            )
        lines = convert.polyphony_risk_lines(risks)
        # This list is no longer polyphony-only: _verify_written's zone-loss
        # finding arrives through it too, carrying its own sentence. The
        # per-note-voice explanation below is appended only when a polyphony
        # risk is actually present, or a bank that lost zones would be
        # answered with advice about stereo voice cost.
        polyphony = [r for r in risks if not r.get("message")]
        # Three kinds share this channel now. A `code` marks one of mpc2emu's
        # structured diagnostics (build/convert.py's _diagnostic_risks) --
        # something the CONVERTER changed or could not carry, which is not the
        # same news as a bank that came out of the writer short, and counting
        # them together would report both under whichever label was written
        # first.
        diags = [r for r in risks if r.get("code")]
        lost = sum(1 for r in diags if r.get("content_lost"))
        others = len(risks) - len(polyphony) - len(diags)
        summary = ", ".join(
            part
            for part in (
                f"{len(polyphony)} preset(s) over the per-note voice limit"
                if polyphony
                else "",
                (
                    f"{len(diags)} conversion warning(s)"
                    + (f", {lost} losing content" if lost else "")
                )
                if diags
                else "",
                f"{others} written-bank warning(s)" if others else "",
            )
            if part
        )
        self.statusBar().showMessage(summary, 8000)
        # CAPPED. Even grouped, a conversion can produce more distinct
        # findings than anybody reads standing up, and a box taller than the
        # screen is one nobody reads at all -- the first few are the ones
        # that get acted on either way.
        shown, extra = lines[:8], max(0, len(lines) - 8)
        detail = "\n\n".join(shown)
        if extra:
            detail += (
                f"\n\n… and {extra} more finding(s) of other kinds. "
                f"They are of the same severity as these; nothing is "
                f"being hidden because it is worse."
            )
        if polyphony:
            detail += (
                "\n\nA stereo sample costs two voices, and the ceiling is per "
                "NOTE -- both measured on the machine itself: 32 per note on "
                "the E4XT (whose global polyphony is 128), and 24 on the "
                "K2000R, which is its whole polyphony.\n\n"
                "To fix, re-import with Convert Options' \"Reduce Velocity "
                'Layers", or pick a Stereo Samples method other than Keep '
                "Stereo to halve every stereo zone's cost."
            )
        self._show_risk_box(title, detail, risks)

    #: mpc2emu's "you asked for a size I cannot reach by thinning". It carries
    #: the size it COULD reach, which is the whole reason this gets a button
    #: of its own rather than a paragraph: the fix is one number.
    _SHRINK_UNREACHABLE = "SHRINK_TARGET_UNREACHABLE"

    #: mpc2emu's "this zone can be played past the E4XT's rate ceiling". Like
    #: the shrink one, it publishes the number that fixes it --
    #: `highest_safe_key`, exact for the zone it names -- so the remedy is a
    #: button rather than a sentence asking the user to open Adjust Placement
    #: and work it out. Their writer's own comment anticipates this UI.
    #:
    #: It is NOT applied automatically. Across 113 commercial E4B banks here,
    #: 10.8% of presets carry a zone above the ceiling AS AUTHORED -- the keys
    #: are at the top of the keyboard and nobody plays them -- so clamping on
    #: import would silently edit a tenth of a bought library to fix something
    #: inaudible.
    _CEILING_CODE = "E4B_ZONE_ABOVE_PLAYBACK_CEILING"

    def _with_pending_shrink(self, opts):
        """Apply a target the user raised from the warning box, once.

        The re-import goes through the ordinary dialog -- same route, same
        options as before -- and this is the one value that has to come from
        the button instead. Consumed on use, so a later import the user
        starts themselves is not quietly re-targeted.
        """
        target = getattr(self, "_pending_shrink_target", None)
        if target is None or opts is None:
            return opts
        self._pending_shrink_target = None
        from dataclasses import replace  # noqa: PLC0415, RUF100

        try:
            return replace(opts, shrink_to_bytes=int(target), shrink_by_pct=None)
        except TypeError:
            return opts

    def _show_risk_box(self, title: str, detail: str, risks: list) -> None:
        """The warning box, plus a way out where the record supplies one.

        Most diagnostics can only be reported: the user decides whether they
        care. SHRINK_TARGET_UNREACHABLE is different -- it says the target
        could not be met AND publishes the smallest size that could, so the
        obvious next step is a single number this program already knows. It
        offers that as a button rather than as a sentence asking the user to
        retype it.
        """
        reachable = [
            r
            for r in risks
            if r.get("code") == self._SHRINK_UNREACHABLE
            and isinstance(r.get("detail"), dict)
            and r["detail"].get("reached_bytes")
        ]
        ceiling = [
            r["detail"]
            for r in risks
            if r.get("code") == self._CEILING_CODE
            and isinstance(r.get("detail"), dict)
            and r["detail"].get("highest_safe_key") is not None
        ]
        offer_shrink = bool(
            reachable and getattr(self, "_last_import", None) is not None
        )
        if not (offer_shrink or ceiling):
            # THE ORDINARY PATH STAYS THE ORDINARY CALL. Replacing this with a
            # constructed box unconditionally broke every test that triggers a
            # warning -- they stub QMessageBox.warning, which a constructed
            # box with its own exec() never reaches, so they hung on a modal
            # dialog. The custom box is for the one case that has a button to
            # add; everything else keeps the static call it always used.
            QMessageBox.warning(self, title, detail)
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(detail)
        raise_btn = None
        if offer_shrink:
            # The biggest of them: raising to the largest unreachable floor is
            # the only single target that clears every preset in the bank.
            target = max(int(r["detail"]["reached_bytes"]) for r in reachable)
            raise_btn = box.addButton(
                f"Raise target to {human_size(target)} and re-import",
                QMessageBox.ButtonRole.ActionRole,
            )
            box.setDefaultButton(raise_btn)
        narrow_btn = None
        # ONLY WHEN SOMETHING IS ATTRIBUTABLE. An import of several presets
        # produces findings that name no preset, so the button would have
        # nothing to write to; offering it anyway is a button that reports
        # success and changes nothing, which is the fault the token fixed.
        if ceiling and self._ceiling_narrowable:
            keys = sorted({int(d["highest_safe_key"]) for d in ceiling})
            where = (
                f"key {keys[0]}"
                if len(keys) == 1
                else f"their highest safe keys ({keys[0]}–{keys[-1]})"
            )
            narrow_btn = box.addButton(
                f"Narrow {len(ceiling)} zone(s) to {where}",
                QMessageBox.ButtonRole.ActionRole,
            )
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if raise_btn is not None and box.clickedButton() is raise_btn:
            self._retry_with_shrink_target(target)
        elif narrow_btn is not None and box.clickedButton() is narrow_btn:
            self._narrow_zones_to_ceiling(ceiling)

    def _set_ceiling_warning(self, on: bool) -> None:
        """View menu, and the warning box's own way out, write the same
        setting -- one state, so the menu never disagrees with what the box
        just did."""
        self._config.warn_playback_ceiling = bool(on)
        self._config.save()
        if hasattr(self, "_ceiling_warn_action"):
            self._ceiling_warn_action.blockSignals(True)
            self._ceiling_warn_action.setChecked(bool(on))
            self._ceiling_warn_action.blockSignals(False)
        self.statusBar().showMessage(
            "Rate-ceiling warnings on"
            if on
            else "Rate-ceiling warnings off — View menu turns them back on",
            8000,
        )

    def _on_ceiling_zones_added(self, findings: list) -> None:
        """A plain Add found zones past the E4XT's rate ceiling.

        The same box and the same Narrow button an IMPORT gets, because it is
        the same fact about the same bytes -- only the route differed, and
        until now so did whether anyone told you.

        Off by choice rather than by nagging: the box carries "Don't warn
        again", which writes the View-menu setting. The rate is a property of
        the library (43.9% of presets in one synth library here, 0% in an
        orchestral one), so somebody working in the wrong kind would otherwise
        answer this on every other preset.
        """
        if not findings or not getattr(self._config, "warn_playback_ceiling", True):
            return
        # ONE LINE PER SAMPLE, not per zone. A preset stacks voices, and
        # every voice has its own zone on the same sample -- the one that
        # prompted this has three, so the box said the same sentence three
        # times with the safe key differing by one, which reads as a bug
        # rather than as a preset with three layers. Same rule the conversion
        # warnings already follow for identical findings.
        grouped: dict = {}
        for d in findings:
            name = (d.get("sample_name") or "?").strip()
            cur = grouped.get(name)
            # The STRICTEST safe key, because narrowing is per sample and the
            # strictest is the only one that clears every zone using it.
            if cur is None or int(d["highest_safe_key"]) < int(cur["highest_safe_key"]):
                d = dict(d)
                d["zone_count"] = (cur or {}).get("zone_count", 0) + 1
                grouped[name] = d
            else:
                cur["zone_count"] = cur.get("zone_count", 1) + 1
        worst = sorted(grouped.values(), key=lambda d: -int(d.get("keys_over") or 0))
        lines = []
        for d in worst[:5]:
            n = int(d.get("zone_count") or 1)
            zones = "" if n == 1 else f" in {n} of its voices"
            lines.append(
                f"'{(d.get('sample_name') or '?').strip()}'{zones} plays keys "
                f"{int(d['highest_safe_key']) + 1}–{d['hi_key']} past the "
                f"E4XT's rate ceiling ({d['sample_rate']} Hz, root "
                f"{d['root_key']}); the highest key that plays correctly is "
                f"{d['highest_safe_key']}."
            )
        extra = len(grouped) - len(lines)
        detail = "\n\n".join(lines)
        if extra > 0:
            detail += f"\n\n… and {extra} more sample(s) of the same kind."
        detail += (
            "\n\nTHIS IS HOW THE SOURCE BANK WAS AUTHORED. Adding a preset "
            "copies its bytes verbatim — nothing here has changed a zone, a "
            "root key or a sample rate, so what this describes was already "
            "true of the bank you added it from, and is true of it on the "
            "machine today.\n\n"
            # ENDS HERE. The prevalence measurement and the "not damaged"
            # reassurance were both true and both belong in the README, not
            # in a box somebody is reading in order to get on with something
            # else. Jan, 2026-09-28: "this is too much for a warning window".
            "Above that rate the voice runs past the end of its sample into "
            "neighbouring sample RAM. The keys it affects are at the very top "
            "of the keyboard, above where such a preset is normally played, "
            "which is why it often goes unnoticed."
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Zones above the E4XT's rate ceiling")
        box.setText(detail)
        # The button counts SAMPLES, because that is what a placement edit
        # moves -- one per sample, at its strictest safe key.
        keys = sorted({int(d["highest_safe_key"]) for d in grouped.values()})
        where = (
            f"key {keys[0]}"
            if len(keys) == 1
            else f"their highest safe keys ({keys[0]}–{keys[-1]})"
        )
        narrow_btn = box.addButton(
            f"Narrow {len(grouped)} sample(s) to {where}",
            QMessageBox.ButtonRole.ActionRole,
        )
        never_btn = box.addButton(
            "Don't warn again", QMessageBox.ButtonRole.DestructiveRole
        )
        # NOT "OK". Two of the three buttons here DO something, and the third
        # is the one most people will press -- "OK" reads as consent to
        # whatever was just described rather than as "change nothing", which
        # on a box about zones the vendor authored is exactly backwards.
        keep_btn = box.addButton(
            "Leave the zones as they are", QMessageBox.ButtonRole.RejectRole
        )
        box.setDefaultButton(keep_btn)
        box.setEscapeButton(keep_btn)
        box.exec()
        if box.clickedButton() is narrow_btn:
            self._narrow_zones_to_ceiling(findings)
        elif box.clickedButton() is never_btn:
            self._set_ceiling_warning(False)

    def _narrow_zones_to_ceiling(self, findings: list) -> None:
        """Apply the writer's own `highest_safe_key` to the staged zones.

        Through the SAME placement machinery Adjust Placement… uses, so the
        edit is the one the bank already knows how to carry and is applied
        where every other placement edit is applied -- at assemble time,
        against the source sample's name.

        Says what it did, including what it did NOT do: mpc2emu reports one
        zone per preset, its worst, so a preset with several over-zones is
        only partly answered by this and the count of the rest is the only
        honest thing to show.
        """
        applied, unmatched, siblings = self._bank_pane.narrow_zones_to_ceiling(findings)
        parts = []
        if applied:
            # SAMPLES, and bank-wide: a placement is keyed by the sample's
            # name, so this moved it in every staged preset that uses it. Not
            # a detail to leave for someone to discover -- Jan discovered it.
            parts.append(
                f"narrowed {applied} sample(s) to their highest safe "
                f"key, in every staged preset using them"
            )
        if unmatched:
            parts.append(
                f"{unmatched} left alone (no match, or the sample "
                f"sits at another root in another staged preset)"
            )
        if siblings:
            parts.append(
                f"{siblings} further over-zone(s) were counted but "
                f"not named by the converter, and are untouched"
            )
        self.statusBar().showMessage("; ".join(parts) or "nothing to narrow", 12000)

    def _retry_with_shrink_target(self, target_bytes: int) -> None:
        """Re-run the last import with the shrink target raised.

        The presets that came out of the failed attempt are left alone rather
        than removed: they were thinned as far as thinning goes, which may
        well be what the user wants to keep. This adds the corrected import
        beside them, and New Bank's duplicate prompt is what decides -- the
        same question it asks any other time the same thing arrives twice.
        """
        self._pending_shrink_target = int(target_bytes)
        again = getattr(self, "_last_import", None)
        if again is None:
            return
        again()

    def _on_xpm_import_error(self, message: str) -> None:
        last_line = workers.last_error_line(message)
        self.statusBar().showMessage(f"MPC import failed: {last_line}", 8000)
        QMessageBox.warning(
            self, "Import MPC Program", f"Import failed:\n\n{last_line}"
        )

    # -- sample-folder import -------------------------------------------------------

    def _import_sample_dir(self) -> None:
        """File > Import Sample Folder... -- deliberately not offered from
        Explorer (see build/sampledir_import.py's module docstring for
        why): the user picks a folder and decides what to do with it,
        there's no "browse to this and recognize it" moment the way a
        real .xpm file or an already-native preset has."""
        if self._sample_dir_import_worker is not None:
            self.statusBar().showMessage("A sample folder import is already running")
            return
        path = QFileDialog.getExistingDirectory(
            self,
            "Import Sample Folder",
            self._start_dir("last_sample_dir"),
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not path:
            return
        self._remember_dir("last_sample_dir", path)
        self._start_sample_import(path, Path(path).name)

    def _import_sample_files(self) -> None:
        """File > Import Samples... -- the same import, for a folder that
        holds more than one instrument (or more files than belong together).
        The picked files are staged into one directory, since that is the
        shape mpc2emu's parse_sample_dir() reads; see
        build/sampledir_import.stage_files()."""
        if self._sample_dir_import_worker is not None:
            self.statusBar().showMessage("A sample folder import is already running")
            return
        paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "Import Samples",
            self._start_dir("last_sample_dir"),
            "Audio files (*.wav *.WAV *.aif *.aiff);;All files (*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not paths:
            return
        self._remember_dir("last_sample_dir", paths[0])
        staged = sampledir_import.stage_files(paths)
        self.statusBar().showMessage(
            f"{len(paths)} sample(s) selected from {Path(paths[0]).parent.name}"
        )
        # The real origin, not staged.name: the files were copied into a temp
        # directory to give parse_sample_dir() the shape it reads, and naming
        # that at the top of the dialog would point at a path the user has
        # never seen. The folder they picked from, and how many they picked.
        self._start_sample_import(
            staged.name,
            sampledir_import.selection_label(paths),
            staged,
            source_text=f"{len(paths)} file(s) from " f"{Path(paths[0]).parent}",
        )

    def _start_sample_import(
        self, path: str, label: str, staged=None, source_text: str = ""
    ) -> None:
        """Shared tail of both sample imports: same options dialog, same
        worker, same landing in New Bank. `label` names the preset -- the
        folder for a folder import, what the filenames have in common for a
        hand-picked one -- and `staged`, when there is one, is the temporary
        directory holding the selection, kept alive until the import is over
        (the dialog re-reads it for its preview) and dropped either way.

        `source_text` is what the dialog shows at the top, and it is a
        SEPARATE argument from `path` on purpose: for a hand-picked selection
        `path` is the staging directory, so showing it would name a temp
        directory the user has never seen instead of the files they chose."""
        (opts, octave_offset, zone_overrides, naming, velocity_overrides) = (
            SampleDirImportDialog.get_import_options(
                self,
                locked_format=self._bank_pane.format,
                sample_loader=lambda octave: sampledir_import.load_samples_for_test(
                    path, octave
                ),
                placement_loader=lambda octave: sampledir_import.parse_preview(
                    path, octave
                ),
                source_text=source_text or path,
            )
        )

        def drop_staging(*_):
            if staged is not None:
                try:
                    staged.cleanup()
                except OSError:
                    pass  # already gone, or the OS took it first

        if opts is None:
            drop_staging()
            return
        self.statusBar().showMessage(f"Importing {label}…")
        risks: list = []
        # octave for the generated names: the picker's own choice, or the
        # display fallback when it is on Auto-detect (nothing else knows yet).
        name_octave = octave_offset if octave_offset is not None else 2
        # Keywords past `octave_offset`: this call grew a parameter in the
        # middle and the positional form silently slid `risks` into
        # `velocity_overrides`, which type-checks and quietly does the wrong
        # thing at runtime.
        w = workers.Worker(
            sampledir_import.import_sample_dir,
            path,
            opts,
            octave_offset,
            zone_overrides=zone_overrides,
            velocity_overrides=velocity_overrides,
            risks_out=risks,
            bank_name=label,
            name_base=naming[0],
            name_octave=name_octave,
            name_with_key=naming[1],
            name_overrides=naming[2],
        )
        w.signals.finished.connect(
            lambda tmp_path, p=path, n=label, r=risks: self._on_sample_dir_imported(
                tmp_path, p, opts, r, n
            )
        )
        w.signals.error.connect(self._on_sample_dir_import_error)
        w.signals.finished.connect(
            lambda *_: setattr(self, "_sample_dir_import_worker", None)
        )
        w.signals.error.connect(
            lambda *_: setattr(self, "_sample_dir_import_worker", None)
        )
        w.signals.finished.connect(drop_staging)
        w.signals.error.connect(drop_staging)
        self._sample_dir_import_worker = w
        workers.run(w)

    def _on_sample_dir_imported(
        self,
        tmp_path: str,
        dir_path: str,
        opts: convert.ConversionOptions,
        risks: Optional[list] = None,  # noqa: UP045
        label: str = "",
    ) -> None:
        # Same "single preset, straight into New Bank" landing as XPM
        # import -- parse_sample_dir() always produces exactly one
        # multisampled preset per folder, never a whole bank of its own.
        result = self._read_back_converted(tmp_path, opts, label_path=dir_path)
        if result is None:
            return
        bank, preset = result
        name = label or Path(dir_path).name or preset.name.strip() or "Imported Samples"
        self._bank_pane.add_presets(
            [(bank, preset, opts.target_format, name)],
            # Say WHICH import these are, so the
            # ceiling findings it reports can be
            # attributed to them. Recorded per add,
            # so a drag-drop landing mid-import
            # cannot join this batch.
            risk_batch=True,
            check_ceiling=False,
        )
        self._warn_polyphony(risks or [], "Import Sample Folder")

    def _on_sample_dir_import_error(self, message: str) -> None:
        last_line = workers.last_error_line(message)
        self.statusBar().showMessage(f"Sample folder import failed: {last_line}", 8000)
        QMessageBox.warning(
            self, "Import Sample Folder", f"Import failed:\n\n{last_line}"
        )

    def _read_back_converted_presets(
        self, tmp_path: str, opts: convert.ConversionOptions, label_path: str
    ) -> list[tuple]:
        """Shared by _on_xpm_imported() and _on_preset_converted(): both
        hand mpc2emu's freshly-written temp file back to VinSamLib's OWN
        parser (never mpc2emu's) so what lands in New Bank is a normal,
        byte-verbatim VinSamLib bank/preset pair like any other -- and
        both label the re-parse with a caller-chosen stable path rather
        than the throwaway temp path, so BankPane's duplicate check
        (bank.path + preset index/id, see bank_pane.py's _preset_key())
        keeps working.

        Returns every (bank, preset) the written file holds, in the bank's
        own order -- more than one only for a whole-project import; [] after
        showing a warning on failure."""
        try:
            if opts.target_format == "AKAI":
                # A DIRECTORY of loose .P3/.S3 files, not a bank file. Parsed
                # with this project's own reader like every other target, so
                # what lands in New Bank is an ordinary AkaiBank/AkaiProgram
                # pair and the whole downstream -- the object budget, the
                # partition breaks, Build Image -- works on it unchanged.
                from ..banks import akai as vs_akai  # noqa: PLC0415, RUF100

                bank = vs_akai.parse_dir(tmp_path)
                bank.path = label_path
                return [(bank, program) for program in bank.programs]
            data = Path(tmp_path).read_bytes()
            if opts.target_format == "KRZ":
                bank = krz.parse_bytes(data, label_path)
                presets = list(bank.programs.values())
            elif opts.target_format == "EIII":
                bank = eiii.parse_bytes(data, label_path)
                presets = list(bank.presets)
            else:
                bank = e4b.parse_bytes(data, label_path)
                presets = list(bank.presets)
            return [(bank, preset) for preset in presets]
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(
                self,
                "Import via mpc2emu",
                f"Couldn't read back the converted bank:\n\n{ex}",
            )
            return []

    def _read_back_converted(
        self, tmp_path: str, opts: convert.ConversionOptions, label_path: str
    ) -> Optional[tuple]:  # noqa: UP045
        """The single-preset callers' view of _read_back_converted_presets()
        -- a sample-folder import and a preset conversion each write exactly
        one preset, so anything past the first would be a bug, not a case to
        handle."""
        pairs = self._read_back_converted_presets(tmp_path, opts, label_path)
        return pairs[0] if pairs else None

    # -- convert an existing E4B preset via mpc2emu --------------------------------

    # ── soundfont-style import sources (SF2, SFZ, EXS24, TAL, GIG) ─────────

    def _import_foreign_file(self) -> None:
        """File ▸ Import Instrument… — the picker route into _import_foreign.

        A file chosen here is imported whole: picking one preset out of a
        multi-preset SoundFont is what the Explorer's rows are for, and this
        dialog has no way to show them.
        """
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Import Instrument",
            self._start_dir("last_instrument_dir"),
            "Sampler instruments (*.sf2 *.sfz *.exs *.talsmpl *.gig)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not path:
            return
        self._remember_dir("last_instrument_dir", path)
        verdict = foreign_import.inspect(path)
        if verdict is not None and verdict.empty_reason:
            # The Explorer greys such a row; a file picker has no row to grey,
            # so say it here rather than starting an import that cannot work.
            self.statusBar().showMessage(
                f"{Path(path).name}: {verdict.empty_reason}", 10000
            )
            return
        self._import_requests(
            [
                {
                    "path": path,
                    "format": foreign_import.format_for(path) or "",
                    "ordinal": None,
                    "name": Path(path).stem,
                }
            ]
        )

    #: How many presets may be counted before the answer becomes "unknown".
    #: Measured 2026-10-02: `summarize_*` + `zone_stats` costs ~110 ms per
    #: preset on a real E4B, so counting a 50-preset selection would block
    #: the window for 5.5 s to decide whether to hide two controls.
    #:
    #: A byte-level shortcut over the voice's velocity window was written and
    #: MEASURED against this one on 126 real presets, and it disagreed on 6 of
    #: them -- always counting higher, because `zone_stats()` counts over ZONES
    #: that resolve to a real sample and a byte walk counts every voice that
    #: declares one. Shipping it would have meant a second definition of "how
    #: many velocity layers", disagreeing with the number the Detail pane
    #: prints, which is the defect this branch just fixed for sizes. So the
    #: slow path stays the only definition and the work is bounded instead.
    _VELOCITY_COUNT_BUDGET = 4

    @staticmethod
    def _selection_has_velocity_layers(nodes: list):
        """Does any selected preset carry more than one velocity layer?

        `None` when it cannot be answered within the budget -- the dialog
        treats that as "unknown" and hides nothing, because absence of a count
        is not a count of zero. Three ways to get `None`, all honest refusals
        rather than a guess:

          * the node is not a preset, or its bank is not one this counts;
          * reading it raised, so there is no answer to report;
          * more presets than `_VELOCITY_COUNT_BUDGET`, where counting would
            cost seconds of frozen window.
        """
        from ..banks import summary as _summary  # noqa: PLC0415, RUF100

        seen = False
        counted = 0
        for node in nodes:
            # `>=`, NOT `>`. With `>`, a five-preset selection summarised
            # five, because the check ran before the count that the budget is
            # supposed to bound: the fifth node saw counted == 4, compared
            # 4 > 4, and went ahead. One extra reparse is ~110 ms of frozen
            # window on a number that decides nothing about a fifth preset --
            # the answer is already "unknown" either way.
            if counted >= MainWindow._VELOCITY_COUNT_BUDGET:
                return None
            payload = getattr(node, "payload", None)
            if not isinstance(payload, tuple) or len(payload) != 2:
                return None
            bank, preset = payload
            try:
                if hasattr(bank, "program_keymap_refs"):
                    ps = _summary.summarize_krz_program(bank, preset)
                elif hasattr(bank, "samples") and hasattr(preset, "body"):
                    ps = _summary.summarize_e4b_preset(bank, preset)
                else:
                    return None
                stats = _summary.zone_stats(getattr(ps, "zones", []) or [])
            except Exception:  # noqa: BLE001
                # A preset we cannot read is a preset we know nothing about,
                # so the answer is "unknown", which hides nothing. Caught here
                # rather than left to propagate: this runs while building the
                # import dialog, so an exception would take the import with it.
                return None
            counted += 1
            if stats is None:
                continue
            seen = True
            if stats.vel_layer_count > 1:
                return True
        return False if seen else None

    @staticmethod
    def _is_mpc_request(request: dict) -> bool:
        return Path(request["path"]).suffix.lower() in xpm_import.MPC_EXT_FORMAT

    def _import_requests(self, requests: list) -> None:
        """Import one or more convert-first sources into New Bank.

        Everything that has to be CONVERTED before it can be a preset comes
        through here -- the soundfont-style formats and the MPC's
        `.xpm`/`.xty`/`.xpj` containers alike -- reached by double-click, by
        the Explorer's right-click "Import…", or by dragging the row onto New
        Bank. All three routes produce the same list of request dicts (see
        ui/dnd.py), so there is one handler rather than one per route.

        The two families keep their own importer and their own naming rules
        (see _run_next_import): a dragged `.xpm` must land exactly as the
        same `.xpm` imported from its context menu does, or the two routes
        would quietly disagree about what a preset is called.

        One shared Convert Options dialog covers the whole batch, exactly as
        a multi-preset "Import via mpc2emu…" does, and the conversions run
        one at a time afterwards: parsing a soundfont holds every one of its
        samples in memory, and doing several at once is how a 1 GB SoundFont
        becomes an out-of-memory kill rather than a slow import.
        """
        self._remember_import(lambda r=list(requests): self._import_requests(r))
        if self._import_worker is not None or self._xpm_import_worker is not None:
            self.statusBar().showMessage("An import is already running")
            return
        requests = [r for r in requests if r.get("path")]
        if not requests:
            return
        first = requests[0]
        all_mpc = all(self._is_mpc_request(r) for r in requests)
        noun = "program" if all_mpc else "instrument"
        source_text = (
            first["path"] if len(requests) == 1 else f"{len(requests)} {noun}s"
        )
        title = (
            f"Import {noun}"
            if len(requests) == 1
            else f"Import {len(requests)} {noun}s"
        )
        # The import-method row only appears when every selected source is
        # the SAME format: the firmware arm is a statement about one
        # device's behaviour with one kind of disc, and a mixed selection
        # has no single answer to make it about.
        source_formats = {foreign_import.format_for(r["path"]) or "" for r in requests}
        source_format = source_formats.pop() if len(source_formats) == 1 else ""
        opts = FormatConvertDialog.get_import_options(
            self,
            title=title,
            source_format=source_format,
            warning_text=None
            if all_mpc
            else (
                "These formats are import sources only — VinSamLib reads "
                "them and writes the hardware bank you choose here, never "
                "the other way round."
            ),
            # The pane's own lock is enforced by greying the picker, the way
            # every other import dialog here does it, rather than by
            # rejecting the drop after the fact.
            locked_format=self._bank_pane.format,
            bank_loader=lambda r=first: (
                xpm_import.load_samples_for_test(r["path"], None, r.get("ordinal"))
                if self._is_mpc_request(r)
                else foreign_import.load_samples_for_test(r["path"], r.get("ordinal"))
            ),
            source_text=source_text,
        )
        opts = self._with_pending_shrink(opts)
        if opts is None:
            return
        self._import_queue = list(requests)
        self._import_opts = opts
        self._import_risks = []
        self._run_next_import()

    def _run_next_import(self) -> None:
        if not self._import_queue:
            risks, self._import_risks = self._import_risks, []
            self._warn_polyphony(risks, "Import")
            return
        request = self._import_queue.pop(0)
        opts = self._import_opts
        path, ordinal = request["path"], request.get("ordinal")
        self.statusBar().showMessage(
            f"Importing {request.get('name') or Path(path).name}…"
        )
        if self._is_mpc_request(request):
            # Deliberately the SAME importer and the same completion handler
            # the MPC context menu uses, not a parallel one: naming an
            # imported program is fiddly (filename for a lone .xpm, program
            # names for a project's rows -- see _on_xpm_imported) and a
            # dragged row must land identically to a right-clicked one.
            w = workers.Worker(
                xpm_import.import_xpm, path, opts, None, self._import_risks, ordinal
            )
            w.signals.finished.connect(
                lambda tmp_path, p=path, o=opts, i=ordinal: self._on_xpm_imported(
                    tmp_path, p, o, None, i
                )
            )
            w.signals.error.connect(self._on_xpm_import_error)
        else:
            w = workers.Worker(
                foreign_import.import_foreign,
                path,
                opts,
                ordinal,
                None,
                self._import_risks,
            )
            w.signals.finished.connect(
                lambda tmp_path, r=request, o=opts: self._on_foreign_imported(
                    tmp_path, r, o
                )
            )
            w.signals.error.connect(self._on_foreign_import_error)
        w.signals.finished.connect(lambda *_: self._advance_import())
        w.signals.error.connect(lambda *_: self._advance_import())
        self._import_worker = w
        workers.run(w)

    def _advance_import(self) -> None:
        self._import_worker = None
        self._run_next_import()

    def _on_foreign_imported(
        self, tmp_path: str, request: dict, opts: convert.ConversionOptions
    ) -> None:
        path, ordinal = request["path"], request.get("ordinal")
        # The `#ordinal` suffix is load-bearing, not cosmetic. Importing one
        # preset of a SoundFont writes a ONE-preset bank, so every preset of
        # the same file comes back as index 0 of the same path -- and
        # BankPane's duplicate check keys on (format, bank.path, index).
        # Without this, importing a second instrument out of one .sf2 would
        # be silently swallowed as a duplicate of the first. Same fix, same
        # reason as the per-program MPC project import above.
        label_path = path if ordinal is None else f"{path}#{ordinal}"
        pairs = self._read_back_converted_presets(tmp_path, opts, label_path=label_path)
        if not pairs:
            return
        stem = Path(path).stem
        if len(pairs) == 1:
            # The row's own label beats the written preset name: the latter
            # is already cut to the 16 characters a hardware name field
            # holds, while the row shows what the instrument is really
            # called.
            names = [_via_mpc2emu(request.get("name") or stem or "Imported instrument")]
        else:
            # A whole multi-preset file, or an SFZ that split into one preset
            # per keyswitch articulation -- the file name is shared by all of
            # them, so their own names are what tell them apart.
            names = [
                _via_mpc2emu(preset.name.strip() or f"{stem} {i + 1}")
                for i, (_bank, preset) in enumerate(pairs)
            ]
        self._bank_pane.add_presets(
            [
                (bank, preset, opts.target_format, name)
                for (bank, preset), name in zip(pairs, names)
            ],  # noqa: B905, RUF100
            # A conversion: mpc2emu's writer has already reported any zone
            # over the rate ceiling, and two boxes about one fact is worse
            # than none.
            #
            # Say WHICH import these are, so the ceiling findings it reports
            # can be attributed to them rather than to whatever happens to be
            # last in the pane. Recorded per add, so a drag-drop landing
            # mid-import cannot join this batch.
            risk_batch=True,
            check_ceiling=False,
        )

    def _on_foreign_import_error(self, message: str) -> None:
        self.statusBar().showMessage(workers.last_error_line(message))

    def _convert_preset_via_mpc2emu(self, nodes: list) -> None:
        """Explorer's right-click "Import via mpc2emu..." on one or more
        real presets (see explorer_pane.py's convertPresetRequested) --
        the same resample/reduce/target-format dialog and pipeline XPM
        import already uses, just starting from already-native preset(s)
        instead of a foreign XPM. Works for E4B, KRZ and EIII sources now
        (mpc2emu's parsers.krz_parser, added 2026-07-27, and
        parsers.eiii_parser, added 2026-07-28, made KRZ/EIII real *input*
        formats too -- see build/convert.py's module docstring).

        A multi-selection shares ONE Convert Options dialog -- the same
        chosen options are applied to every preset in the list, converted
        one at a time (see _run_next_preset_conversion()), not a separate
        dialog per preset."""
        self._remember_import(lambda n=list(nodes): self._convert_preset_via_mpc2emu(n))
        if self._preset_convert_worker is not None:
            self.statusBar().showMessage("A conversion is already running")
            return
        if not nodes:
            return
        # Default the target-format picker to the presets' own shared
        # format if they all agree -- "same format, with options" (the
        # common case: apply resample/reduce without converting) is a
        # better default than always landing on E4B, now that a KRZ
        # source is just as valid a start. A mixed-format selection has
        # no single sensible default, so it falls back to E4B. If New
        # Bank already has a format lock, that takes priority over
        # either (see locked_format below) -- converting to anything
        # else would just be rejected after the fact.
        source_fmts = {n.parent.format_label for n in nodes if n.parent is not None}
        # Kept apart from source_fmt below, which FALLS BACK to "E4B" for a
        # mixed selection. That fallback is a sensible default target; it is
        # not a statement about where the presets came from, and feeding it
        # to the import-method row would claim a shared source format that
        # the selection does not have.
        shared_source_fmt = source_fmts.copy().pop() if len(source_fmts) == 1 else ""
        source_fmt = shared_source_fmt or "E4B"
        title = (
            "Import via mpc2emu"
            if len(nodes) == 1
            else f"Import {len(nodes)} presets via mpc2emu"
        )
        # No path to show here -- the source is a preset (or several) already
        # in the tree, so name it the way the tree does. The bank it sits in
        # is the disambiguating half: preset names repeat across banks, and
        # the title only says how MANY were selected.
        bank_names = {n.parent.label for n in nodes if n.parent is not None}
        in_bank = f" — {bank_names.pop()}" if len(bank_names) == 1 else ""
        source_text = (
            f"{nodes[0].label}{in_bank}"
            if len(nodes) == 1
            else f"{len(nodes)} presets{in_bank}"
        )
        sources = [node.payload for node in nodes]
        opts = FormatConvertDialog.get_import_options(
            self,
            initial=convert.ConversionOptions(target_format=source_fmt or "E4B"),
            title=title,
            source_format=shared_source_fmt,
            # Counted, not assumed: a preset the Detail pane describes as
            # "1 velocity layer" has nothing to split and nothing to reduce,
            # so the dialog does not offer either. None where we cannot count
            # cheaply, and then nothing is hidden on that ground.
            has_velocity_layers=self._selection_has_velocity_layers(nodes),
            warning_text=(
                "Converting goes through mpc2emu's own model, same as any "
                "other conversion here; a few advanced parameters may not "
                "carry over. Resample/reduce below are optional and off "
                "by default for either target format."
            ),
            locked_format=self._bank_pane.format,
            bank_loader=lambda: convert.load_sources_samples_for_test(
                sources, source_fmt or "E4B"
            ),
            source_text=source_text,
        )
        opts = self._with_pending_shrink(opts)
        if opts is None:
            return
        self._preset_convert_queue = list(nodes)
        self._preset_convert_opts = opts
        self._preset_convert_risks = []
        self._run_next_preset_conversion()

    def _run_next_preset_conversion(self) -> None:
        if not self._preset_convert_queue:
            # Queue drained (this is also the path a failed last conversion
            # takes) -- report the whole batch's voice-budget findings once.
            risks, self._preset_convert_risks = self._preset_convert_risks, []
            self._warn_polyphony(risks, "Import via mpc2emu")
            return
        node = self._preset_convert_queue.pop(0)
        opts = self._preset_convert_opts
        bank, preset_obj = node.payload
        self.statusBar().showMessage(f"Converting {node.label}…")
        w = workers.Worker(
            convert.convert_preset, bank, preset_obj, opts, self._preset_convert_risks
        )
        w.signals.finished.connect(
            lambda tmp_path, n=node, o=opts: self._on_preset_converted(tmp_path, n, o)
        )
        w.signals.error.connect(self._on_preset_convert_error)
        w.signals.finished.connect(lambda *_: self._advance_preset_conversion())
        w.signals.error.connect(lambda *_: self._advance_preset_conversion())
        self._preset_convert_worker = w
        workers.run(w)

    def _advance_preset_conversion(self) -> None:
        self._preset_convert_worker = None
        self._run_next_preset_conversion()

    def _on_preset_converted(
        self, tmp_path: str, node, opts: convert.ConversionOptions
    ) -> None:
        # Fresh temp-path label (NOT the source preset's own bank.path) --
        # unlike XPM's static source file, the *options* chosen here are
        # part of what makes this result distinct: converting the same
        # source preset twice with different resample/reduce choices must
        # not be deduped against each other, only an identical repeat
        # should be. Using the source identity would incorrectly conflate
        # those; a fresh identity per conversion is the safer default.
        result = self._read_back_converted(tmp_path, opts, label_path=tmp_path)
        if result is None:
            return
        bank, preset = result
        name = self._bank_pane.unique_name(_via_mpc2emu(node.label))
        self._bank_pane.add_presets(
            [(bank, preset, opts.target_format, name)],
            # Say WHICH import these are, so the
            # ceiling findings it reports can be
            # attributed to them. Recorded per add,
            # so a drag-drop landing mid-import
            # cannot join this batch.
            risk_batch=True,
            check_ceiling=False,
        )

    def _on_preset_convert_error(self, message: str) -> None:
        last_line = workers.last_error_line(message)
        self.statusBar().showMessage(f"Conversion failed: {last_line}", 8000)
        QMessageBox.warning(
            self, "Import via mpc2emu", f"Conversion failed:\n\n{last_line}"
        )

    def _toggle_samples_column(self, checked: bool) -> None:
        self._samples.setVisible(checked)
        self.statusBar().showMessage(
            "Samples column shown" if checked else "Samples column hidden"
        )

    def _toggle_dupe_check(self, checked: bool) -> None:
        self._bank_pane.set_dedupe_enabled(checked)
        self.statusBar().showMessage(
            "Duplicate preset check enabled"
            if checked
            else "Duplicate preset check disabled"
        )

    def _toggle_dupe_prompt(self, checked: bool) -> None:
        self._bank_pane.set_prompt_on_duplicate(checked)
        self.statusBar().showMessage(
            "Duplicates will prompt before being skipped"
            if checked
            else "Duplicates will be skipped silently"
        )

    # -- crash safety ---------------------------------------------------------

    def _start_autosave(self) -> None:
        """Arm the recovery timer, or leave it off when the interval is 0."""
        seconds = int(getattr(self._config, "autosave_seconds", 60) or 0)
        if not hasattr(self, "_autosave_timer"):
            self._autosave_timer = QTimer(self)
            self._autosave_timer.timeout.connect(self._autosave_tick)
        self._autosave_timer.stop()
        if seconds > 0:
            self._autosave_timer.start(seconds * 1000)

    def _autosave_tick(self) -> None:
        """Write the recovery file, off the GUI thread.

        SNAPSHOTTED HERE, WRITTEN THERE. The lists are copied on this thread
        -- shallow copies of tuples and dicts, microseconds -- and the worker
        then reads only the bank objects, which nothing mutates in place. A
        save that walked self._items directly would be reading a list the
        user can reorder from under it.

        Skipped entirely when nothing is staged, and when one is already in
        flight: a project carrying converted banks writes real megabytes, and
        stacking those up behind a slow disk would turn a safety net into the
        thing making the program slow.
        """
        if getattr(self, "_autosave_busy", False):
            return
        bp, pp = self._bank_pane, self._pending_pane
        if not bp._items and not pp._pending:
            # Nothing staged: drop any stale recovery file rather than leave
            # one that would offer an empty project back after the next crash.
            project.clear_autosave()
            return
        args = dict(  # noqa: C408
            bank_items=list(bp._items),
            bank_format=bp.format,
            bank_name=bp._name_edit.text(),
            sample_renames=dict(bp._sample_renames),
            # The placement/velocity maps ride on the items now, and
            # `_items_json` writes them there. These two stay for the v1
            # bank-wide slot, which nothing writes any more.
            zone_placement={},
            voice_velocity={},
            pending=[dict(e) for e in pp._pending],
            partition_breaks=set(pp._partition_breaks),
            image=self._image_state(),
            explorer=self._explorer.view_state(),
        )
        self._autosave_busy = True

        def _write():
            target = project.autosave_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            return project.save(str(target), **args)

        w = workers.Worker(_write)
        w.signals.finished.connect(lambda *_: setattr(self, "_autosave_busy", False))
        w.signals.error.connect(
            lambda msg: (
                setattr(self, "_autosave_busy", False),
                self.statusBar().showMessage(
                    f"Autosave failed: " f"{workers.last_error_line(msg)}", 8000
                ),
            )
        )
        workers.run(w)

    def _offer_recovery(self) -> None:
        """A recovery file at startup means the last run did not finish."""
        path = project.autosave_path()
        if not path.exists():
            return
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        answer = QMessageBox.question(
            self,
            "Recover Unsaved Work",
            f"VinSamLib did not shut down cleanly last time.\n\n"
            f"There is staged work from {when}. Load it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            # Kept, not deleted. Saying "no" once -- perhaps by reflex on a
            # dialog that appeared during startup -- must not be what destroys
            # the only copy of an evening's work. It is replaced by the next
            # autosave and removed by the next clean exit.
            self.statusBar().showMessage(
                f"Recovery file kept at {path} — it will be replaced by the "
                f"next autosave",
                12000,
            )
            return
        try:
            rep = project.load(str(path))
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(
                self,
                "Recover Unsaved Work",
                f"The recovery file could not be read:\n\n{ex}",
            )
            return
        self._apply_loaded_project(rep, str(path))

    # -- project save / load ------------------------------------------------

    def _save_project(self) -> None:
        """Write everything staged to one file, so the work survives a quit."""
        bp, pp = self._bank_pane, self._pending_pane
        if not bp._items and not pp._pending:
            self.statusBar().showMessage("Nothing staged to save", 6000)
            return
        start = str(self._config.last_image_dir or Path.home())
        path, _f = QFileDialog.getSaveFileName(
            self,
            "Save Project",
            str(Path(start) / f"Untitled{project.SUFFIX}"),
            f"VinSamLib projects (*{project.SUFFIX})",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not path:
            return
        try:
            summary = project.save(
                path,
                bank_items=list(bp._items),
                bank_format=bp.format,
                bank_name=bp._name_edit.text(),
                sample_renames=dict(bp._sample_renames),
                zone_placement={},
                voice_velocity={},
                pending=list(pp._pending),
                partition_breaks=set(pp._partition_breaks),
                image=self._image_state(),
                explorer=self._explorer.view_state(),
            )
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(self, "Save Project", f"Could not save:\n\n{ex}")
            return
        self.statusBar().showMessage(summary, 10000)

    def _load_project(self) -> None:
        """Read one back, replacing what is staged now.

        Asks first when there is work to lose: loading is not an import, it
        REPLACES both columns, and doing that silently to a queue somebody
        spent an evening on is not recoverable.
        """
        bp, pp = self._bank_pane, self._pending_pane
        if (bp._items or pp._pending) and QMessageBox.question(
            self,
            "Load Project",
            "Loading replaces what is in New Bank and Pending for Image. " "Continue?",
        ) != QMessageBox.StandardButton.Yes:
            return
        start = str(self._config.last_image_dir or Path.home())
        path, _f = QFileDialog.getOpenFileName(
            self,
            "Load Project",
            start,
            f"VinSamLib projects (*{project.SUFFIX})",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not path:
            return
        try:
            rep = project.load(path)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(self, "Load Project", f"Could not load:\n\n{ex}")
            return
        self._apply_loaded_project(rep, path)

    def _image_state(self) -> Optional[dict]:  # noqa: UP045
        """Which image the Image column has open, if any."""
        path = getattr(self._image_pane, "_path", None)
        if not path:
            return None
        return {"path": str(path), "kind": getattr(self._image_pane, "_kind", None)}

    def _apply_loaded_project(self, rep, path: str) -> None:  # noqa: C901, RUF100
        """Put a loaded project into the panes. Shared with crash recovery,
        which has to land in exactly the same state a manual load does."""
        bp, pp = self._bank_pane, self._pending_pane
        bp._clear()
        if rep.banks:
            bp.add_presets(
                [(b, p, rep.bank_format or "", n) for b, p, n, *_e in rep.banks],
                restoring=True,
            )
        # AFTER the presets, not before: add_presets() rewrites the name field
        # for a freshly-locked bank, so setting it first was silently undone.
        bp._name_edit.setText(rep.bank_name)
        bp._sample_renames = dict(rep.sample_renames)
        # Per item, from the restored rows; a v1 file's bank-wide maps are
        # applied to EVERY item, because that is what they meant.
        if len(bp._items) != len(rep.banks):
            # The zip below pairs a restored row with a staged item BY
            # POSITION. `add_presets(restoring=True)` appends one-for-one and
            # skips no dedupe, so they are the same length -- but that is an
            # invariant of a different function, and if it ever stops holding,
            # every edit lands on the WRONG preset with no error anywhere.
            rep.problems.append(
                f"{len(rep.banks)} preset(s) in the project restored as "
                f"{len(bp._items)}; their placement edits were left off "
                f"rather than guessed at."
            )
        else:
            for row, item in zip(rep.banks, bp._items):  # noqa: B905, RUF100
                edits = item[3]
                src = row[3] if len(row) > 3 else {}
                edits.setdefault("placement", {}).update(src.get("placement") or {})
                edits.setdefault("velocity", {}).update(src.get("velocity") or {})
                if rep.zone_placement:
                    edits["placement"].update(rep.zone_placement)
                if rep.voice_velocity:
                    edits["velocity"].update(rep.voice_velocity)
        bp._refresh()
        pp._pending = list(rep.pending)
        pp._format = rep.pending[0]["format"] if rep.pending else None
        pp._partition_breaks = set(rep.partition_breaks)
        pp._refresh()
        # The desk, after the work: the disc that was open and the folders
        # that were unfolded. Both are best-effort and neither is allowed to
        # stop a load -- an image that has since been deleted is a note in
        # the problems list, not a refusal to restore the queue that was
        # going to be written to it.
        if rep.image and rep.image.get("path"):
            img = Path(rep.image["path"])
            if img.exists():
                self._image_pane._open_image(str(img), known_kind=rep.image.get("kind"))
            else:
                rep.problems.append(f"The image that was open, {img}, is gone.")
        if rep.explorer:
            self._explorer.restore_view_state(rep.explorer)
        loaded = (
            f"Loaded {Path(path).name}: {len(rep.banks)} preset(s) in "
            f"New Bank, {len(rep.pending)} bank(s) pending"
        )
        if getattr(rep, "call_log_lines", 0):
            # Worth saying out loud: someone who turned the switch on to
            # capture a problem needs to know the capture is in the file
            # they just opened, and the file is where they will look.
            loaded += (
                f" — carries a debug log of " f"{rep.call_log_lines} mpc2emu call(s)"
            )
        self.statusBar().showMessage(loaded, 10000)
        if rep.problems:
            # Never a silent partial load: what did not come back is the half
            # the user has to act on, and it scrolls away in a status bar.
            QMessageBox.warning(
                self,
                "Loaded With Problems",
                loaded
                + "\n\n"
                + "\n\n".join(rep.problems[:12])
                + (
                    ""
                    if len(rep.problems) <= 12
                    else f"\n\n… and {len(rep.problems) - 12} more."
                ),
            )

    def _remember_import(self, again) -> None:
        """Keep how to run the most recent import again, with its options.

        Stored as a no-argument callable rather than as arguments: the four
        import routes take different ones, and the pane offering the button
        must not have to know which route it was.
        """
        self._last_import = again
        self._bank_pane._can_redo_import = again is not None

    def _redo_last_import(self) -> None:
        """ "Change Import Settings…" from the over-limit dialog.

        The add has already been undone by the pane, so this reopens the
        import exactly as it was asked for the first time -- same source,
        same dialog, pre-filled with the options that produced a bank too
        big. Changing "Reduce Sample Count" or the stereo method and pressing
        OK is then one gesture away, which is the whole point: the previous
        behaviour offered only "undo" or "keep", and both leave the user to
        find their way back to the dialog themselves.
        """
        again = getattr(self, "_last_import", None)
        if again is None:
            self.statusBar().showMessage(
                "Nothing to re-import — this bank was not filled by an import", 6000
            )
            return
        again()

    def _add_node_to_bank(self, nodes: list) -> None:
        # The Explorer's right-click "Add to New Bank" (now multi-select
        # aware) — the in-process equivalent of dragging the same preset
        # row(s) onto the New Bank column, for anyone who'd rather click
        # than drag.
        items = []
        for node in nodes:
            bank, preset_obj = node.payload
            fmt = node.parent.format_label if node.parent else ""
            items.append((bank, preset_obj, fmt, node.label))
        self._bank_pane.add_presets(items)

    def _add_favourites(self, node) -> None:
        """Explorer right-click on a BANK row: paste the numbers noted on the
        hardware and add exactly those presets.

        The bank is chosen by the click rather than matched by name, which is
        deliberate -- the spreadsheet heading and the bank on the media do not
        have to agree, and in the case that prompted this they did not.
        """
        if node.handle is not None:
            self._open_favourites(node)
            return
        # OFF THE UI THREAD. A collapsed row has not been read yet, and the
        # first version parsed it inline behind a wait cursor. On its own that
        # is about half a second -- but the read competes with the background
        # library scan for the same disk, and inline it froze the window for
        # minutes showing nothing but "Reading ..." in the status bar. Every
        # other bank read in this program goes through a worker for exactly
        # this reason; this one had no business being the exception.
        if self._favourites_worker is not None:
            self.statusBar().showMessage("Still reading the last bank…", 4000)
            return
        self.statusBar().showMessage(f"Reading {node.label}…")
        w = workers.Worker(models.parse_bank_node, node)
        w.signals.finished.connect(lambda _ok, n=node: self._on_favourites_read(n))
        w.signals.error.connect(lambda msg, n=node: self._on_favourites_error(n, msg))
        self._favourites_worker = w
        workers.run(w)

    def _on_favourites_read(self, node) -> None:
        self._favourites_worker = None
        self.statusBar().clearMessage()
        self._open_favourites(node)

    def _on_favourites_error(self, node, message: str) -> None:
        self._favourites_worker = None
        last = workers.last_error_line(message)
        self.statusBar().showMessage(f"Could not read {node.label}: {last}", 8000)
        QMessageBox.warning(
            self, "Add Favourites", f'Could not read "{node.label}":\n\n{last}'
        )

    def _open_favourites(self, node) -> None:
        bank = node.handle
        # models.bank_presets, never `bank.presets`: a KRZ keeps its programs
        # in a dict and has no `presets` at all, so the attribute lookup came
        # back empty and the action quietly did nothing on the very format
        # these lists are mostly written for.
        presets = models.bank_presets(bank) if bank is not None else []
        if not presets:
            QMessageBox.warning(
                self,
                "Add Favourites",
                f'Could not read any presets from "{node.label}".'
                + (f"\n\n{node.error}" if node.error else ""),
            )
            return
        fmt = node.format_label or ""
        names = [
            (getattr(p, "name", "") or "").strip() or f"preset {i}"
            for i, p in enumerate(presets)
        ]
        dialog = FavouritesDialog(node.label, fmt, names, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        items = [(bank, presets[i], fmt, names[i]) for i in dialog.positions()]
        if self._bank_pane.add_presets(items):
            self.statusBar().showMessage(
                f"Added {len(items)} favourite(s) from {node.label}", 6000
            )

    def _show_about(self) -> None:
        from ..audition import render as _render_mod  # noqa: PLC0415, RUF100

        QMessageBox.about(
            self,
            "About VinSamLib",
            "VinSamLib — a librarian for E-mu E4B/EIII and Kurzweil KRZ sample banks.\n\n"
            "Built on mpc2emu's format-writing code, with its own read path "
            "for EMU3/FAT images and E4B/KRZ/EIII banks.\n\n"
            f"Audition renderer: {_render_mod.renderer_description()}.\n"
            f"Events render in parallel: "
            f"{'yes' if _render_mod.parallel_supported() else 'no'} "
            f"({os.cpu_count()} cores).",
        )

    # -- background indexing ---------------------------------------------------

    def _start_scan(self, roots: list[Path]) -> None:
        if not roots or self._scan_worker is not None:
            return
        self.statusBar().showMessage(
            f"Scanning {len(roots)} librar{'y' if len(roots)==1 else 'ies'}…"
        )
        w = workers.Worker(self._run_scan, roots)
        w.signals.finished.connect(self._on_scan_finished)
        w.signals.error.connect(self._on_scan_error)
        self._scan_worker = w
        workers.run(w)

    def _run_scan(self, roots: list[Path]) -> dict:
        # Runs on a worker thread: Python's sqlite3 connections are bound
        # to the thread that created them, so this opens its own
        # connection to the same database file rather than touching
        # self._index_db (which stays on the GUI thread for search()) —
        # SQLite itself handles one writer + concurrent readers on the
        # same file safely; sharing one Python Connection object across
        # threads is what isn't safe.
        scan_db = IndexDB(user_data_dir() / "index.db")
        try:
            scan(roots, scan_db)
            return scan_db.stats()
        finally:
            scan_db.close()

    def _on_scan_finished(self, stats: dict) -> None:
        self._scan_worker = None
        self.statusBar().showMessage(
            f"Indexed {stats['containers']} file(s), {stats['items']} item(s)", 8000
        )

    def _on_scan_error(self, message: str) -> None:
        self._scan_worker = None
        last_line = workers.last_error_line(message)
        self.statusBar().showMessage(f"Scan failed: {last_line}", 8000)


def _program_number(preset_index: Optional[int], position: int) -> int:  # noqa: UP045
    """1-based label for a program whose own name is empty: its number in
    the project when one program was picked out, its position in the batch
    when the whole project came in at once."""
    return (position if preset_index is None else preset_index) + 1
