"""
Read/rename additions on top of mpc2emu's FAT writers.

mpc2emu's ``writers.fat12.Fat12`` / ``writers.fat16.Fat16`` /
``writers.fat32.Fat32`` already give ``list_dir`` / ``find_dir`` /
``makedir`` / ``add_file`` (and Fat16/Fat32 additionally ``delete_file``) —
everything a *writer* needs to build K2000 disks and EOS `.hda` images. They
never needed to read a file's bytes back out, or (for Fat12) delete/rename —
a librarian does. Rather than touching mpc2emu, each class here subclasses
around one of those writers and adds exactly those verbs, reusing the
private helpers (`_read_dir`, `_iter_entries`, cluster-chain following)
mpc2emu's own writer tests already exercise.

Directory-entry attribute constants (`_ATTR_DIR = 0x10`, `_ATTR_LFN = 0x0F`,
`_DELETED = 0xE5`) are FAT constants straight from the spec, duplicated here
rather than importing mpc2emu's underscore-prefixed module globals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import Entry, EntryKind, WritableVolume
from ..mpc2emu_bridge import fat12 as _fat12_mod
from ..mpc2emu_bridge import fat16 as _fat16_mod
from ..mpc2emu_bridge import fat32 as _fat32_mod

_ATTR_DIR = 0x10
_ATTR_LFN = 0x0F
_DELETED = 0xE5
_FAT16_EOC = 0xFFF8
_FAT32_EOC = 0x0FFFFFF8

_BANK_EXTS = {".krz", ".k25", ".k26", ".e4b", ".e3x", ".esi", ".e3b"}


def _classify(name: str) -> EntryKind:
    return EntryKind.BANK if Path(name).suffix.lower() in _BANK_EXTS else EntryKind.OTHER_FILE


class Fat16Volume(WritableVolume):
    """Wraps mpc2emu's writers.fat16.Fat16 (EOS-native .hda, ≤~1 GB, and
    K2000 disk-image-copy media, which is FAT16 without a partition)."""

    def __init__(self, path: str):
        self.path = str(path)
        self._fs = _fat16_mod.Fat16(path)

    def close(self) -> None:
        self._fs.close()

    def _chain(self, start: int) -> list[int]:
        out, c, fat = [], start, self._fs.fat
        seen = set()
        while 2 <= c < _FAT16_EOC and c < len(fat):
            if c in seen:
                break
            seen.add(c)
            out.append(c)
            c = fat[c]
        return out

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:
        cl = folder.ref if folder is not None else None
        data, _ = self._fs._read_dir(cl)
        Fat16 = type(self._fs)
        out = []
        for o, name, attr, entry_cl, size in Fat16._iter_entries(data):
            if name in (".", ".."):
                continue
            if attr & _ATTR_DIR:
                out.append(Entry(name=name, kind=EntryKind.FOLDER, ref=entry_cl))
            else:
                out.append(Entry(
                    name=name, kind=_classify(name), size=size,
                    ref={"folder": cl, "offset": o, "cluster": entry_cl, "size": size}))
        return out

    def read(self, entry: Entry) -> bytes:
        r = entry.ref
        buf = bytearray()
        for c in self._chain(r["cluster"]):
            self._fs.f.seek(self._fs._cluster_offset(c))
            buf += self._fs.f.read(self._fs.cluster_bytes)
        return bytes(buf[:r["size"]])

    def rename(self, entry: Entry, new_name: str) -> None:
        r = entry.ref
        Fat16 = type(self._fs)
        data, wb = self._fs._read_dir(r["folder"])
        _mark_deleted(data, r["offset"])
        used = self._fs._used_shortnames(data)
        entries = _build_entries(Fat16, new_name, 0x20, r["cluster"], r["size"], used)
        _insert_entries(Fat16, data, entries)
        wb(data)

    def delete(self, entry: Entry) -> None:
        self._fs.delete_file(entry.name, entry.ref["folder"])

    def append(self, files: list[str], folder: Optional[Entry] = None) -> int:
        cl = folder.ref if folder is not None else None
        n = 0
        for path in files:
            self._fs.add_file(path, Path(path).name, cl)
            n += 1
        return n


class Fat32Volume(WritableVolume):
    """Wraps mpc2emu's writers.fat32.Fat32 (EOS-native .hda, > ~1 GB)."""

    def __init__(self, path: str):
        self.path = str(path)
        self._fs = _fat32_mod.Fat32(path)

    def close(self) -> None:
        self._fs.close()

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:
        cl = folder.ref if folder is not None else self._fs.root_clus
        data, _ = self._fs._read_dir(cl)
        Fat16 = _fat16_mod.Fat16
        out = []
        for o, name, attr, entry_cl, size in Fat16._iter_entries(data):
            if name in (".", ".."):
                continue
            if attr & _ATTR_DIR:
                out.append(Entry(name=name, kind=EntryKind.FOLDER, ref=entry_cl))
            else:
                out.append(Entry(
                    name=name, kind=_classify(name), size=size,
                    ref={"folder": cl, "offset": o, "cluster": entry_cl, "size": size}))
        return out

    def read(self, entry: Entry) -> bytes:
        r = entry.ref
        buf = bytearray()
        for c in self._fs._chain(r["cluster"]):
            self._fs.f.seek(self._fs._cluster_offset(c))
            buf += self._fs.f.read(self._fs.cluster_bytes)
        return bytes(buf[:r["size"]])

    def rename(self, entry: Entry, new_name: str) -> None:
        r = entry.ref
        Fat16 = _fat16_mod.Fat16
        cl = r["folder"] if r["folder"] is not None else self._fs.root_clus
        data, wb = self._fs._read_dir(cl)
        _mark_deleted(data, r["offset"])
        used = self._fs._used_shortnames(data)
        short = Fat16._short_name(new_name, used)
        entries = Fat16._lfn_entries(new_name, short) + \
            [self._fs._short_entry_hi(short, 0x20, r["cluster"], r["size"])]
        _insert_entries(Fat16, data, entries)
        wb(data)

    def delete(self, entry: Entry) -> None:
        self._fs.delete_file(entry.name, entry.ref["folder"])

    def append(self, files: list[str], folder: Optional[Entry] = None) -> int:
        cl = folder.ref if folder is not None else None
        n = 0
        for path in files:
            self._fs.add_file(path, Path(path).name, cl)
            n += 1
        return n


