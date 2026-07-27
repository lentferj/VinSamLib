"""
Import XPM dialog: target format (E4B/KRZ) picker on top of the exact same
resample/reduce section ConvertOptionsDialog already built for the Pending
pane's "Process before building..." -- reused via subclassing rather than
duplicated, since the two features share every field except target_format
(see build/xpm_import.py's XpmImportOptions vs build/convert.py's
ConversionOptions).

Only real addition beyond "which dialog do I subclass": switching the
target format to KRZ nudges (doesn't force) the max-sample-rate step to a
sane default -- mpc2emu's own convert.py defaults to a downsample when
targeting KRZ and leaves it off for E4B, because the K2000 only gives
+1.46 st of up-pitch headroom at 44.1 kHz before wide key zones clamp,
while E4XT has no such ceiling. This mirrors that default in spirit with
a flat Hz value rather than convert.py's fancier per-sample "headroom-
aware" auto-downsample (that one is inline main()-only logic upstream,
not a reusable function -- reproducing it faithfully is deferred, see
docs/mpc2emu_conversion_integration_plan.md).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QWidget

from .convert_options_dialog import ConvertOptionsDialog
from ..build.convert import ConversionOptions
from ..build.xpm_import import XpmImportOptions

_KRZ_SANE_MAX_RATE_HZ = 24000


class XpmImportDialog(ConvertOptionsDialog):
    def __init__(self, parent=None, initial: Optional[XpmImportOptions] = None):
        conv_initial = ConversionOptions(
            resample_profile=initial.resample_profile,
            no_bandpass=initial.no_bandpass,
            resample_keep_gain=initial.resample_keep_gain,
            max_sample_rate=initial.max_sample_rate,
            reduce_key_zones_pct=initial.reduce_key_zones_pct,
            reduce_velocity_layers_pct=initial.reduce_velocity_layers_pct,
        ) if initial is not None else None
        super().__init__(parent, initial=conv_initial)
        self.setWindowTitle("Import XPM")
        self._warning_label.setText(
            "Importing goes through mpc2emu's own model, same as any other "
            "conversion here; a few advanced parameters the original XPM "
            "used may not carry over. Resample/reduce below are optional "
            "and off by default for either target format.")

        format_row = QWidget()
        row_layout = QHBoxLayout(format_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(QLabel("Import as:"))
        self._format_box = QComboBox()
        self._format_box.addItems(["E4B", "KRZ"])
        self._format_box.setCurrentText(initial.target_format if initial else "E4B")
        self._format_box.currentTextChanged.connect(self._on_target_format_changed)
        row_layout.addWidget(self._format_box)
        row_layout.addStretch()
        self.layout().insertWidget(0, format_row)

        if initial is None and self._format_box.currentText() == "KRZ":
            self._apply_krz_sane_default()

    def _on_target_format_changed(self, fmt: str) -> None:
        if fmt == "KRZ":
            self._apply_krz_sane_default()

    def _apply_krz_sane_default(self) -> None:
        # Only nudges the max-sample-rate step (now its own independent
        # group in ConvertOptionsDialog, not nested inside Vintage
        # Resample); resample/reduce stay off by default for either
        # target, and this never overrides a value the user already set
        # (only fires when the group is still unchecked).
        if not self._max_rate_group.isChecked():
            self._max_rate_group.setChecked(True)
            self._max_rate_spin.setValue(_KRZ_SANE_MAX_RATE_HZ)

    def _to_import_options(self) -> XpmImportOptions:
        conv = self._to_options()
        return XpmImportOptions(
            target_format=self._format_box.currentText(),
            resample_profile=conv.resample_profile,
            no_bandpass=conv.no_bandpass,
            resample_keep_gain=conv.resample_keep_gain,
            max_sample_rate=conv.max_sample_rate,
            reduce_key_zones_pct=conv.reduce_key_zones_pct,
            reduce_velocity_layers_pct=conv.reduce_velocity_layers_pct,
        )

    @staticmethod
    def get_import_options(parent=None, initial: Optional[XpmImportOptions] = None) -> Optional[XpmImportOptions]:
        dialog = XpmImportDialog(parent, initial=initial)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog._to_import_options()
