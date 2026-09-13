"""
Safety wrapper around every operation that creates or mutates a real disk
image, for UI use. Two rules, unconditionally:

1. Never touches stdin — every mpc2emu builder that can prompt on a name
   collision (`on_duplicate='prompt'`) is always called with an explicit
   'add-new' / 'skip' / 'overwrite' policy instead.
2. Any in-place mutation of an EXISTING image (append / delete / rename)
   happens on a `<image>.vinsamlib-tmp` copy first, swapped into place with
   `os.replace()` only once the mutation fully succeeds — a crash or
   exception mid-operation leaves the original file completely untouched.
   Creating a brand-new image has nothing to protect, so it writes directly.

Dispatch mirrors mpc2emu's own `convert.py` (the one place all of this logic
was already proven against real hardware): appending an E4B bank picks
`emu_hdd_append` or `fat_hda_append` depending on what filesystem the target
`.hda`/`.iso` actually has (`hda_builder.detect_hda_fs`), while KRZ always
goes through `k2000_disk_append` — K2000 media has no EMU-fs equivalent.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .. import tempdirs
from . import akai_image
from . import calllog
from ..filenames import safe_path_component
from ..mpc2emu_bridge import fat12, hda_builder, iso_builder
from ..vfs.base import Entry, EntryKind
from ..vfs.detect import open_volume

# kind -> (bank format it holds, human label, default volume label)
IMAGE_KINDS: dict[str, tuple[str, str, str]] = {
    "emu3_cd": ("E4B", "EMU3 CD (E4XT/EOS ZuluSCSI CD-ROM)", "EMU_BANK"),
    "emu3_hd_emu": ("E4B", "EMU3 HD — native EMU filesystem (any EOS)", "EMU_DISK"),
    "emu3_hd_fat": ("E4B", "EMU3 HD — FAT filesystem (EOS 4.7+)", "EMU_DISK"),
    "k2000_fat16": ("KRZ", "K2000 FAT16 disk (CD or SCSI HD, any OS)", "K2000"),
    "k2000_iso9660": ("KRZ", "K2000 ISO 9660 CD (OS v3.87+, burn-once)", "K2000"),
    "fat12_floppy": ("KRZ", "Gotek FAT12 floppy", "K2000"),
    # AKAI media. Content is a FOLDER per volume, not a bank file -- an AKAI
    # volume is a set of files (see build/akai_image.py). Gated on
    # Config.check_akai_write_support(); nothing offers these unless an
    # mpc2emu checkout with the writer is configured, and the branch they
    # live on stays unmerged until an S3000XL has mounted one.
    "akai_hd": ("AKAI", "AKAI S3000 hard disk (SCSI/ZuluSCSI)", "VOLUME 001"),
    "akai_cd3000": ("AKAI", "AKAI CD3000 CD-ROM (raw, not ISO 9660)", "VOLUME 001"),
    "akai_floppy": ("AKAI", "AKAI floppy, 1.6 MB high density", "VOLUME 001"),
}

#: The kinds whose `bank_paths` are folders rather than files.
FOLDER_INPUT_KINDS = frozenset(akai_image.AKAI_IMAGE_KINDS)

# Kinds append_banks() can grow via a *fast, true in-place* append (mpc2emu
# has a real incremental-append function and the image was built with spare
# room for it). emu3_cd is deliberately absent: iso_builder.build_iso takes
# no size_mb and is always exact-fit, so incremental append always fails —
# but append_banks() still handles it, by falling back to a full rebuild
# (see _rebuild_emu3_with_extra_banks) the moment mpc2emu reports no free
# clusters. A real ZuluSCSI "CD" is just a file on an SD card, not an
# actually-burned disc, so there is no reason to treat it as unappendable
# the way a real CD-R would have to be. k2000_iso9660 has no such fallback:
# it's read through Iso9660Volume, which is read-only (mpc2emu never
# implemented an ISO 9660 writer beyond the initial build), so it's the one
# kind that is genuinely create-once here — matching a real K2000 factory
# CD before OS v3.87 made ISO 9660 readable at all. A floppy's ~1.4 MB
# leaves no realistic room to grow either way.
APPENDABLE_KINDS = ({"emu3_cd", "emu3_hd_emu", "emu3_hd_fat", "k2000_fat16"}
                    | set(akai_image.AKAI_APPENDABLE))


class ImageOpError(RuntimeError):
    """Raised for any failed image operation; message is safe to show the user."""


def _run_captured(fn: Callable, *args, **kwargs) -> tuple[Any, str]:
    # Recorded into the debug call log for the same reason convert.py's
    # namesake is: writing an image is the far end of the same path, and a
    # log that stopped at the importer would answer "how was this bank made"
    # but not "how did it get onto the disc" -- which is where the object
    # budget, the partition layout and the volume naming are decided.
    #
    # Unlike convert.py's, this one hands its captured log back to the
    # caller, which already shows it. Recording it as well is not redundant:
    # that copy is shown once and discarded, and the question this log
    # answers is always asked later.
    buf = io.StringIO()
    started = time.monotonic()
    try:
        with contextlib.redirect_stdout(buf):
            result = fn(*args, **kwargs)
    except Exception as ex:
        calllog.record_call(fn, args, kwargs, ok=False,
                            seconds=time.monotonic() - started,
                            output=buf.getvalue(), error=str(ex))
        raise ImageOpError(f"{buf.getvalue()}\n\n{ex}".strip()) from ex
    calllog.record_call(fn, args, kwargs, ok=True,
                        seconds=time.monotonic() - started,
                        output=buf.getvalue())
    return result, buf.getvalue()


def _cleanup_partial(path: str) -> None:
    p = Path(path)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


# ── creating a brand-new image ──────────────────────────────────────────────

def create_image(kind: str, output_path: str, bank_paths: list[str],
                  volume_label: str = "", size_mb: Optional[int] = None,
                  floppy_kind: str = "1440",
                  partitions: Optional[list] = None) -> str:
    """Build a brand-new image at `output_path` containing `bank_paths` (may
    be empty for the appendable kinds, to create a blank starter image).
    Returns the captured build log."""
    if kind not in IMAGE_KINDS:
        raise ImageOpError(f"unknown image kind: {kind}")
    if Path(output_path).exists():
        raise ImageOpError(f"{output_path} already exists — choose a new name.")

    _, default_label, _ = IMAGE_KINDS[kind]
    label = volume_label or IMAGE_KINDS[kind][2]

    if kind in FOLDER_INPUT_KINDS:
        # AKAI takes folders, one per volume -- see build/akai_image.py. Its
        # own gate raises AkaiWriteUnavailable, which is already a sentence
        # fit to show, so it just changes class here.
        try:
            # `volume_label` raw, not the defaulted `label`: an AKAI volume
            # already has a name -- its folder's -- and that is more specific
            # than this table's generic fallback. Passing the default made a
            # floppy staged as VOL1 come back named "VOLUME 001".
            return akai_image.create_image(kind, output_path, bank_paths,
                                            volume_label=volume_label,
                                            size_mb=size_mb,
                                            partitions=partitions)
        except Exception as ex:
            _cleanup_partial(output_path)
            raise ImageOpError(str(ex)) from ex

    try:
        if kind == "emu3_cd":
            if not bank_paths:
                raise ImageOpError("a CD image needs at least one bank.")
            _, log = _run_captured(iso_builder.build_iso, bank_paths, output_path, label)
        elif kind == "emu3_hd_emu":
            sz = size_mb or (iso_builder.auto_hda_size_mb(bank_paths, "emu") if bank_paths else 1024)
            _, log = _run_captured(hda_builder.build_hda_emu, output_path, label, bank_paths, sz)
        elif kind == "emu3_hd_fat":
            sz = size_mb or (iso_builder.auto_hda_size_mb(bank_paths, "fat") if bank_paths else 1024)
            _, log = _run_captured(hda_builder.build_hda_fat, output_path, sz, label, bank_paths)
        elif kind == "k2000_fat16":
            _, log = _run_captured(iso_builder.build_k2000_disk, bank_paths, output_path,
                                    label, "BANKS", size_mb)
        elif kind == "k2000_iso9660":
            if not bank_paths:
                raise ImageOpError("an ISO 9660 image needs at least one bank "
                                    "(this format can't be appended to later).")
            _, log = _run_captured(iso_builder.build_iso_9660, bank_paths, output_path, label)
        elif kind == "fat12_floppy":
            log = _build_floppy(output_path, bank_paths, label, floppy_kind)
        else:  # pragma: no cover - guarded above
            raise ImageOpError(f"unknown image kind: {kind}")
    except ImageOpError:
        _cleanup_partial(output_path)
        raise
    except Exception as ex:
        _cleanup_partial(output_path)
        raise ImageOpError(str(ex)) from ex
    return log


def _build_floppy(output_path: str, bank_paths: list[str], label: str, floppy_kind: str) -> str:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fs = fat12.format_new(output_path, floppy_kind, label[:11])
            try:
                for p in bank_paths:
                    fs.add_file(p, Path(p).name)
            finally:
                fs.close()
    except Exception as ex:
        raise ImageOpError(f"{buf.getvalue()}\n\n{ex}".strip()) from ex
    return buf.getvalue()


# ── safe in-place mutation (append / delete / rename) ───────────────────────

def _mutate_in_place(image_path: str, mutate: Callable[[str], Any]) -> Any:
    """Copy `image_path` to a sibling `.vinsamlib-tmp` file, run `mutate` on
    the COPY's path, then atomically swap it into place. The original is
    never touched until `mutate` has fully succeeded; on any failure the
    temp copy is discarded and the original is left exactly as it was."""
    src = Path(image_path)
    if not src.exists():
        raise ImageOpError(f"{image_path} does not exist.")
    tmp = src.with_name(src.name + ".vinsamlib-tmp")
    # The COPY can fail too, and the way it fails matters: a full disk raises
    # here, outside the try below, and left a half-written .vinsamlib-tmp
    # sitting beside the image -- consuming the very space that was short, and
    # looking to the user like a damaged second copy of their disc. The
    # original is safe either way (nothing unlinks it; os.replace only runs
    # after a clean mutate), but failing tidily is part of failing safely.
    try:
        shutil.copy2(src, tmp)
    except OSError as ex:
        tmp.unlink(missing_ok=True)
        free = shutil.disk_usage(src.parent).free
        raise ImageOpError(
            f"Could not stage a working copy of {src.name}: {ex}. "
            f"{free / 1048576:.0f} MB free in {src.parent} — this needs room "
            f"for a second copy of the image ({src.stat().st_size / 1048576:.0f} MB)."
        ) from ex
    # Messages name the USER'S file, not the staging copy. Every writer is
    # handed the temp path, so their refusals came back saying "no room on
    # hd0.img.vinsamlib-tmp" -- a filename the user has never seen, attached
    # to the one moment they are being told something went wrong.
    def _own_name(text: str) -> str:
        return text.replace(tmp.name, src.name)

    try:
        result = mutate(str(tmp))
    except ImageOpError as ex:
        tmp.unlink(missing_ok=True)
        raise ImageOpError(_own_name(str(ex))) from ex
    except Exception as ex:
        tmp.unlink(missing_ok=True)
        raise ImageOpError(_own_name(str(ex))) from ex
    os.replace(tmp, src)   # same directory as src -> atomic, incl. on Windows
    # The success path can carry the staging name too -- the rebuild
    # fallback reports which image it grew.
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], str):
        result = (result[0], _own_name(result[1]))
    elif isinstance(result, str):
        result = _own_name(result)
    return result


#: mpc2emu's phrasing when an AKAI disc cannot take another volume. Matched
#: on the message because their appender raises one AkaiImageError for every
#: kind of refusal, and a rebuild is only the right answer to this one.
def _akai_out_of_room(message: str) -> bool:
    low = message.lower()
    return "no room" in low or "free volume slot" in low or "free blocks" in low


def _rebuild_akai_with_extra_volumes(image_path: str, folders: list[str],
                                      on_duplicate: str) -> tuple[int, str]:
    """Rebuild an AKAI image large enough to hold what it has plus `folders`.

    The AKAI counterpart of _rebuild_emu3_with_extra_banks: read every volume
    already on the disc, write each one back out as a folder of loose files,
    and build a fresh image from the old and the new together. Nothing is
    appended in place, so the partition table is laid out once for the final
    content -- which is the only way an AKAI disc legitimately gets bigger.
    """
    from . import akai_image as vs_akai_image

    vol = open_volume(image_path)
    if vol is None:
        raise ImageOpError(f"{Path(image_path).name}: not a recognised image")
    kind = "akai_cd3000" if getattr(vol, "media_kind", lambda: "")() == "cdrom" \
        else "akai_hd"
    with tempdirs.temp_dir("vinsamlib_akai_regrow_") as staging:
        existing: list[str] = []
        existing_names: set[str] = set()
        try:
            for entry in vol.list():
                name = safe_path_component(
                    (entry.meta.get("volume_name") or entry.name).strip(),
                    fallback=f"VOL{len(existing)}")
                out = Path(staging) / name
                i = 2
                while out.exists():
                    out = Path(staging) / f"{name}_{i}"
                    i += 1
                out.mkdir(parents=True)
                for fname, data in vol.volume_files(entry):
                    (out / fname).write_bytes(data)
                existing.append(str(out))
                existing_names.add(name.upper())
        finally:
            vol.close()

        combined = list(existing)
        added = 0
        for f in folders:
            stem = Path(f).name.upper()
            if stem in existing_names:
                if on_duplicate == "skip":
                    continue
                if on_duplicate == "overwrite":
                    combined = [c for c in combined
                                if Path(c).name.upper() != stem]
            combined.append(f)
            added += 1
        if not added:
            return 0, "nothing to add"

        # NO SIZE FROM HERE. mpc2emu's builder auto-sizes when size_mb is
        # None, and the rule follows from partition planning -- which is
        # format knowledge this side should not be holding a second copy of.
        # The first draft computed content x 1.25 + 16 MB against their
        # max(content x 1.25, 8 MB): both defensible, neither the same, and
        # two tools quietly disagreeing about how big one disc should be is
        # the kind of drift that surfaces months later as a bug in whichever
        # one is looked at second.
        #
        # The split that does hold: the POLICY is ours -- when to rebuild,
        # that it happens only on a no-room refusal, that a failure leaves
        # the original untouched. The NUMBER is theirs.
        rebuilt = Path(staging) / "rebuilt.img"
        vs_akai_image.create_image(kind, str(rebuilt), combined, size_mb=None)
        grown_mb = rebuilt.stat().st_size / 1048576
        shutil.copyfile(rebuilt, image_path)
    return added, (f"rebuilt {Path(image_path).name} at {grown_mb:.0f} MB to "
                   f"fit {len(combined)} volume(s)")


def append_banks(image_path: str, bank_format: str, bank_paths: list[str],
                  folder: Optional[str] = None, on_duplicate: str = "add-new") -> tuple[int, str]:
    """Append `bank_paths` (all of format `bank_format`: 'E4B', 'EIII' or
    'KRZ') into an existing image. Returns (count actually added, captured
    log). EIII takes the exact same branch as E4B -- it shares E4B's whole
    EMU3-filesystem container, and mpc2emu's iso_builder/hda_builder
    append functions are bank-content-agnostic (the one format-specific
    detail, the dir-content entry's props tag, mpc2emu itself now derives
    from each bank's own bytes rather than assuming E4B -- see mpc2emu's
    `_bank_props()`, added alongside EIII output support)."""
    def _do(tmp_path: str) -> tuple[int, str]:
        if bank_format == "AKAI":
            try:
                return akai_image.append_volumes(tmp_path, bank_paths,
                                                  on_duplicate=on_duplicate)
            except Exception as ex:
                # OUT OF ROOM IS NOT THE END, and E4B has said so for a long
                # time: emu_hdd_append gives up on free clusters and
                # _rebuild_emu3_with_extra_banks rebuilds the image at
                # whatever size the content needs, carrying the existing
                # banks across. An AKAI disc had no such fallback and simply
                # refused, which is a different answer to the same question
                # for no reason the user can see.
                #
                # mpc2emu's appender will NOT grow the file in place, and is
                # right not to: a partition table declares its own size, so
                # writing past it lands data the directory cannot address --
                # their clamp turns that silent corruption into this loud
                # refusal. Rebuilding is the sanctioned way to get a bigger
                # disc, which is exactly what EMU3 does here.
                if not _akai_out_of_room(str(ex)):
                    raise ImageOpError(str(ex)) from ex
                try:
                    return _rebuild_akai_with_extra_volumes(
                        tmp_path, bank_paths, on_duplicate)
                except ImageOpError:
                    raise
                except Exception as rex:
                    raise ImageOpError(
                        f"{ex}\n\nRebuilding it larger also failed: {rex}"
                    ) from rex
        if bank_format in ("E4B", "EIII"):
            fs = hda_builder.detect_hda_fs(tmp_path)
            if fs == "emu":
                try:
                    return _run_captured(iso_builder.emu_hdd_append, tmp_path,
                                          bank_paths, folder, on_duplicate)
                except ImageOpError as ex:
                    if "not enough free clusters" not in str(ex):
                        raise
                    return _rebuild_emu3_with_extra_banks(tmp_path, bank_paths, on_duplicate)
            fn = hda_builder.fat_hda_append
        elif bank_format == "KRZ":
            fn = iso_builder.k2000_disk_append
        else:
            raise ImageOpError(f"unknown bank format: {bank_format}")
        return _run_captured(fn, tmp_path, bank_paths, folder, on_duplicate)
    return _mutate_in_place(image_path, _do)


def _rebuild_emu3_with_extra_banks(tmp_path: str, extra_paths: list[str],
                                    on_duplicate: str) -> tuple[int, str]:
    """Fallback for an exact-fit EMU3 image (built by `iso_builder.build_iso`,
    which takes no `size_mb` and so never has spare clusters): there's no
    incremental append possible, but the same visible effect — "this bank is
    now on the image" — is achieved by exporting every bank already on the
    image to temp files, combining them with the new ones (respecting
    `on_duplicate` by filename stem), and rebuilding the whole image fresh
    in `tmp_path`'s place. `tmp_path` is itself already a throwaway copy
    (see `_mutate_in_place`), so rebuilding it in place here is still safe —
    the real image is untouched until the caller's own swap succeeds."""
    from ..vfs.emu3 import Emu3Volume

    vol = Emu3Volume(tmp_path)
    export_dir = Path(tempfile.mkdtemp(prefix="vinsamlib_rebuild_"))
    try:
        existing_paths: list[str] = []
        existing_names: set[str] = set()
        for folder_entry in vol.list():
            for entry in vol.list(folder_entry):
                if entry.kind != EntryKind.BANK:
                    continue
                data = vol.read(entry)
                # A bank name off a real image is device metadata, and 41 of
                # 3 147 in this author's own discs carry a "/" or a trailing
                # dot -- `GroovesFilz/Hitz`, `Synth/FX/Misc...`. Used raw as
                # a filename the "/" becomes a directory that was never
                # created and the whole rebuild dies with FileNotFoundError.
                # The gentle form, not safe_filename: this stem is what
                # build_iso turns back into the bank's name on the rebuilt
                # image, so `Synths & Keys` must not become `Synths _ Keys`.
                name = safe_path_component(
                    entry.name.strip(), fallback=f"bank{len(existing_paths)}")
                # Extension doesn't affect iso_builder.build_iso (it's
                # content-agnostic, same as every builder here), but using
                # the entry's own real detected format keeps exported temp
                # files honestly labeled rather than always claiming .e4b
                # for what might be a real EIII bank.
                ext = "e3x" if entry.meta.get("format") == "EIII" else "e4b"
                out = export_dir / f"{name}.{ext}"
                i = 2
                while out.exists():   # EMU3's 16-char names can collide once flattened
                    out = export_dir / f"{name}_{i}.{ext}"
                    i += 1
                out.write_bytes(data)
                existing_paths.append(str(out))
                existing_names.add(name.upper())

        combined = list(existing_paths)
        added = 0
        for p in extra_paths:
            stem = Path(p).stem.upper()
            if stem in existing_names:
                if on_duplicate == "skip":
                    continue
                if on_duplicate == "overwrite":
                    combined = [cp for cp in combined if Path(cp).stem.upper() != stem]
            combined.append(p)
            existing_names.add(stem)
            added += 1

        rebuilt = Path(tmp_path).with_name(Path(tmp_path).name + ".rebuild")
        rebuilt.unlink(missing_ok=True)
        _, log = _run_captured(iso_builder.build_iso, combined, str(rebuilt), "EMU_BANK")
        os.replace(rebuilt, tmp_path)   # same directory as tmp_path -> atomic
        return added, log
    finally:
        shutil.rmtree(export_dir, ignore_errors=True)


