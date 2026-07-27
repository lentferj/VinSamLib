"""
EIII bank container: parse and assemble at the raw-object level.

An E-mu Emulator IIIX / ESI-32/2000/4000 (`.e3x`/`.esi`, and the older,
read-only-here `.e3b` Emulator III) bank is a flat header + two fixed-offset
address tables (presets, samples) + a preset area + a sample area — see
``mpc2emu/docs/EIII_FORMAT.md`` and ``mpc2emu/writers/eiii_writer.py``
(the writer this project's own KRZ support, then this module, take as
their reference; mpc2emu also has a real EIII *reader* now,
``parsers/eiii_parser.py``, used for the vintage resample/reduce
conversion pipeline via ``models.common.Bank``). As with ``banks/e4b.py``
and ``banks/krz.py``, this module works directly on the on-disk bytes
rather than through that semantic model — **container-level surgery, not
parse-and-re-serialize** — because assembling a new bank from browsed
presets must preserve every parameter a real soundset carries byte-for-
byte, including anything neither this project's own RE nor mpc2emu's
`Bank` model covers, which a parse-and-rebuild through *any* semantic
model would silently lose. Same reasoning as ``banks/e4b.py``/
``banks/krz.py``, which keep their own container-level readers for the
identical reason even though mpc2emu has real parsers for both formats
now too.

All multi-byte values are **little-endian** — unlike E4B/KRZ (68k-derived,
big-endian), EIII/ESI hardware is not. Sample/preset positions are byte
offsets, not frame indices.

Bank variants (``docs/EIII_FORMAT.md`` "Bank variants"): the first 16 bytes
of a bank are a 15-char identifier + NUL. All three variants share the same
preset/note-zone/zone/sample byte layout and differ only in the position/
size of the two address tables and (EMULATOR_THREE only) an address bias.
Only EMULATOR_3X and ESI_32_V3 are `assemble()` write targets — EMULATOR_THREE
is read-only here, matching both ConvertWithMoss's and mpc2emu's own writer
scope decision (its compact, biased address tables were never a write
target for either).

Address tables (``EIII_FORMAT.md`` "Address tables"): each table holds one
entry per slot **plus one terminating entry**. A preset slot is empty when
its table entry equals its successor's (this is what deleting a preset on
the device leaves behind); a sample slot is empty when its entry is exactly
0. Neither is a simple terminator — occupied slots can follow a hole, so
the table must always be walked to its end. This module additionally reads
a slot's *physical byte extent* as the difference between its table entry
and the next non-empty one — the same "trust the structure, not an
embedded count" navigation ``banks/e4b.py``/``banks/krz.py`` already use
for E4B's voice/zone tables and KRZ's object blocks (a preset's own zone
*count* is only ever implicit in this extent, never stored directly).

Preset chains (``EIII_FORMAT.md`` "Preset", RESOLUTION_NOTES.md §EIII):
an on-disk EIII preset holds only ONE primary-layer note-zone table; the
sampler stacks further layers by `link`-chaining single-primary-layer
presets together (mpc2emu's writer does exactly this — one linked EIII
preset per `VoiceLayer`). A chain that isn't itself the *target* of some
other preset's `link` field is the head of a genuine, independently-
playable "preset" from a user's perspective — that's the granularity this
module exposes as one `EIIIPreset`: its `body` is the concatenation of
every segment in the chain (in link order), and `assemble()` re-chains the
copied segments' own `link` fields to match their new positions. A linked
*target* preset (referenced by some other preset's `link`) is never listed
on its own — same suppression `parsers/eiii_parser.py`'s `linked` set
applies.

Preset (142-byte header + note zones + zones — ``EIII_FORMAT.md`` "Preset"):
    [0:16]   name, 16-byte space-padded ASCII
    [0x31:0x33]  link, LE u16, 1-based index of the next chained preset, 0 = none
    [0x35]   number of note zones (uint8)
    [142:]   note zone table: n_note_zones * 4-byte entries
    [...]    zone table: (remaining segment bytes) / 48 — NOT a stored count,
             derived from the segment's own physical extent (see above)

Zone (48 bytes — ``EIII_FORMAT.md`` "Zone"):
    [1:3]    1-based sample number, LE u16; ESI sets bits 14/15 for unknown
             reasons (`ZONE_SAMPLE_INDEX_MASK` masks them off when reading
             the *value*, but assemble() preserves those bits verbatim when
             patching — only the low 14 bits it understands change)

Sample (92-byte header + 16-bit PCM — ``EIII_FORMAT.md`` "Sample"): copied
here as one opaque verbatim blob per sample (this module never decodes
PCM/loop points — those stay exactly as the source bank wrote them, same as
``banks/e4b.py``'s `E4BSample.body`). Unlike E4B, an EIII sample carries no
embedded "own index" field anywhere in its body — it's addressed purely by
its slot in the sample address table, so `assemble()` never needs to patch
one.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

NAME_LENGTH = 16
PRESET_SIZE = 142
NOTE_ZONE_SIZE = 4
ZONE_SIZE = 48
SAMPLE_HEADER_SIZE = 92
SAMPLE_ADDRESS_OFFSET = 0x400000

PRESET_LINK = 0x31                  # LE u16, 1-based index of next linked preset, 0 = none
PRESET_NUM_NOTE_ZONES = 0x35        # uint8

NOTE_ZONE_PRIMARY = 2               # byte offset within a 4-byte note zone entry
NOTE_ZONE_SECONDARY = 3
UNUSED = 0xFF                       # "no zone" marker in a note zone's primary/secondary field

ZONE_SAMPLE_INDEX = 1               # LE u16 at zone offset + 1
ZONE_SAMPLE_INDEX_MASK = 0x3FFF     # low 14 bits are the real sample number

BANK_NAME = 0x10
BANK_OBJECTS = 0x20
BANK_NEXT_PRESET = 0x30
BANK_NEXT_SAMPLE = 0x34
BANK_PRESET_BLOCKS = 0x3C
BANK_SAMPLE_BLOCKS = 0x40
BANK_TOTAL_BLOCKS = 0x48
BANK_SELECTED_PRESET = 0x5C

BLOCK_SIZE = 512
EMPTY_BANK_SIZE = 0x2B73
MAX_BANK_SIZE = 128 * 1024 * 1024   # docs/EIII_FORMAT.md "Device requirements when writing"


class EIIIFormatError(ValueError):
    pass


@dataclass(frozen=True)
class BankFormat:
    identifier: str             # 15 chars + NUL, as stored on disk
    file_ending: str
    sample_area_marker: int     # filler byte between the preset and sample areas
    preset_table_offset: int
    sample_table_offset: int
    preset_area_offset: int
    max_presets: int
    max_samples: int
    preset_address_bias: int = 0   # only EMULATOR_THREE biases its preset table


# Mirrors writers/eiii_writer.py's own BankFormat instances byte-for-byte
# (identifiers, table offsets, capacities) — kept as this module's own
# constants rather than imported, same as banks/e4b.py mirrors e4b_writer's
# tags/offsets instead of importing them, so parsing/browsing a bank never
# needs mpc2emu on sys.path (only assemble() reaches into the bridge, and
# only for the one opaque skeleton blob below).
EMULATOR_3X = BankFormat(
    identifier='EMULATOR 3X    ', file_ending='.e3x', sample_area_marker=0x74,
    preset_table_offset=0x17CA, sample_table_offset=0x1BD2, preset_area_offset=0x2B72,
    max_presets=256, max_samples=999)

ESI_32_V3 = BankFormat(
    identifier='EMU SI-32 v3   ', file_ending='.esi', sample_area_marker=0xEE,
    preset_table_offset=0x17CA, sample_table_offset=0x1BD2, preset_area_offset=0x2B72,
    max_presets=256, max_samples=999)

EMULATOR_THREE = BankFormat(
    identifier='EMULATOR THREE ', file_ending='.e3b', sample_area_marker=0x00,
    preset_table_offset=0x6C, sample_table_offset=0x204, preset_area_offset=0x74A,
    max_presets=100, max_samples=99, preset_address_bias=0x1A6FE)

ALL_BANK_FORMATS = (EMULATOR_3X, ESI_32_V3, EMULATOR_THREE)   # readable
WRITE_FORMATS = {"e3x": EMULATOR_3X, "esi": ESI_32_V3}         # assemble() targets — no EMULATOR_THREE


@dataclass
class EIIIPreset:
    index: int                          # 0-based slot index of the chain's HEAD in the source preset table
    name: str
    body: bytes                          # every linked segment's bytes, concatenated in chain order
    segment_lengths: list[int]           # each segment's byte length within body, in chain order
    zone_refs: list[tuple[int, int]] = field(default_factory=list)
    # (absolute offset of a zone's 2-byte sample-index field within `body`, its current raw LE value incl. any ESI flag bits)

    @property
    def sample_indices(self) -> list[int]:
        return sorted({raw & ZONE_SAMPLE_INDEX_MASK for _off, raw in self.zone_refs
                        if raw & ZONE_SAMPLE_INDEX_MASK})

    @property
    def num_links(self) -> int:
        return len(self.segment_lengths)


@dataclass
class EIIISample:
    index: int              # 1-based, as currently embedded in the sample table
    name: str
    body: bytes               # verbatim 92-byte header + PCM

    @property
    def size(self) -> int:
        return len(self.body)


@dataclass
class EIIIFile:
    path: str
    format: BankFormat
    name: str
    presets: list[EIIIPreset]
    samples: dict[int, EIIISample]   # keyed by (original) 1-based sample number
    warnings: list[str] = field(default_factory=list)


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _put_u16(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<H", data, offset, value & 0xFFFF)


def _put_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value & 0xFFFFFFFF)


def _decode_name(data: bytes, offset: int) -> str:
    if offset + NAME_LENGTH > len(data):
        return ""
    return data[offset:offset + NAME_LENGTH].rstrip(b"\x00 ").decode("latin-1", "replace")


def detect_format(data: bytes) -> BankFormat | None:
    if len(data) < 16 or data[15] != 0:
        return None
    text = data[0:15].decode("ascii", errors="replace")
    for fmt in ALL_BANK_FORMATS:
        if fmt.identifier == text:
            return fmt
    return None


# ── parsing ──────────────────────────────────────────────────────────────────

def _extent_map(entries: list[int], present) -> dict[int, int]:
    """Maps each PRESENT table entry value to its physical byte extent: the
    gap to the next larger DISTINCT entry value among all present slots —
    not the value of the next INDEX's slot. A real hardware-edited bank
    doesn't necessarily keep table-index order and physical/address order
    in sync (e.g. after an insert following an earlier delete), so sizing
    slot i purely from `table[i+1] - table[i]` occasionally walks into an
    unrelated LATER preset's/sample's bytes and decodes them as if they
    were more of slot i's own zones/PCM — seen in real commercial banks in
    this project's own corpus as implausible, scattered extra zone/sample
    references. Deriving the extent from the sorted set of all present
    slots' own addresses is robust to that: it only assumes what the format
    actually guarantees (no gap between two physically-adjacent live
    entries), not that logical and physical order match.

    `entries` is the full table including its terminating entry;
    `present(i)` tests whether slot i (0 <= i < len(entries) - 1) is
    occupied."""
    starts = sorted({entries[i] for i in range(len(entries) - 1) if present(i)})
    starts.append(entries[-1])   # terminator, always a valid right boundary
    extent: dict[int, int] = {}
    for k in range(len(starts) - 1):
        length = starts[k + 1] - starts[k]
        if length > 0:
            extent.setdefault(starts[k], length)
    return extent


def _preset_segment(data: bytes, fmt: BankFormat, preset_table: list[int],
                     preset_extent: dict[int, int], i: int):
    """(offset, length, body) for the physical preset segment at table slot
    `i`, or None if the slot is empty (its entry equals its successor's —
    ``EIII_FORMAT.md``'s documented per-slot empty test) or its extent
    (see `_extent_map`) doesn't land inside the file."""
    if preset_table[i] == preset_table[i + 1]:
        return None
    length = preset_extent.get(preset_table[i])
    if not length or length < PRESET_SIZE:
        return None
    offset = fmt.preset_area_offset + preset_table[i] - fmt.preset_address_bias
    if offset < 0 or offset + length > len(data):
        return None
    return offset, length, data[offset:offset + length]


def _zone_refs_in_chain(chain_bodies: list[bytes]) -> list[tuple[int, int]]:
    """Every zone's (absolute offset within the CONCATENATED chain body,
    raw LE sample-index field value) that a segment's own note-zone table
    actually references (primary or secondary slot) — NOT every zone slot
    physically present in the segment's zone table.

    A zone table's length is never stored directly (see the module
    docstring: it's whatever's left after the note-zone table, divided by
    ZONE_SIZE), and that physical extent can genuinely be larger than the
    preset's live zone count: editing a preset down on the device doesn't
    necessarily clear the tail of its own zone table, leaving stale/
    orphaned entries with leftover garbage sample-index bytes from an
    earlier, bigger version of the same preset slot — confirmed against
    real commercial banks in this project's own corpus, where trusting
    every physical zone slot picked up implausible sample numbers no
    genuine zone referenced. The note-zone table is the only thing that
    says which zone indices are actually live, so this walks it the same
    way mpc2emu's own ``parsers/eiii_parser.py`` `_parse_layers` does,
    rather than the raw zone table directly."""
    refs: list[tuple[int, int]] = []
    cumulative = 0
    for body in chain_bodies:
        if len(body) >= PRESET_SIZE + 1:
            num_note_zones = body[PRESET_NUM_NOTE_ZONES]
            note_zone_off = PRESET_SIZE
            zone_table_off = note_zone_off + num_note_zones * NOTE_ZONE_SIZE
            n_zones_avail = max(0, (len(body) - zone_table_off) // ZONE_SIZE)
            seen: set[int] = set()
            for nzi in range(num_note_zones):
                nz_off = note_zone_off + nzi * NOTE_ZONE_SIZE
                if nz_off + NOTE_ZONE_SIZE > len(body):
                    break
                for field_off in (NOTE_ZONE_PRIMARY, NOTE_ZONE_SECONDARY):
                    zone_idx = body[nz_off + field_off]
                    if zone_idx == UNUSED or zone_idx in seen or zone_idx >= n_zones_avail:
                        continue
                    seen.add(zone_idx)
                    zo = zone_table_off + zone_idx * ZONE_SIZE
                    if zo + ZONE_SAMPLE_INDEX + 2 > len(body):
                        continue
                    raw = _u16(body, zo + ZONE_SAMPLE_INDEX)
                    if raw & ZONE_SAMPLE_INDEX_MASK:   # 0 = zone unused, nothing to track/patch
                        refs.append((cumulative + zo + ZONE_SAMPLE_INDEX, raw))
        cumulative += len(body)
    return refs


def parse_bytes(data: bytes, path: str = "<bytes>") -> EIIIFile:
    fmt = detect_format(data)
    if fmt is None:
        raise EIIIFormatError(f"{path}: not an EIII bank (unrecognised 16-byte identifier)")

    warnings: list[str] = []
    bank_name = _decode_name(data, BANK_NAME)

    n_preset_entries = fmt.max_presets + 1
    preset_table_end = fmt.preset_table_offset + n_preset_entries * 4
    if preset_table_end > len(data):
        raise EIIIFormatError(f"{path}: truncated (preset address table runs past EOF)")
    preset_table = [_u32(data, fmt.preset_table_offset + i * 4) for i in range(n_preset_entries)]

    n_sample_entries = fmt.max_samples + 1
    sample_table_end = fmt.sample_table_offset + n_sample_entries * 4
    if sample_table_end > len(data):
        raise EIIIFormatError(f"{path}: truncated (sample address table runs past EOF)")
    sample_table = [_u32(data, fmt.sample_table_offset + i * 4) for i in range(n_sample_entries)]

    # sampleArea = presetArea + 1(filler byte) + presetTable[maxPresets] - bias
    # (EIII_FORMAT.md "Address tables" — the terminating preset-table entry
    # doubles as the used preset area's total size).
    preset_area_size = preset_table[fmt.max_presets] - fmt.preset_address_bias
    sample_area_start = fmt.preset_area_offset + 1 + preset_area_size

    preset_extent = _extent_map(preset_table, lambda i: preset_table[i] != preset_table[i + 1])
    sample_extent = _extent_map(sample_table, lambda i: sample_table[i] != 0)

    samples: dict[int, EIIISample] = {}
    for i in range(fmt.max_samples):
        entry = sample_table[i]
        if entry == 0:
            continue   # deleted/empty slot — not a terminator, keep scanning
        length = sample_extent.get(entry)
        address = sample_area_start + entry - SAMPLE_ADDRESS_OFFSET
        if not length or address < 0 or address + length > len(data):
            warnings.append(f"sample slot {i + 1}: computed extent runs past EOF, skipping")
            continue
        body = data[address:address + length]
        samples[i + 1] = EIIISample(index=i + 1, name=_decode_name(body, 0), body=body)

    # A preset that's the LINK TARGET of another preset is never its own
    # top-level browsable item — see the module docstring's "Preset chains".
    linked: set[int] = set()
    for i in range(fmt.max_presets):
        seg = _preset_segment(data, fmt, preset_table, preset_extent, i)
        if seg is None:
            continue
        _off, _len, body = seg
        link = _u16(body, PRESET_LINK)
        if 0 < link <= fmt.max_presets and (link - 1) != i:
            linked.add(link - 1)

    presets: list[EIIIPreset] = []
    for i in range(fmt.max_presets):
        if i in linked:
            continue
        if _preset_segment(data, fmt, preset_table, preset_extent, i) is None:
            continue
        chain_bodies: list[bytes] = []
        visited: set[int] = set()
        idx: int | None = i
        name = ""
        while idx is not None and idx not in visited and 0 <= idx < fmt.max_presets:
            visited.add(idx)
            seg = _preset_segment(data, fmt, preset_table, preset_extent, idx)
            if seg is None:
                break
            _off, _len, body = seg
            if not chain_bodies:
                name = _decode_name(body, 0)
            chain_bodies.append(body)
            link = _u16(body, PRESET_LINK)
            idx = (link - 1) if (0 < link <= fmt.max_presets and link - 1 != idx) else None
        if not chain_bodies:
            continue
        presets.append(EIIIPreset(
            index=i, name=name, body=b"".join(chain_bodies),
            segment_lengths=[len(b) for b in chain_bodies],
            zone_refs=_zone_refs_in_chain(chain_bodies)))

    return EIIIFile(path=path, format=fmt, name=bank_name, presets=presets,
                     samples=samples, warnings=warnings)


def parse(path: str) -> EIIIFile:
    with open(path, "rb") as f:
        data = f.read()
    return parse_bytes(data, path)


# ── assembly ─────────────────────────────────────────────────────────────────

def assemble(selections: list[tuple[EIIIFile, EIIIPreset]], variant: str = "e3x",
             bank_name: str | None = None) -> bytes:
    """Build a new EIII bank from selected (source_bank, preset) pairs.

    Each preset's every linked segment is copied verbatim; only each
    segment's own `link` field (re-chained to the segment's new position)
    and each zone's 2-byte sample-index field (renumbered, ESI flag bits
    preserved — only the low 14 bits `ZONE_SAMPLE_INDEX_MASK` understands
    change) are patched, following ``banks/e4b.py``'s exact technique.
    Samples are deduplicated by (name, exact content) across every source
    bank touched, same as e4b.py/krz.py.

    `variant`: 'e3x' (EMULATOR_3X, default — also what the E4XT's backward-
    compatibility loader reads) or 'esi' (ESI_32_V3). EMULATOR_THREE is
    never a write target here (see WRITE_FORMATS / the module docstring).

    `bank_name`: written into the new bank's own internal name field
    (offset `BANK_NAME` — unlike E4B/KRZ, whose only "name" a real device
    ever shows is the filename, EIII stores one on disk). Defaults to the
    first selected preset's source bank's own name when not given.
    """
    if not selections:
        raise ValueError("no presets selected")
    fmt = WRITE_FORMATS.get(variant)
    if fmt is None:
        raise ValueError(f"unknown EIII write variant {variant!r}, expected one of {sorted(WRITE_FORMATS)}")

    if bank_name is None:
        bank_name = selections[0][0].name or "NewBank"

    new_sample_bodies: list[bytes] = []
    new_sample_names: list[str] = []
    dedupe_key_to_new_idx: dict[tuple, int] = {}
    flat_segments: list[bytes] = []

    for src, preset in selections:
        base = len(flat_segments)
        n_segs = len(preset.segment_lengths)
        seg_start = 0
        patched: list[bytes] = []
        for si, seg_len in enumerate(preset.segment_lengths):
            seg_bytes = bytearray(preset.body[seg_start:seg_start + seg_len])
            for off, old_raw in preset.zone_refs:
                if not (seg_start <= off < seg_start + seg_len):
                    continue
                old_idx = old_raw & ZONE_SAMPLE_INDEX_MASK
                samp = src.samples.get(old_idx)
                if samp is None:
                    continue   # dangling reference (deleted/missing sample); leave as-is
                key = (samp.name, samp.body)
                new_idx = dedupe_key_to_new_idx.get(key)
                if new_idx is None:
                    if len(new_sample_bodies) >= fmt.max_samples:
                        raise ValueError(f"too many distinct samples: > {fmt.max_samples}")
                    new_idx = len(new_sample_bodies) + 1
                    new_sample_bodies.append(samp.body)
                    new_sample_names.append(samp.name)
                    dedupe_key_to_new_idx[key] = new_idx
                new_raw = (old_raw & ~ZONE_SAMPLE_INDEX_MASK) | (new_idx & ZONE_SAMPLE_INDEX_MASK)
                _put_u16(seg_bytes, off - seg_start, new_raw)
            is_last = (si == n_segs - 1)
            _put_u16(seg_bytes, PRESET_LINK, 0 if is_last else (base + si + 2))
            patched.append(bytes(seg_bytes))
            seg_start += seg_len
        flat_segments.extend(patched)
        if len(flat_segments) > fmt.max_presets:
            raise ValueError(f"too many presets: {len(flat_segments)} physical preset "
                              f"slot(s) > {fmt.max_presets} ({fmt.file_ending} limit)")

    return _build_bank(fmt, bank_name, flat_segments, new_sample_bodies)


def _build_bank(fmt: BankFormat, bank_name: str, preset_segments: list[bytes],
                 sample_bodies: list[bytes]) -> bytes:
    # The empty-bank skeleton (header + address-table placeholders + the
    # device master-settings block a real E4XT/EIII sampler expects on
    # load) is ~11 KB of otherwise-undocumented magic bytes — reused from
    # mpc2emu's own writer via the bridge rather than re-derived here, same
    # as banks/e4b.py falls back to e4b_writer._build_e4ma()/_build_emst()
    # for its own opaque bank-wide blobs. This is assemble()'s only
    # dependency on mpc2emu being on disk — parse_bytes() above needs none.
    from ..mpc2emu_bridge import eiii_writer
    skeleton = eiii_writer._create_empty_bank(eiii_writer.BANK_FORMATS[
        "e3x" if fmt is EMULATOR_3X else "esi"], bank_name)

    preset_area_size = sum(len(b) for b in preset_segments)
    sample_area_size = sum(len(b) for b in sample_bodies)
    size = EMPTY_BANK_SIZE + preset_area_size + 1 + sample_area_size
    if size > MAX_BANK_SIZE:
        raise ValueError(f"assembled bank too large: {size} > {MAX_BANK_SIZE} bytes")

    out = bytearray(size)
    out[0:EMPTY_BANK_SIZE] = bytes(skeleton[:EMPTY_BANK_SIZE])

    offset = fmt.preset_area_offset
    for i, seg in enumerate(preset_segments):
        _put_u32(out, fmt.preset_table_offset + i * 4, offset - fmt.preset_area_offset)
        out[offset:offset + len(seg)] = seg
        offset += len(seg)
    for i in range(len(preset_segments), fmt.max_presets + 1):
        _put_u32(out, fmt.preset_table_offset + i * 4, preset_area_size)

    out[offset] = fmt.sample_area_marker
    offset += 1

    sample_area_offset = offset
    for i, body in enumerate(sample_bodies):
        _put_u32(out, fmt.sample_table_offset + i * 4,
                  offset - sample_area_offset + SAMPLE_ADDRESS_OFFSET)
        out[offset:offset + len(body)] = body
        offset += len(body)
    _put_u32(out, fmt.sample_table_offset + fmt.max_samples * 4,
              offset - sample_area_offset + SAMPLE_ADDRESS_OFFSET)

    _put_u32(out, BANK_OBJECTS, len(preset_segments) + len(sample_bodies))
    _put_u32(out, BANK_NEXT_PRESET, _u32(out, BANK_NEXT_PRESET) + preset_area_size)
    _put_u32(out, BANK_NEXT_SAMPLE, offset - sample_area_offset)
    _put_u32(out, BANK_SELECTED_PRESET, 0)

    preset_blocks = (sample_area_offset - 1 + BLOCK_SIZE - 1) // BLOCK_SIZE
    total_blocks = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
    _put_u32(out, BANK_PRESET_BLOCKS, preset_blocks)
    _put_u32(out, BANK_SAMPLE_BLOCKS, total_blocks - preset_blocks)
    _put_u32(out, BANK_TOTAL_BLOCKS, total_blocks)

    return bytes(out)
