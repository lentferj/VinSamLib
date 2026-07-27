"""
Sniff a file on disk and decide which Volume subclass (if any) can open it
as an image. Detection is by content, not extension — the library has
".img" files that are FAT12 floppies and ".iso" files that are the EMU3
filesystem (not actually ISO 9660 at all; see EMU3_ISO_FORMAT.md §4.3 on why
"ISO" is a misnomer there), so extension alone is not trustworthy.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional

from .base import Volume
from .emu3 import EMU3_MAGIC, Emu3Volume
from .fatvol import Fat12Volume, Fat16Volume, Fat32Volume
from .iso9660 import Iso9660Volume

_SECTOR = 512


def sniff(path: str) -> Optional[type[Volume]]:
    """Return the Volume subclass that can open `path`, or None if it is not
    a recognised image (in which case it's just a regular file)."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(_SECTOR)
    except OSError:
        return None
    if len(head) < _SECTOR:
        return None

    if head[:4] == EMU3_MAGIC:
        return Emu3Volume

    if head[510:512] == b"\x55\xAA":
        # Either an MBR (partition table) or a FAT12/16 boot sector sharing
        # the same signature at the same offset. Distinguish by the
        # filesystem-type string mpc2emu writes at BPB offset 54 / 82, and
        # by scanning the MBR partition-type bytes for FAT16/FAT32/EMU.
        fstype16 = head[54:62]
        fstype32 = head[82:90] if len(head) >= 90 else b""
        if fstype16.startswith(b"FAT12"):
            return Fat12Volume
        if fstype16.startswith(b"FAT16"):
            return Fat16Volume
        if fstype32.startswith(b"FAT32"):
            return Fat32Volume
        # MBR with a partition table: inspect partition type bytes.
        for i in range(4):
            entry = head[0x1BE + i * 16: 0x1BE + i * 16 + 16]
            if len(entry) < 5:
                continue
            ptype = entry[4]
            if ptype == 0x0C or ptype == 0x0B:
                return Fat32Volume
            if ptype == 0x06 or ptype == 0x04:
                return Fat16Volume
        # No recognisable BPB/partition — could still be a FAT12 floppy with
        # a non-standard OEM/fstype string; fall through to a raw BPB check.
        bps = struct.unpack_from("<H", head, 11)[0]
        media = head[21] if len(head) > 21 else 0
        if bps == 512 and media in (0xF0, 0xF8, 0xF9):
            return Fat12Volume
        return None

    # No boot signature at all -- could still be a genuine FAT12/16 K2000
    # disk. mpc2emu's own Fat16/Fat12 readers never check the 55/AA
    # signature or the FS-type label to open a volume (see fat16.py's
    # _part_offset()/_read_bpb() -- only the numeric BPB fields matter),
    # and real vintage K2000-format CDs predating mpc2emu's own writer
    # (e.g. third-party "Best Service"/Kurzweil-branded sample discs from
    # the 1990s) can be missing both cosmetic fields while every numeric
    # BPB field otherwise matches the documented K2000 form exactly
    # (KRZ_FORMAT.md §5.1) -- confirmed byte-for-byte against a real
    # BestServiceGigaSetCD1.iso. Validating several BPB fields together
    # (not just one) keeps this from false-triggering on an arbitrary file
    # that happens to start with the same jump opcode.
    cls = _sniff_fat_without_signature(head, p)
    if cls is not None:
        return cls

    # ISO 9660: "CD001" at byte 1 of the Primary Volume Descriptor, sector 16
    try:
        with open(path, "rb") as f:
            f.seek(16 * 2048)
            pvd_head = f.read(6)
    except OSError:
        pvd_head = b""
    if len(pvd_head) == 6 and pvd_head[0] == 1 and pvd_head[1:6] == b"CD001":
        return Iso9660Volume

    return None


def _sniff_fat_without_signature(head: bytes, path: Path) -> Optional[type[Volume]]:
    """FAT12/16 detection for a BPB with no 55/AA boot signature and no
    FS-type label -- classifies FAT12 vs FAT16 the authoritative way (by
    computed cluster count, the same convention every real FAT
    implementation uses) rather than trusting an absent label. Never
    guesses FAT32: a real FAT32 BPB stores root_ents=0 and its sectors/FAT
    in a separate 32-bit field the 16-bit fatsz16 read here would show as
    0 for, which this deliberately treats as "not enough to go on" rather
    than trying to also parse FAT32's differently-shaped BPB blind."""
    if head[0] not in (0xE9, 0xEB):   # a real x86 jump opcode at offset 0
        return None
    bps = struct.unpack_from("<H", head, 11)[0]
    spc = head[13]
    rsvd = struct.unpack_from("<H", head, 14)[0]
    nfats = head[16]
    root_ents = struct.unpack_from("<H", head, 17)[0]
    total16 = struct.unpack_from("<H", head, 19)[0]
    media = head[21] if len(head) > 21 else 0
    fatsz16 = struct.unpack_from("<H", head, 22)[0]
    total32 = struct.unpack_from("<I", head, 32)[0]

    if bps != 512 or spc not in (1, 2, 4, 8, 16, 32, 64, 128):
        return None
    if rsvd < 1 or nfats not in (1, 2):
        return None
    if media not in (0xF0, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE, 0xFF):
        return None
    if fatsz16 == 0 or root_ents == 0:
        return None

    total_sectors = total16 or total32
    if total_sectors == 0:
        return None
    try:
        actual_size = path.stat().st_size
    except OSError:
        return None
    # The BPB's own claimed size must actually fit inside the real file
    # (never smaller -- that would mean this isn't really this volume);
    # bigger is fine and common (CD lead-in/session padding beyond the
    # logical filesystem, e.g. ~600 KB extra on the real disc this was
    # written against).
    if actual_size < total_sectors * bps:
        return None

    root_dir_sectors = (root_ents * 32 + bps - 1) // bps
    data_sectors = total_sectors - (rsvd + nfats * fatsz16 + root_dir_sectors)
    if data_sectors <= 0:
        return None
    cluster_count = data_sectors // spc

    if cluster_count < 4085:
        return Fat12Volume
    if cluster_count < 65525:
        return Fat16Volume
    return None


def open_volume(path: str) -> Optional[Volume]:
    """Sniff and open `path` as an image, or return None if it isn't one."""
    cls = sniff(path)
    if cls is None:
        return None
    return cls(path)


def _walk_print(vol: Volume, folder=None, indent: str = "") -> None:
    from .base import EntryKind
    for entry in vol.list(folder):
        size_str = f"  ({entry.size:,} B)" if entry.size else ""
        fmt = entry.meta.get("format")
        fmt_str = f"  fmt={fmt}" if fmt else ""
        print(f"{indent}{entry.name}  [{entry.kind.name}]{size_str}{fmt_str}")
        if entry.kind in (EntryKind.FOLDER, EntryKind.DIRECTORY):
            _walk_print(vol, entry, indent + "  ")


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: python -m vinsamlib.vfs.detect <path>")
        return 2
    path = argv[1]
    cls = sniff(path)
    if cls is None:
        print(f"{path}: not a recognised image (kind: {cls})")
        return 1
    print(f"{path}: {cls.__name__}")
    with cls(path) as vol:
        _walk_print(vol)
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
