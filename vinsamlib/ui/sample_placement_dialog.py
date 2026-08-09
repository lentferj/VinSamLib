"""
Sample Placement dialog: manual override of each WAV's auto-computed key
placement (low border / root note / high border) before a Sample Folder
import commits to it -- reachable via that dialog's own "Adjust Sample
Placement..." button (ui/sampledir_import_dialog.py), never from
anywhere else; parse_sample_dir()'s own auto-mapping (split at the
midpoints between adjacent roots) is usually right, this is for the
cases it isn't.

A simple box matrix -- one row per sample, columns Sample / Low / Root /
High -- next to an 88-key piano image coloring each sample's range in
its own color (same color both places). Rows are kept sorted low-to-high
and reorder live if an edit changes that relative order. Two kinds of
trouble are flagged by tinting the offending rows' note fields: ranges
that overlap each other (light red) and rows that aren't a playable zone
at all -- low above high, or a root outside its own range (stronger
red). Both are warnings only, never blocking OK/Continue: real hardware
samplers do sometimes use intentional overlapping zones for layering, so
that stays the user's judgment call rather than an error mpc2emu or
VinSamLib enforces, and the same "show, don't block" rule is kept for
the unplayable case so one bad row can't trap a user in the dialog.
"""

from __future__ import annotations

import functools
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView,
                             QLabel, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout)

from .note_naming import midi_to_name, name_to_midi
from .piano_keyboard import PianoKeyboardWidget

# Full MIDI key range -- what a real E4B/KRZ/EIII zone (and
# parse_sample_dir()'s own 0/127 outer bounds) actually spans. The piano
# widget's narrower 88-key A0-C8 span is a DRAWING limit only.
MIDI_MIN, MIDI_MAX = 0, 127

# Two ranges [lo1,hi1]/[lo2,hi2] overlap iff lo1 <= hi2 and lo2 <= hi1 --
# the standard interval-intersection test.
_OVERLAP_BG = "#ffb3b3"

# A row whose own numbers don't form a playable zone: lo above hi (an
# inverted, empty range), or a root note outside the range it belongs to.
# Deliberately a stronger red than _OVERLAP_BG: overlapping zones are a
# legitimate layering technique, but these never sound on real hardware,
# and note that the interval test above can NEVER flag an inverted range
# (lo > hi fails both of its comparisons), so this is not redundant with
# it. Still only a warning -- OK stays enabled, same as for overlaps.
_INVALID_BG = "#ff6b6b"


def _contrasting_text(color: QColor) -> QColor:
    luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return QColor(0, 0, 0) if luminance > 140 else QColor(255, 255, 255)


def _baseline_name(row: dict) -> str:
    """What this row is called before anyone types in it.

    `scheme_name` is the name the caller's own naming scheme will give this
    sample (build/sample_names.names_from_base). When it is supplied, THAT is
    what the editor must show -- showing the pre-scheme name meant editing
    names in a dialog that displayed something other than what the import
    would produce.

    Deliberately kept apart from `new_name`. Seeding `new_name` with the
    scheme name would have shown the right text too, but name_overrides()
    reports every row with a `new_name` as hand-typed, and a hand-typed name
    beats the scheme -- so changing the base name afterwards would silently
    stop working. `name` remains the row's identity throughout, whatever is
    displayed on top of it.
    """
    return row.get("scheme_name") or row["name"]


def _shown_name(row: dict) -> str:
    """The text in the name cell: a hand-typed name if there is one, else the
    scheme's, else the original."""
    return row.get("new_name") or _baseline_name(row)


