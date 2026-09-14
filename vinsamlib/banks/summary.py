"""
Plain-data summaries of a bank/preset for the UI (Detail pane, Samples pane).

No Qt, no printing/ANSI — just dataclasses the UI renders directly. Two very
different strategies per format, because only some of them have an existing
semantic reader to lean on:

- **E4B** and **EIII**: neither `banks/e4b.py`'s nor `banks/eiii.py`'s own
  container objects carry zone semantics (key/vel range, root, loop) — only
  what's needed for byte-level assembly. Rather than re-deriving that from
  raw bytes a second time, this reuses `banks.e4b.assemble()`/
  `banks.eiii.assemble()` to build a throwaway one-preset bank file, then
  hands it to mpc2emu's own `parsers.e4b_parser.parse_e4b()`/
  `parsers.eiii_parser.parse_eiii()` for the rich, hardware-accurate
  `models.common` zone model — the same reuse-not-reinvent approach the rest
  of this project takes toward mpc2emu.
- **KRZ** and **AKAI**: walks `banks/krz.py`'s / `banks/akai.py`'s own model
  directly, so that
  browsing a K2000 library needs no mpc2emu checkout at all (mpc2emu has had
  a KRZ reader since 2026-07-27, but reaching for it here would make the
  Explorer's KRZ support conditional on configuration the E4B path can
  demand and this one doesn't need). A keymap's entries are collapsed into
  runs of consecutive keys pointing at the same sample, since a K2000 keymap
  has no explicit key-range field at all (KRZ_FORMAT.md §3.2) — each entry
  just names its own sample, and entry `i` sounds at key `i + 12`. AKAI
  carries its zone semantics openly in the program file, so there is nothing
  to re-derive there; and its mpc2emu support sits on an unmerged branch,
  which would make an Explorer row that depended on it appear or vanish with
  whichever branch the configured checkout happens to be on.
"""

from __future__ import annotations

import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import akai, e4b, eiii, krz, loopcheck
from ..mpc2emu_bridge import e4b_parser, eiii_parser

_LOOP_NAMES = {0: "none", 1: "forward", 2: "alternating", 3: "forward (release)"}


@dataclass
class ZoneSummary:
    sample_name: str
    lo_key: int
    hi_key: int
    lo_vel: int
    hi_vel: int
    root_key: int
    loop: str                        # 'none' | 'forward' | 'alternating' | 'forward (release)' | '?'
    sample_rate: int | None = None
    bit_depth: int | None = None


@dataclass
class ZoneStats:
    """Condensed, single-line-per-fact substitute for showing every zone's
    own row (see ui/detail_pane.py's zone_stats_lines()) -- a real preset
    can carry dozens of zones, and per earlier feedback, the detailed key/
    velocity table for each one is more than a human actually wants at a
    glance."""
    key_zone_count: int              # distinct (lo_key, hi_key) ranges
    total_samples: int               # distinct sample names across ALL zones
    vel_layer_count: int             # distinct (lo_vel, hi_vel) ranges
    vel_samples_min: int             # fewest distinct samples in any one velocity layer
    vel_samples_max: int             # most distinct samples in any one velocity layer
    bit_depths: tuple[int, ...] = ()      # distinct values seen, sorted, empty if unknown
    sample_rates: tuple[int, ...] = ()    # distinct values seen (Hz), sorted, empty if unknown


def zone_stats(zones: list[ZoneSummary]) -> ZoneStats | None:
    if not zones:
        return None
    key_zone_count = len({(z.lo_key, z.hi_key) for z in zones})
    total_samples = len({z.sample_name for z in zones})
    vel_layers: dict[tuple[int, int], set[str]] = {}
    for z in zones:
        vel_layers.setdefault((z.lo_vel, z.hi_vel), set()).add(z.sample_name)
    per_layer_counts = [len(s) for s in vel_layers.values()]
    return ZoneStats(
        key_zone_count=key_zone_count,
        total_samples=total_samples,
        vel_layer_count=len(vel_layers),
        vel_samples_min=min(per_layer_counts),
        vel_samples_max=max(per_layer_counts),
        bit_depths=tuple(sorted({z.bit_depth for z in zones if z.bit_depth})),
        sample_rates=tuple(sorted({z.sample_rate for z in zones if z.sample_rate})),
    )


