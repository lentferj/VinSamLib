"""
Sample-folder import dialog: XpmImportDialog's target-format picker plus
resample/reduce sections, with one addition specific to a bare WAV
folder -- an explicit "middle C" octave-convention picker. An XPM's own
zones already carry real MIDI key numbers; a folder of loose WAVs
doesn't, so mpc2emu's parse_sample_dir() has to guess where "middle C"
falls from the filenames themselves (majority vote, its own CLI default)
unless told directly. Auto-detect is offered as the default choice, but
an explicit C3/C4/C5 override is here for when auto-detect would guess
wrong and there's no cheap way to preview the outcome first.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QWidget

from .xpm_import_dialog import XpmImportDialog
from ..build.convert import ConversionOptions

# QComboBox row index -> parsers.sampledir_parser.parse_sample_dir()'s own
# octave_offset convention (2=C3, 1=C4, 0=C5; None lets it auto-detect).
_OCTAVE_CHOICES: list[tuple[str, Optional[int]]] = [
    ("Auto-detect", None),
    ("C3 (K2000/vintage convention)", 2),
    ("C4 (general MIDI convention)", 1),
    ("C5", 0),
]

_DEFAULT_WARNING = (
    "Importing goes through mpc2emu's own model, same as any other "
    "conversion here. Each WAV is auto-mapped to the keys nearest its "
    "filename's root note, key-tracked, into one multisampled preset. "
    "Resample/reduce below are optional and off by default for either "
    "target format.")


class SampleDirImportDialog(XpmImportDialog):
    def __init__(self, parent=None, initial: Optional[ConversionOptions] = None,
                 title: str = "Import Sample Folder", warning_text: Optional[str] = None,
                 locked_format: Optional[str] = None):
        super().__init__(parent, initial=initial, title=title,
                          warning_text=warning_text or _DEFAULT_WARNING,
                          locked_format=locked_format)

        octave_row = QWidget()
        row_layout = QHBoxLayout(octave_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(QLabel("Middle C is:"))
        self._octave_box = QComboBox()
        self._octave_box.addItems([label for label, _offset in _OCTAVE_CHOICES])
        row_layout.addWidget(self._octave_box)
        row_layout.addStretch()
        # Index 0 is the format-picker row XpmImportDialog just inserted.
        self.layout().insertWidget(1, octave_row)

    def octave_offset(self) -> Optional[int]:
        return _OCTAVE_CHOICES[self._octave_box.currentIndex()][1]

    @staticmethod
    def get_import_options(parent=None, initial: Optional[ConversionOptions] = None,
                            title: str = "Import Sample Folder", warning_text: Optional[str] = None,
                            locked_format: Optional[str] = None
                            ) -> tuple[Optional[ConversionOptions], Optional[int]]:
        """Returns (opts, octave_offset) -- None, None if cancelled. Two
        values, unlike XpmImportDialog.get_import_options()'s one, since
        the octave convention isn't part of ConversionOptions (it only
        matters at parse time, before there's a Bank to apply resample/
        reduce options to at all)."""
        dialog = SampleDirImportDialog(parent, initial=initial, title=title,
                                        warning_text=warning_text, locked_format=locked_format)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None, None
        return dialog._to_options(), dialog.octave_offset()
