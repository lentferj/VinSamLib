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

import struct
from dataclasses import dataclass, field

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

#: The K2000 sounds keymap entry `i` at MIDI key `i + 12`, so entries cover
#: keys 12..139 and keys 0..11 cannot be addressed. Hardware-confirmed by
#: mpc2emu 2026-08-02 (`791364a`); its corpus-only reading before that, like
#: this project's, had entry `i` at key `i` and was wrong.
KEYMAP_ENTRY_NOTE_OFFSET = 12

SAMPLE_HDR = 12
SFH_SIZE = 32
ENV_SIZE = 12

CAL_TAG = 0x40
CAL_KEYMAP_OFF_1 = 7    # 2 bytes, BE u16 — primary keymap slot
CAL_KEYMAP_OFF_2 = 11   # 2 bytes, BE u16 — secondary keymap slot ("CAL[7,8] is a 2nd keymap slot")

MAX_PRESETS = 1000      # K2000 hardware limit on user object ids per type (id space 200-999-ish)


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
    return KeymapLayout(header_sid=header_sid & 0xFFFF, method=method,
                        num_keys=entries_per_vel + 1, table=table,
                        stride=stride, id_off=id_off)


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
        yield tag, data_start, body[data_start:data_start + length]
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
        for k in range(lay.num_keys):
            eo = lay.table + k * lay.stride + lay.id_off
            if eo + 2 > len(body):
                break
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
        return min(starts) if starts else 0

    def _sample_exact_words(self, samp: KrzObject) -> int | None:
        """Sum of every ONE-SHOT (non-looped) local-data header's exact
        word count (`sampleEnd - sampleStart + 1`), or None if the object
        has any looped local-data header (whose `sampleEnd` is the loop
        end, not necessarily the true PCM end — see `sample_word_extent`).
        A header without the 0x40 "data present" flag (device ROM
        reference) is skipped entirely (contributes neither words nor a
        None-forcing looped flag)."""
        body = samp.body()
        num_headers = struct.unpack_from(">h", body, 2)[0] + 1
        total = 0
        any_header = False
        for h in range(num_headers):
            ho = SAMPLE_HDR + h * SFH_SIZE
            if ho + SFH_SIZE > len(body):
                break
            flags = body[ho + 1]
            if not (flags & 0x40):
                continue
            any_header = True
            if not (flags & 0x80):   # 0x80 clear = looped -> sampleEnd unreliable
                return None
            s = struct.unpack_from(">i", body, ho + 8)[0]
            e = struct.unpack_from(">i", body, ho + 20)[0]
            total += max(0, e - s + 1)
        return total if any_header else 0

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
            # Negative/zero means this file's samples aren't laid out
            # sequentially (seen in some real hardware-saved banks) — fall
            # back to the (possibly loop-truncated, but at least
            # non-negative) exact-style computation rather than producing
            # a nonsensical span.
            lengths[sid] = n if n > 0 else self._sample_exact_words_unconditional(self.samples[sid])
        self._length_cache = lengths
        return lengths

    def _sample_exact_words_unconditional(self, samp: KrzObject) -> int:
        """Last-resort fallback: `sampleEnd - sampleStart + 1` regardless
        of loop status (may truncate a looped sample's post-loop tail —
        see `_all_sample_lengths` — but never negative/crashing)."""
        body = samp.body()
        num_headers = struct.unpack_from(">h", body, 2)[0] + 1
        total = 0
        for h in range(num_headers):
            ho = SAMPLE_HDR + h * SFH_SIZE
            if ho + SFH_SIZE > len(body):
                break
            if not (body[ho + 1] & 0x40):
                continue
            s = struct.unpack_from(">i", body, ho + 8)[0]
            e = struct.unpack_from(">i", body, ho + 20)[0]
            total += max(0, e - s + 1)
        return total

    def sample_word_extent(self, samp: KrzObject) -> tuple[int, int]:
        """(start_word, num_words) for a sample object — see
        `_all_sample_lengths` for how num_words is determined."""
        sid = samp.id
        start = self._sample_start(samp)
        lengths = self._all_sample_lengths()
        return start, lengths.get(sid, self._sample_exact_words_unconditional(samp))


# ── parsing ──────────────────────────────────────────────────────────────────

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

    pos = 32
    while True:
        if pos + 4 > len(data):
            raise KrzFormatError(f"{path}: truncated — no end marker before EOF")
        blocksize = struct.unpack_from(">i", data, pos)[0]
        if blocksize == 0:
            break
        if blocksize >= 0:
            raise KrzFormatError(f"{path}: expected a negative blocksize at {pos}, got {blocksize}")
        next_pos = pos - blocksize
        if next_pos <= pos or next_pos > len(data):
            raise KrzFormatError(f"{path}: bad blocksize at offset {pos} (-> {next_pos})")
        if pos + 10 > next_pos:
            raise KrzFormatError(f"{path}: object at {pos} shorter than its fixed header")

        hash_val = struct.unpack_from(">H", data, pos + 4)[0]
        type_code, obj_id = _decode_hash(hash_val)

        name_start = pos + 10
        try:
            name_end = data.index(b"\x00", name_start, next_pos)
        except ValueError:
            name_end = next_pos
        name = data[name_start:name_end].decode("ascii", "replace")

        obj = KrzObject(type=type_code, id=obj_id, name=name, block=data[pos:next_pos])
        if type_code == T_PROGRAM:
            programs[obj_id] = obj
        elif type_code == T_KEYMAP:
            keymaps[obj_id] = obj
        elif type_code == T_SAMPLE:
            samples[obj_id] = obj
        else:
            other_objects.append(obj)
        pos = next_pos

    if osize < pos or osize > len(data):
        raise KrzFormatError(f"{path}: header osize={osize} doesn't land inside the file "
                              f"(objects end at {pos}, file length {len(data)})")
    pcm = data[osize:]

    return KrzFile(path=path, rest=rest, osize=osize, programs=programs,
                    keymaps=keymaps, samples=samples, other_objects=other_objects, pcm=pcm)


