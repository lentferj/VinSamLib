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

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMainWindow, QMessageBox, QSplitter

from . import workers
from .bank_pane import BankPane
from .explorer_pane import ExplorerPane
from .image_pane import ImagePane
from .models import LibraryTreeModel
from .pending_pane import PendingBanksPane
from .samples_pane import SamplesPane
from .settings_dialog import SettingsDialog
from .xpm_import_dialog import XpmImportDialog
from ..banks import e4b, krz
from ..build import convert, xpm_import
from ..config import Config, user_data_dir
from ..index.db import IndexDB
from ..index.scanner import scan


class MainWindow(QMainWindow):
    def __init__(self, config: Config):
        super().__init__()
        self._config = config
        self.setWindowTitle("VinSamLib")
        self.resize(1280, 800)

        self._index_db = IndexDB(user_data_dir() / "index.db")
        self._scan_worker: workers.Worker | None = None
        self._xpm_import_worker: workers.Worker | None = None
        self._preset_convert_worker: workers.Worker | None = None
        self._preset_convert_queue: list = []
        self._preset_convert_opts: Optional[convert.ConversionOptions] = None

        self._model = LibraryTreeModel(list(config.library_roots))

        self._explorer = ExplorerPane(self._model, self._index_db)
        self._samples = SamplesPane()
        self._samples.setVisible(False)
        self._explorer.selectionChanged.connect(self._samples.show_node)

        self._bank_pane = BankPane(self._config)
        self._bank_pane.statusMessage.connect(lambda msg: self.statusBar().showMessage(msg, 6000))
        self._explorer.addToBankRequested.connect(self._add_node_to_bank)
        self._explorer.importXpmRequested.connect(self._import_xpm)
        self._explorer.convertPresetRequested.connect(self._convert_preset_via_mpc2emu)
        self._explorer.removeLibraryRootRequested.connect(self._remove_library_root)

        self._pending_pane = PendingBanksPane()
        self._pending_pane.statusMessage.connect(lambda msg: self.statusBar().showMessage(msg, 6000))
        self._bank_pane.sendToPendingRequested.connect(self._pending_pane.add_pending)
        self._pending_pane.moveToNewBankRequested.connect(self._bank_pane.load_pending)

        self._image_pane = ImagePane(config)
        self._image_pane.statusMessage.connect(lambda msg: self.statusBar().showMessage(msg, 6000))
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
                "Add a library folder to get started — File ▸ Add Library Folder…")
        else:
            self.statusBar().showMessage("Ready")
            self._start_scan(list(config.library_roots))

    def closeEvent(self, event) -> None:
        # Give in-flight background workers (tree fetches, a scan) a bounded
        # window to finish before the widget tree they'd signal back into
        # starts getting torn down — under PySide6/Shiboken, a worker still
        # mid-flight at that point raises a hard "Signal source has been
        # deleted" RuntimeError from its own background thread (PyQt5
        # tolerated the same race silently). Bounded rather than unbounded
        # so quitting never hangs on a slow scan.
        QThreadPool.globalInstance().waitForDone(3000)
        self._index_db.close()
        super().closeEvent(event)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        add_action = QAction("Add Library Folder…", self)
        add_action.triggered.connect(self._add_library_folder)
        file_menu.addAction(add_action)

        remove_action = QAction("Remove Library Folder…", self)
        remove_action.triggered.connect(lambda: self._remove_library_folder())
        file_menu.addAction(remove_action)

        rescan_action = QAction("Rescan Library", self)
        rescan_action.triggered.connect(lambda: self._start_scan(list(self._config.library_roots)))
        file_menu.addAction(rescan_action)

        file_menu.addSeparator()

        import_xpm_action = QAction("Import XPM…", self)
        import_xpm_action.triggered.connect(lambda: self._import_xpm())
        xpm_ok, xpm_reason = self._config.check_xpm_import_support()
        import_xpm_action.setEnabled(xpm_ok)
        import_xpm_action.setToolTip(
            xpm_reason if xpm_ok else f"Unavailable: {xpm_reason}")
        file_menu.addAction(import_xpm_action)

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

        dupe_prompt_action = QAction("Prompt Before Skipping Duplicates", self, checkable=True)
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

    def _add_library_folder(self) -> None:
        start_dir = str(self._config.last_library_dir) if self._config.last_library_dir else ""
        path = QFileDialog.getExistingDirectory(
            self, "Add Library Folder", start_dir,
            options=QFileDialog.Option.DontUseNativeDialog)
        if not path:
            return
        p = Path(path)
        if p in self._config.library_roots:
            self.statusBar().showMessage(f"{p} is already in the library")
            return
        self._config.library_roots.append(p)
        self._config.last_library_dir = p.parent
        self._config.save()
        self._model.add_root(p)
        self._start_scan([p])

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
            self, "Remove Library Folder", "Folder to remove:", items, 0, False)
        if not ok or not choice:
            return
        self._remove_library_root(Path(choice))

    def _remove_library_root(self, path: Path) -> None:
        if QMessageBox.question(
                self, "Remove Library Folder",
                f"Remove {path} from your library?\n\n"
                "This only stops VinSamLib from tracking it -- no files on disk "
                "are touched, and you can add it back any time.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self._config.library_roots.remove(path)
        self._config.save()
        self._model.remove_root(path)
        self._index_db.forget_containers_under(str(path))
        self.statusBar().showMessage(f"Removed {path} from your library", 6000)

    def _show_settings(self) -> None:
        dialog = SettingsDialog(self._config, self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            self._bank_pane.refresh_size_limits()
            if dialog.path_changed:
                self.statusBar().showMessage(
                    "mpc2emu path updated — restart VinSamLib to apply", 8000)

    # -- XPM import ---------------------------------------------------------------

    def _import_xpm(self, path: Optional[str] = None) -> None:
        """path: pre-chosen (e.g. Explorer's "Import…" on a .xpm row/hit --
        see importXpmRequested) or None to prompt with a file picker
        (File > Import XPM…)."""
        if self._xpm_import_worker is not None:
            self.statusBar().showMessage("An XPM import is already running")
            return
        if not path:
            path, _filter = QFileDialog.getOpenFileName(
                self, "Import XPM", "", "Akai XPM programs (*.xpm)",
                options=QFileDialog.Option.DontUseNativeDialog)
            if not path:
                return
        opts = XpmImportDialog.get_import_options(self, locked_format=self._bank_pane.format)
        if opts is None:
            return
        self.statusBar().showMessage(f"Importing {Path(path).name}…")
        w = workers.Worker(xpm_import.import_xpm, path, opts)
        w.signals.finished.connect(lambda tmp_path, p=path: self._on_xpm_imported(tmp_path, p, opts))
        w.signals.error.connect(self._on_xpm_import_error)
        w.signals.finished.connect(lambda *_: setattr(self, "_xpm_import_worker", None))
        w.signals.error.connect(lambda *_: setattr(self, "_xpm_import_worker", None))
        self._xpm_import_worker = w
        workers.run(w)

    def _on_xpm_imported(self, tmp_path: str, xpm_path: str,
                          opts: convert.ConversionOptions) -> None:
        # No save dialog, no library folder at all -- an XPM always holds
        # exactly one preset (mpc2emu's own parse_xpm() appends exactly
        # one Preset, never more; see its docstring), so it belongs in New
        # Bank as a single program/preset, the same as dragging one preset
        # in from Explorer -- not a whole one-preset "bank" of its own in
        # Pending for Image.
        #
        # Read the just-converted temp file's bytes but label the result
        # with the ORIGINAL xpm_path, not the (freshly, uniquely,
        # per-import) generated temp path -- BankPane's duplicate check
        # keys on bank.path + preset index/id (see bank_pane.py's
        # _preset_key()), and every import of the *same* source XPM
        # should be recognized as the same preset, not a new one each time
        # just because its throwaway temp file happened to land somewhere
        # else. index/id are already stable for a fresh single-preset
        # bank, so this is the only piece that needed fixing.
        result = self._read_back_converted(tmp_path, opts, label_path=xpm_path)
        if result is None:
            return
        bank, preset = result
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
        name = Path(xpm_path).stem or preset.name.strip() or "Imported XPM"
        self._bank_pane.add_presets([(bank, preset, opts.target_format, name)])

    def _on_xpm_import_error(self, message: str) -> None:
        last_line = message.strip().splitlines()[-1] if message else "error"
        self.statusBar().showMessage(f"XPM import failed: {last_line}", 8000)
        QMessageBox.warning(self, "Import XPM", f"Import failed:\n\n{last_line}")

    def _read_back_converted(self, tmp_path: str, opts: convert.ConversionOptions,
                              label_path: str) -> Optional[tuple]:
        """Shared by _on_xpm_imported() and _on_preset_converted(): both
        hand mpc2emu's freshly-written temp file back to VinSamLib's OWN
        parser (never mpc2emu's) so what lands in New Bank is a normal,
        byte-verbatim VinSamLib bank/preset pair like any other -- and
        both label the re-parse with a caller-chosen stable path rather
        than the throwaway temp path, so BankPane's duplicate check
        (bank.path + preset index/id, see bank_pane.py's _preset_key())
        keeps working. Returns (bank, preset) or None after showing a
        warning on failure."""
        try:
            data = Path(tmp_path).read_bytes()
            if opts.target_format == "KRZ":
                bank = krz.parse_bytes(data, label_path)
                preset = next(iter(bank.programs.values()))
            else:
                bank = e4b.parse_bytes(data, label_path)
                preset = bank.presets[0]
            return bank, preset
        except Exception as ex:
            QMessageBox.warning(
                self, "Import via mpc2emu",
                f"Couldn't read back the converted bank:\n\n{ex}")
            return None

    # -- convert an existing E4B preset via mpc2emu --------------------------------

    def _convert_preset_via_mpc2emu(self, nodes: list) -> None:
        """Explorer's right-click "Import via mpc2emu..." on one or more
        real presets (see explorer_pane.py's convertPresetRequested) --
        the same resample/reduce/target-format dialog and pipeline XPM
        import already uses, just starting from already-native preset(s)
        instead of a foreign XPM. Works for both E4B and KRZ sources now
        (mpc2emu's parsers.krz_parser, added 2026-07-27, made KRZ a real
        *input* format too -- see build/convert.py's module docstring).

        A multi-selection shares ONE Convert Options dialog -- the same
        chosen options are applied to every preset in the list, converted
        one at a time (see _run_next_preset_conversion()), not a separate
        dialog per preset."""
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
        source_fmt = source_fmts.pop() if len(source_fmts) == 1 else "E4B"
        title = "Import via mpc2emu" if len(nodes) == 1 \
            else f"Import {len(nodes)} presets via mpc2emu"
        opts = XpmImportDialog.get_import_options(
            self, initial=convert.ConversionOptions(target_format=source_fmt or "E4B"),
            title=title,
            warning_text=(
                "Converting goes through mpc2emu's own model, same as any "
                "other conversion here; a few advanced parameters may not "
                "carry over. Resample/reduce below are optional and off "
                "by default for either target format."),
            locked_format=self._bank_pane.format)
        if opts is None:
            return
        self._preset_convert_queue = list(nodes)
        self._preset_convert_opts = opts
        self._run_next_preset_conversion()

    def _run_next_preset_conversion(self) -> None:
        if not self._preset_convert_queue:
            return
        node = self._preset_convert_queue.pop(0)
        opts = self._preset_convert_opts
        bank, preset_obj = node.payload
        self.statusBar().showMessage(f"Converting {node.label}…")
        w = workers.Worker(convert.convert_preset, bank, preset_obj, opts)
        w.signals.finished.connect(
            lambda tmp_path, n=node, o=opts: self._on_preset_converted(tmp_path, n, o))
        w.signals.error.connect(self._on_preset_convert_error)
        w.signals.finished.connect(lambda *_: self._advance_preset_conversion())
        w.signals.error.connect(lambda *_: self._advance_preset_conversion())
        self._preset_convert_worker = w
        workers.run(w)

    def _advance_preset_conversion(self) -> None:
        self._preset_convert_worker = None
        self._run_next_preset_conversion()

    def _on_preset_converted(self, tmp_path: str, node, opts: convert.ConversionOptions) -> None:
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
        name = self._bank_pane.unique_name(f"{node.label} (mpc2emu)")
        self._bank_pane.add_presets([(bank, preset, opts.target_format, name)])

    def _on_preset_convert_error(self, message: str) -> None:
        last_line = message.strip().splitlines()[-1] if message else "error"
        self.statusBar().showMessage(f"Conversion failed: {last_line}", 8000)
        QMessageBox.warning(self, "Import via mpc2emu", f"Conversion failed:\n\n{last_line}")

    def _toggle_samples_column(self, checked: bool) -> None:
        self._samples.setVisible(checked)
        self.statusBar().showMessage("Samples column shown" if checked else "Samples column hidden")

    def _toggle_dupe_check(self, checked: bool) -> None:
        self._bank_pane.set_dedupe_enabled(checked)
        self.statusBar().showMessage(
            "Duplicate preset check enabled" if checked else "Duplicate preset check disabled")

    def _toggle_dupe_prompt(self, checked: bool) -> None:
        self._bank_pane.set_prompt_on_duplicate(checked)
        self.statusBar().showMessage(
            "Duplicates will prompt before being skipped" if checked
            else "Duplicates will be skipped silently")

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

    def _show_about(self) -> None:
        QMessageBox.about(
            self, "About VinSamLib",
            "VinSamLib — a librarian for E-mu E4B and Kurzweil KRZ sample banks.\n\n"
            "Built on mpc2emu's format-writing code, with its own read path "
            "for EMU3/FAT images and E4B/KRZ banks.",
        )

    # -- background indexing ---------------------------------------------------

    def _start_scan(self, roots: list[Path]) -> None:
        if not roots or self._scan_worker is not None:
            return
        self.statusBar().showMessage(f"Scanning {len(roots)} librar{'y' if len(roots)==1 else 'ies'}…")
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
            f"Indexed {stats['containers']} file(s), {stats['items']} item(s)", 8000)

    def _on_scan_error(self, message: str) -> None:
        self._scan_worker = None
        last_line = message.strip().splitlines()[-1] if message else "error"
        self.statusBar().showMessage(f"Scan failed: {last_line}", 8000)
