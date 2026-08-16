"""
AKAI S1000/S3000 disk-image reader — hard disk, CD3000 CD-ROM and floppy.

This is what makes an AKAI library disk browsable without the sampler: no
mounting, no drive-parameter overrides, just the partition table, the volume
directories and the FAT chains. The layout is documented in
``mpc2emu/docs/AKAI_S3000_FORMAT.md`` "Disk structure".

Structure, and how it maps onto this project's `Volume`/`Entry` pair:

    disk  ->  partitions (A, B, …, up to 18, laid end to end)
                ->  volumes (up to 100 per partition)   == FOLDER entries
                      ->  files (up to 510 per volume)  == BANK / OTHER_FILE

The partition level is deliberately **not** a tree level of its own. A real
disk usually has one partition, and the letter matters only to disambiguate
two volumes that share a name — so it rides in the folder's own name
(`A/STRINGS`) and in `meta`, rather than adding a level that would be empty
scenery on almost every disk.

Programs are surfaced as BANK entries and samples as OTHER_FILE, which is
this project's existing split between "playable content you would import"
and "the material it is made of".

Three media in one reader, because they differ in less than they look:

- **Hard disk / ZuluSCSI** (`.hda`, `.img`): partition header, root
  directory of volumes, FAT, 8 KB blocks.
- **CD3000 CD-ROM** (`.iso`): the identical partition format written raw —
  *not* ISO 9660, so it never reaches `iso9660.py`. Its volumes are typed
  0x07 and the three blocks after each partition header hold a flat index of
  the partition's files, which this reader ignores: that index is a cache
  the sampler browses, and the files themselves live where the FAT says. A
  disc whose index went stale still reads correctly here.
- **Floppy** (`.img`): 800 KB or 1.6 MB, no partition table and no root
  directory — the whole disk is one volume. 1 KB blocks. Not DOS-formatted
  (80 x 2 x 10 x 1024), which is why an ordinary PC drive reports it as
  unformatted and only an image of it is readable at all.

Read-only, unlike `emu3.py`/`fatvol.py`. Writing AKAI media means laying out
a FAT, a partition header and a checksum, and mpc2emu's writer for that is
verified byte-identical to `akaiutil`'s output over whole images; a second
implementation here would be a second thing to get wrong for no gain. See
`build/akai_image.py`, which drives that writer.
"""

from __future__ import annotations

import os
import struct
from typing import Optional

from .base import Entry, EntryKind, Volume
from ..banks import akai as vs_akai

# ── block geometry ───────────────────────────────────────────────────────────
HD_BLOCK = 0x2000                   # 8 KB
FL_BLOCK = 0x0400                   # 1 KB

PARTHEAD_BLKS = 3
VOLDIR_HD_BLKS = 2
VOLDIR_FL_BLKS = 12
FLL_HEAD_BLKS = 4                    # low-density floppy header
FLH_HEAD_BLKS = 5                    # high-density floppy header

FLL_SIZE = 0x0320                    # 800 blocks = 800 KB
FLH_SIZE = 0x0640                    # 1600 blocks = 1.6 MB

# ── capacity limits ──────────────────────────────────────────────────────────
PART_MAX_BLOCKS = 0x1E00             # 60 MB — the sampler's per-partition cap
MAX_PARTITIONS = 18
ROOTDIR_ENTRIES = 100                # volumes per partition
VOLDIR_ENTRIES = 510                 # files per volume

# ── offsets within a partition header ────────────────────────────────────────
_OFF_MAGIC = 0x0002
_OFF_ROOTDIR = 0x00CA
_OFF_FAT = 0x070A
_OFF_PARTTAB = 0x4400

_MAGICNUM, _MAGICVAL = 98, 3333

# ── FAT codes ────────────────────────────────────────────────────────────────
FAT_FREE = 0x0000
FAT_SYS = 0x4000                     # reserved for the system (headers)
FAT_DIREND = 0x8000                  # end of a volume-directory chain
FAT_FILEEND = 0xC000                 # end of a file chain

VOL_TYPE_INACTIVE = 0x00
VOL_TYPE_S1000 = 0x01
VOL_TYPE_S3000 = 0x03
VOL_TYPE_CD3000 = 0x07