@dataclass
class PresetSummary:
    name: str
    format: str                      # 'E4B' | 'KRZ' | 'EIII'
    voice_count: int                 # voices (E4B/EIII) / keymaps referenced (KRZ)
    zones: list[ZoneSummary] = field(default_factory=list)
    total_sample_bytes: int = 0      # unique samples referenced by this preset's zones
    #: What is behind that figure, so a caller can dedupe ACROSS presets --
    #: the Pending queue totals a whole bank's worth, and two presets sharing
    #: a multisample must not pay for it twice.
    #:
    #: Two fields because the two families answer differently. E4B, EIII and
    #: AKAI resolve a zone to a sample and know its byte count there, so the
    #: sizes travel with the names and merging dicts is the whole job. KRZ
    #: keeps its audio in one region addressed by word offsets, so a set of
    #: object ids is all that means anything and the size comes from the
    #: union of their extents -- see krz_audio_bytes.
    sample_sizes: dict = field(default_factory=dict)   # name -> bytes
    sample_keys: frozenset = frozenset()               # KRZ object ids
    #: AKAI only: {sample name: cents off} for samples whose header declares
    #: a rate the machine cannot produce. mpc2emu's AKAI_SSRATE_UNPLAYABLE
    #: catches this while CONVERTING; this is the same finding for a volume
    #: already sitting on a disc, which no diagnostic ever runs over.
    unplayable_rates: dict = field(default_factory=dict)
    #: Advisory lines for the Detail pane — things worth knowing about the
    #: preset that are not part of its structure. Empty unless a check that
    #: produces them was switched on; nothing here is computed by default,
    #: because these cost a walk of the PCM.
    notes: list[str] = field(default_factory=list)


@dataclass
class BankSummary:
    name: str
    format: str
    preset_count: int
    sample_count: int
    total_size: int
    preset_names: list[str] = field(default_factory=list)


# ── E4B ──────────────────────────────────────────────────────────────────────

def summarize_e4b_bank(bank: e4b.E4BFile) -> BankSummary:
    total = len(bank.e4ma_body) + len(bank.emst_body)
    total += sum(len(p.body) for p in bank.presets)
    total += sum(s.size for s in bank.samples.values())
    return BankSummary(
        name=bank.path,
        format="E4B",
        preset_count=len(bank.presets),
        sample_count=len(bank.samples),
        total_size=total,
        preset_names=[p.name.strip() for p in bank.presets],
    )


def summarize_e4b_preset(bank: e4b.E4BFile, preset: e4b.E4BPreset) -> PresetSummary:
    data = e4b.assemble([(bank, preset)])
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".e4b", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        parsed = e4b_parser.parse_e4b(tmp_path)
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)

    mpc_preset = parsed.presets[0] if parsed.presets else None
    zones: list[ZoneSummary] = []
    voice_count = 0
    sample_sizes: dict[str, int] = {}   # dedupe -- a preset's zones can share one sample
    if mpc_preset is not None:
        voice_count = len(mpc_preset.voices)
        for voice in mpc_preset.voices:
            for z in voice.zones:
                if not z.sample_name:
                    # A placeholder/dummy voice with no real sample assigned
                    # — seen even in real commercial banks (often the very
                    # first "preset" in a bank, matching the bank's own
                    # name, carries no actual content). Not real playable
                    # content, so it's dropped here for the same reason
                    # unresolvable KRZ keymap entries are (see
                    # _keymap_zone_runs).
                    continue
                sample = parsed.find_sample(z.sample_name)
                if sample is not None and z.sample_name not in sample_sizes:
                    sample_sizes[z.sample_name] = len(sample.data)
                zones.append(ZoneSummary(
                    sample_name=z.sample_name,
                    lo_key=z.lo_key, hi_key=z.hi_key,
                    lo_vel=z.lo_vel, hi_vel=z.hi_vel,
                    root_key=z.root_key,
                    loop=_LOOP_NAMES.get(int(sample.loop_type), "?") if sample else "?",
                    sample_rate=sample.sample_rate if sample else None,
                    bit_depth=sample.bit_depth if sample else None,
                ))
    return PresetSummary(name=preset.name.strip(), format="E4B",
                          sample_sizes=dict(sample_sizes),
                          voice_count=voice_count, zones=zones,
                          total_sample_bytes=sum(sample_sizes.values()))


