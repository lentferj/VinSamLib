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

    def list(
        self,
        folder: Optional[Entry] = None,  # noqa: UP045
        size_suffixes: Optional[frozenset] = None,  # noqa: UP045
    ) -> list[Entry]:  # noqa: RUF100, UP045
        """Entries under `folder`, or the volume root.

        `size_suffixes` names the file suffixes whose SIZE and MTIME the
        caller actually uses; everything else is listed with size 0 and no
        mtime, and is never stat'ed. Default None means "stat everything",
        which is what the index scanner wants -- it needs a size for every
        container it indexes.

        WHY IT IS WORTH A PARAMETER. `os.stat` is the whole cost of listing a
        sample folder: one of this library's expansion folders holds 8 912
        entries of which ~30 can become rows, and stat'ing the other 8 880
        took 3.4 seconds on an NFS mount. Measured on that mount, following
        symlinks makes no difference -- `os.lstat` costs the same as
        `os.stat` there, on disjoint halves so neither warmed the other -- so
        the fix is not to stat cheaply but not to stat at all.
        """
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
            suffix = os.path.splitext(child.name)[1].lower()
            try:
                is_dir = child.is_dir()
                # A directory is always stat'ed: there are few of them, and
                # their mtime is what a rescan check reads.
                if is_dir or size_suffixes is None or suffix in size_suffixes:
                    st = child.stat()
                    size = 0 if is_dir else st.st_size
                    mtime = st.st_mtime
                else:
                    size, mtime = 0, None
            except OSError:  # a broken symlink, or it went away mid-listing
                continue
            out.append(
                Entry(
                    name=child.name,
                    kind=_classify(suffix, is_dir),
                    size=size,
                    ref=child.path,
                    meta={"mtime": mtime, "is_image": suffix in _IMAGE_EXTS},
                )
            )
        return out

    def read(self, entry: Entry) -> bytes:
        return Path(entry.ref).read_bytes()