#: Volume-directory shape by volume type: (blocks, entries). An S1000
#: volume's directory is ONE block of 126 entries, not the S3000's two of
#: 510, and the FAT cannot substitute for knowing that: an S1000 hard disk
#: terminates its directory chain with 0x4000, the very value the S3000 uses
#: for "reserved for system", so a chain-only walk cannot tell them apart.
#: Reading an S1000 volume with the S3000 shape walks past the directory and
#: invents files out of whatever follows -- on a third-party S1000 library
#: disc that turned 1 799 real files into 2 606, the surplus carrying type
#: bytes that map to nothing.
_VOLDIR_LAYOUT = {
    VOL_TYPE_S1000: (1, 126),
    VOL_TYPE_S3000: (VOLDIR_HD_BLKS, 510),
    VOL_TYPE_CD3000: (VOLDIR_HD_BLKS, 510),
}
_VOLDIR_DEFAULT = (VOLDIR_HD_BLKS, 510)

#: CD3000 file index: the three blocks right after the partition header.
CDINFO_BLK = PARTHEAD_BLKS
CDINFO_BLKS = 3

#: File type 0xFF — never a valid type — in a floppy header's first entry
#: slot is what marks it as carrying an S3000 volume directory.
_FL_S3000_FLAG_TYPE = 0xFF

_VOL_TYPE_NAMES = {VOL_TYPE_S1000: "S1000", VOL_TYPE_S3000: "S3000",
                   VOL_TYPE_CD3000: "CD3000"}


class AkaiImageError(ValueError):
    pass


def _u16(data: bytes, off: int) -> int:
    return struct.unpack_from("<H", data, off)[0]


def _u24(data: bytes, off: int) -> int:
    return data[off] | (data[off + 1] << 8) | (data[off + 2] << 16)


# ── detection ────────────────────────────────────────────────────────────────

def _has_parthead_magic(head: bytes) -> bool:
    """The 98 magic fields are what marks a medium as an AKAI hard disk.

    Several spread-out fields are checked rather than one, because magic
    field 0 is legitimately 0x0000 — testing it alone matches any zero-filled
    file, which on a machine full of disk images is a real false positive and
    not a theoretical one.
    """
    if len(head) < _OFF_ROOTDIR:
        return False
    return all(_u16(head, _OFF_MAGIC + 2 * i) == (i * _MAGICVAL) & 0xFFFF
               for i in (1, 2, 3, 17, 50, 97))


def _is_akai_floppy(head: bytes, size: int) -> bool:
    if size not in (FLL_SIZE * FL_BLOCK, FLH_SIZE * FL_BLOCK):
        return False
    return len(head) > 16 and head[16] == _FL_S3000_FLAG_TYPE


def matches(head: bytes, path: str) -> bool:
    """True if an already-read `head` (and, only if needed, the file's size)
    identifies AKAI media.

    Takes the head rather than re-reading it because `detect.sniff()` is on
    the library scanner's hot path — every file in the library reaches it, and
    a full scan of this author's library takes ~26 s as it is. A hard disk is
    settled from the head alone; only the floppy marker byte, which is a
    file-type value no real type uses, costs a `stat`.
    """
    if _has_parthead_magic(head):
        return True
    if len(head) > 16 and head[16] == _FL_S3000_FLAG_TYPE:
        try:
            return _is_akai_floppy(head, os.path.getsize(path))
        except OSError:
            return False
    return False


