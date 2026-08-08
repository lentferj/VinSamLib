"""
MIDI note-number <-> note-name conversion, shared by the UI (the placement
editor's spinboxes, the sample-name preview) and by build/ (naming samples
after the key they play).

It lives here, above both, because build/ importing ui/ would be backwards
and duplicating twelve note names to avoid that would be worse. Same
octave-offset convention as build/sampledir_import.py's own parameter
(2=C3, 1=C4, 0=C5), which is the inverse of mpc2emu's
sampledir_parser._note_to_midi(); pure arithmetic, no mpc2emu needed.
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
