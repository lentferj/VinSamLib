"""
FAT12/16/32 filesystem reader -- the format K2000 floppies/CDs/HDs and
EOS FAT-based hard disks use. Like vfs/emu3.py and vfs/iso9660.py, this is
a standalone, from-scratch reader against the public FAT specification
(unlike E4B/KRZ/EIII, FAT12/16/32 is vendor-neutral and fully documented,
so no reverse-engineering was needed here) -- browsing, loading, and bank-
building never need mpc2emu installed at all. Only append() -- adding new
content to an existing image, the one genuinely complex "find free space
and allocate" operation -- still delegates to mpc2emu's own
writers.fat12/fat16/fat32, matching vfs/emu3.py's own append() precedent.
No caller in this codebase currently invokes Fat*Volume.append() at all
(build/images.py's append_banks() calls mpc2emu's writers directly); it's
kept only so isinstance(vol, WritableVolume) holds for the UI's
append-enabled check.

Three real-world variants this reads:
- FAT16, MBR-partitioned, EOS-native ``.hda`` hard disks (OEM ``E-MU SYS``).
- FAT16, no partition table at all, Kurzweil K2000 FAT16 CD/HD media (OEM
  ``KCDM1.2``, BPB straight at LBA 0) -- see
  mpc2emu/docs/re_procedures/emu_hdd_fs.md §1 and mpc2emu/TODO.md's real
  hardware-confirmation notes for both variants.
- FAT32, MBR-partitioned, EOS-native ``.hda`` hard disks (>~1 GB).
- FAT12, no partition table, flat root directory only, Gotek/FlashFloppy
  1.44 MB/720 KB floppies for the K2000.

Real hardware (the K2000, and the E4XT's own file browser) only ever reads
the 8.3 short name of a directory entry -- a long (VFAT) name is purely a
browsing convenience here, never load-bearing. ``rename()`` therefore
tries a clean 8.3-only encoding first (see ``_try_83``) before falling
back to a generated ``NAME~1``-style short alias plus VFAT long-name
entries, so a name that already fits 8.3 never gets a mangled on-device
alias.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Callable, Optional

from .base import Entry, EntryKind, WritableVolume

SECTOR = 512

_ATTR_VOLUME = 0x08
_ATTR_DIR = 0x10
_ATTR_LFN = 0x0F
_FREE = 0x00
_DELETED = 0xE5

# Characters allowed unescaped in an 8.3 short name (FAT spec charset).
_SFN_OK = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'()-@^_`{}~")

# End-of-chain thresholds and the reserved "bad cluster" marker, per variant
# -- the bad-cluster marker sits just below the EOC range in every FAT
# variant, so chain-following must exclude it explicitly rather than treat
# it as "still a valid cluster, keep going" (a real bug in some readers).
_FAT12_EOC_MIN, _FAT12_BAD = 0xFF8, 0xFF7
_FAT16_EOC_MIN, _FAT16_BAD = 0xFFF8, 0xFFF7
_FAT32_EOC_MIN, _FAT32_BAD = 0x0FFFFFF8, 0x0FFFFFF7

# Only the low 28 bits of a FAT32 entry are the cluster number; the top 4
# are reserved and must survive a write untouched (the two flags the spec
# does define in FAT[1] -- ClnShut 0x08000000, HrdErr 0x04000000 -- sit
# inside the low 28 and so are unaffected either way).
_FAT32_MASK = 0x0FFFFFFF

_MBR_PART_TYPES = {0x01, 0x04, 0x06, 0x0B, 0x0C, 0x0E}

_BANK_EXTS = {".krz", ".k25", ".k26", ".e4b", ".e3x", ".esi", ".e3b"}


class FatFormatError(ValueError):
    pass


def _classify(name: str) -> EntryKind:
    return EntryKind.BANK if Path(name).suffix.lower() in _BANK_EXTS else EntryKind.OTHER_FILE


# ── MBR / BPB parsing ────────────────────────────────────────────────────

def _part_offset(f) -> int:
    """Byte offset of the first recognised primary partition, or 0 for a
    partitionless "superfloppy" image -- real K2000 CD/floppy media has no
    MBR at all, so this fallback is required, not an edge case."""
    f.seek(0)
    s0 = f.read(SECTOR)
    if len(s0) >= 512 and s0[510:512] == b"\x55\xAA":
        for i in range(4):
            e = s0[0x1BE + i * 16: 0x1BE + i * 16 + 16]
            if len(e) < 16:
                continue
            if e[4] in _MBR_PART_TYPES:
                lba = struct.unpack_from("<I", e, 8)[0]
                if lba:
                    return lba * SECTOR
    return 0


def _read_bpb_fat16(f) -> dict:
    part_off = _part_offset(f)
    f.seek(part_off)
    b = f.read(SECTOR)
    if len(b) < 512:
        raise FatFormatError("BPB sector truncated")
    bps = struct.unpack_from("<H", b, 11)[0]
    spc = b[13]
    rsvd = struct.unpack_from("<H", b, 14)[0]
    nfats = b[16]
    root_ents = struct.unpack_from("<H", b, 17)[0]
    tot16 = struct.unpack_from("<H", b, 19)[0]
    fatsz = struct.unpack_from("<H", b, 22)[0]
    tot32 = struct.unpack_from("<I", b, 32)[0]
    if not bps or not spc or not fatsz or not nfats or not root_ents:
        raise FatFormatError("not a FAT12/16 BPB (a required field is 0)")
    total_sectors = tot16 or tot32   # real spec precedence: nonzero 16-bit wins
    root_sectors = (root_ents * 32 + bps - 1) // bps
    fat_start = rsvd
    root_start = rsvd + nfats * fatsz
    data_start = root_start + root_sectors
    return dict(part_off=part_off, bps=bps, spc=spc, rsvd=rsvd, nfats=nfats,
                root_ents=root_ents, fatsz=fatsz, total_sectors=total_sectors,
                root_sectors=root_sectors, fat_start=fat_start,
                root_start=root_start, data_start=data_start)


def _read_bpb_fat32(f) -> dict:
    part_off = _part_offset(f)
    f.seek(part_off)
    b = f.read(SECTOR)
    if len(b) < 512:
        raise FatFormatError("BPB sector truncated")
    bps = struct.unpack_from("<H", b, 11)[0]
    spc = b[13]
    rsvd = struct.unpack_from("<H", b, 14)[0]
    nfats = b[16]
    fatsz = struct.unpack_from("<I", b, 36)[0]
    root_clus = struct.unpack_from("<I", b, 44)[0]
    total_sectors = struct.unpack_from("<I", b, 32)[0]
    if not bps or not spc or not fatsz or not nfats or not root_clus:
        raise FatFormatError("not a FAT32 BPB (a required field is 0)")
    fat_start = rsvd
    data_start = rsvd + nfats * fatsz
    return dict(part_off=part_off, bps=bps, spc=spc, rsvd=rsvd, nfats=nfats,
                fatsz=fatsz, total_sectors=total_sectors, fat_start=fat_start,
                data_start=data_start, root_clus=root_clus)


def _read_bpb_fat12(f) -> dict:
    """Real K2000/Gotek floppy media never has a partition table -- the BPB
    always sits at absolute LBA 0."""
    f.seek(0)
    b = f.read(SECTOR)
    if len(b) < 512:
        raise FatFormatError("BPB sector truncated")
    bps = struct.unpack_from("<H", b, 11)[0]
    spc = b[13]
    rsvd = struct.unpack_from("<H", b, 14)[0]
    nfats = b[16]
    root_ents = struct.unpack_from("<H", b, 17)[0]
    tot16 = struct.unpack_from("<H", b, 19)[0]
    fatsz = struct.unpack_from("<H", b, 22)[0]
    tot32 = struct.unpack_from("<I", b, 32)[0]
    if not bps or not spc or not fatsz or not nfats or not root_ents:
        raise FatFormatError("not a FAT12 BPB (a required field is 0)")
    total_sectors = tot16 or tot32
    root_sectors = (root_ents * 32 + bps - 1) // bps
    fat_start = rsvd
    root_start = rsvd + nfats * fatsz
    data_start = root_start + root_sectors
    return dict(bps=bps, spc=spc, rsvd=rsvd, nfats=nfats, root_ents=root_ents,
                fatsz=fatsz, total_sectors=total_sectors,
                root_sectors=root_sectors, fat_start=fat_start,
                root_start=root_start, data_start=data_start)


def _cluster_offset(part_off: int, data_start: int, bps: int, spc: int, cluster: int) -> int:
    return part_off + (data_start + (cluster - 2) * spc) * bps


def _read_chain(f, chain: list[int], offset_of: Callable[[int], int],
                 cluster_bytes: int) -> bytearray:
    """Read a whole cluster chain, coalescing runs of consecutive clusters
    into one seek + one read.

    A chain is nearly always contiguous on real media -- these images are
    written once, in order -- so reading it cluster by cluster costs one
    syscall per cluster for no reason. Indexing this project's own library
    issued 1.53 million reads and spent 8.6 s inside them; the same data in
    coalesced runs is a few thousand.

    Zero-padding is per run rather than per cluster, which is the same
    bytes: a truncated image yields a short read at the end of whichever
    run runs off the end, and the tail is zero-filled either way. That
    padding matters -- see the callers reading directory data, where a
    short read would shift every later entry's offset."""
    buf = bytearray()
    i, n = 0, len(chain)
    while i < n:
        j = i + 1
        while j < n and chain[j] == chain[j - 1] + 1:
            j += 1
        want = (j - i) * cluster_bytes
        f.seek(offset_of(chain[i]))
        buf += f.read(want).ljust(want, b"\x00")
        i = j
    return buf


