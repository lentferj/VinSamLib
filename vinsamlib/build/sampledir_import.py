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

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

from .convert import ConversionOptions, _apply_and_write, _run_captured
from ..mpc2emu_bridge import sampledir_parser


_AUDIO_EXTS = (".wav", ".aif", ".aiff")


def stage_files(paths: list[str]) -> tempfile.TemporaryDirectory:
    """Gather hand-picked audio files into one directory, so everything below
    can go on taking a directory.

    mpc2emu's parse_sample_dir() reads a *folder* and rglobs it, which is the
    right shape when a folder holds one instrument -- and the wrong one when
    it holds three, or when only half its files belong together. Rather than
    grow a second parser entry point, the selection becomes a directory.

    Linked, never copied where the filesystem allows it: a multisample runs to
    tens of megabytes and this is read exactly once. Filenames are kept
    EXACTLY as they are -- they carry the root note, and `Piano C3 (2).wav`
    would no longer parse as C3 -- so a name that appears twice goes into a
    numbered subdirectory instead of being renamed. parse_sample_dir() walks
    the tree, so it finds them either way.

    The caller owns the returned TemporaryDirectory and must keep it alive
    until the import (and the options dialog's preview, which re-reads it)
    is done."""
    staged = tempfile.TemporaryDirectory(prefix="vinsamlib_samples_")
    root = Path(staged.name)
    seen: dict[str, int] = {}
    for src in paths:
        source = Path(src)
        n = seen.get(source.name, 0)
        seen[source.name] = n + 1
        target_dir = root if n == 0 else root / f"same-name-{n}"
        target_dir.mkdir(exist_ok=True)
        target = target_dir / source.name
        try:
            os.symlink(source, target)
        except (OSError, NotImplementedError, AttributeError):
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
    return staged


def selection_label(paths: list[str]) -> str:
    """A name for a preset built from hand-picked files. The part their names
    share reads best (`Rhodes C2.wav`, `Rhodes F3.wav` -> "Rhodes"); with
    nothing in common, the folder they came from is a better answer than the
    first file's name."""
    stems = [Path(p).stem for p in paths]
    common = os.path.commonprefix(stems) if stems else ""
    # Back off to a separator: the shared part of "channel1-note36" and
    # "channel1-note37" is "channel1-note3", which is a cut through the middle
    # of a number, not a name.
    cut = max((common.rfind(c) for c in " _-."), default=-1)
    if cut > 0 and len(common) < max(len(s) for s in stems):
        common = common[:cut]
    common = common.strip(" _-.")
    if len(common) >= 3:
        return common
    parents = {Path(p).parent for p in paths}
    if len(parents) == 1:
        return parents.pop().name
    return "Imported Samples"


def load_samples_for_test(dir_path: str, octave_offset: Optional[int] = None) -> list:
    """Read-only: parses just far enough to list samples, for the Convert
    Options dialog's stereo Test button -- same parse
    import_sample_dir() itself does, never writes anything."""
    bank = _run_captured(sampledir_parser.parse_sample_dir, dir_path,
                          octave_offset=octave_offset)
    return bank.samples


def parse_preview(dir_path: str, octave_offset: Optional[int] = None) -> Any:
    """Read-only: the same parse import_sample_dir() itself does, for the
    Sample Placement dialog -- never writes anything. Returns the full
    mpc2emu Bank (one preset, one voice, one zone per sample; see
    parsers/sampledir_parser.py) so a caller can inspect or override each
    zone's auto-computed key-range assignment before the real import
    commits to it."""
    return _run_captured(sampledir_parser.parse_sample_dir, dir_path,
                          octave_offset=octave_offset)


def import_sample_dir(dir_path: str, opts: ConversionOptions,
                       octave_offset: Optional[int] = None,
                       zone_overrides: Optional[dict] = None,
                       risks_out: Optional[list] = None,
                       bank_name: Optional[str] = None) -> str:
    """Parses a folder of WAV files (via mpc2emu's own parse_sample_dir)
    into a single multisampled preset and writes it out as a real E4B/
    KRZ/EIII bank file in a fresh temp dir, applying whatever resample/
    reduce options were chosen along the way -- see build/convert.py's
    _apply_and_write() for everything after the parse step. Returns the
    new file's path; never touches dir_path itself.

    octave_offset: 2=C3, 1=C4, 0=C5 -- which octave "middle C" (MIDI 60)
    falls on for filenames without an explicit octave (or a bare MIDI
    number). None lets mpc2emu auto-detect it from a majority vote across
    the folder's own filenames (its own CLI default).

    zone_overrides: {sample_name: (lo_key, root_key, hi_key)} from the
    Sample Placement dialog's manual review, applied to the matching
    zone right after THIS SAME parse -- parse_sample_dir() is a pure
    function of dir_path/octave_offset, so the zone set it produces here
    is identical to whatever parse_preview() already showed the dialog.
    A zone whose sample_name isn't in the dict keeps its auto-computed
    placement; None (the default) applies no overrides at all.

    risks_out: collects convert.polyphony_risk() dicts for the written
    bank. Manual placement overrides feed straight into it -- widening
    several zones over the same keys is exactly how a folder that placed
    cleanly ends up stacking voices on one note."""
    bank = _run_captured(sampledir_parser.parse_sample_dir, dir_path,
                          octave_offset=octave_offset)
    if zone_overrides:
        for voice in bank.presets[0].voices:
            for zone in voice.zones:
                override = zone_overrides.get(zone.sample_name)
                if override is not None:
                    zone.lo_key, zone.root_key, zone.hi_key = override
    # bank_name: what the preset is called. The folder's name is right for a
    # folder import and meaningless for a staged selection, whose directory is
    # a temp path -- see stage_files().
    return _apply_and_write(bank, opts, bank_name or Path(dir_path).name, risks_out)