# ── KRZ ──────────────────────────────────────────────────────────────────────

def summarize_krz_bank(bank: krz.KrzFile) -> BankSummary:
    total = len(bank.pcm)
    for objs in (bank.programs, bank.keymaps, bank.samples):
        total += sum(len(o.block) for o in objs.values())
    total += sum(len(o.block) for o in bank.other_objects)
    return BankSummary(
        name=bank.path,
        format="KRZ",
        preset_count=len(bank.programs),
        sample_count=len(bank.samples),
        total_size=total,
        preset_names=[p.name.strip() for p in bank.programs.values()],
    )


def krz_audio_bytes(bank: krz.KrzFile, sample_ids) -> int:
    """Bytes of AUDIO the given KRZ samples need, deduped.

    NOT `len(obj.block)`, which is what this used to be and is the object's
    HEADER -- 88 to 92 bytes. A 1.4 MB bank of 61 samples summed to 21 KB
    that way, and the Detail pane reported it as "total sample size" for as
    long as the pane has existed. It only became obvious once the same figure
    went onto every row: a preset that cannot be converted at all, because it
    references only ROM, was announcing "236 B audio".

    KRZ keeps its audio in one region addressed by word offsets, so the size
    is the union of the referenced extents -- a UNION and not a sum, because
    several sample objects can address overlapping words and adding them
    counts shared audio twice.

    Clamped to the region actually present. A bank split across several discs
    declares extents for audio that is on the NEXT disc, which is real (the
    program does need it) but describes something this file does not contain
    -- and a row reporting 2.67 MB of audio in a 1.39 MB file reads as a
    defect, not as a split bank.
    """
    spans = []
    for sid in sample_ids:
        samp = bank.samples.get(sid)
        if samp is None:
            continue
        try:
            start, words = bank.sample_word_extent(samp)
        except Exception:
            continue
        if words and words > 0:
            spans.append((start, start + words))
    if not spans:
        return 0
    spans.sort()
    total = 0
    cur_start, cur_end = spans[0]
    for start, end in spans[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    total += cur_end - cur_start
    return min(total * 2, len(bank.pcm))
def loop_notes(bank, obj) -> list[str]:
    """Advisory lines about clicking loops, for any of the three formats.

    Dispatches to the per-format loop accessors, each of which knows its own
    PCM offset and byte order — getting either wrong does not fail loudly, it
    reports a corpus of impossibly clean loops (see banks/loopcheck.py).
    """
    if isinstance(bank, krz.KrzFile):
        return krz_loop_notes(bank, obj)
    if isinstance(bank, e4b.E4BFile):
        return _loop_notes_from(
            [(e4b.sample_pcm(s), lp, s.name.strip(), e4b.PCM_BIG_ENDIAN)
             for s in _e4b_preset_samples(bank, obj)
             for lp in e4b.sample_loops(s)])
    if isinstance(bank, eiii.EIIIFile):
        return _loop_notes_from(
            [(eiii.sample_pcm(s), lp, getattr(s, "name", "").strip(),
              eiii.PCM_BIG_ENDIAN)
             for s in _eiii_preset_samples(bank, obj)
             for lp in eiii.sample_loops(s)])
    return []


def _e4b_preset_samples(bank, preset):
    seen = set()
    for idx in getattr(preset, "sample_indices", []) or []:
        if idx in seen:
            continue
        seen.add(idx)
        s = bank.samples.get(idx)
        if s is not None:
            yield s


def _eiii_preset_samples(bank, preset):
    seen = set()
    for idx in getattr(preset, "sample_indices", []) or []:
        if idx in seen:
            continue
        seen.add(idx)
        s = bank.samples.get(idx)
        if s is not None:
            yield s


def _loop_notes_from(items) -> list[str]:
    """One aggregated line naming the worst offender, from (pcm, (ls, le),
    name, big_endian) tuples."""
    worst, n = None, 0
    for pcm, (ls, le), name, be in items:
        hit = loopcheck.check_loop(pcm, ls, le, name, big_endian=be)
        if hit:
            n += 1
            if worst is None or hit.step_pct > worst.step_pct:
                worst = hit
    if worst is None:
        return []
    others = f" (and {n - 1} more)" if n > 1 else ""
    return [f"Loop clicks: {worst.sample_name!r} steps {worst.step_pct:.0f}% of "
            f"its local level at the loop point{others} — audible as a tick on "
            f"every repeat. The loop is as the source authored it; nothing here "
            f"changes it."]


def krz_loop_notes(bank: krz.KrzFile, prog: krz.KrzObject) -> list[str]:
    """Advisory lines for loops that click, for one program.

    Aggregated to ONE line per program naming the worst offender, not one per
    sample: a multisample with fifteen clicking zones is a single authoring
    problem, and fifteen lines in the Detail pane would bury everything else.
    ConvertWithMoss reached the same shape independently.

    Only called when `Config.loop_click_check` is on — it reads the PCM
    around every loop the program touches.
    """
    worst = None
    n = 0
    seen: set[int] = set()
    for kid in bank.program_keymap_refs(prog):
        km = bank.keymaps.get(kid)
        if km is None:
            continue
        for sid in bank.keymap_sample_refs(km):
            if sid in seen:
                continue
            seen.add(sid)
            samp = bank.samples.get(sid)
            if samp is None:
                continue
            for start, end in bank.sample_loops(samp):
                hit = loopcheck.check_loop(bank.pcm, start, end, samp.name.strip())
                if hit:
                    n += 1
                    if worst is None or hit.step_pct > worst.step_pct:
                        worst = hit
    if worst is None:
        return []
    others = f" (and {n - 1} more)" if n > 1 else ""
    return [f"Loop clicks: {worst.sample_name!r} steps {worst.step_pct:.0f}% of "
            f"its local level at the loop point{others} — audible as a tick on "
            f"every repeat. The loop is as the source authored it; nothing here "
            f"changes it."]


def summarize_krz_program(bank: krz.KrzFile, prog: krz.KrzObject) -> PresetSummary:
    keymap_ids = list(dict.fromkeys(bank.program_keymap_refs(prog)))  # dedupe, keep order
    zones: list[ZoneSummary] = []
    sample_ids: set[int] = set()   # dedupe -- several keymaps can share a sample
    for kid in keymap_ids:
        km = bank.keymaps.get(kid)
        if km is not None:
            km_zones, km_sample_ids = _keymap_zone_runs(bank, km)
            zones.extend(km_zones)
            sample_ids.update(km_sample_ids)
    total_sample_bytes = krz_audio_bytes(bank, sample_ids)
    return PresetSummary(name=prog.name.strip(), format="KRZ",
                          sample_keys=frozenset(sample_ids),
                          voice_count=len(keymap_ids), zones=zones,
                          total_sample_bytes=total_sample_bytes)


def _keymap_entry_sample_ids(km: krz.KrzObject) -> list[int]:
    """The sample id each of a keymap's entries names, in entry order —
    every entry naming the header's own sample when the keymap is compacted.
    The layout rules live in `banks/krz.py`'s `keymap_layout()`; this only
    reads them out."""
    body = km.body()
    lay = krz.keymap_layout(body)
    if lay is None:
        return []
    if lay.id_off is None:
        return [lay.header_sid] * lay.num_keys

    ids = []
    for off in lay.entry_offsets():
        if off + 2 > len(body):
            break
        ids.append(struct.unpack_from(">H", body, off)[0])
    return ids


def _keymap_bands(km: krz.KrzObject) -> list[tuple[int, int, list[int]]]:
    """[(lo_vel, hi_vel, [sample id per key])] — one tuple per velocity band.

    This used to be a single `lay.table + k*lay.stride + lay.id_off` walk,
    one of three hand-rolled copies that had drifted from
    `KeymapLayout.entry_offsets()` -- whose docstring says it exists so "the
    reference walk and the id patcher cannot drift apart". They had: the
    assembler read all eight bands while the Detail pane, and both detectors
    in tools/check_krz_banks.py, read only the softest one. A four-layer
    velocity multisample showed as ONE zone naming whichever sample band 0
    happens to hold."""
    body = km.body()
    lay = krz.keymap_layout(body)
    if lay is None:
        return []
    if lay.id_off is None:
        return [(0, 127, [lay.header_sid] * lay.num_keys)]
    windows = krz.band_velocity_windows(body, lay.num_keys, lay.stride)
    out = []
    for base in (lay.bands or (lay.table,)):
        ids = []
        for k in range(lay.num_keys):
            off = base + k * lay.stride + lay.id_off
            if off + 2 > len(body):
                break
            ids.append(struct.unpack_from(">H", body, off)[0])
        lo_vel, hi_vel = windows.get(base, (0, 127))
        out.append((lo_vel, hi_vel, ids))
    return out


def _keymap_zone_runs(bank: krz.KrzFile, km: krz.KrzObject) -> tuple[list[ZoneSummary], set[int]]:
    """Collapse a keymap's key->sample entries into runs of consecutive keys
    sharing the same sample id.

    Real hardware-saved banks can carry keymap slots pointing at a sample id
    that doesn't exist in this bank at all — leftover/uninitialized entries
    from editing on the K2000 itself, not real content (seen in this
    project's own library, e.g. ids like 256 or 51201 sitting beside
    genuinely-referenced samples in the same keymap). Those runs are
    dropped rather than shown as `<sample NNNN>` noise — a librarian is
    for finding real, playable content, not surfacing hardware artifacts."""
    def key_of(entry: int) -> int:
        # Entry i sounds at key i+12, so entries run past 127 and the tail is
        # clamped rather than shown as a key the keyboard doesn't have.
        return min(127, entry + krz.KEYMAP_ENTRY_NOTE_OFFSET)

    runs: list[ZoneSummary] = []
    sample_ids: set[int] = set()
    # Once per velocity band, so a layered keymap reports one set of key runs
    # per layer rather than only its softest.
    for lo_vel, hi_vel, sample_by_entry in _keymap_bands(km):
        lo, prev_sid = None, None
        for entry, sid in enumerate(sample_by_entry):
            if sid != prev_sid:
                if prev_sid and prev_sid in bank.samples:
                    runs.append(_krz_zone(bank, prev_sid, key_of(lo),
                                          key_of(entry) - 1, lo_vel, hi_vel))
                    sample_ids.add(prev_sid)
                lo, prev_sid = entry, sid
        if prev_sid and prev_sid in bank.samples:
            runs.append(_krz_zone(bank, prev_sid, key_of(lo),
                                  key_of(len(sample_by_entry) - 1),
                                  lo_vel, hi_vel))
            sample_ids.add(prev_sid)
    return runs, sample_ids


def _krz_zone(bank: krz.KrzFile, sid: int, lo_key: int, hi_key: int,
               lo_vel: int = 0, hi_vel: int = 127) -> ZoneSummary:
    samp = bank.samples[sid]   # caller already checked sid is a real sample
    name = samp.name.strip()
    root_key, loop = 60, "?"
    sample_rate, bit_depth = None, None
    b = samp.body()
    if len(b) > krz.SAMPLE_HDR:
        root_key = b[krz.SAMPLE_HDR]                     # Soundfilehead byte 0
    if len(b) > krz.SAMPLE_HDR + 1:
        # KRZ shares E4B's loop-flag convention (KRZ_FORMAT.md §3.1):
        # bit 0x80 clear = looped, set = one-shot.
        loop = "none" if (b[krz.SAMPLE_HDR + 1] & 0x80) else "forward"
    if len(b) >= krz.SAMPLE_HDR + 32:
        # Soundfilehead offset 28:32, `samplePeriod` = round(1e9 /
        # sample_rate) (KRZ_FORMAT.md §3.1) -- the one field in this
        # header that encodes the real sample rate directly and exactly,
        # unlike maxPitch (derived from rootkey too, lossy to invert).
        period = struct.unpack_from(">I", b, krz.SAMPLE_HDR + 28)[0]
        if period:
            sample_rate = round(1e9 / period)
        # The K2000's own PCM is always raw 16-bit (KRZ_FORMAT.md §2/3.1)
        # -- no per-sample bit-depth field exists because there's nothing
        # else it could be.
        bit_depth = 16
    return ZoneSummary(sample_name=name, lo_key=lo_key, hi_key=hi_key,
                        sample_rate=sample_rate, bit_depth=bit_depth,
                        lo_vel=lo_vel, hi_vel=hi_vel, root_key=root_key, loop=loop)


# ── EIII ─────────────────────────────────────────────────────────────────────

def summarize_eiii_bank(bank: eiii.EIIIFile) -> BankSummary:
    # No E4Ma/EMSt-style bank-wide chunk to add in here -- EMPTY_BANK_SIZE
    # (the header + address tables + device master-settings block every
    # EIII bank carries) stands in for that fixed overhead, same role
    # e4ma_body/emst_body play in summarize_e4b_bank's total.
    total = eiii.EMPTY_BANK_SIZE
    total += sum(len(p.body) for p in bank.presets)
    total += sum(s.size for s in bank.samples.values())
    return BankSummary(
        name=bank.path,
        format="EIII",
        preset_count=len(bank.presets),
        sample_count=len(bank.samples),
        total_size=total,
        preset_names=[p.name.strip() for p in bank.presets],
    )


def summarize_eiii_preset(bank: eiii.EIIIFile, preset: eiii.EIIIPreset) -> PresetSummary:
    data = eiii.assemble([(bank, preset)])
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".e3x", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        parsed = eiii_parser.parse_eiii(tmp_path)
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)

    mpc_preset = parsed.presets[0] if parsed.presets else None
    zones: list[ZoneSummary] = []
    voice_count = 0
    sample_sizes: dict[str, int] = {}   # dedupe -- a preset's zones can share one sample
    if mpc_preset is not None:
        voice_count = len(mpc_preset.voices)
        for voice in mpc_preset.voices:
            for z in voice.zones:
                if not z.sample_name:
                    continue
                sample = parsed.find_sample(z.sample_name)
                if sample is not None and z.sample_name not in sample_sizes:
                    sample_sizes[z.sample_name] = len(sample.data)
                zones.append(ZoneSummary(
                    sample_name=z.sample_name,
                    lo_key=z.lo_key, hi_key=z.hi_key,
                    lo_vel=z.lo_vel, hi_vel=z.hi_vel,
                    root_key=z.root_key,
                    loop=_LOOP_NAMES.get(int(sample.loop_type), "?") if sample else "?",
                    sample_rate=sample.sample_rate if sample else None,
                    bit_depth=sample.bit_depth if sample else None,
                ))
    return PresetSummary(name=preset.name.strip(), format="EIII",
                          sample_sizes=dict(sample_sizes),
                          voice_count=voice_count, zones=zones,
                          total_sample_bytes=sum(sample_sizes.values()))


