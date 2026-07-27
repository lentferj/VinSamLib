"""Ad-hoc corpus round-trip runner for banks/eiii.py, mirroring
roundtrip_e4b_corpus.py / roundtrip_krz_corpus.py. mpc2emu's own EIII
*reader* validation (parsers/eiii_parser.py, see mpc2emu's
docs/RESOLUTION_NOTES.md §EIII) was read-only structural checking against
1118 real banks; this instead exercises banks/eiii.py's *raw* parse ->
assemble -> re-parse round trip (the container-level surgery Layer 1 does
for browsing/same-format reassembly), checking (1) no exception at any
stage, (2) preset/sample counts are sane, (3) referential integrity (every
zone's masked sample index resolves to an assembled sample), and (4) every
referenced sample's bytes (header + PCM, compared as a raw-body multiset --
dedup/renumbering can legitimately reorder, never alter content) survive.

Real commercial EIII/EIIIX/ESI banks aren't loose files here -- they live
inside CD-ROM .ISO images. Locating them doesn't need this project's own
EMU3/ISO9660 filesystem reader at all: scanning the raw image bytes for the
three 16-byte bank identifiers and slicing from each match to the next (or
a size cap) finds every bank boundary directly -- the exact technique
mpc2emu's own one-off corpus validation script used (see mpc2emu's
docs/RESOLUTION_NOTES.md §EIII "Real-world validation").
"""
import glob
import mmap
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vinsamlib.banks import eiii

_IDENTIFIERS = [b"EMULATOR THREE ", b"EMULATOR 3X    ", b"EMU SI-32 v3   "]
_SIZE_CAP = 130 * 1024 * 1024
_MAX_TOTAL_BANKS = 600   # a good-sized sample, not all ~1118 -- this test does
                          # real work (assemble + re-parse + content-compare)
                          # per bank, unlike mpc2emu's read-only corpus pass


def _find_bank_offsets(mm: mmap.mmap) -> list[int]:
    """Every offset where a 16-byte EIII/EIIIX/ESI bank identifier
    (15 chars + a confirmed NUL terminator, same test detect_format()
    itself applies) starts."""
    offsets = []
    for ident in _IDENTIFIERS:
        start = 0
        while True:
            pos = mm.find(ident, start)
            if pos == -1:
                break
            if pos + 15 < len(mm) and mm[pos + 15] == 0:
                offsets.append(pos)
            start = pos + 1
    return sorted(offsets)


def check_bytes(data: bytes, label: str) -> bool:
    try:
        bank = eiii.parse_bytes(data, label)
    except Exception as ex:
        print(f"PARSE FAIL  {label}: {ex!r}")
        return False
    if not bank.presets:
        print(f"SKIP (no presets) {label}")
        return True

    try:
        selection = [(bank, p) for p in bank.presets]
        out = eiii.assemble(selection)
    except Exception as ex:
        print(f"ASSEMBLE FAIL {label} ({len(bank.presets)} preset(s), "
              f"{len(bank.samples)} sample(s)): {ex!r}")
        return False

    try:
        new_bank = eiii.parse_bytes(out, label + " (reassembled)")
    except Exception as ex:
        print(f"REPARSE FAIL {label}: {ex!r}")
        return False

    ok = True
    if len(new_bank.presets) != len(bank.presets):
        print(f"PRESET COUNT MISMATCH {label}: {len(bank.presets)} vs {len(new_bank.presets)}")
        ok = False

    # Real commercial banks in this corpus genuinely ship presets with
    # zone references that don't resolve to any sample IN THAT SAME BANK
    # (e.g. a "Temp" preset carried over from a larger master library
    # without its full sample set) -- assemble() correctly leaves such a
    # dangling reference's bytes untouched, same policy banks/e4b.py's own
    # assemble() uses, rather than inventing content. So a dangling ref is
    # only a real round-trip bug if the ORIGINAL bank didn't already have
    # it -- comparing new-vs-original (not asserting zero) is the same
    # "orphan tolerance" the E4B/KRZ corpus tests already apply.
    # assemble() preserves selection order and parse_bytes() always yields
    # presets in ascending physical-slot order, so new_bank.presets[k]
    # corresponds to the k-th selected (== original) preset positionally --
    # NOT by `.index`, which is renumbered by assemble() (a fresh, 0-based,
    # sequential slot assignment unrelated to the source bank's own slots).
    if len(new_bank.presets) == len(bank.presets):
        for orig_p, new_p in zip(bank.presets, new_bank.presets):
            orig_dangling = {sid for sid in orig_p.sample_indices if sid not in bank.samples}
            for sid in new_p.sample_indices:
                if sid not in new_bank.samples and sid not in orig_dangling:
                    print(f"NEW DANGLING SAMPLE REF {label}: preset {new_p.name!r} -> sample {sid}")
                    ok = False

    # Sample content as a (header+PCM) multiset, not keyed by name: real
    # EIII banks commonly reuse generic sample names -- same reasoning the
    # E4B/KRZ corpus tests already use for switching away from name-keying.
    orig_multiset = Counter(s.body for s in bank.samples.values())
    new_multiset = Counter(s.body for s in new_bank.samples.values())
    referenced_bodies = set()
    for p in bank.presets:
        for sid in p.sample_indices:
            samp = bank.samples.get(sid)
            if samp is not None:
                referenced_bodies.add(samp.body)
    missing = orig_multiset - new_multiset
    real_missing = sum(cnt for body, cnt in missing.items() if body in referenced_bodies)
    if real_missing:
        print(f"SAMPLE MISMATCH {label}: {real_missing} referenced sample(s) missing/altered")
        ok = False

    return ok


def main() -> int:
    total = passed = 0
    iso_paths = sorted(set(
        glob.glob("/home/lentferj/Dokumente/SYNTHS/E4XT/*.ISO") +
        glob.glob("/home/lentferj/Dokumente/SYNTHS/E4XT/ISO-Images/*.iso")))

    for path in iso_paths:
        if total >= _MAX_TOTAL_BANKS:
            break
        try:
            with open(path, "rb") as f:
                mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                try:
                    offsets = _find_bank_offsets(mm)
                    for i, off in enumerate(offsets):
                        if total >= _MAX_TOTAL_BANKS:
                            break
                        next_off = offsets[i + 1] if i + 1 < len(offsets) else len(mm)
                        end = min(next_off, off + _SIZE_CAP, len(mm))
                        data = mm[off:end]
                        total += 1
                        label = f"{path}@{off}"
                        if check_bytes(data, label):
                            passed += 1
                finally:
                    mm.close()
        except Exception as ex:
            print(f"FILE ERROR {path}: {ex!r}")

    print(f"\n{passed}/{total} EIII banks round-tripped semantically clean")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
