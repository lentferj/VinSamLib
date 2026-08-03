"""
EMU3 filesystem reader — the format shared by E-mu EIII/ESI/EIV CD and
hard-disk images. This module is the read side that mpc2emu never needed:
mpc2emu only ever *appends* to or *creates* EMU3 images, so its parsing logic
lives buried inside ``writers.iso_builder.emu_hdd_append()``. That parsing
(superblock geometry, FAT, root directory, dir-content blocks) is lifted out
here into a standalone, importable reader — see
``mpc2emu/docs/EMU3_ISO_FORMAT.md`` §2-3 for the on-disk layout this mirrors.

Entries whose type marker is ``E4B0`` (an E-mu E4B bank) or whose content
starts with one of the three EIII/ESI identifier strings are surfaced as
BANK entries — the EMU3 *filesystem* is shared by EIII/ESI/EIV alike
(emu3fs), so an EMU3-filesystem disc commonly holds EIII-format bank data
alongside (or instead of) E4B banks, and ``banks/eiii.py`` can read it now.
Other EMU3 content (e.g. the EIII-era `.EFE`/ROM special files) is still
listed but left untyped.
"""

from __future__ import annotations

import struct
from typing import Optional

from .base import Entry, EntryKind, WritableVolume
from ..banks import eiii as vs_eiii

BSIZE = 512
BSIZE_BITS = 9
EMU3_MAGIC = b"EMU3"
EMU3_LAST_CLUSTER = 0x7FFF
EMU3_ENTRIES_PER_BLOCK = BSIZE // 32   # 16
EMU3_BLOCKS_PER_DIR = 7
EMU3_FTYPE_STD = 0x81   # regular bank
EMU3_FTYPE_UPD = 0x83   # regular bank, first file after a deleted one
EMU3_FTYPE_SYS = 0x80   # special ROM/system file (fixed ids, not a user bank)
E4B_PROPS = b"\x00E4B0"


class Emu3FormatError(ValueError):
    pass


def _cluster_size(cse: int) -> int:
    return (1 << (15 + cse - BSIZE_BITS)) * BSIZE


def _is_filler_pattern(raw: bytes) -> bool:
    """True if `raw` (a 32-byte directory entry) is a short repeating
    byte pattern across its ENTIRE length -- name, dtype, and block/
    cluster pointers all included. Real CD masters have been found
    stamping unused directory-block space with such a pattern instead of
    a known erase convention (confirmed byte-for-byte on real reference
    discs: one disc's unused root-directory block is 0x6c repeated 32
    times, another's is 0xcf/0x23 alternating) -- neither is zero-fill
    nor the 0xFF-fill convention already handled separately, so both were
    slipping through as garbage folder/file entries in the tree. A
    genuine entry never has this shape: its name is followed by a
    distinct dtype byte and varied little-endian block-pointer shorts,
    never a period-1/2 repeat spanning the whole 32 bytes."""
    for period in (1, 2):
        if all(raw[i] == raw[i % period] for i in range(len(raw))):
            return True
    return False


def _duplicate_blocks(meta: bytearray, block_offsets: list[int], block_size: int = BSIZE) -> set[bytes]:
    """Return the set of `block_size`-byte block contents that occur more
    than once among `block_offsets` (byte offsets into `meta`), ignoring
    an all-zero block -- that's the normal, expected shape for
    legitimately unused directory space. Seen verbatim on a real
    reference disc (an acoustic-bass library CD) where 3 of its 4
    root-directory blocks are byte-for-byte copies of each other --
    real-looking-but-bogus data (not a simple repeating pattern, so
    `_is_filler_pattern` alone doesn't catch it), apparently stale/reused
    buffer content the mastering process never actually cleared. A
    genuine root/dir-content block would never collide byte-for-byte
    with another distinct block by chance, so any block seen more than
    once here is untrustworthy in its entirety."""
    counts: dict[bytes, int] = {}
    for off in block_offsets:
        b = bytes(meta[off:off + block_size])
        if b != b"\x00" * block_size:
            counts[b] = counts.get(b, 0) + 1
    return {b for b, c in counts.items() if c > 1}


