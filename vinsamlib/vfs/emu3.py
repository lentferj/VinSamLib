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

from __future__ import annotations  # noqa: I001

import struct
from typing import Optional

from .base import Entry, EntryKind, WritableVolume
from ..banks import eiii as vs_eiii

BSIZE = 512
BSIZE_BITS = 9
EMU3_MAGIC = b"EMU3"
EMU3_LAST_CLUSTER = 0x7FFF
EMU3_ENTRIES_PER_BLOCK = BSIZE // 32  # 16
EMU3_BLOCKS_PER_DIR = 7
EMU3_FTYPE_STD = 0x81  # regular bank
EMU3_FTYPE_UPD = 0x83  # regular bank, first file after a deleted one
EMU3_FTYPE_SYS = 0x80  # special ROM/system file (fixed ids, not a user bank)
E4B_PROPS = b"\x00E4B0"

#: Bytes of an entry's own data that identify its bank format. `list()` reads
#: exactly this much per entry, and never less.
_SNIFF_BYTES = 16

#: How many bytes ONE read may cover when it is serving several entries at
#: once. See `Emu3Volume._sniff_heads` for why this exists at all.
_SNIFF_CHUNK = 1 << 22  # 4 MiB

#: How many bytes one `list()` may spend sniffing in TOTAL. Past this the
#: entries go back to one 16-byte read each -- the whole point of the caps is
#: that answering a sixteen-byte question must never cost a proportion of the
#: disk.
_SNIFF_BUDGET = 1 << 25  # 32 MiB


def _plausible_size_fields(
    n_clusters: int, blks: int, brem: int, blocks_per_cluster: int
) -> bool:
    """Can (clusters, blks, brem) describe a file at all?

    ``writers.iso_builder._alloc()`` is the only writer of these three
    fields, and what it guarantees is narrower than it first looks --
    ``blks`` and ``brem`` describe the LAST cluster, not the whole file:

        size == 0  ->  (1, 0, 0)      the zero-length sentinel
        size  > 0  ->  1 <= blks <= blocks_per_cluster, 1 <= brem <= 512

    so ``blks == 0`` on anything but that one sentinel means the size
    arithmetic is going to subtract a block that does not exist, which is
    how a size goes negative in the first place. ``brem`` is left alone: it
    never makes a size negative, and the only writer of these fields is
    mpc2emu's, so an out-of-range ``brem`` on a commercial disc is a
    convention nobody has verified rather than a corruption.

    The zero-length sentinel is KEPT, deliberately: it is what a real empty
    file looks like on disk, ``_true_size`` answers 0 for it once it stops
    subtracting that block, and dropping the entry instead would delete a
    real (if useless) bank from the tree.
    """
    if n_clusters == 0:
        # Nothing allocated, so nothing may be claimed for it.
        return not (blks or brem)
    if blks == 0:
        return n_clusters == 1 and brem == 0
    # `blks` counts blocks in the last cluster, never across the file.
    return blks <= blocks_per_cluster


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


