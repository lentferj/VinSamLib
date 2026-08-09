"""Paste a list of favourite preset numbers and add those presets.

Written against how the notes are actually kept: a spreadsheet column filled
in while auditioning a CD on the hardware, one column per bank. So the input
is a paste box rather than a file picker — the notes live in a cell range, not
in a file with a format worth parsing, and a column pastes as text from any
spreadsheet without an export step.

THE PREVIEW IS THE POINT. The numbers are positions after loading, and on a
K2000 the load point is chosen at load time, so the same program is 205 or 405
depending on where it went. `infer_base` guesses that from the lowest number
and can be wrong. Showing the resolved preset NAMES makes a wrong guess
obvious at once, in a way an "N of M matched" count never would.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                                QListWidget, QPlainTextEdit, QSpinBox,
                                QVBoxLayout)

from ..build import favourites


class FavouritesDialog(QDialog):
    def __init__(self, bank_label: str, fmt: str, preset_names: list[str],
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Favourites from a List")
        self.setMinimumSize(620, 520)
        self._fmt = fmt
        self._names = preset_names
        self._positions: list[int] = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"<b>{bank_label}</b> — {len(preset_names)} preset(s), {fmt}"))

        info = QLabel(
            "Paste the preset numbers you noted on the hardware — one per "
            "line, or however they came out of a spreadsheet. Anything that "
            "is not a number is ignored, so a column heading and blank rows "
            "can stay.\n"
            "The numbers are read as positions in this bank, not as stored "
            "ids: on a K2000 you choose the destination bank when you load, "
            "so what counts is the offset from that load point.")
        info.setWordWrap(True)
        info.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        layout.addWidget(info)

        self._text = QPlainTextEdit()
        self._text.setPlaceholderText("P002\nP008\nP010\n…   or   200\n206\n207")
        self._text.textChanged.connect(self._refresh)
        layout.addWidget(self._text, 2)

        row = QHBoxLayout()
        row.addWidget(QLabel("Bank was loaded starting at preset:"))
        self._base = QSpinBox()
        self._base.setRange(0, 9999)
        self._base.setSingleStep(100)
        # Only meaningful for KRZ; an E4B loaded into an empty machine always
        # starts at 0, and offering a control that must stay 0 invites an
        # edit that can only be wrong.
        self._base.setEnabled(fmt == "KRZ")
        # Programmatic updates go through blockSignals, so this fires only
        # on a real edit -- which is exactly when the guess must stop.
        self._base.valueChanged.connect(self._on_base_edited)
        row.addWidget(self._base)
        row.addStretch(1)
        layout.addLayout(row)

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        layout.addWidget(QLabel("These presets will be added:"))
        self._preview = QListWidget()
        layout.addWidget(self._preview, 3)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)
        self._refresh()

    def _refresh(self) -> None:
        numbers = favourites.parse_numbers(self._text.toPlainText())
        if numbers and not self._base_touched():
            # Follow the paste until the user takes the wheel: re-guessing
            # after they have set it by hand would undo their correction on
            # the next keystroke.
            self._base.blockSignals(True)
            self._base.setValue(favourites.infer_base(numbers, self._fmt))
            self._base.blockSignals(False)
        base = self._base.value()
        self._positions, missing = favourites.resolve(
            numbers, base, len(self._names))
        self._summary.setText(
            favourites.describe(self._fmt, base, self._positions, missing,
                                 len(self._names))
            if numbers else "Nothing pasted yet.")
        self._preview.clear()
        for pos in self._positions:
            self._preview.addItem(f"{base + pos:>4}   {self._names[pos]}")
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            bool(self._positions))

    def _on_base_edited(self, _value: int) -> None:
        self._base.setProperty("touched", True)
        self._refresh()

    def _base_touched(self) -> bool:
        return self._base.property("touched") is True

    def positions(self) -> list[int]:
        """Indices into the bank's preset list, in the order pasted."""
        return list(self._positions)
