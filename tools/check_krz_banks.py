#!/usr/bin/env python3
"""Scan already-built KRZ banks for two defects that leave no visible trace.

Both produce a `.KRZ` file that parses cleanly, re-reads correctly, and
looks entirely normal — the damage only shows on a K2000, or not at all.
Neither can be repaired in place; the answer to both is to rebuild the
bank from its source, which is why this only reports.

  KEYMAP-SHIFT  Multisample banks written by mpc2emu before 2026-08-02.
                The K2000 sounds keymap entry `i` at MIDI key `i + 12`,
                and the writer put each zone at `entry[key]` instead of
                `entry[key - 12]`, so the program plays ONE sample
                key-tracked across the keyboard instead of the right
                sample per key. Fixed upstream in mpc2emu `791364a`.

  ENTRY-SCRIBBLE
                Banks assembled by VinSamLib before 2026-08-03 from a
                source program whose keymap is *compacted*. Assembly
                walked keymap entries at a fixed 5-byte stride and wrote
                a remapped sample id into each, which on a compacted
                keymap (no per-entry ids at all) overwrote tuning and
                subSample bytes instead. Fixed in this project's
                `685179f`.

  SOURCE-DRIFT  Only with `--against`. Compares each keymap against the
                bank it was built from. A conversion may renumber
                samples, rename objects and re-encode PCM, but it must
                not move where the keyboard is split — so unlike
                KEYMAP-SHIFT above, any difference here is a fact rather
                than an inference. This is what catches damage that is
                not a clean 12-entry shift, including the pre-fix KRZ→KRZ
                round trip that dropped a zone outright.

Usage:
    python3 tools/check_krz_banks.py PATH [PATH ...]
    python3 tools/check_krz_banks.py --against SOURCE PATH [PATH ...]

PATH may be a `.krz` file, a directory (searched recursively), or a disk
image / floppy image VinSamLib can read, in which case every KRZ bank
inside it is scanned.

SOURCE is the bank the scanned files were built from — either the `.krz`
of a KRZ→KRZ conversion, or the `.e4b` an E4B→KRZ one started from (that
form needs a configured mpc2emu, for its zone model). A bank the source
cannot account for is reported as *not compared*, not as a defect, and
does not affect the exit code; give each source its own run when a batch
mixes them. Comparing against an unrelated bank tells you nothing, which
is why this is opt-in.

Only the splits *between* zones are compared, never the outer edges: a
KRZ keymap always fills all 128 entries, so a source zone reaching the
end of the keyboard and one stopping short both end at entry 127. An
E4B source with velocity-split zones is skipped rather than guessed at,
since only the first velocity table is read here.

Exits 0 clean, 1 flagged, 2 on a bad path or bad arguments.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vinsamlib.banks import krz

_IMAGE_SUFFIXES = (".img", ".iso", ".hda", ".hfe", ".ima")
_BANK_SUFFIXES = (".krz", ".k25", ".k26")


# ── detectors ────────────────────────────────────────────────────────────────

def _zones_of(body: bytes, lay: krz.KeymapLayout) -> list[tuple[int, int, int]]:
    """Runs of consecutive entries sharing a sample id, as
    (first_entry, last_entry, sample_id). Compacted keymaps have exactly
    one run by definition and are never multisample."""
    if lay.id_off is None:
        return []
    ids = []
    for k in range(lay.num_keys):
        p = lay.table + k * lay.stride + lay.id_off
        if p + 2 > len(body):
            break
        ids.append(struct.unpack_from(">H", body, p)[0])

    runs, lo, prev = [], 0, None
    for i, sid in enumerate(ids):
        if sid != prev:
            if prev:
                runs.append((lo, i - 1, prev))
            lo, prev = i, sid
    if prev:
        runs.append((lo, len(ids) - 1, prev))
    return runs


def _looks_mpc2emu_written(body: bytes) -> bool:
    """mpc2emu's keymap write form, exactly: method 0x13, basePitch 0,
    100 cents per entry, 128 entries, entrySize 5 (§KRZKEYMAP records this
    header as byte-identical to real multi-sample keymaps, which is why the
    root test below is needed on top of it).

    Scoping the check this way matters: a K2000 authored the banks in a
    normal library, and where *it* puts a sample's root relative to a zone
    is its own business. Only mpc2emu could have written the shift."""
    if len(body) < krz.KEYMAP_HDR_FIXED:
        return False
    _sid, method, base, cents, entries_per_vel, entry_size = \
        struct.unpack_from(">6h", body, 0)
    return ((method & 0xFFFF) == 0x13 and base == 0 and cents == 100
            and entries_per_vel == 127 and entry_size == 5)


def check_keymap_shift(bank: krz.KrzFile) -> list[str]:
    """Flag multisample keymaps that mpc2emu wrote 12 entries too high.

    Decided per zone by where the zone's own sample root note falls. A
    correctly written zone covers entries [lo, hi] sounding at keys
    [lo+12, hi+12], and mpc2emu maps each sample so its root is inside its
    own zone — so the root should land in [lo+12, hi+12]. If it lands in
    [lo, hi] instead, the zone was written 12 entries high.

    That test is evidence, not proof — it is the same root-inside-zone
    reading whose corpus statistics (39.6% vs 26.4%) were too weak to
    settle the entry base at all, and unrestricted it misfires on 115
    keymaps of this project's own library. So it is only applied to
    keymaps carrying mpc2emu's own write form, a zone that cannot
    distinguish the two abstains silently, and a majority of the keymap
    has to agree. What that leaves, measured over 2289 hardware-sourced
    banks: **2 flagged**, both from converted commercial sets rather than
    K2000-authored floppies. So treat a KEYMAP-SHIFT flag as "play this
    one and listen", not as a verdict — unlike ENTRY-SCRIBBLE, which
    matches a byte pattern only the old assembler produces and flagged
    none of those 2289.

    **It detects a clean +12 shift, not keymap damage in general.**
    Measured 2026-08-03 against the pre-fix hardware-confirmation batch: a
    KRZ→KRZ row built before the fix came out with 4 zones where its
    source has 5, and boundaries matching neither the source nor a uniform
    shift — because the old *reader* was off by 12 too, so a KRZ→KRZ round
    trip mangled the keymap instead of translating it. This check stayed
    silent on that. A missing flag means "no clean shift signature", not
    "this bank is fine". To verify a KRZ→KRZ rebuild, compare its keymap
    entry boundaries against the source's: they should match exactly."""
    findings = []
    for kid, km in bank.keymaps.items():
        body = km.body()
        lay = krz.keymap_layout(body)
        if lay is None or not _looks_mpc2emu_written(body):
            continue
        runs = _zones_of(body, lay)
        if len({sid for _, _, sid in runs}) < 2:
            continue                      # single-sample: unaffected either way

        shifted = correct = 0
        for lo, hi, sid in runs:
            samp = bank.samples.get(sid)
            if samp is None:
                continue
            b = samp.body()
            if len(b) <= krz.SAMPLE_HDR:
                continue
            root = b[krz.SAMPLE_HDR]      # Soundfilehead byte 0
            in_shifted = lo <= root <= hi
            in_correct = lo + 12 <= root <= hi + 12
            if in_shifted and not in_correct:
                shifted += 1
            elif in_correct and not in_shifted:
                correct += 1

        # The writer shifted every zone alike, so a real case shows the
        # shift across MOST of the keymap. Sporadic hits are the root test
        # being noisy on content it cannot really judge: requiring a
        # majority is what takes this from 20 false positives on a
        # hardware-authored library down to none, while still catching a
        # synthesized pre-fix bank at 3 zones of 4.
        if shifted > correct and shifted >= 2 and shifted * 2 >= len(runs):
            findings.append(
                f"keymap {kid} ({km.name.strip()!r}): {len(runs)} zones, "
                f"{shifted} place their sample's root 12 semitones high"
                + (f" against {correct} that don't" if correct else ""))
    return findings