def _duplicate_blocks(
    meta: bytearray, block_offsets: list[int], block_size: int = BSIZE
) -> set[bytes]:
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
        b = bytes(meta[off : off + block_size])
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
        self._fat: Optional[tuple] = None  # noqa: UP045
        self._parse_geometry()

    def _read_fat(self, f) -> tuple:
        if self._fat is None:
            n_fat = self.fat_blocks * (BSIZE // 2)
            f.seek(self.fat_start * BSIZE)
            self._fat = struct.unpack("<%dH" % n_fat, f.read(n_fat * 2))  # noqa: UP031
        return self._fat

    # ── geometry / metadata parsing ─────────────────────────────────────────

    def _parse_geometry(self) -> None:
        with open(self.path, "rb") as f:
            sb = f.read(BSIZE)
        if sb[:4] != EMU3_MAGIC:
            raise Emu3FormatError(f"{self.path}: not an EMU3 filesystem")

        def g(i: int) -> int:
            return struct.unpack_from("<I", sb, i * 4)[0]

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
            meta, [root_off + b * BSIZE for b in range(self.root_blocks)]
        )
        out = []
        for i in range(root_entries):
            block_off = root_off + (i // entries_per_block) * BSIZE
            if bytes(meta[block_off : block_off + BSIZE]) in dup_blocks:
                continue
            eo = root_off + i * 32
            raw = meta[eo : eo + 32]
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
            out.append(
                {
                    "name": nm.decode("latin-1"),
                    "dtype": meta[eo + 17],
                    "off": eo,
                    "blocks": blocks,
                }
            )
        return out

    def _bank_entries(self, meta: bytearray, folder: dict) -> list[tuple[int, dict]]:
        """Yield (entry_offset, fields) for every occupied dir-content slot
        in a folder, across all of its dir-content blocks."""
        out = []
        dup_blocks = _duplicate_blocks(meta, [blk * BSIZE for blk in folder["blocks"]])
        for blk in folder["blocks"]:
            if bytes(meta[blk * BSIZE : blk * BSIZE + BSIZE]) in dup_blocks:
                continue
            for e in range(EMU3_ENTRIES_PER_BLOCK):
                eo = blk * BSIZE + e * 32
                raw = meta[eo : eo + 32]
                if _is_filler_pattern(raw):
                    # Same blank-disc filler convention as _folders() --
                    # unused dir-content slots can carry it too, not just
                    # unused root-directory ones.
                    continue
                nm = raw[:16].rstrip(b" \x00")
                if not nm:
                    continue
                start_cluster, n_clusters, blks, brem = struct.unpack_from(
                    "<HHHH", meta, eo + 18
                )
                if not _plausible_size_fields(
                    n_clusters, blks, brem, self.blocks_per_cluster
                ):
                    # A slot whose (clusters, blocks, bytes) triple no file
                    # can have. Nothing downstream can size it, so there is
                    # nothing to list -- and leaving it in is how a size
                    # ends up negative and indexed.
                    continue
                ftype = meta[eo + 26]
                props = bytes(meta[eo + 27 : eo + 32])
                out.append(
                    (
                        eo,
                        {
                            "name": nm.decode("latin-1"),
                            "slot": meta[eo + 17],
                            "start_cluster": start_cluster,
                            "n_clusters": n_clusters,
                            "blks": blks,
                            "brem": brem,
                            "ftype": ftype,
                            "props": props,
                        },
                    )
                )
        return out

    @staticmethod
    def _true_size(n_clusters: int, blks: int, brem: int, cluster_size: int) -> int:
        """Inverse of writers.iso_builder._alloc(): recover the exact byte
        length of a file from its (clusters, blks-in-last-cluster, bytes-
        valid-in-last-block) triple."""
        if n_clusters == 0:
            return 0
        # A size is a length, so it cannot be negative. `blks == 0` makes
        # the middle term -512 and takes the whole sum below zero:
        # _true_size(1, 0, 0, 524288) == -512, and that -512 is what
        # `buf[:-512]` in read() then hands back -- 1536 bytes of the NEXT
        # file, filed under this entry's name. The zero-length sentinel
        # reaches 0; a corrupt slot is rejected in _bank_entries before it
        # gets here. This floor is the backstop for both.
        return max(0, (n_clusters - 1) * cluster_size + (blks - 1) * BSIZE + brem)

    # ── Volume interface ────────────────────────────────────────────────────

    def _sniff_heads(self, f, clusters: list[int], data_off: int) -> dict[int, bytes]:
        """First ``_SNIFF_BYTES`` of every cluster in ``clusters``.

        ONE SEEK AND ONE READ PER BATCH, not one per entry.

        ``list()`` used to issue a seek() + a read(16) for every bank entry
        it wanted a content header from -- 31 seeks and 32 reads for the
        31-bank EMU3 hard disk this project's own ``build_emu_hdd``
        produces, one 16-byte read per entry from offsets a whole cluster
        apart. The same shape cost 1.53 M reads and 8.6 s across a whole
        library index (``vfs/fatvol.py:227``), which is why ``read()``
        already coalesces the runs it walks into one seek+read each and why
        ``vfs/akai.py:_coalesce`` exists.

        A batch is as many start clusters as fit inside ``_SNIFF_CHUNK``
        bytes of the batch's first cluster. Entries written in one pass
        take consecutive start clusters -- that is the case that coalesces,
        and it is the normal one -- so a folder's whole bank list becomes a
        handful of reads. Clusters further apart than ``_SNIFF_CHUNK`` form
        a batch per cluster and keep exactly the syscall count they had
        before; nothing is slower, and nothing changes about which bytes
        are read for which cluster.

        THE CAPS ARE THE POINT, not an afterthought. Those 16 useful bytes
        sit a whole cluster apart, so serving N entries from one read means
        reading the span between the first and the last -- 1 MiB of bank data
        per entry on a 1 GB disk. ``_SNIFF_CHUNK`` bounds any single read,
        ``_SNIFF_BUDGET`` bounds what one listing may spend, and past the
        budget every entry goes back to its own 16-byte read. Reading a
        gigabyte to answer a sixteen-byte question is not a win however few
        syscalls it costs.
        """
        heads: dict[int, bytes] = {}
        stride = self.cluster_size
        bpc = self.blocks_per_cluster
        spent = 0
        batch: list[int] = []
        base = 0  # byte offset of batch[0]

        def flush() -> None:
            nonlocal batch, spent
            if not batch:
                return
            want = (batch[-1] - batch[0]) * stride + _SNIFF_BYTES
            f.seek(base)
            buf = f.read(want)
            if len(buf) < want:
                # A truncated image ends a read early. Zero-fill rather
                # than shift, exactly as fatvol._read_chain does: a short
                # read must not move a later entry's header.
                buf = buf.ljust(want, b"\x00")
            for c in batch:
                # The position in the BATCH is not the byte offset: cluster
                # c sits (c - batch[0]) clusters in, which is the same thing
                # only when every cluster in the run is consecutive. The
                # scattered case is exactly where those two part company.
                rel = (c - batch[0]) * stride
                heads[c] = buf[rel : rel + _SNIFF_BYTES]
            spent += want
            batch = []

        for c in clusters:
            off = data_off + (c - 1) * bpc * BSIZE
            span = off - base + _SNIFF_BYTES if batch else _SNIFF_BYTES
            if batch and span <= _SNIFF_CHUNK and spent + span <= _SNIFF_BUDGET:
                batch.append(c)
                continue
            flush()
            batch = [c]
            base = off
        flush()
        return heads

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:  # noqa: UP045
        meta = self._read_meta()
        if folder is None:
            return [
                Entry(name=fo["name"], kind=EntryKind.FOLDER, ref=fo)
                for fo in self._folders(meta)
            ]
        fo = folder.ref
        out = []
        data_off = self.data_start * BSIZE
        entries = self._bank_entries(meta, fo)
        with open(self.path, "rb") as f:
            # Every entry that wants a content header, fetched in batches
            # rather than one 16-byte read each -- see _sniff_heads. The set
            # is deduplicated: several slots can name the same start
            # cluster, and there is no reason to read it twice.
            wanted = sorted(
                {
                    fields["start_cluster"]
                    for _eo, fields in entries
                    if fields["ftype"] in (EMU3_FTYPE_STD, EMU3_FTYPE_UPD)
                    and fields["start_cluster"]
                }
            )
            heads = self._sniff_heads(f, wanted, data_off) if wanted else {}
            for eo, fields in entries:
                size = self._true_size(
                    fields["n_clusters"],
                    fields["blks"],
                    fields["brem"],
                    self.cluster_size,
                )
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
                if (
                    fields["ftype"] in (EMU3_FTYPE_STD, EMU3_FTYPE_UPD)
                    and fields["start_cluster"]
                ):
                    head = heads[fields["start_cluster"]]
                    if head[:4] == b"FORM" and head[8:12] == b"E4B0":
                        is_bank = True
                        detected_format = "E4B"
                    elif vs_eiii.detect_format(head) is not None:
                        is_bank = True
                        detected_format = "EIII"
                    else:
                        detected_format = "unknown"
                out.append(
                    Entry(
                        name=fields["name"],
                        kind=EntryKind.BANK if is_bank else EntryKind.OTHER_FILE,
                        size=size,
                        ref={"folder": fo, "entry_offset": eo, **fields},
                        meta={
                            "format": detected_format,
                            "props_tag": fields["props"] == E4B_PROPS,
                        },
                    )
                )
        return out

    def read(self, entry: Entry) -> bytes:
        r = entry.ref
        data_off = self.data_start * BSIZE
        bpc = self.blocks_per_cluster

        with open(self.path, "rb") as f:
            fat = self._read_fat(f)

            # Follow the chain and read it in one pass. Runs of consecutive
            # clusters become a single seek + read: an EMU3 volume is written
            # once and in order, so a chain is nearly always contiguous, and
            # one seek+read per cluster was costing a syscall apiece for
            # nothing (1.06 M of them across this project's own library index
            # run). The run is accumulated while walking rather than in a
            # second pass over the cluster list, because on a 100 MB bank
            # that list is tens of thousands of entries and each extra Python
            # pass over it is measurable on its own.
            buf = bytearray()
            n_clusters = 0
            run_start = run_len = 0
            c = r["start_cluster"]
            seen = set()
            cluster_size = self.cluster_size

            def flush(start: int, length: int) -> None:
                if length:
                    f.seek(data_off + (start - 1) * bpc * BSIZE)
                    buf.extend(f.read(length * cluster_size))

            while c and c != EMU3_LAST_CLUSTER:
                if c in seen or c >= len(fat):
                    raise Emu3FormatError(f"corrupt FAT chain reading '{entry.name}'")
                seen.add(c)
                n_clusters += 1
                if run_len and c == run_start + run_len:
                    run_len += 1
                else:
                    flush(run_start, run_len)
                    run_start, run_len = c, 1
                c = fat[c]
            flush(run_start, run_len)

        true_size = self._true_size(n_clusters, r["blks"], r["brem"], self.cluster_size)
        return bytes(buf[:true_size])

    # ── WritableVolume interface ────────────────────────────────────────────

    def delete(self, entry: Entry) -> None:
        """Free the entry's FAT chain and zero its 32-byte dircon slot —
        mirrors the proven 'overwrite' branch of
        writers.iso_builder.emu_hdd_append (iso_builder.py:915-920)."""
        self._fat = None  # this rewrites the FAT
        r = entry.ref
        with open(self.path, "r+b") as f:
            f.seek(0)
            meta = bytearray(f.read(self.data_start * BSIZE))
            fat_off = self.fat_start * BSIZE
            n_fat = self.fat_blocks * (BSIZE // 2)
            fat = list(struct.unpack_from("<%dH" % n_fat, meta, fat_off))  # noqa: UP031

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
            meta[eo : eo + 32] = b"\x00" * 32

            struct.pack_into("<%dH" % n_fat, meta, fat_off, *[v & 0xFFFF for v in fat])  # noqa: UP031
            f.seek(0)
            f.write(meta)

    def rename(self, entry: Entry, new_name: str) -> None:
        r = entry.ref
        eo = r["entry_offset"]
        name_bytes = new_name.encode("ascii", "replace")[:16].ljust(16, b" ")
        with open(self.path, "r+b") as f:
            f.seek(eo)
            f.write(name_bytes)

    def append(self, files: list[str], folder: Optional[Entry] = None) -> int:  # noqa: UP045
        """Delegates to mpc2emu's proven allocator rather than
        reimplementing cluster/slot allocation here."""
        # Imported here, not at module scope: vfs is imported by the tree
        # model on startup and build/ pulls in the whole mpc2emu bridge.
        from ..build import calllog
        from ..mpc2emu_bridge import iso_builder

        self._fat = None  # the allocator rewrites the FAT
        folder_name = folder.ref["name"].strip() if folder is not None else None
        return calllog.traced(
            iso_builder.emu_hdd_append,
            self.path,
            files,
            folder=folder_name,
            on_duplicate="add-new",
        )
