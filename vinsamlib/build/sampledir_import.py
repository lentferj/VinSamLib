"""
Imports a folder of WAV files (whose names carry the root note, e.g.
"Piano C3.wav", "Cello-A#2.wav", "Pad_60.wav") into a real, native
E4B/KRZ/EIII bank file, via mpc2emu's own
parse_sample_dir -> Bank -> [resample/reduce] -> write pipeline. Each
sample is auto-mapped to the keys nearest its root (split at the
midpoints between adjacent roots, key-tracked) into a single playable,
multisampled preset -- see mpc2emu's parsers/sampledir_parser.py.

Deliberately not offered from Explorer/the library tree: unlike an XPM
file or an already-native preset, a folder of loose WAVs isn't something
you browse to and recognize as "one importable thing" -- it's a special
case the user picks a folder for and decides on a case-by-case basis,
hence its own top-level "Import Sample Folder..." menu action instead.

Shares its ConversionOptions shape and its whole reduce/resample/write
tail with build/convert.py and build/xpm_import.py (see
build/convert.py's _apply_and_write()) -- this differs from XPM import
only in how the starting mpc2emu Bank gets parsed (a directory of loose
WAVs vs. an XPM's own zone/layer XML) and in accepting an explicit
octave-convention override XPM import has no equivalent for (an XPM's
zones already carry real MIDI key numbers; a bare WAV folder's filenames
don't, so where "middle C" falls has to come from somewhere).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .convert import ConversionOptions, _apply_and_write, _run_captured
from ..mpc2emu_bridge import sampledir_parser


def load_samples_for_test(dir_path: str, octave_offset: Optional[int] = None) -> list:
    """Read-only: parses just far enough to list samples, for the Convert
    Options dialog's stereo Test button -- same parse
    import_sample_dir() itself does, never writes anything."""
    bank = _run_captured(sampledir_parser.parse_sample_dir, dir_path,
                          octave_offset=octave_offset)
    return bank.samples


def import_sample_dir(dir_path: str, opts: ConversionOptions,
                       octave_offset: Optional[int] = None) -> str:
    """Parses a folder of WAV files (via mpc2emu's own parse_sample_dir)
    into a single multisampled preset and writes it out as a real E4B/
    KRZ/EIII bank file in a fresh temp dir, applying whatever resample/
    reduce options were chosen along the way -- see build/convert.py's
    _apply_and_write() for everything after the parse step. Returns the
    new file's path; never touches dir_path itself.

    octave_offset: 2=C3, 1=C4, 0=C5 -- which octave "middle C" (MIDI 60)
    falls on for filenames without an explicit octave (or a bare MIDI
    number). None lets mpc2emu auto-detect it from a majority vote across
    the folder's own filenames (its own CLI default)."""
    bank = _run_captured(sampledir_parser.parse_sample_dir, dir_path,
                          octave_offset=octave_offset)
    return _apply_and_write(bank, opts, Path(dir_path).name)
