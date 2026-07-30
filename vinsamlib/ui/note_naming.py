"""
Shared MIDI <-> note-name conversion for the Sample Placement editor's
matrix spinboxes and piano keyboard labels -- uses the SAME octave-offset
convention as build/sampledir_import.py's own octave_offset parameter
(2=C3, 1=C4, 0=C5), so whatever "Middle C is:" choice an import used is
what the note names shown here reflect too.

Pure arithmetic, the exact inverse of mpc2emu's own
sampledir_parser._note_to_midi(); duplicated rather than imported since
this is generic MIDI note-number math, not mpc2emu-specific DSP/format
code (see mpc2emu_bridge.py's own "never edits mpc2emu, only wraps it"
rule -- this doesn't need mpc2emu at all).
"""

from __future__ import annotations

import re
from typing import Optional

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_NAME_RE = re.compile(r'^\s*([A-Ga-g])(#?)(-?\d+)\s*$')


def midi_to_name(midi: int, octave_offset: int) -> str:
    semitone = midi % 12
    octave = midi // 12 - octave_offset
    return f"{NOTE_NAMES[semitone]}{octave}"


def name_to_midi(text: str, octave_offset: int) -> Optional[int]:
    m = _NAME_RE.match(text)
    if not m:
        return None
    name = f"{m.group(1).upper()}{m.group(2)}"
    if name not in NOTE_NAMES:
        return None
    semitone = NOTE_NAMES.index(name)
    octave = int(m.group(3))
    return (octave + octave_offset) * 12 + semitone