def parse(path: str) -> KrzFile:
    with open(path, "rb") as f:
        data = f.read()
    return parse_bytes(data, path)


# ── assembly ─────────────────────────────────────────────────────────────────

def assemble(selections: list[tuple[KrzFile, KrzObject]]) -> bytes:
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

    base_id = 200

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
        src = next(s for s, _ in selections if id(s) == key[0])
        km = keymap_lookup[key]
        for sid in src.keymap_sample_refs(km):
            skey = (id(src), sid)
            samp = src.samples.get(sid)
            if samp is None or skey in sample_lookup:
                continue
            sample_lookup[skey] = samp
            sample_order.append(skey)

    # ── dedupe samples by (name, content); assign new ids 200.. ─────────────
    # Each sample's word extent (KrzFile.sample_word_extent — see that
    # method for why it isn't a simple per-object computation) is resolved
    # against its OWN source file, so samples from different sources can
    # be laid out in simple dedup-encounter order with a running cursor.
    sample_key_to_new_id: dict[tuple[int, int], int] = {}
    dedupe_key_to_new_id: dict[tuple, int] = {}
    patched_sample_blocks: list[bytes] = []
    pcm_pieces: list[bytes] = []
    cursor = 0

    for key in sample_order:
        samp = sample_lookup[key]
        content_key = (samp.name, samp.block)
        new_id = dedupe_key_to_new_id.get(content_key)
        if new_id is None:
            src = next(s for s, _ in selections if id(s) == key[0])
            old_start, n_words = src.sample_word_extent(samp)

            new_id = base_id + len(patched_sample_blocks)
            dedupe_key_to_new_id[content_key] = new_id

            new_start = cursor
            if n_words:
                pcm_pieces.append(src.pcm[old_start * 2:(old_start + n_words) * 2])
            cursor += n_words

            delta = new_start - old_start
            patched_sample_blocks.append(_rebias_sample_block(samp, delta))
        sample_key_to_new_id[key] = new_id

    new_pcm = b"".join(pcm_pieces)

    # ── build new sample objects (renumbered hash) ──────────────────────────
    sample_objs: list[bytes] = []
    for i, block in enumerate(patched_sample_blocks):
        new_id = base_id + i
        sample_objs.append(_repack_block(block, T_SAMPLE, new_id))

    # ── build new keymap objects (renumbered hash + sample-id fields) ───────
    keymap_new_id: dict[tuple[int, int], int] = {}
    keymap_objs: list[bytes] = []
    for i, key in enumerate(keymap_order):
        new_id = base_id + len(sample_objs) + i
        keymap_new_id[key] = new_id
        src = next(s for s, _ in selections if id(s) == key[0])
        km = keymap_lookup[key]
        patched = _repatch_keymap_samples(km, key, sample_key_to_new_id, src)
        keymap_objs.append(_repack_block(patched, T_KEYMAP, new_id))

    # ── build new program objects (renumbered hash + CAL keymap-id fields) ─
    program_objs: list[bytes] = []
    for i, (src, prog) in enumerate(prog_list):
        new_id = base_id + len(sample_objs) + len(keymap_objs) + i
        patched = _repatch_program_keymaps(prog, id(src), keymap_new_id)
        program_objs.append(_repack_block(patched, T_PROGRAM, new_id))

    preserve_from = selections[0][0]
    return _build_file(preserve_from.rest, sample_objs, keymap_objs, program_objs, new_pcm)


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
            struct.pack_into(">i", block, pos, val + delta)
    return bytes(block)


def _repatch_keymap_samples(km: KrzObject, src_key: tuple[int, int],
                             sample_key_to_new_id: dict, src: "KrzFile") -> bytes:
    block = bytearray(km.block)
    body_start = km.body_start()
    src_id = src_key[0]

    def _remap(old_sid: int) -> int:
        if not old_sid:
            return 0
        return sample_key_to_new_id.get((src_id, old_sid), 0)

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

    for k in range(lay.num_keys):
        eo = body_start + lay.table + k * lay.stride + lay.id_off
        if eo + 2 > len(block):
            break
        old_sid = struct.unpack_from(">H", block, eo)[0]
        struct.pack_into(">H", block, eo, _remap(old_sid))
    return bytes(block)


def _repatch_program_keymaps(prog: KrzObject, src_id: int, keymap_new_id: dict) -> bytes:
    block = bytearray(prog.block)
    body_start = prog.body_start()
    body = bytes(block[body_start:])
    for tag, data_start, data in _walk_segments(body):
        if tag != CAL_TAG:
            continue
        abs_data_start = body_start + data_start
        for off in (CAL_KEYMAP_OFF_1, CAL_KEYMAP_OFF_2):
            old_kid = struct.unpack_from(">H", data, off)[0]
            if not old_kid:
                continue
            new_kid = keymap_new_id.get((src_id, old_kid), 0)
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
                 program_objs: list[bytes], pcm: bytes) -> bytes:
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
    out += struct.pack(">i", 0)   # object-section end marker
    osize = len(out)
    struct.pack_into(">i", out, osize_pos, osize)
    out += pcm
    return bytes(out)
