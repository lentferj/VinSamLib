"""Naming what is inside a non-hardware instrument file, without opening it.

The Explorer lists a directory on a worker thread every time a user clicks a
folder, and the indexer walks every library root. Neither may parse a
soft-sampler file: mpc2emu's ``parse_sf2`` reads the whole thing and slices
out every sample's PCM, which for one 1 GB SoundFont in this author's library
is 2.7 s and ~3 GB of RSS. Doing that per row is not slow, it is impossible.

Every one of these formats already carries its names in a header, far from
the audio. Reading only that header is the entire trick, and it is cheap
enough to disappear: all 7 instrument names of a **985 MB** ``.gig`` come out
in **1.6 ms**, because the wave pool is never touched.

This is the same split ``build/xpm_import.py``'s ``project_program_names()``
makes for MPC projects, for the same reason, and it keeps the same two rules:

* **Never guess.** A name that cannot be read honestly is not invented. The
  ordinals these functions hand out are positions *in the file*, and
  ``build/foreign_import.resolve_ordinal()`` aligns them against what
  mpc2emu's parser actually returns rather than assuming the two agree —
  ``sf2_parser`` and ``gig_parser`` both skip entries that carry no zones.
* **Never hide what cannot be understood.** A ``None``/negative verdict here
  means "do not list this as one of ours", not "this file is broken"; callers
  show a broken file with its reason rather than making it vanish.

Nothing here imports Qt, mpc2emu or ``build/`` — it is bytes in, names out,
so it can be run over a whole corpus by a plain script.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional

# ── shared RIFF walking (SF2 and GIG are both RIFF) ────────────────────────
#
# Streaming, never whole-file: every walk seeks over chunk payloads instead of
# reading them, which is what keeps a 985 MB .gig as cheap as a 5 KB one.

_MAX_CHUNK_DEPTH_BYTES = 1 << 20   # a header chunk we are willing to read whole


def _riff_form(f, expect: bytes) -> Optional[int]:
    """End offset of a RIFF file whose form type is *expect*, else None."""
    head = f.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != expect:
        return None
    size = struct.unpack_from("<I", head, 4)[0]
    return 8 + size


def _chunks(f, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Yield (chunk id, payload offset, payload size) within *end*.

    The walker restores the file position after each yield, so a caller may
    descend into a chunk without having to put the cursor back. Sizes are
    clamped to *end*: a truncated file is common enough in a real library (one
    ``.sf2`` here is cut off mid-``pdta``) that running off the end must be a
    stop, not an exception.
    """
    while True:
        pos = f.tell()
        if pos + 8 > end:
            return
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        cid = hdr[:4]
        size = struct.unpack_from("<I", hdr, 4)[0]
        data_off = f.tell()
        if size > end - data_off:
            size = max(0, end - data_off)
        nxt = data_off + size + (size & 1)   # RIFF pads odd payloads
        yield cid, data_off, size
        f.seek(nxt)


def _find_list(f, end: int, list_type: bytes) -> Optional[tuple[int, int]]:
    """(payload offset, end offset) of the first ``LIST`` of *list_type*."""
    for cid, off, size in _chunks(f, end):
        if cid != b"LIST":
            continue
        if f.read(4) == list_type:
            return f.tell(), off + size
    return None


def _find_chunk(f, end: int, want: bytes) -> Optional[tuple[int, int]]:
    """(payload offset, size) of the first chunk with id *want*."""
    for cid, off, size in _chunks(f, end):
        if cid == want:
            return off, size
    return None


def _cstr(raw: bytes, strip: bool = True) -> str:
    """A fixed-width name field: NUL-padded, not NUL-terminated-and-trimmed.

    ``strip=False`` for anything that will later be matched against a name
    mpc2emu produced. Its ``_safe_name()`` keeps leading whitespace, so
    trimming here would make two names that are the same name compare unequal
    — which is exactly how a preset ends up mapped to its neighbour. Found on
    ``TJMOOG02.sf2``, where 2 of 21 presets are stored as ``" MOOG SQ"``.
    """
    out = raw.split(b"\x00")[0].decode("latin-1", errors="replace")
    return out.strip() if strip else out


