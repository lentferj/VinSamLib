"""Importing a soft-sampler instrument into a real, native hardware bank.

Five formats, none of them from a hardware sampler: SoundFont 2 (``.sf2``),
SFZ, Logic EXS24 (``.exs``), TAL-Sampler (``.talsmpl``) and GigaSampler
(``.gig``). They are **import sources only** — VinSamLib browses them and
converts out of them, and will never write one. Output stays what the
hardware understands: E4B, KRZ, EIII.

That asymmetry is the whole design. Everywhere else in this project a format
that can be read can also be assembled, and several layers quietly assume it
(``ui/bank_pane.py``'s ``_ASSEMBLE_FNS`` would raise ``KeyError`` on a format
it has no writer for). Nothing here ever reaches those layers: an import runs
mpc2emu's parser, hands the resulting Bank to ``convert._apply_and_write()``,
and what comes back is an ordinary E4B/KRZ/EIII file that
``MainWindow._read_back_converted_presets()`` re-parses with VinSamLib's own
readers. **New Bank only ever holds native presets** — the same rule the MPC
import already follows, and the reason a format label like "SF2" can never
leak into a writer.

This module owns the format table *and* the import, exactly as
``build/xpm_import.py`` owns ``MPC_EXT_FORMAT`` *and* ``import_xpm``. There
are already nine places in this project that separately decide what an
extension means (``ui/models.py``, three ``vfs`` volumes, ``ui/image_pane.py``
…) and they have drifted from each other; the ``.xpj`` work avoided adding a
tenth by having one module own its table and everyone import it. Same here.

Two things here have no counterpart in the MPC import:

* **Listing without parsing.** ``vinsamlib/foreign_names.py`` reads names out
  of headers, so expanding a 1 GB SoundFont costs milliseconds. See
  ``resolve_ordinal()`` for the correctness problem that creates.
* **An availability gate.** These formats exist in the UI only when mpc2emu
  is actually on disk (``available()``), because without it there is no way
  to import one — a browsable row that can only ever fail is worse than no
  row at all.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from . import convert as convert_mod
from .convert import ConversionOptions, _apply_and_write, _run_captured
from .sample_names import apply_sample_names, names_from_base
from .xpm_import import XpmSummary, _preset_samples, summarize_program
from .. import foreign_names
from ..config import Config
from ..mpc2emu_bridge import parser_registry, xpm_parser

# The format label each extension carries through the Explorer, the index and
# the format filter -- one definition, since the tree, the scanner and the
# search results all have to agree on it.
FOREIGN_EXT_FORMAT = {
    ".sf2": "SF2",
    ".sfz": "SFZ",
    ".exs": "EXS24",
    ".talsmpl": "TAL",
    ".gig": "GIG",
}
# Files holding many instruments: browsed like a bank, one row per preset.
CONTAINER_EXTS = (".sf2", ".gig")
# Files holding one instrument: a single importable row, like a `.xpm`.
#
# SFZ is here despite mpc2emu splitting a keyswitched file into one preset per
# articulation (63 of the 1821 in this author's library do that, up to 16 of
# them). Importing such a row simply yields several presets at once, which the
# read-back already handles -- whereas expanding it into rows would mean
# reproducing mpc2emu's articulation naming in a header reader, i.e. exactly
# the duplication that ui/models.py's _project_program_labels() warns about.
LEAF_EXTS = (".sfz", ".exs", ".talsmpl")

FOREIGN_FORMATS = frozenset(FOREIGN_EXT_FORMAT.values())

# Unlike the MPC's three containers -- which are three wrappers around one
# keygroup program, and so earn a single "MPC" chip in the format dropdown --
# these are five unrelated ecosystems that happen to share a job. A user
# looking for a SoundFont is not looking for an EXS24 instrument.
FORMAT_FILTERS = ("SF2", "SFZ", "EXS24", "TAL", "GIG")

# macOS writes a metadata fork beside every real file on a non-HFS volume.
# There are 22 of them among this author's .exs files alone, each carrying the
# same name as a real preset -- listing them would double every row in those
# packs. They are not broken instruments, they are a different kind of file.
_APPLEDOUBLE_PREFIX = "._"

# GIG's parser caps itself far lower than the others (32 instruments, 512
# samples). Passed explicitly so what the Explorer lists is what an import
# produces; the corpus's largest .gig holds 7 instruments, so the ceiling is
# only insurance.
_MAX_SAMPLES = 100_000


# ── availability ───────────────────────────────────────────────────────────

_available: Optional[bool] = None


def set_available(ok: bool) -> None:
    """Record whether mpc2emu can supply these parsers, for this process.

    Seeded by MainWindow before the tree model is built. The value is cached
    for the process rather than re-checked, which matches how the rest of the
    app treats the mpc2emu path (Settings already says a change needs a
    restart) and keeps a per-row filesystem stat off the listing path.
    """
    global _available
    _available = bool(ok)


def available() -> bool:
    """Whether these formats should exist in the UI at all.

    Falls back to reading the config when nothing seeded it, so that
    ``index.scanner`` and stand-alone scripts -- neither of which has a
    MainWindow -- still get the right answer.
    """
    global _available
    if _available is None:
        try:
            _available = Config.load().check_foreign_import_support()[0]
        except Exception:
            _available = False
    return _available


# ── what a file is, and whether to show it ─────────────────────────────────

@dataclass(frozen=True)
class FileVerdict:
    """Everything the Explorer and the indexer need about one file, decided
    from its header alone.

    ``empty_reason`` non-empty means the row is shown greyed and refuses to
    import -- the file was read fine and holds nothing usable. ``note`` means
    it imports, with a caveat worth saying out loud. The distinction is the
    one ui/models.py already draws between ``empty_reason`` and ``error``:
    never claim a failure that did not happen.
    """
    format: str
    label: str
    container: bool = False
    empty_reason: str = ""
    note: str = ""

    @property
    def importable(self) -> bool:
        return not self.empty_reason


def format_for(path) -> Optional[str]:
    """The format label for a path's extension, or None if it is not ours."""
    return FOREIGN_EXT_FORMAT.get(Path(path).suffix.lower())


