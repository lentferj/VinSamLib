"""
The audition dialog: the unconditional header, the report, transport, and
Save as WAV.

The header is shown every time, above everything, never reworded to sound
better:

    This is a model of the preset's parameters. It is not a model of the
    sampler, and it will not sound like the hardware.

Save as WAV is not a convenience. It is the path that works with no audio
device and the one a headless test can assert on; it writes a sibling ``.txt``
carrying ``report.as_text()``.
"""

from __future__ import annotations  # noqa: I001

from pathlib import Path

from PySide6.QtCore import Qt  # noqa: F401
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from ..audition.caveats import SEVERITY_ORDER
from .audition_player import AuditionPlayer, check_playback, write_wav

HEADER = (
    "This is a model of the preset's parameters. It is not a model of "
    "the sampler, and it will not sound like the hardware."
)


class AuditionDialog(QDialog):
    """Holds the player and the rendered audio for one audition."""

    #: The user unticked "show this every time". Emitted rather than written
    #: straight to the Config, because the window that owns the setting also
    #: owns the menu item that mirrors it -- two places writing it separately
    #: is how a checkbox and a menu tick come to disagree.
    showReportChanged = Signal(bool)
    #: The user ticked/unticked "Play automatically". Same one-writer rule as
    #: showReportChanged: the window that owns the setting owns the menu item
    #: that mirrors it.
    autoPlayChanged = Signal(bool)

    def __init__(
        self,
        rendering,
        title: str = "Audition",
        parent=None,
        show_report_default: bool = True,
        auto_play_default: bool = True,
        volume: int = 100,
        player=None,
    ):
        super().__init__(parent)
        self._rendering = rendering
        # `player` is a RUNNING player handed over by the notice window, so
        # opening the report does not interrupt the sound it describes.
        self._player = (
            player if player is not None else AuditionPlayer(self, volume=volume)
        )
        if player is not None:
            self._player.setParent(self)
        self._player.finished.connect(self._on_finished)
        self._player.failed.connect(self._on_failed)
        self.setWindowTitle(title)
        self.setMinimumSize(560, 420)

        layout = QVBoxLayout(self)

        header = QLabel(HEADER)
        header.setWordWrap(True)
        header.setStyleSheet("font-weight: 600;")
        layout.addWidget(header)

        self._report = QTextBrowser()
        self._report.setOpenExternalLinks(False)
        self._report.setHtml(self._render_report_html())
        layout.addWidget(self._report, 1)

        transport = QHBoxLayout()
        self._play_btn = QPushButton("Play")
        self._play_btn.clicked.connect(self._on_play)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self._on_stop)
        self._save_btn = QPushButton("Save as WAV…")
        self._save_btn.clicked.connect(self._on_save)
        transport.addWidget(self._play_btn)
        transport.addWidget(self._stop_btn)
        transport.addStretch(1)
        transport.addWidget(self._save_btn)
        layout.addLayout(transport)

        close_row = QHBoxLayout()
        # The opt-out sits beside Close, where someone who has finished
        # reading is already looking. It says what it does in the positive,
        # so the ticked state is the one that matches what is on screen.
        self._show_again = QCheckBox("Show this report every time")
        self._show_again.setChecked(bool(show_report_default))
        self._show_again.setToolTip(
            "Off: an audition plays straight away behind a small notice.\n"
            "The report still opens when there is no way to play it, and\n"
            "Save as WAV\u2026 always writes it beside the audio."
        )
        self._show_again.toggled.connect(self.showReportChanged)
        close_row.addWidget(self._show_again)
        # Beside the opt-out above, and for the same reason: both are
        # decisions about what happens NEXT time, made by someone who has just
        # heard (or failed to hear) this audition, which is the only moment the
        # question is worth asking. Order is deliberate -- what to PLAY first,
        # then whether to be shown the paperwork about it.
        self._auto_play = QCheckBox("Play automatically")
        self._auto_play.setChecked(bool(auto_play_default))
        self._auto_play.setToolTip(
            "On: an audition starts playing as soon as it is ready, and you\n"
            "can still press Play or Stop at any point.\n"
            "Off: it waits for you, which is worth it when auditioning a list\n"
            "of presets and you only want to hear one of them."
        )
        self._auto_play.toggled.connect(self.autoPlayChanged)
        close_row.addWidget(self._auto_play)
        close_row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        close_row.addWidget(close)
        layout.addLayout(close_row)

        # A device may have vanished between the menu being offered and here.
        # Gated on playback BY ANY ROUTE, not on Qt having found a device:
        # Qt finds none on a working JACK desktop, and disabling Play there
        # would refuse a machine that can in fact play the audio.
        ok, reason = check_playback()
        self._play_btn.setEnabled(ok)
        self._play_btn.setToolTip(reason)
        if not ok:
            self._play_btn.setText("No audio output")
        self._sync_transport()

        # START PLAYING, unless there is a reason not to.
        #
        # Waiting for the show is not optional here. The caller
        # (`main_window._show_audition_report`) does `dialog.show()` AFTER
        # this constructor returns, and a QAudioSink started on a window that
        # has never been shown plays into an output device that has not been
        # activated yet -- on this desktop the first sample came out clipped
        # and the rest of the note was dropped. Waiting is also what makes
        # this correct for the "Show report" hand-off, where the player
        # arrives already RUNNING and must not be restarted.
        #
        # `showEvent`, not a `showed` signal: this PySide6 has no `showed` on
        # QDialog (it arrived in Qt 6.7 and is absent here), and the event
        # override is the hook that has always existed.
        self._autoplay_pending = bool(auto_play_default)
        if self._player is not None and self._player.playing:
            self._autoplay_pending = False

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._autoplay_pending:
            self._autoplay_if_wanted()

    # -- report -------------------------------------------------------------

    def _render_report_html(self) -> str:
        report = self._rendering.report
        parts = ["<html><body>"]
        if not len(report):
            parts.append("<p><i>No caveats were raised for this preset.</i></p>")
        for severity in SEVERITY_ORDER:
            group = report.of(severity)
            if not group:
                continue
            parts.append(
                f"<p><b>{severity.heading}</b> "
                f"<span style='color:gray'>({severity.value})</span></p><ul>"
            )
            for c in group:
                parts.append(f"<li><b>{_esc(c.subject)}</b> — " f"{_esc(c.text)}</li>")
            parts.append("</ul>")
        parts.append("</body></html>")
        return "".join(parts)

    def rendered_text(self) -> str:
        """The report as the user reads it -- what test 10.5 asserts on."""
        return self._report.toPlainText()

    # -- transport ----------------------------------------------------------

    def _on_stop(self) -> None:
        self._player.stop()
        self._sync_transport()

    def _sync_transport(self) -> None:
        """Make the buttons say what the player is actually doing.

        The label is a STATE, not a history. It used to be set to "Replay"
        when play was pressed and left there, which was harmless while this
        window always started silent -- and became wrong the moment it could
        open over a note already sounding, or be looked at again after one
        had finished. "Replay" means "there is sound now"; "Play" means there
        is not.
        """
        playing = bool(self._player is not None and self._player.playing)
        self._stop_btn.setEnabled(playing)
        if self._play_btn.isEnabled():
            self._play_btn.setText("Replay" if playing else "Play")

    def _on_play(self) -> None:
        if not self._player.play(self._rendering):
            ok, reason = check_playback()  # noqa: RUF059
            self._play_btn.setEnabled(False)
            self._play_btn.setText("No audio output")
            self._play_btn.setToolTip(reason)
            return
        self._sync_transport()
        if self._player.route() == "external":
            # Worth saying: the external route has no position reporting and
            # Stop kills a process, so "Replay" behaves slightly differently
            # from the Qt route and the tooltip should not claim otherwise.
            self._play_btn.setToolTip(
                "playing through an external player — Qt found no audio "
                "device on this system"
            )

    def _autoplay_if_wanted(self) -> None:
        """Start playing on the first show, then never again on its own.

        One-shot by construction: `_autoplay_pending` is cleared first, so
        re-showing a window the user had closed and reopened does not restart
        audio they had deliberately stopped. The checkbox remains live, so
        ticking it mid-session affects the NEXT audition rather than this one
        -- which is the honest reading of a setting about what happens when an
        audition opens.
        """
        if not self._autoplay_pending:
            return
        self._autoplay_pending = False
        if not self._play_btn.isEnabled():
            # No route: the button already says "No audio output" and the
            # tooltip carries the reason. Pressing it here would only replace
            # that with a second, vaguer failure.
            return
        self._on_play()

    def _on_finished(self) -> None:
        self._sync_transport()

    def _on_failed(self, why: str) -> None:
        """A player that was there and could not open the device.

        Deliberately NOT a modal. The button is the thing just clicked, so the
        message lands where the user is already looking; a QMessageBox here
        would be a fourth raise site in this package for a case the transport
        can state itself. Save as WAV… remains, and the tooltip carries the
        player's own last line of stderr.
        """
        self._stop_btn.setEnabled(False)
        self._play_btn.setText("Playback failed")  # not a state: an outcome
        self._play_btn.setToolTip(
            f"{why}\n\nUse Save as WAV… and play the file yourself."
        )

    def _on_save(self) -> None:
        # An instance rather than getSaveFileName(), only because the static
        # helper cannot set a default suffix: a name typed without ".wav"
        # otherwise produces a file the desktop will not open, and the
        # sidecar lands beside it under a matching stem either way.
        dlg = QFileDialog(self, "Save Audition as WAV")
        dlg.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dlg.setNameFilter("WAV audio (*.wav)")
        dlg.setDefaultSuffix("wav")
        dlg.selectFile("audition.wav")
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        if not dlg.exec():
            return
        chosen = dlg.selectedFiles()
        if not chosen:
            return
        path = chosen[0]
        try:
            sidecar = write_wav(path, self._rendering)
        except OSError as exc:
            # A full disc, a read-only card, a path that vanished between the
            # dialog and the write. Silently leaving the button reading
            # "Save as WAV…" would look like nothing happened, and with no
            # audio device this is the ONLY way to hear the render at all --
            # a failure here has to be said out loud.
            QMessageBox.warning(
                self,
                "Save Audition",
                f"Could not write {Path(path).name}\n\n{exc.strerror or exc}",
            )
            return
        if sidecar is None:
            QMessageBox.warning(
                self,
                "Save Audition",
                f"{Path(path).name} was written, but its report could not be "
                f"saved beside it.\n\nThe audio now travels with none of the "
                f"caveats that say what it is.",
            )
        self._save_btn.setText(f"Saved {Path(path).name}")

    def done(self, result: int) -> None:
        """Every way out of this dialog stops the sound.

        `closeEvent` alone was not enough and the difference is a Qt trap:
        `QDialog.reject()` -- which the Close button calls, and which Escape
        calls for free -- HIDES the dialog without a close event, so the
        player went on playing a preset whose window was gone. Only the
        window manager's X fired `closeEvent`, so the X worked and the button
        did not. `done()` is the one choke point both paths pass through.
        """
        self._player.stop()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._player.stop()
        super().closeEvent(event)


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class AuditionNotice(QDialog):
    """ "Auditioning <preset>" — plays at once and closes itself when done.

    What the report window becomes once the user has opted out of it. It owns
    the player for the same reason the dialog does: a collected ``QBuffer``
    stops playback mid-note under PySide6 with no error.

    THREE THINGS IT MUST NOT DO, each of which would make opting out a way to
    lose information rather than a way to skip a window:

    * vanish on a failure. A player that cannot open the device closes this
      with its reason shown, and does not auto-close on top of it;
    * hide the caveats for good. ``Show report`` reopens the full window for
      this audition, and the setting is still in View and in that window;
    * start silently when nothing can play. The caller checks for a route
      first and falls back to the report window, which holds Save as WAV.
    """

    #: Ask the owner to open the full report for this same rendering. Carries
    #: the PLAYER, so the sound does not stop to show the report.
    reportRequested = Signal(object)

    def __init__(self, rendering, name: str, parent=None, volume: int = 100):
        super().__init__(parent)
        self._rendering = rendering
        self.setWindowTitle("Audition")
        from .audition_player import AuditionPlayer

        self._player = AuditionPlayer(self, volume=volume)
        self._player.finished.connect(self._on_finished)
        self._player.failed.connect(self._on_failed)

        layout = QVBoxLayout(self)
        self._label = QLabel(f"Auditioning {name}…")
        self._label.setWordWrap(True)
        self._label.setStyleSheet("font-weight: 600;")
        layout.addWidget(self._label)
        self._sub = QLabel(HEADER)
        self._sub.setWordWrap(True)
        self._sub.setStyleSheet("color: palette(placeholdertext);" " font-size: 11px;")
        layout.addWidget(self._sub)

        row = QHBoxLayout()
        self._report_btn = QPushButton("Show report")
        self._report_btn.clicked.connect(self._on_report)
        row.addWidget(self._report_btn)
        row.addStretch(1)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self._on_stop)
        row.addWidget(self._stop_btn)
        layout.addLayout(row)
        self.setMinimumWidth(360)

    def start(self) -> bool:
        """Begin playback. False when no route would play (caller falls back
        to the full report window, which is the only way to hear it then)."""
        return self._player.play(self._rendering)

    def _on_finished(self) -> None:
        self.accept()

    def _on_failed(self, why: str) -> None:
        # Stays open: the whole point of the notice is that it is the only
        # thing on screen, so closing it on a failure would leave the user
        # with a click that did nothing and no reason anywhere.
        self._label.setText("Audition could not play")
        self._sub.setText(
            f"{why}\n\nUse Show report to save it as a WAV " f"and play it yourself."
        )
        self._stop_btn.setEnabled(False)

    def _on_stop(self) -> None:
        if self._player is not None:
            self._player.stop()
        self.reject()

    def detach_player(self):
        """Give up the player WITHOUT stopping it, and stop owning it.

        The player is a Qt CHILD of this window, so closing the notice would
        destroy it and the note would cut off mid-way. Handing it over means
        reparenting it first and dropping our own reference, or `closeEvent`
        below would stop the very playback we just gave away.
        """
        player = self._player
        if player is None:
            return None
        for sig, slot in (
            (player.finished, self._on_finished),
            (player.failed, self._on_failed),
        ):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        player.setParent(None)
        self._player = None
        return player

    def _on_report(self) -> None:
        # Deliberately NOT stop(). Asking to see the report is not asking for
        # silence -- the report describes what you are listening to, and
        # having to restart the sound to read about it is the opposite of
        # what the button is for.
        self.reportRequested.emit(self.detach_player())
        self.accept()

    def done(self, result: int) -> None:
        # Same Qt trap as the report window: Escape and any `reject()` hide a
        # QDialog without a close event. None once the player has been handed
        # to the report window, which is the one case that must NOT stop it.
        if self._player is not None:
            self._player.stop()
        super().done(result)

    def closeEvent(self, event) -> None:
        # None once the player has been handed to the report window.
        if self._player is not None:
            self._player.stop()
        super().closeEvent(event)


