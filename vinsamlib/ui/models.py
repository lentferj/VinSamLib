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
from PySide6.QtGui import QColor

from . import dnd, workers
from ..banks import e4b, eiii, krz
from ..build import xpm_import
from ..vfs.base import EntryKind
from ..vfs.detect import open_volume, sniff
from ..vfs.localdir import LocalDirVolume

EXPANDABLE_KINDS = {"directory", "volume_root", "folder", "bank", "mpc_project"}

_KIND_ICON = {
    "directory": "\U0001F4C1",     # 📁
    "volume_root": "\U0001F4BF",   # 💿
    "folder": "\U0001F4C1",        # 📁
    "bank": "\U0001F4E6",          # 📦
    "preset": "\U0001F3B9",        # 🎹
    "xpm": "\U0001F39B",           # 🎛
    "mpc_project": "\U0001F5C2",   # 🗂
    "mpc_program": "\U0001F39B",   # 🎛
    "unsupported": "\U00002753",   # ❓
}

_BANK_EXT_FORMAT = {".e4b": "E4B", ".krz": "KRZ", ".k25": "KRZ", ".k26": "KRZ",
                     ".e3x": "EIII", ".esi": "EIII", ".e3b": "EIII"}
# The MPC's three containers for one and the same keygroup program (see
# build/xpm_import.py, which owns the mapping): the leaf ones hold exactly
# one, a project holds one per track and is browsed like a bank.
MPC_FORMATS = frozenset(xpm_import.MPC_EXT_FORMAT.values())
# One "MPC" entry in the format dropdown covers all three -- three chips for
# what a user thinks of as one kind of file would be noise, and .xty/.xpj are
# far rarer than .xpm.
MPC_FILTER = "MPC"


def format_matches_filter(format_label: str, wanted: Optional[str]) -> bool:
    """Shared by the tree's filter proxy and the search-results filter, so
    both read one definition of what the dropdown's entries mean."""
    if wanted is None:
        return True
    if wanted == MPC_FILTER:
        return format_label in MPC_FORMATS
    return format_label == wanted


def _guess_format(name: str, meta_format: str = "") -> str:
    """Best format label available *before* a bank is actually opened —
    accurate for EMU3 entries (meta already carries a magic-sniffed value),
    a plausible guess from the extension otherwise (corrected once the bank
    node is actually fetched and its own magic bytes are checked)."""
    if meta_format in ("E4B", "EIII"):
        return meta_format
    if meta_format and meta_format != "system":
        return ""   # an unrecognised detected format — not one this app shows as a bank
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
    kind: str                                  # 'directory' | 'volume_root' | 'folder' | 'bank' | 'preset'
                                                # | 'xpm' | 'mpc_project' | 'mpc_program'
    label: str
    parent: Optional["TreeNode"]
    payload: Any                                # Path | (Volume, Entry) | (BankFile, preset_obj)
                                                # | (Path, preset index) for 'mpc_program'
    children: Optional[list["TreeNode"]] = None  # None == not yet fetched
    handle: Any = None                          # opened Volume (volume_root/folder) or parsed BankFile (bank)
    size: int = 0
    format_label: str = ""
    fetching: bool = False
    error: Optional[str] = None
    note: str = ""                              # tooltip/Detail-pane reason for an
                                                # 'unsupported' row, when the generic
                                                # "no reader for this format" is wrong

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
    if node.kind == "mpc_project":
        return _fetch_mpc_project(node)
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
        elif e.kind == EntryKind.OTHER_FILE and Path(e.name).suffix.lower() == xpm_import.PROJECT_EXT:
            # An MPC project holds one keygroup program per track, so it
            # browses like a bank -- expandable into its programs. Its own
            # kind, not "bank": _fetch_bank parses E4B/KRZ/EIII magic bytes
            # and would only fail on it.
            out.append(TreeNode("mpc_project", e.name, node, Path(e.ref), size=e.size,
                                 format_label=xpm_import.MPC_EXT_FORMAT[xpm_import.PROJECT_EXT]))
        elif e.kind == EntryKind.OTHER_FILE and Path(e.name).suffix.lower() in xpm_import.PROGRAM_EXTS:
            # One program per file: importable (see build/xpm_import.py),
            # with nothing to browse into -- a leaf row. But a project's data
            # folder holds one .xpm per track, and only a keygroup program
            # converts; see that module for what the other kinds are and why
            # each is treated the way it is here.
            kind = xpm_import.program_kind(e.ref)
            if kind is None or kind in xpm_import.CONVERTIBLE_KINDS:
                label = xpm_import.MPC_EXT_FORMAT[Path(e.name).suffix.lower()]
                # A drum program reaching here is always MPC 2.x XML -- an
                # MPC 3 one is gzipped and reports kind None -- and 2.x is
                # exactly the case whose pad->key map is missing.
                out.append(TreeNode(
                    "xpm", e.name, node, Path(e.ref), size=e.size,
                    format_label=f"{label} drum kit" if kind == xpm_import.DRUM else label,
                    note=xpm_import.DRUM_2X_PAD_MAP_NOTE if kind == xpm_import.DRUM else ""))
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
        elif e.kind == EntryKind.OTHER_FILE and e.meta.get("format"):
            # Real content VinSamLib has no reader for (e.g. EIII/ESI-32
            # banks living inside an EMU3-filesystem disc alongside real
            # E4B ones -- see vfs/emu3.py's own detected_format). Shown
            # greyed out with its detected format rather than silently
            # dropped, so the folder doesn't look mysteriously empty when
            # it actually holds real (just unsupported) content -- not
            # expandable/importable, there's nothing to read it with yet.
            out.append(TreeNode("unsupported", e.name, parent_node, None, size=e.size,
                                 format_label=e.meta["format"]))
        # Plain OTHER_FILE with no detected format at all (WAVs, docs,
        # ...): still genuinely out of scope, not listed.
    out.sort(key=lambda n: (n.kind != "folder", n.label.lower()))
    return out