def inspect(path) -> Optional[FileVerdict]:
    """What row, if any, *path* should produce.

    None means "not one of ours, do not list" -- an unknown extension, a
    macOS metadata fork, or a file whose magic says it is something else. A
    file that IS one of ours but cannot be used comes back as a verdict with
    an ``empty_reason``, never as None: a broken instrument the user can see
    and ask about beats one that silently is not there.
    """
    p = Path(path)
    fmt = format_for(p)
    if fmt is None or not available():
        return None
    if p.name.startswith(_APPLEDOUBLE_PREFIX):
        return None
    ext = p.suffix.lower()

    if ext == ".exs":
        name = foreign_names.exs_instrument_name(p)
        if name is None:
            return FileVerdict(fmt, p.stem, empty_reason=(
                f"{p.name} does not read as an EXS24 instrument — its header "
                f"carries no EXS magic."))
        return FileVerdict(fmt, name)

    if ext == ".sfz":
        if not foreign_names.sfz_is_listable(p):
            return FileVerdict(fmt, p.stem, empty_reason=(
                f"{p.name} defines no region and includes no file, so there "
                f"is nothing to import — it is settings for regions that are "
                f"somewhere else."))
        return FileVerdict(fmt, p.stem)

    if ext == ".talsmpl":
        verdict = foreign_names.talsmpl_state(p)
        if verdict is None:
            return FileVerdict(fmt, p.stem, empty_reason=(
                f"{p.name} does not read as a TAL-Sampler preset."))
        if verdict.state is foreign_names.TalState.ENCRYPTED:
            return FileVerdict(fmt, verdict.program_name, empty_reason=(
                f"every sample this preset uses is an encrypted .talwav, "
                f"which only TAL-Sampler itself can decode — the preset is "
                f"readable, its audio is not."))
        if verdict.state is foreign_names.TalState.ROM_ONLY:
            return FileVerdict(fmt, verdict.program_name, empty_reason=(
                f"this preset plays TAL-Sampler's own built-in waveforms, "
                f"not sampled audio — there is no sample here to convert."))
        if verdict.state is foreign_names.TalState.NO_SAMPLES:
            return FileVerdict(fmt, verdict.program_name, empty_reason=(
                f"{p.name} references no sample at all."))
        note = ""
        if verdict.state is foreign_names.TalState.PARTIAL:
            note = (f"{verdict.encrypted} of {verdict.total} samples are "
                    f"encrypted .talwav and will be left out; the rest import "
                    f"normally.")
        return FileVerdict(fmt, verdict.program_name, note=note)

    # Containers: SF2 and GIG.
    listed = list_presets(p)
    if listed is None:
        return FileVerdict(fmt, p.stem, container=True, empty_reason=(
            f"{p.name} does not read as a {fmt} file — it is empty, "
            f"truncated, or something else with this extension."))
    if not listed:
        return FileVerdict(fmt, p.stem, container=True, empty_reason=(
            f"{p.name} holds no preset."))
    return FileVerdict(fmt, p.stem, container=True)