# ── FAT12 12-bit entry packing ───────────────────────────────────────────

def _fat12_get(fat: bytes, n: int) -> int:
    o = (n * 3) // 2
    if n & 1:
        return ((fat[o] >> 4) | (fat[o + 1] << 4)) & 0xFFF
    return (fat[o] | ((fat[o + 1] & 0x0F) << 8)) & 0xFFF


def _fat12_set(fat: bytearray, n: int, v: int) -> None:
    o = (n * 3) // 2
    v &= 0xFFF
    if n & 1:
        fat[o] = (fat[o] & 0x0F) | ((v & 0x0F) << 4)
        fat[o + 1] = (v >> 4) & 0xFF
    else:
        fat[o] = v & 0xFF
        fat[o + 1] = (fat[o + 1] & 0xF0) | ((v >> 8) & 0x0F)


# ── cluster-chain following (shared by all three variants) ──────────────

def _walk_chain(get: Callable[[int], int], start: int, eoc_min: int, bad: int,
                 n_entries: int) -> list[int]:
    """Follow a FAT chain from `start`, stopping (not raising) at a real
    end-of-chain marker or the reserved bad-cluster marker -- both are
    normal chain terminators, not errors. Raises FatFormatError on a
    cycle or an out-of-range cluster, matching vfs/emu3.py's own
    cluster-chain convention (never a silent `break`)."""
    out = []
    c = start
    seen = set()
    while 2 <= c < eoc_min and c != bad:
        if c in seen or c >= n_entries:
            raise FatFormatError(f"corrupt or cyclic FAT chain at cluster {c}")
        seen.add(c)
        out.append(c)
        c = get(c)
    return out


