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

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QTextBrowser,
                               QVBoxLayout)

from ..audition.caveats import SEVERITY_ORDER
from .audition_player import (AuditionPlayer, check_playback, write_wav)

HEADER = ("This is a model of the preset's parameters. It is not a model of "
          "the sampler, and it will not sound like the hardware.")


class AuditionDialog(QDialog):
    """Holds the player and the rendered audio for one audition."""

    def __init__(self, rendering, title: str = "Audition", parent=None):
        super().__init__(parent)
        self._rendering = rendering
        self._player = AuditionPlayer(self)
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
        self._stop_btn.clicked.connect(self._player.stop)
        self._save_btn = QPushButton("Save as WAV…")
        self._save_btn.clicked.connect(self._on_save)
        transport.addWidget(self._play_btn)
        transport.addWidget(self._stop_btn)
        transport.addStretch(1)
        transport.addWidget(self._save_btn)
        layout.addLayout(transport)

        close_row = QHBoxLayout()
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
        self._stop_btn.setEnabled(ok)
        self._play_btn.setToolTip(reason)
        if not ok:
            self._play_btn.setText("No audio output")

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
                f"<p><b>{severity.name}</b> "
                f"<span style='color:gray'>({severity.value})</span></p><ul>")
            for c in group:
                parts.append(f"<li><b>{_esc(c.subject)}</b> — "
                             f"{_esc(c.text)}</li>")
            parts.append("</ul>")
        parts.append("</body></html>")
        return "".join(parts)

    def rendered_text(self) -> str:
        """The report as the user reads it -- what test 10.5 asserts on."""
        return self._report.toPlainText()

    # -- transport ----------------------------------------------------------

    def _on_play(self) -> None:
        if not self._player.play(self._rendering):
            ok, reason = check_playback()
            self._play_btn.setEnabled(False)
            self._play_btn.setText("No audio output")
            self._play_btn.setToolTip(reason)
            return
        self._stop_btn.setEnabled(True)
        self._play_btn.setText("Replay")
        if self._player.route() == "external":
            # Worth saying: the external route has no position reporting and
            # Stop kills a process, so "Replay" behaves slightly differently
            # from the Qt route and the tooltip should not claim otherwise.
            self._play_btn.setToolTip(
                "playing through an external player — Qt found no audio "
                "device on this system")

    def _on_finished(self) -> None:
        self._stop_btn.setEnabled(False)

    def _on_failed(self, why: str) -> None:
        """A player that was there and could not open the device.

        Deliberately NOT a modal. The button is the thing just clicked, so the
        message lands where the user is already looking; a QMessageBox here
        would be a fourth raise site in this package for a case the transport
        can state itself. Save as WAV… remains, and the tooltip carries the
        player's own last line of stderr.
        """
        self._stop_btn.setEnabled(False)
        self._play_btn.setText("Playback failed")
        self._play_btn.setToolTip(
            f"{why}\n\nUse Save as WAV… and play the file yourself.")

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
                self, "Save Audition",
                f"Could not write {Path(path).name}\n\n{exc.strerror or exc}")
            return
        if sidecar is None:
            QMessageBox.warning(
                self, "Save Audition",
                f"{Path(path).name} was written, but its report could not be "
                f"saved beside it.\n\nThe audio now travels with none of the "
                f"caveats that say what it is.")
        self._save_btn.setText(f"Saved {Path(path).name}")

    def closeEvent(self, event) -> None:
        self._player.stop()
        super().closeEvent(event)


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