def list_presets(path) -> Optional[list]:
    """The rows a container file expands into, read from its header only.

    Returns ``list[foreign_names.ListedPreset]``, ``[]`` for a readable file
    holding nothing, or None when it does not read as its format at all.
    """
    ext = Path(path).suffix.lower()
    if ext == ".sf2":
        return foreign_names.sf2_preset_names(path)
    if ext == ".gig":
        return foreign_names.gig_instrument_names(path)
    return None


def is_container(path) -> bool:
    return Path(path).suffix.lower() in CONTAINER_EXTS


# ── parsing, via mpc2emu ───────────────────────────────────────────────────

def _stage_tal_samples(path: Path):
    """Gather a TAL preset's samples into one directory, under the names
    mpc2emu will look for. Returns a TemporaryDirectory, or None if nothing
    needs it.

    **Why this exists.** TAL stores sample paths the way Windows wrote them:
    ``url="..\\Crystal Bellz\\Crystal Bellz (0).wav"``. ``talsmpl_parser``
    tries four candidates, and on Linux three of them are dead —
    ``preset_dir / url`` is one filename containing literal backslashes, and
    the two that use the basename only look in one directory, while a real
    pack spreads its samples over several sibling folders. The parser then
    skips every zone and appends its preset anyway, so the import
    *succeeds* and writes a bank with nothing in it.

    This is not a rare shape: **547 of the 1712 presets** in this author's
    library reference their samples this way, against 320 that resolve as
    they are. Without this, the majority of a TAL library imports as silence.

    The fix stays on our side of the line — VinSamLib never edits mpc2emu.
    Resolving the path is something we can do perfectly (normalise the
    separators, join to the preset's folder), and one of the parser's own
    candidates is ``search_dir / basename``, so linking each resolved file
    into a directory handed over as ``wav_dir`` makes all four work.

    Same link-else-copy ladder as ``sampledir_import.stage_files``, and for
    the same reason: a multisample is tens of megabytes and is read once.

    Samples that already sit beside the preset are left alone — the parser
    finds those first, and staging them would only risk shadowing. Where two
    referenced files share a basename the first wins, which is a limit of
    mpc2emu's basename lookup, not of this staging.
    """
    urls = foreign_names.talsmpl_sample_urls(path)
    if not urls:
        return None
    resolved: dict[str, Path] = {}
    for url in urls:
        posix = url.replace("\\", "/")
        base = posix.rsplit("/", 1)[-1]
        if base in resolved or (path.parent / base).exists():
            continue
        candidate = Path(os.path.normpath(path.parent / PurePosixPath(posix)))
        try:
            if candidate.is_file():
                resolved[base] = candidate
        except OSError:
            continue
    if not resolved:
        return None
    staged = tempfile.TemporaryDirectory(prefix="vinsamlib_tal_")
    root = Path(staged.name)
    for base, source in resolved.items():
        target = root / base
        try:
            os.symlink(source, target)
        except (OSError, NotImplementedError, AttributeError):
            try:
                os.link(source, target)
            except OSError:
                try:
                    shutil.copy2(source, target)
                except OSError:
                    pass
    return staged


def parse_foreign(path, wav_dir: Optional[str] = None,
                  max_presets: Optional[int] = None):
    """Parse any of the five formats into an mpc2emu Bank.

    Goes through mpc2emu's own ``parsers/registry.py`` rather than five
    parser imports, because that table already normalises their signatures
    (``parse_sf2`` takes ``max_presets`` and no ``wav_dir``, ``parse_exs24``
    takes a *list* of sample directories, ``parse_gig`` takes
    ``max_instruments``/``max_samples``) and calls itself the one source of
    truth for it.

    **Not cheap.** This loads every sample: one 1 GB SoundFont here costs
    2.7 s and about 3 GB of RSS. Browsing must not call it -- that is what
    ``foreign_names`` is for -- and callers that do should be on a worker.

    ``max_presets`` defaults in mpc2emu to 64 (SF2) and 32 (GIG), which would
    silently truncate: 11 SoundFonts in this author's library hold more than
    64 presets, the largest 444. Pass the listed count so that what the
    Explorer shows is what actually imports.

    No sample-search directory is invented. mpc2emu's own resolvers are
    better than anything worth writing here -- ``exs24_parser`` walks eight
    ancestor levels building a case-insensitive name-and-stem index over
    audio-named folders (which is what resolves Logic's ``Sampler
    Instruments/`` + ``Samples/`` layout), and ``sfz_parser`` has its own.
    """
    p = Path(path)
    ext = p.suffix.lower()
    parser = parser_registry.PARSERS.get(ext)
    if parser is None:
        raise ValueError(f"{p.name}: no parser for {ext} files.")
    kw = {"max_samples": _MAX_SAMPLES}
    if max_presets:
        kw["max_presets"] = max_presets
    if ext == ".talsmpl" and wav_dir is None:
        staged = _stage_tal_samples(p)
        if staged is not None:
            # The staging directory must outlive the parse, not the call:
            # mpc2emu reads the audio into the Bank while it parses, so once
            # this returns nothing points at those files any more.
            with staged:
                return _run_captured(parser, str(p), staged.name, **kw)
    return _run_captured(parser, str(p), wav_dir, **kw)