def check_entry_scribble(bank: krz.KrzFile) -> list[str]:
    """Flag compacted keymaps bearing the old assembler's write pattern.

    The buggy loop wrote two bytes at body offset 30 + 5k for k in
    0..127 — almost always 0x0000, since a sample id it did not
    recognise remapped to zero. On a compacted keymap those offsets are
    tuning/subSample bytes, so the signature is zero pairs appearing on
    that exact 5-byte grid far more often than off it."""
    findings = []
    for kid, km in bank.keymaps.items():
        body = km.body()
        lay = krz.keymap_layout(body)
        if lay is None or lay.id_off is not None:
            continue                      # only compacted keymaps were hit

        end = min(len(body) - 1, krz.KEYMAP_HDR + 2 + (krz.NUM_KEYS - 1) * 5)
        on = [p for p in range(krz.KEYMAP_HDR + 2, end, 5)]
        if len(on) < 20:
            continue
        on_zero = sum(1 for p in on if body[p] == 0 and body[p + 1] == 0)

        off = [p for p in range(krz.KEYMAP_HDR + 2, end)
               if (p - (krz.KEYMAP_HDR + 2)) % 5 and p + 1 < len(body)]
        off_zero = sum(1 for p in off if body[p] == 0 and body[p + 1] == 0)
        on_rate = on_zero / len(on)
        off_rate = (off_zero / len(off)) if off else 0.0

        # A real compacted table has no reason to zero every fifth pair.
        if on_rate > 0.5 and on_rate > off_rate * 2 + 0.25:
            findings.append(
                f"keymap {kid} ({km.name.strip()!r}): compacted, but "
                f"{on_zero}/{len(on)} of the old writer's slots are zeroed "
                f"({on_rate:.0%} on the 5-byte grid vs {off_rate:.0%} off it)")
    return findings


