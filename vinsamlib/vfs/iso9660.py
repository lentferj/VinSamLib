"""
Minimal ISO 9660 reader.

Only needed to read back the plain ISO 9660 CDs that
``mpc2emu/writers/iso_builder.py:455 build_iso_9660()`` writes for K2000 OS
v3.87+ (see ``mpc2emu/docs/KRZ_FORMAT.md`` §5.2) — every *other* image format
in this project (EMU3 CD/HD, FAT12/16 disk-image-copy) has its own reader.
No Joliet/Rock Ridge support: mpc2emu writes plain ISO 9660 8.3-ish names,
and that is all this needs to read back.
"""

from __future__ import annotations

import struct
from typing import Optional

from .base import Entry, EntryKind, Volume

SECTOR = 2048
_BANK_EXTS = {".krz", ".k25", ".k26", ".e4b", ".e3x", ".esi", ".e3b"}


class Iso9660FormatError(ValueError):
    pass


def _classify(name: str) -> EntryKind:
    from pathlib import Path
    return EntryKind.BANK if Path(name).suffix.lower() in _BANK_EXTS else EntryKind.OTHER_FILE


class Iso9660Volume(Volume):
    def __init__(self, path: str):
        self.path = path
        self._root_extent, self._root_size = self._read_pvd()

    def _read_pvd(self) -> tuple[int, int]:
        with open(self.path, "rb") as f:
            f.seek(16 * SECTOR)
            pvd = f.read(SECTOR)
        if len(pvd) < 190 or pvd[0] != 1 or pvd[1:6] != b"CD001":
            raise Iso9660FormatError(f"{self.path}: not an ISO 9660 image (no PVD)")
        root_record = pvd[156:156 + 34]
        extent = struct.unpack_from("<I", root_record, 2)[0]
        size = struct.unpack_from("<I", root_record, 10)[0]
        return extent, size

    def _read_dir_records(self, extent: int, size: int) -> list[dict]:
        with open(self.path, "rb") as f:
            f.seek(extent * SECTOR)
            data = f.read(size)
        out = []
        pos = 0
        while pos < len(data):
            length = data[pos]
            if length == 0:
                # records never cross a sector boundary; skip to the next one
                pos = ((pos // SECTOR) + 1) * SECTOR
                continue
            rec = data[pos:pos + length]
            flags = rec[25]
            name_len = rec[32]
            raw_name = rec[33:33 + name_len]
            rec_extent = struct.unpack_from("<I", rec, 2)[0]
            rec_size = struct.unpack_from("<I", rec, 10)[0]
            if raw_name not in (b"\x00", b"\x01"):  # skip '.' and '..'
                name = raw_name.decode("latin-1", "replace").split(";", 1)[0]
                out.append({
                    "name": name,
                    "is_dir": bool(flags & 0x02),
                    "extent": rec_extent,
                    "size": rec_size,
                })
            pos += length
        return out

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:
        extent, size = folder.ref if folder is not None else (self._root_extent, self._root_size)
        out = []
        for rec in self._read_dir_records(extent, size):
            if rec["is_dir"]:
                out.append(Entry(name=rec["name"], kind=EntryKind.FOLDER,
                                  ref=(rec["extent"], rec["size"])))
            else:
                out.append(Entry(name=rec["name"], kind=_classify(rec["name"]),
                                  size=rec["size"], ref=(rec["extent"], rec["size"])))
        return out

    def read(self, entry: Entry) -> bytes:
        extent, size = entry.ref
        with open(self.path, "rb") as f:
            f.seek(extent * SECTOR)
            return f.read(size)
