"""
Sample-folder import dialog: FormatConvertDialog's target-format picker plus
resample/reduce sections, with two additions specific to a bare WAV
folder -- an explicit "middle C" octave-convention picker, and an
"Adjust Sample Placement..." button for manually overriding the
auto-computed key range/root of individual samples (see
sample_placement_dialog.py). An XPM's own zones already carry real MIDI
key numbers; a folder of loose WAVs doesn't, so mpc2emu's
parse_sample_dir() has to guess where "middle C" falls from the
filenames themselves (majority vote, its own CLI default) unless told
directly, and its key-range split (midpoints between adjacent roots) is
usually right but not always what the user actually wants.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
                             QPushButton, QWidget)

from .format_convert_dialog import FormatConvertDialog
from .sample_placement_dialog import SamplePlacementDialog
from ..build.convert import ConversionOptions

# Fallback note-name display convention (C4=60) when the user left "Middle
# C is:" on Auto-detect -- parse_sample_dir() resolves its own real octave
# offset internally in that case but doesn't return it, so there is no
# single concrete offset to display note names in; this only affects how
# Sample Placement's fields are LABELED, never the actual key numbers.
_DISPLAY_OCTAVE_OFFSET_FALLBACK = 1

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


class SampleDirImportDialog(FormatConvertDialog):
    def __init__(self, parent=None, initial: Optional[ConversionOptions] = None,
                 title: str = "Import Sample Folder", warning_text: Optional[str] = None,
                 locked_format: Optional[str] = None,
                 sample_loader: Optional[Callable[[Optional[int]], list]] = None,
                 placement_loader: Optional[Callable[[Optional[int]], Any]] = None):
        super().__init__(parent, initial=initial, title=title,
                          warning_text=warning_text or _DEFAULT_WARNING,
                          locked_format=locked_format)

        self._placement_loader = placement_loader
        self._zone_overrides: Optional[dict] = None

        octave_row = QWidget()
        row_layout = QHBoxLayout(octave_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(QLabel("Middle C is:"))
        self._octave_box = QComboBox()
        self._octave_box.addItems([label for label, _offset in _OCTAVE_CHOICES])
        row_layout.addWidget(self._octave_box)
        row_layout.addStretch()
        # Index 0 is the format-picker row FormatConvertDialog just inserted.
        self.layout().insertWidget(1, octave_row)

        placement_row = QWidget()
        placement_layout = QHBoxLayout(placement_row)
        placement_layout.setContentsMargins(0, 0, 0, 0)
        self._placement_button = QPushButton("Adjust Sample Placement…")
        self._placement_button.setEnabled(placement_loader is not None)
        self._placement_button.clicked.connect(self._on_adjust_placement_clicked)
        placement_layout.addWidget(self._placement_button)
        self._placement_status = QLabel("Auto-computed placement (default)")
        self._placement_status.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        placement_layout.addWidget(self._placement_status, 1)
        self.layout().insertWidget(2, placement_row)

        # sample_loader/placement_loader both take the LIVE octave_offset
        # (this dialog's own choice), unlike ConvertOptionsDialog's plain
        # zero-arg bank_loader -- parse_sample_dir()'s root-note detection
        # depends on it, so both must re-read the combo box's current
        # value each time, not whatever it was when the dialog opened.
        # Wired up here (after the octave widget exists, after the base
        # constructor already built the Stereo group against a bank_loader
        # of None) rather than threaded through as a constructor arg to
        # __init__, since __init__ has no octave_offset() to close over yet.
        if sample_loader is not None:
            self._bank_loader = lambda: sample_loader(self.octave_offset())
            self._test_button.setEnabled(True)
            self._test_button.setToolTip(
                "Check the actual samples for stereo content and, for Mix, "
                "whether averaging would cancel signal on any of them.")

    def octave_offset(self) -> Optional[int]:
        return _OCTAVE_CHOICES[self._octave_box.currentIndex()][1]

    def zone_overrides(self) -> Optional[dict]:
        return self._zone_overrides

    def _on_adjust_placement_clicked(self) -> None:
        if self._placement_loader is None:
            return
        self._placement_button.setEnabled(False)
        try:
            bank = self._placement_loader(self.octave_offset())
        except Exception as ex:
            QMessageBox.warning(self, "Adjust Sample Placement",
                                 f"Couldn't parse the folder:\n\n{ex}")
            return
        finally:
            self._placement_button.setEnabled(True)

        zones = bank.presets[0].voices[0].zones
        rows = [{"name": z.sample_name, "lo": z.lo_key, "root": z.root_key, "hi": z.hi_key}
                for z in zones]
        display_octave = self.octave_offset()
        if display_octave is None:
            display_octave = _DISPLAY_OCTAVE_OFFSET_FALLBACK
        dialog = SamplePlacementDialog(rows, octave_offset=display_octave, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._zone_overrides = dialog.overrides()
            self._placement_status.setText(
                f"Custom placement set for {len(self._zone_overrides)} sample(s)")
        # Cancel: whatever override (if any) was already set stays as-is.

    @staticmethod
    def get_import_options(parent=None, initial: Optional[ConversionOptions] = None,
                            title: str = "Import Sample Folder", warning_text: Optional[str] = None,
                            locked_format: Optional[str] = None,
                            sample_loader: Optional[Callable[[Optional[int]], list]] = None,
                            placement_loader: Optional[Callable[[Optional[int]], Any]] = None
                            ) -> tuple[Optional[ConversionOptions], Optional[int], Optional[dict]]:
        """Returns (opts, octave_offset, zone_overrides) -- None, None, None
        if cancelled. octave_offset isn't part of ConversionOptions (it
        only matters at parse time, before there's a Bank to apply
        resample/reduce options to at all); zone_overrides is the Sample
        Placement dialog's manual per-sample key-range/root override, or
        None if it was never opened or never accepted."""
        dialog = SampleDirImportDialog(parent, initial=initial, title=title,
                                        warning_text=warning_text, locked_format=locked_format,
                                        sample_loader=sample_loader, placement_loader=placement_loader)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None, None, None
        return dialog._to_options(), dialog.octave_offset(), dialog.zone_overrides()
