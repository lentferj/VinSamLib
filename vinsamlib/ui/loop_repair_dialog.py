"""Choose, per sample, what to do about a loop that clicks.

Nothing here is pre-selected. Every row defaults to "Leave as authored", so
pressing OK without touching anything is a no-op — the same rule the Rename
and Placement dialogs follow, and for a sharper reason: a clicking loop may be
deliberate. Percussive and rhythmic material clicks on purpose, and
ConvertWithMoss found 17 of 152 presets clicking across three commercial
libraries, which is too many to be all mistakes.

THE ROWS ARE NOT A LIST OF DEFECTS, and the wording avoids saying they are.
They are loops whose wrap steps far enough to be audible, measured against how
fast the waveform is already moving there (banks/loopcheck.py). The user knows
their material; this dialog knows arithmetic.

WHY THE THREE REPAIRS ARE NOT RANKED. Their measured success rates differ a
great deal — 64%, 99%, 100% on 101 real clicking K2000 loops — and showing
them in that order with the best one preselected would be the obvious design
and the wrong one. The most effective repair is the only lossy one: a
cross-fade destroys the frames it smooths, and it is irreversible in the sense
that matters, since the built bank is what gets written to media. The two that
only move loop POINTS cannot make the audio wrong at all. So they are offered
in increasing order of what they cost, the lossy one is labelled as such in
its own text rather than in a footnote, and none of them is chosen for anyone.

Nothing written here touches the library. The repair is applied while the new
bank is assembled, exactly like a rename: the source file on disk is never
modified by this program.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog,
                               QDialogButtonBox, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ..banks import loopcheck

#: The empty choice comes first and is the default for every row.
_CHOICES: list[tuple[str, str]] = [
    ("", "Leave as authored"),
    ("snap", "Snap both points to zero crossings"),
    ("nudge", "Nudge the loop end to match the start"),
    ("fade", "Cross-fade the wrap  (rewrites audio)"),
]

#: Button captions for "apply to all"; the full wording is the combo's.
_SHORT = {"snap": "Snap", "nudge": "Nudge", "fade": "Cross-fade"}

_HINTS = {
    "": "The loop is written to the new bank exactly as the source has it.",
    "snap": "Moves both loop points to the nearest zero crossing of the same "
            "slope. Audio untouched. Cleared the click in about two thirds of "
            "the loops it was measured on — it is the least invasive and the "
            "least reliable.",
    "nudge": "Searches backwards for the point where the waveform best matches "
             "the loop start, and moves the loop END there. Audio untouched; "
             "the loop gets slightly shorter. Cleared the click in almost "
             "every loop measured.",
    "fade": "Blends the frames approaching the loop end into the frames before "
            "the loop start, so the wrap is continuous. Always works. THIS "
            "REWRITES AUDIO in the new bank — the original frames in the fade "
            "window are gone, and the source file on disk is untouched.",
}


class LoopRepairDialog(QDialog):
    """Per-sample repair choices. `rows` are dicts with 'name', 'step_pct'
    and optionally 'presets' (labels the sample appears in)."""

    def __init__(self, rows: list[dict], existing: Optional[dict] = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Clicking Loops")
        self._rows = rows

        layout = QVBoxLayout(self)
        intro = QLabel(
            f"{len(rows)} sample(s) have a loop that steps audibly where it "
            f"wraps.\nNothing is changed unless you choose a repair — and a "
            f"loop that clicks may well be intended.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._table = QTableWidget(len(rows), 3, self)
        self._table.setHorizontalHeaderLabels(["Sample", "Step", "Repair"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)

        self._combos: list[QComboBox] = []
        for r, row in enumerate(rows):
            name = row["name"]
            item = QTableWidgetItem(name)
            where = row.get("presets") or []
            if where:
                item.setToolTip("Used by: " + ", ".join(sorted(where))
                                + "\nA repair is keyed by sample, so it "
                                  "applies everywhere the sample is used.")
            self._table.setItem(r, 0, item)

            # The step as a percentage of the local peak: the number that
            # corresponds to how loud the tick actually is, rather than the
            # raw sample-value difference, which means nothing on its own.
            step = QTableWidgetItem(f"{row['step_pct']:.0f}%")
            step.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(r, 1, step)

            combo = QComboBox()
            for kind, label in _CHOICES:
                combo.addItem(label, kind)
            prev = (existing or {}).get(name, "")
            idx = combo.findData(prev)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.currentIndexChanged.connect(self._update_hint)
            self._table.setCellWidget(r, 2, combo)
            self._combos.append(combo)

        layout.addWidget(self._table)

        # Shown only once a repair is actually chosen. Always-on warnings are
        # read once and then not at all; this one appears at the moment it
        # becomes true, which is also the moment the user can still change
        # their mind. Same standing as renaming and placement: exercised
        # against real banks, never played on a sampler.
        self._experimental = QLabel(
            "⚠ Repairs are EXPERIMENTAL — no sampler has yet played a loop "
            "repaired this way. Your source file is never modified; keep the "
            "bank you build until you have heard it.")
        self._experimental.setWordWrap(True)
        self._experimental.setStyleSheet("color: #c07000;")
        self._experimental.setVisible(False)
        layout.addWidget(self._experimental)

        self._hint = QLabel(_HINTS[""])
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color: palette(placeholdertext);")
        layout.addWidget(self._hint)

        # Applying one repair to every row is a genuine convenience -- a
        # multisample usually wants the same treatment throughout -- but it is
        # offered as an explicit button rather than as a header default, so it
        # cannot happen by accident.
        # In a row rather than stacked full-width: three of them down the
        # dialog read as the primary controls, which they are not -- the
        # per-row combos are.
        all_row = QHBoxLayout()
        all_row.addWidget(QLabel("Apply to all:"))
        for kind, label in _CHOICES[1:]:
            btn = QPushButton(_SHORT[kind])
            btn.setToolTip(label)
            btn.clicked.connect(lambda _=False, k=kind: self._set_all(k))
            all_row.addWidget(btn)
        all_row.addStretch(1)
        reset = QPushButton("None")
        reset.setToolTip("Leave every loop as authored")
        reset.clicked.connect(lambda: self._set_all(""))
        all_row.addWidget(reset)
        layout.addLayout(all_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(620, 420)
        self._update_hint()

    def _set_all(self, kind: str) -> None:
        for combo in self._combos:
            i = combo.findData(kind)
            if i >= 0:
                combo.setCurrentIndex(i)

    def _update_hint(self) -> None:
        """Describe whichever repair the user is currently looking at."""
        kinds = {c.currentData() for c in self._combos}
        self._experimental.setVisible(
            any(k in loopcheck.REPAIRS for k in kinds))
        kind = kinds.pop() if len(kinds) == 1 else None
        if kind is None:
            self._hint.setText("Different repairs chosen for different samples.")
        else:
            self._hint.setText(_HINTS.get(kind, ""))

    def repairs(self) -> dict:
        """{sample name: kind} for the rows the user actually set.

        Rows left at "Leave as authored" are OMITTED rather than returned with
        an empty value. The placement dialog's habit of returning every row it
        displayed meant pressing OK with nothing edited re-placed the whole
        preset while every assertion still passed; an omitted key here cannot
        do that, because assemble() only repairs names it is given.
        """
        out = {}
        for row, combo in zip(self._rows, self._combos):
            kind = combo.currentData()
            if kind in loopcheck.REPAIRS:
                out[row["name"]] = kind
        return out

    @staticmethod
    def get_repairs(rows: list[dict], existing: Optional[dict] = None,
                    parent=None) -> Optional[dict]:
        """The dict, or None if cancelled."""
        dlg = LoopRepairDialog(rows, existing=existing, parent=parent)
        if dlg.exec() != QDialog.Accepted:
            return None
        return dlg.repairs()
