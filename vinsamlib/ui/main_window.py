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

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFileDialog, QMainWindow, QMessageBox, QSplitter

from . import workers
from .bank_pane import BankPane
from .explorer_pane import ExplorerPane
from .image_pane import ImagePane
from .models import LibraryTreeModel
from .pending_pane import PendingBanksPane
from .samples_pane import SamplesPane
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

        self._model = LibraryTreeModel(list(config.library_roots))

        self._explorer = ExplorerPane(self._model, self._index_db)
        self._samples = SamplesPane()
        self._samples.setVisible(False)
        self._explorer.selectionChanged.connect(self._samples.show_node)

        self._bank_pane = BankPane()
        self._bank_pane.statusMessage.connect(lambda msg: self.statusBar().showMessage(msg, 6000))
        self._explorer.addToBankRequested.connect(self._add_node_to_bank)

        self._pending_pane = PendingBanksPane()
        self._pending_pane.statusMessage.connect(lambda msg: self.statusBar().showMessage(msg, 6000))
        self._bank_pane.sendToPendingRequested.connect(self._pending_pane.add_pending)
        self._pending_pane.moveToNewBankRequested.connect(self._bank_pane.load_pending)

        self._image_pane = ImagePane(config)
        self._image_pane.statusMessage.connect(lambda msg: self.statusBar().showMessage(msg, 6000))
        self._pending_pane.buildRequested.connect(self._image_pane.receive_bank_files)

        splitter = QSplitter(Qt.Orientation.Horizontal)
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

        rescan_action = QAction("Rescan Library", self)
        rescan_action.triggered.connect(lambda: self._start_scan(list(self._config.library_roots)))
        file_menu.addAction(rescan_action)

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
        path = QFileDialog.getExistingDirectory(self, "Add Library Folder")
        if not path:
            return
        p = Path(path)
        if p in self._config.library_roots:
            self.statusBar().showMessage(f"{p} is already in the library")
            return
        self._config.library_roots.append(p)
        self._config.save()
        self._model.add_root(p)
        self._start_scan([p])

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
