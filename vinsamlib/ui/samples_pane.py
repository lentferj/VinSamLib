"""
SamplesPane: structured list of the samples a selected preset/program uses —
hidden by default (View ▸ Show Samples Column), and the natural drag-source
for individual samples once M5 adds drag-and-drop.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QLabel, QTableView, QVBoxLayout, QWidget

from . import workers
from .models import TreeNode
from ..banks import summary
from ..build import xpm_import


class ZoneTableModel(QAbstractTableModel):
    HEADERS = ["Sample", "Key range", "Vel range", "Root", "Loop"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._zones: list[summary.ZoneSummary] = []

    def set_zones(self, zones: list[summary.ZoneSummary]) -> None:
        self.beginResetModel()
        self._zones = zones
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._zones)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: int, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole or not index.isValid():
            return None
        z = self._zones[index.row()]
        return (
            z.sample_name,
            f"{z.lo_key}–{z.hi_key}",
            f"{z.lo_vel}–{z.hi_vel}",
            str(z.root_key),
            z.loop,
        )[index.column()]


class SamplesPane(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._title = QLabel("Select a preset or program to list the samples it uses.")
        self._title.setWordWrap(True)
        self._title.setContentsMargins(10, 8, 10, 6)
        layout.addWidget(self._title)

        self._model = ZoneTableModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)          # Sample: flexible
        for col in (1, 2, 3, 4):                                     # Key/Vel/Root/Loop: compact
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table)

        self._gen = 0
        self._live_workers: list[workers.Worker] = []

    def show_node(self, node: TreeNode | None) -> None:
        self._gen += 1
        gen = self._gen
        if node is not None and node.kind in ("xpm", "mpc_program"):
            self._show_mpc_program(node, gen)
            return
        if node is None or node.kind != "preset":
            self._title.setText("Select a preset or program to list the samples it uses.")
            self._model.set_zones([])
            return

        bank, preset_obj = node.payload
        self._title.setText(f"Loading {node.label}…")
        w = workers.Worker(summary.summarize_preset, bank, preset_obj)
        w.signals.finished.connect(lambda ps, g=gen: self._apply(g, ps))
        w.signals.error.connect(lambda msg, g=gen: self._apply_error(g, msg))
        w.signals.finished.connect(lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        w.signals.error.connect(lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        self._live_workers.append(w)
        workers.run(w)

    def _show_mpc_program(self, node: TreeNode, gen: int) -> None:
        """An MPC program lists its samples like any preset -- its zones come
        from build/xpm_import instead of banks/summary, but they are the same
        ZoneSummary rows.

        Which call to make is the same choice DetailPane makes, for the same
        reason: a program inside an already-expanded project reads its zones
        off the Bank cached on the project node, while a loose .xpm/.xty has
        to be parsed. Parsing loads every WAV the program references, so
        reusing that cache is what keeps clicking through a project's
        programs instant."""
        self._title.setText(f"Loading {node.label}…")
        if node.kind == "mpc_program":
            path, preset_index = node.payload
            project = node.parent.handle if node.parent is not None else None
            if project is not None:
                self._run(xpm_import.summarize_program, (project, preset_index), gen)
                return
            self._run(xpm_import.summarize_xpm, (str(path), None, preset_index), gen)
            return
        self._run(xpm_import.summarize_xpm, (str(node.payload),), gen)

    def _run(self, fn, args: tuple, gen: int) -> None:
        w = workers.Worker(fn, *args)
        w.signals.finished.connect(lambda xs, g=gen: self._apply_program(g, xs))
        w.signals.error.connect(lambda msg, g=gen: self._apply_error(g, msg))
        w.signals.finished.connect(
            lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        w.signals.error.connect(
            lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        self._live_workers.append(w)
        workers.run(w)

    def _apply_program(self, gen: int, xs: xpm_import.XpmSummary) -> None:
        if gen != self._gen:
            return
        self._title.setText(f"{xs.preset_name} — {xs.sample_count} sample(s)")
        self._model.set_zones(xs.zones)

    def _apply(self, gen: int, ps: summary.PresetSummary) -> None:
        if gen != self._gen:
            return
        self._title.setText(f"{ps.name} — {len(ps.zones)} sample(s)")
        self._model.set_zones(ps.zones)

    def _apply_error(self, gen: int, message: str) -> None:
        if gen != self._gen:
            return
        self._title.setText("Failed to load samples.")
        self._model.set_zones([])
