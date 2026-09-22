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
                                QRadioButton, QSizePolicy, QVBoxLayout,
                                QWidget)

from .convert_options_dialog import ConvertOptionsDialog
from ..build import firmware_sim, foreign_import
from ..build.convert import ConversionOptions

_KRZ_SANE_MAX_RATE_HZ = 24000


#: Shown instead of _DEFAULT_WARNING while device matching is selected.
#: The default says the import "goes through mpc2emu's own model, same as any
#: other conversion here", which is precisely what this arm does NOT do --
#: leaving it up would have the dialog contradict the radio button above it.
_DEVICE_MATCH_WARNING = (
    "This writes what the sampler's own disk importer would have written, "
    "byte for byte — traced from the E-MU EOS 4.7 and Kurzweil K2000 v3.87J "
    "ROMs. It is deliberately LOWER fidelity than a normal conversion, "
    "including whatever the device itself discards; its purpose is that the "
    "result can be diffed against a real device import. The options below "
    "are this project's own processing and are skipped.")

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
                 source_text: str = "", source_format: str = ""):
        super().__init__(parent, initial=initial, bank_loader=bank_loader)
        self.setWindowTitle(title)
        self._default_warning = warning_text or _DEFAULT_WARNING
        self._warning_label.setText(self._default_warning)

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
        # AKAI last, and only where this checkout can actually write it. An
        # entry that always failed would be worse than its absence: the
        # refusal would arrive after a real, possibly slow, conversion had
        # already run -- the same reasoning as the locked_format branch
        # below.
        targets = ["E4B", "KRZ", "EIII"]
        from ..config import Config
        try:
            if Config.load().check_akai_write_support()[0]:
                targets.append("AKAI")
        except Exception:
            pass
        self._format_box.addItems(targets)
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

        self._source_format = source_format
        self._method_row = self._build_method_row(source_format, initial)
        if self._method_row is not None:
            self.layout().insertWidget(1, self._method_row)

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
        self._refresh_krz_layers_availability()
        self._refresh_akai_hw_availability()

    # ── import method ──────────────────────────────────────────────────────

    def _build_method_row(self, source_format: str,
                          initial: Optional[ConversionOptions]):
        """The import-method choice, or None when there is nothing to choose.

        **Two different things are called "firmware" here and the row exists
        to keep them apart** (see build/firmware_sim.py):

        * Importing an Ensoniq or Roland disc uses a firmware-DERIVED
          reader, because those discs have no documented layout. What comes
          out is then written normally. That is an ordinary conversion with
          an unusual parser, and all the options below still apply to it.
        * Matching the sampler's own import writes what the device's disk
          importer would have written, byte for byte. It is deliberately
          WORSE output whose value is that it can be diffed against a real
          device import. It takes no options, because any of them would make
          the result something the device would not produce.

        The second arm is shown for every source a sampler can actually
        import and **disabled with the reason** when the path is not
        available -- mpc2emu asked for that specifically: a user who picks
        "match the device" and silently receives our normal output would
        have no way to know.
        """
        importable = (foreign_import.EPS_FORMAT, foreign_import.ROLAND_FORMAT,
                      "AKAI")
        if source_format not in importable:
            return None

        row = QWidget()
        box = QVBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 6)
        box.addWidget(QLabel("Import method:"))

        self._mpc_radio = QRadioButton("mpc2emu import")
        self._device_radio = QRadioButton(
            "Match the sampler's own import (experimental)")
        box.addWidget(self._mpc_radio)
        box.addWidget(self._device_radio)

        if source_format in (foreign_import.EPS_FORMAT,
                             foreign_import.ROLAND_FORMAT):
            self._mpc_radio.setText(
                f"mpc2emu import — the only way to read "
                f"{'an' if source_format[0] in 'AEIOU' else 'a'} "
                f"{source_format} disc")
            self._mpc_radio.setToolTip(
                f"A {source_format} disc has no documented layout, so the "
                f"reader is derived from the sampler's own firmware. The "
                f"bank it produces is then written normally, and the options "
                f"below apply as they do to any other import.")
        self._mpc_radio.setChecked(True)

        self._device_note = QLabel("")
        self._device_note.setWordWrap(True)
        self._device_note.setStyleSheet(
            "color: palette(placeholdertext); font-size: 11px; "
            "margin-left: 20px;")
        box.addWidget(self._device_note)

        self._device_radio.toggled.connect(self._on_method_changed)
        self._refresh_device_arm()
        if (initial is not None and getattr(initial, "match_device_import", False)
                and self._device_radio.isEnabled()):
            self._device_radio.setChecked(True)
        self._on_method_changed()
        return row

    def _refresh_device_arm(self) -> None:
        """Re-ask whether THIS source→target pair can match the device.

        Re-run whenever the target format changes: the answer is per PATH,
        not per source. Upstream has AKAI→KRZ working and AKAI→E4B still
        being wired, so the same disc can be matchable into one format and
        not the other, and a row decided once at construction would be wrong
        the moment the picker moved.
        """
        radio = getattr(self, "_device_radio", None)
        if radio is None:
            return
        target = self._current_target_format()
        st = firmware_sim.status(self._source_format, target)
        radio.setEnabled(st.available)
        if st.available:
            # What this path does NOT reproduce, in mpc2emu's own words plus
            # the coverage ratio. Shown on the row rather than saved for a
            # post-import risk, because it is a reason to choose the other
            # arm -- after the conversion it is too late to be a choice.
            self._device_note.setText(
                firmware_sim.fidelity(self._source_format, target))
        else:
            self._device_note.setText(f"Unavailable: {st.reason}")
        radio.setToolTip(
            "Writes what the sampler's own disk importer would have written, "
            "byte for byte. This is deliberately LOWER fidelity than a normal "
            "conversion — it exists so the result can be compared against a "
            "real device import, not to sound better."
            if st.available else st.reason)
        if not st.available and radio.isChecked():
            self._mpc_radio.setChecked(True)

    def _on_method_changed(self, *_args) -> None:
        """Matching the device takes no options, so the option body is off.

        Only for THAT arm. An Ensoniq or Roland import is a normal
        conversion -- its reader is unusual, its writer is not -- so greying
        its options out would withhold resampling and rate ceilings from the
        two formats most likely to need them.
        """
        radio = getattr(self, "_device_radio", None)
        if radio is None:
            return
        on = radio.isChecked()
        self._warning_label.setText(
            _DEVICE_MATCH_WARNING if on else self._default_warning)
        self._scroll.setEnabled(not on)
        self._scroll.setToolTip(
            "Matching the device reproduces its own conversion — these "
            "options would make the result something it would not produce."
            if on else "")

    def _device_match_selected(self) -> bool:
        radio = getattr(self, "_device_radio", None)
        return bool(radio is not None and radio.isChecked() and radio.isEnabled())

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
        self._refresh_krz_layers_availability()
        self._refresh_akai_hw_availability()
        self._refresh_device_arm()

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
                                    target_format=self._format_box.currentText(),
                                    match_device_import=self._device_match_selected())

    @staticmethod
    def get_import_options(parent=None, initial: Optional[ConversionOptions] = None,
                            title: str = "Import MPC Program", warning_text: Optional[str] = None,
                            locked_format: Optional[str] = None,
                            bank_loader: Optional[Callable[[], list]] = None,
                            source_text: str = "", source_format: str = ""
                            ) -> Optional[ConversionOptions]:
        dialog = FormatConvertDialog(parent, initial=initial, title=title, warning_text=warning_text,
                                  locked_format=locked_format, bank_loader=bank_loader,
                                  source_text=source_text, source_format=source_format)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog._to_options()