def _listed_count(path) -> Optional[int]:
    listed = list_presets(path) if is_container(path) else None
    return len(listed) if listed else None


def resolve_ordinal(bank, listed: list, ordinal: int) -> int:
    """Map a row's position in the FILE to its position in the PARSED bank.

    These are not the same number, and assuming they are is how a user asks
    for one preset and gets its neighbour. ``sf2_parser`` and ``gig_parser``
    both drop an entry that yielded no zones, so a file listing 8 presets can
    parse to 5.

    **RARE, and an earlier number here was wrong.** This docstring used to
    claim "27 dropped entries across a 25-file SF2 sample". Re-measured
    2026-09-05 over the whole local library -- 446 of 474 SoundFonts parsed
    (the rest too large to parse in one pass, or unreadable) -- the real
    figure is **2 files and 4 entries**, which mpc2emu's independent scan of
    504 files agrees with exactly.

    The old number conflated this with TRUNCATION, which is a different
    mechanism and two orders of magnitude more common: mpc2emu's
    ``max_presets`` defaults to 64 for SF2 and 32 for GIG, and with that
    default in play the same 446 files lose **900 entries across 10 files**.
    One of them loses exactly 27, which is very likely where the old figure
    came from. Truncation is why ``parse_foreign`` passes the listed count at
    every call site; it is not why this function exists.

    Do not re-derive either number by sampling: drops are concentrated in a
    couple of files, so a small random sample reports either zero or a wildly
    inflated rate. Measure the whole set and print the denominator.

    Two cases, and nothing in between:

    * **Same count** -- nothing was dropped, so the mapping is identity. This
      is the overwhelmingly common case and it needs no name matching at all,
      which matters because names are not unique (one corpus SoundFont has
      ``MOOG SQ`` at nine different positions).
    * **Fewer parsed than listed** -- align the parsed names as a subsequence
      of the listed ones, comparing through mpc2emu's own ``_safe_name()`` so
      the two cannot drift apart. If the wanted row was one of the dropped
      ones, say so; if the alignment does not come out uniquely, **refuse**
      rather than guess -- the same discipline ``_project_program_labels()``
      applies to MPC project rows.
    """
    parsed = bank.presets
    if not 0 <= ordinal < len(listed):
        raise ValueError(
            f"preset {ordinal + 1} is not in this file any more — it holds "
            f"{len(listed)}. Collapse and re-expand it to re-read.")
    if len(parsed) == len(listed):
        return ordinal
    if len(parsed) > len(listed):
        raise ValueError(
            f"this file parsed to {len(parsed)} presets but its header lists "
            f"{len(listed)}; import the whole file rather than one preset of "
            f"it.")

    safe_name = getattr(xpm_parser, "_safe_name", None)
    def norm(text: str) -> str:
        return safe_name(text) if safe_name else text.strip()

    want = [norm(entry.name) for entry in listed]
    mapping: dict[int, int] = {}
    i = 0
    for parsed_index, preset in enumerate(parsed):
        name = preset.name
        while i < len(want) and want[i] != name:
            i += 1
        if i == len(want):
            raise ValueError(
                f"this file's presets could not be matched to its header "
                f"({len(listed)} listed, {len(parsed)} read); import the "
                f"whole file rather than one preset of it.")
        mapping[i] = parsed_index
        i += 1
    if ordinal not in mapping:
        raise ValueError(
            f"'{listed[ordinal].display}' holds no sampled content — it has "
            f"no zone referencing a sample, so there is nothing to import.")
    return mapping[ordinal]