def _fetch_mpc_project(node: TreeNode) -> list[TreeNode]:
    """One row per keygroup program in an MPC project, the way a bank node
    lists its presets.

    The parsed mpc2emu Bank is cached on the node exactly as _fetch_bank
    caches a parsed E4B: parsing pulls every referenced WAV into memory
    (tens of MB for a real project), and re-doing that per Detail-pane click
    would make browsing crawl. A project with no keygroup program at all --
    only drum, MIDI or plugin tracks -- raises out of parse_mpc(), and the
    model's own fetch-error path shows that message on the row.

    Programs are NOT draggable into New Bank the way real presets are: an
    MPC program only becomes an E4B/KRZ preset once it has been through a
    conversion, so its row offers Import (the same Convert Options dialog a
    .xpm row opens), not drag-and-drop."""
    path: Path = node.payload
    if node.handle is None:
        node.handle = xpm_import.parse_mpc(str(path))
    return [TreeNode("mpc_program", preset.name.strip() or "(untitled)", node, (path, i))
            for i, preset in enumerate(node.handle.presets)]


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
        elif eiii.detect_format(data) is not None:
            node.handle = eiii.parse_bytes(data, entry.name)
            node.format_label = "EIII"
        else:
            node.error = "not an E4B, KRZ or EIII bank"
            return []

    bank = node.handle
    out: list[TreeNode] = []
    if isinstance(bank, e4b.E4BFile) or isinstance(bank, eiii.EIIIFile):
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
        sorted_roots = sorted(roots, key=lambda p: str(p).lower())
        self._roots: list[TreeNode] = [TreeNode("directory", str(p), None, p) for p in sorted_roots]
        self._live_workers: list[workers.Worker] = []   # keep references alive until done

    # -- growing/shrinking the tree from the outside (File > Add/Remove
    # Library Folder…) -----------------------------------------------------

    def add_root(self, path: Path) -> None:
        # Kept sorted alphabetically by path, not by insertion order --
        # otherwise a folder added later would always show up last
        # regardless of where it belongs alongside the others.
        key = str(path).lower()
        insert_at = 0
        while insert_at < len(self._roots) and str(self._roots[insert_at].payload).lower() < key:
            insert_at += 1
        self.beginInsertRows(QModelIndex(), insert_at, insert_at)
        self._roots.insert(insert_at, TreeNode("directory", str(path), None, path))
        self.endInsertRows()

    def remove_root(self, path: Path) -> bool:
        for i, node in enumerate(self._roots):
            if node.payload == path:
                self.beginRemoveRows(QModelIndex(), i, i)
                del self._roots[i]
                self.endRemoveRows()
                return True
        return False

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
        node.error = workers.last_error_line(message)
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
        if role == Qt.ItemDataRole.ToolTipRole:
            if node.error:
                return node.error
            if node.kind == "unsupported":
                return node.note or ("Real content, but VinSamLib has no reader "
                                      f"for this format ({node.format_label}) yet.")
        if role == Qt.ItemDataRole.ForegroundRole and node.kind == "unsupported":
            return QColor(Qt.GlobalColor.gray)
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
    All/E4B/KRZ/EIII/XPM filter dropdown, without teaching the lazy tree model
    itself anything about filtering. Confirmed empirically (this project's
    running rule for anything PySide6-specific) that QSortFilterProxyModel
    correctly forwards canFetchMore()/fetchMore() *and* flags()/mimeData()
    to a custom lazy source model under PySide6/Qt6 -- both are needed
    here, since the tree only grows on demand and presets still need to
    stay draggable through the proxy.

    Only "bank", "xpm" and "mpc_project" nodes are ever actually filtered
    out. A directory/image/folder that turns out to contain zero matching
    rows is still shown (just ends up empty once expanded) rather than
    hidden pre-emptively -- knowing in advance which containers have
    matching content would mean scanning everything up front, which is
    exactly what this tree's lazy design exists to avoid. A project's own
    program rows are likewise never filtered: reaching one means its
    project already passed."""

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
        if node is None or node.kind not in ("bank", "xpm", "mpc_project"):
            return True
        return format_matches_filter(node.format_label, self._format_filter)
