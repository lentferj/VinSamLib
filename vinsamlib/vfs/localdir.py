"""
A plain filesystem directory, seen through the same Volume interface as an
image. This is the top of every browsing tree: the explorer walks a
LocalDirVolume until it hits a file that `detect.sniff()` recognises as an
image, then hands off to that image's own Volume.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .base import Entry, EntryKind, Volume

_BANK_EXTS = {".krz", ".k25", ".k26", ".e4b", ".e3x", ".esi", ".e3b"}
_IMAGE_EXTS = {".iso", ".img", ".hda"}


def _classify(suffix: str, is_dir: bool) -> EntryKind:
    if is_dir:
        return EntryKind.DIRECTORY
    if suffix in _BANK_EXTS:
        return EntryKind.BANK
    return EntryKind.OTHER_FILE


class LocalDirVolume(Volume):
    """``folder`` refs are plain absolute path strings; ``None`` means the
    volume's own root directory."""

    def __init__(self, root: str):
        self.path = root

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:
        base = folder.ref if folder is not None else self.path
        out: list[Entry] = []
        try:
            # scandir, not iterdir: a DirEntry answers is_dir() from the
            # directory read itself, so each child costs one stat instead of
            # three. Listing a big tree is the browser's inner loop.
            with os.scandir(base) as it:
                children = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            return out
        for child in children:
            try:
                is_dir = child.is_dir()
                st = child.stat()
            except OSError:      # a broken symlink, or it went away mid-listing
                continue
            suffix = os.path.splitext(child.name)[1].lower()
            out.append(Entry(
                name=child.name,
                kind=_classify(suffix, is_dir),
                size=0 if is_dir else st.st_size,
                ref=child.path,
                meta={"mtime": st.st_mtime, "is_image": suffix in _IMAGE_EXTS},
            ))
        return out

    def read(self, entry: Entry) -> bytes:
        return Path(entry.ref).read_bytes()
