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

Usage:
    python3 tools/check_krz_banks.py PATH [PATH ...]

PATH may be a `.krz` file, a directory (searched recursively), or a disk
image / floppy image VinSamLib can read, in which case every KRZ bank
inside it is scanned. Exits 1 if anything was flagged.
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


# ── plumbing ─────────────────────────────────────────────────────────────────

def scan_bytes(data: bytes, label: str) -> bool:
    try:
        bank = krz.parse_bytes(data, label)
    except Exception as ex:
        print(f"  ?  {label}: not readable as KRZ ({ex})")
        return False

    shift = check_keymap_shift(bank)
    scribble = check_entry_scribble(bank)
    if not shift and not scribble:
        return False

    print(f"  ⚠  {label}")
    for f in shift:
        print(f"       KEYMAP-SHIFT   {f}")
    for f in scribble:
        print(f"       ENTRY-SCRIBBLE {f}")
    return True


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
    if len(argv) < 2:
        print(__doc__)
        return 2

    scanned = flagged = 0
    bad_path = False
    for arg in argv[1:]:
        p = Path(arg).expanduser()
        if not p.exists():
            # Exits 2, not 0: a mistyped path must never read as "all clear".
            print(f"  ?  {p}: no such path")
            bad_path = True
            continue
        print(f"Scanning {p} …")
        for label, data in _iter_targets(p):
            scanned += 1
            if scan_bytes(data, label):
                flagged += 1

    print(f"\n{scanned} KRZ bank(s) scanned, {flagged} flagged.")
    if flagged:
        print("Neither defect can be repaired in place: rebuild an affected "
              "bank from its source material with a current mpc2emu and "
              "VinSamLib.\nENTRY-SCRIBBLE is a byte pattern only the old "
              "assembler produced. KEYMAP-SHIFT is inference — on a bank you "
              "did not build yourself, confirm by ear before rebuilding.")
    if bad_path:
        return 2
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
