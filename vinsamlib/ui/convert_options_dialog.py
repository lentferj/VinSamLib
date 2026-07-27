"""
Convert Options dialog: lets the user opt into mpc2emu's vintage resample
and/or sample-count reduction passes before "Build Image ->" assembles a
pending E4B bank (see build/convert.py -- this dialog only builds the
data, it never touches mpc2emu itself).

Three independent top-level toggles (Vintage Resample / Limit Maximum
Sample Rate / Reduce Sample Count), each collapsing its own body when
unchecked: QGroupBox's built-in checkable behavior enables/disables
children for free but doesn't hide them, so each group's real content
lives in an inner QWidget whose setVisible() is wired to the group's own
toggled(bool) too. Max sample rate used to be nested inside Vintage
Resample (matching convert.py's own CLI grouping) but that made it
impossible to apply on its own even though convert.py itself treats it as
a fully independent pipeline stage -- it's its own toggle now.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QSlider, QSpinBox, QVBoxLayout, QWidget)

from ..build.convert import ConversionOptions
from ..mpc2emu_bridge import resampler

_MIN_HZ = 4000
_MAX_HZ = 48000
_DEFAULT_HZ = 24000
# reduce_bank() early-returns when both pcts are <= 0, so a freshly
# checked reduce group starts at a non-zero percentage -- leaving it at
# 0 right after enabling it would be a silent no-op.
_DEFAULT_REDUCE_PCT = 30


class ConvertOptionsDialog(QDialog):
    def __init__(self, parent=None, initial: Optional[ConversionOptions] = None):
        super().__init__(parent)
        self.setWindowTitle("Convert Options")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)

        self._warning_label = QLabel(
            "Applying vintage resample/reduce re-encodes this bank through "
            "mpc2emu's own model; a few advanced parameters not covered by "
            "that model may reset to defaults, and the final bank size may "
            "differ from what New Bank's meter showed.")
        self._warning_label.setWordWrap(True)
        self._warning_label.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        layout.addWidget(self._warning_label)

        layout.addWidget(self._build_resample_group())
        layout.addWidget(self._build_max_rate_group())
        layout.addWidget(self._build_reduce_group())
        layout.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if initial is not None:
            self._apply_initial(initial)

    def _apply_initial(self, opts: ConversionOptions) -> None:
        """Re-opening for a bank that already has options set (per-bank
        storage, see pending_pane.py) should show its current choice, not
        silently reset to defaults."""
        if opts.resample_profile is not None:
            self._resample_group.setChecked(True)
            idx = self._profile_keys.index(opts.resample_profile)
            self._profile_box.setCurrentIndex(idx)
            self._bandpass_check.setChecked(not opts.no_bandpass)
            self._keep_gain_check.setChecked(opts.resample_keep_gain)
        if opts.max_sample_rate:
            self._max_rate_group.setChecked(True)
            self._max_rate_spin.setValue(opts.max_sample_rate)
        if opts.reduce_key_zones_pct > 0:
            self._key_zone_group.setChecked(True)
            self._key_zone_slider.setValue(int(opts.reduce_key_zones_pct))
        if opts.reduce_velocity_layers_pct > 0:
            self._velocity_group.setChecked(True)
            self._velocity_slider.setValue(int(opts.reduce_velocity_layers_pct))

    def _wire_resize_on_toggle(self, group: QGroupBox) -> None:
        """QDialog doesn't auto-grow when a child's visibility toggles after
        the window is already shown -- checking just one reduce/resample
        group fits fine at whatever size the dialog first appeared at, but
        checking a SECOND one needs more vertical space than that now-fixed
        window has, and nothing else tells Qt to grow it (the user would
        have to notice and manually drag the edge). Deferred via
        singleShot(0, ...) so this runs after the layout has actually
        recalculated its sizeHint post-toggle, not before."""
        group.toggled.connect(lambda _checked: QTimer.singleShot(0, self.adjustSize))

    # -- Group A: Vintage Resample --------------------------------------------

    def _build_resample_group(self) -> QGroupBox:
        group = QGroupBox("Vintage Resample")
        group.setCheckable(True)
        group.setChecked(False)
        self._resample_group = group
        outer = QVBoxLayout(group)

        body = QWidget()
        body.setVisible(False)
        group.toggled.connect(body.setVisible)
        self._wire_resize_on_toggle(group)
        form = QFormLayout(body)
        form.setContentsMargins(0, 0, 0, 0)

        self._profile_box = QComboBox()
        self._profile_keys = list(resampler.PROFILES.keys())
        for key in self._profile_keys:
            self._profile_box.addItem(resampler.PROFILES[key].display_name)
        form.addRow("Profile:", self._profile_box)

        self._bandpass_check = QCheckBox("Apply bandpass coloring")
        self._bandpass_check.setChecked(True)
        form.addRow("", self._bandpass_check)

        self._keep_gain_check = QCheckBox("Keep gain-staged (hot) level")
        self._keep_gain_check.setChecked(False)
        form.addRow("", self._keep_gain_check)

        outer.addWidget(body)
        return group

    # -- Independent: max sample rate ------------------------------------------
    # convert.py treats --max-sample-rate as its own pipeline stage, entirely
    # independent of --resample (it ran unconditionally in convert.py's own
    # main(), before this dialog existed the plan doc already flagged this:
    # "grouped [with Resample] for UX only" -- so it needs to be checkable
    # on its own, not gated behind Vintage Resample also being checked.

    def _build_max_rate_group(self) -> QGroupBox:
        group = QGroupBox("Limit Maximum Sample Rate")
        group.setCheckable(True)
        group.setChecked(False)
        self._max_rate_group = group
        outer = QVBoxLayout(group)

        body = QWidget()
        body.setVisible(False)
        group.toggled.connect(body.setVisible)
        self._wire_resize_on_toggle(group)
        row = QHBoxLayout(body)
        row.setContentsMargins(0, 0, 0, 0)

        self._max_rate_spin = QSpinBox()
        self._max_rate_spin.setRange(_MIN_HZ, _MAX_HZ)
        self._max_rate_spin.setSingleStep(1000)
        self._max_rate_spin.setValue(_DEFAULT_HZ)
        self._max_rate_spin.setSuffix(" Hz")
        # QSpinBox's own sizeHint doesn't reliably reserve room for the
        # widest value + suffix on every platform/theme -- explicit
        # minimum width so e.g. "48000 Hz" never clips.
        self._max_rate_spin.setMinimumWidth(90)
        row.addWidget(self._max_rate_spin)
        row.addStretch()

        outer.addWidget(body)
        return group

    # -- Group B: Reduce Sample Count ------------------------------------------

    def _build_reduce_group(self) -> QGroupBox:
        # Not checkable itself -- reduce is a distinct feature from
        # resample, not a sub-option of it, so this outer box is just a
        # visual grouping; its two inner boxes are the real toggles.
        group = QGroupBox("Reduce Sample Count")
        outer = QVBoxLayout(group)

        self._key_zone_group, self._key_zone_slider = self._build_reduce_subgroup(
            "Reduce Key Zones by")
        outer.addWidget(self._key_zone_group)

        self._velocity_group, self._velocity_slider = self._build_reduce_subgroup(
            "Reduce Velocity Layers by")
        outer.addWidget(self._velocity_group)

        return group

    def _build_reduce_subgroup(self, title: str) -> tuple[QGroupBox, QSlider]:
        group = QGroupBox(title)
        group.setCheckable(True)
        group.setChecked(False)

        body = QWidget()
        body.setVisible(False)
        group.toggled.connect(body.setVisible)
        self._wire_resize_on_toggle(group)

        row = QHBoxLayout(body)
        row.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(_DEFAULT_REDUCE_PCT)
        spin = QSpinBox()
        spin.setRange(0, 100)
        spin.setSuffix("%")
        spin.setValue(_DEFAULT_REDUCE_PCT)
        # Same reasoning as the max-rate spinbox above -- guarantee "100%"
        # never clips regardless of platform/theme font metrics.
        spin.setMinimumWidth(65)
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)
        row.addWidget(slider, 1)
        row.addWidget(spin)

        outer = QVBoxLayout(group)
        outer.addWidget(body)
        return group, slider

    # -- result ----------------------------------------------------------------

    def _to_options(self) -> ConversionOptions:
        resample_profile = None
        no_bandpass = False
        resample_keep_gain = False
        if self._resample_group.isChecked():
            resample_profile = self._profile_keys[self._profile_box.currentIndex()]
            no_bandpass = not self._bandpass_check.isChecked()
            resample_keep_gain = self._keep_gain_check.isChecked()

        max_sample_rate = self._max_rate_spin.value() if self._max_rate_group.isChecked() else None

        reduce_key_zones_pct = (
            float(self._key_zone_slider.value()) if self._key_zone_group.isChecked() else 0.0)
        reduce_velocity_layers_pct = (
            float(self._velocity_slider.value()) if self._velocity_group.isChecked() else 0.0)

        return ConversionOptions(
            resample_profile=resample_profile,
            no_bandpass=no_bandpass,
            resample_keep_gain=resample_keep_gain,
            max_sample_rate=max_sample_rate,
            reduce_key_zones_pct=reduce_key_zones_pct,
            reduce_velocity_layers_pct=reduce_velocity_layers_pct,
        )

    @staticmethod
    def get_options(parent=None, initial: Optional[ConversionOptions] = None) -> Optional[ConversionOptions]:
        """Modal convenience entry point, matching this codebase's other
        static dialog helpers (QInputDialog.getText(), QFileDialog.get...).
        Returns None on Cancel, a populated ConversionOptions on OK.
        `initial`, when given, pre-fills the dialog with an already-chosen
        set of options (e.g. reopening for a pending bank that already has
        its own conversion choice -- see pending_pane.py)."""
        dialog = ConvertOptionsDialog(parent, initial=initial)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog._to_options()
