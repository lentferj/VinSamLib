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
    def __init__(self, zones: list[dict], octave_offset: int = 1, parent=None):
        """`zones`: [{"name": str, "lo": int, "root": int, "hi": int}, ...],
        one per sample -- see sampledir_import_dialog.py's caller for how
        these come from an mpc2emu Bank's zones. `octave_offset` is
        display-only (note-name convention), independent of the actual
        key numbers being edited."""
        super().__init__(parent)
        self.setWindowTitle("Sample Placement")
        self.setMinimumSize(880, 480)
        self._octave_offset = octave_offset
        self._rows: list[dict] = [dict(z) for z in zones]
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

        info = QLabel(
            "Override each sample's key range and root note. Rows stay "
            "sorted low to high and reorder automatically if an edit "
            "changes that order. Overlapping ranges turn light red; a row "
            "that can never sound (low above high, or a root outside its "
            "own range) turns a stronger red. Both are warnings only -- OK "
            "still applies whatever is shown.")
        info.setWordWrap(True)
        info.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        layout.addWidget(info)

        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["Sample", "Low", "Root", "High"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, 1)

        self._piano = PianoKeyboardWidget(octave_offset=octave_offset)
        layout.addWidget(self._piano)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._rebuild_table()

    # -- table (re)construction --------------------------------------------------

    def _rebuild_table(self, focus: Optional[tuple[str, int]] = None) -> None:
        self._table.setRowCount(len(self._rows))
        for r, row in enumerate(self._rows):
            name_item = QTableWidgetItem(row["name"])
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled)   # read-only, no editing
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
        self._refresh_warnings()
        self._refresh_piano()

    def _on_value_changed(self, name: str, key: str, value: int) -> None:
        row = next(r for r in self._rows if r["name"] == name)
        row[key] = value
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

    def overrides(self) -> dict[str, tuple[int, int, int]]:
        """{sample_name: (lo_key, root_key, hi_key)} for every row, in its
        current (possibly reordered, possibly overlapping) state."""
        return {row["name"]: (row["lo"], row["root"], row["hi"]) for row in self._rows}
