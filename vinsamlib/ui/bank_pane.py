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
import struct
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QFrame, QHBoxLayout,
                             QGridLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMenu,
                             QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from . import dnd, workers
from .detail_pane import _escape, zone_stats_lines
from .sample_placement_dialog import SamplePlacementDialog, vel_window
from .sample_rename_dialog import SampleRenameDialog
from ..banks import e4b, eiii, krz, summary
from ..filenames import safe_filename
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

#: An EIII zone stores its root as an E-mu key number where 0 is MIDI 21
#: (A-1) -- mpc2emu's eiii_writer KEY_OFFSET. E4B stores plain MIDI.
_EIII_KEY_OFFSET = 21

#: Octave convention for the key suffix a bulk rename appends. Matches the
#: import path's own name_octave fallback, so a bank named here and one
#: named at import agree instead of sitting an octave apart.
_RENAME_OCTAVE = 2


def _sanitize_bank_name(name: str) -> str:
    """Neither E4B nor KRZ has an internal 'bank name' field — the name a
    real E4XT/K2000 shows for a bank is always taken from its *filename*
    (mpc2emu's own convert.py derives output names the same way: `f"{bank
    .name}{ext}"`). EIII is the exception (it has a real on-disk name
    field, threaded through via banks.eiii.assemble()'s `bank_name`
    parameter — see _recompute()/_save_as() below), but the sanitized
    result is used as this bank's *filename* everywhere regardless of
    format, so it has to survive as a real filename on every target
    platform either way -- see filenames.safe_filename for why that is an
    allowlist rather than the Windows-forbidden set this used to strip."""
    return safe_filename(name, fallback=_DEFAULT_BANK_NAME)