class NoteSpinBox(QSpinBox):
    """A QSpinBox whose displayed text is a note name (e.g. "C3", "F#4")
    over the real MIDI value, using the SAME octave-offset convention as
    build/sampledir_import.py's own octave_offset ("Middle C is:").

    Range is the FULL MIDI 0-127, deliberately NOT the 88-key 21-108 span
    the piano widget draws: parse_sample_dir() gives its lowest zone
    lo_key=0 and its highest hi_key=127 so the preset covers the whole
    keyboard, and E4B/KRZ/EIII zones are 0-127 too. Clamping the editor to
    21-108 silently rewrote exactly those two outer zones -- the spinbox
    displayed A0/C8 while the model still held 0/127, and one arrow-press
    on such a field jumped it to 22 (dropping MIDI 0-21 coverage) instead
    of stepping by a semitone. The piano widget clips its own DRAWING to
    the 88 keys it has (see its set_zones()/paintEvent), which is the right
    place for that limit -- the data model must not be narrowed to it."""

    def __init__(self, octave_offset: int, parent=None):
        super().__init__(parent)
        self._octave_offset = octave_offset
        self.setRange(MIDI_MIN, MIDI_MAX)

    def textFromValue(self, value: int) -> str:
        return midi_to_name(value, self._octave_offset)

    def valueFromText(self, text: str) -> int:
        midi = name_to_midi(text, self._octave_offset)
        return midi if midi is not None else self.value()