# ── read-only previews ─────────────────────────────────────────────────────

def summarize_foreign(path, ordinal: Optional[int] = None,
                      wav_dir: Optional[str] = None) -> XpmSummary:
    """One instrument's zones, for the Detail pane.

    Reuses ``xpm_import.summarize_program`` rather than growing a second
    summariser: it already returns ``ZoneSummary`` rows in the shape
    ``banks/summary.py`` produces for a real E4B preset, which is what makes
    the Detail and Samples panes render a SoundFont exactly like a bank.

    Parses. Callers must be on a worker thread, and should think twice for a
    large container -- see ``parse_foreign``.
    """
    bank = parse_foreign(path, wav_dir, max_presets=_listed_count(path))
    index = 0
    if ordinal is not None and is_container(path):
        listed = list_presets(path) or []
        index = resolve_ordinal(bank, listed, ordinal)
    return summarize_program(bank, index)


def load_samples_for_test(path, ordinal: Optional[int] = None,
                          wav_dir: Optional[str] = None) -> list:
    """Read-only sample list for the Convert Options dialog's stereo Test
    button. Same parse the Detail pane preview does; never writes."""
    bank = parse_foreign(path, wav_dir, max_presets=_listed_count(path))
    if ordinal is None or not is_container(path):
        return bank.samples
    listed = list_presets(path) or []
    return _preset_samples(bank, bank.presets[resolve_ordinal(bank, listed, ordinal)])


# ── the import ─────────────────────────────────────────────────────────────

def import_foreign(path, opts: ConversionOptions,
                   ordinal: Optional[int] = None,
                   wav_dir: Optional[str] = None,
                   risks_out: Optional[list] = None,
                   name_base: str = "", name_octave: int = 2,
                   name_with_key: bool = True,
                   name_overrides: Optional[dict] = None) -> str:
    """Convert a soft-sampler instrument into a real E4B/KRZ/EIII bank file
    in a fresh temp dir, and return its path. Never touches *path*.

    ``ordinal`` imports ONE preset of a container (the Explorer's per-preset
    row); None writes everything the file yielded.

    Everything after the parse is ``build/convert.py``'s ``_apply_and_write``
    -- the same trim/pan/mono/reduce/resample/write/verify tail every other
    import in this app converges on. Nothing about these formats gets its own
    write path, which is precisely why they cannot become an output format by
    accident.
    """
    p = Path(path)
    # Collected around the PARSE, not just the write: mpc2emu's SoundFont
    # parser is where SF2_ENTRIES_DROPPED and SF2_PRESETS_TRUNCATED are
    # emitted, and a dropped entry SHIFTS every later ordinal -- the fault
    # resolve_ordinal() exists to reconcile.
    with convert_mod.collect_diagnostics_into(risks_out):
        bank = parse_foreign(p, wav_dir, max_presets=_listed_count(p))
    if not bank.presets:
        raise ValueError(
            f"{p.name} holds no sampled content: nothing in it references a "
            f"sample, so there is nothing to import.")
    if ordinal is not None and is_container(p):
        # Narrow the freshly-parsed Bank in place -- safe because it was
        # built for this call alone. Dropping the other presets' samples is
        # not an optimisation: a SoundFont's presets SHARE one sample pool
        # (one here spreads 5737 samples across 219 presets), so without this
        # every single-preset import would carry the whole font's audio.
        listed = list_presets(p) or []
        preset = bank.presets[resolve_ordinal(bank, listed, ordinal)]
        preset.program_number = 0
        bank.presets = [preset]
        bank.samples = _preset_samples(bank, preset)
    if not any(voice.zones for preset in bank.presets for voice in preset.voices):
        # A TAL preset whose every sample is an encrypted .talwav parses
        # SUCCESSFULLY into a preset with no zones -- talsmpl_parser appends
        # its preset unconditionally. Without this check that would be
        # written out as a bank with nothing in it. foreign_names greys those
        # rows in the Explorer; this is the same refusal at the other end,
        # for the paths that do not go through a row (search, a script).
        raise ValueError(
            f"{p.name} yielded no playable zone — its samples could not be "
            f"read. For a TAL-Sampler preset this normally means they are "
            f"encrypted .talwav files.")
    # Base scheme first, then per-sample edits on top, as import_xpm does.
    wanted = names_from_base(bank, name_base, name_octave, name_with_key)
    wanted.update(name_overrides or {})
    apply_sample_names(bank, wanted)
    return _apply_and_write(bank, opts, p.stem, risks_out)
