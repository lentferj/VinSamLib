"""
New Bank column (M5): accepts preset drops from the Explorer tree, locking
to whichever format (E4B or KRZ) the first drop carries — a later drop of
the other format is rejected, matching the "no cross-format conversion"
rule (that's what mpc2emu's convert.py is for). A live size/count meter
recomputes by actually calling banks.*.assemble() on the accumulated
selection (not an estimate — the real assembled bytes, since that's cheap
enough and already exhaustively validated), and Save As… writes exactly
those bytes.

M6 adds a "Send to Image Column" button: once the meter is valid, it hands
this bank's (bank, preset, name) recipe straight to the Pending for Image
column, where it waits — alongside any other banks sent the same way — until
"Build Image →" actually assembles and writes them to a real file. This used
to be a drag-and-drop-only affordance (a small hover-draggable label), but a
custom manual QDrag off a bare QLabel turned out to be exactly as fragile as
it sounds: easy to miss visually (competing for space with three other
buttons in a narrow column) and easy to fumble the press-move-release
gesture on. A plain button does the identical thing with one click and can't
be missed or dropped mid-gesture.

Double-clicking a bank back over in Pending for Image sends its recipe back
here via load_pending(), replacing whatever's currently staged — the same
(bank, preset, name) tuples, so it's genuinely editable again, not just a
frozen copy of already-assembled bytes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
                             QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from . import dnd, workers
from ..banks import e4b, krz

_RECOMPUTE_DEBOUNCE_MS = 250
_E4B_MAX_BYTES = 128 * 1024 * 1024   # writers/bank_splitter.py — hardware limit
_E4B_MAX_PRESETS = 1000
_KRZ_MAX_PRESETS = 1000

_FORMAT_EXT = {"E4B": "e4b", "KRZ": "krz"}
_DEFAULT_BANK_NAME = "NewBank"


def _sanitize_bank_name(name: str) -> str:
    """Neither E4B nor KRZ has an internal 'bank name' field — the name a
    real E4XT/K2000 shows for a bank is always taken from its *filename*
    (mpc2emu's own convert.py derives output names the same way: `f"{bank
    .name}{ext}"`). So whatever the user types here has to survive as a
    real filename on every target platform, hence stripping the characters
    Windows forbids even though this app also runs on Linux/macOS."""
    name = name.strip()
    if not name:
        return _DEFAULT_BANK_NAME
    return re.sub(r'[\\/:*?"<>|]', "_", name)


class BankPane(QWidget):
    statusMessage = Signal(str)
    sendToPendingRequested = Signal(str, str, list)   # (name, format, items)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

        self._format: Optional[str] = None
        self._items: list[tuple[Any, Any, str]] = []   # (bank, preset_obj, name)
        self._dedupe_enabled = True
        self._prompt_on_duplicate = True
        self._last_bytes: Optional[bytes] = None
        self._gen = 0
        self._live_workers: list[workers.Worker] = []
        self._recompute_timer = QTimer(self)
        self._recompute_timer.setSingleShot(True)
        self._recompute_timer.timeout.connect(self._recompute)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._head = QLabel("New Bank")
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
        hint = QLabel("Drag presets here from the library\nto start a new bank.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: palette(mid);")
        box_layout.addWidget(hint)
        box_layout.addStretch()
        outer.addWidget(box)
        return page

    def _build_filled_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 8, 10, 10)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(_DEFAULT_BANK_NAME)
        self._name_edit.setToolTip(
            "Used as the filename wherever this bank ends up (Save as… / "
            "Send to Image Column) — that filename is what a real E4XT or "
            "K2000 actually shows as the bank's name.")
        name_row.addWidget(self._name_edit, 1)
        layout.addLayout(name_row)

        self._meter_label = QLabel("")
        self._meter_label.setStyleSheet("color: palette(mid); font-size: 11px;")
        layout.addWidget(self._meter_label)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_list_context_menu)
        # Preset order matters -- it's what determines each preset's index
        # (E4B) / id (KRZ) in the assembled bank -- so dragging a row to a
        # new position needs to actually reorder self._items, not just move
        # pixels around; _on_rows_moved reads the list's new order back out
        # via each item's UserRole payload once Qt's internal move finishes.
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.model().rowsMoved.connect(self._on_rows_moved)
        delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self._list)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_shortcut.activated.connect(self._remove_selected)
        layout.addWidget(self._list, 1)

        row1 = QHBoxLayout()
        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self._remove_selected)
        row1.addWidget(remove_btn)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear)
        row1.addWidget(clear_btn)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        self._send_to_image_btn = QPushButton("Send to Image Column")
        self._send_to_image_btn.setToolTip(
            "Add this bank to the Pending for Image queue — nothing is "
            "written to a real image until Build Image → is clicked there")
        self._send_to_image_btn.clicked.connect(self._send_to_pending)
        row2.addWidget(self._send_to_image_btn)
        self._save_btn = QPushButton("Save as…")
        self._save_btn.clicked.connect(self._save_as)
        row2.addWidget(self._save_btn)
        layout.addLayout(row2)

        return page

    def _send_to_pending(self) -> None:
        if not self._items or self._format is None or self._last_bytes is None:
            self.statusMessage.emit(
                "Nothing ready to send yet — wait for the size to finish calculating")
            return
        if not self._save_btn.isEnabled():
            self.statusMessage.emit("Can't send an over-limit bank — remove some presets first")
            return
        name = _sanitize_bank_name(self._name_edit.text())
        self.sendToPendingRequested.emit(name, self._format, list(self._items))

    # -- duplicate-check options (View menu) -------------------------------------

    def set_dedupe_enabled(self, enabled: bool) -> None:
        self._dedupe_enabled = enabled

    def set_prompt_on_duplicate(self, enabled: bool) -> None:
        self._prompt_on_duplicate = enabled

    def load_pending(self, name: str, fmt: str, items: list[tuple[Any, Any, str]]) -> None:
        """Public entry point for the Pending column's double-click "send
        back to New Bank" — replaces whatever's currently staged here with
        the given recipe, exactly as if it had been assembled from scratch."""
        self._items = list(items)
        self._format = fmt
        self._name_edit.setText(name)
        self._head.setText(f"New Bank  [{fmt}]")
        self._refresh()

    # -- drag & drop ------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if self._acceptable(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        if not self._acceptable(mime):
            event.ignore()
            return
        descriptor = dnd.descriptor_from(mime)
        payload = dnd.payload_from(mime)
        items = [(bank, preset_obj, d["format"], d.get("name") or "(untitled)")
                 for (bank, preset_obj), d in zip(payload, descriptor)]
        event.acceptProposedAction()
        _added, dupes = self._add_items(items)
        if dupes:
            self.statusMessage.emit(
                f"Already in New Bank, skipped: {', '.join(dupes)}")

    def _acceptable(self, mime) -> bool:
        descriptor = dnd.descriptor_from(mime)
        payload = dnd.payload_from(mime)
        if not descriptor or len(descriptor) != len(payload):
            return False
        formats = {d.get("format") for d in descriptor}
        if len(formats) != 1 or None in formats or "" in formats:
            self.statusMessage.emit("Can't drop a mix of formats into one bank")
            return False
        fmt = formats.pop()
        if self._format is not None and fmt != self._format:
            self.statusMessage.emit(f"This bank is already {self._format} — can't add a {fmt} preset")
            return False
        return True

    # -- public entry point for the Explorer's right-click "Add to New Bank" ----

    def add_presets(self, items: list[tuple[Any, Any, str, str]]) -> bool:
        """items: list of (bank, preset_obj, format, name) -- the in-process
        equivalent of a drag-drop, for callers that aren't dragging (the
        Explorer tree's context menu). Same format-lock rules as a drop."""
        if not items:
            return False
        formats = {fmt for _bank, _preset, fmt, _name in items}
        if len(formats) != 1 or None in formats or "" in formats:
            self.statusMessage.emit("Can't add a mix of formats to one bank")
            return False
        fmt = formats.pop()
        if self._format is not None and fmt != self._format:
            self.statusMessage.emit(f"This bank is already {self._format} — can't add a {fmt} preset")
            return False
        added, dupes = self._add_items(items)
        if added:
            names = ", ".join(f'"{name}"' for name in added)
            msg = f"Added {names} to New Bank"
            if dupes:
                msg += f" ({len(dupes)} already present, skipped)"
            self.statusMessage.emit(msg)
        elif dupes:
            self.statusMessage.emit(
                f"Already in New Bank, skipped: {', '.join(dupes)}")
        return bool(added)

    def _add_items(self, items: list[tuple[Any, Any, str, str]]) -> tuple[list[str], list[str]]:
        """Appends items, optionally skipping ones already present -- keyed
        by bank path + preset index/id rather than Python object identity:
        presets reached through search results are re-parsed from scratch
        on every lookup (search_resolve.resolve_result() has no cache), so
        the same hit added twice arrives as two distinct objects each time
        -- an identity check would silently miss that duplicate. Content is
        stable across re-parses, so it's the only reliable key.

        self._dedupe_enabled (View menu) turns the check off entirely.
        self._prompt_on_duplicate switches a caught duplicate from "skip
        silently" to "ask before skipping" (QMessageBox, one per
        duplicate) -- either way returns (names added, names skipped)."""
        fmt = items[0][2]
        if self._format is None:
            self._format = fmt
            self._head.setText(f"New Bank  [{fmt}]")
        if not self._dedupe_enabled:
            added_names = [name for _bank, _preset, _fmt, name in items]
            for bank, preset_obj, _fmt, name in items:
                self._items.append((bank, preset_obj, name))
            self._refresh()
            return added_names, []
        existing = {_preset_key(bank, preset_obj, self._format) for bank, preset_obj, _name in self._items}
        added_names = []
        dupe_names = []
        for bank, preset_obj, _fmt, name in items:
            key = _preset_key(bank, preset_obj, self._format)
            if key in existing:
                if self._prompt_on_duplicate and self._confirm_duplicate(name):
                    self._items.append((bank, preset_obj, name))
                    added_names.append(name)
                    continue
                dupe_names.append(name)
                continue
            existing.add(key)
            self._items.append((bank, preset_obj, name))
            added_names.append(name)
        self._refresh()
        return added_names, dupe_names

    def _confirm_duplicate(self, name: str) -> bool:
        return QMessageBox.question(
            self, "Duplicate Preset",
            f'"{name}" is already in this bank. Add it again anyway?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    # -- list management --------------------------------------------------------

    def _on_list_context_menu(self, pos) -> None:
        if not self._items:
            return
        index = self._list.indexAt(pos)
        if index.isValid() and index.row() not in {i.row() for i in self._list.selectedIndexes()}:
            self._list.setCurrentRow(index.row())
        if not self._list.selectedIndexes():
            return
        menu = QMenu(self)
        label = "Remove Selected" if len(self._list.selectedIndexes()) > 1 else "Remove"
        remove_action = menu.addAction(label)
        chosen = menu.exec(self._list.viewport().mapToGlobal(pos))
        if chosen == remove_action:
            self._remove_selected()

    def _remove_selected(self) -> None:
        rows = sorted((idx.row() for idx in self._list.selectedIndexes()), reverse=True)
        for row in rows:
            del self._items[row]
        self._refresh()

    def _clear(self) -> None:
        self._items = []
        self._format = None
        self._head.setText("New Bank")
        self._name_edit.clear()
        self._refresh()

    def _refresh(self) -> None:
        self._list.clear()
        for item in self._items:
            _bank, _preset, name = item
            widget_item = QListWidgetItem(name)
            widget_item.setData(Qt.ItemDataRole.UserRole, item)
            self._list.addItem(widget_item)
        self._stack.setCurrentIndex(1 if self._items else 0)
        if self._items:
            self._meter_label.setText("Calculating…")
            self._recompute_timer.start(_RECOMPUTE_DEBOUNCE_MS)

    def _on_rows_moved(self, *_args) -> None:
        """Fires once Qt's InternalMove drag-drop finishes reordering rows
        in self._list -- read the new visual order back out (each item
        carries its own (bank, preset, name) tuple) and keep self._items in
        sync, then reassemble since preset order changed."""
        self._items = [
            self._list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._list.count())
        ]
        self._meter_label.setText("Calculating…")
        self._recompute_timer.start(_RECOMPUTE_DEBOUNCE_MS)

    # -- size meter (recomputes via the real assemble(), not an estimate) -------

    def _recompute(self) -> None:
        if not self._items or self._format is None:
            return
        self._gen += 1
        gen = self._gen
        selections = [(bank, preset) for bank, preset, _name in self._items]
        fn = e4b.assemble if self._format == "E4B" else krz.assemble
        w = workers.Worker(fn, selections)
        w.signals.finished.connect(lambda data, g=gen: self._apply_size(g, data))
        w.signals.error.connect(lambda msg, g=gen: self._apply_size_error(g, msg))
        w.signals.finished.connect(lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        w.signals.error.connect(lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        self._live_workers.append(w)
        workers.run(w)

    def _apply_size(self, gen: int, data: bytes) -> None:
        if gen != self._gen:
            return
        self._last_bytes = data
        n = len(self._items)
        if self._format == "E4B":
            self._meter_label.setText(
                f"{n} preset(s) — {_human(len(data))} / {_human(_E4B_MAX_BYTES)}")
            over = len(data) > _E4B_MAX_BYTES or n > _E4B_MAX_PRESETS
        else:
            self._meter_label.setText(f"{n} preset(s) — {_human(len(data))}")
            over = n > _KRZ_MAX_PRESETS
        self._meter_label.setStyleSheet(
            f"color: {'#c0392b' if over else 'palette(mid)'}; font-size: 11px;")
        self._save_btn.setEnabled(not over)

    def _apply_size_error(self, gen: int, message: str) -> None:
        if gen != self._gen:
            return
        self._last_bytes = None
        last_line = message.strip().splitlines()[-1] if message else "error"
        self._meter_label.setText(f"Can't assemble: {last_line}")
        self._meter_label.setStyleSheet("color: #c0392b; font-size: 11px;")
        self._save_btn.setEnabled(False)

    # -- save --------------------------------------------------------------------

    def _save_as(self) -> None:
        if not self._items or self._format is None:
            return
        selections = [(bank, preset) for bank, preset, _name in self._items]
        fn = e4b.assemble if self._format == "E4B" else krz.assemble
        try:
            data = fn(selections)
        except Exception as ex:
            self.statusMessage.emit(f"Save failed: {ex}")
            return

        ext = _FORMAT_EXT[self._format]
        name = _sanitize_bank_name(self._name_edit.text())
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save Bank", f"{name}.{ext}", f"{self._format} bank (*.{ext})",
            options=QFileDialog.Option.DontUseNativeDialog)
        if not path:
            return
        try:
            Path(path).write_bytes(data)
        except OSError as ex:
            self.statusMessage.emit(f"Save failed: {ex}")
            return
        self.statusMessage.emit(f"Saved {path}")


def _preset_key(bank: Any, preset_obj: Any, fmt: Optional[str]) -> tuple:
    """A duplicate-detection key that survives re-parsing the same bank
    file (bank.path is the label parse_bytes() was called with; presets
    carry their own stable index/id within that file)."""
    path = getattr(bank, "path", None)
    if fmt == "KRZ":
        return ("KRZ", path, getattr(preset_obj, "id", None))
    return ("E4B", path, getattr(preset_obj, "index", None))


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