@dataclass(frozen=True)
class ListedPreset:
    """One row a container file will show, before anything is parsed.

    ``name`` is the name **as stored**, not as mpc2emu will render it: a
    browse row shows the whole thing, while a written preset gets truncated
    to the 16 ASCII characters an E4B name field holds. Same distinction
    ``_project_program_labels`` draws in ui/models.py, and for the same
    reason — a row called ``Grand Piano Mk II`` must not display as
    ``Grand Piano Mk``.

    ``name`` is also kept **untrimmed**, because it is what
    ``resolve_ordinal()`` matches against mpc2emu's parsed preset names; use
    ``display`` for anything a user reads.
    """
    name: str
    program: int = 0
    bank: int = 0

    @property
    def display(self) -> str:
        return self.name.strip() or "(unnamed)"


# ── SoundFont 2 ────────────────────────────────────────────────────────────

_PHDR_SIZE = 38   # sf2_parser.PHDR_SIZE


def sf2_preset_names(path) -> Optional[list[ListedPreset]]:
    """Every preset in a SoundFont, from its ``pdta``/``phdr`` chunk alone.

    ``phdr`` is a flat array of 38-byte records (20-byte name, preset and
    bank numbers, a bag index) and its **last record is the terminal "EOP"
    sentinel** required by the spec — a real preset count is ``count - 1``.
    ``sf2_parser`` drops it the same way (``phdr_cnt - 1``); a sentinel shown
    as a row would be a phantom preset named EOP.

    Returns None when the file is not a readable SoundFont at all — no
    ``RIFF``/``sfbk``, or no ``pdta``. Two of the 474 in this author's library
    answer None (one 0 bytes, one truncated before its ``pdta``); both are
    genuinely unreadable and mpc2emu raises on them too.
    """
    try:
        with open(path, "rb") as f:
            end = _riff_form(f, b"sfbk")
            if end is None:
                return None
            pdta = _find_list(f, end, b"pdta")
            if pdta is None:
                return None
            pdta_off, pdta_end = pdta
            f.seek(pdta_off)
            found = _find_chunk(f, pdta_end, b"phdr")
            if found is None:
                return None
            off, size = found
            count = size // _PHDR_SIZE
            if count < 2:      # only the sentinel, or not even that
                return []
            f.seek(off)
            raw = f.read(count * _PHDR_SIZE)
    except OSError:
        return None
    out = []
    for i in range(count - 1):          # drop the EOP sentinel
        rec = raw[i * _PHDR_SIZE:(i + 1) * _PHDR_SIZE]
        if len(rec) < 24:
            break
        preset, bank = struct.unpack_from("<HH", rec, 20)
        out.append(ListedPreset(_cstr(rec[:20], strip=False), preset, bank))
    return out


# ── GigaSampler ────────────────────────────────────────────────────────────

def gig_instrument_names(path) -> Optional[list[ListedPreset]]:
    """Every instrument in a ``.gig``, from the DLS instrument list alone.

    GIG is DLS with extensions, so the instruments live in
    ``RIFF 'DLS ' → LIST 'lins' → LIST 'ins '``, each naming itself in its
    ``INFO``/``INAM``. The wave pool (``wvpl``) is the entire file size and is
    seeked straight over.

    An instrument with no ``INAM`` falls back to ``InstNNN`` exactly as
    ``gig_parser`` does (``f"Inst{inst_count:03d}"``), so a row and the preset
    it imports to carry the same name.
    """
    try:
        with open(path, "rb") as f:
            end = _riff_form(f, b"DLS ")
            if end is None:
                return None
            lins = _find_list(f, end, b"lins")
            if lins is None:
                return None
            lins_off, lins_end = lins
            f.seek(lins_off)
            out: list[ListedPreset] = []
            for cid, off, size in _chunks(f, lins_end):
                if cid != b"LIST" or f.read(4) != b"ins ":
                    continue
                ins_end = off + size
                name, program, bank = None, 0, 0
                here = f.tell()
                insh = _find_chunk(f, ins_end, b"insh")
                if insh is not None and insh[1] >= 12:
                    f.seek(insh[0])
                    _regions, bank, program = struct.unpack("<III", f.read(12))
                    program &= 0x7F     # gig_parser masks the same bit
                f.seek(here)
                info = _find_list(f, ins_end, b"INFO")
                if info is not None:
                    info_off, info_end = info
                    f.seek(info_off)
                    inam = _find_chunk(f, info_end, b"INAM")
                    if inam is not None and inam[1] <= _MAX_CHUNK_DEPTH_BYTES:
                        f.seek(inam[0])
                        name = _cstr(f.read(inam[1]), strip=False)
                out.append(ListedPreset(
                    name or f"Inst{len(out):03d}", program, bank))
    except OSError:
        return None
    return out


