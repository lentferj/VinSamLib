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
