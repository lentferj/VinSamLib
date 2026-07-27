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

import functools
import re
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
                             QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from . import dnd, workers
from .detail_pane import _escape, zone_stats_lines
from ..banks import e4b, eiii, krz, summary
from ..config import Config

_RECOMPUTE_DEBOUNCE_MS = 250
# Hard format-technical ceilings (writers/bank_splitter.py for E4B/KRZ,
# docs/EIII_FORMAT.md's "Device requirements when writing" for EIII) --
# these are real write-format limits banks/e4b.py's/banks/krz.py's/
# banks/eiii.py's own assemble() enforces via raise, not adjustable here.
# The separate, lower, user-configurable per-format byte limit
# (Config.e4b_bank_limit_mb/krz_bank_limit_mb) is a soft "will this fit MY
# hardware's actual RAM" warning underneath this.
_E4B_MAX_PRESETS = 1000
_KRZ_MAX_PRESETS = 1000
_EIII_MAX_PRESETS = 256   # EMULATOR_3X/ESI_32_V3 -- the tighter of the two
                           # write targets; eiii.assemble() itself enforces
                           # the exact physical-preset-slot count (a preset
                           # with several linked layers can use more than
                           # one slot), this is just the meter's proxy.

_ASSEMBLE_FNS = {"E4B": e4b.assemble, "KRZ": krz.assemble, "EIII": eiii.assemble}
_FORMAT_EXT = {"E4B": "e4b", "KRZ": "krz", "EIII": "e3x"}
_DEFAULT_BANK_NAME = "NewBank"


def _sanitize_bank_name(name: str) -> str:
    """Neither E4B nor KRZ has an internal 'bank name' field — the name a
    real E4XT/K2000 shows for a bank is always taken from its *filename*
    (mpc2emu's own convert.py derives output names the same way: `f"{bank
    .name}{ext}"`). EIII is the exception (it has a real on-disk name
    field, threaded through via banks.eiii.assemble()'s `bank_name`
    parameter — see _recompute()/_save_as() below), but the sanitized
    result is used as this bank's *filename* everywhere regardless of
    format, so it has to survive as a real filename on every target
    platform either way, hence stripping the characters Windows forbids
    even though this app also runs on Linux/macOS."""
    name = name.strip()
    if not name:
        return _DEFAULT_BANK_NAME
    return re.sub(r'[\\/:*?"<>|]', "_", name)


