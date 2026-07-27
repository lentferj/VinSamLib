"""
Pending for Image column: a staging queue between New Bank and Image.
"Send to Image Column" in the New Bank column no longer writes anything to
disk immediately — it hands over a named bank recipe (the same (bank,
preset_obj, name) items New Bank itself works with) which lands here as one
row. Nothing touches a real image file until "Build Image →" is clicked,
which assembles every pending bank (in the order shown) to temp files and
hands that list to the Image column in one go.

This is what makes bank order controllable at all: the Image column's own
append operations write in whatever order they're called, and reordering
banks already committed to a real file would mean rebuilding its directory
structure — reordering here, before anything is written, is what "which
bank ends up where on the image" actually means for a fresh build or a
fresh batch of appends.

Locks to one bank format (E4B or KRZ) on first content, same rule as New
Bank and Image — a pending queue mixing formats could never become one
real image anyway.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QInputDialog,
                             QLabel, QListWidget, QListWidgetItem, QMenu,
                             QPushButton, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from . import workers
from .bank_pane import _FORMAT_EXT, _sanitize_bank_name
from .convert_options_dialog import ConvertOptionsDialog
from ..banks import e4b, krz
from ..build.convert import apply_conversion

_PENDING_TEMP_PREFIX = "vinsamlib_pending_"


def _assemble_all(pending: list[dict]) -> list[str]:
    """Runs off the GUI thread: assemble every pending bank's real bytes
    and write each to its own throwaway temp file, in order. Raising here
    (e.g. one bank's selection no longer resolves) aborts the whole build
    rather than handing the Image column a partial, out-of-order set.

    Each entry carries its own "convert_opts" (per-bank, not global --
    see pending_pane.py's _show_convert_options()); when set, it's run
    through build/convert.py's own parse -> Bank -> resample/reduce ->
    write round trip on top of the just-assembled E4B temp file,
    replacing it with the processed version before it's handed onward
    -- never applied to KRZ (mpc2emu has no .krz *input* parser, so
    there is no round trip possible)."""
    paths: list[str] = []
    for entry in pending:
        fmt = entry["format"]
        fn = e4b.assemble if fmt == "E4B" else krz.assemble
        selections = [(bank, preset) for bank, preset, _name in entry["items"]]
        data = fn(selections)
        ext = _FORMAT_EXT[fmt]
        name = _sanitize_bank_name(entry["name"])
        tmp_dir = Path(tempfile.mkdtemp(prefix=_PENDING_TEMP_PREFIX))
        tmp_path = tmp_dir / f"{name}.{ext}"
        tmp_path.write_bytes(data)
        final_path = str(tmp_path)
        convert_opts = entry.get("convert_opts")
        if fmt == "E4B" and convert_opts is not None:
            final_path = apply_conversion(final_path, convert_opts)
        paths.append(final_path)
    return paths


class PendingBanksPane(QWidget):
    statusMessage = Signal(str)
    moveToNewBankRequested = Signal(str, str, list)   # (name, format, items)
    buildRequested = Signal(list, str)                # (temp_file_paths, format)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._pending: list[dict[str, Any]] = []
        self._format: Optional[str] = None
        self._live_workers: list[workers.Worker] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._head = QLabel("Pending for Image")
        self._head.setStyleSheet("font-weight: 600; padding: 6px 10px;"
                                  "border-bottom: 1px solid palette(mid);")
        layout.addWidget(self._head)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)
        self._stack.addWidget(self._build_empty_page())
        self._stack.addWidget(self._build_filled_page())
        self._stack.setCurrentIndex(0)

    def _build_empty_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(10, 10, 10, 10)
        box = QFrame()
        box.setStyleSheet("QFrame { border: 1px dashed palette(mid); border-radius: 6px; }")
        box_layout = QVBoxLayout(box)
        box_layout.addStretch()
        hint = QLabel("Banks sent from New Bank appear here.\n"
                       "Reorder, rename, or drop them before building an image.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: palette(placeholdertext);")
        box_layout.addWidget(hint)
        box_layout.addStretch()
        outer.addWidget(box)
        return page

    def _build_filled_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 8, 10, 10)

        self._summary_label = QLabel("")
        self._summary_label.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        layout.addWidget(self._summary_label)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_list_context_menu)
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.model().rowsMoved.connect(self._on_rows_moved)
        self._list.currentItemChanged.connect(self._on_current_changed)
        self._list.itemDoubleClicked.connect(lambda _item: self._move_selected_to_new_bank())
        self._list.setToolTip("Double-click a bank to send it back to New Bank for editing")
        list_delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self._list)
        list_delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        list_delete_shortcut.activated.connect(self._delete_selected)

        self._contents_list = QListWidget()
        self._contents_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._contents_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._contents_list.model().rowsMoved.connect(self._on_contents_rows_moved)
        self._contents_list.setToolTip(
            "Drag to reorder this bank's own preset order before it's built")
        self._contents_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._contents_list.customContextMenuRequested.connect(self._on_contents_context_menu)
        contents_delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self._contents_list)
        contents_delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        contents_delete_shortcut.activated.connect(self._remove_selected_contents)

        # A real splitter (drag handle, both sides independently scrollable
        # once content overflows -- QListWidget already scrolls on its own,
        # the earlier bug was a hard setMaximumHeight cap on Contents that
        # left it stuck small regardless of window size) rather than two
        # fixed-ratio boxes; Contents starts larger since a bank's preset
        # list is usually the thing worth seeing more of at a glance.
        lists_splitter = QSplitter(Qt.Orientation.Vertical)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(QLabel("Pending banks:"))
        top_layout.addWidget(self._list)
        lists_splitter.addWidget(top)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(QLabel("Contents:"))
        bottom_layout.addWidget(self._contents_list)
        lists_splitter.addWidget(bottom)

        lists_splitter.setStretchFactor(0, 1)
        lists_splitter.setStretchFactor(1, 1)
        lists_splitter.setSizes([200, 260])
        layout.addWidget(lists_splitter, 1)

        row1 = QHBoxLayout()
        rename_btn = QPushButton("Rename…")
        rename_btn.clicked.connect(self._rename_selected)
        row1.addWidget(rename_btn)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._delete_selected)
        row1.addWidget(delete_btn)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear)
        row1.addWidget(clear_btn)
        layout.addLayout(row1)

        self._convert_btn = QPushButton("Process before building…")
        self._convert_btn.clicked.connect(self._show_convert_options)
        layout.addWidget(self._convert_btn)

        self._build_btn = QPushButton("Build Image →")
        self._build_btn.setToolTip(
            "Assemble every pending bank, in the order shown, and hand them "
            "to the Image column")
        self._build_btn.clicked.connect(self._build_image)
        layout.addWidget(self._build_btn)

        return page

    # -- receiving from New Bank ---------------------------------------------

    def add_pending(self, name: str, fmt: str, items: list[tuple[Any, Any, str]]) -> bool:
        if not items:
            return False
        if self._format is not None and fmt != self._format:
            self.statusMessage.emit(
                f"Pending queue is already {self._format} — can't add a {fmt} bank")
            return False
        if self._format is None:
            self._format = fmt
        self._pending.append({"name": name or "NewBank", "format": fmt, "items": list(items),
                               "convert_opts": None})
        self._refresh()
        self._list.setCurrentRow(len(self._pending) - 1)
        self.statusMessage.emit(f'Added "{name}" to the pending queue')
        return True

    # -- list management --------------------------------------------------------

    def _refresh(self) -> None:
        self._list.clear()
        for entry in self._pending:
            self._list.addItem(self._make_item(entry))
        self._stack.setCurrentIndex(1 if self._pending else 0)
        n = len(self._pending)
        self._summary_label.setText(f"{n} bank{'s' if n != 1 else ''} pending")
        self._build_btn.setEnabled(bool(self._pending))
        is_krz = self._format == "KRZ"
        self._convert_btn.setEnabled(not is_krz)
        self._convert_btn.setToolTip(
            "mpc2emu has no KRZ reader; vintage resample/reduce is E4B-only" if is_krz
            else "Choose vintage resample / sample-count reduction to apply "
                 "to the SELECTED pending bank's next Build Image (per bank, "
                 "not the whole queue)")
        self._update_contents_preview()

    def _make_item(self, entry: dict) -> QListWidgetItem:
        label = f"{entry['name']}  [{entry['format']}]  — {len(entry['items'])} preset(s)"
        if entry.get("convert_opts") is not None:
            label += "  · processing set"
        widget_item = QListWidgetItem(label)
        widget_item.setData(Qt.ItemDataRole.UserRole, entry)
        return widget_item

    def _on_rows_moved(self, *_args) -> None:
        self._pending = [
            self._list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._list.count())
        ]

    def _on_current_changed(self, _current, _previous) -> None:
        self._update_contents_preview()

    def _update_contents_preview(self) -> None:
        self._contents_list.clear()
        item = self._list.currentItem()
        entry = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if entry is None:
            return
        for preset_tuple in entry["items"]:
            _bank, _preset, name = preset_tuple
            widget_item = QListWidgetItem(name)
            widget_item.setData(Qt.ItemDataRole.UserRole, preset_tuple)
            self._contents_list.addItem(widget_item)

    def _on_contents_rows_moved(self, *_args) -> None:
        """Reordering a bank's own preset list here, before it's ever
        built, is exactly as meaningful as reordering it in New Bank --
        preset order determines index (E4B) / id (KRZ) assignment. Writes
        back into self._pending *by row index*, not via the dict retrieved
        from the list item's UserRole data -- unlike a tuple (see
        _on_rows_moved), PySide6/Shiboken converts a Python dict crossing
        through QVariant storage into a plain copy, not the same object,
        so mutating that copy would silently never reach self._pending."""
        row = self._list.currentRow()
        if row < 0 or row >= len(self._pending):
            return
        self._pending[row]["items"] = [
            self._contents_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._contents_list.count())
        ]

    def _on_contents_context_menu(self, pos) -> None:
        index = self._contents_list.indexAt(pos)
        if not index.isValid():
            return
        # Same "preserve an existing multi-selection" rule as New Bank's
        # own list context menu: only collapse to the clicked row if it
        # wasn't already part of the selection.
        selected = self._contents_list.selectionModel().selectedIndexes()
        if index.row() not in {i.row() for i in selected}:
            self._contents_list.setCurrentRow(index.row())
        if not self._contents_list.selectedIndexes():
            return
        menu = QMenu(self)
        label = "Remove Selected" if len(self._contents_list.selectedIndexes()) > 1 else "Remove"
        remove_action = menu.addAction(label)
        chosen = menu.exec(self._contents_list.viewport().mapToGlobal(pos))
        if chosen == remove_action:
            self._remove_selected_contents()

    def _remove_selected_contents(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._pending):
            return
        rows = sorted((idx.row() for idx in self._contents_list.selectedIndexes()), reverse=True)
        if not rows:
            return
        for r in rows:
            self._contents_list.takeItem(r)
        # Same by-row-index write-back as _on_contents_rows_moved (see its
        # docstring: a dict read back via UserRole is a copy, not
        # self._pending[row] itself).
        entry = self._pending[row]
        entry["items"] = [
            self._contents_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._contents_list.count())
        ]
        self._list.item(row).setData(Qt.ItemDataRole.UserRole, entry)
        self._list.item(row).setText(self._make_item(entry).text())

    def _on_list_context_menu(self, pos) -> None:
        index = self._list.indexAt(pos)
        if not index.isValid():
            return
        self._list.setCurrentRow(index.row())
        menu = QMenu(self)
        rename_action = menu.addAction("Rename…")
        delete_action = menu.addAction("Delete")
        menu.addSeparator()
        move_action = menu.addAction("Send to New Bank")
        chosen = menu.exec(self._list.viewport().mapToGlobal(pos))
        if chosen == rename_action:
            self._rename_selected()
        elif chosen == delete_action:
            self._delete_selected()
        elif chosen == move_action:
            self._move_selected_to_new_bank()

    def _rename_selected(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        entry = self._pending[row]
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=entry["name"])
        if not ok or not new_name.strip():
            return
        entry["name"] = new_name.strip()
        self._refresh()
        self._list.setCurrentRow(row)

    def _delete_selected(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        del self._pending[row]
        if not self._pending:
            self._format = None
        self._refresh()

    def _move_selected_to_new_bank(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        entry = self._pending.pop(row)
        if not self._pending:
            self._format = None
        self._refresh()
        self.moveToNewBankRequested.emit(entry["name"], entry["format"], entry["items"])

    def _clear(self) -> None:
        self._pending = []
        self._format = None
        self._refresh()

    # -- conversion options -------------------------------------------------------

    def _show_convert_options(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            self.statusMessage.emit("Select a pending bank first")
            return
        entry = self._pending[row]
        opts = ConvertOptionsDialog.get_options(self, initial=entry.get("convert_opts"))
        if opts is None:
            return   # Cancel -- leave whatever was already chosen for this bank untouched
        entry["convert_opts"] = None if opts.is_noop() else opts
        self._refresh()
        self._list.setCurrentRow(row)
        self.statusMessage.emit(
            f'Will apply vintage resample/reduce to "{entry["name"]}" on the next Build Image'
            if entry["convert_opts"] is not None
            else f'Conversion options cleared for "{entry["name"]}"')

    # -- build ------------------------------------------------------------------

    def _build_image(self) -> None:
        if not self._pending:
            return
        fmt = self._format
        pending_snapshot = list(self._pending)
        self._build_btn.setEnabled(False)
        self.statusMessage.emit(f"Assembling {len(pending_snapshot)} pending bank(s)…")
        w = workers.Worker(_assemble_all, pending_snapshot)
        w.signals.finished.connect(lambda paths, f=fmt: self._on_build_assembled(paths, f))
        w.signals.error.connect(self._on_build_error)
        w.signals.finished.connect(lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        w.signals.error.connect(lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        self._live_workers.append(w)
        workers.run(w)

    def _on_build_assembled(self, paths: list[str], fmt: str) -> None:
        # Building no longer empties the queue automatically -- the temp
        # files handed to buildRequested are independent copies, so the
        # pending recipes stay available to rebuild, tweak, or send to a
        # second image; Clear (or Delete per-row) is how the user empties
        # it, same as New Bank's own explicit Clear button.
        self._build_btn.setEnabled(bool(self._pending))
        self.buildRequested.emit(paths, fmt)

    def _on_build_error(self, message: str) -> None:
        self._build_btn.setEnabled(bool(self._pending))
        last_line = message.strip().splitlines()[-1] if message else "error"
        self.statusMessage.emit(f"Build failed: {last_line}")