# ── comparison against the source a bank was built from ──────────────────────

def _boundaries(km: krz.KrzObject) -> list[tuple[int, int]] | None:
    """A keymap's entry runs as (first_entry, last_entry) pairs, ignoring
    which sample each names. Two keymaps describing the same instrument
    must split the keyboard at the same places, whatever the samples were
    renumbered to."""
    body = km.body()
    lay = krz.keymap_layout(body)
    if lay is None:
        return None
    if lay.id_off is None:
        return [(0, lay.num_keys - 1)]      # compacted: one run by definition

    ids = []
    for k in range(lay.num_keys):
        p = lay.table + k * lay.stride + lay.id_off
        if p + 2 > len(body):
            break
        ids.append(struct.unpack_from(">H", body, p)[0])
    if not ids:
        return None

    runs, lo, prev = [], 0, ids[0]
    for i, sid in enumerate(ids[1:], 1):
        if sid != prev:
            runs.append((lo, i - 1))
            lo, prev = i, sid
    runs.append((lo, len(ids) - 1))
    return runs


def _internal_splits(runs: list[tuple[int, int]]) -> list[int]:
    """Entry indices where one run gives way to the next.

    The outer edges carry no information: a KRZ keymap always fills all
    128 entries, so a source zone reaching the end of the keyboard and one
    stopping short both come out as a run ending at 127. Only the splits
    between runs reflect a decision the writer had to get right."""
    return [lo for lo, _hi in runs[1:]]


def _expected_splits_from_voice(voice) -> list[int] | None:
    """Where a source E4B voice's zones say the KRZ keymap must split.

    Zones are in MIDI keys and keymap entries run 12 lower, so a boundary
    at key K belongs at entry K-12. Adjacent zones naming the same sample
    produce no boundary at all — the K2000 keymap stores one id per entry,
    so they merge into a single run. Returns None when the voice is
    velocity-split, since only the first velocity table is read here and a
    comparison would be against the wrong data."""
    zones = sorted(voice.zones, key=lambda z: (z.lo_key, z.lo_vel))
    if not zones:
        return None
    if any((z.lo_vel, z.hi_vel) != (zones[0].lo_vel, zones[0].hi_vel)
           for z in zones):
        return None

    splits = []
    for prev, cur in zip(zones, zones[1:]):
        if cur.sample_name == prev.sample_name:
            continue                      # merges into one run
        entry = cur.lo_key - krz.KEYMAP_ENTRY_NOTE_OFFSET
        if 0 < entry < krz.NUM_KEYS:      # outside the table, unrepresentable
            splits.append(entry)
    return sorted(set(splits))