class AuditionProgress(QDialog):
    """ "Preparing audition of <preset>" — up immediately, gone when it is.

    The window that was missing. Rendering a big multisample takes seconds,
    and until this existed the only sign was a line in the status bar: the
    audition simply appeared to do nothing, then a window arrived. A 340 MB
    library folder makes that several seconds of apparent nothing.

    Shown for EVERY audition, cache hits excepted (those never get here --
    the caller looks the render up on the GUI thread and skips straight to
    playing it). It carries Cancel, because the honest answer to "this is
    taking too long" is a way to stop it, not a faster spinner.
    """

    cancelled = Signal()

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Audition")
        layout = QVBoxLayout(self)
        self._label = QLabel(f"Preparing audition of {name}…")
        self._label.setWordWrap(True)
        self._label.setStyleSheet("font-weight: 600;")
        layout.addWidget(self._label)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        # No text on the bar: the phase line below says more than a number,
        # and two readouts of the same thing disagree the moment one lags.
        self._bar.setTextVisible(False)
        layout.addWidget(self._bar)

        self._phase = QLabel("Starting…")
        self._phase.setStyleSheet(
            "color: palette(placeholdertext);" " font-size: 11px;"
        )
        layout.addWidget(self._phase)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self._on_cancel)
        row.addWidget(cancel)
        layout.addLayout(row)
        self.setMinimumWidth(340)

    def set_progress(self, percent: int, text: str) -> None:
        self._bar.setValue(max(0, min(100, int(percent))))
        if text:
            self._phase.setText(text)

    def _on_cancel(self) -> None:
        self.cancelled.emit()
        self.reject()