# ── directory entries: 8.3 + VFAT long names ─────────────────────────────

def _iter_dir_entries(data: bytes):
    """Yield dicts (offset, name, attr, cluster, size) for real (non-
    deleted, non-volume-label) entries in a directory's raw bytes. A
    preceding run of VFAT long-name entries is reassembled into the
    display name; real hardware (the K2000, the E4XT) only ever reads the
    8.3 short name, so the long name is a browsing convenience only.
    FstClusHI (offset 20) is always folded in, not just FstClusLO (offset
    26) -- FAT12/16 never use offset 20 (always 0 there), but FAT32 does,
    and reading only the low word silently truncates any FAT32 cluster
    number >= 65536."""
    lfn: list[tuple[int, bytes]] = []
    for o in range(0, len(data) - 31, 32):
        first = data[o]
        if first == _FREE:
            break
        if first == _DELETED:
            lfn = []
            continue
        attr = data[o + 11]
        if attr == _ATTR_LFN:
            seq = first & 0x1F
            chars = data[o + 1:o + 11] + data[o + 14:o + 26] + data[o + 28:o + 32]
            lfn.append((seq, chars))
            continue
        if (attr & _ATTR_VOLUME) and not (attr & _ATTR_DIR):
            lfn = []
            continue
        name = ""
        if lfn:
            for _seq, chars in sorted(lfn):
                name += chars.decode("utf-16-le", "ignore")
            name = name.split("\x00", 1)[0].rstrip("￿")
        if not name:
            base = data[o:o + 8].decode("latin-1").rstrip()
            ext = data[o + 8:o + 11].decode("latin-1").rstrip()
            name = base + ("." + ext if ext else "")
        cluster_hi = struct.unpack_from("<H", data, o + 20)[0]
        cluster_lo = struct.unpack_from("<H", data, o + 26)[0]
        size = struct.unpack_from("<I", data, o + 28)[0]
        yield {"offset": o, "name": name, "attr": attr,
               "cluster": (cluster_hi << 16) | cluster_lo, "size": size}
        lfn = []


