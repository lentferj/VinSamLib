"""
KRZ bank container: parse and assemble at the raw-object level.

A `.KRZ` file is a flat object database, not a chunk hierarchy — see
``mpc2emu/docs/KRZ_FORMAT.md`` §1-2 and ``mpc2emu/writers/krz_writer.py``.
mpc2emu also has its own KRZ *reader* now (``parsers/krz_parser.py``,
added 2026-07-27) — used for the vintage resample/reduce conversion
pipeline (``build/convert.py``), where going through its semantic
``models.common.Bank`` is the whole point. But `assemble()` below still
works directly on the on-disk bytes rather than through that model —
**container-level surgery, not parse-and-re-serialize** — because the
two have different jobs: assembling a new bank from browsed presets must
preserve every parameter a real soundset carries byte-for-byte,
including any this project's own RE (or mpc2emu's Bank model) hasn't
covered, which a parse-and-rebuild through *any* semantic model would
silently lose. Same reasoning as ``banks/e4b.py``, which keeps its own
container-level reader for the identical reason even though mpc2emu's
E4B parser has existed all along.

Container layout (big-endian throughout; see KRZ_FORMAT.md §2):
    PRAM <osize> <rest[6]>          32-byte file header
    object block  (Sample | Keymap | Program | other, e.g. FX)  — repeated
    int32 = 0                       object-section end marker
    <PCM>                           raw 16-bit signed BE, from `osize`

Object block (variable length):
    [0:4]   blocksize, BE i32, NEGATIVE = block_start - block_end
    [4:6]   hash, BE u16 = (type<<10)+id  (types 36/37/38, bit 0x8000 set)
                        or (type<<8)+id   (other types — KurzFiler/CWM's
                        conditional decode; mpc2emu's writer never emits
                        these but real soundsets do, e.g. type 28 = FX)
    [6:8]   size, BE u16 (redundant with blocksize; not used for navigation)
    [8:10]  ofs, BE u16 = byte offset from the `ofs` field to the object's
            own data (i.e. object data starts at block_start + 8 + ofs)
    [10:]   name, ASCII, null-terminated, then padded to the `ofs` boundary

A block's own physical length never needs to change during assemble() (name
and body length are copied verbatim) — only a few embedded reference fields
get patched in place: the hash (renumbered type/id), a Program layer's CAL
segment keymap-id, a Keymap entry's sample-id, and a Sample's PCM word
offsets. `blocksize` itself is therefore reused unmodified.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

#: Longest authored name in 9 700 real KRZ objects, and the width of the
#: K2000's own display. E4B and EIII enforce it with a fixed field; KRZ
#: has to be told.
MAX_NAME = 16

FILE_MAGIC = b"PRAM"

T_PROGRAM = 36
T_KEYMAP = 37
T_SAMPLE = 38

KEYMAP_HDR = 28
KEYMAP_ENTRY_SIZE = 5
NUM_KEYS = 128

KEYMAP_HDR_FIXED = 12   # `>6h`: sampleId, method, basePitch, centsPerEntry,
                        # entriesPerVel, entrySize
KEYMAP_LEVELS_OFF = 12  # velocity-level table: 2-byte signed offset each,
                        # relative to its own position
#: K2000 velocity slots (ppp..fff). A keymap holds one entry table per
#: DISTINCT `Level[]` address, so between 1 and 8 of them -- see band_starts().
NUM_VELO_LEVELS = 8

#: The K2000 sounds keymap entry `i` at MIDI key `i + 12`, so entries cover
#: keys 12..139 and keys 0..11 cannot be addressed. Hardware-confirmed by
#: mpc2emu 2026-08-02 (`791364a`); its corpus-only reading before that, like
#: this project's, had entry `i` at key `i` and was wrong.
KEYMAP_ENTRY_NOTE_OFFSET = 12

SAMPLE_HDR = 12
SFH_SIZE = 32
ENV_SIZE = 12

CAL_TAG = 0x40
# A program layer's FX/Studio reference: tag 0x0F, id in the first u16 of its
# 7 data bytes. Every 0x0F ref measured in this library names a type-113
# object; ids for such types are 8-bit (see _encode_hash), so the whole space
# is 0..255 per type.
FX_TAG = 0x0F
FX_ID_OFF = 0
FX_MAX_ID = 0xFF
CAL_KEYMAP_OFF_1 = 7    # 2 bytes, BE u16 — primary keymap slot
CAL_KEYMAP_OFF_2 = 11   # 2 bytes, BE u16 — secondary keymap slot ("CAL[7,8] is a 2nd keymap slot")

MAX_PRESETS = 1000      # K2000 hardware limit on user object ids per type (id space 200-999-ish)

# The id space a program/keymap/sample hash can actually address. _encode_hash
# packs those three types as `(type << 10) | (id & 0x3FF)`, so 1023 is the
# largest id that survives the round trip -- while both reference writers emit
# the id as a full u16. Past this, a reference names an id no object owns and
# two objects collide onto one hash.
#
# MAX_PRESETS does NOT bound this: it caps the number of PROGRAMS staged,
# and assemble() mints from one shared counter across all three types. 781
# programs -- comfortably inside the cap -- once produced 2829 objects, ids
# wrapping into 0..1023 and 1536 program keymap references pointing at
# keymaps that no longer existed. banks/e4b.py caps its samples separately
# (MAX_SAMPLES); this is the KRZ equivalent it never had.
MAX_OBJECT_ID = 0x3FF


class KrzFormatError(ValueError):
    pass


@dataclass(frozen=True)
class KeymapLayout:
    """Where a keymap's entries live and what each one carries.

    A keymap's entry layout is NOT fixed: the header's `method` bitfield says
    which per-entry fields are present, which sets both the stride and where
    the sample id sits (KRZ_FORMAT.md §3.2).

        0x10 tuning i16   0x08 tuning i8   0x04 volAdj u8
        0x02 sampleID u16                  0x01 subSample u8

    `id_off` is None when bit 0x02 is clear — a **compacted** keymap, whose
    entries carry no sample id at all: every key plays `header_sid`.

    Assuming mpc2emu's own write form (method 0x13: a 5-byte entry with the
    id at offset 2) does not survive contact with real content. Of 1584
    keymaps across this project's 201-file K2000 library, 1450 use a
    different stride and 1145 are compacted."""
    header_sid: int
    method: int
    num_keys: int
    table: int          # body offset of the first velocity level's entry table
    stride: int
    id_off: int | None  # offset of the sample id within an entry, or None
    #: Body offset of EVERY velocity band's entry table, `table` included and
    #: first. A keymap is not one table but up to eight -- see `band_starts()`
    #: for the arithmetic. Reading only `table` reads only the softest band.
    bands: tuple[int, ...] = ()

    def entry_offsets(self):
        """Body offset of every entry's SAMPLE ID, across every band.

        The one place the band walk lives, so the reference walk and the
        id patcher cannot drift apart -- they did, and both read band 0."""
        if self.id_off is None:
            return
        for base in (self.bands or (self.table,)):
            for k in range(self.num_keys):
                yield base + k * self.stride + self.id_off


def keymap_layout(body: bytes) -> KeymapLayout | None:
    """Decode a keymap body's header into a KeymapLayout, or None if it is
    too short to be one. Mirrors mpc2emu's `krz_parser._parse_keymap_object()`
    — keep the two in step."""
    if len(body) < KEYMAP_HDR_FIXED:
        return None
    header_sid, method, _base, _cents, entries_per_vel, _entry_size = \
        struct.unpack_from(">6h", body, 0)
    method &= 0xFFFF

    # Recomputed from the method bits rather than trusted from the header's
    # own entrySize field, since it must agree with how entries are walked.
    id_off, stride = None, 0
    if method & 0x10:
        stride += 2
    elif method & 0x08:
        stride += 1
    if method & 0x04:
        stride += 1
    if method & 0x02:
        id_off, stride = stride, stride + 2
    if method & 0x01:
        stride += 1
    stride = max(1, stride)

    if len(body) < KEYMAP_LEVELS_OFF + 2:
        return None
    table = KEYMAP_LEVELS_OFF + struct.unpack_from(">h", body,
                                                   KEYMAP_LEVELS_OFF)[0]
    if table < 0 or table > len(body):
        return None
    num_keys = entries_per_vel + 1
    return KeymapLayout(header_sid=header_sid & 0xFFFF, method=method,
                        num_keys=num_keys, table=table,
                        stride=stride, id_off=id_off,
                        bands=band_starts(body, num_keys, stride))


def band_starts(body: bytes, num_keys: int, stride: int) -> tuple[int, ...]:
    """Body offset of every velocity band's entry table, lowest first.

    A Keymap is a 28-byte header followed by up to EIGHT bands of
    `num_keys` entries each, one per velocity slot (ppp..fff). Which slot
    uses which band is encoded entirely in the header's `Level[8]` table at
    `KEYMAP_LEVELS_OFF`, as `Level[j] = (8-j)*2 + band(j) * band_size` --
    the arithmetic is `KKeymap.write()`'s, by way of kurzfiler-ng's
    `krz/keymap_bands.py`, which derived it from that source and confirmed
    it against two real multi-band keymaps.

    Each `Level[j]` is a signed offset **relative to its own position** in the
    table -- exactly what `KEYMAP_LEVELS_OFF`'s comment has always said -- so
    slot `j`'s band begins at `KEYMAP_LEVELS_OFF + 2*j + Level[j]`. Distinct
    values are the distinct bands. This is the form
    `krz_parser._parse_keymap_object()` uses, and this function exists to put
    us back in step with it: mpc2emu has read all eight slots since it was
    written, and only this project's container layer read one.

    THE MISREADING, kept because it is the natural one and survives almost
    every file: dropping the `2*j` and taking `KEYMAP_LEVELS_OFF + Level[j]`.
    For `j = 0` those agree, so a single-band keymap -- 10 463 of the 10 650
    here -- decodes perfectly either way. On a real 2-band keymap carrying
    `[16,14,12,10,8,6,388,386]` the correct reading gives bands at 28 and 412;
    the naive one puts the two loudest slots at 398 and 400, inside band 0's
    own entries, where they decode to tuning bytes read as sample ids.

    Returns `(table,)` -- one band, exactly the behaviour this had before --
    whenever the slots resolve to a single address or anything looks wrong.
    A keymap that was already read correctly must keep assembling
    byte-for-byte identically; that is the property worth more than the fix.
    """
    single = (KEYMAP_LEVELS_OFF
              + struct.unpack_from(">h", body, KEYMAP_LEVELS_OFF)[0],)
    if len(body) < KEYMAP_LEVELS_OFF + 2 * NUM_VELO_LEVELS:
        return single
    levels = struct.unpack_from(f">{NUM_VELO_LEVELS}h", body, KEYMAP_LEVELS_OFF)
    starts = sorted({KEYMAP_LEVELS_OFF + 2 * j + lv
                     for j, lv in enumerate(levels)})
    if len(starts) <= 1:
        return single
    span = num_keys * stride
    # Every band must lie inside the object's own body. A band that would be
    # walked past the end is not a band we can read, and reading it anyway
    # pulls neighbouring objects' bytes in as sample ids.
    if starts[0] < 0 or starts[-1] + span > len(body):
        return single
    return tuple(starts)


def band_velocity_windows(body: bytes, num_keys: int, stride: int) -> dict:
    """{band start offset: (lo_vel, hi_vel)} for every band `band_starts()`
    finds, from the SAME `Level[8]` arithmetic.

    The eight slots are the K2000's fixed velocity levels ppp..fff, each
    covering 16 of the 128 velocities: slot j is `16*j .. 16*j+15`. Several
    slots can point at one band -- that is how a keymap has, say, two layers
    rather than eight -- so a band's window runs from the lowest to the
    highest slot that names it.

    A single-band keymap gets `(0, 127)`, which is what every caller assumed
    unconditionally before velocity bands were read at all."""
    starts = band_starts(body, num_keys, stride)
    if len(starts) <= 1:
        return {starts[0]: (0, 127)} if starts else {}
    if len(body) < KEYMAP_LEVELS_OFF + 2 * NUM_VELO_LEVELS:
        return {starts[0]: (0, 127)}
    levels = struct.unpack_from(f">{NUM_VELO_LEVELS}h", body, KEYMAP_LEVELS_OFF)
    slots: dict = {}
    for j, lv in enumerate(levels):
        slots.setdefault(KEYMAP_LEVELS_OFF + 2 * j + lv, []).append(j)
    out = {}
    for st in starts:
        js = slots.get(st)
        if not js:
            continue
        step = 128 // NUM_VELO_LEVELS
        out[st] = (min(js) * step, max(js) * step + step - 1)
    return out


def _decode_hash(hash_val: int) -> tuple[int, int]:
    """Conditional decode per KRZ_FORMAT.md §2.2's cross-implementation
    note: when bit 0x8000 is set (types 36/37/38), type = hash>>10,
    id = hash & 0x3FF. When clear (other types, e.g. FX=28), KurzFiler and
    ConvertWithMoss decode type = hash>>8, id = hash & 0xFF instead of
    mpc2emu's writer-only unconditional >>10 — real soundsets contain those
    other types, so this reader needs the conditional form to label them
    correctly (mpc2emu itself never emits or reads them)."""
    if hash_val & 0x8000:
        return hash_val >> 10, hash_val & 0x3FF
    return hash_val >> 8, hash_val & 0xFF


def _encode_hash(type_code: int, obj_id: int) -> int:
    if type_code in (T_PROGRAM, T_KEYMAP, T_SAMPLE):
        return ((type_code << 10) | (obj_id & 0x3FF)) & 0xFFFF
    return (((type_code & 0xFF) << 8) | (obj_id & 0xFF)) & 0xFFFF


def _seg_len(tag: int) -> int:
    """Segment (tag, fixed-length body) table — mirrors
    writers/krz_writer.py's own `_seg_len`. A tag byte of 0x00 is never a
    real segment (none of the tags any program layer uses is ever 0 — see
    `_TPL_GLOBAL`/`_TPL_LAYER` in krz_writer.py), so it unambiguously marks
    the 2-byte `struct.pack('>H', 0)` end-of-segments terminator that
    `_write_program_object` writes directly (not through `_pack_segment`,
    so it doesn't follow this table itself)."""
    if tag in (0x08, 0x09):
        return 15
    if tag == 0x0F:
        return 7
    masked = tag & 0xF8
    if masked == 0x18:
        return 3
    if masked in (0x10, 0x14, 0x68):
        return 7
    if masked in (0x20, 0x50):
        return 15
    if masked in (0x40, 0x78):
        return 31
    return 0


def _walk_segments(body: bytes):
    """Yield (tag, data_start_abs, data) for each segment in a Program
    body, stopping at the 2-byte zero terminator. `data_start_abs` is the
    byte offset (within `body`) of the segment's data, i.e. right after
    its 1-byte tag — used to compute absolute patch offsets."""
    pos = 0
    while pos < len(body):
        tag = body[pos]
        if tag == 0:
            return
        length = _seg_len(tag)
        data_start = pos + 1
        seg = body[data_start:data_start + length]
        # A truncated final segment yields fewer bytes than the tag promises,
        # and every caller unpacks at a fixed offset -- which raised
        # struct.error out of assemble() AND out of the browse path, so one
        # damaged program made a whole bank unopenable. Stop instead: what
        # follows cannot be walked either.
        if len(seg) < length:
            return
        yield tag, data_start, seg
        pos = data_start + length


@dataclass
class KrzObject:
    type: int
    id: int
    name: str
    block: bytes   # the full physical block: blocksize field through end of block

    def _ofs(self) -> int:
        return struct.unpack_from(">H", self.block, 8)[0]

    def body_start(self) -> int:
        """Absolute offset (within `block`) where the object's own
        type-specific data begins."""
        return 8 + self._ofs()

    def body(self) -> bytes:
        return self.block[self.body_start():]


@dataclass
class KrzFile:
    path: str
    rest: tuple            # header rest[0..5]; rest[2] = software version (KRZ_SOFTWARE_VERSION)
    osize: int
    programs: dict[int, KrzObject]
    keymaps: dict[int, KrzObject]
    samples: dict[int, KrzObject]
    other_objects: list[KrzObject]
    pcm: bytes              # raw big-endian 16-bit PCM region (== data[osize:])
    #: (type, id, name) of any object a later object with the SAME id hid.
    #: Empty for all but 3 banks here; surfaced so a duplicate cannot pass as
    #: a bank that simply held fewer objects.
    shadowed_ids: tuple = ()
    _length_cache: dict = field(default_factory=dict, repr=False, compare=False)

    # ── reference extraction (used by both summary display and assemble) ──

    def program_keymap_refs(self, prog: KrzObject) -> list[int]:
        """Every (nonzero) keymap id referenced by a Program's CAL segments,
        across all its layers, in encounter order (may repeat)."""
        out = []
        for tag, data_start, data in _walk_segments(prog.body()):
            if tag != CAL_TAG:
                continue
            for off in (CAL_KEYMAP_OFF_1, CAL_KEYMAP_OFF_2):
                kid = struct.unpack_from(">H", data, off)[0]
                if kid:
                    out.append(kid)
        return out

    def program_fx_refs(self, prog: KrzObject) -> list[int]:
        """Every (nonzero) FX/Studio object id a Program's 0x0F segments name,
        in encounter order (may repeat).

        The id is the first u16 of the segment's 7 data bytes. Located by
        measurement, not by documentation: across 120 banks that carry FX
        objects of their own, offset 0 lands on an id the bank really owns
        720 times and offsets 1-5 do so 3, 0, 0, 0 and 0 times."""
        out = []
        for tag, _ds, data in _walk_segments(prog.body()):
            if tag != FX_TAG:
                continue
            fid = struct.unpack_from(">H", data, FX_ID_OFF)[0]
            if fid:
                out.append(fid)
        return out

    def other_by_id(self) -> dict[int, KrzObject]:
        """FX/Studio objects keyed by id, first occurrence winning.

        Keying by id ALONE, across every non-program/keymap/sample type, even
        though those types have separate id spaces and a 0x0F segment carries
        no type. Measured over 2237 banks: of 10 985 references that resolve
        in-bank, exactly **0** find a candidate in more than one type, so the
        ambiguity this could create does not occur in real material. 10 395
        name a type-113 object (mpc2emu's KRZ_FORMAT.md leaves 112-vs-113
        labelling open, and this settles which one an FX segment means); the
        remaining 590 name a 111/100/104 object with no 113 competing for the
        number, and carrying those is still better than dropping them."""
        out: dict[int, KrzObject] = {}
        for o in self.other_objects:
            out.setdefault(o.id, o)
        return out

    def keymap_sample_refs(self, km: KrzObject) -> list[int]:
        """Every (nonzero) sample id referenced by a Keymap: the default
        sampleId in its header plus every one of its entries, in encounter
        order (may repeat many times — most keys share a sample).

        A compacted keymap has no per-entry ids, so the header's own id is
        all there is; walking it at a fixed stride anyway would collect
        tuning bytes read as ids and pull unrelated samples into an
        assembled bank."""
        body = km.body()
        out = []
        default_sid = struct.unpack_from(">H", body, 0)[0]
        if default_sid:
            out.append(default_sid)
        lay = keymap_layout(body)
        if lay is None or lay.id_off is None:
            return out
        # EVERY velocity band, not just the softest. Walking only `table`
        # dropped the samples of bands 2..8 -- and since assemble() picks the
        # samples to write from this list, they were dropped from the built
        # bank while the keymap went on referencing them. 136 keymaps in this
        # library lose ids that way; one dual-layer bass program wants 10 samples and
        # was built with 5.
        for eo in lay.entry_offsets():
            if eo + 2 > len(body):
                continue
            sid = struct.unpack_from(">H", body, eo)[0]
            if sid:
                out.append(sid)
        return out

    def _sample_start(self, samp: KrzObject) -> int:
        """The earliest LOCAL-data header's sampleStart word position (0 if
        the object has no local-data header — a ROM-only reference)."""
        body = samp.body()
        num_headers = struct.unpack_from(">h", body, 2)[0] + 1
        starts = []
        for h in range(num_headers):
            ho = SAMPLE_HDR + h * SFH_SIZE
            if ho + SFH_SIZE > len(body):
                break
            if body[ho + 1] & 0x40:
                starts.append(struct.unpack_from(">i", body, ho + 8)[0])
        # Clamped at 0: a negative sampleStart would make assemble()'s
        # `src.pcm[old_start * 2:...]` slice from the END of the PCM buffer
        # and copy unrelated audio without any error. None of the 2237 banks
        # here carries one, but a plain slice turning a bad number into
        # plausible-looking audio is the wrong failure mode to leave open.
        return max(0, min(starts)) if starts else 0

    def _sample_exact_words(self, samp: KrzObject) -> int | None:
        """The SPAN covered by every ONE-SHOT (non-looped) local-data header
        — `max(sampleEnd) - min(sampleStart) + 1` — or None if the object has
        any looped local-data header (whose `sampleEnd` is the loop end, not
        necessarily the true PCM end — see `sample_word_extent`). A header
        without the 0x40 "data present" flag (device ROM reference) is
        skipped entirely (contributes neither words nor a None-forcing
        looped flag).

        A SPAN, not the sum of the per-header lengths it used to be. The two
        agree whenever a multi-header sample's planes sit back-to-back, which
        is 757 of the 802 multi-header samples here — but not for the other
        45, where the channels are laid out with a gap between them. The
        caller copies `num_words` from `_sample_start()`, which is
        `min(sampleStart)`, and `_rebias_sample_block()` then shifts every
        header by ONE delta; so anything short of the full span truncates the
        last plane by exactly the gap. Measured: a stereo sample with left at
        words 21 256..194 599 and right at 215 856..389 199 summed to 346 688
        and spans 367 944, so the right channel lost its final 21 256 words
        into whatever was appended next."""
        body = samp.body()
        num_headers = struct.unpack_from(">h", body, 2)[0] + 1
        lo = hi = None
        for h in range(num_headers):
            ho = SAMPLE_HDR + h * SFH_SIZE
            if ho + SFH_SIZE > len(body):
                break
            flags = body[ho + 1]
            if not (flags & 0x40):
                continue
            if not (flags & 0x80):   # 0x80 clear = looped -> sampleEnd unreliable
                return None
            st = struct.unpack_from(">i", body, ho + 8)[0]
            en = struct.unpack_from(">i", body, ho + 20)[0]
            lo = st if lo is None else min(lo, st)
            hi = en if hi is None else max(hi, en)
        return max(0, hi - lo + 1) if lo is not None else 0

    def _all_sample_lengths(self) -> dict:
        """{sample_id: num_words} for every sample in this file, cached.

        Two signals, neither reliable alone across real-world files:
          - One-shot headers: `sampleEnd - sampleStart + 1` is exact per
            KRZ_FORMAT.md and krz_writer.py's own `_write_sample_object`
            (`sample_end_field = abs_end` when not looped) — and matches
            ConvertWithMoss's independent `KurzweilSampleHeader.
            extractSampleData`, which doesn't distinguish loop status at
            all (see below for why that turned out to matter here).
          - Looped headers: krz_writer.py writes `sample_end_field =
            abs_loop_end` instead of the true PCM end for these — the loop
            point can legitimately sit before the sample's real tail. A
            real test file in this project's own corpus
            (`JRFX48.KRZ`) demonstrates exactly this: trusting `sampleEnd`
            unconditionally (i.e. doing what CWM's reader does) makes
            consecutive samples' declared word ranges overlap by one word.
            The only other available signal is the gap to the NEXT
            sample's own start position within the same file (mpc2emu's
            own writer lays samples out back-to-back with no gaps; a real
            hardware-saved file may not hold that invariant exactly, but
            it's the best fallback available).
        """
        if self._length_cache:
            return self._length_cache
        starts = {sid: self._sample_start(s) for sid, s in self.samples.items()}
        exact = {sid: self._sample_exact_words(s) for sid, s in self.samples.items()}
        by_start = sorted(self.samples.keys(), key=lambda sid: starts[sid])
        total_words = len(self.pcm) // 2
        lengths = {}
        for i, sid in enumerate(by_start):
            if exact[sid] is not None:
                lengths[sid] = exact[sid]
                continue
            next_start = starts[by_start[i + 1]] if i + 1 < len(by_start) else total_words
            n = next_start - starts[sid]
            # Never shorter than the sample's OWN headers say it reaches.
            # `n` is the gap to the next sample's start, which assumes the
            # samples are laid out one after another — false for a bank whose
            # stereo planes are INTERLEAVED, where the next start sits inside
            # this sample. Measured on a real ARP bank: every sample is
            # looped, so the exact path returns None for all of them, and a
            # stereo sample spanning words 243 584..420 377 got the gap to the
            # next start, 10 950 words of 176 794 -- its right channel
            # entirely absent and its left one cut to an eighth.
            #
            # The header span is a floor, not the answer: for a LOOPED header
            # `sampleEnd` is the loop end and the true tail can lie past it,
            # which is the whole reason the gap heuristic exists. Taking the
            # larger keeps that benefit and stops the interleaved case from
            # truncating. Over-copying only duplicates audio another sample
            # also carries; under-copying loses it.
            span = self._sample_exact_words_unconditional(self.samples[sid])
            lengths[sid] = max(n, span) if n > 0 else span
        self._length_cache = lengths
        return lengths

    def _sample_exact_words_unconditional(self, samp: KrzObject) -> int:
        """Last-resort fallback: the span `max(sampleEnd) - min(sampleStart)
        + 1` regardless of loop status (may truncate a looped sample's
        post-loop tail — see `_all_sample_lengths` — but never
        negative/crashing).

        A span for the same reason as `_sample_exact_words`: the caller
        copies from `min(sampleStart)` and rebiases every header by one
        delta, so summing per-header lengths loses the gap between
        non-contiguous planes."""
        body = samp.body()
        num_headers = struct.unpack_from(">h", body, 2)[0] + 1
        lo = hi = None
        for h in range(num_headers):
            ho = SAMPLE_HDR + h * SFH_SIZE
            if ho + SFH_SIZE > len(body):
                break
            if not (body[ho + 1] & 0x40):
                continue
            st = struct.unpack_from(">i", body, ho + 8)[0]
            en = struct.unpack_from(">i", body, ho + 20)[0]
            lo = st if lo is None else min(lo, st)
            hi = en if hi is None else max(hi, en)
        return max(0, hi - lo + 1) if lo is not None else 0

    def sample_word_extent(self, samp: KrzObject) -> tuple[int, int]:
        """(start_word, num_words) for a sample object — see
        `_all_sample_lengths` for how num_words is determined."""
        sid = samp.id
        start = self._sample_start(samp)
        lengths = self._all_sample_lengths()
        return start, lengths.get(sid, self._sample_exact_words_unconditional(samp))


# ── parsing ──────────────────────────────────────────────────────────────────

_FIRST_OBJECT = 32


def _split_bank_hint(pos: int) -> str:
    """Explain the overwhelmingly likely cause when a PRAM file has a valid
    header but nothing that parses as an object at all.

    A K2000 bank too big for one floppy is saved across several, and only
    the first disk carries the object table -- the rest are continuation
    data the K2000 joins on load, and they are not readable on their own.
    Measured across this project's library: 31 of 2268 KRZ blobs (1.4%)
    fail here, 21 of them sitting in a DISK2/DISK3/... folder and 22 having
    a same-named blob elsewhere that parses cleanly. Hedged wording,
    because a genuinely damaged file fails identically and nothing in the
    header distinguishes the two."""
    if pos != _FIRST_OBJECT:
        return ""
    return (" — nothing parses as an object here, so this is most likely one "
            "of several disks a single bank was split across; open the first "
            "disk of the set instead")


def parse_bytes(data: bytes, path: str = "<bytes>") -> KrzFile:
    if data[:4] != FILE_MAGIC:
        raise KrzFormatError(f"{path}: not a KRZ file (missing PRAM header)")
    if len(data) < 32:
        raise KrzFormatError(f"{path}: file too short for a KRZ header")
    osize = struct.unpack_from(">i", data, 4)[0]
    rest = struct.unpack_from(">iiiiii", data, 8)

    programs: dict[int, KrzObject] = {}
    keymaps: dict[int, KrzObject] = {}
    samples: dict[int, KrzObject] = {}
    other_objects: list[KrzObject] = []
    shadowed: list = []

    pos = 32
    while True:
        if pos + 4 > len(data):
            raise KrzFormatError(f"{path}: truncated — no end marker before EOF")
        blocksize = struct.unpack_from(">i", data, pos)[0]
        if blocksize == 0:
            break
        if blocksize >= 0:
            raise KrzFormatError(f"{path}: expected a negative blocksize at {pos}, "
                                  f"got {blocksize}{_split_bank_hint(pos)}")
        next_pos = pos - blocksize
        if next_pos <= pos or next_pos > len(data):
            raise KrzFormatError(f"{path}: bad blocksize at offset {pos} "
                                  f"(-> {next_pos}){_split_bank_hint(pos)}")
        if pos + 10 > next_pos:
            raise KrzFormatError(f"{path}: object at {pos} shorter than its fixed header")

        hash_val = struct.unpack_from(">H", data, pos + 4)[0]
        type_code, obj_id = _decode_hash(hash_val)

        name_start = pos + 10
        # The name field is MAX_NAME bytes; searching to the END OF THE BLOCK
        # for a terminator reads whatever field follows it whenever those 16
        # bytes are full. 486 objects here parsed a longer name that way, and
        # 6 picked up unprintable bytes with it -- 'General MIDI kit\x9d\xdb',
        # exactly the shape mpc2emu's KRZ_FORMAT.md warns about. _rename_block
        # already truncates to MAX_NAME when WRITING, so reading past it also
        # made a name that could not survive a round trip.
        name_limit = min(next_pos, name_start + MAX_NAME)
        try:
            name_end = data.index(b"\x00", name_start, name_limit)
        except ValueError:
            name_end = name_limit
        # latin-1, not ASCII: a real K2000 bank puts bytes above 0x7E in
        # here. Measured over 2 237 banks INCLUDING those inside disc
        # images -- 157 carry one, 0x7F alone appearing 4 036 times as a
        # separator ("BRA:Sect.3.01 <7F> L"). ASCII-with-replace turned
        # each into U+FFFD before anything could see it, the same fault
        # reported in mpc2emu's E4B parser. A loose-file-only scan found
        # zero and produced exactly the wrong conclusion.
        name = data[name_start:name_end].decode("latin-1")

        obj = KrzObject(type=type_code, id=obj_id, name=name, block=data[pos:next_pos])
        # A duplicate id overwrites, and the object that was there is gone
        # with no trace -- 3 in this library. Keep the FIRST, which is the one
        # every id reference in the file was written against, and record the
        # loss rather than pretending the bank held one object all along.
        table = {T_PROGRAM: programs, T_KEYMAP: keymaps, T_SAMPLE: samples}.get(type_code)
        if table is None:
            other_objects.append(obj)
        elif obj_id in table:
            shadowed.append((type_code, obj_id, obj.name))
        else:
            table[obj_id] = obj
        pos = next_pos

    if osize < pos or osize > len(data):
        raise KrzFormatError(f"{path}: header osize={osize} doesn't land inside the file "
                              f"(objects end at {pos}, file length {len(data)})")
    pcm = data[osize:]

    return KrzFile(path=path, rest=rest, osize=osize, programs=programs,
                    keymaps=keymaps, samples=samples, other_objects=other_objects,
                    pcm=pcm, shadowed_ids=tuple(shadowed))


def parse(path: str) -> KrzFile:
    with open(path, "rb") as f:
        data = f.read()
    return parse_bytes(data, path)


# ── assembly ─────────────────────────────────────────────────────────────────

def assemble(selections: list[tuple[KrzFile, KrzObject]],
             sample_names: dict | None = None) -> bytes:
    """Build a new KRZ file from selected (source_bank, program) pairs.

    Each selected Program pulls in the Keymaps its CAL segments reference,
    and each of those Keymaps pulls in the Samples its entries reference —
    the same Program/Keymap/Sample reference chain `write_krz` builds
    (KRZ_FORMAT.md §1). Every object's block bytes are copied verbatim
    (name and body untouched); only the hash (renumbered id), each CAL
    segment's keymap-id fields, each keymap entry's sample-id field, and
    each sample's four PCM word-offset fields (per Soundfilehead) are
    patched. Objects are renumbered into a single fresh id space starting
    at 200 (`base_id` in krz_writer.py), matching write_krz's own
    convention, with Samples numbered first (so their PCM offsets are
    known before Keymap/Program patching needs them), then Keymaps, then
    Programs. Samples are deduplicated by (name, exact content) across
    every source bank touched, same as banks/e4b.py.
    """
    if not selections:
        raise ValueError("no programs selected")
    if len(selections) > MAX_PRESETS:
        raise ValueError(f"too many programs: {len(selections)} > {MAX_PRESETS}")

    # Ids are allocated PER TYPE -- samples 200.., keymaps 200.., programs
    # 200.. -- because that is what the K2000 itself does: 1631 banks in this
    # library carry a sample, a keymap AND a program all numbered 200, against
    # 63 whose ranges merely happen not to overlap. A reference is typed by
    # where it sits (a keymap entry names a sample, a CAL slot names a
    # keymap), so nothing is ambiguous.
    #
    # This used to be ONE counter shared across all three types, which spent
    # the 824-id space three times over: 781 programs, well inside
    # MAX_PRESETS, produced 2829 objects whose ids wrapped past MAX_OBJECT_ID
    # into 0..1023, leaving 1536 program references pointing at keymaps that
    # no longer existed.
    base_id = 200
    # One lookup instead of the linear `next(s for s, _ in selections ...)`
    # scan this function used in four places; keys are id() of the source
    # bank, which is what every (src_bank, old_id) key here already carries.
    src_by_id = {id(s): s for s, _ in selections}

    # ── walk the reference graph: which keymaps, then which samples ────────
    # (src_bank_id, old_id) -> object, preserving first-encounter order
    keymap_order: list[tuple[int, int]] = []
    keymap_lookup: dict[tuple[int, int], KrzObject] = {}
    sample_order: list[tuple[int, int]] = []
    sample_lookup: dict[tuple[int, int], KrzObject] = {}
    prog_list: list[tuple[KrzFile, KrzObject]] = []

    for src, prog in selections:
        prog_list.append((src, prog))
        for kid in src.program_keymap_refs(prog):
            key = (id(src), kid)
            km = src.keymaps.get(kid)
            if km is None or key in keymap_lookup:
                continue
            keymap_lookup[key] = km
            keymap_order.append(key)

    for key in keymap_order:
        src = src_by_id[key[0]]
        km = keymap_lookup[key]
        for sid in src.keymap_sample_refs(km):
            skey = (id(src), sid)
            samp = src.samples.get(sid)
            if samp is None or skey in sample_lookup:
                continue
            sample_lookup[skey] = samp
            sample_order.append(skey)

    # ── dedupe samples by (name, header, audio); assign new ids 200.. ──────
    # Each sample's word extent (KrzFile.sample_word_extent — see that
    # method for why it isn't a simple per-object computation) is resolved
    # against its OWN source file, so samples from different sources can
    # be laid out in simple dedup-encounter order with a running cursor.
    sample_key_to_new_id: dict[tuple[int, int], int] = {}
    dedupe_key_to_new_id: dict[tuple, int] = {}
    patched_sample_blocks: list[bytes] = []
    sample_names_by_pos: list[str] = []
    pcm_pieces: list[bytes] = []
    cursor = 0

    for key in sample_order:
        samp = sample_lookup[key]
        src = src_by_id[key[0]]
        old_start, n_words = src.sample_word_extent(samp)
        piece = src.pcm[old_start * 2:(old_start + n_words) * 2] if n_words else b""

        # The key includes a digest of the AUDIO, not just the object header.
        # `KrzObject.block` is the header alone -- unlike banks/e4b.py, whose
        # `E4BSample.body` really is header+PCM, so the two are not the
        # equivalent keys this comment used to claim they were. Measured: 10
        # distinct (name, header) pairs in this library sit over genuinely
        # different audio, e.g. the same tenor-sax note in two volumes of one
        # sax set. Staging one program from each collapsed them into a single
        # sample and the second program played the first one's sound -- while
        # also inheriting its word extent, which is resolved per file by a gap
        # heuristic and need not agree.
        content_key = (samp.name, samp.block,
                       hashlib.blake2b(piece, digest_size=16).digest())
        new_id = dedupe_key_to_new_id.get(content_key)
        if new_id is None:
            new_id = base_id + len(patched_sample_blocks)
            dedupe_key_to_new_id[content_key] = new_id

            new_start = cursor
            got_words = len(piece) // 2
            if got_words < n_words:
                # The sample's declared extent runs past the end of its own
                # bank's PCM region, so the slice comes back short (or empty).
                # This is the multi-disk split bank _split_bank_hint() already
                # names: disk 1 carries the object table and the audio is on
                # the next volume, which is why the bank parses perfectly and
                # only the PCM is missing. 310 of 27 217 samples here are like
                # this.
                #
                # Refusing rather than writing it, for the same reason as a
                # ROM-only program: the result cannot play. Silently, this was
                # worse than one dud sample -- `cursor` advanced by the
                # DECLARED count while only the short piece was appended, so
                # every LATER sample's rebiased offset pointed into audio that
                # was never written, and one truncated sample corrupted all
                # the samples behind it.
                raise ValueError(
                    f"{samp.name!r} needs {n_words} words of audio from "
                    f"{str(src.path).split('/')[-1]} but only {got_words} are "
                    f"in the file -- this bank's sample data is not all here (a "
                    f"multi-disc set stores it on the next volume), so the "
                    f"programs using it cannot be rebuilt from this file "
                    f"alone")
            if piece:
                pcm_pieces.append(piece)
            cursor += got_words

            delta = new_start - old_start
            patched_sample_blocks.append(_rebias_sample_block(samp, delta))
            # Parallel to patched_sample_blocks: the ORIGINAL name of the
            # sample at each position, which is the key a rename is looked
            # up by. Recorded here rather than re-derived later because the
            # list is already deduped and reordered by this point.
            sample_names_by_pos.append(samp.name)
        sample_key_to_new_id[key] = new_id

    new_pcm = b"".join(pcm_pieces)

    # ── build new sample objects (renumbered hash) ──────────────────────────
    sample_objs: list[bytes] = []
    for i, block in enumerate(patched_sample_blocks):
        new_id = base_id + i
        repacked = _repack_block(block, T_SAMPLE, new_id)
        # AFTER the dedupe decision and after the PCM rebias, same ordering
        # rule as banks/e4b.py and banks/eiii.py: samples dedupe by (name,
        # content), so renaming earlier could merge two distinct samples or
        # split one that appears twice. Unlike those two, a rename here can
        # change the block's LENGTH -- see _rename_block for why that is safe
        # and for the blocksize trap it avoids.
        wanted = (sample_names or {}).get(sample_names_by_pos[i])
        if wanted is not None and wanted != sample_names_by_pos[i]:
            repacked = _rename_block(repacked, wanted)
        sample_objs.append(repacked)

    # ── build new keymap objects (renumbered hash + sample-id fields) ───────
    keymap_new_id: dict[tuple[int, int], int] = {}
    keymap_objs: list[bytes] = []
    for i, key in enumerate(keymap_order):
        new_id = base_id + i
        keymap_new_id[key] = new_id
        km = keymap_lookup[key]
        patched = _repatch_keymap_samples(km, key, sample_key_to_new_id,
                                          range(base_id, base_id + len(sample_objs)))
        keymap_objs.append(_repack_block(patched, T_KEYMAP, new_id))

    # ── carry the FX/Studio objects the programs name ───────────────────────
    # Nothing used to write these, so a bank that ships its own effects came
    # out with the effects deleted and the programs still naming them by id.
    # That is the one reference class where "absent means the machine
    # resolves it" is provably false: measured across this library, 3146
    # program FX segments in 168 banks name an FX object of their OWN bank,
    # and the program then loads whatever effect happens to sit at that
    # number on the machine.
    #
    # These types are not renumbered into the 200+ space -- their hash packs
    # the id into 8 bits (_encode_hash), a separate per-type space that
    # nothing else here allocates from -- so an id is kept as-is and moved
    # only when two source banks disagree about what lives at that number.
    fx_order: list[tuple[int, int]] = []
    fx_lookup: dict[tuple[int, int], KrzObject] = {}
    for src, prog in prog_list:
        owned = src.other_by_id()
        for fid in src.program_fx_refs(prog):
            key = (id(src), fid)
            obj = owned.get(fid)
            if obj is None or key in fx_lookup:
                continue
            fx_lookup[key] = obj
            fx_order.append(key)

    fx_new_id: dict[tuple[int, int], int] = {}
    fx_objs: list[bytes] = []
    fx_taken: set[tuple[int, int]] = set()          # (type, id) already written
    fx_by_content: dict[tuple, int] = {}
    for key in fx_order:
        obj = fx_lookup[key]
        content = (obj.type, obj.block)
        shared = fx_by_content.get(content)
        if shared is not None:
            fx_new_id[key] = shared
            continue
        nid = obj.id
        if (obj.type, nid) in fx_taken:
            # From 1: id 0 is "no effect" in a 0x0F segment, so an object
            # placed there is referenced by a field that reads as empty.
            nid = next((c for c in range(1, FX_MAX_ID + 1)
                        if (obj.type, c) not in fx_taken), None)
            if nid is None:
                continue                            # space full: leave it out
        fx_taken.add((obj.type, nid))
        fx_by_content[content] = nid
        fx_new_id[key] = nid
        fx_objs.append(_repack_block(obj.block, obj.type, nid))

    # ── build new program objects (renumbered hash + CAL keymap-id fields) ─
    program_objs: list[bytes] = []
    fx_written = {i for _t, i in fx_taken}
    for i, (src, prog) in enumerate(prog_list):
        new_id = base_id + i
        patched = _repatch_program_refs(
            prog, id(src), keymap_new_id,
            range(base_id, base_id + len(keymap_objs)),
            fx_new_id, fx_written)
        program_objs.append(_repack_block(patched, T_PROGRAM, new_id))

    room = MAX_OBJECT_ID - base_id + 1
    for kind, objs in (("sample", sample_objs), ("keymap", keymap_objs),
                       ("program", program_objs)):
        if len(objs) > room:
            raise ValueError(
                f"too many {kind}s for the KRZ id space: {len(objs)} needed, "
                f"ids run {base_id}..{MAX_OBJECT_ID} so {room} are available. "
                f"Build this in smaller banks: past the limit an id wraps, "
                f"references point at ids nothing owns, and two objects "
                f"collide onto one id.")

    preserve_from = selections[0][0]
    out = _build_file(preserve_from.rest, sample_objs, keymap_objs, program_objs,
                      fx_objs, new_pcm)

    # Self-check, and ONLY when a rename actually resized a block. This is the
    # single path in this module that changes a block's physical length, so it
    # is the single path where the object walk can be left inconsistent -- and
    # a wrong `blocksize` does not corrupt audio quietly, it makes the walk
    # land mid-block, which re-parsing catches at once. Costs a parse of a
    # file we just built, on a path the user explicitly asked for; the normal
    # assemble pays nothing.
    if sample_names:
        try:
            check = parse_bytes(out, "rename-selfcheck")
        except Exception as ex:
            raise ValueError(
                f"renaming produced a bank that cannot be read back "
                f"({ex}) -- refusing to hand it on") from ex
        if len(check.samples) != len(sample_objs):
            raise ValueError(
                f"renaming produced a bank with {len(check.samples)} of "
                f"{len(sample_objs)} sample(s) -- refusing to hand it on")
    return out


def _rebias_sample_block(samp: KrzObject, delta: int) -> bytes:
    """Shift every LOCAL-data Soundfilehead's 4 word-offset fields
    (sampleStart, altSampleStart, sampleLoopStart, sampleEnd — Soundfilehead
    offsets 8/12/16/20, KRZ_FORMAT.md §3.1) by `delta` words. A header
    without the 0x40 "data present" flag references device ROM and is left
    untouched (its offset fields don't address this file's PCM region at
    all). Handles multi-header (stereo) samples by rebiasing every local
    header identically, which preserves their relative offsets — mpc2emu's
    own writer only ever emits mono, so this path is unverified against a
    real stereo file."""
    block = bytearray(samp.block)
    body_start = samp.body_start()
    body = samp.body()
    num_headers = struct.unpack_from(">h", body, 2)[0] + 1
    for h in range(num_headers):
        hdr_off = body_start + SAMPLE_HDR + h * SFH_SIZE
        if hdr_off + SFH_SIZE > len(block):
            break
        if not (block[hdr_off + 1] & 0x40):
            continue
        for field_off in (8, 12, 16, 20):
            pos = hdr_off + field_off
            val = struct.unpack_from(">i", block, pos)[0]
            moved = val + delta
            if not (-0x80000000 <= moved <= 0x7FFFFFFF):
                raise ValueError(
                    f"{samp.name!r}: rebiasing word offset {val} by {delta} "
                    f"leaves the 32-bit field it lives in — this bank's sample "
                    f"offsets are not consistent with its PCM layout")
            struct.pack_into(">i", block, pos, moved)
    return bytes(block)


def _repatch_keymap_samples(km: KrzObject, src_key: tuple[int, int],
                             sample_key_to_new_id: dict, reserved: range) -> bytes:
    block = bytearray(km.block)
    body_start = km.body_start()
    src_id = src_key[0]

    def _remap(old_sid: int) -> int:
        """New id for a sample we carried over; the id UNCHANGED for one we
        did not.

        An id absent from the bank is a ROM id, and the K2000 resolves it
        against the machine. Returning 0 -- "no sample" -- silenced it.

        That is not a corner case. **433 banks in this library hold programs
        and ZERO sample objects**, one of them 229 programs; they are shipping
        products whose every reference is to ROM. mpc2emu reverse-engineered
        its KRZ writer against one of them (100 programs, no samples AND no
        keymaps). Under the old rule every program in all 433 would be
        silent.

        Measured on a real build before the fix: three programs of a
        30-program bank lost their reference to ROM sample 168.

        The counter-evidence that made this look settled the other way --
        mpc2emu's note about a bank measuring silent below key 60 -- turned
        out to be a bank referencing a ROM sample absent from THAT MACHINE's
        ROM. Silent because nothing resolved it, not because an out-of-bank
        id is invalid.

        EXCEPT inside `reserved` -- the id window THIS build is minting into.
        Passing an id through only works because nothing else claims it, and
        `assemble()` hands out 200, 201, ... to the samples it writes. An
        absent id landing in that window would name a real, unrelated sample
        of the new bank. Measured on two ordinary library banks: a drum
        keymap in the first points 55 of its keys at an absent sample 249,
        the build mints 50 samples ending at 249, and those 55 keys come out
        playing a tuned percussion sample from the SECOND bank. 173 of 4200
        ordered two-bank pairs in this library collide that way.

        Zero is the honest answer there and the only one available: the id
        cannot be preserved, since a real object of the output bank now owns
        it. Silent is a worse bank than correct and a better one than wrong
        -- and it is only ever reached for the ~2.5% of absent ids at >= 200,
        which are dangling references to another disk's user samples rather
        than ROM. Genuine ROM ids (97.8% of them, below 200, plus soundblock
        ids above the window) still pass through untouched.
        """
        new_sid = sample_key_to_new_id.get((src_id, old_sid))
        if new_sid is not None:
            return new_sid
        return old_sid if old_sid not in reserved else 0

    default_sid = struct.unpack_from(">H", block, body_start)[0]
    struct.pack_into(">H", block, body_start, _remap(default_sid))

    # Only a keymap that HAS per-entry sample ids gets its entries patched.
    # A compacted one carries none: its header id (already remapped above) is
    # the whole story, and writing ids at a fixed stride into it overwrites
    # tuning and subSample bytes instead -- measured on this project's own
    # library, that corrupted every one of 941 compacted-keymap programs.
    lay = keymap_layout(km.body())
    if lay is None or lay.id_off is None:
        return bytes(block)

    for rel in lay.entry_offsets():
        eo = body_start + rel
        if eo + 2 > len(block):
            break
        old_sid = struct.unpack_from(">H", block, eo)[0]
        struct.pack_into(">H", block, eo, _remap(old_sid))
    return bytes(block)


def _repatch_program_refs(prog: KrzObject, src_id: int, keymap_new_id: dict,
                          reserved: range, fx_new_id: dict,
                          fx_written: set) -> bytes:
    """Renumber a Program's outgoing references: keymap ids in its CAL
    segments, FX/Studio ids in its 0x0F segments.

    This used to open with `if tag != CAL_TAG: continue`, so tag 0x0F was
    never even read -- which is why the FX objects went unwritten and
    unnoticed for as long as they did."""
    block = bytearray(prog.block)
    body_start = prog.body_start()
    body = bytes(block[body_start:])
    for tag, data_start, data in _walk_segments(body):
        abs_data_start = body_start + data_start
        if tag == FX_TAG:
            old_fid = struct.unpack_from(">H", data, FX_ID_OFF)[0]
            if not old_fid:
                continue
            new_fid = fx_new_id.get((src_id, old_fid))
            if new_fid is None:
                # Not ours to carry: a ROM effect, kept verbatim unless this
                # build put one of its own objects on that number, in which
                # case the same rule as samples and keymaps applies -- zero,
                # because wrong is worse than absent.
                new_fid = old_fid if old_fid not in fx_written else 0
            struct.pack_into(">H", block, abs_data_start + FX_ID_OFF, new_fid)
            continue
        if tag != CAL_TAG:
            continue
        for off in (CAL_KEYMAP_OFF_1, CAL_KEYMAP_OFF_2):
            old_kid = struct.unpack_from(">H", data, off)[0]
            if not old_kid:
                continue
            # Same rule as sample ids, including the reserved window, and it
            # matters here too: a program can reference a ROM KEYMAP (the bank
            # the KRZ writer was built against has no keymap objects at all),
            # but keymaps are minted right after the samples, so the window
            # MOVES with the sample count -- whether a passed-through id
            # aliases depends on what else is staged. Measured on a real
            # pair: 31 samples then 24 keymaps are minted, window [231,255),
            # so an absent keymap 254 becomes a wind-instrument keymap of the
            # other bank and four pad programs sound it instead.
            new_kid = keymap_new_id.get((src_id, old_kid))
            if new_kid is None:
                new_kid = old_kid if old_kid not in reserved else 0
            if new_kid != old_kid:
                struct.pack_into(">H", block, abs_data_start + off, new_kid)
    return bytes(block)


def _repack_block(block: bytes, type_code: int, new_id: int) -> bytes:
    """Patch only the hash field (bytes[4:6]) — every other byte (including
    `blocksize`, since the block's own byte length never changes) is kept
    exactly as parsed."""
    out = bytearray(block)
    struct.pack_into(">H", out, 4, _encode_hash(type_code, new_id))
    return bytes(out)


def _build_file(rest: tuple, sample_objs: list[bytes], keymap_objs: list[bytes],
                 program_objs: list[bytes], fx_objs: list[bytes],
                 pcm: bytes) -> bytes:
    out = bytearray()
    out += FILE_MAGIC
    osize_pos = len(out)
    out += struct.pack(">i", 0)   # osize placeholder
    for v in rest:
        out += struct.pack(">i", v)
    for block in sample_objs:
        out += block
    for block in keymap_objs:
        out += block
    for block in program_objs:
        out += block
    # After the programs, which is where a real bank that ships its own
    # effects puts them: the observed object order of such a bank is 100
    # programs followed by its 10 FX objects, so nothing requires a
    # referenced object to precede its referrer.
    for block in fx_objs:
        out += block
    out += struct.pack(">i", 0)   # object-section end marker
    osize = len(out)
    struct.pack_into(">i", out, osize_pos, osize)
    out += pcm
    return bytes(out)


def _rename_block(block: bytes, new_name: str) -> bytes:
    """Return `block` with its name replaced, resizing the block if needed.

    The one place in this module where a block's physical length changes.
    Every other patch is in place, because a KRZ name slot has no slack --
    measured over 111 objects in 12 real banks, the median spare is ZERO
    bytes and 1 in 111 could hold a 16-character name. A longer name has to
    grow the block.

    Growing is LOCAL, which is what makes this tractable at all. Reviewed
    against mpc2emu 2026-08-08, with its code cited for each point:

    * nothing points into the object section by file offset -- objects
      reference each other by id, and a reader walks by `-blocksize`;
    * `osize` is recomputed from the assembled length by `_build_file()`,
      and the K2000 takes the PCM region purely from it;
    * a sample's PCM word offsets are indexes INTO that region
      (`start_byte = osize + 2 * start_word`), so when the object section
      grows, `osize` grows with it and every offset stays valid untouched.

    THE TRAP, and it is the reason this is not a two-line function: `size`
    is measured to the 2-byte-aligned end of the object, and only THEN is
    the block padded to a 4-byte boundary and `blocksize` computed from the
    padded length. `delta` is always even but not always a multiple of 4, so
    `size += delta` is right while `blocksize += delta` is wrong about half
    the time -- growing a name from 4 to 6 characters flips whether the
    block needs its two pad bytes. `blocksize` is therefore RECOMPUTED from
    the re-padded length, never adjusted by the delta.
    """
    old_ofs = struct.unpack_from(">H", block, 8)[0]
    data_start = 8 + old_ofs                      # object body begins here
    body = block[data_start:]

    # Name field is `name + NUL`, padded so the body starts on an even
    # offset -- `ofs` is even in every real block measured.
    # Capped at MAX_NAME even though the container could hold more -- this is
    # the one format here with no fixed name field, so nothing stops a longer
    # name physically. The corpus does: across 9 700 objects in real K2000
    # banks the longest authored name is exactly 16, which reads as the
    # format's ceiling rather than anyone's taste, and the K2000's own display
    # is 16 wide. Writing 34 produced a structurally valid file that no real
    # bank resembles -- and the rename dialog already warns at 16, so without
    # this the warning was simply untrue for KRZ.
    # latin-1, the inverse of the reader above. ASCII here would be the
    # very asymmetry reported in mpc2emu's E4B parser -- reading 0x7F
    # correctly and then writing "?" back out. `errors="replace"` still
    # earns its place on the encode side: a name typed in the dialog can
    # hold a codepoint above 0xFF, which latin-1 genuinely cannot carry.
    encoded = new_name.encode("latin-1", errors="replace")[:MAX_NAME]
    field_len = (len(encoded) + 1 + 1) // 2 * 2
    new_ofs = field_len + 2
    delta = new_ofs - old_ofs
    if delta == 0 and block[10:10 + field_len].split(b"\x00")[0] == encoded:
        return block                              # nothing to do

    head = bytearray(block[:10])
    name_field = encoded + b"\x00" * (field_len - len(encoded))

    # `size` counts to the 2-byte end, so it moves with the name by exactly
    # `delta`. Adjusted rather than recomputed: recomputing would need to
    # know where this block's body truly ends, and for a third-party block
    # that is not knowable from here -- the delta is exact either way.
    old_size = struct.unpack_from(">H", block, 6)[0]
    struct.pack_into(">H", head, 6, (old_size + delta) & 0xFFFF)
    struct.pack_into(">H", head, 8, new_ofs)

    out = bytearray(head) + name_field + body
    pad = (-len(out)) % 4                         # 4-byte boundary, then size it
    out += b"\x00" * pad
    struct.pack_into(">i", out, 0, -len(out))
    return bytes(out)