# ── Logic EXS24 ────────────────────────────────────────────────────────────

_EXS_MAGIC = 0x01000000          # exs24_parser.HEADER_MAGIC_LE
_EXS_MAGIC_V11 = 0x00000101      # exs24_parser.HEADER_MAGIC_LE_V11
_EXS_TYPE_FLAG = 0x40000000      # exs24_parser._V11_TYPE_FLAG


def exs_instrument_name(path) -> Optional[str]:
    """The instrument name of an ``.exs``, from its 84-byte header.

    Detection mirrors ``parse_exs24`` exactly, including the order of the two
    tests and the ``0x40000000`` flag bit some Logic Pro X exports OR into the
    magic: get this wrong in either direction and the Explorer disagrees with
    what an import will do.

    Only the classic layout stores a real name (64 bytes at offset 20); the
    v1.1 layout does not, and ``parse_exs24`` uses the file stem there, so
    this does too.

    Returning None for "not an EXS" also quietly disposes of the 22 macOS
    AppleDouble ``._*`` forks sitting next to the real files in this author's
    library — they are 4 KB of resource-fork metadata carrying the same name
    as a real preset, and listing them would double every row in those packs.
    """
    p = Path(path)
    try:
        with open(p, "rb") as f:
            head = f.read(84)
    except OSError:
        return None
    if len(head) < 84:
        return None
    magic = struct.unpack_from("<I", head, 0)[0]
    if (magic & ~_EXS_TYPE_FLAG) == _EXS_MAGIC_V11:
        return p.stem
    if magic != _EXS_MAGIC:
        return None
    return _cstr(head[20:84]) or p.stem


# ── SFZ ────────────────────────────────────────────────────────────────────

# SFZ is plain text with no magic number, so "is this one of ours?" can only
# be answered by content. A file is listable once it declares a region or
# pulls one in by include -- <control>/<global> alone describe settings for
# regions that are not there. 1 MB of head covers every one of the 1821 in
# this author's library (the largest is 412 KB whole).
_SFZ_HEAD_BYTES = 1 << 20
_SFZ_MARKERS = (b"<region", b"#include")


def sfz_is_listable(path) -> bool:
    """Whether an ``.sfz`` holds anything an import could act on."""
    try:
        with open(path, "rb") as f:
            head = f.read(_SFZ_HEAD_BYTES).lower()
    except OSError:
        return False
    return any(m in head for m in _SFZ_MARKERS)


# ── TAL-Sampler ────────────────────────────────────────────────────────────

class TalState(Enum):
    """What an import would be able to do with a ``.talsmpl``."""
    OK = "ok"
    PARTIAL = "partial"        # some samples encrypted, some usable
    ENCRYPTED = "encrypted"    # every sample encrypted -- nothing to import
    NO_SAMPLES = "no_samples"  # references no sample at all
    ROM_ONLY = "rom_only"      # only TAL's built-in waveforms, which are not files


@dataclass(frozen=True)
class TalVerdict:
    state: TalState
    encrypted: int = 0
    total: int = 0
    program_name: str = ""

    @property
    def importable(self) -> bool:
        return self.state in (TalState.OK, TalState.PARTIAL)


# TAL 6.0 embeds its audio as base64 float32 inside the preset, which is why
# these files reach hundreds of MB (the largest here is 232 MB). Crucially
# <sampledata> sits *before* the <multisample> elements, so a bounded head
# read would find the audio and miss the references -- there is no cheap
# partial read of a big one. There does not need to be: a .talsmpl only gets
# big *because* it embeds audio, so past this size the verdict is OK by
# construction and the file is never opened. Classifying the whole 1712-file
# corpus costs 0.5 s this way against 9.8 s reading every byte.
_EMBEDDED_AUDIO_MIN = 4 << 20