class Emu3Volume(WritableVolume):
    """One EMU3 image (CD or HD). Opens, parses, and closes the underlying
    file on every call rather than holding a persistent handle — this keeps
    the class safe to use from a background scan thread and avoids Windows
    file-locking surprises when a mutation follows a read."""

    def __init__(self, path: str):
        self.path = path
        # The FAT, unpacked once. read() used to re-read and re-unpack the
        # whole table on every single file -- 5.7 s of a library index run,
        # for a table that cannot change between two reads of an unmodified
        # image. Cached on the same terms the geometry above already is, and
        # dropped by every mutation this class performs.
        self._fat: Optional[tuple] = None
        self._parse_geometry()

    def _read_fat(self, f) -> tuple:
        if self._fat is None:
            n_fat = self.fat_blocks * (BSIZE // 2)
            f.seek(self.fat_start * BSIZE)
            self._fat = struct.unpack("<%dH" % n_fat, f.read(n_fat * 2))
        return self._fat

    # ── geometry / metadata parsing ─────────────────────────────────────────

    def _parse_geometry(self) -> None:
        with open(self.path, "rb") as f:
            sb = f.read(BSIZE)
        if sb[:4] != EMU3_MAGIC:
            raise Emu3FormatError(f"{self.path}: not an EMU3 filesystem")
        g = lambda i: struct.unpack_from("<I", sb, i * 4)[0]
        self.root_start, self.root_blocks = g(2), g(3)
        self.dircon_start, self.dircon_blocks = g(4), g(5)
        self.fat_start, self.fat_blocks = g(6), g(7)
        self.data_start, self.total_clusters = g(8), g(9)
        self.cse = sb[0x28]
        self.cluster_size = _cluster_size(self.cse)
        self.blocks_per_cluster = self.cluster_size // BSIZE

    def _read_meta(self) -> bytearray:
        with open(self.path, "rb") as f:
            return bytearray(f.read(self.data_start * BSIZE))

    def _folders(self, meta: bytearray) -> list[dict]:
        root_off = self.root_start * BSIZE
        root_entries = self.root_blocks * (BSIZE // 32)
        entries_per_block = BSIZE // 32
        dup_blocks = _duplicate_blocks(
            meta, [root_off + b * BSIZE for b in range(self.root_blocks)])
        out = []
        for i in range(root_entries):
            block_off = root_off + (i // entries_per_block) * BSIZE
            if bytes(meta[block_off:block_off + BSIZE]) in dup_blocks:
                continue
            eo = root_off + i * 32
            raw = meta[eo:eo + 32]
            raw_name = raw[:16]
            if raw_name == b"\xff" * 16:
                # Erased/unused root slot (0xFF fill, dtype also 0xFF, no
                # dir-content blocks) -- not a real folder. The empty-name
                # check below only catches an all-space/null slot, not this
                # convention, so real discs with deleted folders were
                # showing garbage "\xffP\xff..." entries in the tree.
                continue
            if _is_filler_pattern(raw):
                continue
            nm = raw_name.rstrip(b" \x00")
            if not nm and meta[eo + 17] == 0:
                continue
            blocks = [b for b in struct.unpack_from("<7h", meta, eo + 18) if b != -1]
            out.append({
                "name": nm.decode("latin-1"),
                "dtype": meta[eo + 17],
                "off": eo,
                "blocks": blocks,
            })
        return out

    def _bank_entries(self, meta: bytearray, folder: dict) -> list[tuple[int, dict]]:
        """Yield (entry_offset, fields) for every occupied dir-content slot
        in a folder, across all of its dir-content blocks."""
        out = []
        dup_blocks = _duplicate_blocks(meta, [blk * BSIZE for blk in folder["blocks"]])
        for blk in folder["blocks"]:
            if bytes(meta[blk * BSIZE:blk * BSIZE + BSIZE]) in dup_blocks:
                continue
            for e in range(EMU3_ENTRIES_PER_BLOCK):
                eo = blk * BSIZE + e * 32
                raw = meta[eo:eo + 32]
                if _is_filler_pattern(raw):
                    # Same blank-disc filler convention as _folders() --
                    # unused dir-content slots can carry it too, not just
                    # unused root-directory ones.
                    continue
                nm = raw[:16].rstrip(b" \x00")
                if not nm:
                    continue
                start_cluster, n_clusters, blks, brem = struct.unpack_from(
                    "<HHHH", meta, eo + 18)
                ftype = meta[eo + 26]
                props = bytes(meta[eo + 27:eo + 32])
                out.append((eo, {
                    "name": nm.decode("latin-1"),
                    "slot": meta[eo + 17],
                    "start_cluster": start_cluster,
                    "n_clusters": n_clusters,
                    "blks": blks,
                    "brem": brem,
                    "ftype": ftype,
                    "props": props,
                }))
        return out

    @staticmethod
    def _true_size(n_clusters: int, blks: int, brem: int, cluster_size: int) -> int:
        """Inverse of writers.iso_builder._alloc(): recover the exact byte
        length of a file from its (clusters, blks-in-last-cluster, bytes-
        valid-in-last-block) triple."""
        if n_clusters == 0:
            return 0
        return (n_clusters - 1) * cluster_size + (blks - 1) * BSIZE + brem

    # ── Volume interface ────────────────────────────────────────────────────

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:
        meta = self._read_meta()
        if folder is None:
            return [
                Entry(name=fo["name"], kind=EntryKind.FOLDER, ref=fo)
                for fo in self._folders(meta)
            ]
        fo = folder.ref
        out = []
        data_off = self.data_start * BSIZE
        bpc = self.blocks_per_cluster
        with open(self.path, "rb") as f:
            for eo, fields in self._bank_entries(meta, fo):
                size = self._true_size(fields["n_clusters"], fields["blks"],
                                         fields["brem"], self.cluster_size)
                # `props == '\x00E4B0'` is confirmed on some reference discs
                # (an industrial sound-design library) but all-zero on others
                # (a synth-and-drums library series) — not a reliable tag. And the
                # EMU3 *filesystem* is shared by EIII/ESI/EIV alike (emu3fs),
                # so an EMU3-filesystem disc can hold EIII-format bank data
                # rather than E4B ('FORM'...'E4B0') even when the directory-
                # entry file type is regular STD/UPD — check the actual
                # content header rather than trusting either.
                is_bank = False
                detected_format = "system"
                if fields["ftype"] in (EMU3_FTYPE_STD, EMU3_FTYPE_UPD) and fields["start_cluster"]:
                    f.seek(data_off + (fields["start_cluster"] - 1) * bpc * BSIZE)
                    head = f.read(16)
                    if head[:4] == b"FORM" and head[8:12] == b"E4B0":
                        is_bank = True
                        detected_format = "E4B"
                    elif vs_eiii.detect_format(head) is not None:
                        is_bank = True
                        detected_format = "EIII"
                    else:
                        detected_format = "unknown"
                out.append(Entry(
                    name=fields["name"],
                    kind=EntryKind.BANK if is_bank else EntryKind.OTHER_FILE,
                    size=size,
                    ref={"folder": fo, "entry_offset": eo, **fields},
                    meta={"format": detected_format,
                          "props_tag": fields["props"] == E4B_PROPS},
                ))
        return out

    def read(self, entry: Entry) -> bytes:
        r = entry.ref
        data_off = self.data_start * BSIZE
        bpc = self.blocks_per_cluster

        with open(self.path, "rb") as f:
            fat = self._read_fat(f)

            clusters = []
            c = r["start_cluster"]
            seen = set()
            while c and c != EMU3_LAST_CLUSTER:
                if c in seen or c >= len(fat):
                    raise Emu3FormatError(f"corrupt FAT chain reading '{entry.name}'")
                seen.add(c)
                clusters.append(c)
                c = fat[c]

            # Runs of consecutive clusters are read in one go: an EMU3 volume
            # is written once and in order, so a chain is nearly always
            # contiguous, and one seek+read per cluster was costing a syscall
            # apiece for nothing (1.06 M of them across this project's own
            # library index run).
            buf = bytearray()
            i, n = 0, len(clusters)
            while i < n:
                j = i + 1
                while j < n and clusters[j] == clusters[j - 1] + 1:
                    j += 1
                f.seek(data_off + (clusters[i] - 1) * bpc * BSIZE)
                buf += f.read((j - i) * self.cluster_size)
                i = j

        true_size = self._true_size(len(clusters), r["blks"], r["brem"], self.cluster_size)
        return bytes(buf[:true_size])

    # ── WritableVolume interface ────────────────────────────────────────────

    def delete(self, entry: Entry) -> None:
        """Free the entry's FAT chain and zero its 32-byte dircon slot —
        mirrors the proven 'overwrite' branch of
        writers.iso_builder.emu_hdd_append (iso_builder.py:915-920)."""
        self._fat = None            # this rewrites the FAT
        r = entry.ref
        with open(self.path, "r+b") as f:
            f.seek(0)
            meta = bytearray(f.read(self.data_start * BSIZE))
            fat_off = self.fat_start * BSIZE
            n_fat = self.fat_blocks * (BSIZE // 2)
            fat = list(struct.unpack_from("<%dH" % n_fat, meta, fat_off))

            c = r["start_cluster"]
            seen = set()
            while c and c != EMU3_LAST_CLUSTER and c < len(fat):
                if c in seen:
                    break
                seen.add(c)
                # NOT `c, fat[c] = fat[c], 0` -- Python assigns targets
                # left to right, so by the time the `fat[c]` target is
                # evaluated, `c` already holds the *new* value and the
                # wrong (or out-of-range) slot gets zeroed instead of the
                # cluster actually being freed.
                nxt = fat[c]
                fat[c] = 0
                c = nxt

            eo = r["entry_offset"]
            meta[eo:eo + 32] = b"\x00" * 32

            struct.pack_into("<%dH" % n_fat, meta, fat_off, *[v & 0xFFFF for v in fat])
            f.seek(0)
            f.write(meta)

    def rename(self, entry: Entry, new_name: str) -> None:
        r = entry.ref
        eo = r["entry_offset"]
        name_bytes = new_name.encode("ascii", "replace")[:16].ljust(16, b" ")
        with open(self.path, "r+b") as f:
            f.seek(eo)
            f.write(name_bytes)

    def append(self, files: list[str], folder: Optional[Entry] = None) -> int:
        """Delegates to mpc2emu's proven allocator rather than
        reimplementing cluster/slot allocation here."""
        from ..mpc2emu_bridge import iso_builder
        self._fat = None            # the allocator rewrites the FAT
        folder_name = folder.ref["name"].strip() if folder is not None else None
        return iso_builder.emu_hdd_append(self.path, files, folder=folder_name,
                                            on_duplicate="add-new")
