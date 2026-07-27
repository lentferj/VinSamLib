"""Manual smoke test for M6's build/images.py safety wrapper: create /
append / delete / rename / export against real E4B and KRZ banks from the
library. No Qt here — this is the pure-Python layer image_pane.py sits on
top of. Per mpc2emu/CLAUDE.md, output goes to ~/temp/, never /tmp/.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vinsamlib import mpc2emu_bridge
from vinsamlib.build import images
from vinsamlib.config import Config
from vinsamlib.vfs.base import EntryKind
from vinsamlib.vfs.detect import open_volume

OUT = Path.home() / "temp" / "vinsamlib_m6"
OUT.mkdir(parents=True, exist_ok=True)

E4B_DIR = Path.home() / "Dokumente/SYNTHS/E4XT/E4Bs/Rob.Papen-Techno.Synth.Construction.Yard.E4/Techno Synths RP"
E4B_BANKS = [
    str(E4B_DIR / "B.007-Dance Organ   RP.e4b"),
]
E4B_APPEND = [str(E4B_DIR / "B.003-Jupy organ    RP.e4b")]

KRZ_DIR = Path.home() / "Dokumente/SYNTHS/K2000R/Soundsets/K2KFARM"
KRZ_BANKS = [str(KRZ_DIR / "VOX.KRZ"), str(KRZ_DIR / "FXSOUNDS.KRZ")]
KRZ_APPEND = [str(KRZ_DIR / "BELLS.KRZ")]


def list_banks(path: str) -> list:
    """Recursively walk every folder in the volume and return every non-
    folder entry — mirrors how Emu3Volume (root -> folder -> banks) and
    Fat16Volume (root -> optional BANKS subdir -> banks) both nest content
    one level differently."""
    vol = open_volume(path)
    assert vol is not None, f"{path}: not recognised"
    out = []

    def _walk(folder=None):
        for e in vol.list(folder):
            if e.kind == EntryKind.FOLDER:
                _walk(e)
            else:
                out.append(e)

    try:
        _walk()
    finally:
        vol.close()
    return out


def vol_read_helper(path: str, entry) -> bytes:
    """Read one entry's bytes via a fresh Volume open -- Entry.ref is pure
    structural data (offsets/cluster numbers), not a live handle, so it
    stays valid across a close()+reopen of the same file on disk."""
    vol = open_volume(path)
    try:
        return vol.read(entry)
    finally:
        vol.close()


def test_e4b_emu3_cd():
    print("\n=== EMU3 CD (E4B) — exact-fit, append via rebuild fallback ===")
    img = OUT / "test_cd.iso"
    img.unlink(missing_ok=True)
    log = images.create_image("emu3_cd", str(img), E4B_BANKS, volume_label="TESTCD")
    print(log.strip().splitlines()[0] if log.strip() else "(no log)")
    entries = list_banks(str(img))
    names = [e.name for e in entries]
    print("after create:", names)
    assert any("DANCE" in n.upper() for n in names), names
    original_dance_bytes = next(vol_read_helper(str(img), e) for e in entries if "DANCE" in e.name.upper())

    # A true in-place append is impossible here (build_iso is always
    # exact-fit, zero free clusters by construction) -- append_banks()
    # transparently falls back to exporting the existing bank(s), combining
    # them with the new one, and rebuilding the whole CD image fresh.
    n, log = images.append_banks(str(img), "E4B", E4B_APPEND)
    print("appended via rebuild:", n)
    entries = list_banks(str(img))
    names = [e.name for e in entries]
    print("after append:", names)
    assert any("JUPY" in n.upper() for n in names), names
    assert any("DANCE" in n.upper() for n in names), "original entry lost during rebuild!"

    rebuilt_dance_bytes = next(vol_read_helper(str(img), e) for e in entries if "DANCE" in e.name.upper())
    print("original bank byte-identical after rebuild:", original_dance_bytes == rebuilt_dance_bytes)
    assert original_dance_bytes == rebuilt_dance_bytes
    print("EMU3 CD: OK")


def test_e4b_hda_emu():
    print("\n=== EMU3 HD, native EMU filesystem (E4B) — full lifecycle ===")
    img = OUT / "test_hda_emu.hda"
    img.unlink(missing_ok=True)
    log = images.create_image("emu3_hd_emu", str(img), E4B_BANKS,
                               volume_label="TESTHDA", size_mb=512)
    print(log.strip().splitlines()[0] if log.strip() else "(no log)")
    entries = list_banks(str(img))
    names = [e.name for e in entries]
    print("after create:", names)
    assert any("DANCE" in n.upper() for n in names), names

    n, log = images.append_banks(str(img), "E4B", E4B_APPEND)
    print("appended:", n)
    entries = list_banks(str(img))
    names = [e.name for e in entries]
    print("after append:", names)
    assert any("JUPY" in n.upper() for n in names), names
    assert any("DANCE" in n.upper() for n in names), "original entry lost after append!"

    target = next(e for e in entries if "JUPY" in e.name.upper())
    images.rename_entry(str(img), target, "RENAMEDBANK")
    entries = list_banks(str(img))
    names = [e.name for e in entries]
    print("after rename:", names)
    assert any("RENAMEDBANK" in n.upper() for n in names), names

    export_path = OUT / "exported_dance.e4b"
    export_path.unlink(missing_ok=True)
    dance_entry = next(e for e in entries if "DANCE" in e.name.upper())
    images.export_entry(str(img), dance_entry, str(export_path))
    original_bytes = Path(E4B_BANKS[0]).read_bytes()
    exported_bytes = export_path.read_bytes()
    print("export byte-identical:", original_bytes == exported_bytes,
          f"({len(exported_bytes)} bytes)")
    assert original_bytes == exported_bytes

    to_delete = next(e for e in entries if "RENAMEDBANK" in e.name.upper())
    images.delete_entry(str(img), to_delete)
    entries = list_banks(str(img))
    names = [e.name for e in entries]
    print("after delete:", names)
    assert not any("RENAMEDBANK" in n.upper() for n in names), names
    assert any("DANCE" in n.upper() for n in names), names
    print("EMU3 HD (emu): OK")


def test_e4b_hda_fat():
    print("\n=== EMU3 HD, FAT flavor (E4B) ===")
    img = OUT / "test_hda_fat.hda"
    img.unlink(missing_ok=True)
    log = images.create_image("emu3_hd_fat", str(img), E4B_BANKS,
                               volume_label="TESTHDA", size_mb=512)
    print(log.strip().splitlines()[0] if log.strip() else "(no log)")
    entries = list_banks(str(img))
    print("after create:", [e.name for e in entries])

    n, _log = images.append_banks(str(img), "E4B", E4B_APPEND)
    entries = list_banks(str(img))
    names = [e.name for e in entries]
    print("after append:", names)
    assert any("JUPY" in n.upper() for n in names), names
    print("EMU3 HD (fat): OK")


def test_krz_k2000_fat16():
    print("\n=== K2000 FAT16 disk (KRZ) ===")
    img = OUT / "test_k2000.hda"
    img.unlink(missing_ok=True)
    log = images.create_image("k2000_fat16", str(img), KRZ_BANKS, volume_label="TESTK2K")
    print(log.strip().splitlines()[0] if log.strip() else "(no log)")
    entries = list_banks(str(img))
    names = [e.name for e in entries]
    print("after create:", names)
    # build_k2000_disk renames to a sequential {label-prefix}_NN.KRZ scheme
    # (matches a real factory K2000 CD's clean 8.3 names) rather than
    # preserving the source filenames — k2000_disk_append (tested below)
    # does preserve them, since it's adding to an already-organised disk.
    assert len(names) == 2 and all(n.upper().startswith("TESTK") for n in names), names

    n, _log = images.append_banks(str(img), "KRZ", KRZ_APPEND)
    entries = list_banks(str(img))
    names = [e.name for e in entries]
    print("after append:", names)
    assert any("BELLS" in n.upper() for n in names), names

    target = next(e for e in entries if "BELLS" in e.name.upper())
    images.rename_entry(str(img), target, "RENAMED.KRZ")
    entries = list_banks(str(img))
    names = [e.name for e in entries]
    print("after rename:", names)
    assert any("RENAMED" in n.upper() for n in names), names

    export_path = OUT / "exported_vox.krz"
    export_path.unlink(missing_ok=True)
    # created first -> TESTK_01.KRZ under build_k2000_disk's naming scheme
    vox_entry = next(e for e in entries if e.name.upper().startswith("TESTK_01"))
    images.export_entry(str(img), vox_entry, str(export_path))
    original_bytes = Path(KRZ_BANKS[0]).read_bytes()
    exported_bytes = export_path.read_bytes()
    print("export byte-identical:", original_bytes == exported_bytes)
    assert original_bytes == exported_bytes

    to_delete = next(e for e in entries if "RENAMED" in e.name.upper())
    images.delete_entry(str(img), to_delete)
    entries = list_banks(str(img))
    names = [e.name for e in entries]
    print("after delete:", names)
    assert not any("RENAMED" in n.upper() for n in names), names
    print("K2000 FAT16: OK")


def test_krz_iso9660():
    print("\n=== K2000 ISO 9660 (KRZ, burn-once) ===")
    img = OUT / "test_k2000.iso"
    img.unlink(missing_ok=True)
    log = images.create_image("k2000_iso9660", str(img), KRZ_BANKS, volume_label="TESTISO")
    print(log.strip().splitlines()[0] if log.strip() else "(no log)")
    vol = open_volume(str(img))
    assert vol is not None, "ISO 9660 image not recognised by vfs.detect"
    names = [e.name for e in vol.list()]
    vol.close()
    print("contents:", names)
    assert any("VOX" in n.upper() for n in names), names

    try:
        images.append_banks(str(img), "KRZ", KRZ_APPEND)
        raised = False
    except images.ImageOpError:
        raised = True
    print("append correctly rejected:", raised)
    assert raised
    print("K2000 ISO 9660: OK")


def test_fat12_floppy():
    print("\n=== Gotek FAT12 floppy (KRZ) ===")
    img = OUT / "test_floppy.img"
    img.unlink(missing_ok=True)
    log = images.create_image("fat12_floppy", str(img), [KRZ_BANKS[0]],
                               volume_label="TESTFLOP", floppy_kind="1440")
    print(log.strip().splitlines()[0] if log.strip() else "(no log)")
    entries = list_banks(str(img))
    names = [e.name for e in entries]
    print("contents:", names)
    assert any("VOX" in n.upper() for n in names), names
    print("FAT12 floppy: OK")


def test_safety_original_untouched_on_bad_append():
    print("\n=== safety: failed append must not touch the original ===")
    img = OUT / "test_safety.hda"
    img.unlink(missing_ok=True)
    images.create_image("k2000_fat16", str(img), KRZ_BANKS, volume_label="SAFE")
    before = img.read_bytes()
    tmp_path = img.with_name(img.name + ".vinsamlib-tmp")
    tmp_path.unlink(missing_ok=True)
    try:
        images.append_banks(str(img), "KRZ", ["/nonexistent/path/NOPE.KRZ"])
        failed = False
    except images.ImageOpError:
        failed = True
    after = img.read_bytes()
    print("append raised ImageOpError:", failed)
    print("original untouched:", before == after)
    print("no leftover tmp file:", not tmp_path.exists())
    assert failed and before == after and not tmp_path.exists()
    print("safety wrapper: OK")


if __name__ == "__main__":
    config = Config.load()
    mpc2emu_bridge.install(config)
    test_e4b_emu3_cd()
    test_e4b_hda_emu()
    test_e4b_hda_fat()
    test_krz_k2000_fat16()
    test_krz_iso9660()
    test_fat12_floppy()
    test_safety_original_untouched_on_bad_append()
    print("\nALL M6 IMAGE-OPS TESTS PASSED")