class BankPane(QWidget):
    statusMessage = Signal(str)
    sendToPendingRequested = Signal(str, str, list)   # (name, format, items)

    def __init__(self, config: Optional[Config] = None, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._config = config or Config()

        self._format: Optional[str] = None
        self._items: list[tuple[Any, Any, str]] = []   # (bank, preset_obj, name)
        self._dedupe_enabled = True
        self._prompt_on_duplicate = True
        self._last_bytes: Optional[bytes] = None
        self._gen = 0
        self._info_gen = 0
        self._pre_add_snapshot: Optional[list] = None
        self._was_over_limit = False
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
        hint.setStyleSheet("color: palette(placeholdertext);")
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
        self._meter_label.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
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
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self._list)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_shortcut.activated.connect(self._remove_selected)
        layout.addWidget(self._list, 1)

        self._info_label = QLabel("")
        self._info_label.setWordWrap(True)
        self._info_label.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        self._info_label.setContentsMargins(0, 4, 0, 6)
        layout.addWidget(self._info_label)

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
        if self._format == "EIII":
            self.statusMessage.emit(
                "EIII banks aren't placed on a disk image from here yet — use Save as…")
            return
        if not self._save_btn.isEnabled():
            self.statusMessage.emit("Can't send an over-limit bank — remove some presets first")
            return
        name = _sanitize_bank_name(self._name_edit.text())
        self.sendToPendingRequested.emit(name, self._format, list(self._items))

    @property
    def format(self) -> Optional[str]:
        """The format this bank is currently locked to ("E4B"/"KRZ"), or
        None while still empty/unlocked -- lets callers that open a
        target-format picker (XpmImportDialog) know when only one choice
        can actually succeed."""
        return self._format

    def refresh_size_limits(self) -> None:
        """Re-applies the size check against the last-assembled bytes
        without a full reassemble -- called by Settings after the
        configurable per-format RAM limit changes, so an already-staged
        bank's meter/warning reflect the new threshold immediately rather
        than waiting for the next add."""
        if self._last_bytes is not None:
            self._apply_size(self._gen, self._last_bytes)

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

    def unique_name(self, base: str) -> str:
        """Returns `base` unchanged if no current item already displays that
        exact name, otherwise `base` with an incrementing " 2", " 3", ...
        suffix (same convention as a file manager's "Copy"/"Copy 2" naming).

        For callers that intentionally give each conversion its own fresh
        identity (XPM import, "Import via mpc2emu..." on a preset) so that
        re-converting the same source with different options isn't treated
        as a duplicate and skipped -- content-based dedup (_preset_key())
        doesn't collide, but the display name would, since it's derived
        from the same source name/filename every time. Without this, three
        conversions of the same preset all show up as identical, indistin-
        guishable rows."""
        existing = {name for _bank, _preset, name in self._items}
        if base not in existing:
            return base
        i = 2
        while f"{base} {i}" in existing:
            i += 1
        return f"{base} {i}"

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
        duplicate) -- either way returns (names added, names skipped).

        Snapshots self._items *before* this batch is appended -- if the
        resulting bank turns out to be over the format's size/count limit,
        _maybe_warn_over_limit() offers to undo back to this exact state."""
        self._pre_add_snapshot = list(self._items)
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
        if not self._items:
            # Removing the last preset one at a time (Remove/Remove
            # Selected/Delete) used to leave the format lock stuck on
            # whatever it was, forever refusing a different-format add
            # even though the bank was genuinely empty again -- only
            # Clear reset it. An empty bank should never stay locked.
            self._reset_format_lock()
        self._refresh()

    def _clear(self) -> None:
        self._items = []
        self._reset_format_lock()
        self._name_edit.clear()
        self._refresh()

    def _reset_format_lock(self) -> None:
        self._format = None
        self._head.setText("New Bank")

    def _refresh(self) -> None:
        # QListWidget.clear() doesn't reliably emit itemSelectionChanged in
        # every Qt version -- invalidate any in-flight info lookup and
        # blank the label explicitly rather than relying on that signal.
        self._info_gen += 1
        self._info_label.setText("")
        self._list.clear()
        for item in self._items:
            _bank, _preset, name = item
            widget_item = QListWidgetItem(name)
            widget_item.setData(Qt.ItemDataRole.UserRole, item)
            self._list.addItem(widget_item)
        self._stack.setCurrentIndex(1 if self._items else 0)
        # No image target exists for a raw EIII bank yet (Pending for
        # Image / the Image column only build E4B EMU3 images and KRZ
        # K2000 disks -- see build/images.py's IMAGE_KINDS) -- Save As…
        # still works for EIII (a real .e3x/.esi file), same as every
        # other format, so only this one button is gated.
        is_eiii = self._format == "EIII"
        self._send_to_image_btn.setEnabled(not is_eiii)
        self._send_to_image_btn.setToolTip(
            "EIII banks aren't placed on a disk image from here yet — use "
            "Save as… to write a real .e3x/.esi file" if is_eiii
            else "Add this bank to the Pending for Image queue — nothing is "
                 "written to a real image until Build Image → is clicked there")
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

    # -- selection info (single selected preset only) ---------------------------

    def _on_selection_changed(self) -> None:
        """Same "general info" DetailPane already shows for a preset in
        Explorer, reused here (zone_stats_lines()) since New Bank had no
        per-item info at all before -- only a whole-bank size meter.
        Computed off the GUI thread since summarize_preset() reassembles
        + reparses an E4B preset through mpc2emu (see banks/summary.py);
        the generation-counter pattern matches DetailPane's own."""
        self._info_gen += 1
        gen = self._info_gen
        selected = self._list.selectedItems()
        if len(selected) != 1:
            self._info_label.setText("")
            return
        bank, preset_obj, name = selected[0].data(Qt.ItemDataRole.UserRole)
        self._info_label.setText("Loading…")
        w = workers.Worker(summary.summarize_preset, bank, preset_obj)
        w.signals.finished.connect(lambda ps, g=gen, n=name: self._apply_preset_info(g, n, ps))
        w.signals.error.connect(lambda msg, g=gen: self._apply_preset_info_error(g, msg))
        w.signals.finished.connect(lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        w.signals.error.connect(lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        self._live_workers.append(w)
        workers.run(w)

    def _apply_preset_info(self, gen: int, name: str, ps: summary.PresetSummary) -> None:
        if gen != self._info_gen:
            return
        voice_label = "Keymaps" if ps.format == "KRZ" else "Voices"
        self._info_label.setText(
            f"<b>{_escape(name)}</b><br>"
            f"{voice_label}: {ps.voice_count} &middot; "
            f"Total sample size: {_human(ps.total_sample_bytes)}<br>"
            f"{zone_stats_lines(ps.zones)}")

    def _apply_preset_info_error(self, gen: int, message: str) -> None:
        if gen != self._info_gen:
            return
        last_line = message.strip().splitlines()[-1] if message else "error"
        self._info_label.setText(f"<i>Failed to load: {_escape(last_line)}</i>")

    # -- size meter (recomputes via the real assemble(), not an estimate) -------

    def _recompute(self) -> None:
        if not self._items or self._format is None:
            return
        self._gen += 1
        gen = self._gen
        selections = [(bank, preset) for bank, preset, _name in self._items]
        fn = self._assemble_fn()
        w = workers.Worker(fn, selections)
        w.signals.finished.connect(lambda data, g=gen: self._apply_size(g, data))
        w.signals.error.connect(lambda msg, g=gen: self._apply_size_error(g, msg))
        w.signals.finished.connect(lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        w.signals.error.connect(lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        self._live_workers.append(w)
        workers.run(w)

    def _assemble_fn(self):
        """The real assemble() to call for the currently-locked format,
        pre-bound with the user's typed bank name for EIII (the one format
        of the three with a real on-disk name field — see
        _sanitize_bank_name()'s docstring). Returned as a plain callable
        taking just `selections`, so it drops straight into
        `workers.Worker(fn, selections)` the same way as before."""
        fn = _ASSEMBLE_FNS[self._format]
        if self._format == "EIII":
            fn = functools.partial(fn, bank_name=_sanitize_bank_name(self._name_edit.text()))
        return fn

    def _apply_size(self, gen: int, data: bytes) -> None:
        if gen != self._gen:
            return
        self._last_bytes = data
        n = len(self._items)
        if self._format == "E4B":
            limit_bytes = self._config.e4b_bank_limit_mb * 1024 * 1024
            self._meter_label.setText(
                f"{n} preset(s) — {_human(len(data))} / {_human(limit_bytes)}")
            over = len(data) > limit_bytes or n > _E4B_MAX_PRESETS
            detail = (f"{n} presets exceed the E4XT's {_E4B_MAX_PRESETS}-preset limit."
                      if n > _E4B_MAX_PRESETS else
                      f"{_human(len(data))} exceeds your configured {_human(limit_bytes)} "
                      f"E4XT RAM limit (Settings…).")
        elif self._format == "EIII":
            # No dedicated EIII RAM-limit setting (Settings only offers
            # E4XT/K2000) -- EIII banks load on the same E4XT hardware E4B
            # does (via its backward-compatibility loader, EIII_FORMAT.md),
            # so the E4XT setting doubles as EIII's soft warning threshold
            # too rather than adding a third near-identical spinbox.
            limit_bytes = self._config.e4b_bank_limit_mb * 1024 * 1024
            self._meter_label.setText(
                f"{n} preset(s) — {_human(len(data))} / {_human(limit_bytes)}")
            over = len(data) > limit_bytes or n > _EIII_MAX_PRESETS
            detail = (f"{n} presets may exceed the EIIIX/ESI {_EIII_MAX_PRESETS}-preset "
                      f"limit (some presets use more than one preset slot)."
                      if n > _EIII_MAX_PRESETS else
                      f"{_human(len(data))} exceeds your configured {_human(limit_bytes)} "
                      f"E4XT RAM limit (Settings…).")
        else:
            limit_bytes = self._config.krz_bank_limit_mb * 1024 * 1024
            self._meter_label.setText(
                f"{n} preset(s) — {_human(len(data))} / {_human(limit_bytes)}")
            over = len(data) > limit_bytes or n > _KRZ_MAX_PRESETS
            detail = (f"{n} presets exceed the K2000's {_KRZ_MAX_PRESETS}-preset limit."
                      if n > _KRZ_MAX_PRESETS else
                      f"{_human(len(data))} exceeds your configured {_human(limit_bytes)} "
                      f"K2000 RAM limit (Settings…).")
        self._meter_label.setStyleSheet(
            f"color: {'#c0392b' if over else 'palette(placeholdertext)'}; font-size: 11px;")
        self._save_btn.setEnabled(not over)
        self._maybe_warn_over_limit(over, detail)

    def _apply_size_error(self, gen: int, message: str) -> None:
        if gen != self._gen:
            return
        self._last_bytes = None
        last_line = message.strip().splitlines()[-1] if message else "error"
        self._meter_label.setText(f"Can't assemble: {last_line}")
        self._meter_label.setStyleSheet("color: #c0392b; font-size: 11px;")
        self._save_btn.setEnabled(False)
        self._maybe_warn_over_limit(True, _friendly_assemble_error(last_line))

    # -- over-limit popup ---------------------------------------------------------

    def _maybe_warn_over_limit(self, over: bool, detail: str) -> None:
        """Rising-edge only -- fires once when the bank crosses from fitting
        to not fitting (right after an add), not again on every subsequent
        recompute while it's still over (e.g. reordering, or a second add
        while already over). Offers to undo back to the state captured by
        _add_items() just before the add that pushed it over, if that
        state is still available."""
        if not over:
            self._was_over_limit = False
            self._pre_add_snapshot = None
            return
        if self._was_over_limit:
            return
        self._was_over_limit = True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Bank Too Large")
        box.setText(f"This bank can't be built as-is.\n\n{detail}")
        keep_btn = box.addButton("Keep Anyway", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(keep_btn)
        undo_btn = None
        if self._pre_add_snapshot is not None:
            undo_btn = box.addButton("Undo Last Add", QMessageBox.ButtonRole.DestructiveRole)
        box.exec()
        if undo_btn is not None and box.clickedButton() is undo_btn:
            self._items = self._pre_add_snapshot
            self._pre_add_snapshot = None
            self._was_over_limit = False
            if not self._items:
                # If the over-limit add was the bank's very first one,
                # undoing it empties the bank -- same stuck-lock bug as
                # _remove_selected(), just reached a different way.
                self._reset_format_lock()
            self._refresh()

    # -- save --------------------------------------------------------------------

    def _save_as(self) -> None:
        if not self._items or self._format is None:
            return
        selections = [(bank, preset) for bank, preset, _name in self._items]
        fn = self._assemble_fn()
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
    if fmt == "EIII":
        return ("EIII", path, getattr(preset_obj, "index", None))
    return ("E4B", path, getattr(preset_obj, "index", None))


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


_TOO_LARGE_RE = re.compile(r"assembled bank too large: (\d+) > (\d+) bytes")


def _friendly_assemble_error(last_line: str) -> str:
    """banks.e4b.assemble() raises a raw byte-count ValueError -- reformat
    the common "too large" case into human-readable sizes for the over-
    limit popup; anything else (a genuine bug) is shown as-is."""
    m = _TOO_LARGE_RE.search(last_line)
    if m:
        got, limit = int(m.group(1)), int(m.group(2))
        return f"{_human(got)} exceeds the E4XT's {_human(limit)} limit."
    return last_line
