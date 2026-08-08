"""
"Sample names" section of the import dialogs: one base name, and every
sample named `<base>-<key>` after the key it plays.

Why generate rather than let a user type 40 names (that is the next step):
the names an import produces are the source filenames shortened to the 16
characters an E4B/KRZ field holds, which is exactly where they stop being
readable -- `Drumulator Clea1`, `o sampled-081 A1`. What is wanted on the
hardware is `Rhodes-C3`, `Rhodes-F3`. The key is the one thing that reliably
tells a multisample's samples apart, and it is already known per zone.

The section collects only the base name. The mapping is built after the
parse (build/sample_names.names_from_base), because root notes do not exist
before it -- parsing a folder just to preview names would undo the laziness
these dialogs are built around.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QGroupBox, QLabel, QLineEdit,
                                QVBoxLayout, QWidget)

from ..notes import midi_to_name

#: What an E4B/KRZ sample-name field holds. The key suffix eats into it:
#: "-C#-2" is five characters, so a long base cannot be honoured in full.
NAME_LIMIT = 16
#: Shown in the preview -- middle C, whatever the octave convention calls it.
_PREVIEW_ROOTS = (60, 65)


class SampleNamesWidget(QGroupBox):
    """Checkable section: off means "keep whatever the conversion produced"."""

    changed = Signal()

    def __init__(self, octave_offset: int = 2, suggested: str = "",
                 parent: Optional[QWidget] = None):
        super().__init__("Sample names", parent)
        self.setCheckable(True)
        self.setChecked(False)
        self._octave_offset = octave_offset

        layout = QVBoxLayout(self)
        grid = QGridLayout()
        grid.addWidget(QLabel("Name samples:"), 0, 0)
        self._base = QLineEdit(suggested)
        self._base.setPlaceholderText("e.g. Rhodes")
        self._base.setToolTip(
            "Every sample is renamed <base>-<key>, after the key it plays.\n"
            "Leave the section unchecked to keep the names the conversion gives them.")
        grid.addWidget(self._base, 0, 1)
        layout.addLayout(grid)

        # E4B keeps a second name field of its own, already suffixed with the
        # note (e4b_writer._sample_display_name), so the key is optional there
        # and load-bearing for KRZ/EIII -- without it every sample would be
        # called the same thing and end up numbered.
        self._with_key = QCheckBox("Add the key each sample plays (Rhodes-C3)")
        self._with_key.setChecked(True)
        self._with_key.setToolTip(
            "Off names every sample the same, which only makes sense for E4B —\n"
            "its own sample-name field already carries the note.")
        self._with_key.toggled.connect(self._refresh)
        layout.addWidget(self._with_key)

        self._preview = QLabel()
        self._preview.setWordWrap(True)
        self._preview.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._preview)

        self._base.textChanged.connect(self._refresh)
        self.toggled.connect(self._refresh)
        self._refresh()

    def set_octave_offset(self, octave_offset: Optional[int]) -> None:
        """Follow the dialog's "Middle C is:" picker: a preview that said C3
        where the import wrote C4 would be worse than no preview at all."""
        if octave_offset is not None:
            self._octave_offset = octave_offset
            self._refresh()

    def base_name(self) -> str:
        """What to pass as `name_base`, or "" when the section is off."""
        return self._base.text().strip() if self.isChecked() else ""

    def with_key(self) -> bool:
        """What to pass as `name_with_key`."""
        return self._with_key.isChecked()

    def _refresh(self) -> None:
        self.changed.emit()
        if not self.isChecked():
            self._preview.setText("Samples keep the names the conversion gives them.")
            return
        base = self._base.text().strip()
        if not base:
            self._preview.setText("Enter a name to use for every sample.")
            return
        if not self._with_key.isChecked():
            self._preview.setText(
                f"Every sample will be named {base!r} — E4B adds the note itself; "
                f"for KRZ or EIII they would be numbered apart.")
            return
        keys = [midi_to_name(r, self._octave_offset).replace("#", "s") for r in _PREVIEW_ROOTS]
        example = ", ".join(f"{base}-{k}" for k in keys)
        text = f"Samples will be named: {example}, …"
        longest = len(base) + 1 + max(len(k) for k in keys) + 1   # room for a sharp
        if longest > NAME_LIMIT:
            text += (f"\n⚠️ {longest} characters — a name field holds {NAME_LIMIT}, "
                     f"so the end will be cut. Try a shorter base.")
        self._preview.setText(text)