def is_akai_image(path: str) -> bool:
    """True if `path` is an AKAI disk or floppy image.

    `.img`, `.hda` and `.iso` are all shared extensions — an `.img` may be an
    MPC60 disk or a FAT12 floppy, an `.iso` may be a real ISO 9660 CD or an
    EMU3 one — so this decides on content, as everything in `detect.py` does.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(0x200)
    except OSError:
        return False
    return matches(head, path)


# ── FAT walking ──────────────────────────────────────────────────────────────

def _chain(fat: list[int], start: int, limit: int,
           end_codes: tuple[int, ...]) -> list[int]:
    """Follow a FAT chain from `start`, refusing to loop forever.

    A damaged image can point a block at itself or back into the chain. A
    library disk that has sat in a loft for thirty years is exactly where
    that shows up, so the walk is bounded and visited blocks are tracked —
    a browser must not hang on a bad disc, it must show what is readable.
    """
    out: list[int] = []
    seen: set[int] = set()
    b = start
    while 0 <= b < limit and len(out) < limit:
        if b in seen:
            raise AkaiImageError(f"FAT chain loops at block 0x{b:04x}")
        seen.add(b)
        out.append(b)
        nxt = fat[b] if b < len(fat) else FAT_FREE
        if nxt in end_codes or nxt in (FAT_SYS, FAT_FREE):
            # Reserved or free means the chain ran off its end without a
            # terminator: stop, but keep what was already found.
            return out
        b = nxt
    return out


def _coalesce(blocks: list[int]) -> list[tuple[int, int]]:
    """Consecutive block numbers -> (first, count) runs, so reading a file
    costs one seek per run rather than one per block. AKAI writes files
    contiguously whenever it can, so this is usually a single run — an 8 MB
    sample is 1024 blocks and would otherwise be 1024 syscalls."""
    runs: list[tuple[int, int]] = []
    for b in blocks:
        if runs and b == runs[-1][0] + runs[-1][1]:
            runs[-1] = (runs[-1][0], runs[-1][1] + 1)
        else:
            runs.append((b, 1))
    return runs


class AkaiVolume(Volume):
    """Read-only browse over an AKAI hard-disk, CD-ROM or floppy image."""

    def __init__(self, path: str):
        self.path = path
        self._size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(0x200)
        self._floppy = _is_akai_floppy(head, self._size)
        if not self._floppy and not _has_parthead_magic(head):
            raise AkaiImageError(
                f"{path} is not an AKAI disk image — the partition header "
                f"magic is missing, and it is not an 800 KB / 1.6 MB AKAI "
                f"floppy either.")
        self._parts: Optional[list[dict]] = None
        #: Files listed in a directory whose data is not in this image --
        #: see truncation_warning().
        self._truncated = 0
        #: Entries refused because their FAT chain cannot cover the size they
        #: declare -- see _list_files. Counted rather than silent so a disc
        #: that trips it can be recognised as unusual.
        self._short_chain = 0

    # ── structure ────────────────────────────────────────────────────────────

    def _partitions(self) -> list[dict]:
        if self._parts is None:
            self._parts = self._read_floppy() if self._floppy else self._read_harddisk()
        return self._parts

    def _read_harddisk(self) -> list[dict]:
        with open(self.path, "rb") as f:
            f.seek(_OFF_PARTTAB)
            table = f.read(0x200)
            # The partition table lives in the FIRST partition only, and its
            # entries give each partition's size in blocks; partitions are
            # then laid end to end. A corrupt count must not walk off the end
            # of the table into the tag names, hence the clamp.
            partnum = min(table[0x100], MAX_PARTITIONS) if len(table) > 0x100 else 0
            sizes = [_u16(table, 0x102 + 2 * i) for i in range(partnum)
                     if 0x104 + 2 * i <= len(table)]

            parts: list[dict] = []
            start = 0
            if not sizes:
                f.seek(0)
                sizes = [_u16(f.read(2), 0)]     # single partition, no table
            for pi, psize in enumerate(sizes):
                base = start * HD_BLOCK
                if psize == 0 or base >= self._size:
                    break                         # table over-declares
                start += psize
                f.seek(base)
                head = f.read(PARTHEAD_BLKS * HD_BLOCK)
                if not _has_parthead_magic(head):
                    continue                      # a DD partition, or junk
                nblocks = min(psize, _u16(head, 0) or psize, PART_MAX_BLOCKS)
                fat = [_u16(head, _OFF_FAT + 2 * i) for i in range(PART_MAX_BLOCKS)]
                parts.append(dict(
                    letter=chr(ord("A") + pi), base=base, nblocks=nblocks,
                    block=HD_BLOCK, fat=fat,
                    volumes=self._root_volumes(head, nblocks),
                    has_cdinfo=self._has_cdinfo(head),
                    is_cdrom=(self._has_cdinfo(head)
                              or self._has_cd3000_volumes(head)),
                ))
            return parts

    @staticmethod
    def _root_volumes(head: bytes, nblocks: int) -> list[dict]:
        vols = []
        for vi in range(ROOTDIR_ENTRIES):
            o = _OFF_ROOTDIR + 16 * vi
            if o + 16 > len(head) or head[o + 12] == VOL_TYPE_INACTIVE:
                continue
            vstart = _u16(head, o + 14)
            if vstart >= nblocks:
                continue
            vols.append(dict(name=vs_akai.akai_to_str(head[o:o + vs_akai.NAME_LEN]),
                             vtype=head[o + 12], start=vstart, index=vi))
        return vols

    @staticmethod
    def _has_cdinfo(head: bytes) -> bool:
        """Whether this partition carries the CD-ROM file index: the three
        blocks right after the header, marked reserved-for-system in the FAT
        like the header itself.

        Reported for information only; nothing here reads that index. It is a
        cache the sampler browses, the files live where the FAT says, and a
        disc whose index went stale still reads correctly here."""
        return all(_u16(head, _OFF_FAT + 2 * b) == FAT_SYS
                   for b in range(CDINFO_BLK, CDINFO_BLK + CDINFO_BLKS))

    @staticmethod
    def _has_cd3000_volumes(head: bytes) -> bool:
        """Whether any volume is typed CD3000 (0x07) rather than S3000.

        Separate from `_has_cdinfo` because the two are independent, which
        only real discs revealed: of eight commercial library CD-ROMs, seven
        are CD3000-typed but just three carry the info block, and one is
        typed plain S3000. Either alone would mislabel half of them."""
        return any(head[_OFF_ROOTDIR + 16 * i + 12] == VOL_TYPE_CD3000
                   for i in range(ROOTDIR_ENTRIES)
                   if _OFF_ROOTDIR + 16 * i + 12 < len(head))

    def _read_floppy(self) -> list[dict]:
        hd = self._size == FLH_SIZE * FL_BLOCK
        total = FLH_SIZE if hd else FLL_SIZE
        head_blks = FLH_HEAD_BLKS if hd else FLL_HEAD_BLKS
        with open(self.path, "rb") as f:
            head = f.read(head_blks * FL_BLOCK)
        # The header's 64 file-entry slots (24 bytes each) go unused on an
        # S3000 floppy; the FAT starts straight after them, and the volume
        # label straight after that.
        fat_at = 64 * 24
        fat = [_u16(head, fat_at + 2 * i) for i in range(total)]
        label_at = fat_at + total * 2
        name = vs_akai.akai_to_str(head[label_at:label_at + vs_akai.NAME_LEN])
        return [dict(
            letter="FL", base=0, nblocks=total, block=FL_BLOCK, fat=fat,
            volumes=[dict(name=name or "FLOPPY", vtype=VOL_TYPE_S3000,
                          start=head_blks, index=0, dir_blocks=VOLDIR_FL_BLKS)],
            is_cdrom=False, has_cdinfo=False,
        )]

    def _volume_dir(self, part: dict, vol: dict) -> tuple[bytes, int]:
        """One volume's directory bytes, and how many entries it may hold.

        The chain says WHERE the directory lives; the volume's type says how
        BIG it is, and both are needed -- see _VOLDIR_LAYOUT."""
        block = part["block"]
        fixed = vol.get("dir_blocks")
        if fixed:
            # A floppy's directory is a fixed run behind the header rather
            # than a FAT chain: its blocks are marked reserved, so chaining
            # would stop after the first one.
            blocks = list(range(vol["start"], vol["start"] + fixed))
            max_entries = 510
        else:
            dir_blks, max_entries = _VOLDIR_LAYOUT.get(vol["vtype"], _VOLDIR_DEFAULT)
            blocks = _chain(part["fat"], vol["start"], part["nblocks"],
                            (FAT_DIREND, FAT_FILEEND))[:dir_blks]
        return self._read_blocks(part["base"], blocks, block), max_entries

    def _read_blocks(self, base: int, blocks: list[int], block: int) -> bytes:
        out = bytearray()
        with open(self.path, "rb") as f:
            for first, count in _coalesce(blocks):
                f.seek(base + first * block)
                out += f.read(count * block)
        return bytes(out)

    # ── Volume interface ─────────────────────────────────────────────────────

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:
        if folder is None:
            return self._list_volumes()
        pi, vi = folder.ref
        parts = self._partitions()
        if pi >= len(parts):
            return []
        part = parts[pi]
        if vi >= len(part["volumes"]):
            return []
        return self._list_files(part, part["volumes"][vi])

    def _list_volumes(self) -> list[Entry]:
        out: list[Entry] = []
        for pi, part in enumerate(self._partitions()):
            for vi, vol in enumerate(part["volumes"]):
                name = vol["name"] or f"VOLUME {vol['index'] + 1}"
                # The partition letter is part of the displayed name on a
                # hard disk (two partitions may hold a volume of the same
                # name and neither is wrong), and meaningless on a floppy.
                label = name if self._floppy else f"{part['letter']}/{name}"
                out.append(Entry(
                    name=label, kind=EntryKind.FOLDER, ref=(pi, vi),
                    meta={"format": "AKAI", "akai_volume": True,
                          "partition": part["letter"],
                          "volume_name": name,
                          "media": ("floppy" if self._floppy
                                    else "cdrom" if part["is_cdrom"] else "harddisk"),
                          "vol_type": _VOL_TYPE_NAMES.get(vol["vtype"],
                                                          f"type {vol['vtype']}")}))
        return out

    def _list_files(self, part: dict, vol: dict) -> list[Entry]:
        try:
            dirbytes, max_entries = self._volume_dir(part, vol)
        except AkaiImageError:
            return []
        block, base, nblocks = part["block"], part["base"], part["nblocks"]
        out: list[Entry] = []
        for i in range(min(max_entries, len(dirbytes) // 24)):
            e = dirbytes[24 * i:24 * i + 24]
            ftype = e[16]
            if ftype in (0x00, _FL_S3000_FLAG_TYPE):
                continue                          # free slot, or the floppy marker
            size = _u24(e, 17)
            start = _u16(e, 20)
            if size == 0 or start >= nblocks:
                continue
            try:
                blocks = _chain(part["fat"], start, nblocks, (FAT_FILEEND,))
            except AkaiImageError:
                continue
            # A file whose blocks run past the end of the image is not there
            # to read. That happens for real: a half-downloaded disc image
            # keeps its whole partition table and directory -- those live at
            # the front -- so it lists its full contents and can only deliver
            # the beginning of them. Skipped and counted rather than served
            # short, because a truncated sample is not a smaller sample, it
            # is the wrong audio with a plausible length.
            if base + (max(blocks) + 1) * block > self._size:
                self._truncated += 1
                continue
            # The chain must actually COVER what the entry declares. A junk
            # directory slot whose type byte happens to land on a live value
            # would otherwise be emitted as a file: `_chain` does not raise
            # when a chain runs off its end into a free or reserved block, it
            # returns what it has, and `read()` truncates to `size` without
            # noticing it got less. A phantom PROGRAM is caught by the parser;
            # a phantom SAMPLE is raw PCM by definition and would enter a bank
            # as ordinary audio, which is the case worth closing.
            #
            # Measured before adding it: across 21 discs, 1843 volumes and
            # 49 984 emitted files, exactly 0 entries fail this — and 0 of the
            # 375 623 unallocated slots carry a non-zero type byte at all, so
            # the exposure was theoretical here. It is kept because that is a
            # property of the tools that wrote these discs (they clear the
            # type byte and little else), not of the format: a disc written by
            # something less thorough puts this straight into play.
            if len(blocks) * block < size:
                self._short_chain += 1
                continue
            name = vs_akai.akai_to_str(e[0:vs_akai.NAME_LEN])
            ext = vs_akai.ftype_to_ext(ftype)
            kind = (EntryKind.BANK if ftype in vs_akai.PROGRAM_TYPES
                    else EntryKind.OTHER_FILE)
            out.append(Entry(
                name=f"{name}.{ext}", kind=kind, size=size,
                ref=(base, block, tuple(blocks), size),
                meta={"format": "AKAI", "akai_type": ftype,
                      "akai_name": name,
                      "role": "program" if ftype in vs_akai.PROGRAM_TYPES
                              else "sample" if ftype in vs_akai.SAMPLE_TYPES
                              else "other"}))
        return out

    def read(self, entry: Entry) -> bytes:
        base, block, blocks, size = entry.ref
        return self._read_blocks(base, list(blocks), block)[:size]

    # ── convenience for the bank layer ───────────────────────────────────────

    def volume_files(self, folder: Entry) -> list[tuple[str, bytes]]:
        """Every file of one volume as `(filename, data)` — what
        `banks.akai.parse_volume()` takes."""
        return [(e.name, self.read(e)) for e in self.list(folder)]

    def volume_programs(self, folder: Entry) -> list:
        """Just the volume's programs, without reading a byte of its samples.

        For the library scanner, which wants program names and nothing else.
        Reading whole volumes there would mean pulling every sample's PCM
        off the disc: ten real library discs are 5.5 GB of audio against a
        few MB of programs, and a scan that costs the former to learn the
        latter is not a scan anyone will leave switched on."""
        out = []
        for e in self.list(folder):
            if e.meta.get("role") != "program":
                continue
            prog = vs_akai.parse_program(
                self.read(e), e.name,
                s3000=vs_akai.generation_of_ftype(e.meta.get("akai_type")),
                typed=True)
            if prog is not None:
                out.append(prog)
        return out

    def volume_bank(self, folder: Entry) -> vs_akai.AkaiBank:
        """One volume, parsed as an `AkaiBank`."""
        return vs_akai.parse_volume(
            self.volume_files(folder),
            name=folder.meta.get("volume_name", folder.name),
            path=f"{self.path}:{folder.name}",
            partition=folder.meta.get("partition", ""))

    def label(self) -> str:
        """The medium's own label: a CD3000 disc's, or a floppy's. A hard
        disk has none — its volumes carry the names."""
        parts = self._partitions()
        if self._floppy and parts and parts[0]["volumes"]:
            return parts[0]["volumes"][0]["name"]
        if parts and parts[0]["has_cdinfo"]:
            # Only the info block holds a label. A CD3000-typed disc without
            # one has no label to show, which is most of them.
            return self._cd_label(parts[0])
        return ""

    def _cd_label(self, part: dict) -> str:
        off = (part["base"] + CDINFO_BLK * HD_BLOCK
               + 2 + 2 * ROOTDIR_ENTRIES)
        with open(self.path, "rb") as f:
            f.seek(off)
            return vs_akai.akai_to_str(f.read(vs_akai.NAME_LEN))

    def declared_size(self) -> int:
        """How many bytes the image's own partition table says it should be.

        A partition table and its volume directories live at the front of the
        disc, so a half-downloaded image still lists its whole contents and
        can deliver only the beginning of them."""
        total = 0
        for part in self._partitions():
            total = max(total, part["base"] + part["nblocks"] * part["block"])
        return total

    def truncation_warning(self) -> str:
        """A sentence naming what this image is missing, or "" if it is
        whole. Worth surfacing rather than swallowing: without it, a partial
        download converts to a plausible-looking subset of a library and
        nothing says which files were never there."""
        declared = self.declared_size()
        if declared <= self._size and not self._truncated:
            return ""
        pct = 100.0 * self._size / declared if declared else 100.0
        return (f"this image holds {self._size:,} bytes of the "
                f"{declared:,} its own partition table declares ({pct:.0f}%)"
                + (f"; {self._truncated} file(s) listed in its directories are "
                   f"not in it and were skipped" if self._truncated else "")
                + ". It looks like an incomplete copy.")

    def media_kind(self) -> str:
        """'floppy' | 'cdrom' | 'harddisk' — what the image actually is,
        which its extension does not say."""
        if self._floppy:
            return "floppy"
        parts = self._partitions()
        return "cdrom" if parts and parts[0]["is_cdrom"] else "harddisk"
