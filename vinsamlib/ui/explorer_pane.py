"""
ExplorerPane: a search box over either the lazy library tree (empty search
box) or a flat, index-backed result list (non-empty box) — the M4 upgrade
promised in the M3 plan, where the tree's own filtering was deliberately
left out because it could only ever match what was already expanded.
Both views sit over the same DetailPane, and both funnel selection through
one path so the rest of the app doesn't need to know which one is active.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QModelIndex, QTimer, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem, QMenu,
                             QSplitter, QStackedWidget, QTreeView, QVBoxLayout,
                             QWidget)

from . import dnd, search_resolve
from .detail_pane import DetailPane
from .models import (MPC_FILTER, BankFormatFilterProxy, LibraryTreeModel, TreeNode,
                     _FOREIGN_KINDS, _IMPORT_DRAG_KINDS, _import_request,
                     format_matches_filter)
from . import models
from ..build import foreign_import
from ..index.db import IndexDB, SearchResult

def _format_filters() -> list[str]:
    """The dropdown's entries, decided when the pane is built rather than at
    import time -- the soundfont-style formats are only listed when mpc2emu
    can actually import them, and offering a filter for rows that cannot
    exist would be a dead end.

    Five separate entries rather than one grouped chip: unlike the MPC's
    three containers for a single keygroup program, these are five unrelated
    ecosystems, and someone hunting a SoundFont is not hunting an EXS24.

    AKAI is unconditional: its reader is this project's own, so those rows
    exist with or without mpc2emu -- only CONVERTING one needs it.
    """
    entries = ["All", "E4B", "KRZ", "EIII", "AKAI", MPC_FILTER]
    if foreign_import.available():
        entries.extend(foreign_import.FORMAT_FILTERS)
    return entries


class _ResultsListWidget(QListWidget):
    """QListWidget's own default drag payload only carries enough to
    reorder items within itself -- to make a multi-selected drag out of
    the search results produce the same MIME payload the tree's own
    preset drag does (so it drops onto New Bank the same way), each
    selected hit has to be resolved back into a live TreeNode first."""

    def mimeData(self, items):
        payload_items = []
        requests = []
        for widget_item in items:
            hit = widget_item.data(Qt.ItemDataRole.UserRole)
            if hit is None:
                continue
            node = search_resolve.resolve_result(hit)
            if node is None:
                continue
            if node.kind == "preset":
                bank, preset_obj = node.payload
                fmt = node.parent.format_label if node.parent else ""
                payload_items.append((bank, preset_obj, fmt, node.label))
            elif node.kind in _IMPORT_DRAG_KINDS and not node.empty_reason:
                requests.append(_import_request(node))
        if payload_items and requests:
            # Same refusal the tree makes, for the same reason -- see
            # LibraryTreeModel.mimeData().
            return None
        if requests:
            return dnd.build_import_mime_data(requests)
        if not payload_items:
            return None
        return dnd.build_mime_data(payload_items)

_KIND_ICON = {"folder": "\U0001F4C1", "bank": "\U0001F4E6", "preset": "\U0001F3B9",
              "xpm": "\U0001F39B", "mpc_project": "\U0001F5C2",
              "mpc_program": "\U0001F39B", "foreign_bank": "\U0001F4DA",
              "foreign_preset": "\U0001F3BC"}
_SEARCH_DEBOUNCE_MS = 200


class ExplorerPane(QWidget):
    selectionChanged = Signal(object)   # TreeNode | None
    addToBankRequested = Signal(list)   # list[TreeNode] (always kind == "preset")
    addFavouritesRequested = Signal(object)   # a single TreeNode, kind == "bank"
    # (absolute path to an MPC .xpm/.xty/.xpj file, program index or None).
    # None means "everything the file holds" -- one program for a .xpm or
    # .xty, every keygroup track for a project.
    importXpmRequested = Signal(str, object)
    convertPresetRequested = Signal(list)   # list[TreeNode], one or more "preset" nodes
    # Import-request dicts for soundfont-style sources (see ui/dnd.py) --
    # the same payload a drag onto New Bank carries, so both routes land in
    # one handler.
    importForeignRequested = Signal(list)
    removeLibraryRootRequested = Signal(object)   # Path of a root "directory" node

    def __init__(self, model: LibraryTreeModel, index_db: Optional[IndexDB] = None, parent=None):
        super().__init__(parent)
        self._index_db = index_db
        self._current_node: Optional[TreeNode] = None
        #: What format New Bank is locked to right now, or None while it is
        #: empty. Set by MainWindow. A CALLABLE rather than a stored value
        #: because the lock changes as the user fills and clears New Bank,
        #: and a menu built from a stale copy would offer exactly the action
        #: that is about to be refused.
        self.locked_format: Callable[[], Optional[str]] = lambda: None

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
        self._filter_box.addItems(_format_filters())
        self._filter_box.setToolTip(
            "Only show banks of this format (MPC covers .xpm programs, "
            ".xty tracks and .xpj projects)")
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
            hits = [h for h in hits if format_matches_filter(h.format, format_filter)]
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
        # Presets, xpm rows and a project's program rows are all leaves, so
        # Qt's default expand/collapse-on-double-click is a no-op for them
        # anyway -- safe to also treat the double-click as each one's
        # primary action (add / import, matching the right-click menu)
        # without fighting the tree's own toggle behavior on folder/bank/
        # project rows, which keep expanding as they should.
        node = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
        self._trigger_primary_action(node)

    def _on_result_double_clicked(self, item: QListWidgetItem) -> None:
        hit = item.data(Qt.ItemDataRole.UserRole)
        if hit is None:
            return
        node = search_resolve.resolve_result(hit)
        # allow_container: in the tree a double-click on a project expands it
        # (Qt's own behaviour, which importing on top of would hijack), but a
        # search result has nothing to expand -- there, importing the whole
        # project is the only sensible primary action.
        self._trigger_primary_action(node, allow_container=True)

    def _trigger_primary_action(self, node: Optional[TreeNode],
                                 allow_container: bool = False) -> None:
        if node is None:
            return
        if node.kind == "mpc_project":
            if allow_container:
                self.importXpmRequested.emit(str(node.payload), None)
            return
        if node.kind == "foreign_bank":
            # Same rule as an MPC project: in the tree a double-click expands
            # it, which importing on top of would hijack.
            if allow_container and not node.empty_reason:
                self.importForeignRequested.emit([_import_request(node)])
            return
        if node.kind == "preset":
            self.addToBankRequested.emit([node])
        elif node.kind == "xpm":
            self.importXpmRequested.emit(str(node.payload), None)
        elif node.kind == "mpc_program":
            path, preset_index = node.payload
            self.importXpmRequested.emit(str(path), preset_index)
        elif node.kind == "foreign_preset" and not node.empty_reason:
            self.importForeignRequested.emit([_import_request(node)])

    def _select(self, node: Optional[TreeNode]) -> None:
        self._current_node = node
        self._detail.show_node(node)
        self.selectionChanged.emit(node)

    # -- context menus ------------------------------------------------------------

    def view_state(self) -> dict:
        """Which rows are unfolded, and which one is current.

        Paths rather than model indices: an index means nothing once the tree
        has been rebuilt, and a lazy tree rebuilds from nothing every start.
        """
        expanded = []
        model = self._tree.model()

        def walk(parent):
            for row in range(model.rowCount(parent)):
                idx = model.index(row, 0, parent)
                if not self._tree.isExpanded(idx):
                    continue
                node = idx.data(Qt.ItemDataRole.UserRole)
                path = models._container_path_of(node) if node is not None else ""
                if path:
                    expanded.append(path)
                walk(idx)

        walk(QModelIndex())
        cur = self._tree.currentIndex()
        node = cur.data(Qt.ItemDataRole.UserRole) if cur.isValid() else None
        return {"expanded": expanded,
                "current": (models._container_path_of(node)
                            if node is not None else "")}

    def restore_view_state(self, state: dict) -> None:
        """Unfold what was unfolded, as the tree fills in.

        A lazy tree cannot be restored in one pass: expanding a row starts a
        FETCH, and its children do not exist until that returns. So the wanted
        paths are held and applied again every time rows arrive -- the tree
        unfolds itself level by level, and a path that never appears (its
        folder is gone) is simply never reached, which needs no error of its
        own because the row is not there to explain.
        """
        self._wanted_expanded = set(state.get("expanded") or ())
        self._wanted_current = state.get("current") or ""
        # Connected once and left connected. Disconnecting first "in case"
        # emits a libpyside RuntimeWarning when there was nothing to
        # disconnect, and a try/except does not suppress a warning -- so the
        # state is tracked instead of being probed for.
        if not getattr(self, "_expansion_hooked", False):
            model = self._tree.model()
            if hasattr(model, "rowsInserted"):
                model.rowsInserted.connect(self._apply_wanted_expansion)
                self._expansion_hooked = True
        self._apply_wanted_expansion()

    def _apply_wanted_expansion(self, *_args) -> None:
        if not getattr(self, "_wanted_expanded", None) and not getattr(
                self, "_wanted_current", ""):
            return
        model = self._tree.model()

        def walk(parent):
            for row in range(model.rowCount(parent)):
                idx = model.index(row, 0, parent)
                node = idx.data(Qt.ItemDataRole.UserRole)
                path = models._container_path_of(node) if node is not None else ""
                if path and path in getattr(self, "_wanted_expanded", ()):
                    if not self._tree.isExpanded(idx):
                        self._tree.expand(idx)
                if path and path == getattr(self, "_wanted_current", ""):
                    self._tree.setCurrentIndex(idx)
                    self._wanted_current = ""
                if self._tree.isExpanded(idx):
                    walk(idx)

        walk(QModelIndex())

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
        programs = [n for n in nodes if n is not None and n.kind == "mpc_program"]
        projects = [n for n in nodes if n is not None and n.kind == "mpc_project"]
        # A library root is a top-level "directory" node (no parent) --
        # only those are individually tracked in Config.library_roots and
        # thus removable; a plain subdirectory isn't its own library entry.
        roots = [n for n in nodes if n is not None and n.kind == "directory" and n.parent is None]
        banks = [n for n in nodes if n is not None and n.kind == "bank"]
        # A row that already declared itself unimportable offers no import
        # action -- it stays visible and searchable, and says why in the
        # Detail pane, which is the whole point of showing it.
        foreigns = [n for n in nodes if n is not None
                    and n.kind in _FOREIGN_KINDS and not n.empty_reason]
        if not presets and not xpms and not programs and not projects \
                and not roots and not foreigns and not banks:
            return
        menu = QMenu(self)
        add_action = None
        convert_action = None
        import_action = None
        remove_action = None
        # Only the presets New Bank would actually ACCEPT. It locks to the
        # first format put in it and refuses any other, so offering "Add" for
        # an E4B preset while New Bank is holding AKAI produced an action
        # that could only ever fail -- the user clicked it and got "This bank
        # is already AKAI". Converting is the real answer at that point, and
        # "Import via mpc2emu..." below is exactly that, so dropping the dead
        # action leaves the right one in place rather than an empty menu.
        locked = self.locked_format()
        addable = [n for n in presets
                   if not locked or _node_format(n) == locked]
        if addable:
            label = f'Add "{addable[0].label}" to New Bank' if len(addable) == 1 \
                else f"Add {len(addable)} presets to New Bank"
            add_action = menu.addAction(label)
        # Excludes any preset node with no resolvable parent bank -- same
        # guard as before, just applied per-node instead of only to a lone
        # selection, since a multi-select can now use this action too.
        convertible = [p for p in presets if p.parent is not None]
        # A KRZ program that references ONLY the sampler's own ROM carries no
        # audio in the file, so there is nothing to convert INTO another
        # machine -- convert_preset() refuses it with exactly that sentence,
        # after the conversion dialog has been filled in and a real assemble
        # has run. Onto a KRZ target it is perfectly fine (a K2000 resolves
        # its own ROM), so this only removes the action where the target is
        # already fixed to something else.
        rom_only = []
        if locked and locked != "KRZ":
            rom_only = [n for n in convertible if _krz_rom_only(n)]
            convertible = [n for n in convertible if n not in rom_only]
        if convertible:
            # One shared Convert Options dialog covers the whole
            # selection -- same options applied to every preset, not one
            # dialog per preset. E4B, KRZ and EIII presets all get this
            # now -- mpc2emu's parsers.krz_parser (added 2026-07-27) and
            # parsers.eiii_parser (added 2026-07-28) made KRZ/EIII real
            # *input* formats too, so a preset from any of the three can
            # be converted the same way, to any target format.
            label = "Import via mpc2emu…" if len(convertible) == 1 \
                else f"Import {len(convertible)} presets via mpc2emu…"
            convert_action = menu.addAction(label)
        if rom_only:
            # Named rather than simply absent: "why can I not convert this
            # one" is the question the row otherwise leaves behind, and the
            # answer is a property of the material, not of the program.
            what = (f'"{rom_only[0].label}" uses' if len(rom_only) == 1
                    else f"{len(rom_only)} of these use")
            menu.addAction(
                f"{what} only the K2000's own ROM — nothing to convert to "
                f"{locked}").setEnabled(False)
        if len(xpms) == 1:
            # Multi-XPM import isn't supported yet -- only offered for a
            # single selected .xpm row.
            import_action = menu.addAction(f'Import "{xpms[0].label}"…')
        elif len(programs) == 1:
            # One keygroup program out of a project: same dialog, same
            # landing in New Bank as importing a standalone .xpm.
            import_action = menu.addAction(f'Import "{programs[0].label}"…')
        elif len(projects) == 1:
            # The whole project at once -- every keygroup program it holds
            # lands in New Bank together, the way adding a bank's presets
            # does. The program count is only known once the project has
            # been expanded (that is what parses it), so don't promise one.
            import_action = menu.addAction(
                f'Import all programs of "{projects[0].label}"…')
        foreign_action = None
        if foreigns:
            # Unlike the MPC actions above this one takes a multi-selection:
            # the requests are just paths, and MainWindow already runs them
            # through one shared options dialog and a serial queue.
            if len(foreigns) == 1:
                node = foreigns[0]
                label = (f'Import all of "{node.label}"…'
                         if node.kind == "foreign_bank"
                         else f'Import "{node.label}"…')
            else:
                label = f"Import {len(foreigns)} instruments…"
            foreign_action = menu.addAction(label)
        fav_action = None
        if len(banks) == 1:
            # On the BANK row, not a preset: the numbers are positions within
            # one bank, so the bank is the thing being named. Picking it here
            # also means no name matching -- the list in the spreadsheet says
            # "Big Bank 64" where the image says "Big Bank 64k".
            fav_action = menu.addAction(
                f'Add favourites from a list to New Bank…')
        if len(roots) == 1:
            # Multi-root removal isn't offered either -- same reasoning,
            # keep the one-item-at-a-time pattern consistent.
            remove_action = menu.addAction(f'Remove "{roots[0].label}" from Library…')
        if not menu.actions():
            # An empty QMenu still pops up -- as a sliver with nothing in it,
            # which reads as the menu having failed to open. Every action
            # above that is single-item-only (favourites, MPC import, removing
            # a library root) silently produced exactly that on a
            # multi-selection. Say which one it was instead.
            menu.addAction(_no_action_reason(
                banks, xpms, programs, projects, roots, presets, locked)
            ).setEnabled(False)
        chosen = menu.exec(global_pos)
        if fav_action is not None and chosen == fav_action:
            self.addFavouritesRequested.emit(banks[0])
        elif add_action is not None and chosen == add_action:
            self.addToBankRequested.emit(addable)
        elif convert_action is not None and chosen == convert_action:
            self.convertPresetRequested.emit(convertible)
        elif import_action is not None and chosen == import_action:
            if xpms:
                self.importXpmRequested.emit(str(xpms[0].payload), None)
            elif programs:
                path, preset_index = programs[0].payload
                self.importXpmRequested.emit(str(path), preset_index)
            else:
                self.importXpmRequested.emit(str(projects[0].payload), None)
        elif foreign_action is not None and chosen == foreign_action:
            self.importForeignRequested.emit([_import_request(n) for n in foreigns])
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


def _node_format(node: TreeNode) -> str:
    """The format a preset row would be added as.

    Read off the PARENT bank row, which is the only node that carries it --
    a preset's own format_label is empty. Same rule MainWindow's
    _add_node_to_bank() uses to build the items, so the menu cannot offer
    something the add would then classify differently.
    """
    return (node.parent.format_label or "") if node.parent is not None else ""


def _krz_rom_only(node: TreeNode) -> bool:
    """True when this KRZ preset has no audio of its own in the file.

    A KRZ program references KEYMAPS, and those reference samples; a sample
    that lives in the machine's ROM has no object in the bank at all. So a
    program none of whose references resolve to a sample object carries no
    audio -- 433 banks in this author's library are entirely like that.

    Deliberately conservative: it answers True only when NOTHING resolves, so
    a program the walk cannot read keeps its convert action and meets
    convert_preset()'s own refusal as before. Cheap enough for a menu -- it
    reads already-parsed objects and touches no PCM.
    """
    payload = getattr(node, "payload", None)
    if not isinstance(payload, tuple) or len(payload) != 2:
        return False
    bank, preset = payload
    if not hasattr(bank, "program_keymap_refs"):
        return False                     # not KRZ; this question is KRZ-only
    try:
        for km_id in bank.program_keymap_refs(preset):
            km = bank.keymaps.get(km_id)
            if km is None:
                continue
            for sid in bank.keymap_sample_refs(km):
                if bank.samples.get(sid) is not None:
                    return False
    except Exception:
        return False
    return True


def _no_action_reason(banks, xpms, programs, projects, roots, presets,
                      locked) -> str:
    """Why a right-click produced nothing, in the user's terms."""
    if len(banks) > 1:
        return "Adding favourites works on one bank at a time"
    if len(xpms) > 1 or len(programs) > 1 or len(projects) > 1:
        return "MPC programs are imported one at a time"
    if len(roots) > 1:
        return "Library folders are removed one at a time"
    if presets and locked:
        return f"New Bank is holding {locked} — these are a different format"
    return "Nothing to do with this selection"