class SamplePlacementDialog(QDialog):
    def __init__(self, zones: list[dict], octave_offset: int = 1, parent=None,
                  show_velocity: bool = False):
        """`zones`: [{"name": str, "lo": int, "root": int, "hi": int}, ...],
        one per sample -- see sampledir_import_dialog.py's caller for how
        these come from an mpc2emu Bank's zones. `octave_offset` is
        display-only (note-name convention), independent of the actual
        key numbers being edited.

        `show_velocity` adds "Vel lo"/"Vel hi" columns, reading `lo_vel` and
        `hi_vel` off each row. OFF by default, and deliberately so: a folder
        being imported has no velocity information to show -- mpc2emu's
        sampledir parser writes 0-127 on every zone -- so the columns would
        be four identical numbers per row and an invitation to set something
        the import cannot carry. Only New Bank, editing a real bank whose
        voices have real windows, turns them on. A row may also carry
        `vel_locked`: a reason string, which greys that row's velocity
        fields and explains itself in the tooltip.
        """
        super().__init__(parent)
        self.setWindowTitle("Sample Placement")
        self.setMinimumSize(880, 480)
        self._octave_offset = octave_offset
        self._show_velocity = show_velocity
        self._rows: list[dict] = [dict(z) for z in zones]
        if show_velocity:
            for r in self._rows:
                r.setdefault("lo_vel", 0)
                r.setdefault("hi_vel", 127)
        self._rows.sort(key=lambda r: r["lo"])

        # Colors assigned once, keyed by sample name, from the INITIAL
        # low-to-high order -- kept stable across later reorders so a
        # sample's color never changes mid-edit (that would be far more
        # confusing than the row simply moving).
        n = max(1, len(self._rows))
        self._colors = {
            row["name"]: QColor.fromHsv(int(360 * i / n), 190, 230)
            for i, row in enumerate(self._rows)
        }

        layout = QVBoxLayout(self)

        text = ("Override each sample's key range and root note. Rows stay "
                "sorted low to high and reorder automatically if an edit "
                "changes that order. Overlapping ranges turn light red; a row "
                "that can never sound (low above high, or a root outside its "
                "own range) turns a stronger red. Both are warnings only -- OK "
                "still applies whatever is shown.")
        if show_velocity:
            # Say why a field is grey IN THE DIALOG. A disabled spin box with
            # the reason only in its tooltip reads as a broken feature, and
            # every bank built by the sample-folder import is this case --
            # mpc2emu's writer puts every zone in one voice.
            text += ("\nVel lo / Vel hi are the velocity window the sample "
                     "answers to. These belong to the VOICE, not the zone, so "
                     "they are greyed where one voice holds several samples: "
                     "there is a single window and changing it would move the "
                     "others too. Overlapping key ranges are normal once "
                     "samples are separated by velocity — that is what "
                     "layering is.")
        info = QLabel(text)
        info.setWordWrap(True)
        info.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        layout.addWidget(info)

        self._table = QTableWidget()
        heads = ["Sample", "Low", "Root", "High"]
        if show_velocity:
            heads += ["Vel lo", "Vel hi"]
        self._table.setColumnCount(len(heads))
        self._table.setHorizontalHeaderLabels(heads)
        self._table.verticalHeader().setVisible(False)
        # The key columns are spin-box CELL WIDGETS, which work regardless of
        # these two -- but the Sample column is a plain editable item, and
        # NoEditTriggers plus NoSelection made it impossible to type into.
        # That is this dialog's own per-row rename feature, unreachable since
        # the day it was added: its test set the text programmatically, which
        # bypasses the view entirely.
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, 1)

        self._piano = PianoKeyboardWidget(octave_offset=octave_offset)
        layout.addWidget(self._piano)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._table.itemChanged.connect(self._on_name_edited)
        self._rebuild_table()

    # -- table (re)construction --------------------------------------------------

    def _rebuild_table(self, focus: Optional[tuple[str, int]] = None) -> None:
        # Signals off while filling: setItem() emits itemChanged, which would
        # otherwise read half-built rows as user edits.
        self._table.blockSignals(True)
        self._table.setRowCount(len(self._rows))
        for r, row in enumerate(self._rows):
            name_item = QTableWidgetItem(_shown_name(row))
            # Editable: the name a sample gets on the hardware is worth as much
            # as its key range, and this is the one place the whole list is in
            # front of the user. The ORIGINAL name stays the row's identity --
            # placement overrides key on it, and so does the colour map.
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable)
            name_item.setData(Qt.ItemDataRole.UserRole, row["name"])
            color = self._colors[row["name"]]
            name_item.setBackground(color)
            name_item.setForeground(_contrasting_text(color))
            self._table.setItem(r, 0, name_item)

            for col, key in ((1, "lo"), (2, "root"), (3, "hi")):
                spin = NoteSpinBox(self._octave_offset)
                spin.setValue(row[key])
                spin.valueChanged.connect(
                    functools.partial(self._on_value_changed, row["name"], key))
                self._table.setCellWidget(r, col, spin)
                if focus == (row["name"], col):
                    spin.setFocus()

            if not self._show_velocity:
                continue
            # Velocity is a plain 0-127 number, not a note, so these are
            # ordinary spin boxes rather than NoteSpinBox -- showing "C3" for
            # a velocity would be nonsense.
            locked = row.get("vel_locked")
            for col, key in ((4, "lo_vel"), (5, "hi_vel")):
                vspin = QSpinBox()
                vspin.setRange(0, 127)
                vspin.setValue(int(row.get(key, 0 if key == "lo_vel" else 127)))
                if locked:
                    vspin.setEnabled(False)
                    vspin.setToolTip(locked)
                else:
                    vspin.valueChanged.connect(functools.partial(
                        self._on_value_changed, row["name"], key))
                self._table.setCellWidget(r, col, vspin)
                if focus == (row["name"], col):
                    vspin.setFocus()
        self._table.blockSignals(False)
        self._refresh_warnings()
        self._refresh_piano()

    def _on_name_edited(self, item) -> None:
        """A name cell was typed into. Recorded against the row's ORIGINAL
        name, which stays its identity everywhere else."""
        if item.column() != 0:
            return
        original = item.data(Qt.ItemDataRole.UserRole)
        if original is None:
            return
        row = next((r for r in self._rows if r["name"] == original), None)
        if row is None:
            return
        # Compared against what the cell ALREADY SHOWED, not against the row's
        # identity. Two reasons it has to be the shown text: mpc2emu pads names
        # to the field width, so a row nobody touched would read as renamed the
        # moment its trailing spaces were stripped; and when the caller supplied
        # a `scheme_name`, the cell showed that rather than the original, so
        # comparing against the original would record every untouched row as a
        # hand-typed override -- which then WINS over the scheme, and a base
        # name changed afterwards would silently stop taking effect.
        typed = item.text().strip()
        baseline = _baseline_name(row).strip()
        row["new_name"] = typed if typed and typed != baseline else ""
        self._refresh_name_warnings()

    def _refresh_name_warnings(self) -> None:
        """Two things a typed name can be: too long for the 16-character field,
        or the same as another row's. Neither loses audio -- the import applies
        names through mpc2emu's own uniquifier -- but a user who typed one name
        and gets another should see it before pressing OK."""
        # Compared stripped: mpc2emu pads names to the field width, so
        # "Clap Drumulator" and "Clap Drumulator " are the same name once
        # written -- a clash check that missed that would pass the user
        # straight into the collision it exists to prevent.
        final = {row["name"]: _shown_name(row).strip() for row in self._rows}
        clashing = {n for n in final.values() if list(final.values()).count(n) > 1}
        for r, row in enumerate(self._rows):
            item = self._table.item(r, 0)
            if item is None:
                continue
            name = final[row["name"]]
            trouble = []
            if len(name) > 16:
                trouble.append(f"{len(name)} characters — the field holds 16")
            if name in clashing:
                trouble.append("another sample has this name — it will be numbered apart")
            item.setToolTip("; ".join(trouble) if trouble else name)
            font = item.font()
            font.setItalic(bool(trouble))
            item.setFont(font)

    def name_overrides(self) -> dict:
        """{original sample name: typed name} for the rows that were edited."""
        return {row["name"]: row["new_name"] for row in self._rows if row.get("new_name")}

    def _on_value_changed(self, name: str, key: str, value: int) -> None:
        row = next(r for r in self._rows if r["name"] == name)
        row[key] = value
        if key in ("lo_vel", "hi_vel"):
            # Velocity changes neither the low-to-high sort nor the piano, and
            # two samples overlapping in KEY are perfectly normal once they
            # are separated by velocity -- that is what layering IS. Repainting
            # the key warnings here would flag correct layering as a fault.
            self._refresh_warnings()
            return
        old_order = [r["name"] for r in self._rows]
        new_rows = sorted(self._rows, key=lambda r: r["lo"])
        new_order = [r["name"] for r in new_rows]
        if new_order != old_order:
            # The edit changed relative low-to-high order -- rebuild so the
            # rows visually reorder, restoring focus to the field the user
            # was just editing (now at its new row position).
            self._rows = new_rows
            col = {"lo": 1, "root": 2, "hi": 3}[key]
            self._rebuild_table(focus=(name, col))
        else:
            # Same order: update styling/piano in place, without touching
            # any widget identity (avoids stealing focus/clicks mid-edit).
            self._refresh_warnings()
            self._refresh_piano()

    def _refresh_warnings(self) -> None:
        n = len(self._rows)
        overlapping = set()
        for i in range(n):
            a = self._rows[i]
            for j in range(i + 1, n):
                b = self._rows[j]
                if a["lo"] <= b["hi"] and b["lo"] <= a["hi"]:
                    overlapping.add(a["name"])
                    overlapping.add(b["name"])
        invalid = {row["name"] for row in self._rows
                   if row["lo"] > row["hi"] or not (row["lo"] <= row["root"] <= row["hi"])}
        for r, row in enumerate(self._rows):
            if row["name"] in invalid:
                style = f"background-color: {_INVALID_BG};"
            elif row["name"] in overlapping:
                style = f"background-color: {_OVERLAP_BG};"
            else:
                style = ""
            for col in (1, 2, 3):
                widget = self._table.cellWidget(r, col)
                if widget is not None:
                    widget.setStyleSheet(style)

    def _refresh_piano(self) -> None:
        self._piano.set_zones([
            {"lo": row["lo"], "root": row["root"], "hi": row["hi"], "color": self._colors[row["name"]]}
            for row in self._rows
        ])

    # -- result --------------------------------------------------------------

    def velocity_overrides(self) -> dict[str, tuple[int, int]]:
        """{sample_name: (lo_vel, hi_vel)} for every row, when the velocity
        columns are shown; empty otherwise. Like `overrides()` this reports
        EVERY row, not the edited ones -- the caller compares against what it
        supplied."""
        if not self._show_velocity:
            return {}
        return {row["name"]: (int(row.get("lo_vel", 0)),
                              int(row.get("hi_vel", 127)))
                for row in self._rows if not row.get("vel_locked")}

    def overrides(self) -> dict[str, tuple[int, int, int]]:
        """{sample_name: (lo_key, root_key, hi_key)} for every row, in its
        current (possibly reordered, possibly overlapping) state."""
        return {row["name"]: (row["lo"], row["root"], row["hi"]) for row in self._rows}
