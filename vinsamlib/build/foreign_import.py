"""Importing an instrument this project cannot write into a native bank.

Seven formats, in two groups that behave identically here and differ in
where their conversion law comes from.

**Five soft-sampler formats**, none of them from a hardware sampler:
SoundFont 2 (``.sf2``), SFZ, Logic EXS24 (``.exs``), TAL-Sampler
(``.talsmpl``) and GigaSampler (``.gig``).

**Two hardware disc formats**, added 2026-09-22: Ensoniq EPS/ASR and Roland
S-7xx, both CD images. They are here rather than in a module of their own
because every consumer in this project -- ``ui/models.py``, ``index/scanner``,
``ui/search_resolve``, ``ui/main_window``, ``detail_pane``, ``samples_pane``
-- already routes through this module's ``inspect``/``format_for``/
``list_presets``/``import_foreign``, and a parallel spine would mean a second
branch in all seven. That is exactly the drift this file's own note below
warns about.

**What makes them different, and it matters to the UI.** For the five
soft-sampler formats mpc2emu's parser is a reading of a documented file.
For these two it is a reading of *the target sampler's own firmware*: what
EOS 4.7 does when an E4XT imports an Ensoniq or Roland disc, and what the
K2000 does. No Ensoniq or Roland instrument exists to measure against here,
so matching the device is not a compromise, it is the specification -- and
there is no alternative law to offer. Hence ``EXPERIMENTAL_FORMATS``: these
two are marked experimental in every surface that shows them, and the import
dialog says whose behaviour is being reproduced. See mpc2emu's
``docs/FIRMWARE_IMPORT_ROUTINES.md``.

All seven are **import sources only** — VinSamLib browses them and
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

from __future__ import annotations  # noqa: I001

import os
import shutil
import tempfile
from dataclasses import dataclass
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from typing import Optional

from . import convert as convert_mod
from . import firmware_sim
from .convert import ConversionOptions, _apply_and_write, _run_captured
from .sample_names import apply_sample_names, names_from_base
from .xpm_import import XpmSummary, _preset_samples, summarize_program
from .. import foreign_names
from ..config import Config
from .. import mpc2emu_bridge
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

# ── the two hardware disc formats ──────────────────────────────────────────
#
# NOT extension-keyed, and that is the whole difficulty. `.iso` is claimed by
# AKAI media, by the EMU3 filesystem (which is not ISO 9660 at all) and by
# real ISO 9660 CDs, so these are decided on CONTENT -- and only after
# `vfs.detect.sniff()` has declined the file, so an AKAI disc can never reach
# a weaker test. mpc2emu's registry._parse_iso orders its three the same way
# and says why: EPS decodes a real directory in block 2, while Roland has no
# header at all and only checks that a fixed record base holds plausible
# records, which is the weakest test of the three and so goes last.
#
# Measured here: both detectors are 0.1 ms on a 618 MB disc and both return
# False on an E4B file, so running them during a directory listing is
# affordable -- which the `inspect()` note below makes a requirement.
# `.img` and `.hda` joined `.iso` on 2026-09-22. Until that day mpc2emu's
# three-way identification was wired to `.iso` ALONE -- `.img` tried AKAI and
# then fell through to the MPC60 reader, `.hda` went straight to AKAI with no
# test at all -- so the same Roland or Ensoniq disc read fine under one name
# and failed under another. They found that while fixing the dropped-kwargs
# report from here and now share one dispatcher across all three.
#
# Safe to widen because `image_content_format()` gives vfs.detect first
# refusal, and that is what claims AKAI media, EMU3, the FAT volumes and a
# real ISO 9660 -- only a file none of them recognises is offered to the two
# weaker content tests.
IMAGE_CONTENT_EXTS = (".iso", ".img", ".hda")
EPS_FORMAT = "EPS"
ROLAND_FORMAT = "Roland"
IMAGE_CONTENT_FORMATS = (EPS_FORMAT, ROLAND_FORMAT)

#: Shown with an "experimental" marker wherever they appear, and the import
#: dialog names whose firmware is being reproduced. Not a hedge about code
#: quality: it is that the conversion law is a disassembly of someone else's
#: firmware, with no instrument here to check the result against.
EXPERIMENTAL_FORMATS = frozenset(IMAGE_CONTENT_FORMATS)

FOREIGN_FORMATS = frozenset(FOREIGN_EXT_FORMAT.values()) | set(IMAGE_CONTENT_FORMATS)

# Unlike the MPC's three containers -- which are three wrappers around one
# keygroup program, and so earn a single "MPC" chip in the format dropdown --
# these are five unrelated ecosystems that happen to share a job. A user
# looking for a SoundFont is not looking for an EXS24 instrument.
FORMAT_FILTERS = ("SF2", "SFZ", "EXS24", "TAL", "GIG", EPS_FORMAT, ROLAND_FORMAT)

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

_available: Optional[bool] = None  # noqa: UP045


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
        except Exception:  # noqa: BLE001
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


_fw_available: Optional[bool] = None  # noqa: UP045


def set_firmware_available(ok: bool) -> None:
    """Record whether this checkout can read EPS/Roland discs."""
    global _fw_available
    _fw_available = ok


def firmware_available() -> bool:
    """Whether the two disc formats are readable here.

    Separate from ``available()``: the disc parsers live on an mpc2emu
    BRANCH, so a perfectly good checkout supplies SF2/SFZ/EXS/TAL/GIG and
    none of these. One flag for both would hide five working formats
    whenever the branch is absent.
    """
    global _fw_available
    if _fw_available is None:
        try:
            _fw_available = Config.load().check_firmware_import_support()[0]
        except Exception:  # noqa: BLE001
            _fw_available = False
    return _fw_available


def format_for(path) -> Optional[str]:  # noqa: UP045
    """The format label for a path, or None if it is not ours.

    Extension for the five soft-sampler formats; content for the two disc
    formats, which share `.iso` with things that are not ours at all.
    """
    ext = Path(path).suffix.lower()
    fmt = FOREIGN_EXT_FORMAT.get(ext)
    if fmt is not None:
        return fmt
    if ext in IMAGE_CONTENT_EXTS:
        return image_content_format(path)
    return None


#: (path, mtime_ns, size) -> "EPS" / "Roland" / None. Bounded, and keyed on
#: the same (path, stamp) discipline the audition and refaudio caches use, so
#: an edited or replaced disc misses.
#:
#: WHY IT IS WORTH CACHING SOMETHING THIS CHEAP. One select of a disc row was
#: measured running `sniff()` four times and the content test four times --
#: `inspect`, `is_container`, `summary_is_cheap` and `list_presets` each ask
#: independently, and an audition asks three more. Each is an open() and a
#: read; on an NFS share that is latency per call, multiplied by the number
#: of image rows in the folder.
_FORMAT_CACHE: "OrderedDict" = OrderedDict()  # noqa: UP037
_FORMAT_CACHE_MAX = 256


def _image_stamp(path):
    try:
        st = Path(path).stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def image_content_format(path) -> Optional[str]:  # noqa: UP045
    key = _image_stamp(path)
    if key is not None and key in _FORMAT_CACHE:
        _FORMAT_CACHE.move_to_end(key)
        return _FORMAT_CACHE[key]
    answer = _image_content_format_uncached(path)
    if key is not None:
        _FORMAT_CACHE[key] = answer
        _FORMAT_CACHE.move_to_end(key)
        while len(_FORMAT_CACHE) > _FORMAT_CACHE_MAX:
            _FORMAT_CACHE.popitem(last=False)
    return answer


def _image_content_format_uncached(path) -> Optional[str]:  # noqa: UP045
    """``"EPS"``, ``"Roland"`` or None for a disc image.

    **Order is load-bearing and is not ours to reorder casually.** A disc a
    stronger test can identify must never be offered to a weaker one, so:

      1. ``vfs.detect.sniff()`` first. If it claims the file -- AKAI media,
         EMU3, FAT, a real ISO 9660 -- it is already browsable as a volume
         and this module must not also claim it, or one disc would appear
         twice in the tree under two different readers.
      2. EPS, which decodes an actual directory.
      3. Roland, which has no header at all. Last, always.

    Returns None rather than raising for anything unreadable: a listing runs
    this over every `.iso` in a folder and an unreadable file is simply not
    one of ours.
    """
    if not firmware_available():
        return None
    try:
        from ..vfs.detect import sniff

        if sniff(str(path)) is not None:
            return None
    except Exception as exc:  # noqa: BLE001
        _note_disc_failure(path, "vfs.detect.sniff", exc)
        return None
    try:
        if mpc2emu_bridge.eps_parser.is_eps_image(str(path)):
            return EPS_FORMAT
        if mpc2emu_bridge.roland_parser.is_roland_image(str(path)):
            return ROLAND_FORMAT
    except Exception as exc:  # noqa: BLE001
        _note_disc_failure(path, "content test", exc)
        return None
    return None


def _roland_partial_sizes(path, parts) -> dict:
    """Audio bytes per partial, from the two TABLES and no PCM.

    Exactly the arithmetic `parse_roland_image` does on its way to loading
    the audio, without loading it: each zone names a sample id, and the
    sample table carries that sample's `audio_len`. Deduped, because a
    partial can reach one sample through several zones and the figure wanted
    is what THIS partial costs, not the sum of its references.

    Measured: both tables read in 76 ms for a 4004-partial disc, against
    ~700 ms for the whole-disc parse that used to be launched to answer the
    same question -- and against 19 s for the EPS equivalent, which answered
    it with nothing at all.
    """
    try:
        samples = mpc2emu_bridge.roland_parser.read_roland_samples(str(path))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for i, part in enumerate(parts):
        seen, total = set(), 0
        for zone in part.get("zones", ()) or ():
            sid = zone.get("sample")
            if sid is None or sid in seen:
                continue
            seen.add(sid)
            rec = samples.get(sid) if isinstance(samples, dict) else None
            if rec is None and not isinstance(samples, dict):
                rec = samples[sid] if 0 <= sid < len(samples) else None
            if rec:
                total += int(rec.get("audio_len", 0) or 0)
        if total:
            out[i] = total
    return out
    try:
        from ..vfs.detect import sniff

        if sniff(str(path)) is not None:
            return None
    except Exception as exc:  # noqa: BLE001
        _note_disc_failure(path, "vfs.detect.sniff", exc)
        return None
    try:
        if mpc2emu_bridge.eps_parser.is_eps_image(str(path)):
            return EPS_FORMAT
        if mpc2emu_bridge.roland_parser.is_roland_image(str(path)):
            return ROLAND_FORMAT
    except Exception as exc:  # noqa: BLE001
        _note_disc_failure(path, "content test", exc)
        return None
    return None


def _note_disc_failure(path, stage: str, exc: BaseException) -> None:
    """Record a disc that was dropped because something raised.

    Returning None here is right -- a listing must not stop for one odd file
    -- but it makes "this is not one of ours" and "mpc2emu's parser threw"
    the same outcome on screen, and the second one is a disc the user
    expected to see. The call log is where that distinction survives; it is
    off by default and costs nothing when it is.
    """
    try:
        convert_mod.calllog.note(
            "disc-detect-failed",
            source=str(path),
            stage=stage,
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception:  # noqa: BLE001, S110
        pass


def inspect(path) -> Optional[FileVerdict]:  # noqa: UP045
    """What row, if any, *path* should produce.

    None means "not one of ours, do not list" -- an unknown extension, a
    macOS metadata fork, or a file whose magic says it is something else. A
    file that IS one of ours but cannot be used comes back as a verdict with
    an ``empty_reason``, never as None: a broken instrument the user can see
    and ask about beats one that silently is not there.
    """
    # EXTENSION FIRST, AND ON THE STRING. A directory listing runs this over
    # every file it holds, and the folders that matter here hold a handful of
    # programs beside hundreds of WAVs -- one measured folder is 30 programs
    # and ~920 samples. Building a Path (twice, counting format_for) to ask
    # about a suffix cost more than every other part of the listing put
    # together: 14 253 Path constructions for 31 visible rows.
    #
    # Basename only, or a dot in a DIRECTORY name answers for a file that has
    # none of its own.
    name = str(path).rpartition("/")[2]
    dot = name.rfind(".")
    if dot <= 0:
        return None
    suffix = name[dot:].lower()
    # `.iso` is let through to a CONTENT test rather than rejected here. It
    # costs a 512-byte read plus 0.1 ms, and only for files actually called
    # `.iso` -- the sample folders this early return protects hold WAVs and
    # programs, not disc images, so the measured 14 253-Path problem is
    # untouched.
    if suffix not in FOREIGN_EXT_FORMAT and suffix not in IMAGE_CONTENT_EXTS:
        return None
    if not available():
        return None
    p = Path(path)
    fmt = format_for(p)
    if fmt is None:
        return None
    if p.name.startswith(_APPLEDOUBLE_PREFIX):
        return None
    ext = p.suffix.lower()

    if ext == ".exs":
        name = foreign_names.exs_instrument_name(p)
        if name is None:
            return FileVerdict(
                fmt,
                p.stem,
                empty_reason=(
                    f"{p.name} does not read as an EXS24 instrument — its header "
                    f"carries no EXS magic."
                ),
            )
        return FileVerdict(fmt, name)

    if ext == ".sfz":
        if not foreign_names.sfz_is_listable(p):
            return FileVerdict(
                fmt,
                p.stem,
                empty_reason=(
                    f"{p.name} defines no region and includes no file, so there "
                    f"is nothing to import — it is settings for regions that are "
                    f"somewhere else."
                ),
            )
        return FileVerdict(fmt, p.stem)

    if ext == ".talsmpl":
        verdict = foreign_names.talsmpl_state(p)
        if verdict is None:
            return FileVerdict(
                fmt,
                p.stem,
                empty_reason=(f"{p.name} does not read as a TAL-Sampler preset."),
            )
        if verdict.state is foreign_names.TalState.ENCRYPTED:
            return FileVerdict(
                fmt,
                verdict.program_name,
                empty_reason=(
                    f"every sample this preset uses is an encrypted .talwav, "  # noqa: F541
                    f"which only TAL-Sampler itself can decode — the preset is "  # noqa: F541
                    f"readable, its audio is not."  # noqa: F541
                ),
            )  # noqa: F541, RUF100
        if verdict.state is foreign_names.TalState.ROM_ONLY:
            return FileVerdict(
                fmt,
                verdict.program_name,
                empty_reason=(
                    f"this preset plays TAL-Sampler's own built-in waveforms, "  # noqa: F541
                    f"not sampled audio — there is no sample here to convert."  # noqa: F541
                ),
            )  # noqa: F541, RUF100
        if verdict.state is foreign_names.TalState.NO_SAMPLES:
            return FileVerdict(
                fmt,
                verdict.program_name,
                empty_reason=(f"{p.name} references no sample at all."),
            )
        note = ""
        if verdict.state is foreign_names.TalState.PARTIAL:
            note = (
                f"{verdict.encrypted} of {verdict.total} samples are "
                f"encrypted .talwav and will be left out; the rest import "
                f"normally."
            )
        return FileVerdict(fmt, verdict.program_name, note=note)

    if ext in IMAGE_CONTENT_EXTS:
        # Deliberately does NOT list. Unlike SF2/GIG -- where listing is the
        # only way to know the file holds anything -- the content test that
        # got us here already decoded an EPS directory or validated Roland's
        # record base, so a disc that passes and then holds nothing is not a
        # case that occurs. Listing anyway cost 55 ms per Roland disc, which
        # a folder of images multiplies by every row; detection alone is
        # 0.1 ms. An empty disc shows a row that expands to nothing, which
        # is the cheaper wrong answer of the two.
        return FileVerdict(
            fmt,
            p.stem,
            container=True,
            note=(
                f"{fmt} import is experimental: it reproduces what the target "
                f"sampler's own firmware does with this disc, and no {fmt} "
                f"instrument exists here to check the result against."
            ),
        )

    # Containers: SF2 and GIG.
    listed = list_presets(p)
    if listed is None:
        return FileVerdict(
            fmt,
            p.stem,
            container=True,
            empty_reason=(
                f"{p.name} does not read as a {fmt} file — it is empty, "
                f"truncated, or something else with this extension."
            ),
        )
    if not listed:
        return FileVerdict(
            fmt, p.stem, container=True, empty_reason=(f"{p.name} holds no preset.")
        )
    return FileVerdict(fmt, p.stem, container=True)


def list_presets(path) -> Optional[list]:  # noqa: UP045
    """The rows a container file expands into, read from its header only.

    Returns ``list[foreign_names.ListedPreset]``, ``[]`` for a readable file
    holding nothing, or None when it does not read as its format at all.
    """
    ext = Path(path).suffix.lower()
    if ext == ".sf2":
        return foreign_names.sf2_preset_names(path)
    if ext == ".gig":
        return foreign_names.gig_instrument_names(path)
    if ext in IMAGE_CONTENT_EXTS:
        return _list_disc_presets(path)
    return None


def _list_disc_presets(path) -> Optional[list]:  # noqa: UP045
    """Rows for an EPS or Roland disc, WITHOUT decoding any audio.

    Measured on the reference discs: the directory read is 0.01 s for EPS
    (613 instruments) and 0.03 s for Roland (4004 partials), against 19 s to
    parse an EPS disc in full. Browsing must never pay the second number.

    **EPS lists INSTRUMENTS, not presets, and the two counts differ.** One
    instrument becomes up to four presets on import -- EOS's layer-mask
    variants, suffixed ``00``/``0*``/``*0``/``**`` -- so 613 rows here
    correspond to 2396 presets after conversion. Listing the variants would
    show the user four rows for something the disc holds one of, and they
    are an artefact of the conversion, not of the disc. ``resolve_ordinal()``
    reconciles the two counts, which is the very problem it exists for.
    """
    fmt = image_content_format(path)
    if fmt is None:
        return None
    try:
        if fmt == EPS_FORMAT:
            ents = mpc2emu_bridge.eps_parser.eps_instruments(str(path), quiet=True)
            # `size` is already on the directory entry and was being thrown
            # away. Measured on the reference disc: audio is 117.4 MB of
            # 127.2 MB of instrument files, so this is the file's size and
            # ~92 % of it is audio -- the row says which, rather than
            # implying an exact audio figure it did not measure.
            return [
                foreign_names.ListedPreset(
                    name=e.name, program=i, size=int(getattr(e, "size", 0) or 0) or None
                )
                for i, e in enumerate(ents)
            ]
        parts = mpc2emu_bridge.roland_parser.read_roland_partials(str(path))
        sizes = _roland_partial_sizes(path, parts)
        return [
            foreign_names.ListedPreset(
                name=part.get("name", ""), program=i, size=sizes.get(i)
            )
            for i, part in enumerate(parts)
        ]
    except Exception as exc:  # noqa: BLE001
        _note_disc_failure(path, "directory listing", exc)
        return None


def is_container(path) -> bool:
    ext = Path(path).suffix.lower()
    if ext in CONTAINER_EXTS:
        return True
    # A disc holds hundreds of instruments; it is a container by any measure.
    return ext in IMAGE_CONTENT_EXTS and image_content_format(path) is not None


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


def parse_foreign(
    path,
    wav_dir: Optional[str] = None,  # noqa: UP045
    max_presets: Optional[int] = None,  # noqa: UP045
    firmware_sim: bool = False,
):
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
    if ext in IMAGE_CONTENT_EXTS:
        # The disc parsers take (path, wav_dir, quiet, limit) and NEITHER
        # max_samples nor max_presets. Until 2026-09-22 mpc2emu's `.iso`
        # entry swallowed every keyword, so passing them was invisible; they
        # fixed that on our report, and forwarding now raises TypeError --
        # correctly. `limit` is deliberately not passed either: for EPS it
        # would be a PRESET ceiling applied to a count of INSTRUMENTS.
        kw: dict = {}
        if firmware_sim:
            # A DISC is the shape mpc2emu implements these four paths for,
            # so unlike AKAI nothing is assembled first -- the image goes
            # straight to the registry. Refuse rather than degrade if the
            # checkout cannot simulate, the same way the AKAI half does:
            # returning a normal conversion under the device's name is the
            # failure both projects spent a day making impossible.
            if not _disc_parser_takes_firmware_sim():
                raise ValueError(
                    "This mpc2emu checkout's disc parsers cannot simulate "
                    "the firmware. Convert as good as possible instead, or "
                    "point Settings at a checkout that carries it."
                )
            kw["firmware_sim"] = True
    else:
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


def _disc_parser_takes_firmware_sim() -> bool:
    """Do BOTH disc parsers accept ``firmware_sim``?

    Both, not either: the registry dispatches on content, so which one runs
    is not known until the disc is read, and a checkout carrying the flag on
    one reader and not the other would simulate an Ensoniq disc and quietly
    convert a Roland one.
    """
    try:
        for fn in (
            mpc2emu_bridge.eps_parser.parse_eps_image,
            mpc2emu_bridge.roland_parser.parse_roland_image,
        ):
            if "firmware_sim" not in fn.__code__.co_varnames:
                return False
    except Exception:  # noqa: BLE001
        return False
    return True


def _listed_count(path) -> Optional[int]:  # noqa: UP045
    listed = list_presets(path) if is_container(path) else None
    return len(listed) if listed else None


#: EOS's layer-mask variant suffixes, in the order mpc2emu emits them. An
#: EPS instrument becomes up to four presets, one per variant, and an empty
#: variant is skipped rather than written silent.
_EPS_VARIANT_SUFFIXES = ("00", "0*", "*0", "**")


def _eps_base_name(name: str) -> str:
    for suffix in _EPS_VARIANT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def disc_preset_indices(bank, listed: list, ordinal: int) -> list:
    """Which parsed presets belong to disc row *ordinal*.

    EPS is the one format here that parses to MORE presets than it lists --
    613 instruments became 2396 presets on the reference disc -- so
    ``resolve_ordinal()`` does not apply: it handles entries being DROPPED,
    the opposite direction.

    **Positional, not by name, and that was measured.** Name matching looked
    obvious and is wrong twice over on the reference disc: ``HARP`` is the
    name of two different instruments, and 32 instrument names are a prefix
    of another name (``ELEC BASS`` and ``ELEC BASS 1``), so a prefix match
    sweeps a neighbour's variants into the answer -- precisely the "asks for
    one preset and gets its neighbour" failure ``resolve_ordinal`` exists to
    stop.

    What holds instead, verified over the whole disc: mpc2emu emits each
    instrument's variants as one consecutive run, in listed order. A single
    forward pass consumed all 2396 presets with nothing left over, and the
    two ``HARP`` instruments fell into their own runs of four.

    Returns the parsed indices for the row, or ``[]`` when the instrument
    produced no preset at all -- two did on the reference disc, and the
    caller must say so rather than import silence.
    """
    parsed = bank.presets
    if not 0 <= ordinal < len(listed):
        raise ValueError(
            f"row {ordinal + 1} is not on this disc any more — it holds "
            f"{len(listed)}. Collapse and re-expand it to re-read."
        )
    i = 0
    for row, entry in enumerate(listed):
        want = entry.name if hasattr(entry, "name") else str(entry)
        run = []
        while i < len(parsed) and _eps_base_name(parsed[i].name) == want:
            run.append(i)
            i += 1
        if row == ordinal:
            return run
    return []


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
            f"{len(listed)}. Collapse and re-expand it to re-read."
        )
    if len(parsed) == len(listed):
        return ordinal
    if len(parsed) > len(listed):
        raise ValueError(
            f"this file parsed to {len(parsed)} presets but its header lists "
            f"{len(listed)}; import the whole file rather than one preset of "
            f"it."
        )

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
                f"whole file rather than one preset of it."
            )
        mapping[i] = parsed_index
        i += 1
    if ordinal not in mapping:
        raise ValueError(
            f"'{listed[ordinal].display}' holds no sampled content — it has "
            f"no zone referencing a sample, so there is nothing to import."
        )
    return mapping[ordinal]


# ── read-only previews ─────────────────────────────────────────────────────

#: Disc formats whose parse is WHOLE-DISC, so reading one instrument costs
#: what reading all of them costs. Measured on the reference discs, one
#: instrument at a time: Ensoniq 19.0 s, Roland 0.58 s. The difference is not
#: size -- the Roland disc is the bigger of the two at 618 MB against 358 MB.
_WHOLE_DISC_PARSE = frozenset({"EPS"})


def summary_is_cheap(path) -> bool:
    """Whether one instrument's zones can be shown on a click.

    NOT a size test, which is what the Detail pane used to ask. A disc
    preset has no size of its own, so the pane took its parent's -- the whole
    image -- and refused every disc instrument as a "large file", quoting the
    DISC's size as the instrument's. That number was the same 341 MB on every
    row of the disc, which is what made it obviously wrong.
    """
    if Path(str(path)).suffix.lower() not in IMAGE_CONTENT_EXTS:
        return True
    return image_content_format(path) not in _WHOLE_DISC_PARSE


def whole_disc_reason(path) -> str:
    """Why an instrument on this disc cannot be summarised on a click."""
    fmt = image_content_format(path) or "This"
    return (
        f"{fmt} discs parse as a whole — reading one instrument costs "
        f"what reading all of them costs (about 19 s on the reference "
        f"disc), so the zone table is not drawn on a click. Import it, "
        f"or audition it, to read its zones."
    )


def summarize_foreign(
    path,
    ordinal: Optional[int] = None,  # noqa: UP045
    wav_dir: Optional[str] = None,  # noqa: UP045
) -> XpmSummary:  # noqa: RUF100, UP045
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
        index = index_for_row(bank, listed, ordinal, path)
    return summarize_program(bank, index)


def index_for_row(bank, listed: list, ordinal: int, path) -> int:
    """The parsed preset to SHOW for disc row `ordinal`.

    `resolve_ordinal` handles entries being DROPPED between the listing and
    the parse. An EPS disc goes the other way: 613 listed instruments parse
    to 2396 presets, because EOS writes each one's layer-mask variants
    (`00`, `0*`, `*0`, `**`) as separate presets. Asked for row 2 it raised

        this file parsed to 2396 presets but its header lists 613

    and the Detail pane never showed that, because the pane refused every
    disc preset as a "large file" first -- on the DISC's byte size, which it
    had inherited. Two wrongs covering for each other.

    `disc_preset_indices` already owns the positional run mapping; the first
    index of the run is the variant EOS lists, which is the one the row
    names.
    """
    if Path(str(path)).suffix.lower() in IMAGE_CONTENT_EXTS:
        runs = disc_preset_indices(bank, listed, ordinal)
        if runs:
            return runs[0]
        return 0
    return resolve_ordinal(bank, listed, ordinal)


def load_samples_for_test(
    path,
    ordinal: Optional[int] = None,  # noqa: UP045
    wav_dir: Optional[str] = None,  # noqa: UP045
) -> list:  # noqa: RUF100, UP045
    """Read-only sample list for the Convert Options dialog's stereo Test
    button. Same parse the Detail pane preview does; never writes."""
    bank = parse_foreign(path, wav_dir, max_presets=_listed_count(path))
    if ordinal is None or not is_container(path):
        return bank.samples
    listed = list_presets(path) or []
    return _preset_samples(
        bank, bank.presets[index_for_row(bank, listed, ordinal, path)]
    )


# ── the import ─────────────────────────────────────────────────────────────


def import_foreign(
    path,
    opts: ConversionOptions,
    ordinal: Optional[int] = None,  # noqa: UP045
    wav_dir: Optional[str] = None,  # noqa: UP045
    risks_out: Optional[list] = None,  # noqa: UP045
    name_base: str = "",
    name_octave: int = 2,
    name_with_key: bool = True,
    name_overrides: Optional[dict] = None,  # noqa: UP045
) -> str:  # noqa: RUF100, UP045
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
    # REFUSE A PAIR mpc2emu refuses, here rather than inside their parser,
    # and EARLY -- which their contract now explicitly sanctions:
    # `consumers_may_enforce_earlier: "yes -- if you already know the source
    # format, refuse the pair before reading anything; the rule is the pair,
    # not the moment it is checked"`. Their own `enforced_in_mpc2emu` says
    # "after parsing", and that is a fact about their CLI, which sees only a
    # filename. It is not a property of the rule, and reading it as one
    # would make us defer a check we can make immediately.
    # This binds the ordinary conversion too -- the rule's `applies_to` lists
    # both modes -- and a caller that is not the dialog (a restored session,
    # a matrix test, a script) reaches this with no picker to have greyed the
    # target out. Before the parse, because we already know the source
    # format by content and need not pay for a 19-second disc read to learn
    # the answer is no.
    _src_fmt = format_for(p) or ""
    _refusal = firmware_sim.refuse_target(_src_fmt, opts.target_format)
    if _refusal:
        raise ValueError(_refusal)
    # Collected around the PARSE, not just the write: mpc2emu's SoundFont
    # parser is where SF2_ENTRIES_DROPPED and SF2_PRESETS_TRUNCATED are
    # emitted, and a dropped entry SHIFTS every later ordinal -- the fault
    # resolve_ordinal() exists to reconcile.
    with convert_mod.collect_diagnostics_into(risks_out):
        # A disc lists INSTRUMENTS and parses to more presets than that, so
        # its listed count is not a preset ceiling and must not be passed as
        # one. (mpc2emu's `.iso` entry currently ignores the kwarg, so this
        # is belt and braces -- but the day it stops ignoring it, an EPS
        # import would silently keep 613 of 2396 presets.)
        cap = None if p.suffix.lower() in IMAGE_CONTENT_EXTS else _listed_count(p)
        bank = parse_foreign(
            p,
            wav_dir,
            max_presets=cap,
            firmware_sim=bool(getattr(opts, "match_device_import", False)),
        )
    if not bank.presets:
        raise ValueError(
            f"{p.name} holds no sampled content: nothing in it references a "
            f"sample, so there is nothing to import."
        )
    if ordinal is not None and is_container(p):
        # Narrow the freshly-parsed Bank in place -- safe because it was
        # built for this call alone. Dropping the other presets' samples is
        # not an optimisation: a SoundFont's presets SHARE one sample pool
        # (one here spreads 5737 samples across 219 presets), so without this
        # every single-preset import would carry the whole font's audio.
        listed = list_presets(p) or []
        if p.suffix.lower() in IMAGE_CONTENT_EXTS:
            # A disc row is an INSTRUMENT, which can be several presets --
            # EOS's layer-mask variants. Importing one row imports its whole
            # variant group, because the variants are the instrument: taking
            # only the first would silently drop the layers the user can see
            # named on the disc.
            indices = disc_preset_indices(bank, listed, ordinal)
            if not indices:
                name = listed[ordinal].name if ordinal < len(listed) else "?"
                raise ValueError(
                    f"{name!r} yielded no preset — the disc lists it, but "
                    f"nothing in it references a sample. Two instruments on "
                    f"the reference disc are like this."
                )
            chosen = [bank.presets[i] for i in indices]
            for n, preset in enumerate(chosen):
                preset.program_number = n
            bank.presets = chosen
            keep: list = []
            for preset in chosen:
                for sample in _preset_samples(bank, preset):
                    if not any(sample is k for k in keep):
                        keep.append(sample)
            bank.samples = keep
        else:
            # index_for_row, not resolve_ordinal: an EPS disc parses to MORE
            # presets than it lists, which resolve_ordinal refuses outright.
            preset = bank.presets[index_for_row(bank, listed, ordinal, path)]
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
            f"encrypted .talwav files."
        )
    # Base scheme first, then per-sample edits on top, as import_xpm does.
    wanted = names_from_base(bank, name_base, name_octave, name_with_key)
    wanted.update(name_overrides or {})
    apply_sample_names(bank, wanted)
    return _apply_and_write(bank, opts, p.stem, risks_out)