def _lfn_checksum(short11: bytes) -> int:
    s = 0
    for c in short11:
        s = (((s & 1) << 7) + (s >> 1) + c) & 0xFF
    return s


def _try_83(name: str) -> Optional[bytes]:
    """If `name` is already a valid uppercase 8.3 name, return its 11-byte
    padded form so rename() can write a clean short entry with NO VFAT
    long-name entries -- the K2000 reads 8.3 short names only, so a long
    name would otherwise show as a mangled `NAME~1` alias. Otherwise None."""
    stem, _, ext = name.rpartition(".")
    if not stem:
        stem, ext = ext, ""
    if len(stem) > 8 or len(ext) > 3 or not stem:
        return None
    s = stem.upper().encode("latin-1", "replace")
    e = ext.upper().encode("latin-1", "replace")
    if any(c not in _SFN_OK for c in s) or any(c not in _SFN_OK for c in e):
        return None
    return s.ljust(8, b" ") + e.ljust(3, b" ")


def _short_name(longname: str, used: set) -> bytes:
    """Generate a `NAME~1`-style unique 8.3 alias for a name that doesn't
    already fit 8.3 cleanly."""
    stem, _, ext = longname.rpartition(".")
    if not stem:
        stem, ext = ext, ""

    def clean(s: str) -> bytes:
        return bytes(c if c in _SFN_OK else ord("_")
                      for c in s.upper().replace(".", "").encode("latin-1", "replace"))

    cbase = clean(stem) or b"BANK"
    cext = clean(ext)[:3]
    for n in range(1, 1000):
        suffix = b"~" + str(n).encode()
        base = cbase[:8 - len(suffix)] + suffix
        short = base.ljust(8, b" ") + cext.ljust(3, b" ")
        if short not in used:
            return short
    raise FatFormatError("could not generate a unique 8.3 name")


def _lfn_entries(longname: str, short11: bytes) -> list[bytes]:
    cks = _lfn_checksum(short11)
    chars = list(longname.encode("utf-16-le")) + [0x00, 0x00]
    while len(chars) % 26:
        chars.append(0xFF)
    parts = [bytes(chars[i:i + 26]) for i in range(0, len(chars), 26)]
    out = []
    n = len(parts)
    for idx, p in enumerate(parts):
        seq = (idx + 1) | (0x40 if idx == n - 1 else 0)
        e = bytearray(32)
        e[0] = seq
        e[1:11] = p[0:10]
        e[11] = _ATTR_LFN
        e[12] = 0
        e[13] = cks
        e[14:26] = p[10:22]
        e[26:28] = b"\x00\x00"
        e[28:32] = p[22:26]
        out.append(bytes(e))
    return out[::-1]   # stored highest-sequence-first on disk


def _short_entry(short11: bytes, attr: int, cluster: int, size: int) -> bytes:
    e = bytearray(32)
    e[0:11] = short11
    e[11] = attr
    struct.pack_into("<H", e, 20, (cluster >> 16) & 0xFFFF)   # FstClusHI
    struct.pack_into("<H", e, 26, cluster & 0xFFFF)           # FstClusLO
    struct.pack_into("<I", e, 28, size)
    return bytes(e)


def _build_entries(longname: str, attr: int, cluster: int, size: int, used: set) -> list[bytes]:
    short83 = _try_83(longname)
    if short83 is not None and short83 not in used:
        return [_short_entry(short83, attr, cluster, size)]
    short = _short_name(longname, used)
    return _lfn_entries(longname, short) + [_short_entry(short, attr, cluster, size)]


def _find_free_run(data: bytearray, count: int) -> Optional[int]:
    run = 0
    start = None
    for o in range(0, len(data), 32):
        if data[o] in (_FREE, _DELETED):
            if run == 0:
                start = o
            run += 1
            if run >= count:
                return start
        else:
            run = 0
            start = None
    return None


