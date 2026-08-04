"""
SamplesPane: structured list of the samples a selected preset/program uses —
hidden by default (View ▸ Show Samples Column), and the natural drag-source
for individual samples once M5 adds drag-and-drop.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHeaderView, QLabel,
                                QStyle, QStyledItemDelegate, QTableView, QVBoxLayout,
                                QWidget)

from . import workers
from .models import TreeNode
from ..banks import summary
from ..build import xpm_import


class ZoneTableModel(QAbstractTableModel):
    HEADERS = ["Sample", "Key range", "Vel range", "Root", "Loop"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._zones: list[summary.ZoneSummary] = []
        self._full_names: dict[str, str] = {}

    def set_zones(self, zones: list[summary.ZoneSummary],
                  full_names: dict[str, str] | None = None) -> None:
        """`full_names` maps a shortened sample name to the whole name it came
        from, for programs that have not been imported yet -- see
        build/xpm_import.full_sample_names(). Without it the stored names are
        shown as they are, which is what a bank's own presets carry."""
        self.beginResetModel()
        self._zones = zones
        self._full_names = full_names or {}
        self.endResetModel()

    def _name_parts(self, row: int) -> tuple[str, str] | None:
        """(what an import will drop, what it will keep), or None when the
        name survives whole. The split is by length: mpc2emu's _safe_name
        replaces characters one for one before it cuts, so the tail it keeps
        is the same length as the end of the original."""
        stored = self._zones[row].sample_name
        full = self._full_names.get(stored)
        if not full or len(full) <= len(stored):
            return None
        return full[:len(full) - len(stored)], full[len(full) - len(stored):]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._zones)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: int, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if index.column() == 0:
            parts = self._name_parts(index.row())
            if role == Qt.ItemDataRole.UserRole:
                return parts
            if parts is not None and role == Qt.ItemDataRole.ToolTipRole:
                return (f"{parts[0]}{parts[1]}\n\nImports as "
                        f"'{self._zones[index.row()].sample_name}' — an E4B or KRZ "
                        f"sample name holds 16 characters, and the END is kept "
                        f"because that is what tells a multisample's notes apart.")
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        z = self._zones[index.row()]
        if index.column() == 0:
            parts = self._name_parts(index.row())
            return parts[0] + parts[1] if parts else z.sample_name
        return (
            z.sample_name,
            f"{z.lo_key}–{z.hi_key}",
            f"{z.lo_vel}–{z.hi_vel}",
            str(z.root_key),
            z.loop,
        )[index.column()]


# Readable on this pane's normal background and on the selection highlight
# respectively -- one red is never both.
_DROP_RED = QColor("#c0392b")
_DROP_RED_SELECTED = QColor("#ffc9c2")


class TruncatedNameDelegate(QStyledItemDelegate):
    """Draws a sample name that an import will shorten, with the part that
    will not survive in red. Two colours in one cell, which no item role can
    express, so the cell is painted here instead: the row's own background
    and selection come from the style as usual, only the text is ours."""

    def paint(self, painter, option, index) -> None:
        parts = index.data(Qt.ItemDataRole.UserRole)
        if not parts:
            super().paint(painter, option, index)
            return
        dropped, kept = parts

        self.initStyleOption(option, index)
        option.text = ""                      # the style draws everything but the text
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)

        rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, option, option.widget)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        metrics = painter.fontMetrics()
        baseline = rect.top() + (rect.height() + metrics.ascent() - metrics.descent()) // 2
        x = rect.left()

        painter.save()
        painter.setPen(_DROP_RED_SELECTED if selected else _DROP_RED)
        painter.drawText(x, baseline, dropped)
        x += metrics.horizontalAdvance(dropped)
        painter.setPen(option.palette.color(
            QPalette.ColorGroup.Normal,
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text))
        painter.drawText(x, baseline, kept)
        painter.restore()


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
        self._table.setItemDelegateForColumn(0, TruncatedNameDelegate(self))
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
                self._run(xpm_import.summarize_program, (project, preset_index),
                          gen, path, node.label)
                return
            self._run(xpm_import.summarize_xpm, (str(path), None, preset_index),
                      gen, path, node.label)
            return
        self._run(xpm_import.summarize_xpm, (str(node.payload),), gen,
                  node.payload, node.label)

    def _run(self, fn, args: tuple, gen: int, source=None, label: str = "") -> None:
        # The whole names come off the same worker thread as the summary --
        # reading them opens the file again, which the GUI thread must not do.
        def work():
            xs = fn(*args)
            names = ({} if source is None else
                     xpm_import.full_sample_names([z.sample_name for z in xs.zones],
                                                   str(source)))
            return xs, names, label

        w = workers.Worker(work)
        w.signals.finished.connect(lambda xs, g=gen: self._apply_program(g, xs))
        w.signals.error.connect(lambda msg, g=gen: self._apply_error(g, msg))
        w.signals.finished.connect(
            lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        w.signals.error.connect(
            lambda *_: self._live_workers.remove(w) if w in self._live_workers else None)
        self._live_workers.append(w)
        workers.run(w)

    def _apply_program(self, gen: int, result) -> None:
        if gen != self._gen:
            return
        xs, full_names, label = result
        # The tree row's own label, not the preset name: that one is already
        # shortened to 16 characters, and this pane is about what a name is
        # before an import gets to it.
        self._title.setText(f"{label or xs.preset_name} — {xs.sample_count} sample(s)")
        self._model.set_zones(xs.zones, full_names)

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