class BankPane(QWidget):
    statusMessage = Signal(str)
    sendToPendingRequested = Signal(str, str, list, dict, dict, dict)   # (+ voice_velocity)
    #: A soundfont-style source was dropped here: list of import-request
    #: dicts (see ui/dnd.build_import_mime_data). MainWindow converts them
    #: and calls back into add_presets() with what they became.
    importRequested = Signal(list)

    def __init__(self, config: Optional[Config] = None, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._config = config or Config()

        self._format: Optional[str] = None
        self._items: list[tuple[Any, Any, str]] = []   # (bank, preset_obj, name)
        #: {original sample name: new name}, from the Rename Samples dialog.
        #: Cleared with the bank -- a rename belongs to the material that
        #: was staged, not to the pane.
        self._sample_renames: dict = {}
        #: {original sample name: (lo, root, hi)} from Adjust Placement.
        #: Cleared with the bank, exactly like the renames.
        self._zone_placement: dict = {}
        self._voice_velocity: dict = {}
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

        # ONE grid for every button, with both columns forced to equal
        # stretch. Three separate QHBoxLayouts could not stay aligned: each
        # gave its buttons their own sizeHint plus a share of the leftover, so
        # "Clear" and "Save as..." started at different widths and drifted
        # further apart as the pane was resized. A grid with equal column
        # stretch is the same two columns at every width.
        buttons = QGridLayout()
        buttons.setColumnStretch(0, 1)
        buttons.setColumnStretch(1, 1)

        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self._remove_selected)
        buttons.addWidget(remove_btn, 0, 0)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear)
        buttons.addWidget(clear_btn, 0, 1)

        self._rename_btn = QPushButton("Rename Samples…")
        self._rename_btn.clicked.connect(self._rename_samples)
        buttons.addWidget(self._rename_btn, 1, 0)
        self._placement_btn = QPushButton("Adjust Placement…")
        self._placement_btn.clicked.connect(self._adjust_placement)
        buttons.addWidget(self._placement_btn, 1, 1)
        # Second cell left empty rather than filled with the status: a label
        # there would be the only thing in the grid that is not a button, and
        # it was what knocked the row out of line in the first place.
        self._rename_status = QLabel("")
        self._rename_status.setStyleSheet(
            "color: palette(placeholdertext); font-size: 11px;")
        buttons.addWidget(self._rename_status, 2, 0, 1, 2)

        self._send_to_image_btn = QPushButton("Send to Image Column")
        self._send_to_image_btn.setToolTip(
            "Add this bank to the Pending for Image queue — nothing is "
            "written to a real image until Build Image → is clicked there")
        self._send_to_image_btn.clicked.connect(self._send_to_pending)
        buttons.addWidget(self._send_to_image_btn, 3, 0)
        self._save_btn = QPushButton("Save as…")
        self._save_btn.clicked.connect(self._save_as)
        buttons.addWidget(self._save_btn, 3, 1)
        layout.addLayout(buttons)

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
        # The renames travel WITH the recipe. Pending re-assembles from
        # (bank, preset) pairs rather than from the bytes this pane already
        # built, so a rename left behind here would be silently absent from
        # the image -- the meter and Save as... would show one bank and the
        # media would hold another.
        self.sendToPendingRequested.emit(name, self._format, list(self._items),
                                          dict(self._sample_renames),
                                          dict(self._zone_placement),
                                          dict(self._voice_velocity))

    @property
    def format(self) -> Optional[str]:
        """The format this bank is currently locked to ("E4B"/"KRZ"), or
        None while still empty/unlocked -- lets callers that open a
        target-format picker (FormatConvertDialog) know when only one choice
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

    def load_pending(self, name: str, fmt: str, items: list[tuple[Any, Any, str]],
                      sample_renames: Optional[dict] = None,
                      zone_placement: Optional[dict] = None,
                      voice_velocity: Optional[dict] = None) -> None:
        """Public entry point for the Pending column's double-click "send
        back to New Bank" — replaces whatever's currently staged here with
        the given recipe, exactly as if it had been assembled from scratch."""
        self._items = list(items)
        self._sample_renames = dict(sample_renames or {})
        self._zone_placement = dict(zone_placement or {})
        self._voice_velocity = dict(voice_velocity or {})
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
        if dnd.has_import_request(mime):
            requests = dnd.import_requests_from(mime)
            if not requests:
                event.ignore()
                return
            # Accept now, deliver later: converting is MainWindow's job (it
            # owns the Convert Options dialog, the Config and the worker
            # queue), and it cannot finish inside a drop handler anyway. The
            # presets appear when the conversion does, exactly as they do for
            # the same file imported from the Explorer's context menu.
            event.acceptProposedAction()
            self.importRequested.emit(requests)
            return
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
        if dnd.has_import_request(mime):
            # An import source carries no format of its own -- the target
            # format is chosen in the dialog the drop opens, and the pane's
            # own lock is handed to that dialog rather than checked here.
            return bool(dnd.import_requests_from(mime))
        descriptor = dnd.descriptor_from(mime)
        payload = dnd.payload_from(mime)
        if not descriptor or len(descriptor) != len(payload):
            return False
        formats = {d.get("format") for d in descriptor}
        if len(formats) != 1 or None in formats or "" in formats:
            self.statusMessage.emit("Can't drop a mix of formats into one bank")
            return False
        fmt = formats.pop()
        if self._rejects_format(fmt):
            return False
        if self._format is not None and fmt != self._format:
            self.statusMessage.emit(f"This bank is already {self._format} — can't add a {fmt} preset")
            return False
        return True

    def _rejects_format(self, fmt: str) -> bool:
        """True (having said why) when *fmt* is not one this pane can build.

        New Bank assembles E4B, KRZ and EIII and nothing else, but until now
        it only ever checked that a format string was non-empty -- so an
        unlocked pane would accept any label at all, set `self._format` to
        it, and only fall over later in `_assemble_fn()` with a bare KeyError
        on `_ASSEMBLE_FNS`. Nothing produced such a label before; the
        soundfont-style import sources (SF2, SFZ, EXS24, TAL, GIG) are the
        first formats in this project that can be read and never written, so
        the rule is worth stating where it can be enforced rather than left
        as something no caller happens to violate. Import sources reach New
        Bank only *after* conversion, as the E4B/KRZ/EIII they became."""
        if fmt in _ASSEMBLE_FNS:
            return False
        self.statusMessage.emit(
            f"New Bank builds {', '.join(sorted(_ASSEMBLE_FNS))} banks — "
            f"{fmt} is an import source, not an output format")
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
        if self._rejects_format(fmt):
            return False
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
        # Same action as the button, not a second implementation: the
        # button can be off-screen in a narrow pane, and right-clicking
        # the list is where a user looks for what to do WITH the list.
        menu.addSeparator()
        # Scoped to the SELECTION here, unlike the button. Right-clicking a
        # preset means "this one", and a bank of twenty presets otherwise
        # opens a dialog of hundreds of rows -- the same unusability the bulk
        # base-name field exists to solve, one level up. The rename itself is
        # still keyed by sample name, so it lands wherever that sample is
        # used; the dialog says which other staged presets that is.
        n_sel = len(self._list.selectedIndexes())
        rename_action = menu.addAction(
            "Rename Samples of Selected…" if n_sel > 1 else "Rename Samples…")
        rename_action.setEnabled(self._rename_btn.isEnabled())
        rename_action.setToolTip(self._rename_btn.toolTip())
        chosen = menu.exec(self._list.viewport().mapToGlobal(pos))
        if chosen is rename_action:
            self._rename_samples(selected_only=True)
            return
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
        self._sample_renames = {}
        self._zone_placement = {}
        self._voice_velocity = {}
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
        # EIII banks are placed on the exact same EMU3 CD/HD images E4B
        # banks use (mpc2emu's iso_builder/hda_builder are bank-content-
        # agnostic, and now correctly tag each bank's real format on disk
        # -- see build/images.py's append_banks()), so this is enabled the
        # same way for every format now.
        self._send_to_image_btn.setEnabled(True)
        self._send_to_image_btn.setToolTip(
            "Add this bank to the Pending for Image queue — nothing is "
            "written to a real image until Build Image → is clicked there")
        # Here rather than only at drop time: the format lock can change (a
        # Clear, or a bank loaded back from Pending), and the button has to
        # follow it or it would offer a rename for a format that cannot.
        self._sync_rename_button()
        self._sync_placement_button()
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
        last_line = workers.last_error_line(message)
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

    #: All three now. E4B and EIII patch a fixed-width field; KRZ has to GROW
    #: its block, because its name slot is padded only to the next 2-byte
    #: boundary and has a median of zero spare bytes across 111 real objects.
    #: That turned out to be local rather than cascading -- objects reference
    #: each other by id, `osize` is recomputed, and PCM word offsets index
    #: into the PCM region rather than the file -- and banks/krz.py's
    #: assemble() re-reads any bank it resized before handing it on.
    _RENAMEABLE = ("E4B", "EIII", "KRZ")

    def _selected_presets(self) -> list:
        """The staged (bank, preset, name) tuples the user has selected, or
        all of them when nothing is selected."""
        rows = sorted(idx.row() for idx in self._list.selectedIndexes())
        chosen = [self._items[r] for r in rows if 0 <= r < len(self._items)]
        return chosen or list(self._items)

    def _sample_rows_in_bank(self, items=None) -> list[dict]:
        """[{"name", "root"}] for every sample the staged presets reference,
        in bank order, deduped the way assemble() dedupes -- so the list is
        the one the written bank will hold, not one entry per zone.

        `root` is the MIDI note the sample is recorded at, read straight out
        of the zone entry's own bytes and matched to the sample by INDEX.
        That last part is the whole reason it can be done at all: the obvious
        route to a root note is mpc2emu's summary, but joining that back to
        these rows would have to be BY NAME, and its name decoding is exactly
        what produces U+FFFD on some real banks -- untrustworthy as a key for
        precisely the samples most in need of renaming. An index cannot be
        mis-decoded.

        None when a sample's root cannot be read (an unfamiliar layout, or a
        sample no zone references); the caller falls back to numbering.
        """
        scope = self._items if items is None else items
        # Which OTHER staged presets each sample also appears in. A rename is
        # keyed by the sample's name, so it lands wherever that sample is used
        # -- scoping the VIEW to one preset does not scope the effect, and the
        # user should be told rather than surprised.
        elsewhere: dict = {}
        for bank, preset, label in self._items:
            if any(preset is p for _b, p, _n in scope):
                continue
            for name, _root in self._samples_of(bank, preset):
                elsewhere.setdefault(name, set()).add(label)

        # {sample name: (lo_vel, hi_vel)} for the staged presets. Only E4B
        # exposes one; _zone_ranges returns {} for the others.
        vel_by_name: dict = {}
        for bank, preset, _label in scope:
            for idx, info in self._zone_ranges(bank, preset).items():
                samp = bank.samples.get(idx)
                if samp is not None and samp.name not in vel_by_name:
                    vel_by_name[samp.name] = (info[3], info[4])

        # Only worth showing when it DISTINGUISHES something, and "full range"
        # has to mean what a musician means by it: authored banks use 1-127 as
        # often as 0-127, and treating those as two different windows made a
        # preset with no layering at all sprout a "v1-127" on every row but
        # the first. Both normalise to None, so a preset that is really one
        # layer shows nothing and a genuinely layered one shows only the rows
        # that differ.
        vel_by_name = {n: vel_window(v) for n, v in vel_by_name.items()}
        vel_informative = len(set(vel_by_name.values())) > 1

        rows: list[dict] = []
        known: set[str] = set()
        for bank, preset, _name in scope:
            for name, root in self._samples_of(bank, preset):
                if name in known:
                    continue
                known.add(name)
                # A pending move wins over the zone bytes. Both dialogs edit
                # the SAME staged bank, so "Plays" showing the old note after
                # Adjust Placement… moved the sample is the mirror image of the
                # placement list showing old names after a rename -- one edit
                # invisible to the other window. (This is not the Samples pane
                # rule: that pane is UPSTREAM of New Bank and correctly shows
                # the source untouched. These two are the same step.)
                moved = self._zone_placement.get(name)
                if moved is not None:
                    root = moved[1]
                # The velocity window belongs next to the note: two samples in
                # different layers of one key are otherwise identical rows,
                # which is exactly when you most need to tell them apart while
                # naming them. None for formats with nothing to show.
                vel = vel_by_name.get(name) if vel_informative else None
                if name in self._voice_velocity:
                    vel = self._voice_velocity[name]
                rows.append({"name": name, "root": root, "vel": vel,
                             "shared_with": sorted(elsewhere.get(name, ()))})
        return rows

    def _samples_of(self, bank, preset):
        """[(name, root)] for one staged preset, per format.

        KRZ needs its own walk and this is the second time that has bitten:
        a KrzObject has no `sample_indices` because a KRZ program does not
        reference samples at all -- it references KEYMAPS, and those
        reference samples. Reusing E4B's attribute here left the Rename
        Samples dialog opening with zero rows for a KRZ bank while its button
        sat enabled, which is precisely the "control that silently does
        nothing" this whole feature was held back to avoid.
        """
        if self._format == "KRZ":
            for km_id in bank.program_keymap_refs(preset):
                km = bank.keymaps.get(km_id)
                if km is None:
                    continue
                for sid in bank.keymap_sample_refs(km):
                    samp = bank.samples.get(sid)
                    if samp is not None:
                        yield samp.name, self._krz_root(samp)
            return
        roots = self._zone_roots(bank, preset)
        for idx in getattr(preset, "sample_indices", []) or []:
            samp = bank.samples.get(idx)
            if samp is not None:
                yield samp.name, roots.get(idx)

    @staticmethod
    def _krz_root(samp) -> Optional[int]:
        """A KRZ sample carries its own root in Soundfilehead byte 0 -- there
        is no zone entry to read it from, unlike E4B and EIII. Same byte
        banks/summary.py's _krz_zone() uses."""
        body = samp.body()
        if len(body) <= krz.SAMPLE_HDR:
            return None
        root = body[krz.SAMPLE_HDR]
        return root if 0 <= root <= 127 else None

    def _zone_roots(self, bank, preset) -> dict:
        """{sample index: MIDI root note} from a preset's own zone bytes.

        Two formats, two layouts, both byte-level so no parse through
        mpc2emu is involved:

        * E4B -- `zone_refs` gives the offset of each 22-byte zone entry, and
          byte 14 of it is the root key (mpc2emu's e4b_writer writes
          `entry[14] = root_key`, its parser reads the same byte back).
        * EIII -- `zone_refs` gives the offset of the 2-byte sample-index
          field, which sits one byte into the 48-byte zone; byte 0 of the
          zone is the ORIGINAL KEY in E-mu numbering, where key 0 is MIDI 21.

        First zone wins: one sample can be spread over several zones and the
        name belongs to the sample, so it is named for the root it was
        recorded at rather than for wherever it also happens to be mapped.
        """
        out: dict = {}
        body = getattr(preset, "body", None)
        refs = getattr(preset, "zone_refs", None)
        if not body or not refs:
            return out
        for off, raw in refs:
            if self._format == "E4B":
                idx, root_off, bias = raw, off + 14, 0
            else:                                   # EIII
                idx, root_off, bias = raw & 0x3FFF, off - 1, _EIII_KEY_OFFSET
            if idx in out or not (0 <= root_off < len(body)):
                continue
            root = body[root_off] + bias
            if 0 <= root <= 127:
                out[idx] = root
        return out

    def _sync_rename_button(self) -> None:
        ok = bool(self._items) and self._format in self._RENAMEABLE
        self._rename_btn.setEnabled(ok)
        if self._format and self._format not in self._RENAMEABLE:
            self._rename_btn.setToolTip(
                f"{self._format} stores a sample name in a slot sized exactly "
                f"to the name already there, so renaming means rebuilding the "
                f"block. Not offered rather than half-offered.")
        else:
            self._rename_btn.setToolTip(
                "Rename the samples inside this bank. The audio is untouched.")
        n = len(self._sample_renames)
        self._rename_status.setText(f"{n} sample(s) renamed" if n else "")

    #: Formats whose zones carry their own key range, so a placement edit is a
    #: patch in place. EIII is absent and it is not an oversight: an EIII
    #: preset has NO per-zone range at all -- it carries an 88-entry note-zone
    #: table mapping each key to one zone, so moving a sample there means
    #: rewriting that table. KRZ reaches its samples through keymaps, which is
    #: a third shape again. Both are their own piece of work.
    _PLACEABLE = ("E4B",)

    def _sync_placement_button(self) -> None:
        ok = bool(self._items) and self._format in self._PLACEABLE
        self._placement_btn.setEnabled(ok)
        if self._format and self._format not in self._PLACEABLE:
            self._placement_btn.setToolTip(
                f"{self._format} does not store a key range per zone, so a "
                f"sample cannot be moved by patching one. Not offered rather "
                f"than half-offered.")
        else:
            self._placement_btn.setToolTip(
                "Move where the samples in this bank play: key range, root "
                "note, and the velocity window they answer to. The audio is "
                "untouched.\n⚠ Experimental — neither placement nor velocity "
                "has been confirmed on hardware. A velocity change also "
                "rebuilds the preset's voices, since an E4B keeps that window "
                "on the voice rather than the zone.")

    def _placement_rows(self, items) -> list[dict]:
        """[{"name","orig","lo","root","hi"}] for the staged presets, read from
        the zone bytes so the dialog opens on what the bank actually says.

        A sample used by several zones appears ONCE, carrying the widest span
        of them -- moving it moves all of them, which is the decision behind
        this feature, so showing it twice would imply a control that does not
        exist.

        TWO NAMES PER ROW, and the distinction is the whole point. "name" is
        what the user should SEE: a sample already renamed in this session must
        appear under its new name, or the two dialogs describe the same bank
        differently and the placement list looks like it belongs to another
        preset. "orig" is what everything is KEYED by, because `assemble()`
        looks up both `sample_names` and `zone_placement` against the SOURCE
        sample's name -- the bank on disk has not been rewritten yet, so a map
        keyed by the new name matches nothing and the move is silently lost.
        """
        seen: dict = {}
        order: list = []
        used: dict = {}
        for bank, preset, _label in items:
            for idx, (lo, root, hi, lo_vel, hi_vel, v_start) in \
                    self._zone_ranges(bank, preset).items():
                samp = bank.samples.get(idx)
                if samp is None:
                    continue
                # Deduped by ORIGINAL name: that is the sample's identity here,
                # and two samples can carry the same pending new name.
                if samp.name in seen:
                    prev = seen[samp.name]
                    prev["lo"] = min(prev["lo"], lo)
                    prev["hi"] = max(prev["hi"], hi)
                    continue
                shown = self._sample_renames.get(samp.name, samp.name)
                # The dialog keys its result by the displayed name and matches
                # rows with `next(r for r in rows if r["name"] == name)`, so two
                # rows sharing one would edit each other. Defensive, since both
                # rename paths already disambiguate: fall back to the original,
                # which is unique by construction.
                if shown in used:
                    shown = samp.name
                used[shown] = samp.name
                # No lock any more. A sample sharing a voice used to have its
                # velocity field disabled, because a voice carries ONE window
                # and editing it in place would drag the neighbours along --
                # which made the field dead on every bank built here, since
                # mpc2emu's writer emits one voice per window and an imported
                # folder is a single voice holding every zone (156 of them,
                # measured). assemble() now splits the voice instead, so the
                # window shown is a starting value rather than a shared fate.
                seen[samp.name] = {"name": shown, "orig": samp.name,
                                    "lo": lo, "root": root, "hi": hi,
                                    "lo_vel": lo_vel, "hi_vel": hi_vel}
                order.append(samp.name)
        return [seen[n] for n in order]

    def _zone_ranges(self, bank, preset) -> dict:
        """{sample index: (lo, root, hi, lo_vel, hi_vel, voice_start)} AS A
        READER RESOLVES IT.

        The key range has the voice clamp applied -- `max(voice_lo, zone_lo)`
        and `min(voice_hi, zone_hi)` -- because that is what the instrument
        plays and therefore what the dialog must show. Reading the zone entry
        raw would display a range the hardware never uses.

        VELOCITY is read from the voice and nowhere else. The zone entry has
        velocity bytes and they are (0, 127) on every real bank measured here;
        the layering lives at `vpar[18]`/`vpar[21]`. `voice_start` comes back
        with it so the caller can tell whether two samples share a voice --
        which decides whether their velocity can be edited apart."""
        out: dict = {}
        body = getattr(preset, "body", None)
        if not body or self._format != "E4B":
            return out
        for v_start, table_start, n in e4b._walk_voices(body, preset.num_voices):
            vlo = body[v_start + e4b.VOICE_LO_KEY]
            vhi = body[v_start + e4b.VOICE_HI_KEY]
            vlov = body[v_start + e4b.VOICE_LO_VEL]
            vhiv = body[v_start + e4b.VOICE_HI_VEL]
            for k in range(n):
                eo = table_start + k * e4b.ZONE_ENTRY
                if eo + e4b.ZONE_ENTRY > len(body):
                    break
                idx = struct.unpack_from(">H", body, eo + 10)[0]
                if idx not in bank.samples:
                    continue
                lo = max(vlo, body[eo + e4b.ZONE_LO_KEY])
                hi = min(vhi, body[eo + e4b.ZONE_HI_KEY])
                prev = out.get(idx)
                if prev is None:
                    out[idx] = (lo, body[eo + e4b.ZONE_ROOT_KEY], hi,
                                vlov, vhiv, v_start)
                    continue
                # WIDEST SPAN across every zone using this sample, not the
                # first one found. Skipping the later zones here made the
                # editor under-report a sample spread over several: a GIG
                # instrument with 7 zones on one sample covering keys 24-96
                # showed C0-B0, the first zone alone. The per-sample row is
                # supposed to carry all of them, and _placement_rows' own
                # min/max was dead code because this had already deduped.
                #
                # Root and velocity stay FIRST-WINS: a sample has one root
                # worth showing, and an edit applies to every zone using it,
                # so a second opinion here would only be lost again.
                out[idx] = (min(prev[0], lo), prev[1], max(prev[2], hi),
                            prev[3], prev[4], prev[5])
        return out

    def _adjust_placement(self) -> None:
        items = self._selected_presets()
        rows = self._placement_rows(items)
        if not rows:
            self.statusMessage.emit("No samples to place yet")
            return
        for r in rows:                       # show edits already made
            if r["orig"] in self._zone_placement:
                r["lo"], r["root"], r["hi"] = self._zone_placement[r["orig"]]
            if r["orig"] in self._voice_velocity:
                r["lo_vel"], r["hi_vel"] = self._voice_velocity[r["orig"]]

        # Everything the dialog hands back is keyed by the name it DISPLAYED,
        # which is the renamed one; every map we store is keyed by the source
        # sample's own name, because that is what assemble() looks up. This is
        # the only place the two meet.
        to_orig = {r["name"]: r["orig"] for r in rows}

        dialog = SamplePlacementDialog(rows, octave_offset=_RENAME_OCTAVE,
                                        parent=self, show_velocity=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        # `overrides()` returns EVERY row, not the edited ones, and the values
        # it hands back are the ones we displayed: voice-CLAMPED, and widened
        # to the span of all zones sharing the sample. Storing those verbatim
        # would rewrite the zone bytes of samples nobody touched -- narrowing
        # some, widening others -- so a user who opened the dialog and pressed
        # OK would silently re-place the whole preset. Only real changes are
        # kept.
        before = {r["orig"]: (r["lo"], r["root"], r["hi"]) for r in rows}
        before_vel = {r["orig"]: (r["lo_vel"], r["hi_vel"]) for r in rows}
        self._zone_placement = {k: v for k, v in self._zone_placement.items()
                                 if k not in before}
        for shown_name, moved in dialog.overrides().items():
            orig = to_orig.get(shown_name)
            if orig is not None and moved != before.get(orig):
                self._zone_placement[orig] = moved

        # Same rule as placement: only rows that actually differ. A voice's
        # velocity window is shared by every zone in it, so writing back an
        # unchanged value is not the no-op it looks like once a preset has
        # several voices reading the same sample.
        self._voice_velocity = {k: v for k, v in self._voice_velocity.items()
                                 if k not in before_vel}
        for shown_name, vel in dialog.velocity_overrides().items():
            orig = to_orig.get(shown_name)
            if orig is not None and vel != before_vel.get(orig):
                self._voice_velocity[orig] = vel

        # The dialog's Sample column is editable and returns typed names. It
        # feeds the same rename map the Rename Samples dialog fills, rather
        # than a second one -- two maps for one field is how a rename gets
        # applied by one path and dropped by the other.
        for shown_name, typed in dialog.name_overrides().items():
            orig = to_orig.get(shown_name)
            if orig is not None and typed and typed != orig:
                self._sample_renames[orig] = typed

        self._sync_placement_button()
        self._sync_rename_button()
        self._recompute_timer.start(_RECOMPUTE_DEBOUNCE_MS)

    def _rename_samples(self, selected_only: bool = False) -> None:
        # Always follows the selection, from the button and the context menu
        # alike. They used to differ -- button bank-wide, right-click scoped --
        # which was too clever to guess at: with a preset highlighted, the
        # button still listed every other preset's samples. Nothing selected
        # still means the whole bank, the same rule "Remove Selected" uses in
        # this pane.
        del selected_only                       # kept for call-site clarity
        items = self._selected_presets()
        rows = self._sample_rows_in_bank(items)
        if not rows:
            self.statusMessage.emit("No samples to rename yet")
            return
        renames = SampleRenameDialog.get_renames(rows, fmt=self._format or "E4B",
                                                  octave_offset=_RENAME_OCTAVE,
                                                  existing=self._sample_renames,
                                                  parent=self)
        if renames is None:
            return                     # cancelled: keep whatever was set before
        # MERGED, not replaced. Renaming preset A's samples and then opening
        # preset B used to discard A's work, because the dialog only ever
        # returns what its own rows carried. Renames accumulate across the
        # bank; the dialog is a view onto part of it, so only the names it
        # actually showed may be revised by it.
        shown = {r["name"] for r in rows}
        self._sample_renames = {k: v for k, v in self._sample_renames.items()
                                 if k not in shown}
        self._sample_renames.update(renames)
        self._sync_rename_button()
        # The meter re-runs assemble(), and a rename changes the bytes, so the
        # displayed size has to be recomputed rather than left stale.
        self._recompute_timer.start(_RECOMPUTE_DEBOUNCE_MS)

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
        # Bound the same way as bank_name, so the meter, Save as… and Send to
        # Image all assemble the identical bytes -- a rename visible only in
        # one of the three would be worse than no rename at all. Only for the
        # formats whose assemble() accepts it; _sync_rename_button keeps the
        # dict empty for the others, but binding an argument KRZ's assemble()
        # does not take would be a TypeError rather than a no-op.
        if self._sample_renames and self._format in self._RENAMEABLE:
            fn = functools.partial(fn, sample_names=dict(self._sample_renames))
        # Same binding rule as the renames: bound here so the meter, Save as…
        # and Send to Image all assemble identical bytes.
        if self._zone_placement and self._format in self._PLACEABLE:
            fn = functools.partial(fn, zone_placement=dict(self._zone_placement))
        if self._voice_velocity and self._format in self._PLACEABLE:
            fn = functools.partial(fn, voice_velocity=dict(self._voice_velocity))
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
        last_line = workers.last_error_line(message)
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
