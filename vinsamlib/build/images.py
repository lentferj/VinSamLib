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
from pathlib import Path
from typing import Any, Callable, Optional

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
}

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
APPENDABLE_KINDS = {"emu3_cd", "emu3_hd_emu", "emu3_hd_fat", "k2000_fat16"}


class ImageOpError(RuntimeError):
    """Raised for any failed image operation; message is safe to show the user."""


def _run_captured(fn: Callable, *args, **kwargs) -> tuple[Any, str]:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            result = fn(*args, **kwargs)
    except Exception as ex:
        raise ImageOpError(f"{ex}\n\n{buf.getvalue()}".strip()) from ex
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
                  floppy_kind: str = "1440") -> str:
    """Build a brand-new image at `output_path` containing `bank_paths` (may
    be empty for the appendable kinds, to create a blank starter image).
    Returns the captured build log."""
    if kind not in IMAGE_KINDS:
        raise ImageOpError(f"unknown image kind: {kind}")
    if Path(output_path).exists():
        raise ImageOpError(f"{output_path} already exists — choose a new name.")

    _, default_label, _ = IMAGE_KINDS[kind]
    label = volume_label or IMAGE_KINDS[kind][2]

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
        raise ImageOpError(f"{ex}\n\n{buf.getvalue()}".strip()) from ex
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
    shutil.copy2(src, tmp)
    try:
        result = mutate(str(tmp))
    except ImageOpError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as ex:
        tmp.unlink(missing_ok=True)
        raise ImageOpError(str(ex)) from ex
    os.replace(tmp, src)   # same directory as src -> atomic, incl. on Windows
    return result


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
                name = entry.name.strip() or f"bank{len(existing_paths)}"
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
