"""
Import-with-format-choice dialog: target format (E4B/KRZ) picker on top of
the exact same resample/reduce section ConvertOptionsDialog already built
for the Pending pane's "Process before building..." -- reused via
subclassing rather than duplicated, since both features share the exact
same ConversionOptions shape (build/convert.py) now that target_format
lives there directly (it used to be a separate build/xpm_import.py-only
XpmImportOptions dataclass; merged once a second caller -- converting an
existing E4B preset via Explorer's "Import via mpc2emu..." -- needed the
same format choice for a non-XPM source too).

Used for two distinct entry points that both need "pick a target format,
then optionally resample/reduce": importing a foreign XPM program
(main_window.py's _import_xpm()) and converting an already-native E4B
preset in place (main_window.py's _convert_preset_via_mpc2emu()) --
neither the dialog nor build/convert.py's pipeline cares which case it's
in, only the caller does.

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

locked_format: when New Bank already has a format lock (BankPane.format
is not None), callers pass it here so the picker shows and defaults to
that format but can't be changed to the other one -- picking the "wrong"
format would still run a real (possibly slow) mpc2emu conversion only to
have BankPane.add_presets() reject the result afterward, so there's no
point offering that choice live.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QWidget

from .convert_options_dialog import ConvertOptionsDialog
from ..build.convert import ConversionOptions

_KRZ_SANE_MAX_RATE_HZ = 24000


_DEFAULT_WARNING = (
    "Importing goes through mpc2emu's own model, same as any other "
    "conversion here; a few advanced parameters the original XPM "
    "used may not carry over. Resample/reduce below are optional "
    "and off by default for either target format.")


class XpmImportDialog(ConvertOptionsDialog):
    def __init__(self, parent=None, initial: Optional[ConversionOptions] = None,
                 title: str = "Import XPM", warning_text: Optional[str] = None,
                 locked_format: Optional[str] = None):
        super().__init__(parent, initial=initial)
        self.setWindowTitle(title)
        self._warning_label.setText(warning_text or _DEFAULT_WARNING)

        format_row = QWidget()
        row_layout = QHBoxLayout(format_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(QLabel("Import as:"))
        self._format_box = QComboBox()
        self._format_box.addItems(["E4B", "KRZ"])
        default_fmt = locked_format or (initial.target_format if initial else "E4B")
        self._format_box.setCurrentText(default_fmt)
        self._format_box.currentTextChanged.connect(self._on_target_format_changed)
        row_layout.addWidget(self._format_box)
        row_layout.addStretch()
        if locked_format is not None:
            # New Bank already has presets in it -- any other choice here
            # is guaranteed to be rejected by BankPane.add_presets() after
            # a real (possibly slow) conversion already ran, so there's no
            # point offering it. Greyed out rather than hidden: still
            # visible/legible so it's clear what format this is going
            # into, just not a live choice right now.
            self._format_box.setEnabled(False)
            self._format_box.setToolTip(
                f"New Bank already contains {locked_format} presets — "
                f"clear it or send it to Pending first to import as a "
                f"different format.")
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

    def _to_options(self) -> ConversionOptions:
        return dataclasses.replace(super()._to_options(),
                                    target_format=self._format_box.currentText())

    @staticmethod
    def get_import_options(parent=None, initial: Optional[ConversionOptions] = None,
                            title: str = "Import XPM", warning_text: Optional[str] = None,
                            locked_format: Optional[str] = None) -> Optional[ConversionOptions]:
        dialog = XpmImportDialog(parent, initial=initial, title=title, warning_text=warning_text,
                                  locked_format=locked_format)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog._to_options()
