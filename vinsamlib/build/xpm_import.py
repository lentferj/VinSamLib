"""
Imports an Akai MPC keygroup program (Instruments > Keygroup > Layer;
referencing external WAV/AIFF sample files -- MPC 2.x/X/Live/One and MPC 3;
see mpc2emu's parsers/xpm_parser.py) into a real, native E4B or KRZ bank
file, via mpc2emu's own parse_xpm -> Bank -> [resample/reduce] -> write_e4b /
write_krz pipeline. This is the only way an MPC program ever becomes usable
here: VinSamLib has no XPM reader of its own and never will (all
format-writing code in this project comes from mpc2emu; VinSamLib
deliberately never edits mpc2emu, only wraps it).

**Three file types, one reader.** The MPC saves the same keygroup program
inside three containers -- a bare program (`.xpm`), a track (`.xty`) and a
project (`.xpj`) -- and mpc2emu's parse_xpm dispatches on the payload, not
the extension (its parsers/registry.py maps all three to it). A project
carries one program per track, so it is the MPC's own equivalent of an E4B
bank; since mpc2emu `8e20612`, parse_xpm returns one Preset per keygroup
program in it, sharing one sample pool. Hence the split below: PROGRAM_EXTS
always yield exactly one preset and stay leaves in the Explorer, while a
PROJECT_EXT file is browsed like a bank (ui/models.py's "mpc_project" node),
one row per program.

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

import gzip
import json
import os
import re
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

# One keygroup program per file -- always exactly one preset, so these stay
# importable leaves in the Explorer.
PROGRAM_EXTS = (".xpm", ".xty")
# A project holds one program per track: browsed like a bank, not a leaf.
PROJECT_EXT = ".xpj"
# The format label each one carries through the Explorer, the index and the
# format filter. One definition, since the tree, the scanner and the search
# results all have to agree on it.
MPC_EXT_FORMAT = {".xpm": "XPM", ".xty": "XTY", PROJECT_EXT: "XPJ"}

# A `.xpm` is a program of *some* kind, and only two of them carry samples.
# Measured on a real 571-file MPC One backup:
#   KEYGROUP (82 files, 970 zones, 957 samples) -- pitched, multisampled.
#   DRUM     (90 files, 956 zones, 907 samples) -- one-shot hits, roughly as
#            much material as the keygroup programs and denser per file
#            (median 12 samples against 5). Convertible since mpc2emu
#            `27ff6a4`: each pad becomes a one-key zone whose root equals its
#            key, so it sounds at native pitch and does not keytrack.
#   EVERYTHING ELSE (399 files, 0 zones, 0 samples) -- MIDI, Plugin, Audio,
#            CV and Clip programs reference no sample data at all. mpc2emu
#            now refuses them outright, and the Explorer does not list them:
#            no more sample content than a MIDI file has.
KEYGROUP = "Keygroup"
DRUM = "Drum"
CONVERTIBLE_KINDS = (KEYGROUP, DRUM)

# MPC 2.x XML does not record the pad -> MIDI note map at all (every one of
# the 11520 <PadNote> elements in mpc2emu's corpus is empty; the neighbouring
# ProgramPads blob holds pad colours), so mpc2emu falls back to consecutive
# keys from 36 and warns. MPC 3 files carry a real map, and 31 of 56 corpus
# drum programs use a custom -- often General MIDI -- layout, so this is a
# real difference in outcome and not a formality: a 2.x kit whose author used
# such a layout lands on the wrong keys. Everything converts and nothing is
# lost; the pads are simply re-ordered. Surfaced rather than buried, since a
# whole-file check (is it XML?) is exactly what tells the two apart.
DRUM_2X_PAD_MAP_NOTE = (
    "MPC 2.x drum kit: the file does not store which key each pad plays, so "
    "its pads land on consecutive keys from 36 (C1). If this kit used a "
    "General MIDI or hand-built layout on the MPC, the hits will be in a "
    "different order here — all of them present, none at the wrong pitch.")
_XML_PROGRAM_TYPE = re.compile(rb'<Program\s+type="([^"]*)"')
_SNIFF_BYTES = 8192

# How far into an MPC 3 program object its name and type sit -- measured at
# ~110 bytes across this corpus, with room to spare. Only a shortcut: see
# project_program_names(), which decodes the whole object when they are not
# both in here.
_MPC3_PROGRAM_WINDOW = 1024
_JSON_NAME = re.compile(rb'"name"\s*:\s*("(?:[^"\\]|\\.)*")')
_JSON_TYPE = re.compile(rb'"type"\s*:\s*(-?\d+)')


def program_kind(path: str) -> Optional[str]:
    """What kind of program an MPC file holds ("Keygroup", "Drum", "MIDI",
    "Plugin", "Audio", "CV", "Clip"), or None when it cannot be told from a
    header peek -- which is never a parse, since a real parse loads every
    referenced WAV and listing a directory must not do that.

    None is the honest answer for an MPC 3 program (gzipped JSON, whose type
    only mpc2emu's own reader can name) and for anything unusual, and callers
    treat it as "show it": no listing heuristic should hide a file it does not
    understand.

    Verified against a 571-file backup: every file declares its type within
    the first few KB, and it agrees with the MPC's own `<name>.<Kind>.xpm`
    filename convention in all 571 cases. The tag is read rather than the
    name because a renamed file is still perfectly readable."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_SNIFF_BYTES)
    except OSError:
        return None
    if head.lstrip()[:1] != b"<":
        return None     # MPC 3 (gzip+JSON) or not XML at all -- can't tell
    m = _XML_PROGRAM_TYPE.search(head)
    return m.group(1).decode("ascii", "replace") if m else None


def holds_convertible_program(path: str) -> bool:
    """Whether a program file has anything to import -- the test the Explorer
    and the index both list by. Unknown kinds count as convertible: see
    program_kind() for why nothing is hidden on a guess."""
    kind = program_kind(path)
    return kind is None or kind in CONVERTIBLE_KINDS


def project_program_names(path: str) -> list[str]:
    """The names of the programs an MPC 3 project holds, read without parsing
    a single sample -- for the index, which must stay cheap enough to run
    over a whole library in the background.

    MPC 3 keeps its programs inside the .xpj, so nothing else in a library
    can make them findable. An MPC 2.x project keeps each one as its own
    .xpm in the data folder, and those files are indexed in their own right,
    so this returns nothing for them rather than indexing the same program
    twice under two names.

    Only the program objects are decoded, not the document around them. A
    real project here is 22 MB of JSON, of which the programs are a few
    hundred KB -- json.loads on the whole thing costs 16s across this
    library against 4s for these, and a background scan cannot spend that.
    Each `"program"` key is decoded where it sits, which is ordinary JSON
    parsing (no pattern-matching of the text), and the type filter mirrors
    mpc2emu's `_mpc3_program_nodes`: 0 is a drum program, 1 a keygroup, and
    the rest carry no samples. That mirroring is the one thing here that can
    drift, so it is checked against that function over the whole corpus in
    tests/manual_ui_smoke_xpj_project.py.

    The names are what a parse would offer, minus any program it later drops
    for holding no samples -- not knowable without reading them. Anything
    unexpected yields no names at all, leaving the project findable by
    filename as before."""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\x1f\x8b":
                return []                    # MPC 2.x: separate files
        # Bytes, not text: the search needs no character semantics, and
        # decoding a whole library's worth of these (1.8 GB) costs a second
        # by itself. Only a name that is actually kept gets decoded.
        with gzip.open(path, "rb") as g:
            if g.readline().rstrip(b"\n") != b"ACVS":
                return []
            for _ in range(4):               # the rest of the five-line header
                g.readline()
            text = g.read()
    except Exception:
        return []                            # unreadable: still findable by name
    decoder = json.JSONDecoder()
    names, pos = [], 0
    while True:
        key = text.find(b'"program"', pos)
        if key < 0:
            break
        start = text.find(b"{", key)
        if start < 0:
            break
        pos = start + 1
        # A program writes its name and type ahead of `programPads`, which is
        # the bulk of it, so both are inside the first few hundred bytes --
        # reading only those skips megabytes of pad data per project. The
        # values are still decoded as JSON, not lifted as raw text. Anything
        # not in the window falls through to decoding the whole object, so
        # the window is a shortcut and never the thing correctness rests on.
        window = text[start:start + _MPC3_PROGRAM_WINDOW]
        name_match = _JSON_NAME.search(window)
        type_match = _JSON_TYPE.search(window)
        if name_match is not None and type_match is not None:
            program_type = int(type_match.group(1))
            name = json.loads(name_match.group(1).decode("utf-8", "replace")).strip()
        else:
            try:
                program, _ = decoder.raw_decode(text[start:].decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(program, dict):
                continue
            program_type = program.get("type")
            name = str(program.get("name", "")).strip()
        if program_type in (0, 1) and name:
            names.append(name)
    return names


_XML_SAMPLE_NAME = re.compile(rb"<SampleName>([^<]*)</SampleName>")


def source_sample_names(path: str) -> list[str]:
    """Every sample name an MPC file references, at full length.

    A parse cannot answer this: mpc2emu shortens each name to the 16
    characters an E4B/KRZ name field holds and keeps nothing else, so the
    only place the whole name still exists is the file. Read straight out of
    it -- `<SampleName>` in MPC 2.x XML, the `samples` array in MPC 3 -- with
    no parse and no samples loaded.

    For a 2.x project the programs are separate files in the data folder, so
    all of them contribute; names are matched up afterwards, never by
    position, and an unreadable file simply contributes nothing."""
    p = Path(path)
    try:
        with open(p, "rb") as f:
            gzipped = f.read(2) == b"\x1f\x8b"
    except OSError:
        return []
    if gzipped:
        return _mpc3_sample_names(p)
    if p.suffix.lower() == PROJECT_EXT:
        data_dir = p.parent / f"{p.stem}_[ProjectData]"
        if not data_dir.is_dir():
            return []
        names: list[str] = []
        for program in sorted(data_dir.glob("*.xpm")):
            names += _xml_sample_names(program)
        return names
    return _xml_sample_names(p)


def _xml_sample_names(path: Path) -> list[str]:
    try:
        blob = path.read_bytes()
    except OSError:
        return []
    return [name for name in
            (m.group(1).decode("utf-8", "replace").strip()
             for m in _XML_SAMPLE_NAME.finditer(blob))
            if name]


def _mpc3_sample_names(path: Path) -> list[str]:
    """The `samples` arrays of an MPC 3 payload, decoded where they sit --
    same reason as project_program_names(): the document around them runs to
    tens of MB and none of it is needed."""
    try:
        with gzip.open(path, "rb") as g:
            if g.readline().rstrip(b"\n") != b"ACVS":
                return []
            for _ in range(4):
                g.readline()
            text = g.read()
    except Exception:
        return []
    decoder = json.JSONDecoder()
    names, pos = [], 0
    while True:
        key = text.find(b'"samples"', pos)
        if key < 0:
            break
        start = text.find(b"[", key)
        if start < 0:
            break
        pos = start + 1
        try:
            samples, _ = decoder.raw_decode(text[start:].decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(samples, list):
            continue
        for sample in samples:
            if isinstance(sample, dict) and str(sample.get("name", "")).strip():
                names.append(str(sample["name"]).strip())
    return names


def full_sample_names(stored: list[str], path: str) -> dict[str, str]:
    """Map each shortened sample name back to the whole name it came from.

    Only pairings that can be proved are returned: a candidate qualifies when
    mpc2emu's own _safe_name() turns it into exactly that stored name, and a
    stored name claimed by two different candidates is dropped rather than
    guessed at -- the same rule the project rows use for program names. What
    is missing simply shows as the shortened name, which is never wrong."""
    safe_name = getattr(xpm_parser, "_safe_name", None)
    if safe_name is None:
        return {}
    wanted = set(stored)
    if not wanted:
        return {}
    found: dict[str, str] = {}
    ambiguous: set[str] = set()
    for candidate in source_sample_names(path):
        # _safe_name() drops the extension before it shortens, and an MPC 2.x
        # <SampleName> often carries one ("…_C-1.wav"). Dropping it here too
        # keeps the two halves aligned -- otherwise the kept part comes out
        # as "…_2600_E-1.wav" while the name that reaches the bank is
        # "IsOnPCP_2600_E-1", and the split is drawn in the wrong place.
        candidate = os.path.splitext(candidate)[0]
        short = safe_name(candidate, tail=True)
        if short not in wanted or short in ambiguous:
            continue
        if short in found and found[short] != candidate:
            del found[short]
            ambiguous.add(short)
            continue
        found[short] = candidate
    return {short: full for short, full in found.items() if full != short}


@dataclass(frozen=True)
class XpmSummary:
    preset_name: str
    sample_count: int
    total_sample_bytes: int
    zones: list = field(default_factory=list)   # list[ZoneSummary]


@dataclass(frozen=True)
class ProjectSummary:
    """The container itself, not any one of its programs -- deliberately the
    same set of facts banks/summary.py's BankSummary carries for a real E4B
    or KRZ bank, since that is what the Detail pane shows for one."""
    name: str
    program_names: list[str] = field(default_factory=list)
    sample_count: int = 0
    total_sample_bytes: int = 0


def parse_mpc(path: str, wav_dir: Optional[str] = None):
    """Parse any MPC container mpc2emu accepts (program, track or project)
    into its Bank -- the one parse every read-only caller here shares.

    Not cheap: it loads every referenced WAV, so a project can pull tens of
    MB. Callers that browse (ui/models.py) parse once and keep the Bank on
    the tree node; callers that write re-parse, so a mutation never reaches
    a cached one."""
    if wav_dir is None:
        wav_dir = str(Path(path).resolve().parent)
    return _run_captured(xpm_parser.parse_xpm, path, wav_dir)


def _preset_samples(bank, preset) -> list:
    """The samples one preset actually references, in first-use order.

    A project's presets SHARE bank.samples (mpc2emu loads each WAV once,
    however many programs name it), so len(bank.samples) is the whole
    project's pool -- reporting or writing that per program would multiply
    the same megabytes by the number of programs."""
    out, seen = [], set()
    for voice in preset.voices:
        for z in voice.zones:
            if z.sample_name in seen:
                continue
            seen.add(z.sample_name)
            sample = bank.find_sample(z.sample_name)
            if sample is not None:
                out.append(sample)
    return out


def summarize_program(bank, preset_index: int = 0) -> XpmSummary:
    """One program's zones out of an already-parsed Bank -- same
    ZoneSummary shape banks/summary.py uses for E4B/KRZ presets, so the
    Detail pane shows the same sample/key/vel/root/loop table either way.

    Split from summarize_xpm() so browsing a project's programs re-uses the
    Bank the tree already parsed instead of re-reading every WAV per click."""
    preset = bank.presets[preset_index]
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
    samples = _preset_samples(bank, preset)
    return XpmSummary(
        preset_name=preset.name.strip(),
        sample_count=len(samples),
        total_sample_bytes=sum(len(s.data) for s in samples),
        zones=zones,
    )


def summarize_project(bank) -> ProjectSummary:
    """The whole container, for the Detail pane's project row. Sample count
    and size are the shared pool's, counted once -- not the sum over
    programs, which would double-count a WAV two tracks both use."""
    return ProjectSummary(
        name=bank.name.strip(),
        program_names=[p.name.strip() for p in bank.presets],
        sample_count=len(bank.samples),
        total_sample_bytes=sum(len(s.data) for s in bank.samples),
    )


def summarize_xpm(xpm_path: str, wav_dir: Optional[str] = None,
                  preset_index: int = 0) -> XpmSummary:
    """Read-only preview for Explorer's Detail pane -- parses via mpc2emu's
    own xpm_parser (same as import_xpm(), just never writes anything) to
    report one preset's zones and referenced sample data size, without doing
    a full import. Same wav_dir default as import_xpm()."""
    return summarize_program(parse_mpc(xpm_path, wav_dir), preset_index)


def load_samples_for_test(xpm_path: str, wav_dir: Optional[str] = None,
                          preset_index: Optional[int] = None) -> list:
    """Read-only: parses just far enough to list samples, for the Convert
    Options dialog's stereo Test button -- same parse summarize_xpm()
    already does for the Detail pane preview, never writes anything.
    Same wav_dir/preset_index meaning as import_xpm()."""
    bank = parse_mpc(xpm_path, wav_dir)
    if preset_index is None:
        return bank.samples
    return _preset_samples(bank, bank.presets[preset_index])


def import_xpm(xpm_path: str, opts: ConversionOptions, wav_dir: Optional[str] = None,
               risks_out: Optional[list] = None,
               preset_index: Optional[int] = None) -> str:
    """Parses an MPC program, track or project (via mpc2emu's own
    parse_xpm) and writes it out as a real E4B or KRZ bank file in a fresh
    temp dir, applying whatever resample/reduce options were chosen along
    the way -- see build/convert.py's _apply_and_write() for everything
    after the parse step. Returns the new file's path; never touches
    xpm_path itself.

    wav_dir: directory to search for the referenced WAV/AIFF sample files
    if they aren't sitting right next to the source (defaults to its own
    directory, mpc2emu's own convention, when None).

    preset_index: import ONE program of a project rather than all of them
    (Explorer's per-program row). None writes every preset the file
    yielded, which for a project is the whole thing as one bank -- and for
    a program or track is the single preset it always holds anyway.

    risks_out: collects convert.polyphony_risk() dicts for the written bank.
    An MPC program is the likeliest source to trip it -- a keygroup program
    stacks up to four layers per pad by design, and every stereo one of them
    costs two E4B voices."""
    bank = parse_mpc(xpm_path, wav_dir)
    if preset_index is not None:
        # Narrow the freshly-parsed Bank in place -- safe because parse_mpc()
        # just built it for this call alone (the Explorer's cached Bank is
        # never handed to a writer). Dropping the other programs' samples
        # matters: they are one shared pool, so without this a single
        # program would carry the whole project's audio into the bank.
        preset = bank.presets[preset_index]
        preset.program_number = 0
        bank.presets = [preset]
        bank.samples = _preset_samples(bank, preset)
    return _apply_and_write(bank, opts, Path(xpm_path).stem, risks_out)
