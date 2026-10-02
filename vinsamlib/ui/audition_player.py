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

**And a second route, because ``QAudioSink`` is not enough on Linux.** Qt
implements exactly two Linux audio backends, PipeWire and PulseAudio; its own
documentation says audio there *requires* one of them, that the ALSA backend
is experimental and "will be deprecated in future versions of Qt", and it
never mentions JACK at all. The official PySide6 wheel is built without even
that ALSA backend. So on a JACK or bare-ALSA host -- which is what this
project is developed on -- ``QMediaDevices.audioOutputs()`` is empty while
``aplay -D default`` plays perfectly, because the routing lives in
``~/.asoundrc`` and is built by *libasound in-process*, not by any server Qt
can reach. Firefox gets there by falling back to ALSA; Qt has no ALSA to fall
back to.

That is a permanent property of the toolkit on such a host, not a version to
sit out, so playback has a second route: hand the rendered WAV to an external
player. It is not a workaround -- it reaches the audio through the same
interface every other program on the machine uses. ``QAudioSink`` stays the
preferred route where it works, because it needs no temporary file and gives
real transport control.
"""

from __future__ import annotations  # noqa: I001, RUF100

import os
import shutil
import sys
import tempfile
import wave
from pathlib import Path
from typing import List, Optional, Tuple  # noqa: UP035

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QProcess, Signal

try:
    # The state enum lives on QAudio (QtAudio), NOT on QAudioSink: PySide6
    # has no QAudioSink.State at all, so comparing against one raises
    # AttributeError on the first state change. That cannot fail here --
    # this sandbox has no audio device, so the handler never runs -- which
    # is exactly why it needed finding by reading the binding, not the tests.
    from PySide6.QtMultimedia import (
        QAudio,
        QAudioFormat,
        QAudioSink,  # noqa: I001, RUF100
        QMediaDevices,
    )

    _HAVE_MULTIMEDIA = True
except Exception:  # pragma: no cover - depends on the Qt build  # noqa: BLE001
    QAudio = QAudioFormat = QAudioSink = QMediaDevices = None
    _HAVE_MULTIMEDIA = False


def check_audio_output() -> Tuple[bool, str]:  # noqa: UP006
    """``(ok, reason)`` for the audio device -- the same shape as the mpc2emu
    capability probes, so callers can treat them alike.

    Under ``QT_QPA_PLATFORM=offscreen`` ``audioOutputs()`` is empty and
    44100/2/Int16 is unsupported, so this is not a defensive nicety: it is the
    path the test suite takes, and it must be a clean refusal with a legible
    reason.

    **The reason has to name the sound system, not just the absence.** We pick
    no sound system: ``QAudioSink`` is the whole of our audio output, and on
    Linux Qt 6.11 implements exactly two backends, PipeWire and PulseAudio --
    no ALSA, no JACK, and ``QT_AUDIO_BACKEND=alsa`` is silently ignored rather
    than refused. So on a JACK-on-raw-ALSA machine with no PulseAudio daemon
    -- which is what this project is developed on -- there is a working audio
    path that ``aplay`` reaches and Qt cannot. "No audio output device" is a
    false report there: the device is fine. Saying which two servers Qt needs
    turns an apparently broken feature into a legible one, and is the
    difference between the user checking their speakers and reading this line.
    """
    if not _HAVE_MULTIMEDIA:
        return False, "Qt multimedia is not available in this build"
    try:
        dev = QMediaDevices.defaultAudioOutput()
    except Exception as ex:  # noqa: BLE001
        return False, f"could not query audio devices: {ex}"
    if dev is None or dev.isNull():
        return False, _no_device_reason()
    got = negotiate_format()
    if got is None:
        return False, "the audio output has no usable format"
    rate, channels = got
    return True, f"{dev.description()} — {rate} Hz, {channels} ch"


def volume_gain(percent) -> float:
    """A 0-100 setting as a linear amplitude multiplier.

    Through Qt's own perceptual curve, so the control behaves like a volume
    control rather than like a multiplier: halfway down is 0.15 of full
    amplitude, which is what a listener expects, where a plain 0.5 is barely
    quieter at all.
    """
    pct = max(0, min(100, int(percent or 0))) / 100.0
    if not _HAVE_MULTIMEDIA:
        return pct * pct * pct  # a rough cube, same shape, no Qt
    return max(
        0.0,
        float(
            QAudio.convertVolume(
                pct,
                QAudio.VolumeScale.LogarithmicVolumeScale,
                QAudio.VolumeScale.LinearVolumeScale,
            )
        ),
    )


def scaled_pcm(pcm: bytes, gain: float) -> bytes:
    """`pcm` at `gain`, for the routes that cannot be told a volume.

    `QAudioSink` takes a volume; `aplay` does not. Rather than let the two
    routes play at different levels -- which would look like a defect in
    whichever one the user is on -- the external route scales the samples it
    is about to write to its temporary file. The USER'S file is never scaled:
    see `write_wav`.
    """
    if gain >= 0.999:
        return pcm
    if gain <= 0.0:
        return b"\x00" * len(pcm)
    import array

    a = array.array("h")
    a.frombytes(pcm)
    for i, v in enumerate(a):
        s = int(v * gain)
        a[i] = -32768 if s < -32768 else (32767 if s > 32767 else s)  # noqa: FURB136
    return a.tobytes()


def _no_device_reason() -> str:
    """Why there is no device, in the terms the user can act on.

    Qt reports an empty device list identically whether nothing is plugged in
    or whether the sound server it supports is simply not running, and those
    want opposite responses from the user. On Linux we can distinguish the
    second case cheaply enough to be worth it.
    """
    if sys.platform.startswith("linux"):
        # State the rule, not an assumption about this machine: on a PipeWire
        # host with a genuinely absent device, "so your JACK setup shows none"
        # would be a confident wrong explanation.
        return (
            "no audio output device — on Linux Qt reaches only PipeWire "
            "or PulseAudio, not ALSA or JACK directly"
        )
    return "no audio output device"


#: External players, tried in order, ``{}`` standing for the WAV path.
#:
#: THE ORDER IS AN INFERENCE, NOT A PREFERENCE. This list is only ever
#: consulted once ``QAudioSink`` has already reported no device -- and on
#: Linux that specifically means neither PipeWire nor PulseAudio is reachable,
#: since those are the only two backends Qt has. So the server-based players
#: are the ones LEAST likely to work here and go last, even though they would
#: be the obvious first choice in the abstract. ALSA first is not a guess
#: about what is best; it is what is left standing.
_LINUX_PLAYERS = (
    ("aplay", ["aplay", "-q", "{}"]),
    ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", "{}"]),
    ("mpv", ["mpv", "--no-video", "--really-quiet", "{}"]),
    ("paplay", ["paplay", "{}"]),
    ("pw-play", ["pw-play", "{}"]),
)
_DARWIN_PLAYERS = (("afplay", ["afplay", "{}"]),)
_WINDOWS_PLAYERS = (
    (
        "powershell",
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "(New-Object Media.SoundPlayer '{}').PlaySync()",
        ],
    ),
)


def _candidate_players():
    if sys.platform.startswith("linux"):
        return _LINUX_PLAYERS
    if sys.platform == "darwin":
        return _DARWIN_PLAYERS
    if os.name == "nt":
        return _WINDOWS_PLAYERS
    return ()


def find_external_player() -> Optional[Tuple[str, List[str]]]:  # noqa: UP006, UP045
    """``(name, argv_template)`` of the first player on PATH, or ``None``.

    Presence on PATH is all this can check. Whether the player can actually
    open the device is only knowable by running it -- ``aplay -D jack`` exists
    and fails on the development host -- so a failure at play time is reported
    rather than prevented, and that is why ``AuditionPlayer`` carries a
    ``failed`` signal at all.
    """
    for name, argv in _candidate_players():
        if shutil.which(argv[0]):
            return name, list(argv)
    return None


def playback_route() -> Tuple[str, str]:  # noqa: UP006
    """``(kind, description)`` for how audio can leave this process.

    ``kind`` is ``"qt"``, ``"external"`` or ``"none"``. Callers gate the Play
    button on this rather than on :func:`check_audio_output`, which answers the
    narrower question of whether *Qt* found a device -- a question whose
    answer is "no" on a perfectly working JACK desktop.
    """
    ok, why = check_audio_output()
    if ok:
        return "qt", why
    found = find_external_player()
    if found is not None:
        return "external", f"{found[0]} ({why})"
    return "none", why


def check_playback() -> Tuple[bool, str]:  # noqa: UP006
    """``(ok, reason)`` for playback by any route -- the probe the UI gates on."""
    kind, description = playback_route()
    if kind == "qt":
        return True, description
    if kind == "external":
        return True, f"via {description}"
    return False, f"{description}, and no external player on PATH"


def negotiate_format() -> Optional[Tuple[int, int]]:  # noqa: UP006, UP045
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


def write_wav_only(path, rendering, gain: float = 1.0) -> None:
    """The RIFF alone, with no report sidecar.

    Split out for the external-player route: that writes to a temporary file
    that is deleted as soon as playback ends, and dropping a ``.txt`` of
    caveats beside it would be litter nobody ever reads. Every WAV the *user*
    gets still travels with its report -- see :func:`write_wav`.
    """
    # Open the file ourselves. `wave.open(str(p))` constructs a Wave_write
    # BEFORE it opens, so a failed open leaves a half-built object whose
    # __del__ raises AttributeError on top of the real OSError, burying it.
    with open(Path(path), "wb") as fh:  # noqa: SIM117
        with wave.open(fh, "wb") as w:
            w.setnchannels(rendering.channels)
            w.setsampwidth(2)
            w.setframerate(rendering.rate)
            w.writeframes(
                rendering.pcm if gain >= 0.999 else scaled_pcm(rendering.pcm, gain)
            )


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
    write_wav_only(p, rendering)
    sidecar = p.with_suffix(p.suffix + ".txt")
    try:
        sidecar.write_text(rendering.report.as_text(), encoding="utf-8")
    except OSError:
        return None
    return sidecar


class AuditionPlayer(QObject):
    """Owns one playback, by whichever route works. Keep the instance alive.

    Two routes, tried in that order: ``QAudioSink`` where Qt found a device,
    and otherwise an external player fed a temporary WAV. The caller does not
    choose -- it calls :meth:`play` and reads :meth:`route` if it wants to say
    which one ran.
    """

    finished = Signal()
    #: Playback started and then failed -- a player that exists on PATH but
    #: cannot open the device. `aplay -D jack` does exactly this on the
    #: development host, so it is a real state and not a defensive one.
    failed = Signal(str)

    def __init__(self, parent=None, volume: int = 100):
        super().__init__(parent)
        self._sink = None
        self._buffer = None
        self._proc = None
        self._tmp = None
        self._route = "none"
        self._volume = volume

    @property
    def playing(self) -> bool:
        return self._sink is not None or self._proc is not None

    def route(self) -> str:
        """``"qt"``, ``"external"`` or ``"none"`` for the playback in flight."""
        return self._route

    def play(self, rendering) -> bool:
        """Play a rendering. Returns False when no route works at all."""
        self.stop()
        if self._play_via_qt(rendering):
            self._route = "qt"
            return True
        if self._play_via_external(rendering):
            self._route = "external"
            return True
        self._route = "none"
        return False

    # -- route 1: QAudioSink ------------------------------------------------

    def _play_via_qt(self, rendering) -> bool:
        if not _HAVE_MULTIMEDIA:
            return False
        dev = QMediaDevices.defaultAudioOutput()
        if dev is None or dev.isNull():
            return False
        fmt = QAudioFormat()
        fmt.setSampleRate(rendering.rate)
        fmt.setChannelCount(rendering.channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not dev.isFormatSupported(fmt):
            # Never play a format the device merely tolerates: it sounds at
            # the wrong pitch or with swapped channels, silently.
            fmt = dev.preferredFormat()
            if (
                int(fmt.sampleRate()) != rendering.rate
                or int(fmt.channelCount()) < rendering.channels
            ):
                return False
        self._sink = QAudioSink(dev, fmt, self)
        # Set BEFORE start(): a volume applied after the sink is running is
        # audible as a jump on the first note.
        self._sink.setVolume(volume_gain(self._volume))
        self._buffer = QBuffer(self)
        self._buffer.setData(QByteArray(rendering.pcm))
        self._buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self._sink.stateChanged.connect(self._on_state)
        self._sink.start(self._buffer)
        return True

    # -- route 2: an external player ---------------------------------------

    def _play_via_external(self, rendering) -> bool:
        found = find_external_player()
        if found is None:
            return False
        name, argv = found
        fd, tmp = tempfile.mkstemp(prefix="vinsamlib-audition-", suffix=".wav")
        os.close(fd)
        try:
            write_wav_only(tmp, rendering, gain=volume_gain(self._volume))
        except OSError:
            _unlink(tmp)
            return False
        args = [_substitute(a, tmp) for a in argv]
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        proc.setProgram(args[0])
        proc.setArguments(args[1:])
        proc.finished.connect(self._on_proc_finished)
        proc.errorOccurred.connect(self._on_proc_error)
        self._proc, self._tmp = proc, tmp
        self._player_name = name
        self._player_prog = Path(args[0]).name
        proc.start()
        if not proc.waitForStarted(3000):
            # Counts as no route rather than as a failure: the caller can
            # still fall through to its own message, and nothing was heard.
            self._teardown_proc()
            return False
        return True

    # -- shared -------------------------------------------------------------

    def stop(self) -> None:
        # Both Qt objects are parented to the player, so dropping the Python
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
        self._teardown_proc()

    def _teardown_proc(self) -> None:
        """Kill the player and delete its temp file. Safe to call twice."""
        proc, tmp = self._proc, self._tmp
        self._proc = None
        self._tmp = None
        if proc is not None:
            for sig, slot in (
                (proc.finished, self._on_proc_finished),
                (proc.errorOccurred, self._on_proc_error),
            ):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            try:
                if proc.state() != QProcess.ProcessState.NotRunning:
                    proc.kill()
                    # Without this the temp file below is unlinked while the
                    # player still has it open. Harmless on Linux, but on
                    # Windows the unlink simply fails and the file is leaked.
                    proc.waitForFinished(2000)
            except RuntimeError:
                pass
            proc.deleteLater()
        if tmp is not None:
            _unlink(tmp)

    def _on_state(self, state) -> None:
        if self.sender() is not self._sink:
            return  # an earlier sink finishing; not ours to act on
        if state == QAudio.State.IdleState:
            self.stop()
            self.finished.emit()

    def _on_proc_finished(self, code, status) -> None:
        if self.sender() is not self._proc:
            return  # an earlier player exiting; not ours to act on
        name = getattr(self, "_player_name", "the audio player")
        prog = getattr(self, "_player_prog", name)
        err = (
            bytes(self._proc.readAllStandardError()).decode("utf-8", "replace")
            if self._proc is not None
            else ""
        )
        bad = code != 0 or status != QProcess.ExitStatus.NormalExit
        self._teardown_proc()
        if bad:
            # A non-zero exit is the `aplay -D jack` case: the player was
            # there, the device was not. Reported, because the alternative is
            # a Play button that looks like it worked and made no sound.
            detail = _error_line(err, prog) or f"exit code {code}"
            # The player's own line usually already names it ("aplay: ..."),
            # and "aplay: aplay: ..." reads like a bug in us.
            self.failed.emit(
                detail if detail.startswith((name, prog)) else f"{name}: {detail}"
            )
        else:
            self.finished.emit()

    def _on_proc_error(self, error) -> None:
        if self.sender() is not self._proc:
            return
        name = getattr(self, "_player_name", "the audio player")
        self._teardown_proc()
        self.failed.emit(f"{name} could not be run ({error})")


def _substitute(arg: str, path: str) -> str:
    """Put ``path`` into one argv template element.

    Every player but one takes the path as an argv element of its own, where
    it needs no quoting at all -- QProcess passes arguments, not a shell line.
    The Windows entry is the exception: it embeds the path inside a PowerShell
    string literal, where a single quote in the path would end that literal
    early. PowerShell escapes a quote by doubling it. An apostrophe in a user
    name is all it takes to reach this, so it is handled rather than assumed
    away -- though it is UNTESTED, having been written on Linux.
    """
    if arg == "{}":
        return path
    return arg.replace("{}", path.replace("'", "''"))


def _error_line(err: str, program: str) -> str:
    """The one line of a player's stderr worth showing.

    NOT the last line. `aplay -D jack` on the development host prints its real
    complaint first and then an "Available formats:" list, so taking the tail
    reported `- FLOAT_LE` -- true, and useless. NOT simply the first line
    either: the ALSA JACK plugin prints pages of `Jack: ...` debug chatter
    ahead of everything. A tool's own error is conventionally
    ``progname: message``, so prefer that and fall back to the first line.
    """
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    named = [ln for ln in lines if ln.startswith(f"{program}:")]
    if named:
        return named[0]
    return lines[0] if lines else ""


def _unlink(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
