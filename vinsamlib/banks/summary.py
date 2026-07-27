"""
Plain-data summaries of a bank/preset for the UI (Detail pane, Samples pane).

No Qt, no printing/ANSI — just dataclasses the UI renders directly. Two very
different strategies per format, because only one of them has an existing
semantic reader to lean on:

- **E4B**: `banks/e4b.py`'s own container objects don't carry zone semantics
  (key/vel range, root, loop) — only what's needed for byte-level assembly.
  Rather than re-deriving that from raw bytes a second time, this reuses
  `banks.e4b.assemble()` to build a throwaway one-preset E4B file, then hands
  it to mpc2emu's own `parsers.e4b_parser.parse_e4b()` for the rich,
  hardware-accurate `models.common` zone model — the same reuse-not-reinvent
  approach the rest of this project takes toward mpc2emu.
- **KRZ**: mpc2emu has no semantic KRZ reader (it's write-only), so this walks
  `banks/krz.py`'s own reference graph directly: each referenced keymap's 128
  raw key entries are collapsed into runs of consecutive keys pointing at the
  same sample, since a K2000 keymap has no explicit key-range field at all
  (KRZ_FORMAT.md §3.2) — each of the 128 keys just names its own sample.
"""

from __future__ import annotations

import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import e4b, krz
from ..mpc2emu_bridge import e4b_parser

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
    format: str                      # 'E4B' | 'KRZ'
    voice_count: int                 # voices (E4B) / keymaps referenced (KRZ)
    zones: list[ZoneSummary] = field(default_factory=list)
    total_sample_bytes: int = 0      # unique samples referenced by this preset's zones


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
    total_sample_bytes = sum(len(bank.samples[sid].block) for sid in sample_ids)
    return PresetSummary(name=prog.name.strip(), format="KRZ",
                          voice_count=len(keymap_ids), zones=zones,
                          total_sample_bytes=total_sample_bytes)


def _keymap_zone_runs(bank: krz.KrzFile, km: krz.KrzObject) -> tuple[list[ZoneSummary], set[int]]:
    """Collapse a keymap's 128 individual key->sample entries into runs of
    consecutive keys sharing the same sample id.

    Real hardware-saved banks can carry keymap slots pointing at a sample id
    that doesn't exist in this bank at all — leftover/uninitialized entries
    from editing on the K2000 itself, not real content (seen in this
    project's own library, e.g. ids like 256 or 51201 sitting beside
    genuinely-referenced samples in the same keymap). Those runs are
    dropped rather than shown as `<sample NNNN>` noise — a librarian is
    for finding real, playable content, not surfacing hardware artifacts."""
    body = km.body()
    sample_by_key = []
    for k in range(krz.NUM_KEYS):
        eo = krz.KEYMAP_HDR + k * krz.KEYMAP_ENTRY_SIZE
        if eo + krz.KEYMAP_ENTRY_SIZE > len(body):
            sample_by_key.append(0)
            continue
        sample_by_key.append(struct.unpack_from(">H", body, eo + 2)[0])

    runs: list[ZoneSummary] = []
    sample_ids: set[int] = set()
    lo, prev_sid = None, None
    for key in range(krz.NUM_KEYS):
        sid = sample_by_key[key]
        if sid != prev_sid:
            if prev_sid and prev_sid in bank.samples:
                runs.append(_krz_zone(bank, prev_sid, lo, key - 1))
                sample_ids.add(prev_sid)
            lo, prev_sid = key, sid
    if prev_sid and prev_sid in bank.samples:
        runs.append(_krz_zone(bank, prev_sid, lo, krz.NUM_KEYS - 1))
        sample_ids.add(prev_sid)
    return runs, sample_ids


def _krz_zone(bank: krz.KrzFile, sid: int, lo_key: int, hi_key: int) -> ZoneSummary:
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
                        lo_vel=0, hi_vel=127, root_key=root_key, loop=loop)


# ── generic dispatch (what the UI actually calls) ───────────────────────────

def summarize_bank(bank) -> BankSummary:
    if isinstance(bank, e4b.E4BFile):
        return summarize_e4b_bank(bank)
    if isinstance(bank, krz.KrzFile):
        return summarize_krz_bank(bank)
    raise TypeError(f"not a recognised bank type: {type(bank)!r}")


def summarize_preset(bank, obj) -> PresetSummary:
    if isinstance(bank, e4b.E4BFile):
        return summarize_e4b_preset(bank, obj)
    if isinstance(bank, krz.KrzFile):
        return summarize_krz_program(bank, obj)
    raise TypeError(f"not a recognised bank type: {type(bank)!r}")


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
    else:
        print(f"{path}: not a recognised bank (no FORM...E4B0 or PRAM header)")
        return 1

    bs = summarize_bank(bank)
    print(f"{path}: {bs.format}, {bs.preset_count} preset(s), "
          f"{bs.sample_count} sample(s), {bs.total_size:,} bytes")

    presets = bank.presets if isinstance(bank, e4b.E4BFile) else list(bank.programs.values())
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