# ── AKAI ─────────────────────────────────────────────────────────────────────
#
# Walks banks/akai.py's own model directly, like the KRZ path above and for
# the same reason -- only more so. mpc2emu's AKAI support is on an unmerged
# branch, so making the Explorer's AKAI rows depend on it would mean they
# appear or vanish with whichever branch the configured checkout is on.

def _clamp_key(k: int) -> int:
    return max(0, min(127, k))


def summarize_akai_bank(bank: akai.AkaiBank) -> BankSummary:
    return BankSummary(
        name=bank.path,
        format="AKAI",
        preset_count=len(bank.programs),
        sample_count=len(bank.samples),
        total_size=bank.total_size,
        preset_names=[p.name.strip() for p in bank.programs],
    )


def summarize_akai_program(bank: akai.AkaiBank,
                            prog: akai.AkaiProgram) -> PresetSummary:
    """One AKAI program's zones.

    The nesting is the inverse of every other format here: an AKAI *keygroup*
    owns a key range and holds up to four VELOCITY zones inside it, where an
    E4B voice holds zones that each carry their own key range. So one
    keygroup expands to one ZoneSummary per velocity zone, all sharing the
    keygroup's key span.

    A zone's root key and loop come from the SAMPLE, not from the program:
    AKAI keeps the root note in the sample header and the keygroup only tunes
    away from it. A zone naming a sample this volume doesn't hold still gets
    a row -- that is normal for a library split across floppies (see
    AkaiBank.missing_samples), and dropping it would hide the reason a
    program sounds incomplete.

    Key ranges are clamped to the keyboard, not filtered on. 145 of 29 180
    keygroups on eight real library discs declare something outside it -- a
    few inverted by one (89-88), the rest inside files typed as programs
    that plainly hold something else. They carry 412 zones between them, and
    14 of those do name a real sample, so refusing the keygroup outright
    would drop real content to tidy away an artifact. Clamping keeps the row
    honest and the number meaningful."""
    zones: list[ZoneSummary] = []
    sample_sizes: dict[str, int] = {}
    unplayable: dict[str, float] = {}
    for kg in prog.keygroups:
        lo_key, hi_key = _clamp_key(kg.lo_key), _clamp_key(kg.hi_key)
        for z in kg.zones:
            samp = bank.find_sample(z.sample_name)
            if samp is not None and z.sample_name not in sample_sizes:
                # AUDIO, not the file: an AKAI sample file is a header plus
                # PCM, and every other format's figure here is the audio
                # alone. Counting the header made an AKAI preset read a
                # little large and, summed over a queue of volumes, put the
                # Pending column visibly out of step with New Bank's meter,
                # which measures what the sampler loads.
                # SAMPLE_HEADER_BYTES (150), not header_len. They are
                # different quantities that happen to share a number on the
                # S1000: header_len is where the PCM starts in the record
                # (0xC0 = 192 on an S3000), while 150 is the measured
                # difference between a file's size and the SLNGTH the machine
                # reports for it. The RAM figure New Bank's meter shows uses
                # the measured one, so this must too or the two drift by 42
                # bytes per S3000 sample. There is a note about this exact
                # confusion beside the constant; I still reached for the
                # wrong one.
                sample_sizes[z.sample_name] = max(
                    0, samp.size - akai.AkaiBank.SAMPLE_HEADER_BYTES)
                if samp.rate_is_unplayable:
                    unplayable[z.sample_name] = samp.rate_cents_off
            zones.append(ZoneSummary(
                sample_name=z.sample_name,
                lo_key=lo_key, hi_key=hi_key,
                lo_vel=_clamp_key(z.lo_vel), hi_vel=_clamp_key(z.hi_vel),
                root_key=samp.root_key if samp else 60,
                loop=samp.loop if samp else "?",
                sample_rate=samp.sample_rate if samp else None,
                # The format carries 16-bit PCM and no bit-depth field,
                # because there is nothing else it could be.
                bit_depth=16 if samp else None,
            ))
    return PresetSummary(name=prog.name.strip(), format="AKAI",
                          sample_sizes=dict(sample_sizes),
                          unplayable_rates=dict(unplayable),
                          voice_count=len(prog.keygroups), zones=zones,
                          total_sample_bytes=sum(sample_sizes.values()))


