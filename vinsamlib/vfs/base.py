"""
Common shape for anything VinSamLib can browse: a plain directory, an EMU3
CD/HD image, a FAT12/16/32 image, or an ISO 9660 CD. All of these are
"something that contains named entries, some of which are containers
themselves." A ``Volume`` is the read (and, where supported, write) interface
over one such container; an ``Entry`` is one row inside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional


class EntryKind(Enum):
    DIRECTORY = auto()   # plain filesystem directory
    FOLDER = auto()       # in-image folder (EMU3 root entry, FAT directory)
    BANK = auto()         # a bank file: .E4B, .KRZ, or a whole-bank blob in an image
    OTHER_FILE = auto()   # anything else on disk (WAV, SFZ, unrelated file...)


@dataclass
class Entry:
    """One row as seen through a Volume. ``ref`` is opaque data the owning
    Volume needs to re-locate this entry later (e.g. a start cluster, a byte
    offset) — callers should treat it as a black box."""

    name: str
    kind: EntryKind
    size: int = 0
    ref: Any = None
    meta: dict = field(default_factory=dict)


class Volume:
    """Read-only base. Concrete volumes may add write/delete/rename/append
    and should raise NotImplementedError for verbs they don't support rather
    than silently no-op."""

    path: str

    def list(self, folder: Optional[Entry] = None) -> list[Entry]:
        """List entries at the root (folder=None) or inside a FOLDER entry."""
        raise NotImplementedError

    def read(self, entry: Entry) -> bytes:
        """Return the raw bytes of a BANK or OTHER_FILE entry."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> "Volume":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class WritableVolume(Volume):
    """Mixin-ish base for volumes that support in-place mutation. Every
    method here corresponds to one of the confirmed 'Image operations':
    append, delete/rename, export (export is just `read` + write-to-disk,
    so it needs no dedicated verb)."""

    def append(self, files: list[str], folder: Optional[Entry] = None) -> int:
        raise NotImplementedError

    def delete(self, entry: Entry) -> None:
        raise NotImplementedError

    def rename(self, entry: Entry, new_name: str) -> None:
        raise NotImplementedError