def check_against_e4b(bank: krz.KrzFile, source) -> tuple[list[str], list[str]]:
    """Compare KRZ keymaps against the E4B preset they were converted from.

    mpc2emu writes one keymap per voice layer and names it after the
    preset, so the match is by name and then by whichever of that preset's
    voices fits — several voices can legitimately split the same way."""
    by_name: dict[str, list] = {}
    for preset in source.presets:
        by_name.setdefault(_norm(preset.name), []).append(preset)

    findings: list[str] = []
    unverified: list[str] = []
    for kid, km in bank.keymaps.items():
        runs = _boundaries(km)
        if runs is None:
            continue
        presets = by_name.get(_norm(km.name), [])
        if not presets and len(source.presets) == 1:
            presets = list(source.presets)
        if not presets:
            unverified.append(f"keymap {kid} ({km.name.strip()!r})")
            continue

        expected = [s for p in presets for v in p.voices
                    if (s := _expected_splits_from_voice(v)) is not None]
        if not expected:
            unverified.append(
                f"keymap {kid} ({km.name.strip()!r}, velocity-split source)")
            continue

        mine = _internal_splits(runs)
        if any(mine == e for e in expected):
            continue

        best = min(expected, key=lambda e: abs(len(e) - len(mine)))
        if len(best) == len(mine) and mine:
            deltas = {m - b for m, b in zip(mine, best)}
            if len(deltas) == 1:
                k = deltas.pop()
                detail = (f"every split moved by {k:+d} entries — the "
                          f"off-by-12 signature" if abs(k) == 12 else
                          f"every split moved by {k:+d} entries")
            else:
                detail = f"{len(mine)} splits, in different places"
        else:
            detail = (f"{len(mine)} split(s) here vs {len(best)} the source "
                      f"calls for")
        findings.append(
            f"keymap {kid} ({km.name.strip()!r}): {detail}\n"
            f"           built:  {mine}\n"
            f"           source: {best}")
    return findings, unverified


def _norm(name: str) -> str:
    """Keymap names survive conversion with punctuation churn — a source
    `*Flugelhorn` comes back as `*>Flugelhorn` — so match on letters and
    digits only."""
    return "".join(c for c in name.lower() if c.isalnum())


def check_against_source(bank: krz.KrzFile,
                         source: krz.KrzFile) -> tuple[list[str], list[str]]:
    """Compare each keymap's entry boundaries with its counterpart in the
    bank this one was built from.

    This is the exact check the inferential KEYMAP-SHIFT one cannot be: a
    conversion may renumber samples, rename objects and re-encode PCM, but
    it must not move where the keyboard is split. Any difference is real,
    and a uniform difference is the off-by-12 signature outright.

    Only meaningful against the actual source. Pointed at an unrelated
    bank it can compare nothing, which is why it is an explicit flag
    rather than something the scan does on its own.

    Returns (findings, unverified). A keymap with no counterpart is
    **not** a finding — a batch built from several different sources will
    always have some, and calling that damage would make the exit code
    useless as a gate."""
    by_name: dict[str, list[krz.KrzObject]] = {}
    for km in source.keymaps.values():
        by_name.setdefault(_norm(km.name), []).append(km)

    findings: list[str] = []
    unverified: list[str] = []
    for kid, km in bank.keymaps.items():
        mine = _boundaries(km)
        if mine is None:
            continue
        candidates = by_name.get(_norm(km.name), [])
        if not candidates and len(source.keymaps) == 1:
            candidates = list(source.keymaps.values())
        if not candidates:
            unverified.append(f"keymap {kid} ({km.name.strip()!r})")
            continue

        # Any source keymap splitting the keyboard the same way is a match;
        # several programs can legitimately share one keymap's shape.
        theirs = [b for b in (_boundaries(c) for c in candidates) if b]
        if any(b == mine for b in theirs):
            continue

        best = min(theirs, key=lambda b: abs(len(b) - len(mine)), default=None)
        if best is None:
            continue
        detail = f"{len(mine)} zones here vs {len(best)} in the source"
        if len(best) == len(mine):
            # Interior boundaries only: shifting inside a fixed-size entry
            # table pins the first run's start at 0 and the last run's end
            # at the final entry, so those two never move and would hide an
            # otherwise perfectly uniform shift.
            deltas = {m[0] - s[0] for m, s in zip(mine[1:], best[1:])} | \
                     {m[1] - s[1] for m, s in zip(mine[:-1], best[:-1])}
            if len(deltas) == 1:
                k = deltas.pop()
                detail = (f"every boundary moved by {k:+d} entries — the "
                          f"off-by-12 signature" if abs(k) == 12 else
                          f"every boundary moved by {k:+d} entries")
            else:
                detail = f"{len(mine)} zones, boundaries differ"
        findings.append(
            f"keymap {kid} ({km.name.strip()!r}): {detail}\n"
            f"           built:  {mine}\n"
            f"           source: {best}")
    return findings, unverified


# ── plumbing ─────────────────────────────────────────────────────────────────

def load_source(path: Path):
    """The bank the scanned KRZ files were built from: another `.krz`, or
    an `.e4b` they were converted from. An E4B needs mpc2emu's parser for
    the zone model — the KRZ side needs nothing but this project."""
    if path.suffix.lower() in (".e4b",):
        from vinsamlib.mpc2emu_bridge import e4b_parser
        return "e4b", e4b_parser.parse_e4b(str(path))
    return "krz", krz.parse(str(path))


