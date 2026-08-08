"""
Import-with-format-choice dialog: target format (E4B/KRZ/EIII) picker on
top of the exact same resample/reduce section ConvertOptionsDialog already
built
for the Pending pane's "Process before building..." -- reused via
subclassing rather than duplicated, since both features share the exact
same ConversionOptions shape (build/convert.py) now that target_format
lives there directly (it used to be a separate build/xpm_import.py-only
XpmImportOptions dataclass; merged once a second caller -- converting an
existing E4B preset via Explorer's "Import via mpc2emu..." -- needed the
same format choice for a non-XPM source too).

Used directly for two entry points that both need "pick a target format,
then optionally resample/reduce": importing a foreign XPM program
(main_window.py's _import_xpm()) and converting an already-native E4B
preset in place (main_window.py's _convert_preset_via_mpc2emu()) --
neither the dialog nor build/convert.py's pipeline cares which case it's
in, only the caller does. A third case, folder-of-WAVs import, reuses it
via subclassing instead: see ui/sampledir_import_dialog.py's
SampleDirImportDialog, which adds an octave-convention picker XPM/preset
sources don't need.

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
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                                QSizePolicy, QWidget)

from .convert_options_dialog import ConvertOptionsDialog
from ..build.convert import ConversionOptions

_KRZ_SANE_MAX_RATE_HZ = 24000


_DEFAULT_WARNING = (
    "Importing goes through mpc2emu's own model, same as any other "
    "conversion here; a few advanced parameters the original program "
    "used may not carry over. Resample/reduce below are optional "
    "and off by default for either target format.")


class SourceLabel(QLabel):
    """One line naming what is being imported, elided in the MIDDLE.

    Elided rather than wrapped, deliberately. This dialog's groups already
    want more height than `adjustSize()` will grant (see the project notes on
    keeping the QScrollArea), and Qt takes any shortfall out of the group
    bodies -- below their minimumSizeHint, so rows overlap. A path wrapping
    to three lines would spend exactly the budget that shortfall comes from.

    Middle rather than right, because the two ends are the parts that
    identify a source: the volume it is on and the folder it is in. The full
    text is always on the tooltip.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self.setToolTip(text)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Without this the un-elided text sets the dialog's minimum width, and
        # a deep path would make the window wider than the screen.
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:      # noqa: N802  (Qt casing)
        self._full = text
        self.setToolTip(text)
        self._reelide()

    def resizeEvent(self, event) -> None:      # noqa: N802  (Qt casing)
        super().resizeEvent(event)
        self._reelide()

    def _reelide(self) -> None:
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideMiddle,
                                            max(0, self.width())))


class FormatConvertDialog(ConvertOptionsDialog):
    def __init__(self, parent=None, initial: Optional[ConversionOptions] = None,
                 title: str = "Import MPC Program", warning_text: Optional[str] = None,
                 locked_format: Optional[str] = None,
                 bank_loader: Optional[Callable[[], list]] = None,
                 source_text: str = ""):
        super().__init__(parent, initial=initial, bank_loader=bank_loader)
        self.setWindowTitle(title)
        self._warning_label.setText(warning_text or _DEFAULT_WARNING)

        # Built here, inserted after the format row below -- that one also
        # goes in at index 0 and would otherwise end up above this.
        self._source_label: Optional[SourceLabel] = None
        if source_text:
            self._source_label = SourceLabel(source_text)
            self._source_label.setStyleSheet(
                "font-weight: 600; padding-bottom: 2px;")

        format_row = QWidget()
        row_layout = QHBoxLayout(format_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(QLabel("Import as:"))
        self._format_box = QComboBox()
        self._format_box.addItems(["E4B", "KRZ", "EIII"])
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

        # Now, so it lands ABOVE the format picker: the first question this
        # dialog has to answer is "what am I about to import?". Only present
        # when a caller supplies one -- an empty row would be a blank line at
        # the top of every other dialog in this family.
        if self._source_label is not None:
            self.layout().insertWidget(0, self._source_label)

        #: Layout index of the format row. Subclasses insert their own header
        #: rows relative to THIS rather than to a hardcoded 0 -- the source
        #: label above shifts everything down by one when it is present, and
        #: hardcoded indices silently reordered the header when it appeared
        #: (the format picker sank below rows that are meant to follow it).
        self._header_base = self.layout().indexOf(format_row)

        if initial is None and self._format_box.currentText() == "KRZ":
            self._apply_krz_sane_default()
        # The base class already ran this once from its own __init__, but at
        # that point _format_box didn't exist yet and _current_target_format()
        # fell back to "E4B". Re-run now that the real picker is in place.
        self._refresh_pan_law_availability()

    def _current_target_format(self) -> str:
        # Python dispatches to this override from the BASE __init__, which runs
        # before _format_box exists -- fall back to the base answer until it
        # does (the __init__ above re-runs the gate once it has been built).
        box = getattr(self, "_format_box", None)
        return box.currentText() if box is not None else super()._current_target_format()

    def _on_target_format_changed(self, fmt: str) -> None:
        if fmt == "KRZ":
            self._apply_krz_sane_default()
        # Pan compensation is E4B-only, so switching the target has to grey it
        # out (and clear it) rather than leave a setting that would be dropped.
        self._refresh_pan_law_availability()

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
                            title: str = "Import MPC Program", warning_text: Optional[str] = None,
                            locked_format: Optional[str] = None,
                            bank_loader: Optional[Callable[[], list]] = None,
                            source_text: str = ""
                            ) -> Optional[ConversionOptions]:
        dialog = FormatConvertDialog(parent, initial=initial, title=title, warning_text=warning_text,
                                  locked_format=locked_format, bank_loader=bank_loader,
                                  source_text=source_text)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog._to_options()
