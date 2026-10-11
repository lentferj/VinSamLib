"""
Plain-data summaries of a bank/preset for the UI (Detail pane, Samples pane).

No Qt, no printing/ANSI — just dataclasses the UI renders directly. Two very
different strategies per format, because only some of them have an existing
semantic reader to lean on:

- **E4B** and **EIII**: the container already carries every field the zone
  model needs — `preset.body` holds the voice blocks and their zone entries,
  `bank.samples[idx]` holds the audio, its rate and its loop points — so this
  reads them out directly, the same way the KRZ and AKAI paths below do. It
  used to rebuild the whole bank with `assemble()`, write a temp file and hand
  it to mpc2emu's own `parse_e4b()`/`parse_eiii()`, once PER PRESET: measured
  on a 12.6 MB / 120-preset E4B that was 0.45 s and 188 MB of audio touched,
  15x the bank's own, 120 temp files created, parsed and deleted — for one
  column. Extrapolated to a real 75.8 MB / 700-preset bank it is ~1.1 GB of
  temp-file writes and parses per scan. The offsets this reads are the SAME
  ones mpc2emu's parser reads (and the ones `e4b.py`/`eiii.py` already
  document), so the numbers do not change; only the route to them does.
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
from dataclasses import dataclass, field

from . import akai, e4b, eiii, krz, loopcheck

#: The K2000's own playback rates, the same table mpc2emu snaps against
#: (`parsers/krz_parser.py:95-99`, itself taken from ConvertWithMoss).
#: `samplePeriod` is round(1e9 / rate), so inverting it does not give the
#: round number back: a 44100 Hz sample stores 22676 ns, which inverts to
#: 44100.98 Hz and `round()` reported as **44101** in the Detail pane. A hertz
#: is inaudible, but a rate column that reads 44101 where the file says 44100
#: looks like a defect rather than a rounding, and the two readers of the same
#: field disagreed by one. Now they snap the same way.
KRZ_STANDARD_RATES = (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 96000)


def _snap_sample_rate(hz: float) -> int:
    """Nearest standard rate within ±2 Hz, else the rounded value.

    Same rule as mpc2emu's `_snap_sample_rate`: the ±2 window is what a
    period rounded to a whole nanosecond can be out by, so nothing real lands
    outside it and a genuinely odd rate is still reported as itself.
    """
    for std in KRZ_STANDARD_RATES:
        if abs(hz - std) <= 2:
            return std
    return round(hz)


@dataclass
class ZoneSummary:
    sample_name: str
    lo_key: int
    hi_key: int
    lo_vel: int
    hi_vel: int
    root_key: int
    loop: str  # 'none' | 'forward' | 'alternating' | 'forward (release)' | '?'
    sample_rate: int | None = None
    bit_depth: int | None = None


@dataclass
class ZoneStats:
    """Condensed, single-line-per-fact substitute for showing every zone's
    own row (see ui/detail_pane.py's zone_stats_lines()) -- a real preset
    can carry dozens of zones, and per earlier feedback, the detailed key/
    velocity table for each one is more than a human actually wants at a
    glance."""

    key_zone_count: int  # distinct (lo_key, hi_key) ranges
    total_samples: int  # distinct sample names across ALL zones
    vel_layer_count: int  # distinct (lo_vel, hi_vel) ranges
    vel_samples_min: int  # fewest distinct samples in any one velocity layer
    vel_samples_max: int  # most distinct samples in any one velocity layer
    bit_depths: tuple[int, ...] = ()  # distinct values seen, sorted, empty if unknown
    sample_rates: tuple[
        int, ...
    ] = ()  # distinct values seen (Hz), sorted, empty if unknown


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
    format: str  # 'E4B' | 'KRZ' | 'EIII'
    voice_count: int  # voices (E4B/EIII) / keymaps referenced (KRZ)
    zones: list[ZoneSummary] = field(default_factory=list)
    total_sample_bytes: int = 0  # unique samples referenced by this preset's zones
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
    sample_sizes: dict = field(default_factory=dict)  # name -> bytes
    sample_keys: frozenset = frozenset()  # KRZ object ids
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
    """One E4B preset's zones, read straight out of the container.

    Every field here lives in bytes this module already parsed: the voice
    blocks at `preset.body[82:]` carry the voice-level key and velocity windows,
    and the 22-byte zone entries after each of them carry the zone's own
    windows, its root key and the sample index it plays. `bank.samples` holds
    the audio, the rate and the loop fields. The offsets are the ones
    `e4b.py` documents and mpc2emu's own parser reads, so this is the same
    reading by a shorter route — no rebuild, no temp file, no re-parse.

    THREE THINGS THE OLD ROUTE DID THAT ARE KEPT ON PURPOSE:

    * The voice window INTERSECTS the zone's: `lo = max(voice_lo, zone_lo)`.
      Real commercial banks commonly leave the zone entry wide open at 0-127
      and do the actual split at the voice level, and mpc2emu's parser makes
      the same intersection (76 057 zones behind that comment).
    * A zone whose windows do not overlap after the intersection is DEAD — a
      leftover entry — and is dropped, exactly as mpc2emu's guard drops it.
    * A zone naming a sample this bank does not hold (a ROM sample, a dangling
      reference, or one of the empty-zone sentinels 3FFFh/3FFEh that real
      hardware leaves behind) is dropped, which is what the old route did by
      reading `sample_name == ''`. Dropped rather than shown as
      `<sample NNNN>` noise, for the same reason the KRZ path drops an
      unresolvable keymap entry: this is a librarian, not a hex editor.

    WHAT IS DIFFERENT, and only on material where the old route was wrong:
    the loop now comes from this module's own `sample_loops`, which reads the
    RIGHT field pair for a right-channel-only sample (see e4b._OPT_HAS_LEFT),
    where mpc2emu's parser read the left one. The sample name is the
    container's own 16-byte name rather than mpc2emu's display name with the
    `_NoteOctave` suffix stripped off, which is also what the KRZ and AKAI
    paths below report — one convention in this module rather than two.
    """
    body = preset.body
    zones: list[ZoneSummary] = []
    voice_count = 0
    # Deduped by sample OBJECT, not by name. A name is 16 bytes and two real
    # samples can share one -- measured on this corpus, 12 of 336 presets hold
    # several samples under one display name -- and counting by name alone
    # would then report the audio once. Keyed by name for the caller, which
    # only ever has the name, but accumulated per object so nothing is lost.
    sample_sizes: dict[str, int] = {}
    seen_samples: set[int] = set()
    for v_start, table_start, n in preset.voice_zones():
        voice_count += 1
        v_lo_key = body[v_start + e4b.VOICE_LO_KEY]
        v_hi_key = body[v_start + e4b.VOICE_HI_KEY]
        v_lo_vel = body[v_start + e4b.VOICE_LO_VEL]
        v_hi_vel = body[v_start + e4b.VOICE_HI_VEL]
        for k in range(n):
            eo = table_start + k * e4b.ZONE_ENTRY
            if eo + e4b.ZONE_ENTRY > len(body):
                break
            idx = struct.unpack_from(">H", body, eo + e4b.ZONE_SAMPLE_INDEX)[0]
            samp = bank.samples.get(idx)
            if samp is None:
                continue
            lo_key = max(v_lo_key, body[eo + e4b.ZONE_LO_KEY])
            hi_key = min(v_hi_key, body[eo + e4b.ZONE_HI_KEY])
            lo_vel = max(v_lo_vel, body[eo + e4b.ZONE_LO_VEL])
            hi_vel = min(v_hi_vel, body[eo + e4b.ZONE_HI_VEL])
            if lo_key > hi_key or lo_vel > hi_vel:
                continue  # dead zone: the windows do not overlap at all
            name = samp.name
            if idx not in seen_samples:
                seen_samples.add(idx)
                # The audio, not the chunk: an E3S1 body is a 94-byte header
                # plus PCM, and every other format's figure here is the audio
                # alone (see the AKAI path below).
                sample_sizes[name] = sample_sizes.get(name, 0) + len(samp.pcm)
            zones.append(
                ZoneSummary(
                    sample_name=name,
                    lo_key=lo_key,
                    hi_key=hi_key,
                    lo_vel=lo_vel,
                    hi_vel=hi_vel,
                    root_key=body[eo + e4b.ZONE_ROOT_KEY],
                    # The module's own loop accessor, so the Detail pane's loop
                    # column and its loop-click note can never disagree: 0x0001
                    # in the options word is a forward loop, everything else is
                    # not, and a right-channel-only sample's points are read
                    # from the field pair its options name.
                    loop="forward" if e4b.sample_loops(samp) else "none",
                    sample_rate=e4b.sample_rate(samp),
                    # The format carries 16-bit PCM and no bit-depth field,
                    # because there is nothing else it could be.
                    bit_depth=16,
                )
            )
    return PresetSummary(
        name=preset.name.strip(),
        format="E4B",
        sample_sizes=dict(sample_sizes),
        voice_count=voice_count,
        zones=zones,
        total_sample_bytes=sum(sample_sizes.values()),
    )


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
        except Exception:  # noqa: BLE001, S112
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
            [
                (e4b.sample_pcm(s), lp, s.name.strip(), e4b.PCM_BIG_ENDIAN)
                for s in _e4b_preset_samples(bank, obj)
                for lp in e4b.sample_loops(s)
            ]
        )
    if isinstance(bank, eiii.EIIIFile):
        return _loop_notes_from(
            [
                (
                    eiii.sample_pcm(s),
                    lp,
                    getattr(s, "name", "").strip(),
                    eiii.PCM_BIG_ENDIAN,
                )
                for s in _eiii_preset_samples(bank, obj)
                for lp in eiii.sample_loops(s)
            ]
        )
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
    return [
        f"Loop clicks: {worst.sample_name!r} steps {worst.step_pct:.0f}% of "  # noqa: ISC004
        f"its local level at the loop point{others} — audible as a tick on "
        f"every repeat. The loop is as the source authored it; nothing here "
        f"changes it."
    ]


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
    return [
        f"Loop clicks: {worst.sample_name!r} steps {worst.step_pct:.0f}% of "  # noqa: ISC004
        f"its local level at the loop point{others} — audible as a tick on "
        f"every repeat. The loop is as the source authored it; nothing here "
        f"changes it."
    ]


def summarize_krz_program(bank: krz.KrzFile, prog: krz.KrzObject) -> PresetSummary:
    keymap_ids = list(
        dict.fromkeys(bank.program_keymap_refs(prog))
    )  # dedupe, keep order
    zones: list[ZoneSummary] = []
    sample_ids: set[int] = set()  # dedupe -- several keymaps can share a sample
    for kid in keymap_ids:
        km = bank.keymaps.get(kid)
        if km is not None:
            km_zones, km_sample_ids = _keymap_zone_runs(bank, km)
            zones.extend(km_zones)
            sample_ids.update(km_sample_ids)
    total_sample_bytes = krz_audio_bytes(bank, sample_ids)
    return PresetSummary(
        name=prog.name.strip(),
        format="KRZ",
        sample_keys=frozenset(sample_ids),
        voice_count=len(keymap_ids),
        zones=zones,
        total_sample_bytes=total_sample_bytes,
    )


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
    for base in lay.bands or (lay.table,):
        ids = []
        for k in range(lay.num_keys):
            off = base + k * lay.stride + lay.id_off
            if off + 2 > len(body):
                break
            ids.append(struct.unpack_from(">H", body, off)[0])
        lo_vel, hi_vel = windows.get(base, (0, 127))
        out.append((lo_vel, hi_vel, ids))
    return out


def _keymap_zone_runs(
    bank: krz.KrzFile, km: krz.KrzObject
) -> tuple[list[ZoneSummary], set[int]]:
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
                    runs.append(
                        _krz_zone(
                            bank,
                            prev_sid,
                            key_of(lo),
                            key_of(entry) - 1,
                            lo_vel,
                            hi_vel,
                        )
                    )
                    sample_ids.add(prev_sid)
                lo, prev_sid = entry, sid
        if prev_sid and prev_sid in bank.samples:
            runs.append(
                _krz_zone(
                    bank,
                    prev_sid,
                    key_of(lo),
                    key_of(len(sample_by_entry) - 1),
                    lo_vel,
                    hi_vel,
                )
            )
            sample_ids.add(prev_sid)
    return runs, sample_ids


def _krz_zone(
    bank: krz.KrzFile,
    sid: int,
    lo_key: int,
    hi_key: int,
    lo_vel: int = 0,
    hi_vel: int = 127,
) -> ZoneSummary:
    samp = bank.samples[sid]  # caller already checked sid is a real sample
    name = samp.name.strip()
    root_key, loop = 60, "?"
    sample_rate, bit_depth = None, None
    b = samp.body()
    if len(b) > krz.SAMPLE_HDR:
        root_key = b[krz.SAMPLE_HDR]  # Soundfilehead byte 0
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
            sample_rate = _snap_sample_rate(1e9 / period)
        # The K2000's own PCM is always raw 16-bit (KRZ_FORMAT.md §2/3.1)
        # -- no per-sample bit-depth field exists because there's nothing
        # else it could be.
        bit_depth = 16
    return ZoneSummary(
        sample_name=name,
        lo_key=lo_key,
        hi_key=hi_key,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        lo_vel=lo_vel,
        hi_vel=hi_vel,
        root_key=root_key,
        loop=loop,
    )


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


def summarize_eiii_preset(
    bank: eiii.EIIIFile, preset: eiii.EIIIPreset
) -> PresetSummary:
    """One EIII preset's zones, read straight out of the container.

    `eiii.preset_zone_layers()` does the walk -- the preset's own key-mapping
    table, its note-zone table and the zone slots those name, per linked
    segment and per layer -- and this turns each into a display row. As with
    the E4B path above, no rebuild, no temp file, no re-parse, and the layout
    is read by the same offsets mpc2emu's own parser reads.

    TWO THINGS THE OLD ROUTE DID THAT ARE KEPT ON PURPOSE:

    * A zone naming a sample this bank does not hold is dropped, which is what
      the old route did by reading `sample_name == ''`. Dangling indices are
      ordinary in real material: several library CD-ROMs were mastered through
      an 8-bit tool chain that wrote a zone's 16-bit sample index with the low
      byte only, so a preset whose samples sit above slot 256 plays unrelated
      material unless something maps the index back (mpc2emu's own
      "truncated 8-bit repair" pass, which this route never ran -- it read
      whatever the stored index held).
    * A layer with no zones is not a voice, matching mpc2emu's
      `VoiceLayer`-per-non-empty-layer shape.

    WHAT IS DIFFERENT: the loop column now reports what the zone will PLAY --
    the sample's own loop, unless the zone's flags opt out of it
    (`ZONE_FLAG_DISABLE_LOOP`) -- rather than the sample's loop alone. mpc2emu
    models that override by synthesising a second sample named
    `<name> NL<index>`, so the old route showed a zone named "PIANO NL12"
    playing nothing looped; this shows the sample's real name and the honest
    answer. The sample size is the audio rather than the object.
    """
    zones: list[ZoneSummary] = []
    voice_count = 0
    # Deduped by sample object, summed by name -- see the E4B path above for
    # why the two are not the same question.
    sample_sizes: dict[str, int] = {}
    seen_samples: set[int] = set()
    for voice in eiii.preset_zone_layers(preset):
        voice_count += 1
        for z in voice:
            samp = bank.samples.get(z.sample_index)
            if samp is None:
                continue
            name = samp.name
            if z.sample_index not in seen_samples:
                seen_samples.add(z.sample_index)
                # The audio, not the object: an EIII sample body is a 92-byte
                # header plus PCM.
                sample_sizes[name] = sample_sizes.get(name, 0) + len(samp.pcm)
            zones.append(
                ZoneSummary(
                    sample_name=name,
                    lo_key=z.lo_key,
                    hi_key=z.hi_key,
                    lo_vel=z.lo_vel,
                    hi_vel=z.hi_vel,
                    root_key=z.root_key,
                    loop=(
                        "none"
                        if z.loop_disabled
                        else ("forward" if eiii.sample_loops(samp) else "none")
                    ),
                    # The EIII sample header carries the rate at 0x34, the
                    # same field the E4B path reads from its own header -- and
                    # the right field pair for a right-channel-only sample's
                    # loop points (see eiii._EIII_OPT_HAS_LEFT).
                    sample_rate=eiii.sample_rate(samp),
                    bit_depth=16,
                )
            )
    return PresetSummary(
        name=preset.name.strip(),
        format="EIII",
        sample_sizes=dict(sample_sizes),
        voice_count=voice_count,
        zones=zones,
        total_sample_bytes=sum(sample_sizes.values()),
    )


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


def summarize_akai_program(
    bank: akai.AkaiBank, prog: akai.AkaiProgram
) -> PresetSummary:
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
                #
                # `len(samp.pcm)` and NOT `samp.size - SAMPLE_HEADER_BYTES`.
                # For an S3000 sample `samp.size` is 192 header bytes plus
                # PCM padded out to a file block, so `size - 150` is 42 bytes
                # of header residue plus the PADDED audio -- overstating the
                # real audio by 42 bytes plus the block padding, on every
                # sample, in a figure labelled "sample size". `AkaiSample.pcm`
                # is the audio the header declares, from `header_len` to
                # `header_len + frame_count * 2`: no header, no padding.
                #
                # This is deliberately NOT the RAM figure. AkaiBank.ram_words()
                # uses the measured 150 against the file size on purpose,
                # because that is what the machine's own SLNGTH reports and
                # what it was validated against (16 424 982 words predicted,
                # 16 424 982 loaded). A size column and a RAM meter answer
                # different questions and only one of them is "how much audio
                # is this".
                sample_sizes[z.sample_name] = len(samp.pcm)
                if samp.rate_is_unplayable:
                    unplayable[z.sample_name] = samp.rate_cents_off
            zones.append(
                ZoneSummary(
                    sample_name=z.sample_name,
                    lo_key=lo_key,
                    hi_key=hi_key,
                    lo_vel=_clamp_key(z.lo_vel),
                    hi_vel=_clamp_key(z.hi_vel),
                    # EFFECTIVE, not stored. A sample carrying -1200 cents of
                    # its own tuning sounds an octave from the root byte in its
                    # header, and the pane's job is to say what the sampler will
                    # play. mpc2emu reports the same number by a different route
                    # (it has no coarse-tune field, so the semitones go on the
                    # root), and the two disagreed on 274 of 418 samples until
                    # this line stopped reading the raw byte.
                    root_key=samp.effective_root_key if samp else 60,
                    loop=samp.loop if samp else "?",
                    sample_rate=samp.sample_rate if samp else None,
                    # The format carries 16-bit PCM and no bit-depth field,
                    # because there is nothing else it could be.
                    bit_depth=16 if samp else None,
                )
            )
    return PresetSummary(
        name=prog.name.strip(),
        format="AKAI",
        sample_sizes=dict(sample_sizes),
        unplayable_rates=dict(unplayable),
        voice_count=len(prog.keygroups),
        zones=zones,
        total_sample_bytes=sum(sample_sizes.values()),
    )


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
    print(
        f"{path}: {bs.format}, {bs.preset_count} preset(s), "
        f"{bs.sample_count} sample(s), {bs.total_size:,} bytes"
    )

    if isinstance(bank, e4b.E4BFile) or isinstance(bank, eiii.EIIIFile):  # noqa: SIM101
        presets = bank.presets
    else:
        presets = list(bank.programs.values())
    for p in presets[:3]:
        ps = summarize_preset(bank, p)
        print(
            f"\n  {ps.name!r} — {ps.voice_count} voice(s)/keymap(s), {len(ps.zones)} zone(s)"
        )
        for z in ps.zones[:8]:
            print(
                f"    {z.sample_name!r:30s} key {z.lo_key:3d}-{z.hi_key:3d}  "
                f"vel {z.lo_vel:3d}-{z.hi_vel:3d}  root {z.root_key:3d}  loop={z.loop}"
            )
        if len(ps.zones) > 8:
            print(f"    … {len(ps.zones) - 8} more zone(s)")
    if len(presets) > 3:
        print(f"\n  … {len(presets) - 3} more preset(s)")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