class Fat12Volume(WritableVolume):
    """Wraps mpc2emu's writers.fat12.Fat12 (Gotek floppy, flat root only —
    the K2000 stores a single bank per floppy, so no subfolder support)."""

    def __init__(self, path: str):
        self.path = str(path)
        self._fs = _fat12_mod.Fat12(path)

    def close(self) -> None:
        self._fs.close()

    def _chain(self, start: int) -> list[int]:
        out, c = [], start
        fat, get = self._fs.fat, _fat12_mod._fat12_get
        seen = set()
        while 2 <= c < 0xFF8:
            if c in seen:
                break
            seen.add(c)
            out.append(c)
            c = get(fat, c)
        return out

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:
        if folder is not None:
            raise ValueError("FAT12 floppies are flat (root directory only)")
        data, _ = self._fs._read_dir()
        Fat16 = _fat16_mod.Fat16
        out = []
        for o, name, attr, entry_cl, size in Fat16._iter_entries(data):
            if attr & _ATTR_DIR:
                continue  # floppies are flat; ignore any stray dir entry
            out.append(Entry(
                name=name, kind=_classify(name), size=size,
                ref={"offset": o, "cluster": entry_cl, "size": size}))
        return out

    def read(self, entry: Entry) -> bytes:
        r = entry.ref
        buf = bytearray()
        for c in self._chain(r["cluster"]):
            self._fs.f.seek(self._fs._cluster_offset(c))
            buf += self._fs.f.read(self._fs.cluster_bytes)
        return bytes(buf[:r["size"]])

    def rename(self, entry: Entry, new_name: str) -> None:
        r = entry.ref
        Fat16 = _fat16_mod.Fat16
        data, wb = self._fs._read_dir()
        _mark_deleted(data, r["offset"])
        used = {bytes(data[o:o + 11]) for o in range(0, len(data), 32)
                if data[o] not in (0x00, _DELETED) and data[o + 11] != _ATTR_LFN}
        entries = _build_entries(Fat16, new_name, 0x20, r["cluster"], r["size"], used)
        _insert_entries(Fat16, data, entries)
        wb(data)

    def delete(self, entry: Entry) -> None:
        """Mirrors writers.fat16.Fat16.delete_file — Fat12 has no equivalent
        in mpc2emu because its writer only ever appends to a blank floppy."""
        r = entry.ref
        data, wb = self._fs._read_dir()
        cl = r["cluster"]
        set_ = _fat12_mod._fat12_set
        while 2 <= cl < 0xFF8:
            nxt = _fat12_mod._fat12_get(self._fs.fat, cl)
            set_(self._fs.fat, cl, 0)
            cl = nxt
        _mark_deleted(data, r["offset"])
        wb(data)
        self._fs._flush_fat()

    def append(self, files: list[str], folder: Optional[Entry] = None) -> int:
        if folder is not None:
            raise ValueError("FAT12 floppies are flat (root directory only)")
        n = 0
        for path in files:
            self._fs.add_file(path, Path(path).name)
            n += 1
        return n


# ── shared directory-entry helpers ──────────────────────────────────────────

def _mark_deleted(data: bytearray, offset: int) -> None:
    """Mark a short entry and any preceding VFAT long-name entries as
    deleted — the same two-line pattern used by every delete_file()."""
    data[offset] = _DELETED
    j = offset - 32
    while j >= 0 and data[j + 11] == _ATTR_LFN:
        data[j] = _DELETED
        j -= 32


def _build_entries(Fat16, longname: str, attr: int, cluster: int, size: int,
                    used: set) -> list[bytes]:
    short83 = Fat16._try_83(longname)
    if short83 is not None and short83 not in used:
        return [Fat16._short_entry(short83, attr, cluster, size)]
    short = Fat16._short_name(longname, used)
    return Fat16._lfn_entries(longname, short) + [Fat16._short_entry(short, attr, cluster, size)]


def _insert_entries(Fat16, data: bytearray, entries: list[bytes]) -> None:
    off = Fat16._find_free_run(data, len(entries))
    if off is None:
        raise ValueError("directory is full (no room to rename)")
    for i, e in enumerate(entries):
        data[off + i * 32:off + i * 32 + 32] = e
