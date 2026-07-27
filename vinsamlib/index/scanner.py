"""
Background walk of the configured library roots into an IndexDB — no Qt
here, same as vfs/banks/db; the UI wraps `scan()` in a ui.workers.Worker.

Mirrors ui.models's fetch logic (directory -> image -> in-image folder ->
bank -> preset), but writes rows into the index instead of building
TreeNodes, and — unlike the tree, which only parses a bank when a user
actually expands it — parses every bank found, since the whole point of the
index is to make preset names searchable *before* anyone has opened
anything. Skips any container whose (size, mtime) already matches what's
indexed, so a second scan of an unchanged library is fast.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .db import IndexDB
from ..banks import e4b, krz
from ..vfs.base import EntryKind
from ..vfs.detect import open_volume, sniff
from ..vfs.localdir import LocalDirVolume

ProgressCB = Optional[Callable[[str], None]]
_XPM_EXT = ".xpm"


def scan(roots: list[Path], db: IndexDB, progress: ProgressCB = None) -> None:
    seen_paths: set[str] = set()
    for root in roots:
        _scan_directory(root, db, progress, seen_paths)
    # containers whose backing file is gone (moved/deleted since last scan)
    for path in db.all_container_paths():
        if path not in seen_paths and not Path(path).exists():
            db.forget_container(path)


def _scan_directory(path: Path, db: IndexDB, progress: ProgressCB, seen_paths: set) -> None:
    try:
        vol = LocalDirVolume(str(path))
        entries = vol.list()
    except OSError:
        return
    for e in entries:
        if e.kind == EntryKind.DIRECTORY:
            _scan_directory(Path(e.ref), db, progress, seen_paths)
        elif e.kind == EntryKind.BANK:
            _scan_bank_container(e.ref, e.size, db, progress, seen_paths)
        elif e.kind == EntryKind.OTHER_FILE and e.meta.get("is_image"):
            cls = sniff(e.ref)
            if cls is not None:
                _scan_image_container(e.ref, cls, e.size, db, progress, seen_paths)
        elif e.kind == EntryKind.OTHER_FILE and Path(e.ref).suffix.lower() == _XPM_EXT:
            _scan_xpm_container(e.ref, e.size, db, progress, seen_paths)


def _scan_bank_container(path: str, size: int, db: IndexDB, progress: ProgressCB,
                          seen_paths: set) -> None:
    seen_paths.add(path)
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return
    if not db.needs_rescan(path, size, mtime):
        return
    if progress:
        progress(f"Scanning {Path(path).name}…")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as ex:
        cid = db.begin_container(path, "bank", "", size, mtime)
        db.finish_container(cid, error=str(ex))
        return

    fmt, bank = _parse_bank_bytes(data, path)
    cid = db.begin_container(path, "bank", fmt, size, mtime)
    if bank is None:
        db.finish_container(cid, error="not a recognised E4B or KRZ bank")
        return
    _index_bank_presets(db, cid, None, bank, fmt)
    db.finish_container(cid)


def _scan_xpm_container(path: str, size: int, db: IndexDB, progress: ProgressCB,
                         seen_paths: set) -> None:
    # Indexed by filename only, not parsed -- unlike a bank, an XPM's
    # "content" (presets) needs mpc2emu's own xpm_parser plus its
    # referenced WAV/AIFF files to enumerate at all, and doing that for
    # every XPM in a library during a routine background scan would be
    # far too slow. One container == one searchable item == the file
    # itself; the real content only gets parsed on actual import
    # (build/xpm_import.py), triggered explicitly by the user.
    seen_paths.add(path)
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return
    if not db.needs_rescan(path, size, mtime):
        return
    if progress:
        progress(f"Scanning {Path(path).name}…")
    cid = db.begin_container(path, "xpm", "XPM", size, mtime)
    name = Path(path).name
    db.add_item(cid, None, "xpm", name, native_id=name, format="XPM", size=size, ordinal=0)
    db.finish_container(cid)


def _scan_image_container(path: str, volume_cls, size: int, db: IndexDB,
                           progress: ProgressCB, seen_paths: set) -> None:
    seen_paths.add(path)
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return
    if not db.needs_rescan(path, size, mtime):
        return
    if progress:
        progress(f"Scanning {Path(path).name}…")
    cid = db.begin_container(path, "image", volume_cls.__name__, size, mtime)
    try:
        vol = open_volume(path)
        if vol is None:
            db.finish_container(cid, error="failed to open image")
            return
        with vol:
            _scan_vfs_listing(vol, None, db, cid, None)
    except Exception as ex:
        db.finish_container(cid, error=str(ex))
        return
    db.finish_container(cid)


def _scan_vfs_listing(vol, folder_entry, db: IndexDB, container_id: int,
                       parent_item_id: Optional[int]) -> None:
    for ordinal, e in enumerate(vol.list(folder_entry)):
        if e.kind == EntryKind.FOLDER:
            item_id = db.add_item(container_id, parent_item_id, "folder", e.name,
                                   native_id=e.name, ordinal=ordinal)
            _scan_vfs_listing(vol, e, db, container_id, item_id)
        elif e.kind == EntryKind.BANK:
            item_id = db.add_item(container_id, parent_item_id, "bank", e.name,
                                   native_id=e.name, size=e.size, ordinal=ordinal)
            try:
                data = vol.read(e)
            except Exception:
                continue
            fmt, bank = _parse_bank_bytes(data, e.name)
            if bank is not None:
                _index_bank_presets(db, container_id, item_id, bank, fmt)
        # OTHER_FILE (EIII banks, system entries, ...): out of scope, not indexed


def _parse_bank_bytes(data: bytes, label: str):
    if data[:4] == b"FORM" and data[8:12] == b"E4B0":
        try:
            return "E4B", e4b.parse_bytes(data, label)
        except Exception:
            return "E4B", None
    if data[:4] == b"PRAM":
        try:
            return "KRZ", krz.parse_bytes(data, label)
        except Exception:
            return "KRZ", None
    return "", None


def _index_bank_presets(db: IndexDB, container_id: int, parent_item_id: Optional[int],
                         bank, fmt: str) -> None:
    if fmt == "E4B":
        for ordinal, p in enumerate(bank.presets):
            db.add_item(container_id, parent_item_id, "preset", p.name.strip() or "(untitled)",
                        native_id=str(p.index), format="E4B", ordinal=ordinal)
    elif fmt == "KRZ":
        for ordinal, prog in enumerate(bank.programs.values()):
            db.add_item(container_id, parent_item_id, "preset", prog.name.strip() or "(untitled)",
                        native_id=str(prog.id), format="KRZ", ordinal=ordinal)