def delete_entry(image_path: str, entry: Entry) -> None:
    """Delete one entry from an image (bank or otherwise), safely."""
    def _do(tmp_path: str) -> None:
        # An AKAI volume is not a VFS delete. AkaiVolume is a read-only
        # Volume -- mpc2emu owns the FAT and the root directory, and their
        # delete_akai_volume() frees the directory and file blocks and
        # returns the slot to INACTIVE, which is what the sampler leaves
        # behind. Going through the VFS here is what raised 'AkaiVolume'
        # object has no attribute 'delete' after the user had already
        # confirmed that it could not be undone.
        #
        # Still inside _mutate_in_place: it works on a copy and swaps it in
        # only on success, which is the safety the confirmation promises.
        if entry.meta.get("akai_volume"):
            from . import akai_image as vs_akai_image
            vs_akai_image.delete_volume(tmp_path,
                                        entry.meta.get("volume_name") or entry.name,
                                        partition=entry.meta.get("partition"))
            return
        vol = open_volume(tmp_path)
        if vol is None:
            raise ImageOpError(f"{tmp_path}: not a recognised image")
        try:
            vol.delete(entry)
        finally:
            vol.close()
    _mutate_in_place(image_path, _do)


def rename_entry(image_path: str, entry: Entry, new_name: str) -> None:
    """Rename one entry in an image, safely."""
    def _do(tmp_path: str) -> None:
        vol = open_volume(tmp_path)
        if vol is None:
            raise ImageOpError(f"{tmp_path}: not a recognised image")
        try:
            vol.rename(entry, new_name)
        finally:
            vol.close()
    _mutate_in_place(image_path, _do)


def export_entry(image_path: str, entry: Entry, output_path: str) -> None:
    """Copy one entry's bytes out of an image and onto disk. Read-only on
    the image, so it needs none of the mutate-a-copy machinery above."""
    if Path(output_path).exists():
        raise ImageOpError(f"{output_path} already exists.")
    vol = open_volume(image_path)
    if vol is None:
        raise ImageOpError(f"{image_path}: not a recognised image")
    try:
        data = vol.read(entry)
    finally:
        vol.close()
    Path(output_path).write_bytes(data)
