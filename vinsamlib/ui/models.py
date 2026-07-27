"""
The library tree: a single lazy QAbstractItemModel spanning three different
data sources (plain filesystem, vfs.Volume.list(), banks.*.parse()) so a node
expands directory -> image -> in-image folder -> bank -> preset/program, and
stops there (see the M3 plan: sample-level content is the Detail pane / the
Samples pane's job, never further tree rows).

Nothing here runs parsing/IO on the GUI thread: every fetch goes through
ui.workers.Worker on the shared thread pool, and results come back via a
queued Qt signal connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QAbstractItemModel, QMimeData, QModelIndex, QSortFilterProxyModel, Qt

from . import dnd, workers
from ..banks import e4b, krz
from ..vfs.base import EntryKind
from ..vfs.detect import open_volume, sniff
from ..vfs.localdir import LocalDirVolume

EXPANDABLE_KINDS = {"directory", "volume_root", "folder", "bank"}

_KIND_ICON = {
    "directory": "\U0001F4C1",     # 📁
    "volume_root": "\U0001F4BF",   # 💿
    "folder": "\U0001F4C1",        # 📁
    "bank": "\U0001F4E6",          # 📦
    "preset": "\U0001F3B9",        # 🎹
    "xpm": "\U0001F39B",           # 🎛
}

_BANK_EXT_FORMAT = {".e4b": "E4B", ".krz": "KRZ", ".k25": "KRZ", ".k26": "KRZ"}
_XPM_EXT = ".xpm"


def _guess_format(name: str, meta_format: str = "") -> str:
    """Best format label available *before* a bank is actually opened —
    accurate for EMU3 entries (meta already carries a magic-sniffed value),
    a plausible guess from the extension otherwise (corrected once the bank
    node is actually fetched and its own magic bytes are checked)."""
    if meta_format == "E4B":
        return "E4B"
    if meta_format and meta_format != "system":
        return ""   # e.g. "EIII (unsupported)" — not a format this app shows as a bank
    return _BANK_EXT_FORMAT.get(Path(name).suffix.lower(), "")


def human_size(n: int) -> str:
    if n <= 0:
        return ""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@dataclass
class TreeNode:
    kind: str                                  # 'directory' | 'volume_root' | 'folder' | 'bank' | 'preset' | 'xpm'
    label: str
    parent: Optional["TreeNode"]
    payload: Any                                # Path | (Volume, Entry) | (BankFile, preset_obj)
    children: Optional[list["TreeNode"]] = None  # None == not yet fetched
    handle: Any = None                          # opened Volume (volume_root/folder) or parsed BankFile (bank)
    size: int = 0
    format_label: str = ""
    fetching: bool = False
    error: Optional[str] = None

    def display_text(self) -> str:
        icon = _KIND_ICON.get(self.kind, "")
        bits = [icon, self.label] if icon else [self.label]
        text = " ".join(bits)
        if self.format_label:
            text += f"  [{self.format_label}]"
        if self.size:
            text += f"   {human_size(self.size)}"
        if self.error:
            text += "   (failed to open)"
        return text


# ── background fetch functions (run on a worker thread — no Qt here) ───────

def _fetch_children(node: TreeNode) -> list[TreeNode]:
    if node.kind == "directory":
        return _fetch_directory(node)
    if node.kind == "volume_root":
        return _fetch_volume_root(node)
    if node.kind == "folder":
        return _fetch_folder(node)
    if node.kind == "bank":
        return _fetch_bank(node)
    return []


def _fetch_directory(node: TreeNode) -> list[TreeNode]:
    path: Path = node.payload
    vol = LocalDirVolume(str(path))
    out: list[TreeNode] = []
    for e in vol.list():
        if e.kind == EntryKind.DIRECTORY:
            out.append(TreeNode("directory", e.name, node, Path(e.ref)))
        elif e.kind == EntryKind.BANK:
            out.append(TreeNode("bank", e.name, node, (vol, e), size=e.size,
                                 format_label=_guess_format(e.name)))
        elif e.kind == EntryKind.OTHER_FILE and e.meta.get("is_image"):
            if sniff(e.ref) is not None:
                out.append(TreeNode("volume_root", e.name, node, Path(e.ref), size=e.size))
        elif e.kind == EntryKind.OTHER_FILE and Path(e.name).suffix.lower() == _XPM_EXT:
            # Importable (see build/xpm_import.py), not browsable further --
            # a leaf row, not "bank" (which would send it through
            # _fetch_bank's E4B/KRZ magic-byte parsing and fail).
            out.append(TreeNode("xpm", e.name, node, Path(e.ref), size=e.size, format_label="XPM"))
        # plain OTHER_FILE (WAVs, docs, ...): out of scope for this browser
    out.sort(key=lambda n: (n.kind not in ("directory", "volume_root"), n.label.lower()))
    return out


def _fetch_volume_root(node: TreeNode) -> list[TreeNode]:
    path: Path = node.payload
    if node.handle is None:
        vol = open_volume(str(path))
        if vol is None:
            node.error = "not a recognised image"
            return []
        node.handle = vol
    return _fetch_vfs_listing(node.handle, None, node)


def _fetch_folder(node: TreeNode) -> list[TreeNode]:
    vol, entry = node.payload
    return _fetch_vfs_listing(vol, entry, node)


def _fetch_vfs_listing(vol, folder_entry, parent_node: TreeNode) -> list[TreeNode]:
    out: list[TreeNode] = []
    for e in vol.list(folder_entry):
        if e.kind == EntryKind.FOLDER:
            out.append(TreeNode("folder", e.name, parent_node, (vol, e)))
        elif e.kind == EntryKind.BANK:
            out.append(TreeNode("bank", e.name, parent_node, (vol, e), size=e.size,
                                 format_label=_guess_format(e.name, e.meta.get("format", ""))))
        # OTHER_FILE (EIII banks, ROM/system entries, ...): out of scope — see
        # the plan's E4B-only decision; not listed at all, not just unopenable.
    out.sort(key=lambda n: (n.kind != "folder", n.label.lower()))
    return out


def _fetch_bank(node: TreeNode) -> list[TreeNode]:
    vol, entry = node.payload
    if node.handle is None:
        data = vol.read(entry)
        if data[:4] == b"FORM" and data[8:12] == b"E4B0":
            node.handle = e4b.parse_bytes(data, entry.name)
            node.format_label = "E4B"
        elif data[:4] == b"PRAM":
            node.handle = krz.parse_bytes(data, entry.name)
            node.format_label = "KRZ"
        else:
            node.error = "not an E4B or KRZ bank"
            return []

    bank = node.handle
    out: list[TreeNode] = []
    if isinstance(bank, e4b.E4BFile):
        for p in bank.presets:
            out.append(TreeNode("preset", p.name.strip() or "(untitled)", node, (bank, p)))
    else:
        for prog in bank.programs.values():
            out.append(TreeNode("preset", prog.name.strip() or "(untitled)", node, (bank, prog)))
    return out   # preset order preserved — it reflects the bank's own numbering


# ── the Qt model ─────────────────────────────────────────────────────────────

class LibraryTreeModel(QAbstractItemModel):
    def __init__(self, roots: list[Path], parent=None):
        super().__init__(parent)
        self._roots: list[TreeNode] = [TreeNode("directory", str(p), None, p) for p in roots]
        self._live_workers: list[workers.Worker] = []   # keep references alive until done

    # -- growing the tree from the outside (File > Add Library Folder…) -----

    def add_root(self, path: Path) -> None:
        self.beginInsertRows(QModelIndex(), len(self._roots), len(self._roots))
        self._roots.append(TreeNode("directory", str(path), None, path))
        self.endInsertRows()

    def is_empty(self) -> bool:
        return not self._roots

    # -- QAbstractItemModel plumbing -----------------------------------------

    def _node_for(self, index: QModelIndex) -> Optional[TreeNode]:
        if not index.isValid():
            return None
        return index.internalPointer()

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        node = self._node_for(parent)
        siblings = self._roots if node is None else (node.children or [])
        if row >= len(siblings):
            return QModelIndex()
        return self.createIndex(row, column, siblings[row])

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        node: TreeNode = index.internalPointer()
        if node.parent is None:
            return QModelIndex()
        grandparent = node.parent.parent
        siblings = self._roots if grandparent is None else grandparent.children
        row = siblings.index(node.parent)
        return self.createIndex(row, 0, node.parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        node = self._node_for(parent)
        if node is None:
            return len(self._roots)
        return len(node.children) if node.children is not None else 0

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        node = self._node_for(parent)
        if node is None:
            return bool(self._roots)
        if node.kind not in EXPANDABLE_KINDS:
            return False
        if node.children is None:
            return True   # not fetched yet — show the expand arrow optimistically
        return len(node.children) > 0

    def canFetchMore(self, parent: QModelIndex) -> bool:
        node = self._node_for(parent)
        if node is None or node.kind not in EXPANDABLE_KINDS:
            return False
        return node.children is None and not node.fetching

    def fetchMore(self, parent: QModelIndex) -> None:
        node = self._node_for(parent)
        if node is None or node.fetching:
            return
        node.fetching = True
        worker = workers.Worker(_fetch_children, node)
        worker.signals.finished.connect(
            lambda children, n=node, idx=QModelIndex(parent): self._on_fetched(n, children, idx))
        worker.signals.error.connect(lambda msg, n=node: self._on_fetch_error(n, msg))
        worker.signals.finished.connect(lambda *_: self._live_workers.remove(worker))
        worker.signals.error.connect(lambda *_: self._live_workers.remove(worker))
        self._live_workers.append(worker)
        workers.run(worker)

    def _on_fetched(self, node: TreeNode, children: list[TreeNode], parent_index: QModelIndex) -> None:
        node.fetching = False
        if not children:
            node.children = []
            return
        self.beginInsertRows(parent_index, 0, len(children) - 1)
        node.children = children
        self.endInsertRows()

    def _on_fetch_error(self, node: TreeNode, message: str) -> None:
        node.fetching = False
        node.error = message.strip().splitlines()[-1] if message else "error"
        node.children = []
        idx = self.node_index(node)
        if idx.isValid():
            self.dataChanged.emit(idx, idx)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        node = self._node_for(index)
        if node is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return node.display_text()
        if role == Qt.ItemDataRole.UserRole:
            return node
        if role == Qt.ItemDataRole.ToolTipRole and node.error:
            return node.error
        return None

    # -- drag source (M5: presets drag into the New Bank column) ------------

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        node = self._node_for(index)
        if node is not None and node.kind == "preset":
            return base | Qt.ItemFlag.ItemIsDragEnabled
        return base

    def mimeTypes(self) -> list[str]:
        return [dnd.DRAG_MIME_TYPE]

    def mimeData(self, indexes: list[QModelIndex]) -> Optional[QMimeData]:
        seen: set[int] = set()
        items = []
        for idx in indexes:
            node = self._node_for(idx)
            if node is None or node.kind != "preset" or id(node) in seen:
                continue
            seen.add(id(node))
            bank, preset_obj = node.payload
            fmt = node.parent.format_label if node.parent else ""
            items.append((bank, preset_obj, fmt, node.label))
        return dnd.build_mime_data(items) if items else None

    # -- helpers used by the panes --------------------------------------------

    def node_index(self, node: TreeNode) -> QModelIndex:
        """Find the QModelIndex for a node we already have a reference to
        (used to refresh a row after an async error)."""
        parent_children = self._roots if node.parent is None else (node.parent.children or [])
        try:
            row = parent_children.index(node)
        except ValueError:
            return QModelIndex()
        parent_index = QModelIndex() if node.parent is None else self.node_index(node.parent)
        return self.index(row, 0, parent_index)


class BankFormatFilterProxy(QSortFilterProxyModel):
    """Sits between LibraryTreeModel and the tree view to implement the
    All/E4B/KRZ/XPM filter dropdown, without teaching the lazy tree model
    itself anything about filtering. Confirmed empirically (this project's
    running rule for anything PySide6-specific) that QSortFilterProxyModel
    correctly forwards canFetchMore()/fetchMore() *and* flags()/mimeData()
    to a custom lazy source model under PySide6/Qt6 -- both are needed
    here, since the tree only grows on demand and presets still need to
    stay draggable through the proxy.

    Only "bank" and "xpm" nodes are ever actually filtered out. A
    directory/image/folder that turns out to contain zero matching rows
    is still shown (just ends up empty once expanded) rather than hidden
    pre-emptively -- knowing in advance which containers have matching
    content would mean scanning everything up front, which is exactly
    what this tree's lazy design exists to avoid."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._format_filter: Optional[str] = None

    def set_format_filter(self, fmt: Optional[str]) -> None:
        self._format_filter = fmt
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if self._format_filter is None:
            return True
        source_model = self.sourceModel()
        index = source_model.index(source_row, 0, source_parent)
        node = index.data(Qt.ItemDataRole.UserRole)
        if node is None or node.kind not in ("bank", "xpm"):
            return True
        return node.format_label == self._format_filter
