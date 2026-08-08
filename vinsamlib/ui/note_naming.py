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

# Moved to vinsamlib/notes.py so build/ can use it too without importing ui/.
# Re-exported here because this is the name the placement editor and the
# piano keyboard already import.
from ..notes import NOTE_NAMES, midi_to_name, name_to_midi  # noqa: F401