_TAL_URL = re.compile(rb'url="([^"]*)"')
_TAL_PROGRAM_NAME = re.compile(rb'programname="([^"]*)"')
_TAL_SAMPLEDATA = re.compile(rb"<sampledata", re.IGNORECASE)

# A `url` is only a file reference when it names one. TAL also puts its
# built-in oscillator waveforms in the same attribute -- `url="Saw"`,
# `"Rect"`, `"Pulse"`, `"Noise"` -- and those are not files anywhere:
# mpc2emu looks for them on disk, does not find them, and skips the zone.
# 14 presets in this author's library reference nothing else, and counting a
# waveform as a sample would advertise them as importable when they import
# to an empty bank.
_TAL_AUDIO_EXTS = (".wav", ".aif", ".aiff", ".talwav")
_TAL_ENCRYPTED_EXT = ".talwav"


def _tal_urls(raw: bytes) -> tuple[list[str], list[str]]:
    """(file references, built-in waveform names) from a preset's XML."""
    refs, rom = [], []
    for found in _TAL_URL.findall(raw):
        url = found.decode("latin-1").strip()
        if not url:
            continue                      # an unused layer slot, not a sample
        (refs if url.lower().endswith(_TAL_AUDIO_EXTS) else rom).append(url)
    return refs, rom


def talsmpl_sample_urls(path) -> list[str]:
    """The decodable sample files a ``.talsmpl`` references, as stored.

    Paths come out exactly as written, backslashes and all -- resolving them
    is ``build/foreign_import``'s job, and it needs the original to do it.
    Encrypted ``.talwav`` references are left out: nothing can read them, so
    nothing should go looking.
    """
    try:
        p = Path(path)
        if p.stat().st_size >= _EMBEDDED_AUDIO_MIN:
            return []                     # v6.0 carries its audio inside
        raw = p.read_bytes()
    except OSError:
        return []
    refs, _rom = _tal_urls(raw)
    return [u for u in refs if not u.lower().endswith(_TAL_ENCRYPTED_EXT)]


def talsmpl_state(path) -> Optional[TalVerdict]:
    """Whether a ``.talsmpl``'s samples can actually be read, and its name.

    ``.talwav`` is TAL's own encrypted audio container. Nothing outside
    TAL-Sampler can decode it — mpc2emu skips those samples with a warning,
    ConvertWithMoss refuses the file outright — and in this author's library
    that is not an edge case: **749 of 1712 presets** reference only
    ``.talwav``, mostly the commercial vendor packs.

    Saying so here is what lets the Explorer show those presets greyed with a
    reason instead of either hiding them or letting them fail at import.
    The distinction matters more than it looks: ``talsmpl_parser`` appends its
    preset unconditionally, so an all-``.talwav`` preset parses *successfully*
    into a preset with no zones, and would be written out as an empty bank.

    Empty ``url=""`` attributes are unused layer slots, not references, and
    are not counted; neither are built-in waveform names (see ``_tal_urls``).

    Measured over the 1712 presets here: 745 all-encrypted, 66 with embedded
    audio, 14 referencing only built-in waveforms, the rest usable.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
        if size >= _EMBEDDED_AUDIO_MIN:
            return TalVerdict(TalState.OK, 0, 0, p.stem)
        raw = p.read_bytes()
    except OSError:
        return None
    if b"<tal" not in raw[:4096].lower():
        return None
    refs, rom = _tal_urls(raw)
    name = _TAL_PROGRAM_NAME.search(raw)
    program_name = _cstr(name.group(1)) if name else p.stem
    embedded = _TAL_SAMPLEDATA.search(raw) is not None
    if not refs:
        if embedded:
            state = TalState.OK
        elif rom:
            state = TalState.ROM_ONLY
        else:
            state = TalState.NO_SAMPLES
        return TalVerdict(state, 0, 0, program_name or p.stem)
    encrypted = sum(1 for u in refs if u.lower().endswith(_TAL_ENCRYPTED_EXT))
    if encrypted == 0:
        state = TalState.OK
    elif encrypted < len(refs) or embedded:
        # Either some references are readable, or the preset carries embedded
        # audio of its own -- in both cases part of it imports.
        state = TalState.PARTIAL
    else:
        state = TalState.ENCRYPTED
    return TalVerdict(state, encrypted, len(refs), program_name or p.stem)