def scan_bytes(data: bytes, label: str, source=None,
               source_kind: str = "krz") -> tuple[bool, bool]:
    """Returns (flagged, had_unverified_keymaps)."""
    try:
        bank = krz.parse_bytes(data, label)
    except Exception as ex:
        print(f"  ?  {label}: not readable as KRZ ({ex})")
        return False, False

    shift = check_keymap_shift(bank)
    scribble = check_entry_scribble(bank)
    if source is None:
        drift, unverified = [], []
    elif source_kind == "e4b":
        drift, unverified = check_against_e4b(bank, source)
    else:
        drift, unverified = check_against_source(bank, source)

    if shift or scribble or drift:
        print(f"  ⚠  {label}")
        for f in shift:
            print(f"       KEYMAP-SHIFT   {f}")
        for f in scribble:
            print(f"       ENTRY-SCRIBBLE {f}")
        for f in drift:
            print(f"       SOURCE-DRIFT   {f}")
        return True, bool(unverified)

    if unverified:
        # Not a defect: this bank came from some other source. Say so once
        # per bank rather than once per keymap, and keep it out of the count.
        print(f"  ·  {label}: not built from this source, "
              f"{len(unverified)} keymap(s) not compared")
    return False, bool(unverified)


def _iter_targets(path: Path):
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_file():
                yield from _iter_targets(p)
        return
    suffix = path.suffix.lower()
    if suffix in _BANK_SUFFIXES:
        yield path.name, path.read_bytes()
    elif suffix in _IMAGE_SUFFIXES:
        try:
            from vinsamlib.vfs.detect import sniff
            from vinsamlib.vfs.base import EntryKind
            cls = sniff(str(path))
            if cls is None:
                return
            with cls(str(path)) as vol:
                for e in vol.list():
                    if (e.kind == EntryKind.BANK
                            and e.name.lower().strip().endswith(_BANK_SUFFIXES)):
                        yield f"{path.name}::{e.name}", vol.read(e)
        except Exception as ex:
            print(f"  ?  {path}: image not readable ({ex})")


def main(argv: list[str]) -> int:
    args, paths, against = argv[1:], [], None
    while args:
        a = args.pop(0)
        if a in ("-h", "--help"):
            print(__doc__)
            return 0
        if a == "--against":
            if not args:
                print("--against needs the path of the source bank")
                return 2
            against = args.pop(0)
        elif a.startswith("--against="):
            against = a.split("=", 1)[1]
        else:
            paths.append(a)
    if not paths:
        print(__doc__)
        return 2

    source, source_kind = None, "krz"
    if against is not None:
        sp = Path(against).expanduser()
        if not sp.exists():
            print(f"  ?  {sp}: no such source bank")
            return 2
        try:
            source_kind, source = load_source(sp)
        except Exception as ex:
            print(f"  ?  {sp}: source not readable ({ex})")
            return 2
        n = (len(source.presets) if source_kind == "e4b"
             else len(source.keymaps))
        print(f"Comparing against {sp.name} "
              f"({source_kind.upper()}, {n} "
              f"{'preset' if source_kind == 'e4b' else 'keymap'}(s))")

    scanned = flagged = uncompared = 0
    bad_path = False
    for arg in paths:
        p = Path(arg).expanduser()
        if not p.exists():
            # Exits 2, not 0: a mistyped path must never read as "all clear".
            print(f"  ?  {p}: no such path")
            bad_path = True
            continue
        print(f"Scanning {p} …")
        for label, data in _iter_targets(p):
            scanned += 1
            hit, skipped = scan_bytes(data, label, source, source_kind)
            flagged += hit
            uncompared += skipped

    print(f"\n{scanned} KRZ bank(s) scanned, {flagged} flagged.")
    if uncompared:
        print(f"{uncompared} were not built from the given source and were "
              f"checked by the other two detectors only — pass each source "
              f"with its own --against run to compare those too.")
    if flagged:
        print("Neither defect can be repaired in place: rebuild an affected "
              "bank from its source material with a current mpc2emu and "
              "VinSamLib.\nENTRY-SCRIBBLE is a byte pattern only the old "
              "assembler produced, and SOURCE-DRIFT is an exact comparison. "
              "KEYMAP-SHIFT is inference — on a bank you did not build "
              "yourself, confirm by ear before rebuilding.")
    if bad_path:
        return 2
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
