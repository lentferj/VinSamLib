"""
EMU3 filesystem reader — the format shared by E-mu EIII/ESI/EIV CD and
hard-disk images. This module is the read side that mpc2emu never needed:
mpc2emu only ever *appends* to or *creates* EMU3 images, so its parsing logic
lives buried inside ``writers.iso_builder.emu_hdd_append()``. That parsing
(superblock geometry, FAT, root directory, dir-content blocks) is lifted out
here into a standalone, importable reader — see
``mpc2emu/docs/EMU3_ISO_FORMAT.md`` §2-3 for the on-disk layout this mirrors.

Per the project's scope decision, only entries whose type marker is ``E4B0``
(an E-mu E4B bank) are surfaced as BANK entries; other EMU3 content (e.g. the
EIII-era `.EFE`/ROM special files) is listed but left untyped, since E-mu
support in VinSamLib is E4B-only.
"""

from __future__ import annotations

import struct
from typing import Optional

from .base import Entry, EntryKind, WritableVolume

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


class Emu3Volume(WritableVolume):
    """One EMU3 image (CD or HD). Opens, parses, and closes the underlying
    file on every call rather than holding a persistent handle — this keeps
    the class safe to use from a background scan thread and avoids Windows
    file-locking surprises when a mutation follows a read."""

    def __init__(self, path: str):
        self.path = path
        self._parse_geometry()

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
        out = []
        for i in range(root_entries):
            eo = root_off + i * 32
            raw_name = meta[eo:eo + 16]
            if raw_name == b"\xff" * 16:
                # Erased/unused root slot (0xFF fill, dtype also 0xFF, no
                # dir-content blocks) -- not a real folder. The empty-name
                # check below only catches an all-space/null slot, not this
                # convention, so real discs with deleted folders were
                # showing garbage "\xffP\xff..." entries in the tree.
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
        for blk in folder["blocks"]:
            for e in range(EMU3_ENTRIES_PER_BLOCK):
                eo = blk * BSIZE + e * 32
                nm = meta[eo:eo + 16].rstrip(b" \x00")
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
                # (e.g. "Post Industrial Cybr-Sound Depot") but all-zero on
                # others (e.g. "Formula 4000") — not a reliable tag. And the
                # EMU3 *filesystem* is shared by EIII/ESI/EIV alike (emu3fs),
                # so an EMU3-filesystem disc can hold EIII-format bank data
                # (header 'EMULATOR 3X ') rather than E4B ('FORM'...'E4B0')
                # even when the directory-entry file type is regular
                # STD/UPD. Per the E4B-only scope decision, only content
                # whose actual header says E4B is surfaced as a BANK; other
                # STD/UPD content (EIII banks, etc.) is listed but tagged
                # with its detected format instead.
                is_bank = False
                detected_format = "system"
                if fields["ftype"] in (EMU3_FTYPE_STD, EMU3_FTYPE_UPD) and fields["start_cluster"]:
                    f.seek(data_off + (fields["start_cluster"] - 1) * bpc * BSIZE)
                    head = f.read(12)
                    if head[:4] == b"FORM" and head[8:12] == b"E4B0":
                        is_bank = True
                        detected_format = "E4B"
                    elif head[:3] == b"EMU":
                        # EIII-family bank headers seen in the wild: "EMULATOR 3X",
                        # "EMU SI-32 v3" (ESI-32), plausibly others — all begin "EMU".
                        detected_format = "EIII (unsupported)"
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
            fat_off = self.fat_start * BSIZE
            n_fat = self.fat_blocks * (BSIZE // 2)
            f.seek(fat_off)
            fat = struct.unpack("<%dH" % n_fat, f.read(n_fat * 2))

            clusters = []
            c = r["start_cluster"]
            seen = set()
            while c and c != EMU3_LAST_CLUSTER:
                if c in seen or c >= len(fat):
                    raise Emu3FormatError(f"corrupt FAT chain reading '{entry.name}'")
                seen.add(c)
                clusters.append(c)
                c = fat[c]

            buf = bytearray()
            for cl in clusters:
                f.seek(data_off + (cl - 1) * bpc * BSIZE)
                buf += f.read(self.cluster_size)

        true_size = self._true_size(len(clusters), r["blks"], r["brem"], self.cluster_size)
        return bytes(buf[:true_size])

    # ── WritableVolume interface ────────────────────────────────────────────

    def delete(self, entry: Entry) -> None:
        """Free the entry's FAT chain and zero its 32-byte dircon slot —
        mirrors the proven 'overwrite' branch of
        writers.iso_builder.emu_hdd_append (iso_builder.py:915-920)."""
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
        folder_name = folder.ref["name"].strip() if folder is not None else None
        return iso_builder.emu_hdd_append(self.path, files, folder=folder_name,
                                            on_duplicate="add-new")
