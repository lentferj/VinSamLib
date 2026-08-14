"""
E4B bank container: parse and assemble at the raw-chunk level.

An E4B file is an IFF-like `FORM E4B0` container — see
``mpc2emu/docs/E4B_FORMAT.md`` §1 and ``mpc2emu/writers/e4b_writer.py`` (the
only place this layout is otherwise implemented, as a *writer* from a
``models.common.Bank``). This module is independent of that: it works
directly on the on-disk bytes, because **bank assembly here is container-
level surgery, not parse-and-re-serialize**. Copying a preset's/sample's
original chunk bytes verbatim and only patching the handful of embedded
numeric references is lossless — it preserves every parameter the format
carries, including ones neither mpc2emu's `Bank` model nor any public RE
covers. Going through `models.common.Bank` would silently degrade real
commercial banks (which this format is full of — E4B predates any Python
tooling for it).

Container layout (big-endian throughout):
    FORM <size> E4B0
      TOC1   — 32-byte entries: tag, data_size, file_offset(of the chunk's
               OWN tag, not its body), idx, name[16], null, midi_prog
      E4Ma   — 256-byte multimap (MIDI channel -> preset routing)
      E4P1   — one per preset (82-byte header + voice blocks)
      E3S1   — one per sample (94-byte header + 16-bit PCM)
      EMSt   — 1366-byte master setup, ALWAYS last, NOT listed in the TOC

Preset body (relevant fields only — full layout in e4b_writer.py's module
docstring):
    [0:2]   own index, BE u16 (0-based)
    [2:18]  name, 16-byte space-padded ASCII
    [20:22] num_voices, BE u16
    [82:]   voice blocks, back-to-back, each:
        vpar[0:110]      voice params; vpar[4] = this voice's zone count
        vpar[110:374]    primary zone table + mod matrix (not needed here)
        [374:]           secondary zone table: n_zones * 22-byte entries
                         each entry[10:12] = sample index, BE u16, 1-based
        (+2 trailing zero bytes after the LAST voice's zone table only)

Sample body:
    [0:2]   own index, BE u16 (1-based)
    [2:18]  name, 16-byte space-padded ASCII (display form, may include a
            note-name suffix — see e4b_writer._sample_display_name)
    [18:]   struct emu3_sample + PCM (opaque to this module)
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Optional

FORM_MAGIC = b"FORM"
FORM_TYPE = b"E4B0"
TOC_TAG = b"TOC1"
E4MA_TAG = b"E4Ma"
PRES_TAG = b"E4P1"
SAMP_TAG = b"E3S1"
EMST_TAG = b"EMSt"

MAX_NAME = 16
PRES_HDR = 82
VOICE_FIXED = 284
ZONE_ENTRY = 22

#: Byte offsets inside a 22-byte zone entry, confirmed against mpc2emu's
#: e4b_writer (`entry[2]/[5]/[14] = lo/hi/root`) and verified by reading 40
#: zones of a real preset back through its parser -- identical, including the
#: voice clamp below.
ZONE_LO_KEY, ZONE_HI_KEY, ZONE_ROOT_KEY = 2, 5, 14

#: A VOICE carries its own key window at these vpar offsets, and the reader
#: takes `max(voice_lo, zone_lo)` / `min(voice_hi, zone_hi)`. So a zone moved
#: outside its voice's window is silently clamped back -- widening the voice
#: is not optional bookkeeping, it is what makes the edit take effect.
VOICE_LO_KEY, VOICE_HI_KEY = 14, 17

#: A voice's VELOCITY window, same stride as the key window above. This is
#: where velocity layering lives in an E4B -- NOT in the zone entry, whose own
#: velocity bytes read (0, 127) on every real bank measured here. Reading the
#: zone said 0.4% of presets are velocity-layered; reading the voice says
#: 36.4% of 1604, up to nine windows in one preset.
#:
#: Confirmed against mpc2emu's parser over 40 voices of a bank where hi_vel
#: actually VARIES (65 vs 127). That detail is the whole verification: in a
#: bank where every hi_vel is 127, nine different offsets "match" it, and
#: picking one of those would have been a coin toss dressed as a measurement.
VOICE_LO_VEL, VOICE_HI_VEL = 18, 21

# Bank limits from writers/bank_splitter.py (_MAX_SAMPLES_PER_BANK / _MAX_PRESETS_PER_BANK)
MAX_PRESETS = 1000
MAX_SAMPLES = 1000
MAX_BANK_BYTES = 128 * 1024 * 1024


class E4BFormatError(ValueError):
    pass


@dataclass
class E4BPreset:
    index: int              # 0-based, as currently embedded in the body
    name: str
    midi_program: int
    body: bytes              # verbatim E4P1 chunk body
    zone_refs: list[tuple[int, int]] = field(default_factory=list)
    # (absolute byte offset of the 22-byte zone entry within `body`, sample index it currently holds)

    @property
    def sample_indices(self) -> list[int]:
        return sorted({idx for _off, idx in self.zone_refs})

    @property
    def num_voices(self) -> int:
        return struct.unpack_from(">H", self.body, 20)[0]

    @property
    def num_zones(self) -> int:
        return len(self.zone_refs)


@dataclass
class E4BSample:
    index: int               # 1-based, as currently embedded in the body / TOC
    name: str
    body: bytes               # verbatim E3S1 chunk body (94-byte header + PCM)

    @property
    def size(self) -> int:
        return len(self.body)


#: Bytes of E3S1 header before the PCM. Every loop/length field in the header
#: is a BYTE offset from the struct start, so a frame index is
#: `(field - PCM_OFFSET) // 2` (E4B_FORMAT.md, sample-header table).
PCM_OFFSET = 92

#: Where the PCM actually begins in the chunk body. The header fields count
#: from 92, but the body carries a 2-byte `sample_idx` in front of emu3bm's
#: 92-byte struct — mpc2emu's parser slices `body[start_l + 2:]` for exactly
#: this reason. So a frame index is `(field - 92) // 2` and it indexes a
#: buffer starting at 94.
PCM_START = 94

#: E4B PCM is LITTLE-endian, where KRZ is big. Passed explicitly to
#: banks/loopcheck.py, which reads one as the other otherwise and reports a
#: corpus of impossibly clean loops.
PCM_BIG_ENDIAN = False

#: `options` bit that marks a forward loop (0x0031 = MONO_L | LOOP).
_OPT_LOOP = 0x0001


def sample_pcm(samp: "E4BSample") -> bytes:
    """The sample's PCM, without the header. For a stereo sample this is the
    left channel followed by the right; loop points index the left."""
    return samp.body[PCM_START:]


def sample_loops(samp: "E4BSample") -> list[tuple[int, int]]:
    """[(loop_start_frame, loop_end_frame)] for a looped E4B sample.

    Frames are indices into this sample's OWN PCM (unlike KRZ, where they are
    absolute in a shared region), and `loop_end_frame` is the last frame
    PLAYED.

    THE +1 IS NOT A GUESS. `loop_end_l` stores the frame BEFORE the true
    inclusive last loop frame, so the last played frame is
    `(loop_end_l - 92) // 2 + 1`. ConvertWithMoss established this (PR #220)
    by measuring the amplitude step at the loop seam across a commercial
    corpus: reading the raw value left a discontinuity in many samples, and
    the +1 raised the clean-seam share from 78% to 95%. mpc2emu encodes and
    decodes it consistently, so it matters only when reading a third-party
    bank -- which is exactly what this program does.
    """
    b = samp.body
    if len(b) < 62:
        return []
    opts = struct.unpack_from("<H", b, 60)[0]
    if not (opts & _OPT_LOOP):
        return []
    start_b = struct.unpack_from("<I", b, 38)[0]
    end_b = struct.unpack_from("<I", b, 46)[0]
    if start_b < PCM_OFFSET or end_b < PCM_OFFSET:
        return []
    frames = max(0, (len(b) - PCM_START) // 2)
    start = (start_b - PCM_OFFSET) // 2
    end = min((end_b - PCM_OFFSET) // 2 + 1, frames - 1)
    return [(start, end)] if end > start >= 0 else []


@dataclass
class E4BFile:
    path: str
    presets: list[E4BPreset]
    samples: dict[int, E4BSample]   # keyed by (original) 1-based index
    e4ma_body: bytes
    emst_body: bytes
    warnings: list[str] = field(default_factory=list)


def _apply_placement(body: bytearray, voice_start: int, zone_off: int,
                      lo: int, root: int, hi: int) -> None:
    """Move one zone, widening its voice's window to match.

    The voice clamp is the whole subtlety. A reader resolves a zone as
    `max(voice_lo, zone_lo) .. min(voice_hi, zone_hi)`, so patching only the
    zone entry leaves any move BEYOND the voice's existing window with no
    effect at all -- the bytes change and nothing plays differently, which is
    the worst kind of silent failure for an editing feature.
    """
    lo, root, hi = (max(0, min(127, v)) for v in (lo, root, hi))
    body[zone_off + ZONE_LO_KEY] = lo
    body[zone_off + ZONE_HI_KEY] = hi
    body[zone_off + ZONE_ROOT_KEY] = root
    if lo < body[voice_start + VOICE_LO_KEY]:
        body[voice_start + VOICE_LO_KEY] = lo
    if hi > body[voice_start + VOICE_HI_KEY]:
        body[voice_start + VOICE_HI_KEY] = hi


def _name16(s: str) -> bytes:
    # latin-1, to be the exact inverse of _strip_name below. This used to
    # encode ASCII, which silently turned every byte above 0x7E into "?" --
    # and only on the TOC path, because a sample's chunk body is copied
    # verbatim. One real bank in this project's library then came out of
    # assemble() with the SAME sample named two different things depending
    # on which of the two a reader trusts.
    #
    # `replace` still matters here, unlike in _strip_name: a name typed in
    # New Bank can hold a codepoint above 0xFF, which latin-1 genuinely
    # cannot encode. A real bank name off a real image never can.
    return s.encode("latin-1", errors="replace")[:MAX_NAME].ljust(MAX_NAME, b" ")


def _strip_name(raw: bytes) -> str:
    # latin-1 rather than ASCII because a real E4XT writes bytes above 0x7E
    # into this field -- they are whatever glyph the author picked off its
    # front panel, and EOS's character ROM is not ISO-8859-1, so the
    # codepoint we show is a best effort. The BYTE, though, survives
    # unchanged through _name16 and back, which is what a librarian needs.
    #
    # No `errors=` argument: latin-1 maps all 256 byte values by definition
    # and cannot fail. One used to be passed, which read as though this
    # guarded against undecodable input; it never did anything.
    return raw.rstrip(b"\x00 ").decode("latin-1")


def _iff_chunk(tag: bytes, body: bytes) -> bytes:
    out = tag + struct.pack(">I", len(body)) + body
    if len(body) % 2:
        out += b"\x00"
    return out


def _walk_voices(body: bytes, num_voices: int):
    """Yield (voice_start, zone_table_start, n_zones) for each voice in a
    preset body, mirroring e4b_writer._build_voice's packing convention:
    voices are back-to-back with no gap, and only the LAST voice carries
    2 trailing zero bytes after its zone table.

    n_zones comes from vpar[2:4] (`trailer_off`, a BE u16 byte offset to
    where the zone table's trailing entry begins — what e4b_writer's own
    docstring says "E4XT uses to locate/validate the next voice", and what
    mpc2emu's e4b_parser._parse_voice derives n_zones from too), NOT from
    the single-byte vpar[4] zone-count field. Both are written consistently
    by e4b_writer, but at least one real-world test bank in this project's
    corpus has a voice where they disagree — vpar[4] undercounted the real
    zone table by 4 entries, silently dropping a referenced sample when
    trusted. vpar[2:4] is also the one hardware itself actually navigates
    by, per the docstring above, making it the more authoritative field."""
    offset = PRES_HDR
    for i in range(num_voices):
        if offset + 4 > len(body):
            break
        trailer_off = struct.unpack_from(">H", body, offset + 2)[0]
        n_zones = max(0, (trailer_off - VOICE_FIXED) // ZONE_ENTRY)
        zone_table_start = offset + VOICE_FIXED
        voice_len = VOICE_FIXED + n_zones * ZONE_ENTRY
        if i == num_voices - 1:
            voice_len += 2
        yield offset, zone_table_start, n_zones
        offset += voice_len


def _parse_zone_refs(body: bytes) -> list[tuple[int, int]]:
    num_voices = struct.unpack_from(">H", body, 20)[0]
    out = []
    for _voice_start, zone_table_start, n_zones in _walk_voices(body, num_voices):
        for k in range(n_zones):
            eo = zone_table_start + k * ZONE_ENTRY
            if eo + 12 > len(body):
                break
            idx = struct.unpack_from(">H", body, eo + 10)[0]
            out.append((eo, idx))
    return out


# ── parsing ──────────────────────────────────────────────────────────────────

# Padding between/after chunks: an all-zero header parses as a zero-size
# chunk with a zero tag, which is how a 75.8 MB bank produced 6.9 M phantom
# chunks before _walk_chunks_physical learned to skip the run in one search.
_ZERO_TAG = b"\x00\x00\x00\x00"
_NON_ZERO = re.compile(rb"[^\x00]")


def _walk_chunks_physical(data: bytes, path: str, warnings: list[str]):
    """Yield (tag, body) by walking the FORM sequentially using each
    chunk's own physical header (tag + BE size). Mirrors mpc2emu's own
    ``parsers.e4b_parser._walk_chunks`` — its docstring explicitly notes it
    "does not trust TOC1 offsets — more robust against third-party files".
    Real E4XT/EOS hardware reads a FORM the same linear way, so this (not
    the TOC) is the format's actual ground truth.

    Three things learned from real commercial banks that mpc2emu's own
    reader already accounts for and this mirrors:
      - The walk must be bounded by the FORM's declared `form_size` (offset
        4:8), not the physical file length. CD/HD images pad file data out
        to a cluster/sector boundary, so bytes past the real content are
        leftover padding — walking past `form_size` here previously
        mis-parsed that padding as bogus further chunks and crashed.
      - Real banks contain chunk types beyond E4Ma/E4P1/E3S1/TOC1 (e.g.
        'EMS0'-style variants seen in commercial libraries). mpc2emu's own
        `_walk_chunks` doesn't reject them either — its caller just filters
        for the tags it wants and silently ignores everything else. Doing
        the same here (skip, don't raise) is what makes real-world files
        parse at all instead of erroring on their first unrecognised chunk.
      - A run of zero bytes is padding, not chunks. Reading it as chunks
        "works" -- an all-zero header is tag `\\0\\0\\0\\0` with size 0, so
        the walk simply advances 8 bytes and repeats -- but one real 75.8 MB
        commercial bank is mostly such padding, and crawling it produced
        6 949 036 phantom chunks: 8.8 s to parse, +717 MB resident, and (with
        a warning appended per unrecognised chunk, as this used to do) 347 MB
        of identical warning strings retained for the lifetime of the
        E4BFile. The Explorer caches parsed banks on their tree node, so that
        was 700 MB held for one expanded row. The run is now skipped in a
        single search. Padding is skipped, NOT stopped at: the same bank's
        real E3S1/E4P1 chunks continue afterwards.

      - EMSt must NOT be treated as an early-exit stop condition, even
        though it's documented as "always the last chunk": at least one
        real commercial bank in this project's library is a multi-part
        "mega-bank" — several complete TOC1/E4Ma/.../EMSt groups
        concatenated back to back (presumably several originally-separate
        library sub-banks merged into one file). Stopping at the FIRST
        EMSt silently discarded every preset/sample after it. mpc2emu's
        own `_walk_chunks` doesn't special-case EMSt at all — it just
        yields every chunk up to `end` unfiltered, relying on its caller
        to ignore tags it doesn't want. Doing the same here is what let
        this exact bank's remaining 630 of 700 samples surface.
    """
    form_size = struct.unpack_from(">I", data, 4)[0]
    end = min(12 + form_size, len(data))
    pos = 12
    while pos + 8 <= end:
        tag = data[pos:pos + 4]
        size = struct.unpack_from(">I", data, pos + 4)[0]
        if tag == _ZERO_TAG and size == 0:
            # Padding, not a chunk (see the docstring). Jump the whole zero
            # run at once. A chunk tag is ASCII and can hold no zero byte, so
            # the first non-zero byte is the next real header; & ~1 keeps the
            # IFF two-byte alignment a real chunk always starts on. The run is
            # at least these 8 bytes, so pos always advances.
            m = _NON_ZERO.search(data, pos, end)
            if m is None:
                return
            pos = m.start() & ~1
            continue
        body_start = pos + 8
        body_end = body_start + size
        if body_end > len(data):
            warnings.append(f"chunk {tag!r} at {pos} claims size {size}, "
                             f"which runs past the end of the file — stopping scan")
            return
        yield tag, data[body_start:body_end]
        pos = body_end + (size & 1)


def parse_bytes(data: bytes, path: str = "<bytes>") -> E4BFile:
    if data[:4] != FORM_MAGIC or data[8:12] != FORM_TYPE:
        raise E4BFormatError(f"{path}: not an E4B file (missing FORM...E4B0 header)")

    # TOC1 is OPTIONAL: at least one real commercial bank in this project's
    # library goes straight from the FORM/E4B0 header to E4Ma, with no TOC1
    # at all. When present, it's parsed only for the one piece of metadata
    # no chunk body carries — a preset's MIDI program number — keyed by the
    # preset's own embedded index (not by TOC offset/size: those have been
    # observed to disagree with a chunk's own physical header by a couple
    # of bytes, uniformly, in at least one file in this project's own test
    # corpus — a stale writer-side bookkeeping artifact, not real IFF
    # padding). Content extraction below never uses TOC offsets/sizes, only
    # the sequential physical walk, which is what mpc2emu's own reference
    # reader and real hardware both do — and works with or without a TOC.
    toc_chunk_size = 0
    midi_prog_by_idx: dict[int, int] = {}
    if data[12:16] == TOC_TAG:
        toc_chunk_size = struct.unpack_from(">I", data, 16)[0]
        toc_body = data[20:20 + toc_chunk_size]
        for i in range(toc_chunk_size // 32):
            entry = toc_body[i * 32:i * 32 + 32]
            if entry[0:4] == PRES_TAG:
                idx = struct.unpack_from(">H", entry, 12)[0]
                midi_prog_by_idx[idx] = entry[31]

    e4ma_body = b""
    presets: list[E4BPreset] = []
    samples: dict[int, E4BSample] = {}
    warnings: list[str] = []
    unknown_tags: dict[bytes, int] = {}

    for tag, body in _walk_chunks_physical(data, path, warnings):
        if tag == E4MA_TAG:
            if not e4ma_body:   # keep the FIRST one in a multi-part mega-bank
                e4ma_body = body
        elif tag == PRES_TAG:
            if len(body) < PRES_HDR:
                warnings.append(f"skipping malformed E4P1 chunk (shorter than its fixed header)")
                continue
            idx = struct.unpack_from(">H", body, 0)[0]
            name = _strip_name(body[2:18])
            presets.append(E4BPreset(
                index=idx, name=name, midi_program=midi_prog_by_idx.get(idx, 0),
                body=body, zone_refs=_parse_zone_refs(body)))
        elif tag == SAMP_TAG:
            if len(body) < 18:
                warnings.append(f"skipping malformed E3S1 chunk (shorter than its fixed header)")
                continue
            idx = struct.unpack_from(">H", body, 0)[0]
            name = _strip_name(body[2:18])
            samples[idx] = E4BSample(index=idx, name=name, body=body)
        elif tag in (TOC_TAG, EMST_TAG):
            # Expected in a multi-part "mega-bank" (several concatenated
            # TOC1/E4Ma/.../EMSt groups) — a mid-stream TOC1/EMSt belongs
            # to an earlier or later sub-bank, not an anomaly worth
            # surfacing. The bank-wide E4Ma/EMSt actually carried over by
            # this module always come from the FIRST group (see below) /
            # the true last chunk (found separately by tag search).
            pass
        else:
            # Real commercial banks contain other chunk types too (e.g.
            # 'EMS0'-style variants seen in commercial libraries) — skip
            # them rather than failing the whole bank, exactly as
            # mpc2emu's own reference reader's caller does.
            #
            # Counted, not appended one per chunk: a warning list is a
            # diagnostic, and it must not be able to grow with the size of
            # the file it describes. One malformed 75.8 MB bank used to leave
            # 347 MB of identical strings alive inside the returned E4BFile.
            unknown_tags[tag] = unknown_tags.get(tag, 0) + 1

    for tag, count in unknown_tags.items():
        warnings.append(f"skipped unrecognised chunk tag {tag!r}"
                        + (f" ({count} of them)" if count > 1 else ""))

    # EMSt carries no TOC entry and is always the last chunk (E4B_FORMAT.md
    # §1); find it directly by its own tag (which cannot legitimately appear
    # anywhere else in the file) rather than by arithmetic. Not fatal if
    # missing/short — a handful of real commercial banks in this project's
    # library have trailing bytes past the nominal end (CD/HD image
    # padding); losing the master-setup chunk means MIDI routing defaults
    # get re-created on assembly, not that the bank is unusable.
    emst_body = b""
    emst_pos = data.rfind(EMST_TAG)
    if emst_pos < 20 + toc_chunk_size:
        warnings.append("no EMSt (master setup) chunk found")
    else:
        emst_size = struct.unpack_from(">I", data, emst_pos + 4)[0]
        emst_end = min(emst_pos + 8 + emst_size, len(data))
        emst_body = data[emst_pos + 8:emst_end]
        if len(data) - emst_end > 1:
            warnings.append(
                f"{len(data) - emst_end} trailing byte(s) after the EMSt chunk "
                f"(likely CD/HD image padding)")

    presets.sort(key=lambda p: p.index)
    return E4BFile(path=path, presets=presets, samples=samples,
                    e4ma_body=e4ma_body, emst_body=emst_body, warnings=warnings)


def parse(path: str) -> E4BFile:
    with open(path, "rb") as f:
        data = f.read()
    return parse_bytes(data, path)


# ── assembly ─────────────────────────────────────────────────────────────────

def _apply_velocity(body: bytearray, voice_start: int,
                     lo_vel: int, hi_vel: int) -> None:
    """Set the VOICE's velocity window. Unlike a key move there is no zone
    half to this: the zone entry carries no usable velocity range, so the
    voice is the whole edit."""
    # Clamped to the byte range, NOT reordered. An inverted window is how a
    # velocity layer is switched off in real material, so swapping the two
    # would re-enable something somebody silenced on purpose. The editor
    # flags it instead.
    body[voice_start + VOICE_LO_VEL] = max(0, min(127, lo_vel))
    body[voice_start + VOICE_HI_VEL] = max(0, min(127, hi_vel))


def _split_voices_by_velocity(body: bytearray, num_voices: int,
                               wants: dict) -> bytearray:
    """Rebuild a preset body so every distinct velocity window gets its own
    VOICE. `wants` maps a zone's offset in `body` to the (lo_vel, hi_vel) it
    should answer to; a zone not mentioned keeps whatever its current voice
    says.

    WHY THIS HAS TO EXIST. E4B stores the velocity window on the voice, not
    the zone, so "give this one sample its own window" is not a byte patch --
    the sample has to be moved into a voice of its own. Without it the feature
    is unusable on exactly the banks a user builds: mpc2emu's writer emits one
    voice per distinct window, so an imported folder is ONE voice holding
    every zone (156 of them, measured) and every row is locked. Hand-authored
    banks tend to one zone per voice and were the only place it worked.

    Zones wanting the SAME window share the new voice rather than getting one
    each -- that is what the format means by a velocity layer, and it keeps
    the voice count down.

    This is the first edit here that changes a preset body's LENGTH. It is
    safe to do so because chunk sizes and TOC entries are computed from
    `len(body)` further down, never carried over from the source.
    """
    header = bytearray(body[:PRES_HDR])
    rebuilt: list = []
    for v_start, table_start, n in _walk_voices(body, num_voices):
        vpar = bytearray(body[v_start:v_start + VOICE_FIXED])
        current = (vpar[VOICE_LO_VEL], vpar[VOICE_HI_VEL])
        groups: dict = {}
        order: list = []
        for k in range(n):
            off = table_start + k * ZONE_ENTRY
            zone = bytes(body[off:off + ZONE_ENTRY])
            win = wants.get(off, current)
            if win not in groups:
                groups[win] = []
                order.append(win)
            groups[win].append(zone)
        # Order preserved so a bank nobody re-placed comes back unchanged.
        for win in order:
            clone = bytearray(vpar)
            clone[VOICE_LO_VEL], clone[VOICE_HI_VEL] = win
            rebuilt.append((clone, groups[win]))

    out = bytearray(header)
    for vpar, zones in rebuilt:
        nz = len(zones)
        # BOTH places the zone count lives, or the walk and the reader
        # disagree: vpar[2:4] is the offset to the voice's end, and vpar[4]
        # repeats the count. Verified equal across 20 810 real voices.
        struct.pack_into(">H", vpar, 2, VOICE_FIXED + nz * ZONE_ENTRY)
        vpar[4] = min(0xFF, nz)
        out += vpar
        for zone in zones:
            out += zone
    out += b"\x00\x00"        # the single trailer, after the LAST voice only
    struct.pack_into(">H", out, 20, len(rebuilt) & 0xFFFF)
    return out


def assemble(selections: list[tuple[E4BFile, E4BPreset]],
              sample_names: Optional[dict] = None,
              zone_placement: Optional[dict] = None,
              voice_velocity: Optional[dict] = None) -> bytes:
    """Build a new E4B FORM from selected (source_bank, preset) pairs.

    Each preset's original chunk bytes are copied verbatim; only the 2-byte
    sample-index field of each zone entry (and the preset's own embedded
    index at body[0:2]) is patched to match the new, renumbered layout.
    `sample_names` renames samples on the way out, {current name: new name}.
    Applied AFTER the dedupe decision, so renaming cannot merge two distinct
    samples or split one that appears twice -- the bank that comes out has
    exactly the samples it would have had, under different names. Safe in
    this format because an E4B zone points at a sample INDEX, not at a name
    (see the zone patching below); the name is a label, and the only places
    it is stored are the sample chunk's own 16-byte field and its TOC entry,
    both rewritten here.

    Samples are deduplicated by (name, exact content) across every source
    bank touched, so pulling the same sample into a new bank via two
    different presets/banks doesn't duplicate its PCM.

    E4Ma and EMSt are carried over verbatim from the FIRST selected preset's
    source bank — real E4Ma/EMSt content is bank-wide (MIDI routing / master
    setup), not preset-specific, so there is no principled way to "merge"
    them; taking the first source's is what mpc2emu's own multi-source
    tooling (bank_splitter) does for equivalent bank-wide chunks.
    """
    if not selections:
        raise ValueError("no presets selected")
    if len(selections) > MAX_PRESETS:
        raise ValueError(f"too many presets: {len(selections)} > {MAX_PRESETS}")

    preserve_from = selections[0][0]

    new_sample_bodies: list[bytes] = []
    new_sample_names: list[str] = []
    dedupe_key_to_new_idx: dict[tuple, int] = {}

    new_preset_bodies: list[bytes] = []
    new_preset_names: list[str] = []
    new_preset_progs: list[int] = []

    for src, preset in selections:
        body = bytearray(preset.body)
        # zone offset -> the voice that owns it, so a placement edit can widen
        # that voice's own key window. Built once per preset rather than
        # searched per zone.
        vel_wanted: dict = {}
        owner: dict[int, int] = {}
        if zone_placement or voice_velocity:
            for v_start, table_start, n in _walk_voices(preset.body,
                                                         preset.num_voices):
                for k in range(n):
                    owner[table_start + k * ZONE_ENTRY] = v_start
        for zone_off, old_idx in preset.zone_refs:
            samp = src.samples.get(old_idx)
            if samp is None:
                # Dangling reference: leave the byte exactly as it is. Not
                # necessarily damage — the sibling eosed project measured an
                # E4XT leaving every voice pointing at its old sample number
                # after erasing all RAM samples, so "this voice plays sample
                # N" and "sample N exists" are independent questions on real
                # hardware. Also covers a ROM sample and the empty-zone
                # sentinels (3FFFh/3FFEh), which resolve to no sample here
                # and must survive into the new bank unrewritten.
                continue
            # Placement is keyed by SAMPLE NAME, the shape
            # SamplePlacementDialog returns, and applied before the dedupe
            # decision is even consulted -- it rewrites zone bytes in this
            # preset's own body and cannot affect which samples are distinct.
            move = (zone_placement or {}).get(samp.name)
            if move is not None and zone_off in owner:
                _apply_placement(body, owner[zone_off], zone_off, *move)
            # Velocity belongs to the VOICE, so this patches whatever voice
            # owns the zone -- and every other zone in that voice moves with
            # it. That is not a shortcut: a voice has ONE velocity window and
            # there is nowhere else to put a per-zone one. The pane refuses to
            # offer the edit when a voice is shared, rather than silently
            # dragging a neighbour's sample along.
            # Collected rather than applied here. A voice holding several
            # samples has ONE window, so writing it in place would drag the
            # neighbours along; the split pass below gives this zone a voice
            # of its own instead. Applied in place only when the voice already
            # holds nothing else, which the split pass also handles -- it
            # simply produces one group.
            vel = (voice_velocity or {}).get(samp.name)
            if vel is not None and zone_off in owner:
                # Applied as typed; see _apply_velocity for why an inverted
                # window is preserved rather than tidied up.
                vel_wanted[zone_off] = tuple(
                    max(0, min(127, int(v))) for v in vel)
            key = (samp.name, samp.body)
            new_idx = dedupe_key_to_new_idx.get(key)
            if new_idx is None:
                if len(new_sample_bodies) >= MAX_SAMPLES:
                    raise ValueError(f"too many distinct samples: > {MAX_SAMPLES}")
                new_idx = len(new_sample_bodies) + 1
                # The sample's OWN embedded index (body[0:2], BE u16 — see
                # e4b_writer._build_sample_header) must be patched to match
                # its new position too, not just the zone entries that
                # reference it: a reader (mpc2emu's own e4b_parser included)
                # keys samples by this embedded value, not by file position.
                sbody = bytearray(samp.body)
                struct.pack_into(">H", sbody, 0, new_idx & 0xFFFF)
                final_name = (sample_names or {}).get(samp.name, samp.name)
                if final_name != samp.name:
                    # The name lives in the chunk body too, not only in the
                    # TOC entry built further down -- a rename that patched
                    # one and not the other would show two different names
                    # for one sample depending on which a reader trusts.
                    sbody[2:18] = _name16(final_name)
                new_sample_bodies.append(bytes(sbody))
                new_sample_names.append(final_name)
                dedupe_key_to_new_idx[key] = new_idx
            struct.pack_into(">H", body, zone_off + 10, new_idx & 0xFFFF)
        # LAST, because it renumbers every zone offset in the body: the
        # patches above address zones by their offset in the original layout.
        if vel_wanted:
            body = _split_voices_by_velocity(body, preset.num_voices, vel_wanted)
        struct.pack_into(">H", body, 0, len(new_preset_bodies) & 0xFFFF)
        new_preset_bodies.append(bytes(body))
        new_preset_names.append(preset.name)
        new_preset_progs.append(preset.midi_program)

    e4ma_body = preserve_from.e4ma_body
    emst_body = preserve_from.emst_body
    if not e4ma_body or not emst_body:
        # Source bank was missing one of these bank-wide chunks (seen in a
        # few real commercial banks in this project's library — see
        # parse_bytes' warnings). Fall back to mpc2emu's own defaults rather
        # than writing an empty/short chunk a real E4XT wouldn't expect.
        from ..mpc2emu_bridge import e4b_writer
        e4ma_body = e4ma_body or e4b_writer._build_e4ma()
        emst_body = emst_body or e4b_writer._build_emst()

    return _build_form(new_preset_bodies, new_preset_names, new_preset_progs,
                        new_sample_bodies, new_sample_names, e4ma_body, emst_body)


def _toc_entry(tag: bytes, data_size: int, file_offset: int, idx: int,
               name: str, midi_prog: int = 0) -> bytes:
    e = bytearray(32)
    e[0:4] = tag
    struct.pack_into(">I", e, 4, data_size)
    struct.pack_into(">I", e, 8, file_offset)
    struct.pack_into(">H", e, 12, idx)
    e[14:30] = _name16(name)
    e[31] = min(127, max(0, midi_prog)) & 0xFF
    return bytes(e)


def _build_form(preset_bodies: list[bytes], preset_names: list[str],
                 preset_progs: list[int], sample_bodies: list[bytes],
                 sample_names: list[str], e4ma_body: bytes, emst_body: bytes) -> bytes:
    n_toc = 1 + len(preset_bodies) + len(sample_bodies)
    toc_chunk = _iff_chunk(TOC_TAG, bytes(n_toc * 32))   # placeholder, same final length
    e4ma_chunk = _iff_chunk(E4MA_TAG, e4ma_body)
    preset_chunks = [_iff_chunk(PRES_TAG, b) for b in preset_bodies]
    sample_chunks = [_iff_chunk(SAMP_TAG, b) for b in sample_bodies]

    pos = 12 + len(toc_chunk)
    e4ma_off = pos
    pos += len(e4ma_chunk)

    preset_offs = []
    for c in preset_chunks:
        preset_offs.append(pos)
        pos += len(c)

    sample_offs = []
    for c in sample_chunks:
        sample_offs.append(pos)
        pos += len(c)

    toc_entries = bytearray()
    toc_entries += _toc_entry(E4MA_TAG, len(e4ma_body), e4ma_off, 0, "Multimap")
    for i, (body, name, prog) in enumerate(zip(preset_bodies, preset_names, preset_progs)):
        toc_entries += _toc_entry(PRES_TAG, len(body), preset_offs[i], i, name, midi_prog=prog)
    for i, (body, name) in enumerate(zip(sample_bodies, sample_names)):
        toc_entries += _toc_entry(SAMP_TAG, len(body), sample_offs[i], i + 1, name)
    toc_chunk = _iff_chunk(TOC_TAG, bytes(toc_entries))

    emst_chunk = _iff_chunk(EMST_TAG, emst_body)
    pos += len(emst_chunk)

    # FORM size quirk (e4b_writer.write_e4b / E4B_FORMAT.md §6.1): counts chunk
    # bytes only, excludes the 4-byte 'E4B0' form type.
    form_size = pos - 12

    out = bytearray()
    out += FORM_MAGIC
    out += struct.pack(">I", form_size)
    out += FORM_TYPE
    out += toc_chunk
    out += e4ma_chunk
    for c in preset_chunks:
        out += c
    for c in sample_chunks:
        out += c
    out += emst_chunk

    if len(out) > MAX_BANK_BYTES:
        raise ValueError(f"assembled bank too large: {len(out)} > {MAX_BANK_BYTES} bytes")
    return bytes(out)
