"""
Imports an Akai MPC XPM program (Instruments > Keygroup > Layer, XML,
referencing external WAV/AIFF sample files -- MPC 2.x/X/Live/One; see
mpc2emu's parsers/xpm_parser.py) into a real, native E4B or KRZ bank file,
via mpc2emu's own parse_xpm -> Bank -> [resample/reduce] -> write_e4b /
write_krz pipeline. This is the only way an XPM ever becomes usable here:
VinSamLib has no XPM reader of its own and never will (all format-writing
code in this project comes from mpc2emu; VinSamLib deliberately never
edits mpc2emu, only wraps it).

Once written, the resulting file is a completely ordinary E4B/KRZ bank --
it gets no special treatment anywhere else in VinSamLib (Explorer, New
Bank, banks.e4b/krz.assemble()) beyond being reachable like any other bank
once its destination folder is a library root.

Shares its ConversionOptions shape and its whole reduce/resample/write
tail with build/convert.py (see that module's _apply_and_write()) --
import_xpm() and apply_conversion()/convert_preset() differ only in how
the starting mpc2emu Bank gets parsed (foreign XPM vs. an already-native
E4B), not in what happens to it afterward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .convert import ConversionOptions, _apply_and_write, _run_captured
from ..banks.summary import ZoneSummary
from ..mpc2emu_bridge import xpm_parser

# mpc2emu's models.common.LoopType values (0-3) match banks/summary.py's own
# _LOOP_NAMES keys exactly (both ultimately describe the same E4B/KRZ loop-
# flag convention) -- kept as its own tiny copy here rather than reaching
# into that module's private name, since build/ and banks/ stay separate
# layers otherwise.
_LOOP_NAMES = {0: "none", 1: "forward", 2: "alternating", 3: "forward (release)"}


@dataclass(frozen=True)
class XpmSummary:
    preset_name: str
    sample_count: int
    total_sample_bytes: int
    zones: list = field(default_factory=list)   # list[ZoneSummary]


def summarize_xpm(xpm_path: str, wav_dir: Optional[str] = None) -> XpmSummary:
    """Read-only preview for Explorer's Detail pane -- parses via mpc2emu's
    own xpm_parser (same as import_xpm(), just never writes anything) to
    report the one preset's zones (same ZoneSummary shape banks/summary.py
    uses for E4B/KRZ presets, so the Detail pane can show the same
    sample/key/vel/root/loop table either way) and total referenced sample
    data size, without doing a full import. Same wav_dir default as
    import_xpm()."""
    if wav_dir is None:
        wav_dir = str(Path(xpm_path).resolve().parent)
    bank = _run_captured(xpm_parser.parse_xpm, xpm_path, wav_dir)
    preset = bank.presets[0]
    zones: list[ZoneSummary] = []
    for voice in preset.voices:
        for z in voice.zones:
            sample = bank.find_sample(z.sample_name)
            zones.append(ZoneSummary(
                sample_name=z.sample_name,
                lo_key=z.lo_key, hi_key=z.hi_key,
                lo_vel=z.lo_vel, hi_vel=z.hi_vel,
                root_key=z.root_key,
                loop=_LOOP_NAMES.get(int(sample.loop_type), "?") if sample else "?",
                sample_rate=sample.sample_rate if sample else None,
            ))
    total_bytes = sum(len(sample.data) for sample in bank.samples)
    return XpmSummary(
        preset_name=preset.name.strip(),
        sample_count=len(bank.samples),
        total_sample_bytes=total_bytes,
        zones=zones,
    )


def load_samples_for_test(xpm_path: str, wav_dir: Optional[str] = None) -> list:
    """Read-only: parses just far enough to list samples, for the Convert
    Options dialog's stereo Test button -- same parse summarize_xpm()
    already does for the Detail pane preview, never writes anything.
    Same wav_dir default as import_xpm()."""
    if wav_dir is None:
        wav_dir = str(Path(xpm_path).resolve().parent)
    bank = _run_captured(xpm_parser.parse_xpm, xpm_path, wav_dir)
    return bank.samples


def import_xpm(xpm_path: str, opts: ConversionOptions, wav_dir: Optional[str] = None,
               risks_out: Optional[list] = None) -> str:
    """Parses an XPM program (via mpc2emu's own parse_xpm) and writes it
    out as a real E4B or KRZ bank file in a fresh temp dir, applying
    whatever resample/reduce options were chosen along the way -- see
    build/convert.py's _apply_and_write() for everything after the parse
    step. Returns the new file's path; never touches xpm_path itself.

    wav_dir: directory to search for the XPM's referenced WAV/AIFF sample
    files if they aren't sitting right next to the .xpm (defaults to the
    XPM's own directory, mpc2emu's own convention, when None).

    risks_out: collects convert.polyphony_risk() dicts for the written bank.
    An XPM is the likeliest source to trip it -- an MPC keygroup program
    stacks up to four layers per pad by design, and every stereo one of them
    costs two E4B voices."""
    if wav_dir is None:
        wav_dir = str(Path(xpm_path).resolve().parent)
    bank = _run_captured(xpm_parser.parse_xpm, xpm_path, wav_dir)
    return _apply_and_write(bank, opts, Path(xpm_path).stem, risks_out)
