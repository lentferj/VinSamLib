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

_BANK_EXTS = {".krz", ".k25", ".k26", ".e4b"}
_IMAGE_EXTS = {".iso", ".img", ".hda"}


def _classify(path: Path) -> EntryKind:
    if path.is_dir():
        return EntryKind.DIRECTORY
    suffix = path.suffix.lower()
    if suffix in _BANK_EXTS:
        return EntryKind.BANK
    return EntryKind.OTHER_FILE


class LocalDirVolume(Volume):
    """``folder`` refs are plain absolute path strings; ``None`` means the
    volume's own root directory."""

    def __init__(self, root: str):
        self.path = root
        self._root = Path(root)

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:
        base = Path(folder.ref) if folder is not None else self._root
        out = []
        try:
            children = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return out
        for child in children:
            try:
                st = child.stat()
            except OSError:
                continue
            out.append(Entry(
                name=child.name,
                kind=_classify(child),
                size=0 if child.is_dir() else st.st_size,
                ref=str(child),
                meta={"mtime": st.st_mtime,
                      "is_image": child.suffix.lower() in _IMAGE_EXTS},
            ))
        return out

    def read(self, entry: Entry) -> bytes:
        return Path(entry.ref).read_bytes()
