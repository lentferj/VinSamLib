"""
ExplorerPane: a search box over either the lazy library tree (empty search
box) or a flat, index-backed result list (non-empty box) — the M4 upgrade
promised in the M3 plan, where the tree's own filtering was deliberately
left out because it could only ever match what was already expanded.
Both views sit over the same DetailPane, and both funnel selection through
one path so the rest of the app doesn't need to know which one is active.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem, QMenu,
                             QSplitter, QStackedWidget, QTreeView, QVBoxLayout,
                             QWidget)

from . import dnd, search_resolve
from .detail_pane import DetailPane
from .models import BankFormatFilterProxy, LibraryTreeModel, TreeNode
from ..index.db import IndexDB, SearchResult

_FORMAT_FILTERS = ["All", "E4B", "KRZ", "XPM"]


class _ResultsListWidget(QListWidget):
    """QListWidget's own default drag payload only carries enough to
    reorder items within itself -- to make a multi-selected drag out of
    the search results produce the same MIME payload the tree's own
    preset drag does (so it drops onto New Bank the same way), each
    selected hit has to be resolved back into a live TreeNode first."""

    def mimeData(self, items):
        payload_items = []
        for widget_item in items:
            hit = widget_item.data(Qt.ItemDataRole.UserRole)
            if hit is None:
                continue
            node = search_resolve.resolve_result(hit)
            if node is None or node.kind != "preset":
                continue
            bank, preset_obj = node.payload
            fmt = node.parent.format_label if node.parent else ""
            payload_items.append((bank, preset_obj, fmt, node.label))
        if not payload_items:
            return None
        return dnd.build_mime_data(payload_items)

_KIND_ICON = {"folder": "\U0001F4C1", "bank": "\U0001F4E6", "preset": "\U0001F3B9",
              "xpm": "\U0001F39B"}
_SEARCH_DEBOUNCE_MS = 200


class ExplorerPane(QWidget):
    selectionChanged = Signal(object)   # TreeNode | None
    addToBankRequested = Signal(list)   # list[TreeNode] (always kind == "preset")
    importXpmRequested = Signal(str)    # absolute path to a .xpm file
    convertPresetRequested = Signal(list)   # list[TreeNode], one or more "preset" nodes
    removeLibraryRootRequested = Signal(object)   # Path of a root "directory" node

    def __init__(self, model: LibraryTreeModel, index_db: Optional[IndexDB] = None, parent=None):
        super().__init__(parent)
        self._index_db = index_db
        self._current_node: Optional[TreeNode] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        search_row = QHBoxLayout()
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Search library…")
        self._search_box.setClearButtonEnabled(True)
        self._search_box.setContentsMargins(0, 0, 0, 0)
        self._search_box.textChanged.connect(self._on_search_text_changed)
        search_row.addWidget(self._search_box, 1)

        self._filter_box = QComboBox()
        self._filter_box.addItems(_FORMAT_FILTERS)
        self._filter_box.setToolTip("Only show banks (or importable XPM programs) of this format")
        self._filter_box.currentTextChanged.connect(self._on_filter_changed)
        search_row.addWidget(self._filter_box)
        layout.addLayout(search_row)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._run_search)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self._stack = QStackedWidget()

        self._tree_proxy = BankFormatFilterProxy(self)
        self._tree_proxy.setSourceModel(model)
        self._tree = QTreeView()
        self._tree.setModel(self._tree_proxy)
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree.setDragEnabled(True)
        self._tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self._tree.selectionModel().currentChanged.connect(self._on_tree_current_changed)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._tree.doubleClicked.connect(self._on_tree_double_clicked)
        self._stack.addWidget(self._tree)

        self._results = _ResultsListWidget()
        self._results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._results.setDragEnabled(True)
        self._results.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self._results.currentItemChanged.connect(self._on_result_current_changed)
        self._results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._results.customContextMenuRequested.connect(self._on_results_context_menu)
        self._results.itemDoubleClicked.connect(self._on_result_double_clicked)
        self._stack.addWidget(self._results)

        self._stack.setCurrentWidget(self._tree)
        splitter.addWidget(self._stack)

        self._detail = DetailPane()
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout.addWidget(splitter)

    def set_index_db(self, index_db: Optional[IndexDB]) -> None:
        self._index_db = index_db
        if self._search_box.text().strip():
            self._run_search()

    # -- format filter ----------------------------------------------------------

    def _current_format_filter(self) -> Optional[str]:
        text = self._filter_box.currentText()
        return None if text == "All" else text

    def _on_filter_changed(self, _text: str) -> None:
        self._tree_proxy.set_format_filter(self._current_format_filter())
        if self._search_box.text().strip():
            self._run_search()

    # -- search ---------------------------------------------------------------

    def _on_search_text_changed(self, _text: str) -> None:
        self._search_timer.start(_SEARCH_DEBOUNCE_MS)

    def _run_search(self) -> None:
        text = self._search_box.text().strip()
        if not text:
            self._stack.setCurrentWidget(self._tree)
            idx = self._tree.currentIndex()
            node = idx.data(Qt.ItemDataRole.UserRole) if idx.isValid() else None
            self._select(node)
            return

        self._stack.setCurrentWidget(self._results)
        self._results.clear()
        if self._index_db is None:
            placeholder = QListWidgetItem("Index isn't ready yet — still scanning the library.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self._results.addItem(placeholder)
            return

        hits = self._index_db.search(text)
        format_filter = self._current_format_filter()
        if format_filter is not None:
            # Non-bank hits (folders, presets/programs) carry the format of
            # the bank they belong to (see index/scanner.py), so filtering
            # by it here also correctly restricts preset/program results to
            # the selected bank format, not just bare bank hits themselves.
            hits = [h for h in hits if h.format == format_filter]
        if not hits:
            placeholder = QListWidgetItem("No matches.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self._results.addItem(placeholder)
            return
        for hit in hits:
            item = QListWidgetItem(_format_hit(hit))
            item.setData(Qt.ItemDataRole.UserRole, hit)
            self._results.addItem(item)

    # -- selection plumbing -----------------------------------------------------

    def _on_tree_current_changed(self, current, _previous) -> None:
        if self._stack.currentWidget() is not self._tree:
            return
        node = current.data(Qt.ItemDataRole.UserRole) if current.isValid() else None
        self._select(node)

    def _on_result_current_changed(self, current, _previous) -> None:
        if current is None:
            self._select(None)
            return
        hit: Optional[SearchResult] = current.data(Qt.ItemDataRole.UserRole)
        if hit is None:
            self._select(None)
            return
        node = search_resolve.resolve_result(hit)
        self._select(node)

    def _on_tree_double_clicked(self, index) -> None:
        # Presets and xpm rows are both leaves, so Qt's default expand/
        # collapse-on-double-click is a no-op for them anyway -- safe to
        # also treat the double-click as each one's primary action (add /
        # import, matching the right-click menu) without fighting the
        # tree's own toggle behavior on folder/bank rows.
        node = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
        self._trigger_primary_action(node)

    def _on_result_double_clicked(self, item: QListWidgetItem) -> None:
        hit = item.data(Qt.ItemDataRole.UserRole)
        if hit is None:
            return
        node = search_resolve.resolve_result(hit)
        self._trigger_primary_action(node)

    def _trigger_primary_action(self, node: Optional[TreeNode]) -> None:
        if node is None:
            return
        if node.kind == "preset":
            self.addToBankRequested.emit([node])
        elif node.kind == "xpm":
            self.importXpmRequested.emit(str(node.payload))

    def _select(self, node: Optional[TreeNode]) -> None:
        self._current_node = node
        self._detail.show_node(node)
        self.selectionChanged.emit(node)

    # -- context menus ------------------------------------------------------------

    def _on_tree_context_menu(self, pos) -> None:
        index = self._tree.indexAt(pos)
        if not index.isValid():
            return
        # Right-clicking a row that's already part of a multi-selection acts
        # on the whole selection (standard behavior); right-clicking outside
        # the current selection acts on just that one row instead.
        selected = self._tree.selectionModel().selectedIndexes()
        if index not in selected:
            selected = [index]
        nodes = [i.data(Qt.ItemDataRole.UserRole) for i in selected]
        self._show_context_menu(nodes, self._tree.viewport().mapToGlobal(pos))

    def _on_results_context_menu(self, pos) -> None:
        item = self._results.itemAt(pos)
        if item is None:
            return
        selected = self._results.selectedItems()
        if item not in selected:
            selected = [item]
        hits = [i.data(Qt.ItemDataRole.UserRole) for i in selected]
        nodes = [search_resolve.resolve_result(hit) for hit in hits if hit is not None]
        self._show_context_menu(nodes, self._results.viewport().mapToGlobal(pos))

    def _show_context_menu(self, nodes: list[Optional[TreeNode]], global_pos) -> None:
        presets = [n for n in nodes if n is not None and n.kind == "preset"]
        xpms = [n for n in nodes if n is not None and n.kind == "xpm"]
        # A library root is a top-level "directory" node (no parent) --
        # only those are individually tracked in Config.library_roots and
        # thus removable; a plain subdirectory isn't its own library entry.
        roots = [n for n in nodes if n is not None and n.kind == "directory" and n.parent is None]
        if not presets and not xpms and not roots:
            return
        menu = QMenu(self)
        add_action = None
        convert_action = None
        import_action = None
        remove_action = None
        if presets:
            label = f'Add "{presets[0].label}" to New Bank' if len(presets) == 1 \
                else f"Add {len(presets)} presets to New Bank"
            add_action = menu.addAction(label)
        # Excludes any preset node with no resolvable parent bank -- same
        # guard as before, just applied per-node instead of only to a lone
        # selection, since a multi-select can now use this action too.
        convertible = [p for p in presets if p.parent is not None]
        if convertible:
            # One shared Convert Options dialog covers the whole
            # selection -- same options applied to every preset, not one
            # dialog per preset. Both E4B and KRZ presets get this now --
            # mpc2emu's parsers.krz_parser (added 2026-07-27) made KRZ a
            # real *input* format too, so a KRZ preset can be converted
            # the same way an E4B one can, to either target format.
            label = "Import via mpc2emu…" if len(convertible) == 1 \
                else f"Import {len(convertible)} presets via mpc2emu…"
            convert_action = menu.addAction(label)
        if len(xpms) == 1:
            # Multi-XPM import isn't supported yet -- only offered for a
            # single selected .xpm row.
            import_action = menu.addAction(f'Import "{xpms[0].label}"…')
        if len(roots) == 1:
            # Multi-root removal isn't offered either -- same reasoning,
            # keep the one-item-at-a-time pattern consistent.
            remove_action = menu.addAction(f'Remove "{roots[0].label}" from Library…')
        chosen = menu.exec(global_pos)
        if add_action is not None and chosen == add_action:
            self.addToBankRequested.emit(presets)
        elif convert_action is not None and chosen == convert_action:
            self.convertPresetRequested.emit(convertible)
        elif import_action is not None and chosen == import_action:
            self.importXpmRequested.emit(str(xpms[0].payload))
        elif remove_action is not None and chosen == remove_action:
            self.removeLibraryRootRequested.emit(roots[0].payload)


def _format_hit(hit: SearchResult) -> str:
    icon = _KIND_ICON.get(hit.kind, "")
    bits = [icon, hit.name] if icon else [hit.name]
    label = " ".join(bits)
    if hit.format:
        label += f"  [{hit.format}]"
    from pathlib import Path
    container_name = Path(hit.container_path).name
    ancestry = " ▸ ".join(c.name for c in hit.chain[:-1])
    where = f"{container_name}" + (f" ▸ {ancestry}" if ancestry else "")
    return f"{label}   —   {where}"
