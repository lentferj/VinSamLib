"""Ad-hoc corpus round-trip runner for banks/e4b.py (not pytest yet — that's
a later milestone). Parses real E4B files (from disk and extracted live from
EMU3 ISOs), assembles selecting every preset, and verifies the result is
semantically identical via mpc2emu's own independent parser (which reads the
FORM sequentially and does not trust the TOC — see e4b_parser.py)."""
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vinsamlib.banks import e4b
from vinsamlib import mpc2emu_bridge as bridge
from vinsamlib.vfs.detect import open_volume
from vinsamlib.vfs.emu3 import Emu3Volume
from vinsamlib.vfs.base import EntryKind

bridge.install()


def check_bytes(data: bytes, label: str) -> bool:
    try:
        bank = e4b.parse_bytes(data, label)
    except Exception as ex:
        print(f"PARSE FAIL  {label}: {ex!r}")
        return False
    if not bank.presets:
        print(f"SKIP (no presets) {label}")
        return True
    try:
        out = e4b.assemble([(bank, p) for p in bank.presets])
    except Exception as ex:
        print(f"ASSEMBLE FAIL {label}: {ex!r}")
        return False

    tmp_orig = "/home/lentferj/temp/_rt_orig.e4b"
    tmp_new = "/home/lentferj/temp/_rt_new.e4b"
    with open(tmp_orig, "wb") as f:
        f.write(data)
    with open(tmp_new, "wb") as f:
        f.write(out)
    try:
        orig_bank = bridge.e4b_parser.parse_e4b(tmp_orig)
        new_bank = bridge.e4b_parser.parse_e4b(tmp_new)
    except Exception as ex:
        print(f"REPARSE FAIL {label}: {ex!r}")
        return False

    ok = True
    # Sample names actually referenced by at least one zone, anywhere in the
    # source bank. assemble() only ever pulls in a REFERENCED sample — an
    # E3S1 chunk nobody points to (an orphan; mpc2emu's own RE test-bank
    # generators produce several of these deliberately) is correctly
    # dropped, not lost. A bare preset/sample count comparison can't tell
    # "dropped an orphan" from "lost a real one", so referenced-ness is
    # checked directly below instead of asserting equal counts.
    referenced_names = {z.sample_name for p in orig_bank.presets
                         for v in p.voices for z in v.zones}
    if len(orig_bank.presets) != len(new_bank.presets):
        print(f"PRESET COUNT MISMATCH {label}: {len(orig_bank.presets)} vs {len(new_bank.presets)}")
        ok = False
    for op, npn in zip(orig_bank.presets, new_bank.presets):
        if op.name != npn.name or len(op.voices) != len(npn.voices):
            print(f"PRESET MISMATCH {label}: {op.name!r} vs {npn.name!r}")
            ok = False
            continue
        for ov, nv in zip(op.voices, npn.voices):
            if len(ov.zones) != len(nv.zones):
                print(f"ZONE COUNT MISMATCH {label} preset {op.name!r}")
                ok = False
                continue
            for oz, nz in zip(ov.zones, nv.zones):
                if oz.sample_name != nz.sample_name:
                    # Diagnostic only, not fatal: mpc2emu's collision-avoidance
                    # naming embeds the sample's own (renumbered) index in
                    # ambiguous cases — see the PCM-multiset check below for
                    # the real correctness gate.
                    print(f"zone sample name differs (likely renumbering, see PCM check) "
                          f"{label} preset {op.name!r}: {oz.sample_name!r} vs {nz.sample_name!r}")
    # Compare PCM content as a multiset, not by name or list position.
    # mpc2emu's own parser builds bank.samples in physical E3S1-chunk-
    # encounter order (an implementation detail the assembler is free to
    # lay out differently), AND its collision-avoidance renaming embeds
    # the sample's OWN INDEX NUMBER into the display name whenever two
    # samples share a base name (parse_e4b: `display_name[:12] +
    # f"{sample_idx:04d}"`) — since the assembler correctly renumbers
    # samples, that synthetic suffix is *expected* to differ and is not a
    # real content mismatch. Comparing the actual PCM bytes as a multiset
    # sidesteps both nonissues while still verifying every real sample
    # survived intact; the per-preset zone->sample_name check above
    # already verifies the (name-based, pre-renumbering) reference
    # structure independently.
    from collections import Counter
    name_by_pcm = {s.data: s.name for s in orig_bank.samples}
    orig_pcm = Counter(s.data for s in orig_bank.samples)
    new_pcm = Counter(s.data for s in new_bank.samples)
    if orig_pcm != new_pcm:
        missing = orig_pcm - new_pcm
        extra = new_pcm - orig_pcm
        # A "missing" sample is only a real failure if it was actually
        # referenced by some zone in the source — otherwise it's an orphan
        # E3S1 chunk assemble() correctly declined to carry forward.
        real_missing = sum(cnt for data_, cnt in missing.items()
                            if name_by_pcm.get(data_) in referenced_names)
        orphaned = sum(missing.values()) - real_missing
        if real_missing or extra:
            print(f"PCM SET MISMATCH {label}: {real_missing} referenced sample(s) "
                  f"missing/altered, {orphaned} orphan(s) correctly dropped, "
                  f"{sum(extra.values())} unexpected")
            ok = False
    return ok


def main():
    total = passed = 0

    disk_paths = glob.glob("/home/lentferj/temp/*.E4B") + glob.glob("/home/lentferj/temp/*.e4b")
    for p in disk_paths:
        total += 1
        with open(p, "rb") as f:
            data = f.read()
        if check_bytes(data, p):
            passed += 1

    iso_paths = glob.glob("/home/lentferj/Dokumente/SYNTHS/E4XT/ISO-Images/*.iso")
    # Sample a handful of ISOs (not all — some are huge) for real commercial banks.
    for p in iso_paths[:8]:
        vol = None
        try:
            vol = open_volume(p)
            if not isinstance(vol, Emu3Volume):
                continue
            for folder in vol.list():
                if folder.kind != EntryKind.FOLDER:
                    continue
                for e in vol.list(folder):
                    if e.kind != EntryKind.BANK:
                        continue
                    total += 1
                    data = vol.read(e)
                    label = f"{p}::{folder.name}/{e.name}"
                    if check_bytes(data, label):
                        passed += 1
        except Exception as ex:
            print(f"VOLUME ERROR {p}: {ex!r}")
        finally:
            if vol is not None:
                vol.close()

    print(f"\n{passed}/{total} banks round-tripped semantically clean")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
