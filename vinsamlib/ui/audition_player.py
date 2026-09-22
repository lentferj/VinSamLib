"""
Audio output for audition, and the device probe the Settings/menu gates read.

**Pull mode, not push.** The whole buffer is already rendered, so it is
wrapped in a ``QBuffer`` and handed to ``QAudioSink.start(buffer)``. Push
mode's ``bytesFree()`` dance buys nothing here and is where every Qt audio
race lives.

Format negotiation happens **before** rendering, and its result is passed
into the renderer -- so render rate and sink rate are never decided in two
places (spec §8.6, risk 8).

References to the sink AND the buffer are held by whoever owns the player.
Under PySide6 a collected ``QBuffer`` stops playback mid-note with no error,
the same ownership hazard ``workers.py`` documents.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Optional, Tuple

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Signal

try:
    # The state enum lives on QAudio (QtAudio), NOT on QAudioSink: PySide6
    # has no QAudioSink.State at all, so comparing against one raises
    # AttributeError on the first state change. That cannot fail here --
    # this sandbox has no audio device, so the handler never runs -- which
    # is exactly why it needed finding by reading the binding, not the tests.
    from PySide6.QtMultimedia import (QAudio, QAudioFormat, QAudioSink,
                                      QMediaDevices)
    _HAVE_MULTIMEDIA = True
except Exception:  # pragma: no cover - depends on the Qt build
    QAudio = QAudioFormat = QAudioSink = QMediaDevices = None
    _HAVE_MULTIMEDIA = False


def check_audio_output() -> Tuple[bool, str]:
    """``(ok, reason)`` for the audio device -- the same shape as the mpc2emu
    capability probes, so callers can treat them alike.

    Under ``QT_QPA_PLATFORM=offscreen`` ``audioOutputs()`` is empty and
    44100/2/Int16 is unsupported, so this is not a defensive nicety: it is the
    path the test suite takes, and it must be a clean refusal with a legible
    reason.
    """
    if not _HAVE_MULTIMEDIA:
        return False, "Qt multimedia is not available in this build"
    try:
        dev = QMediaDevices.defaultAudioOutput()
    except Exception as ex:
        return False, f"could not query audio devices: {ex}"
    if dev is None or dev.isNull():
        return False, "no audio output device"
    got = negotiate_format()
    if got is None:
        return False, "the audio output has no usable format"
    rate, channels = got
    return True, f"{dev.description()} — {rate} Hz, {channels} ch"


def negotiate_format() -> Optional[Tuple[int, int]]:
    """The (rate, channels) to render at, or None if there is no device.

    Prefers 44100/2/Int16; falls back to the device's own preferred format
    when that is not supported. The result is the one rate the whole render
    runs at.
    """
    if not _HAVE_MULTIMEDIA:
        return None
    dev = QMediaDevices.defaultAudioOutput()
    if dev is None or dev.isNull():
        return None
    fmt = QAudioFormat()
    fmt.setSampleRate(44100)
    fmt.setChannelCount(2)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    if not dev.isFormatSupported(fmt):
        fmt = dev.preferredFormat()
    rate = int(fmt.sampleRate()) or 44100
    channels = 2 if int(fmt.channelCount()) >= 2 else 1
    return rate, channels


def write_wav(path, rendering):
    """Write a ``Rendering`` as a RIFF WAV, plus a ``.txt`` sidecar report.

    This is not a convenience: it is the path that works with no audio device
    and the one a headless test can assert on.

    Raises ``OSError`` if the WAV itself cannot be written. Returns the
    sidecar path, or ``None`` if only the sidecar failed -- the audio is
    then on disc but its caveats are not, and the caller says so rather
    than letting a WAV travel with nothing attached.
    """
    p = Path(path)
    # Open the file ourselves. `wave.open(str(p))` constructs a Wave_write
    # BEFORE it opens, so a failed open leaves a half-built object whose
    # __del__ raises AttributeError on top of the real OSError, burying it.
    with open(p, "wb") as fh:
        with wave.open(fh, "wb") as w:
            w.setnchannels(rendering.channels)
            w.setsampwidth(2)
            w.setframerate(rendering.rate)
            w.writeframes(rendering.pcm)
    sidecar = p.with_suffix(p.suffix + ".txt")
    try:
        sidecar.write_text(rendering.report.as_text(), encoding="utf-8")
    except OSError:
        return None
    return sidecar


class AuditionPlayer(QObject):
    """Owns one ``QAudioSink`` and its buffer. Keep the instance alive."""

    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sink = None
        self._buffer = None

    @property
    def playing(self) -> bool:
        return self._sink is not None

    def play(self, rendering) -> bool:
        """Play a rendering. Returns False when there is no device."""
        if not _HAVE_MULTIMEDIA:
            return False
        dev = QMediaDevices.defaultAudioOutput()
        if dev is None or dev.isNull():
            return False
        self.stop()
        fmt = QAudioFormat()
        fmt.setSampleRate(rendering.rate)
        fmt.setChannelCount(rendering.channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not dev.isFormatSupported(fmt):
            # Never play a format the device merely tolerates: it sounds at
            # the wrong pitch or with swapped channels, silently.
            fmt = dev.preferredFormat()
            if (int(fmt.sampleRate()) != rendering.rate
                    or int(fmt.channelCount()) < rendering.channels):
                return False
        self._sink = QAudioSink(dev, fmt, self)
        self._buffer = QBuffer(self)
        self._buffer.setData(QByteArray(rendering.pcm))
        self._buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self._sink.stateChanged.connect(self._on_state)
        self._sink.start(self._buffer)
        return True

    def stop(self) -> None:
        # Both objects are parented to the player, so dropping the Python
        # reference does NOT free them -- the C++ children outlive it and
        # every Replay adds another sink holding a device handle. Detach the
        # signal first: an outgoing sink keeps emitting stateChanged on its
        # way down, and a stale Idle used to null the buffer belonging to the
        # playback that had just replaced it.
        sink, buffer = self._sink, self._buffer
        self._sink = None
        self._buffer = None
        if sink is not None:
            try:
                sink.stateChanged.disconnect(self._on_state)
            except (RuntimeError, TypeError):
                pass
            try:
                sink.stop()
            except RuntimeError:
                pass
            sink.deleteLater()
        if buffer is not None:
            try:
                buffer.close()
            except RuntimeError:
                pass
            buffer.deleteLater()

    def _on_state(self, state) -> None:
        if self.sender() is not self._sink:
            return          # an earlier sink finishing; not ours to act on
        if state == QAudio.State.IdleState:
            self.stop()
            self.finished.emit()