def audio_bytes_for_keys(bank, fmt: str, keys) -> int:
    """Audio for a set of sample identities out of ONE bank, deduped.

    The companion to PresetSummary.sample_keys: hand back the union of what
    several presets referenced and get the figure ONCE, which is what a whole
    staged bank costs rather than the sum of its presets. On one real bank
    those differ by a factor of three.

    KRZ goes through krz_audio_bytes because its identities are object ids
    and its sizes are word extents in a shared region; the others carry
    sample names and their own byte counts.
    """
    if not keys:
        return 0
    return krz_audio_bytes(bank, keys) if fmt == "KRZ" else 0


# ── generic dispatch (what the UI actually calls) ───────────────────────────

def summarize_bank(bank) -> BankSummary:
    if isinstance(bank, e4b.E4BFile):
        return summarize_e4b_bank(bank)
    if isinstance(bank, krz.KrzFile):
        return summarize_krz_bank(bank)
    if isinstance(bank, eiii.EIIIFile):
        return summarize_eiii_bank(bank)
    if isinstance(bank, akai.AkaiBank):
        return summarize_akai_bank(bank)
    raise TypeError(f"not a recognised bank type: {type(bank)!r}")


def summarize_preset(bank, obj, loop_click_check: bool = False) -> PresetSummary:
    """`loop_click_check` is off by default and costs a walk of the PCM around
    every loop the preset touches — the caller passes the user's setting."""
    if isinstance(bank, e4b.E4BFile):
        out = summarize_e4b_preset(bank, obj)
    elif isinstance(bank, krz.KrzFile):
        out = summarize_krz_program(bank, obj)
    elif isinstance(bank, eiii.EIIIFile):
        out = summarize_eiii_preset(bank, obj)
    elif isinstance(bank, akai.AkaiBank):
        # AKAI arrived on master while this branch was away. It takes the
        # same road as the rest: summarise, then let an opt-in check add
        # its notes. loop_notes() returns [] for a format it does not
        # handle, so an Akai preset simply gets none rather than an error.
        out = summarize_akai_program(bank, obj)
    else:
        raise TypeError(f"not a recognised bank type: {type(bank)!r}")
    if loop_click_check:
        out.notes.extend(loop_notes(bank, obj))
    return out


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m vinsamlib.banks.summary <bank file>")
        return 2
    path = argv[1]
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"FORM" and data[8:12] == b"E4B0":
        bank = e4b.parse_bytes(data, path)
    elif data[:4] == b"PRAM":
        bank = krz.parse_bytes(data, path)
    elif eiii.detect_format(data) is not None:
        bank = eiii.parse_bytes(data, path)
    else:
        print(f"{path}: not a recognised bank (no FORM...E4B0, PRAM or EIII header)")
        return 1

    bs = summarize_bank(bank)
    print(f"{path}: {bs.format}, {bs.preset_count} preset(s), "
          f"{bs.sample_count} sample(s), {bs.total_size:,} bytes")

    if isinstance(bank, e4b.E4BFile) or isinstance(bank, eiii.EIIIFile):
        presets = bank.presets
    else:
        presets = list(bank.programs.values())
    for p in presets[:3]:
        ps = summarize_preset(bank, p)
        print(f"\n  {ps.name!r} — {ps.voice_count} voice(s)/keymap(s), {len(ps.zones)} zone(s)")
        for z in ps.zones[:8]:
            print(f"    {z.sample_name!r:30s} key {z.lo_key:3d}-{z.hi_key:3d}  "
                  f"vel {z.lo_vel:3d}-{z.hi_vel:3d}  root {z.root_key:3d}  loop={z.loop}")
        if len(ps.zones) > 8:
            print(f"    … {len(ps.zones) - 8} more zone(s)")
    if len(presets) > 3:
        print(f"\n  … {len(presets) - 3} more preset(s)")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
