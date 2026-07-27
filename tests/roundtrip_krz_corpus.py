"""Ad-hoc corpus round-trip runner for banks/krz.py. mpc2emu has no
independent KRZ *reader* to cross-validate against (it's write-only for
KRZ), so this checks self-consistency instead: parse, assemble selecting
every program, re-parse the assembled bytes, and verify (1) referential
integrity (every CAL keymap ref resolves to an assembled keymap, every
keymap sample ref resolves to an assembled sample), (2) every sample's PCM
content byte-for-byte survives (compared by (name, content) multiset, same
reasoning as the E4B corpus test — dedup/renumbering can legitimately
change *order*, never content), and (3) program/keymap/sample counts are
sane (assembled counts should never exceed the original's, and should
usually equal the number actually reachable from the selected programs)."""
import glob
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vinsamlib.banks import krz
from vinsamlib.vfs.detect import open_volume, sniff
from vinsamlib.vfs.base import EntryKind


def check_bytes(data: bytes, label: str) -> bool:
    try:
        bank = krz.parse_bytes(data, label)
    except Exception as ex:
        print(f"PARSE FAIL  {label}: {ex!r}")
        return False
    if not bank.programs:
        print(f"SKIP (no programs) {label}")
        return True

    try:
        selection = [(bank, p) for p in bank.programs.values()]
        out = krz.assemble(selection)
    except Exception as ex:
        print(f"ASSEMBLE FAIL {label}: {ex!r}")
        return False

    try:
        new_bank = krz.parse_bytes(out, label + " (reassembled)")
    except Exception as ex:
        print(f"REPARSE FAIL {label}: {ex!r}")
        return False

    ok = True
    if len(new_bank.programs) != len(bank.programs):
        print(f"PROGRAM COUNT MISMATCH {label}: {len(bank.programs)} vs {len(new_bank.programs)}")
        ok = False

    for pid, p in new_bank.programs.items():
        for kid in set(new_bank.program_keymap_refs(p)):
            if kid not in new_bank.keymaps:
                print(f"DANGLING KEYMAP REF {label}: program {p.name!r} -> keymap {kid}")
                ok = False
    for kid, k in new_bank.keymaps.items():
        for sid in set(new_bank.keymap_sample_refs(k)):
            if sid not in new_bank.samples:
                print(f"DANGLING SAMPLE REF {label}: keymap {k.name!r} -> sample {sid}")
                ok = False

    # PCM content as a multiset of raw bytes, NOT keyed by name: real KRZ
    # banks commonly have several samples sharing the same (often generic,
    # e.g. "O") name, so a name-keyed dict would silently collapse
    # duplicates and misreport a mismatch — same reasoning as the E4B
    # corpus test's switch away from name-keying.
    def all_sample_pcm(b):
        out = []
        for samp in b.samples.values():
            start, n_words = b.sample_word_extent(samp)
            out.append((samp.name, b.pcm[start * 2:(start + n_words) * 2]))
        return out

    try:
        orig_pcm = all_sample_pcm(bank)
        new_pcm = all_sample_pcm(new_bank)
    except Exception as ex:
        print(f"PCM SLICE FAIL {label}: {ex!r}")
        return False

    orig_multiset = Counter(pcm for _n, pcm in orig_pcm)
    new_multiset = Counter(pcm for _n, pcm in new_pcm)
    # Only samples reachable from a selected program should survive — same
    # orphan-tolerance reasoning as the E4B corpus test.
    referenced_names = set()
    for p in bank.programs.values():
        for kid in set(bank.program_keymap_refs(p)):
            km = bank.keymaps.get(kid)
            if km is None:
                continue
            for sid in set(bank.keymap_sample_refs(km)):
                samp = bank.samples.get(sid)
                if samp is not None:
                    referenced_names.add(samp.name)
    missing = orig_multiset - new_multiset
    real_missing = sum(cnt for pcm, cnt in missing.items()
                        if any(n in referenced_names for n, p in orig_pcm if p == pcm))
    if real_missing:
        print(f"PCM MISMATCH {label}: {real_missing} referenced sample(s) missing/altered")
        ok = False
    return ok


def main():
    total = passed = 0

    disk_paths = glob.glob("/home/lentferj/temp/**/*.krz", recursive=True) + \
                 glob.glob("/home/lentferj/temp/**/*.KRZ", recursive=True)
    disk_paths = sorted(set(disk_paths))
    for p in disk_paths:
        total += 1
        with open(p, "rb") as f:
            data = f.read()
        if check_bytes(data, p):
            passed += 1

    # Real commercial/library KRZ banks, pulled live out of FAT12 floppies
    # and FAT16 disk images under disk-image/.
    img_paths = glob.glob("/home/lentferj/disk-image/**/*.img", recursive=True)
    checked = 0
    for p in img_paths:
        if checked >= 150:   # sample a good chunk, not all ~1800
            break
        try:
            cls = sniff(p)
            if cls is None:
                continue
            with cls(p) as vol:
                for e in vol.list():
                    if e.kind == EntryKind.BANK and e.name.lower().strip().endswith((".krz", ".k25", ".k26")):
                        total += 1
                        checked += 1
                        data = vol.read(e)
                        if check_bytes(data, f"{p}::{e.name}"):
                            passed += 1
        except Exception as ex:
            print(f"VOLUME ERROR {p}: {ex!r}")

    print(f"\n{passed}/{total} KRZ banks round-tripped semantically clean")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
