"""
Audition: render a preset's parameters to audio, headless.

Public API. **This package imports no Qt** -- the engine is runnable from a
test with no ``QApplication``. All Qt lives under ``vinsamlib/ui/``.

The one sentence that governs everything here, from the spec's §9:

    This is a model of the preset's parameters. It is not a model of the
    sampler, and it will not sound like the hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from ..notes import name_to_midi
from .caveats import AuditionReport, Caveat, Severity
from .params import AuditionError, SourceProvenance
from .voice import Sounding

NOTE_SEP = ","

#: The octave convention this project shows everywhere: C3 = MIDI 60.
OCTAVE_OFFSET = 2


@dataclass(frozen=True)
class AuditionOptions:
    notes: Tuple[int, ...]          # MIDI numbers, in play order
    velocity: int                   # 1..127
    hold_seconds: float             # note-on to note-off
    gap_seconds: float              # silence between notes
    render_rate: int                # Hz, negotiated from the device
    channels: int                   # 1 or 2
    headroom_db: float = -6.0


@dataclass
class Rendering:
    pcm: bytes                      # interleaved signed 16-bit LE
    rate: int
    channels: int
    peak_before_limit: float        # 0..N; >1.0 means the limiter engaged
    report: AuditionReport
    seconds: float

    @property
    def frame_count(self) -> int:
        if self.channels <= 0:
            return 0
        return len(self.pcm) // (2 * self.channels)


def parse_notes(text: str) -> list:
    """``'C3, G3, 72'`` -> ``[60, 67, 72]``.

    Raises ``ValueError`` with the offending token. Used by BOTH the Settings
    validator and the renderer, so the field cannot accept what the renderer
    rejects.

    Note names use the C3 = 60 convention this project shows everywhere
    (``ui/note_naming.py``); bare MIDI integers work too. Out-of-range values
    (<0, >127) are a ``ValueError`` naming the token, not a clamp.
    """
    notes: list = []
    if text is None:
        raise ValueError("no notes given")
    for raw in str(text).split(NOTE_SEP):
        token = raw.strip()
        if not token:
            raise ValueError("empty note in list")
        note = None
        try:
            note = int(token, 10)
        except ValueError:
            note = name_to_midi(token, OCTAVE_OFFSET)
        if note is None:
            raise ValueError(f"{token!r} is not a note name or MIDI number")
        if not (0 <= note <= 127):
            raise ValueError(
                f"{token!r} is MIDI {note}, outside the playable range 0–127")
        notes.append(note)
    if not notes:
        raise ValueError("no notes given")
    return notes


def available(config) -> Tuple[bool, str]:
    """Whether the mpc2emu side can support audition.

    The device probe lives in ``ui/audition_player``; this checks only the
    model, parsers and the two processors audition reuses.
    """
    if config is None:
        from ..config import Config
        config = Config.load()
    return config.check_audition_support()


def acceleration_note() -> str:
    """'' when numpy is present; otherwise the one-line speed note.

    numpy is an accelerator, never a gate (spec §7.2). 'Install numpy to hear
    anything' is a worse product than 'that took four seconds; with numpy it
    takes one'.
    """
    from . import render as _render
    if _render._np is not None:
        return ""
    return ("Audition is running on the pure-Python renderer; installing "
            "numpy makes long presets render several times faster.")


def render_node(payload, kind: str, opts: AuditionOptions) -> Rendering:
    """Explorer TreeNode payload -> audio. Runs on a worker thread."""
    from . import params as _params
    from . import render as _render
    parsed = _params.parameters_for_node(payload, kind)
    return _render.render(parsed.bank, parsed.preset, parsed.provenance, opts)


def render_staged(bank, preset_obj, opts: AuditionOptions,
                  name: str = "") -> Rendering:
    """New Bank's ``(bank, preset_obj, name)`` tuple -> audio."""
    from . import params as _params
    from . import render as _render
    parsed = _params.parameters_for_staged(bank, preset_obj, name)
    return _render.render(parsed.bank, parsed.preset, parsed.provenance, opts)


__all__ = [
    "NOTE_SEP", "OCTAVE_OFFSET", "AuditionOptions", "Rendering",
    "parse_notes", "available", "acceleration_note", "render_node",
    "render_staged", "AuditionReport", "Caveat", "Severity",
    "AuditionError", "SourceProvenance", "Sounding",
]