def _used_shortnames(data: bytearray) -> set:
    out = set()
    for o in range(0, len(data), 32):
        first = data[o]
        if first == _FREE:
            break
        if first == _DELETED or data[o + 11] == _ATTR_LFN:
            continue
        out.add(bytes(data[o:o + 11]))
    return out


def _mark_deleted(data: bytearray, offset: int) -> None:
    """Mark a short entry and any preceding VFAT long-name entries as
    deleted -- the same pattern used by every delete()/rename() below."""
    data[offset] = _DELETED
    j = offset - 32
    while j >= 0 and data[j + 11] == _ATTR_LFN:
        data[j] = _DELETED
        j -= 32


def _insert_entries(data: bytearray, entries: list[bytes]) -> None:
    off = _find_free_run(data, len(entries))
    if off is None:
        raise FatFormatError("directory is full (no room to rename)")
    for i, e in enumerate(entries):
        data[off + i * 32:off + i * 32 + 32] = e


class Fat16Volume(WritableVolume):
    """FAT16 volume: EOS-native ``.hda`` hard disks (MBR-partitioned, OEM
    ``E-MU SYS``) and Kurzweil K2000 FAT16 CD/HD media (no partition table
    at all, OEM ``KCDM1.2``). Root directory is a fixed area (not a
    cluster chain); subdirectories are a real chain, followed in full here
    -- unlike some readers, which only ever read a subdirectory's first
    cluster and silently truncate anything beyond it.

    Opens, parses, and closes the underlying file on every call rather
    than holding a persistent handle -- same reasoning as vfs/emu3.py:
    safe from a background scan thread, no Windows file-locking surprises,
    and (unlike a handle opened 'r+b' at construction) never blocks
    browsing a read-only file."""

    def __init__(self, path: str):
        self.path = str(path)
        with open(self.path, "rb") as f:
            self._geo = _read_bpb_fat16(f)

    def _read_fat(self, f) -> list[int]:
        g = self._geo
        f.seek(g["part_off"] + g["fat_start"] * g["bps"])
        raw = f.read(g["fatsz"] * g["bps"])
        raw = raw.ljust(g["fatsz"] * g["bps"], b"\x00")   # tolerate a truncated image
        return list(struct.unpack_from("<%dH" % (len(raw) // 2), raw))

    def _write_fat(self, f, fat: list[int]) -> None:
        g = self._geo
        raw = struct.pack("<%dH" % len(fat), *fat)
        for i in range(g["nfats"]):
            f.seek(g["part_off"] + (g["fat_start"] + i * g["fatsz"]) * g["bps"])
            f.write(raw)

    def _dir_data(self, f, folder_ref: Optional[int]) -> bytearray:
        g = self._geo
        if folder_ref is None:
            f.seek(g["part_off"] + g["root_start"] * g["bps"])
            return bytearray(f.read(g["root_sectors"] * g["bps"]))
        fat = self._read_fat(f)
        chain = _walk_chain(lambda n: fat[n], folder_ref, _FAT16_EOC_MIN, _FAT16_BAD, len(fat))
        # Padded to the FULL cluster size (see _read_chain): a short read on
        # a truncated image would otherwise shift every later cluster's
        # entries in this concatenated buffer, so the "offset" handed out by
        # list() would address the wrong entry on a later delete()/rename().
        return _read_chain(
            f, chain,
            lambda c: _cluster_offset(g["part_off"], g["data_start"], g["bps"], g["spc"], c),
            g["bps"] * g["spc"])

    def _write_dir(self, f, folder_ref: Optional[int], data: bytearray) -> None:
        g = self._geo
        if folder_ref is None:
            f.seek(g["part_off"] + g["root_start"] * g["bps"])
            f.write(data)
            return
        fat = self._read_fat(f)
        chain = _walk_chain(lambda n: fat[n], folder_ref, _FAT16_EOC_MIN, _FAT16_BAD, len(fat))
        cluster_bytes = g["bps"] * g["spc"]
        for i, c in enumerate(chain):
            f.seek(_cluster_offset(g["part_off"], g["data_start"], g["bps"], g["spc"], c))
            f.write(data[i * cluster_bytes:(i + 1) * cluster_bytes])

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:
        with open(self.path, "rb") as f:
            data = self._dir_data(f, folder.ref if folder is not None else None)
        out = []
        for e in _iter_dir_entries(data):
            if e["name"] in (".", ".."):
                continue
            if e["attr"] & _ATTR_DIR:
                out.append(Entry(name=e["name"], kind=EntryKind.FOLDER, ref=e["cluster"]))
            else:
                out.append(Entry(
                    name=e["name"], kind=_classify(e["name"]), size=e["size"],
                    ref={"folder": folder.ref if folder is not None else None,
                         "offset": e["offset"], "cluster": e["cluster"], "size": e["size"]}))
        return out

    def read(self, entry: Entry) -> bytes:
        g = self._geo
        r = entry.ref
        with open(self.path, "rb") as f:
            fat = self._read_fat(f)
            chain = _walk_chain(lambda n: fat[n], r["cluster"], _FAT16_EOC_MIN, _FAT16_BAD, len(fat))
            buf = _read_chain(
                f, chain,
                lambda c: _cluster_offset(g["part_off"], g["data_start"], g["bps"], g["spc"], c),
                g["bps"] * g["spc"])
        return bytes(buf[:r["size"]])

    def delete(self, entry: Entry) -> None:
        r = entry.ref
        with open(self.path, "r+b") as f:
            fat = self._read_fat(f)
            c = r["cluster"]
            seen = set()
            while 2 <= c < _FAT16_EOC_MIN and c != _FAT16_BAD:
                if c in seen or c >= len(fat):
                    break
                seen.add(c)
                # NOT `c, fat[c] = fat[c], 0` -- Python assigns targets left
                # to right, so by the time `fat[c]` is evaluated, `c` would
                # already hold the new value and the wrong slot gets freed.
                nxt = fat[c]
                fat[c] = 0
                c = nxt
            data = self._dir_data(f, r["folder"])
            _mark_deleted(data, r["offset"])
            self._write_dir(f, r["folder"], data)
            self._write_fat(f, fat)

    def rename(self, entry: Entry, new_name: str) -> None:
        r = entry.ref
        with open(self.path, "r+b") as f:
            data = self._dir_data(f, r["folder"])
            _mark_deleted(data, r["offset"])
            used = _used_shortnames(data)
            entries = _build_entries(new_name, 0x20, r["cluster"], r["size"], used)
            _insert_entries(data, entries)
            self._write_dir(f, r["folder"], data)

    def append(self, files: list[str], folder: Optional[Entry] = None) -> int:
        """Delegates to mpc2emu's proven allocator rather than
        reimplementing cluster/slot allocation here -- see vfs/emu3.py's
        append() for the same reasoning. Unused by any caller in this
        codebase today (build/images.py calls mpc2emu's writers directly);
        kept only so isinstance(vol, WritableVolume) holds."""
        from ..mpc2emu_bridge import fat16 as _fat16_mod
        fs = _fat16_mod.Fat16(self.path)
        try:
            cl = folder.ref if folder is not None else None
            n = 0
            for p in files:
                fs.add_file(p, Path(p).name, cl)
                n += 1
            return n
        finally:
            fs.close()


class Fat32Volume(WritableVolume):
    """FAT32 volume: EOS-native ``.hda`` hard disks (>~1 GB, MBR-
    partitioned). Unlike FAT16, there is no fixed root area -- the root
    directory is itself a cluster chain starting at ``root_clus`` -- and
    every start-cluster is a full 32-bit value (``FstClusHI``/offset 20
    combined with ``FstClusLO``/offset 26; some readers only look at the
    low word, which silently corrupts anything at cluster >= 65536)."""

    def __init__(self, path: str):
        self.path = str(path)
        with open(self.path, "rb") as f:
            self._geo = _read_bpb_fat32(f)

    def _read_fat(self, f) -> list[int]:
        """Entries are returned RAW (all 32 bits), not masked to 28 -- the
        top 4 bits of a FAT32 entry are reserved by the spec and must be
        written back unchanged. Masking here and then packing the masked
        values back in _write_fat() zeroed those bits across the whole FAT
        on every delete(). Mask at the point of use instead: see
        _fat32_next() for chain-following and delete()'s free operation,
        which clears only the low 28 bits."""
        g = self._geo
        f.seek(g["part_off"] + g["fat_start"] * g["bps"])
        raw = f.read(g["fatsz"] * g["bps"])
        raw = raw.ljust(g["fatsz"] * g["bps"], b"\x00")
        n = len(raw) // 4
        return list(struct.unpack_from("<%dI" % n, raw))

    def _write_fat(self, f, fat: list[int]) -> None:
        g = self._geo
        raw = struct.pack("<%dI" % len(fat), *fat)
        for i in range(g["nfats"]):
            f.seek(g["part_off"] + (g["fat_start"] + i * g["fatsz"]) * g["bps"])
            f.write(raw)

    def _dir_data(self, f, folder_ref: Optional[int]) -> bytearray:
        g = self._geo
        start = folder_ref if folder_ref is not None else g["root_clus"]
        fat = self._read_fat(f)
        chain = _walk_chain(lambda n: fat[n] & _FAT32_MASK, start,
                             _FAT32_EOC_MIN, _FAT32_BAD, len(fat))
        # Padded to the FULL cluster size (see _read_chain): a short read on
        # a truncated image would otherwise shift every later cluster's
        # entries in this concatenated buffer, so the "offset" handed out by
        # list() would address the wrong entry on a later delete()/rename().
        return _read_chain(
            f, chain,
            lambda c: _cluster_offset(g["part_off"], g["data_start"], g["bps"], g["spc"], c),
            g["bps"] * g["spc"])

    def _write_dir(self, f, folder_ref: Optional[int], data: bytearray) -> None:
        g = self._geo
        start = folder_ref if folder_ref is not None else g["root_clus"]
        fat = self._read_fat(f)
        chain = _walk_chain(lambda n: fat[n] & _FAT32_MASK, start,
                             _FAT32_EOC_MIN, _FAT32_BAD, len(fat))
        cluster_bytes = g["bps"] * g["spc"]
        if len(data) > len(chain) * cluster_bytes:
            raise FatFormatError("directory grew beyond its allocated clusters")
        for i, c in enumerate(chain):
            f.seek(_cluster_offset(g["part_off"], g["data_start"], g["bps"], g["spc"], c))
            f.write(data[i * cluster_bytes:(i + 1) * cluster_bytes])

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:
        with open(self.path, "rb") as f:
            data = self._dir_data(f, folder.ref if folder is not None else None)
        out = []
        for e in _iter_dir_entries(data):
            if e["name"] in (".", ".."):
                continue
            if e["attr"] & _ATTR_DIR:
                out.append(Entry(name=e["name"], kind=EntryKind.FOLDER, ref=e["cluster"]))
            else:
                out.append(Entry(
                    name=e["name"], kind=_classify(e["name"]), size=e["size"],
                    ref={"folder": folder.ref if folder is not None else None,
                         "offset": e["offset"], "cluster": e["cluster"], "size": e["size"]}))
        return out

    def read(self, entry: Entry) -> bytes:
        g = self._geo
        r = entry.ref
        with open(self.path, "rb") as f:
            fat = self._read_fat(f)
            chain = _walk_chain(lambda n: fat[n] & _FAT32_MASK, r["cluster"],
                                 _FAT32_EOC_MIN, _FAT32_BAD, len(fat))
            buf = _read_chain(
                f, chain,
                lambda c: _cluster_offset(g["part_off"], g["data_start"], g["bps"], g["spc"], c),
                g["bps"] * g["spc"])
        return bytes(buf[:r["size"]])

    def delete(self, entry: Entry) -> None:
        r = entry.ref
        with open(self.path, "r+b") as f:
            fat = self._read_fat(f)
            c = r["cluster"]
            seen = set()
            while 2 <= c < _FAT32_EOC_MIN and c != _FAT32_BAD:
                if c in seen or c >= len(fat):
                    break
                seen.add(c)
                nxt = fat[c] & _FAT32_MASK
                # Free = low 28 bits zero; the reserved top 4 stay as found.
                fat[c] &= ~_FAT32_MASK & 0xFFFFFFFF
                c = nxt
            data = self._dir_data(f, r["folder"])
            _mark_deleted(data, r["offset"])
            self._write_dir(f, r["folder"], data)
            self._write_fat(f, fat)

    def rename(self, entry: Entry, new_name: str) -> None:
        r = entry.ref
        with open(self.path, "r+b") as f:
            data = self._dir_data(f, r["folder"])
            _mark_deleted(data, r["offset"])
            used = _used_shortnames(data)
            entries = _build_entries(new_name, 0x20, r["cluster"], r["size"], used)
            _insert_entries(data, entries)
            self._write_dir(f, r["folder"], data)

    def append(self, files: list[str], folder: Optional[Entry] = None) -> int:
        """See Fat16Volume.append()'s docstring -- same reasoning."""
        from ..mpc2emu_bridge import fat32 as _fat32_mod
        fs = _fat32_mod.Fat32(self.path)
        try:
            cl = folder.ref if folder is not None else None
            n = 0
            for p in files:
                fs.add_file(p, Path(p).name, cl)
                n += 1
            return n
        finally:
            fs.close()


class Fat12Volume(WritableVolume):
    """FAT12 volume: Gotek/FlashFloppy 1.44 MB / 720 KB floppies for the
    K2000, flat root directory only (real K2000 media stores exactly one
    bank per floppy, so there is no subdirectory support to model). No
    partition table on real media -- the BPB always sits at LBA 0."""

    def __init__(self, path: str):
        self.path = str(path)
        with open(self.path, "rb") as f:
            self._geo = _read_bpb_fat12(f)

    def _read_fat(self, f) -> bytes:
        g = self._geo
        f.seek(g["fat_start"] * g["bps"])
        raw = f.read(g["fatsz"] * g["bps"])
        return raw.ljust(g["fatsz"] * g["bps"], b"\x00")

    def _write_fat(self, f, fat: bytearray) -> None:
        g = self._geo
        for i in range(g["nfats"]):
            f.seek((g["fat_start"] + i * g["fatsz"]) * g["bps"])
            f.write(fat)

    def _dir_data(self, f) -> bytearray:
        g = self._geo
        f.seek(g["root_start"] * g["bps"])
        return bytearray(f.read(g["root_sectors"] * g["bps"]))

    def _write_dir(self, f, data: bytearray) -> None:
        g = self._geo
        f.seek(g["root_start"] * g["bps"])
        f.write(data)

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:
        if folder is not None:
            raise ValueError("FAT12 floppies are flat (root directory only)")
        with open(self.path, "rb") as f:
            data = self._dir_data(f)
        out = []
        for e in _iter_dir_entries(data):
            if e["attr"] & _ATTR_DIR:
                continue   # floppies are flat; ignore any stray dir entry
            out.append(Entry(
                name=e["name"], kind=_classify(e["name"]), size=e["size"],
                ref={"folder": None, "offset": e["offset"], "cluster": e["cluster"],
                     "size": e["size"]}))
        return out

    def read(self, entry: Entry) -> bytes:
        g = self._geo
        r = entry.ref
        with open(self.path, "rb") as f:
            fat = self._read_fat(f)
            chain = _walk_chain(lambda n: _fat12_get(fat, n), r["cluster"],
                                 _FAT12_EOC_MIN, _FAT12_BAD, len(fat) * 2 // 3)
            buf = _read_chain(
                f, chain,
                lambda c: _cluster_offset(0, g["data_start"], g["bps"], g["spc"], c),
                g["bps"] * g["spc"])
        return bytes(buf[:r["size"]])

    def delete(self, entry: Entry) -> None:
        r = entry.ref
        with open(self.path, "r+b") as f:
            fat = bytearray(self._read_fat(f))
            n_entries = len(fat) * 2 // 3
            c = r["cluster"]
            seen = set()
            while 2 <= c < _FAT12_EOC_MIN and c != _FAT12_BAD:
                if c in seen or c >= n_entries:
                    break
                seen.add(c)
                nxt = _fat12_get(fat, c)
                _fat12_set(fat, c, 0)
                c = nxt
            data = self._dir_data(f)
            _mark_deleted(data, r["offset"])
            self._write_dir(f, data)
            self._write_fat(f, fat)

    def rename(self, entry: Entry, new_name: str) -> None:
        r = entry.ref
        with open(self.path, "r+b") as f:
            data = self._dir_data(f)
            _mark_deleted(data, r["offset"])
            used = _used_shortnames(data)
            entries = _build_entries(new_name, 0x20, r["cluster"], r["size"], used)
            _insert_entries(data, entries)
            self._write_dir(f, data)

    def append(self, files: list[str], folder: Optional[Entry] = None) -> int:
        """See Fat16Volume.append()'s docstring -- same reasoning."""
        if folder is not None:
            raise ValueError("FAT12 floppies are flat (root directory only)")
        from ..mpc2emu_bridge import fat12 as _fat12_mod
        fs = _fat12_mod.Fat12(self.path)
        try:
            n = 0
            for p in files:
                fs.add_file(p, Path(p).name)
                n += 1
            return n
        finally:
            fs.close()
