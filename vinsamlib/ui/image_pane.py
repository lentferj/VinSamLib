"""
Image column (M6): create a fresh EMU3/K2000/floppy image, open an existing
one, and append/rename/delete/export its bank entries — all funnelled
through build/images.py's safety wrapper so a crash mid-operation can never
corrupt a real library image (every in-place mutation runs on a throwaway
copy that's only swapped in once it fully succeeds).

Like the New Bank column, this locks to one bank format (E4B or KRZ) on
first content — either what's already in an opened image, or the first
thing appended to a blank one — and rejects a mismatched drop, matching the
plan's "Drops are type-checked" rule. Drops are accepted as local file URLs
(`QMimeData.hasUrls()`), which covers both a real OS file-manager drag and
the New Bank column's own drag-out handle (M6) uniformly, since neither
this pane nor mpc2emu's builders care where a bank file came from — only
that it's a real path with the right magic bytes.

Dropping onto an *empty* column (nothing open yet) doesn't reject the drop
the way an unappendable open image would — it opens the New… dialog
pre-seeded with the dropped bank(s), so a bank that only ever existed in
the New Bank column (never manually saved to disk) can go straight onto a
brand-new image in one drag. Without this, creating an EMU3 CD — which,
being exact-fit, must have every one of its banks specified up front — would
otherwise force a save-to-disk-then-browse-for-it round trip for no reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
                             QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMenu, QMessageBox, QPushButton,
                             QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from . import workers
from .models import human_size
from ..banks import eiii
from ..build import images
from ..config import Config
from ..vfs.base import Entry, EntryKind, Volume, WritableVolume
from ..vfs.detect import open_volume
from ..vfs.emu3 import Emu3Volume
from ..vfs.fatvol import Fat12Volume, Fat16Volume, Fat32Volume
from ..vfs.iso9660 import Iso9660Volume

_FORMAT_FROM_EXT = {".e4b": "E4B", ".krz": "KRZ", ".k25": "KRZ", ".k26": "KRZ",
                     ".e3x": "EIII", ".esi": "EIII"}

# E4B and EIII share the exact same EMU3-filesystem image kinds (emu3_cd/
# emu3_hd_emu/emu3_hd_fat) -- IMAGE_KINDS' own format label only lists
# "E4B" for those (the flagship/most common case), so anywhere that label
# is used to match/seed/filter, EIII needs to be treated as equivalent.
_EMU3_FAMILY = {"E4B", "EIII"}


def _sniff_format(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head[:4] == b"FORM" and head[8:12] == b"E4B0":
        return "E4B"
    if head[:4] == b"PRAM":
        return "KRZ"
    if len(head) >= 16 and eiii.detect_format(head[:16]) is not None:
        return "EIII"
    return _FORMAT_FROM_EXT.get(Path(path).suffix.lower())


# Short, plain labels for the info box's Type row -- distinct from
# images.IMAGE_KINDS' longer, parenthetical labels used in the New… dialog's
# combo box, where the extra detail actually helps someone choosing a kind.
_KIND_SHORT_LABEL = {
    "emu3_cd": "EMU3 CD image",
    "emu3_hd_emu": "EMU3 HD image",
    "emu3_hd_fat": "EMU3 HD image (FAT)",
    "k2000_fat16": "K2000 FAT16 disk",
    "k2000_iso9660": "K2000 ISO 9660 CD",
    "fat12_floppy": "FAT12 floppy",
}


def _kind_label(kind: Optional[str], vol: Volume, path: str, fmt: Optional[str]) -> str:
    """A short human label for the info box's Type row. `kind` is exact
    when known (this pane just built the image itself, via the New…
    dialog); otherwise it's guessed from the volume class plus the bank
    format already sniffed for it -- Emu3Volume covers both EMU3 CD and HD
    (identical on-disk format, distinguished here only by file extension,
    since a real CD image is never appendable in practice and an .hda
    always is), and Fat16Volume/Fat32Volume cover both an EOS FAT HD and a
    K2000 FAT16 disk (identical FAT structure, distinguished only by which
    bank format is actually on it)."""
    if kind is not None:
        return _KIND_SHORT_LABEL.get(kind, kind)
    if isinstance(vol, Emu3Volume):
        return "EMU3 CD image" if Path(path).suffix.lower() == ".iso" else "EMU3 HD image"
    if isinstance(vol, Fat12Volume):
        return "FAT12 floppy"
    if isinstance(vol, (Fat16Volume, Fat32Volume)):
        return "EMU3 HD image (FAT)" if fmt in ("E4B", "EIII") else "K2000 FAT16 disk"
    if isinstance(vol, Iso9660Volume):
        return "K2000 ISO 9660 CD"
    return "Unknown"


class ImagePane(QWidget):
    statusMessage = Signal(str)

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._config = config

        self._path: Optional[str] = None
        self._format: Optional[str] = None
        self._appendable = False
        self._entries: list[Entry] = []
        self._kind: Optional[str] = None   # an images.IMAGE_KINDS key, when known
        self._type_label_text = ""
        self._busy = False
        self._live_workers: list[workers.Worker] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._head = QLabel("Image")
        self._head.setStyleSheet("font-weight: 600; padding: 6px 10px;"
                                  "border-bottom: 1px solid palette(mid);")
        layout.addWidget(self._head)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)
        self._stack.addWidget(self._build_empty_page())
        self._stack.addWidget(self._build_open_page())
        self._stack.setCurrentIndex(0)

    # -- pages ------------------------------------------------------------------

    def _build_empty_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(10, 10, 10, 10)

        box = QFrame()
        box.setStyleSheet("QFrame { border: 1px dashed palette(mid); border-radius: 6px; }")
        box_layout = QVBoxLayout(box)
        box_layout.addStretch()
        hint = QLabel("Drop a bank here to start a new image with it,\n"
                       "or create/open one below.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: palette(placeholdertext);")
        box_layout.addWidget(hint)
        box_layout.addStretch()
        outer.addWidget(box, 1)

        buttons = QHBoxLayout()
        new_btn = QPushButton("New…")
        new_btn.clicked.connect(lambda: self._new_image())
        buttons.addWidget(new_btn)
        open_btn = QPushButton("Open…")
        open_btn.clicked.connect(self._open_image_dialog)
        buttons.addWidget(open_btn)
        outer.addLayout(buttons)
        return page

    def _build_open_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 8, 10, 10)

        info_box = QFrame()
        info_box.setStyleSheet(
            "QFrame { border: 1px solid palette(mid); border-radius: 6px; }")
        info_form = QFormLayout(info_box)
        info_form.setContentsMargins(8, 6, 8, 6)
        info_form.setSpacing(4)

        self._info_name_label = QLabel("")
        self._info_name_label.setWordWrap(True)
        self._info_name_label.setStyleSheet("font-weight: 600;")
        info_form.addRow("Name:", self._info_name_label)

        self._info_path_label = QLabel("")
        self._info_path_label.setWordWrap(True)
        self._info_path_label.setStyleSheet("color: palette(placeholdertext);")
        info_form.addRow("Path:", self._info_path_label)

        self._info_type_label = QLabel("")
        info_form.addRow("Type:", self._info_type_label)

        self._info_format_label = QLabel("")
        info_form.addRow("Format:", self._info_format_label)

        self._info_size_label = QLabel("")
        info_form.addRow("Size:", self._info_size_label)

        self._info_contents_label = QLabel("")
        info_form.addRow("Contents:", self._info_contents_label)

        layout.addWidget(info_box)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_list_context_menu)
        delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, self._list)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_shortcut.activated.connect(self._delete_selected)
        layout.addWidget(self._list, 1)

        row1 = QHBoxLayout()
        self._append_btn = QPushButton("Append File(s)…")
        self._append_btn.clicked.connect(self._append_files_dialog)
        row1.addWidget(self._append_btn)
        export_btn = QPushButton("Export…")
        export_btn.clicked.connect(self._export_selected)
        row1.addWidget(export_btn)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        self._rename_btn = QPushButton("Rename…")
        self._rename_btn.clicked.connect(self._rename_selected)
        row2.addWidget(self._rename_btn)
        self._delete_btn = QPushButton("Delete")
        self._delete_btn.clicked.connect(self._delete_selected)
        row2.addWidget(self._delete_btn)
        row2.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self._close_image)
        row2.addWidget(close_btn)
        layout.addLayout(row2)

        return page

    # -- drag & drop --------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if self._acceptable(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        if not self._acceptable(mime):
            event.ignore()
            return
        paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        event.acceptProposedAction()
        if self._path is None:
            # Nothing open yet: a drop here means "start a new image with
            # this" -- the direct fix for not being able to get a bank
            # that's only ever existed in the New Bank column (never saved
            # to disk) onto a fresh image without a manual Save As step.
            fmt = self._validate_formats(paths, warn=True)
            if fmt is not None:
                self._new_image(seed_paths=paths, seed_format=fmt)
            return
        self._append_paths(paths)

    def _acceptable(self, mime) -> bool:
        if self._busy:
            return False
        if not mime.hasUrls():
            return False
        paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        if not paths:
            return False
        if self._path is None:
            # No image open: any single-format, recognised set of bank
            # files is acceptable -- it seeds a brand-new image (see
            # dropEvent) rather than appending to one.
            return self._validate_formats(paths, warn=False) is not None
        if not self._appendable:
            self.statusMessage.emit("This image can't be appended to (it's read-only)")
            return False
        fmt = self._validate_formats(paths, warn=False)
        return fmt is not None

    def _validate_formats(self, paths: list[str], warn: bool = True) -> Optional[str]:
        formats = {_sniff_format(p) for p in paths}
        if None in formats or len(formats) != 1:
            if warn:
                self.statusMessage.emit("Can't drop a mix of formats, or an unrecognised file")
            return None
        fmt = formats.pop()
        if self._format is not None and fmt != self._format:
            if warn:
                self.statusMessage.emit(f"This image is already {self._format} — can't add a {fmt} bank")
            return None
        return fmt

    # -- public entry point for the New Bank column's "Send to Image Column" ----

    def receive_bank_files(self, paths: list[str], fmt: str) -> None:
        """Entry point for the Pending for Image column's "Build Image ->"
        -- a batch of already-assembled bank files, in the order they
        should end up on the image. Seeds a brand-new image (pre-filling
        every path at once, so a fresh EMU3 CD's exact-fit requirement is
        satisfied in one build) if nothing's open, or appends the whole
        batch in one call otherwise (one rebuild-if-needed instead of one
        per bank, for the exact-fit EMU3 CD fallback path)."""
        if not paths:
            return
        if self._path is None:
            self._new_image(seed_paths=paths, seed_format=fmt)
            return
        self._append_paths(paths)

    # -- opening / creating -------------------------------------------------------

    def _dialog_start_dir(self) -> str:
        return str(self._config.last_image_dir) if self._config.last_image_dir else ""

    def _remember_dir(self, chosen_path: str) -> None:
        directory = Path(chosen_path).parent
        if self._config.last_image_dir != directory:
            self._config.last_image_dir = directory
            self._config.save()

    def _open_image_dialog(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Open Image", self._dialog_start_dir(),
            "Disk images (*.iso *.hda *.img);;All files (*)",
            options=QFileDialog.Option.DontUseNativeDialog)
        if path:
            self._remember_dir(path)
            self._kind = None   # unknown provenance -- _open_image() will guess from content
            self._open_image(path)

    def _open_image(self, path: str, known_kind: Optional[str] = None) -> None:
        try:
            vol = open_volume(path)
        except Exception as ex:
            self.statusMessage.emit(f"Couldn't open {Path(path).name}: {ex}")
            return
        if vol is None:
            self.statusMessage.emit(f"{Path(path).name}: not a recognised image")
            return

        entries: list[Entry] = []

        def _walk(folder: Optional[Entry] = None) -> None:
            for e in vol.list(folder):
                if e.kind == EntryKind.FOLDER:
                    _walk(e)
                elif e.kind == EntryKind.BANK:
                    entries.append(e)

        fmt: Optional[str] = None
        try:
            _walk()
            if entries:
                data = vol.read(entries[0])
                if data[:4] == b"FORM" and data[8:12] == b"E4B0":
                    fmt = "E4B"
                elif data[:4] == b"PRAM":
                    fmt = "KRZ"
                elif len(data) >= 16 and eiii.detect_format(data[:16]) is not None:
                    fmt = "EIII"
        finally:
            vol.close()

        self._path = path
        self._format = fmt
        self._appendable = isinstance(vol, WritableVolume)
        self._entries = entries
        # known_kind is only passed by _new_image() (which knows exactly
        # what it just built) -- a refresh after append/rename/delete calls
        # _open_image(self._path) with no known_kind and must not clobber
        # whatever kind was already established.
        if known_kind is not None:
            self._kind = known_kind
        self._type_label_text = _kind_label(known_kind or self._kind, vol, path, fmt)
        self._refresh()
        self.statusMessage.emit(f"Opened {Path(path).name} ({len(entries)} bank(s))")

    def _new_image(self, seed_paths: Optional[list[str]] = None,
                    seed_format: Optional[str] = None) -> None:
        dlg = _NewImageDialog(self, self._config, seed_paths=seed_paths, seed_format=seed_format)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        spec = dlg.result_spec()
        if spec is None:
            return
        self._remember_dir(spec["output_path"])
        self._run_confirmed_op(
            f"Building {Path(spec['output_path']).name}…",
            workers.Worker(images.create_image, spec["kind"], spec["output_path"],
                           spec["bank_paths"], spec["volume_label"], spec["size_mb"],
                           spec["floppy_kind"]),
            on_done=lambda _log, p=spec["output_path"], k=spec["kind"]:
                self._open_image(p, known_kind=k),
        )

    def _close_image(self) -> None:
        self._path = None
        self._format = None
        self._appendable = False
        self._entries = []
        self._kind = None
        self._type_label_text = ""
        self._refresh()

    # -- list refresh ---------------------------------------------------------

    def _refresh(self) -> None:
        if self._path is None:
            self._stack.setCurrentIndex(0)
            return
        self._stack.setCurrentIndex(1)

        path = Path(self._path)
        self._info_name_label.setText(path.name)
        self._info_path_label.setText(str(path.parent))
        self._info_type_label.setText(self._type_label_text or "(unknown)")
        self._info_format_label.setText(self._format or "(none yet — drop or append a bank)")
        try:
            self._info_size_label.setText(human_size(path.stat().st_size))
        except OSError:
            self._info_size_label.setText("(unknown)")
        n = len(self._entries)
        contents = f"{n} bank{'s' if n != 1 else ''}"
        if not self._appendable:
            contents += "  —  read-only"
        self._info_contents_label.setText(contents)

        # rename/delete need the same WritableVolume capability as append
        # (a plain Volume like Iso9660Volume implements neither) — a CD
        # that's merely out of free space to *append* to can still rename
        # or delete what's already on it, but that's a finer distinction
        # than this UI currently bothers to surface.
        self._append_btn.setEnabled(self._appendable)
        self._rename_btn.setEnabled(self._appendable)
        self._delete_btn.setEnabled(self._appendable)

        self._list.clear()
        for e in self._entries:
            item = QListWidgetItem(f"{e.name}   {human_size(e.size)}" if e.size else e.name)
            item.setData(Qt.ItemDataRole.UserRole, e)
            self._list.addItem(item)

    def _selected_entry(self) -> Optional[Entry]:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_list_context_menu(self, pos) -> None:
        index = self._list.indexAt(pos)
        if not index.isValid():
            return
        self._list.setCurrentRow(index.row())
        menu = QMenu(self)
        rename_action = menu.addAction("Rename…")
        rename_action.setEnabled(self._appendable)
        delete_action = menu.addAction("Delete")
        delete_action.setEnabled(self._appendable)
        menu.addSeparator()
        export_action = menu.addAction("Export…")
        chosen = menu.exec(self._list.viewport().mapToGlobal(pos))
        if chosen == rename_action:
            self._rename_selected()
        elif chosen == delete_action:
            self._delete_selected()
        elif chosen == export_action:
            self._export_selected()

    # -- append -----------------------------------------------------------------

    def _append_files_dialog(self) -> None:
        if self._format == "KRZ":
            filt = "KRZ banks (*.krz *.k25 *.k26)"
        elif self._format == "E4B":
            filt = "E4B banks (*.e4b)"
        elif self._format == "EIII":
            filt = "EIII banks (*.e3x *.esi)"
        else:
            filt = "Banks (*.e4b *.krz *.k25 *.k26 *.e3x *.esi)"
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Append Bank File(s)", self._dialog_start_dir(), filt,
            options=QFileDialog.Option.DontUseNativeDialog)
        if paths:
            self._remember_dir(paths[0])
            self._append_paths(paths)

    def _append_paths(self, paths: list[str]) -> None:
        fmt = self._validate_formats(paths)
        if fmt is None:
            return
        image_name = Path(self._path).name
        if not self._confirm(f"Append {len(paths)} bank(s) to '{image_name}'?"):
            return
        if self._format is None:
            self._format = fmt
        self._run_confirmed_op(
            f"Appending to {image_name}…",
            workers.Worker(images.append_banks, self._path, fmt, paths),
            on_done=lambda _result: self._open_image(self._path),
        )

    # -- rename / delete / export -----------------------------------------------

    def _rename_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=entry.name.strip())
        if not ok or not new_name.strip():
            return
        image_name = Path(self._path).name
        if not self._confirm(f"Rename '{entry.name.strip()}' to '{new_name.strip()}' in '{image_name}'?"):
            return
        self._run_confirmed_op(
            f"Renaming in {image_name}…",
            workers.Worker(images.rename_entry, self._path, entry, new_name.strip()),
            on_done=lambda _result: self._open_image(self._path),
        )

    def _delete_selected(self) -> None:
        if not self._appendable:
            return
        entry = self._selected_entry()
        if entry is None:
            return
        image_name = Path(self._path).name
        if not self._confirm(
                f"Delete '{entry.name.strip()}' from '{image_name}'?\nThis cannot be undone."):
            return
        self._run_confirmed_op(
            f"Deleting from {image_name}…",
            workers.Worker(images.delete_entry, self._path, entry),
            on_done=lambda _result: self._open_image(self._path),
        )

    def _export_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        ext = Path(entry.name.strip()).suffix or ".bin"
        start_dir = self._dialog_start_dir()
        default_path = str(Path(start_dir) / entry.name.strip()) if start_dir else entry.name.strip()
        out_path, _filter = QFileDialog.getSaveFileName(
            self, "Export Bank", default_path, f"Bank (*{ext})",
            options=QFileDialog.Option.DontUseNativeDialog)
        if not out_path:
            return
        self._remember_dir(out_path)
        if Path(out_path).exists():
            Path(out_path).unlink()
        self._run_confirmed_op(
            f"Exporting {entry.name.strip()}…",
            workers.Worker(images.export_entry, self._path, entry, out_path),
            on_done=lambda _result: self.statusMessage.emit(f"Exported to {out_path}"),
        )

    # -- shared op runner ---------------------------------------------------------

    def _confirm(self, text: str) -> bool:
        return QMessageBox.question(
            self, "Confirm", text + "\n\nA safety copy is made and only swapped in "
            "if this succeeds, but back up irreplaceable library images regardless.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def _run_confirmed_op(self, busy_message: str, worker: workers.Worker, on_done) -> None:
        if self._busy:
            self.statusMessage.emit("Another image operation is still running")
            return
        self._busy = True
        self._set_buttons_enabled(False)
        self.statusMessage.emit(busy_message)

        def _finished(result):
            self._busy = False
            self._set_buttons_enabled(True)
            on_done(result)

        def _error(message: str):
            self._busy = False
            self._set_buttons_enabled(True)
            last_line = workers.last_error_line(message)
            self.statusMessage.emit(f"Failed: {last_line}")

        worker.signals.finished.connect(_finished)
        worker.signals.error.connect(_error)
        worker.signals.finished.connect(lambda *_: self._live_workers.remove(worker)
                                          if worker in self._live_workers else None)
        worker.signals.error.connect(lambda *_: self._live_workers.remove(worker)
                                       if worker in self._live_workers else None)
        self._live_workers.append(worker)
        workers.run(worker)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for btn in self.findChildren(QPushButton):
            btn.setEnabled(enabled)
        if enabled:
            self._append_btn.setEnabled(self._appendable)
            self._rename_btn.setEnabled(self._appendable)
            self._delete_btn.setEnabled(self._appendable)


class _NewImageDialog(QDialog):
    """New… dialog: pick a kind, an output path, a volume label, and
    (for the appendable HD/disk kinds) a size with real headroom, plus
    optional initial bank files."""

    def __init__(self, parent=None, config: Optional[Config] = None,
                 seed_paths: Optional[list[str]] = None,
                 seed_format: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle("New Image")
        self._config = config
        self._bank_paths: list[str] = list(seed_paths or [])

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._kind_box = QComboBox()
        seed_row = None
        for row, (key, (fmt, label, _default_label)) in enumerate(images.IMAGE_KINDS.items()):
            self._kind_box.addItem(f"{label}  [{fmt}]", key)
            seed_matches = fmt == seed_format or ({fmt, seed_format} <= _EMU3_FAMILY)
            if seed_format is not None and seed_matches and seed_row is None:
                seed_row = row   # first matching kind, e.g. E4B/EIII -> emu3_cd
        if seed_row is not None:
            self._kind_box.setCurrentIndex(seed_row)
        self._kind_box.currentIndexChanged.connect(self._on_kind_changed)
        form.addRow("Kind:", self._kind_box)

        self._label_edit = QLineEdit()
        form.addRow("Volume label:", self._label_edit)

        self._size_spin = QSpinBox()
        self._size_spin.setRange(16, 14 * 1024)
        self._size_spin.setSuffix(" MB")
        self._size_spin.setSpecialValueText("auto")
        self._size_spin.setValue(16)   # == minimum -> shows "auto" (size_mb=None)
        form.addRow("Size:", self._size_spin)

        self._floppy_box = QComboBox()
        self._floppy_box.addItems(["1440", "720"])
        form.addRow("Floppy size (KB):", self._floppy_box)

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("Type a path, or use Browse…")
        path_row.addWidget(self._path_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_output)
        path_row.addWidget(browse_btn)
        form.addRow("Save to:", path_row)

        layout.addLayout(form)

        layout.addWidget(QLabel("Initial banks (optional for HD/disk/floppy kinds):"))
        self._bank_list = QListWidget()
        for p in self._bank_paths:
            self._bank_list.addItem(QListWidgetItem(Path(p).name))
        layout.addWidget(self._bank_list)
        bank_buttons = QHBoxLayout()
        add_btn = QPushButton("Add Files…")
        add_btn.clicked.connect(self._add_bank_files)
        bank_buttons.addWidget(add_btn)
        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self._remove_bank_file)
        bank_buttons.addWidget(remove_btn)
        bank_buttons.addStretch()
        layout.addLayout(bank_buttons)

        self._spec: Optional[dict] = None
        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self._try_accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._on_kind_changed()
        self.resize(480, 420)

    def _current_kind(self) -> str:
        return self._kind_box.currentData()

    def _on_kind_changed(self) -> None:
        kind = self._current_kind()
        fmt, _label, default_label = images.IMAGE_KINDS[kind]
        if not self._label_edit.text():
            self._label_edit.setText(default_label)
        is_floppy = kind == "fat12_floppy"
        needs_size = kind in ("emu3_hd_emu", "emu3_hd_fat", "k2000_fat16")
        self._size_spin.setVisible(needs_size)
        self._floppy_box.setVisible(is_floppy)

    def _start_dir(self) -> str:
        return str(self._config.last_image_dir) if self._config and self._config.last_image_dir else ""

    def _browse_output(self) -> None:
        kind = self._current_kind()
        ext = {"emu3_cd": "iso", "emu3_hd_emu": "hda", "emu3_hd_fat": "hda",
               "k2000_fat16": "hda", "k2000_iso9660": "iso", "fat12_floppy": "img"}[kind]
        start_dir = self._start_dir()
        default = str(Path(start_dir) / f"NewImage.{ext}") if start_dir else f"NewImage.{ext}"
        path, _filter = QFileDialog.getSaveFileName(
            self, "New Image", default, f"*.{ext}",
            options=QFileDialog.Option.DontUseNativeDialog)
        if path:
            self._path_edit.setText(path)

    def _add_bank_files(self) -> None:
        kind = self._current_kind()
        fmt, _label, _dl = images.IMAGE_KINDS[kind]
        filt = ("E4B/EIII banks (*.e4b *.e3x *.esi)" if fmt == "E4B"
                else "KRZ banks (*.krz *.k25 *.k26)")
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Add Bank Files", self._start_dir(), filt,
            options=QFileDialog.Option.DontUseNativeDialog)
        for p in paths:
            self._bank_paths.append(p)
            self._bank_list.addItem(QListWidgetItem(Path(p).name))

    def _remove_bank_file(self) -> None:
        row = self._bank_list.currentRow()
        if row >= 0:
            del self._bank_paths[row]
            self._bank_list.takeItem(row)

    def _try_accept(self) -> None:
        kind = self._current_kind()
        output_path = self._path_edit.text().strip()
        if not output_path:
            QMessageBox.warning(self, "New Image", "Type or choose a location to save the image first.")
            return
        if kind in ("emu3_cd", "k2000_iso9660") and not self._bank_paths:
            QMessageBox.warning(self, "New Image",
                                 "This image format is created once and can't be appended to "
                                 "later — add at least one bank.")
            return
        size_mb = self._size_spin.value() if self._size_spin.value() > 16 else None
        self._spec = {
            "kind": kind,
            "output_path": output_path,
            "bank_paths": list(self._bank_paths),
            "volume_label": self._label_edit.text().strip(),
            "size_mb": size_mb,
            "floppy_kind": self._floppy_box.currentText(),
        }
        self.accept()

    def result_spec(self) -> Optional[dict]:
        return self._spec
