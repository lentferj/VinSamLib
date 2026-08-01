"""
Convert Options dialog: lets the user opt into mpc2emu's vintage resample
and/or sample-count reduction passes before "Build Image ->" assembles a
pending E4B bank (see build/convert.py -- this dialog only builds the
data, it never touches mpc2emu itself).

Independent top-level toggles (Trim Start / Trim Tail / Constant-Power Pan
Compensation / Vintage Resample / Limit Maximum Sample Rate / Reduce Key
Zones / Reduce Velocity Layers), each collapsing its own body when
unchecked: QGroupBox's built-in checkable behavior enables/disables
children for free but doesn't hide them, so each group's real content
lives in an inner QWidget whose setVisible() is wired to the group's own
toggled(bool) too. Max sample rate used to be nested inside Vintage
Resample (matching convert.py's own CLI grouping) but that made it
impossible to apply on its own even though convert.py itself treats it as
a fully independent pipeline stage -- it's its own toggle now.

Groups are ordered roughly the way build/convert.py's _apply_and_write()
runs them, and they sit in a QScrollArea because expanding them all wants
more height than a window is allowed to take (see __init__).

Pan compensation is the one target-dependent control: the loudness law
behind it was measured on an E4XT, so it is offered for E4B output only
and greys out for KRZ/EIII. _current_target_format() is the hook for
that, overridden by FormatConvertDialog's live "Import as:" picker.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                             QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                             QFrame, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                             QPushButton, QScrollArea, QSlider, QSpinBox,
                             QVBoxLayout, QWidget)

from ..build.convert import ConversionOptions, stereo_mono_risk, suggest_mono_side
from ..mpc2emu_bridge import resampler

_MIN_HZ = 4000
_MAX_HZ = 48000
_DEFAULT_HZ = 24000
# reduce_bank() early-returns when both pcts are <= 0, so a freshly
# checked reduce group starts at a non-zero percentage -- leaving it at
# 0 right after enabling it would be a silent no-op.
_DEFAULT_REDUCE_PCT = 30

# QComboBox row index -> build.convert.ConversionOptions.mono. "Keep Stereo"
# (None) is first/default: mpc2emu itself defaults to keeping stereo since
# its own f936b8c ("--mono becomes a vintage-fit reduction"), and it is the
# one choice with zero cancellation risk, so it is not worth nudging users
# away from with a different pre-selection. E-mu hardware confirmed a stereo
# E4B bank loads and plays as stereo with the correct channel order
# (measured per-channel on a real E4XT, mpc2emu 0868233, 2026-07-31).
_MONO_CHOICES: list[tuple[str, Optional[str]]] = [
    ("Keep Stereo", None),
    ("Reduce to Mono — Mix (average both sides)", "mix"),
    ("Reduce to Mono — Left channel only", "left"),
    ("Reduce to Mono — Right channel only", "right"),
]

# Trim defaults mirror mpc2emu's own (--trim-start/--trim-tail default to 72 dB
# below peak with a 5 ms fade, i.e. "silence only" rather than cutting into the
# attack or release). Stored positive here and negated at the call site, the
# same way convert.py's `-abs(args.trim_start)` accepts either sign.
_DEFAULT_TRIM_DB = 72.0
_DEFAULT_TRIM_FADE_MS = 5.0
_MIN_TRIM_DB = 20.0
_MAX_TRIM_DB = 96.0


class ConvertOptionsDialog(QDialog):
    def __init__(self, parent=None, initial: Optional[ConversionOptions] = None,
                 bank_loader: Optional[Callable[[], list]] = None):
        super().__init__(parent)
        self.setWindowTitle("Convert Options")
        self.setMinimumWidth(460)

        # Zero-arg callable returning the samples the chosen stereo setting
        # would actually apply to (an mpc2emu Bank's .samples list), or None
        # when a caller has no cheap way to preview them (e.g. no source
        # picked yet). Only used by the stereo group's Test button and by
        # accept()'s own risk check -- never by the real conversion, which
        # always re-parses its own real source regardless of this.
        self._bank_loader = bank_loader
        self._tested_mono: Optional[str] = None  # method last successfully Tested

        layout = QVBoxLayout(self)

        self._warning_label = QLabel(
            "Applying vintage resample/reduce re-encodes this bank through "
            "mpc2emu's own model; a few advanced parameters not covered by "
            "that model may reset to defaults, and the final bank size may "
            "differ from what New Bank's meter showed.")
        self._warning_label.setWordWrap(True)
        self._warning_label.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        layout.addWidget(self._warning_label)

        # The groups live in a scroll area, and with six of them that is no
        # longer optional: fully expanded they want ~1090 px, while
        # adjustSize() refuses to grow a window past a fraction of the screen
        # height. Without this the shortfall is taken out of the group bodies
        # themselves -- below their own minimumSizeHint, so rows overlap and
        # the trim controls become unreadable rather than merely cramped.
        # Warning text and buttons stay OUTSIDE it, so the buttons are always
        # reachable no matter how much is expanded.
        groups_host = QWidget()
        groups = QVBoxLayout(groups_host)
        groups.setContentsMargins(0, 0, 0, 0)
        # Roughly pipeline order (see build/convert.py's _apply_and_write):
        # the trims run first, then pan law, then the stereo reduction, and
        # only then the resample/reduce passes that were here first.
        groups.addWidget(self._build_trim_group())
        groups.addWidget(self._build_pan_law_group())
        groups.addWidget(self._build_stereo_group())
        groups.addWidget(self._build_resample_group())
        groups.addWidget(self._build_max_rate_group())
        groups.addWidget(self._build_reduce_group())
        groups.addStretch()

        self._scroll = QScrollArea()
        self._scroll.setWidget(groups_host)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Never scroll sideways -- the word-wrapped help labels are sized for
        # the viewport width, and a horizontal bar would mean they wrapped
        # against the wrong one.
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self._scroll, 1)

        self._refresh_pan_law_availability()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept_clicked)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if initial is not None:
            self._apply_initial(initial)

    def _apply_initial(self, opts: ConversionOptions) -> None:
        """Re-opening for a bank that already has options set (per-bank
        storage, see pending_pane.py) should show its current choice, not
        silently reset to defaults."""
        mono_idx = next((i for i, (_label, m) in enumerate(_MONO_CHOICES) if m == opts.mono), 0)
        self._mono_box.setCurrentIndex(mono_idx)
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
        if opts.pan_law == "constant-power":
            self._pan_law_group.setChecked(True)
        if opts.trim_start_db is not None:
            self._trim_start_group.setChecked(True)
            self._trim_start_db_spin.setValue(abs(opts.trim_start_db))
            self._trim_start_fade_spin.setValue(opts.trim_start_fade_ms)
            self._trim_start_keep_loops.setChecked(opts.trim_start_keep_loops)
        if opts.trim_tail_db is not None:
            self._trim_tail_group.setChecked(True)
            self._trim_tail_db_spin.setValue(abs(opts.trim_tail_db))
            self._trim_tail_fade_spin.setValue(opts.trim_tail_fade_ms)
            self._trim_tail_keep_loops.setChecked(opts.trim_tail_keep_loops)

    def _wire_resize_on_toggle(self, group: QGroupBox) -> None:
        """QDialog doesn't auto-grow when a child's visibility toggles after
        the window is already shown -- checking just one reduce/resample
        group fits fine at whatever size the dialog first appeared at, but
        checking a SECOND one needs more vertical space than that now-fixed
        window has, and nothing else tells Qt to grow it (the user would
        have to notice and manually drag the edge). Deferred via
        singleShot(0, ...) so this runs after the layout has actually
        recalculated its sizeHint post-toggle, not before.

        Grows only, and never past the screen: plain adjustSize() stopped
        being right once the groups moved into a QScrollArea, because the
        dialog's own sizeHint no longer reflects what the groups want (a
        scroll area is happy to be small), so it would SHRINK the window on
        every toggle and hide the content the user just asked to see."""
        group.toggled.connect(lambda _checked: QTimer.singleShot(0, self._grow_to_fit))

    def showEvent(self, event) -> None:
        """Open at whatever the current set of groups actually needs, instead
        of the scroll area's own modest default -- otherwise the dialog comes
        up scrolling even when nothing is expanded. First show only: after
        that the size is the user's, and only _grow_to_fit() adjusts it."""
        super().showEvent(event)
        if not getattr(self, "_sized_once", False):
            self._sized_once = True
            self._grow_to_fit()

    def _grow_to_fit(self) -> None:
        host = self._scroll.widget()
        if host is None:
            return
        # Everything except the scroll viewport (warning text, buttons, and
        # any subclass's own rows) keeps its height; only the viewport has to
        # find room for the groups' preferred height.
        chrome = self.height() - self._scroll.viewport().height()
        wanted = chrome + host.sizeHint().height()
        screen = self.screen() or QApplication.primaryScreen()
        cap = int(screen.availableGeometry().height() * 0.9) if screen else wanted
        target = min(wanted, cap)
        if target > self.height():
            self.resize(self.width(), target)

    # -- Group: Trim Silence ----------------------------------------------------
    # Two independent sides, like Reduce Sample Count's two inner boxes:
    # mpc2emu keeps --trim-start and --trim-tail separate (--trim is only a
    # shorthand that sets both), and they are genuinely useful apart -- an
    # autosampler capture typically needs the lead-in cut but wants its
    # natural release left alone.

    def _build_trim_group(self) -> QGroupBox:
        group = QGroupBox("Trim Silence")
        outer = QVBoxLayout(group)

        (self._trim_start_group, self._trim_start_db_spin,
         self._trim_start_fade_spin, self._trim_start_keep_loops) = self._build_trim_subgroup(
            "Trim Start (leading silence)",
            "Cuts everything before the onset and fades in over the first kept "
            "frames so the cut is click-free. A loop starting inside the cut "
            "lead-in (an autosampler's whole-take loop) is dropped unless you "
            "keep it below.")
        outer.addWidget(self._trim_start_group)

        (self._trim_tail_group, self._trim_tail_db_spin,
         self._trim_tail_fade_spin, self._trim_tail_keep_loops) = self._build_trim_subgroup(
            "Trim Tail (trailing decay/silence)",
            "Cuts everything after the last audible frame and fades out into "
            "it. A loop spanning the cut tail is dropped unless you keep it "
            "below.")
        outer.addWidget(self._trim_tail_group)

        return group

    def _build_trim_subgroup(self, title: str, help_text: str
                              ) -> tuple[QGroupBox, QDoubleSpinBox, QDoubleSpinBox, QCheckBox]:
        group = QGroupBox(title)
        group.setCheckable(True)
        group.setChecked(False)

        body = QWidget()
        body.setVisible(False)
        group.toggled.connect(body.setVisible)
        self._wire_resize_on_toggle(group)

        # The spin rows go in a QFormLayout for label alignment, but the
        # word-wrapped help label must NOT: a wrapping QLabel inside a
        # QFormLayout never resolves its height-for-width, and the whole
        # body collapses to a squashed strip with the rows drawn on top of
        # each other. Same QVBoxLayout-with-a-nested-form shape the stereo
        # group already uses for its own wrapped label.
        inner = QVBoxLayout(body)
        inner.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)

        db_spin = QDoubleSpinBox()
        db_spin.setRange(_MIN_TRIM_DB, _MAX_TRIM_DB)
        db_spin.setDecimals(0)
        db_spin.setSingleStep(1.0)
        db_spin.setValue(_DEFAULT_TRIM_DB)
        db_spin.setSuffix(" dB below peak")
        # The threshold is a CEILING that cuts into the sample, so the
        # numbers run the opposite way to most "amount" controls: 72 is the
        # gentle end (silence only) and 45 the aggressive one.
        db_spin.setToolTip(
            "How far below the sample's own peak still counts as silence. "
            "72 dB (the default) removes silence only; lower values such as "
            "45 cut into the natural attack/release for a tighter sample.")
        db_spin.setMinimumWidth(150)
        form.addRow("Threshold:", db_spin)

        fade_spin = QDoubleSpinBox()
        fade_spin.setRange(0.0, 100.0)
        fade_spin.setDecimals(1)
        fade_spin.setSingleStep(1.0)
        fade_spin.setValue(_DEFAULT_TRIM_FADE_MS)
        fade_spin.setSuffix(" ms")
        fade_spin.setToolTip("Click-avoiding fade length at the new cut point.")
        fade_spin.setMinimumWidth(100)
        form.addRow("Fade:", fade_spin)

        inner.addLayout(form)

        keep_loops = QCheckBox("Keep loops (skip samples whose loop the trim would cut)")
        keep_loops.setChecked(False)
        inner.addWidget(keep_loops)

        label = QLabel(help_text)
        label.setWordWrap(True)
        label.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        inner.addWidget(label)

        layout = QVBoxLayout(group)
        layout.addWidget(body)
        return group, db_spin, fade_spin, keep_loops

    # -- Group: Pan Loudness (E4B only) -----------------------------------------

    def _build_pan_law_group(self) -> QGroupBox:
        group = QGroupBox("Constant-Power Pan Compensation")
        group.setCheckable(True)
        group.setChecked(False)     # mpc2emu's own default is --pan-law hardware
        self._pan_law_group = group
        outer = QVBoxLayout(group)

        body = QWidget()
        body.setVisible(False)
        group.toggled.connect(body.setVisible)
        self._wire_resize_on_toggle(group)
        inner = QVBoxLayout(body)
        inner.setContentsMargins(0, 0, 0, 0)

        label = QLabel(
            "Panning the E4XT makes a voice LOUDER — measured ~+2.9 dB at half "
            "pan and ~+4.3 dB hard-panned. Leaving this off reproduces the "
            "instrument, so a converted preset behaves exactly like one panned "
            "on the front panel. Turning it on subtracts that excess so loudness "
            "stays put across pan, which is what SFZ and SF2 sources assume — "
            "use it when you care about the balance the source author heard.\n\n"
            "This is ONE-WAY: the correction lands in each voice's volume and "
            "cannot be undone by re-reading the bank, so don't apply it twice "
            "to the same material.")
        label.setWordWrap(True)
        label.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        inner.addWidget(label)

        outer.addWidget(body)
        return group

    def _current_target_format(self) -> str:
        """Which format this conversion will actually write. The base dialog
        is the Pending pane's, which only ever applies conversion to E4B
        banks (pending_pane.py's _build_one). FormatConvertDialog overrides
        this with its live "Import as:" picker."""
        return "E4B"

    def _refresh_pan_law_availability(self) -> None:
        """Pan compensation is E4B-only and says so, rather than silently
        doing nothing: the law was measured on a real E4XT (+2.88 dB at pan
        0.5, +4.32 at 1.0) and nothing equivalent has been measured for the
        K2000 or the EIII, so applying it to those targets would bake a
        guess irreversibly into the volume byte."""
        is_e4b = self._current_target_format() == "E4B"
        self._pan_law_group.setEnabled(is_e4b)
        if not is_e4b:
            self._pan_law_group.setChecked(False)
            self._pan_law_group.setToolTip(
                f"E4B only — the pan loudness law was measured on an E4XT and "
                f"nothing equivalent is known for {self._current_target_format()}.")
        else:
            self._pan_law_group.setToolTip("")

    # -- Group: Stereo Samples --------------------------------------------------

    def _build_stereo_group(self) -> QGroupBox:
        group = QGroupBox("Stereo Samples")
        outer = QVBoxLayout(group)

        row = QHBoxLayout()
        self._mono_box = QComboBox()
        for label, _method in _MONO_CHOICES:
            self._mono_box.addItem(label)
        self._mono_box.currentIndexChanged.connect(self._on_mono_choice_changed)
        row.addWidget(self._mono_box, 1)

        self._test_button = QPushButton("Test")
        self._test_button.setToolTip(
            "Check the actual samples for stereo content and, for Mix, "
            "whether averaging would cancel signal on any of them.")
        self._test_button.clicked.connect(self._on_test_clicked)
        self._test_button.setEnabled(self._bank_loader is not None)
        if self._bank_loader is None:
            self._test_button.setToolTip(
                "Not available here -- nothing to preview yet for this "
                "conversion.")
        row.addWidget(self._test_button)
        outer.addLayout(row)

        self._stereo_result_label = QLabel()
        self._stereo_result_label.setWordWrap(True)
        self._stereo_result_label.setStyleSheet(
            "color: palette(placeholdertext); font-size: 11px;")
        outer.addWidget(self._stereo_result_label)

        # Fill the label from the CURRENT selection rather than hard-coding
        # any one method's text here: currentIndexChanged never fires for
        # the initial index, so a hard-coded string would sit under a
        # selection it doesn't describe until the user touched the combo
        # box -- the default (Keep Stereo) showed Mix's cancellation
        # warning, which reads as if keeping stereo were the risky choice.
        self._on_mono_choice_changed(self._mono_box.currentIndex())

        return group

    def _current_mono_method(self) -> Optional[str]:
        return _MONO_CHOICES[self._mono_box.currentIndex()][1]

    def _on_mono_choice_changed(self, _index: int) -> None:
        # A Test result only speaks to the method it was run for -- any
        # change invalidates it, so accept() knows to re-check rather than
        # trusting a stale result for a different method.
        self._tested_mono = None
        self._stereo_result_label.setStyleSheet(
            "color: palette(placeholdertext); font-size: 11px;")
        if self._current_mono_method() is None:
            self._stereo_result_label.setText(
                "Keeping stereo samples in stereo -- no cancellation risk, "
                "but roughly doubles the size of every stereo sample "
                "(mpc2emu's E4B writer stores both channels in one object; "
                "a released mpc2emu still downmixes for KRZ and EIII "
                "regardless of this setting -- KRZ stereo exists on an "
                "unmerged branch, and a checkout on it passes stereo "
                "through here too).")
        elif self._current_mono_method() == "mix":
            self._stereo_result_label.setText(
                "Averaging (Mix) can cancel signal on decorrelated stereo "
                "content -- across 247 real stereo E-mu samples, mpc2emu "
                "found a median channel correlation of just 0.076, so this "
                "is common, not an edge case. Use Test to check the actual "
                "samples, or prefer Left/Right if in doubt.")
        else:
            self._stereo_result_label.setText(
                "Picking one side never cancels signal, unlike averaging -- "
                "no test needed for this choice.")

    def _on_test_clicked(self) -> None:
        if self._bank_loader is None:
            return
        method = self._current_mono_method()
        self._test_button.setEnabled(False)
        try:
            samples = self._bank_loader()
        except Exception as ex:
            QMessageBox.warning(self, "Test", f"Couldn't preview the samples:\n\n{ex}")
            return
        finally:
            self._test_button.setEnabled(True)
        self._show_risk(stereo_mono_risk(samples, method or "mix"), method, samples)
        self._tested_mono = method

    def _show_risk(self, risk: dict, method: Optional[str], samples: list) -> None:
        if risk["stereo_count"] == 0:
            self._stereo_result_label.setText("No stereo samples found -- this setting has no effect.")
            self._stereo_result_label.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
            return
        if method != "mix":
            self._stereo_result_label.setText(
                f"{risk['stereo_count']} stereo sample(s) found. "
                f"{'Keeping stereo.' if method is None else 'Picking one side never cancels signal.'}")
            self._stereo_result_label.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
            return
        n = len(risk["decorrelated"])
        if n == 0:
            self._stereo_result_label.setText(
                f"{risk['stereo_count']} stereo sample(s) checked -- none are "
                f"decorrelated enough for averaging to be a concern.")
            self._stereo_result_label.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        else:
            worst_name, worst_r = risk["decorrelated"][0]
            # Same rough loudness heuristic the accept()-time confirmation
            # popup offers as its "Use <side> Instead" button -- surfaced
            # here too so Testing already tells the user what it would
            # suggest, not just that Mix is risky. See suggest_mono_side()'s
            # own docstring for why this is deliberately hedged.
            # (mpc2emu's own db5d599 -- the 111dacd this used to cite was
            # rebased away and is no longer reachable from its main.)
            suggestion = suggest_mono_side(samples)
            side_note = (f" (measured ~{suggestion['avg_db']:.1f} dB louder on average)"
                         if suggestion["n"] else "")
            self._stereo_result_label.setText(
                f"⚠ {n} of {risk['stereo_count']} stereo sample(s) have decorrelated "
                f"channels (worst: \"{worst_name}\", r={worst_r:+.2f}) -- averaging "
                f"cancels signal there. Consider {suggestion['side'].capitalize()} "
                f"instead{side_note}.")
            self._stereo_result_label.setStyleSheet("color: palette(link); font-size: 11px;")

    def _on_accept_clicked(self) -> None:
        """Only Mix carries a cancellation risk (see stereo_mono_risk()).
        If the current choice was already Tested, that result already
        informed the user -- proceed. Otherwise check now if a preview is
        available (freshest info wins over a stale Test click), or fall
        back to a generic warning if it isn't -- either way the user
        chooses explicitly rather than the risk being invisible, mirroring
        mpc2emu's own convert.py CLI warning."""
        if self._current_mono_method() != "mix" or self._tested_mono == "mix":
            self.accept()
            return
        samples = None
        risk = None
        if self._bank_loader is not None:
            try:
                samples = self._bank_loader()
                risk = stereo_mono_risk(samples, "mix")
            except Exception:
                samples, risk = None, None   # fall through to the generic warning below
        if risk is not None and not risk["decorrelated"]:
            self.accept()
            return
        self._confirm_mono_mix_risk(risk, samples)

    def _confirm_mono_mix_risk(self, risk: Optional[dict], samples: Optional[list]) -> None:
        if risk is not None:
            worst_name, worst_r = risk["decorrelated"][0]
            detail = (f"{len(risk['decorrelated'])} of {risk['stereo_count']} stereo "
                      f"sample(s) have decorrelated channels (worst: \"{worst_name}\", "
                      f"r={worst_r:+.2f}).")
        else:
            detail = ("This hasn't been tested, and across 247 real stereo E-mu "
                      "samples mpc2emu measured a median channel correlation of "
                      "just 0.076 -- decorrelated stereo is the common case, not "
                      "the exception.")

        # Picking EITHER side (unlike Mix) never cancels signal, so this
        # suggestion is a secondary nudge, not the actual fix -- see
        # suggest_mono_side()'s own docstring for why it's deliberately
        # rough (mpc2emu's own 111dacd found no reliable way to pick
        # between sides and declined to automate it).
        suggestion = suggest_mono_side(samples) if samples else {"side": "left", "avg_db": 0.0, "n": 0}
        side_label = suggestion["side"].capitalize()
        if suggestion["n"]:
            suggestion_detail = (
                f"The {suggestion['side']} channel measured ~{suggestion['avg_db']:.1f} dB "
                f"louder on average across the affected sample(s) -- a rough loudness "
                f"heuristic, not a strong signal, but picking either side avoids Mix's "
                f"cancellation risk regardless.")
        else:
            suggestion_detail = (
                "No sample data to measure a suggestion from, so this is an arbitrary "
                "pick -- picking either side avoids Mix's cancellation risk regardless.")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Averaging may cancel signal")
        box.setText(
            f"{detail}\n\nAveraging both sides (Mix) can cancel signal on "
            f"decorrelated stereo content.\n\n{suggestion_detail}")
        proceed_btn = box.addButton("Go Ahead Anyway", QMessageBox.ButtonRole.AcceptRole)
        suggested_btn = box.addButton(f"Use {side_label} Instead", QMessageBox.ButtonRole.ActionRole)
        go_back_btn = box.addButton("Go Back", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(suggested_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is proceed_btn:
            self.accept()
        elif clicked is suggested_btn:
            idx = next(i for i, (_label, m) in enumerate(_MONO_CHOICES) if m == suggestion["side"])
            self._mono_box.setCurrentIndex(idx)
        # Go Back (or closing the box): stay on the dialog, current
        # selection unchanged -- lets the user pick a different setting
        # themselves (Left/Right/Keep Stereo) rather than only being
        # offered the one this dialog suggests.

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

        # isChecked() alone would report True for a group that is checked but
        # DISABLED (Qt keeps the check state when a group is greyed out), so a
        # non-E4B target could still smuggle pan compensation through.
        pan_law = ("constant-power"
                    if self._pan_law_group.isEnabled() and self._pan_law_group.isChecked()
                    else "hardware")

        trim_start_db = (self._trim_start_db_spin.value()
                          if self._trim_start_group.isChecked() else None)
        trim_tail_db = (self._trim_tail_db_spin.value()
                         if self._trim_tail_group.isChecked() else None)

        return ConversionOptions(
            resample_profile=resample_profile,
            no_bandpass=no_bandpass,
            resample_keep_gain=resample_keep_gain,
            max_sample_rate=max_sample_rate,
            reduce_key_zones_pct=reduce_key_zones_pct,
            reduce_velocity_layers_pct=reduce_velocity_layers_pct,
            mono=self._current_mono_method(),
            pan_law=pan_law,
            trim_start_db=trim_start_db,
            trim_start_fade_ms=self._trim_start_fade_spin.value(),
            trim_start_keep_loops=self._trim_start_keep_loops.isChecked(),
            trim_tail_db=trim_tail_db,
            trim_tail_fade_ms=self._trim_tail_fade_spin.value(),
            trim_tail_keep_loops=self._trim_tail_keep_loops.isChecked(),
        )

    @staticmethod
    def get_options(parent=None, initial: Optional[ConversionOptions] = None,
                     bank_loader: Optional[Callable[[], list]] = None
                     ) -> Optional[ConversionOptions]:
        """Modal convenience entry point, matching this codebase's other
        static dialog helpers (QInputDialog.getText(), QFileDialog.get...).
        Returns None on Cancel, a populated ConversionOptions on OK.
        `initial`, when given, pre-fills the dialog with an already-chosen
        set of options (e.g. reopening for a pending bank that already has
        its own conversion choice -- see pending_pane.py). `bank_loader`,
        when given, feeds the Stereo group's Test button and accept()'s own
        risk check -- see build/convert.py's load_samples_for_test()/
        load_sources_samples_for_test()."""
        dialog = ConvertOptionsDialog(parent, initial=initial, bank_loader=bank_loader)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog._to_options()
