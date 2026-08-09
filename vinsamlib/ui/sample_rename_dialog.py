"""Rename the samples inside a bank New Bank is about to assemble.

Deliberately NOT sample_placement_dialog.py with its columns hidden. That one
edits where a sample sits on the keyboard, which is a property of a zone in a
folder being imported; this edits what a sample is CALLED in a bank already
assembled from real presets, where the key ranges came with the material and
are not ours to offer. Showing low/root/high here would put three editable
fields in front of the user that change nothing — the same "a control that
silently does nothing" this whole feature was held back to avoid.

The name rules are the format's, not ours: 16 characters is the field width in
both E4B and EIII, and a duplicate is legal but gets numbered apart by the
writer's own uniquifier. Both are warnings, never blocking, on the same
"show, don't block" principle the placement dialog uses.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt

from ..notes import midi_to_name
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog,
                             QDialogButtonBox,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QMessageBox,
                             QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout)

#: Both E4B (`body[2:18]`) and EIII (`body[0:16]`) store a sample name in a
#: fixed 16-byte field. Longer is not refused -- the writer truncates -- but
#: the user should see it before pressing OK rather than after.
MAX_NAME = 16


def _note(root: Optional[int], octave_offset: int) -> str:
    """The key a sample plays, or an em dash when the bank did not say."""
    return midi_to_name(root, octave_offset) if root is not None else "—"


class SampleRenameDialog(QDialog):
    def __init__(self, rows: list, fmt: str = "E4B", octave_offset: int = 2,
                 existing: Optional[dict] = None, parent=None):
        """`rows`: [{"name": str, "root": int|None}] in bank order, or plain
        names for callers that have no mapping. Duplicates are kept as
        separate rows -- two samples really can share a name, and renaming
        one of them is a reasonable thing to want.

        `root` is shown read-only and drives the key suffix. Read-only is
        the point: the placement editor exists to CHANGE where a sample
        sits, and these ranges came with the material. Showing them is
        information; offering to edit them here would be a control that
        does nothing."""
        super().__init__(parent)
        self.setWindowTitle("Rename Samples")
        self.setMinimumSize(560, 420)
        self._octave_offset = octave_offset
        self._rows: list[dict] = [
            {"name": r["name"] if isinstance(r, dict) else r,
             "root": r.get("root") if isinstance(r, dict) else None,
             "vel": r.get("vel") if isinstance(r, dict) else None,
             # Carried through, not rebuilt: the caller knows which other
             # staged presets use this sample and the dialog cannot work it
             # out. Dropping it here silently disabled both the italic
             # marking and the confirmation that depend on it, while the
             # caller went on supplying it -- the two halves each looked
             # right in isolation.
             "shared_with": list(r.get("shared_with") or ()) if isinstance(r, dict) else [],
             # Renames already set for these samples come back in, so
             # reopening the dialog shows the work rather than a blank
             # slate -- and marks them hand-typed, so a bulk apply respects
             # them exactly as if they had just been entered.
             "new_name": (existing or {}).get(
                 r["name"] if isinstance(r, dict) else r, ""),
             "hand_typed": bool((existing or {}).get(
                 r["name"] if isinstance(r, dict) else r))}
            for r in rows]
        self._have_roots = any(r["root"] is not None for r in self._rows)

        layout = QVBoxLayout(self)
        # The warning is in the dialog, not only the README: this is the one
        # place a user is about to commit to it, and for KRZ it is the least
        # verified operation in the program -- the object block physically
        # grows to fit a longer name and no sampler has loaded one.
        info = QLabel(
            f"⚠ Experimental — no sampler has yet loaded a bank renamed this "
            f"way. The audio is never touched; only the label the "
            f"instrument shows changes.\n"
            f"A name over {MAX_NAME} characters is truncated, and two samples "
            f"sharing a name are numbered apart. Italic rows are used by "
            f"another preset too, and will be renamed there as well.")
        info.setWordWrap(True)
        info.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        layout.addWidget(info)

        # Renaming 57 samples one cell at a time is not a feature. The base
        # name fills every row at once and each row stays individually
        # editable afterwards -- the typed row wins, same rule as the
        # placement editor.
        base_row = QHBoxLayout()
        base_row.addWidget(QLabel("Name them all:"))
        self._base_edit = QLineEdit()
        self._base_edit.setPlaceholderText("base name")
        self._base_edit.setToolTip(
            "Fill every row with this name plus a number. Rows you have "
            "already typed into are left alone.")
        base_row.addWidget(self._base_edit, 1)
        # Numbered, NOT key-suffixed, and the difference is deliberate. The
        # import dialog offers `<base>-<key>` because it holds a parsed Bank
        # whose root notes exist by construction. Here the only route to a
        # root note is through mpc2emu's summary, which would have to be
        # joined back to these rows BY NAME -- and its name decoding is the
        # very thing that produces U+FFFD on some real banks, so the join key
        # is untrustworthy for exactly the samples most in need of renaming.
        self._with_key = QCheckBox("append key")
        self._with_key.setChecked(True)
        self._with_key.setEnabled(self._have_roots)
        self._with_key.setToolTip(
            "Name each sample after the key it plays, e.g. Strings-C3 --\n"
            "the same scheme the sample-folder import offers."
            if self._have_roots else
            "No root notes could be read from this bank's zones, so the "
            "samples are numbered instead.")
        base_row.addWidget(self._with_key)
        self._apply_base_btn = QPushButton("Apply")
        self._apply_base_btn.clicked.connect(self._apply_base)
        base_row.addWidget(self._apply_base_btn)
        layout.addLayout(base_row)

        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["Sample", "Plays", "New name"])
        self._table.verticalHeader().setVisible(False)
        # SingleSelection, not NoSelection: an item cannot be edited unless it
        # can become current, so NoSelection silently made the whole "New
        # name" column read-only while its items still carried
        # ItemIsEditable. The flag said editable, the view refused, and a
        # programmatic setText() in the tests hid it -- reported as "I cannot
        # rename a single entry".
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._build()
        self._table.itemChanged.connect(self._on_edited)

    def _build(self) -> None:
        self._table.blockSignals(True)
        self._table.setRowCount(len(self._rows))
        for r, row in enumerate(self._rows):
            original = QTableWidgetItem(row["name"])
            original.setFlags(Qt.ItemFlag.ItemIsEnabled)      # identity, never edited
            self._table.setItem(r, 0, original)

            shared = row.get("shared_with") or []
            if shared:
                # Renaming is keyed by the sample's NAME, so it follows the
                # sample into every preset that uses it. Said here rather than
                # prevented: sharing is normal in a real bank, and silently
                # renaming someone else's sample is the surprise worth avoiding.
                original.setToolTip("also used by " + ", ".join(shared))
                font = original.font()
                font.setItalic(True)
                original.setFont(font)

            # A full-range window (0-127) is the overwhelming default and
            # adds nothing but noise; only a REAL layer is worth the space.
            label = _note(row["root"], self._octave_offset)
            vel = row.get("vel")
            if vel and tuple(vel) != (0, 127):
                label = f"{label}  v{vel[0]}-{vel[1]}"
            plays = QTableWidgetItem(label)
            plays.setFlags(Qt.ItemFlag.ItemIsEnabled)      # read-only: information
            plays.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(r, 1, plays)

            edit = QTableWidgetItem(row["new_name"] or row["name"])
            edit.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable)
            # The ORIGINAL name is the row's identity everywhere else: it is
            # the key assemble() looks a rename up by, so it has to survive
            # the cell being retyped.
            edit.setData(Qt.ItemDataRole.UserRole, row["name"])
            self._table.setItem(r, 2, edit)
        self._table.blockSignals(False)
        self._refresh_warnings()

    def _apply_base(self) -> None:
        """`<base>-NN` across every row, NN zero-padded to the row count.

        Rows already typed into are skipped: a hand-typed name is a decision
        and the bulk field is a convenience, so the convenience does not win.
        Clearing the field and applying resets the untouched rows to their
        originals, which is the only way back out of a mistaken bulk apply
        short of cancelling the dialog.
        """
        base = self._base_edit.text().strip()
        width = max(2, len(str(len(self._rows))))
        by_key = self._with_key.isChecked() and self._have_roots
        for i, row in enumerate(self._rows, 1):
            if row.get("hand_typed"):
                continue
            if not base:
                row["new_name"] = ""
                continue
            # "#" becomes "s" to match build/sample_names.names_from_base --
            # a bank named here and one named at import must not differ by
            # spelling alone. Falls back to the number when a row has no
            # root, so a partial mapping still produces unique names.
            if by_key and row["root"] is not None:
                key = _note(row["root"], self._octave_offset).replace("#", "s")
                row["new_name"] = f"{base}-{key}"
            else:
                row["new_name"] = f"{base}-{i:0{width}d}"

        # Several samples can share a root -- three OBXa samples all recorded
        # at C2 turned into three identical `Foobar1-C2`, which the writer's
        # own uniquifier would then have numbered apart into names the user
        # never chose. Disambiguated here instead, where the choice is
        # visible: the first keeps the plain key, the rest get -2, -3, ...
        # Only names this apply just produced are renumbered; a hand-typed
        # duplicate stays exactly as typed and is flagged, not rewritten.
        seen: dict = {}
        for row in self._rows:
            if row.get("hand_typed") or not row["new_name"]:
                continue
            stem = row["new_name"]
            n = seen.get(stem, 0) + 1
            seen[stem] = n
            if n > 1:
                row["new_name"] = f"{stem}-{n}"
        self._build()

    def _on_edited(self, item) -> None:
        if item.column() != 2:
            return
        original = item.data(Qt.ItemDataRole.UserRole)
        row = next((r for r in self._rows if r["name"] == original), None)
        if row is None:
            return
        # Compared stripped against the original as SHOWN: the field is
        # space-padded on disk, so a row nobody touched would otherwise read
        # as renamed the moment its padding was stripped.
        typed = item.text().strip()
        row["new_name"] = typed if typed and typed != original.strip() else ""
        # Remembered separately from new_name: a bulk apply must not
        # overwrite a row the user typed, and it also must not treat a row
        # IT filled as hand-typed on the next apply.
        row["hand_typed"] = bool(row["new_name"])
        self._refresh_warnings()

    def _refresh_warnings(self) -> None:
        # Signals OFF for the duration. setToolTip()/setFont() are item data
        # changes like any other, so QTableWidget emits itemChanged for each
        # one -- straight back into _on_edited, which calls this again. The
        # first version of this hung the dialog on the first keystroke.
        self._table.blockSignals(True)
        try:
            self._paint_warnings()
        finally:
            self._table.blockSignals(False)

    def _paint_warnings(self) -> None:
        final = {i: (r["new_name"] or r["name"]).strip()
                 for i, r in enumerate(self._rows)}
        counts: dict[str, int] = {}
        for n in final.values():
            counts[n] = counts.get(n, 0) + 1
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 2)
            if item is None:
                continue
            name = final[r]
            trouble = []
            if len(name) > MAX_NAME:
                trouble.append(f"{len(name)} characters — the field holds {MAX_NAME}")
            if counts.get(name, 0) > 1:
                trouble.append("another sample has this name — it will be numbered apart")
            item.setToolTip("; ".join(trouble) if trouble else name)
            font = item.font()
            font.setItalic(bool(trouble))
            item.setFont(font)

    def accept(self) -> None:      # noqa: N802  (Qt casing)
        """Confirm before renaming a sample another preset also uses.

        ONE prompt for the whole dialog, not one per row. Sharing is the norm
        rather than the exception -- measured at 57 of one preset's 77 samples
        also belonging to another -- so a per-row confirmation would be 57
        clicks and would train the user to dismiss it.

        There is no "copy instead" here on purpose. A rename is keyed by the
        sample's name, so it follows the sample wherever it is used; giving
        one preset its own differently-named copy means splitting the sample,
        which duplicates its audio. On the pair measured above that is +22.4 MB
        against a 24.2 MB bank and a 32 MB sampler-RAM limit. Worth building
        deliberately if it is wanted, not worth implying with a greyed button.
        """
        affected = [r for r in self._rows if r["new_name"] and r.get("shared_with")]
        if affected:
            lines = [f"• {r['name'].strip()} → {r['new_name']}"
                     f"   (also in {', '.join(r['shared_with'])})"
                     for r in affected[:8]]
            more = (f"\n…and {len(affected) - 8} more"
                    if len(affected) > 8 else "")
            answer = QMessageBox.question(
                self, "Samples used by other presets",
                f"{len(affected)} of the samples you renamed are also used by "
                f"other presets staged in this bank.\n\n"
                f"A rename follows the sample, so those presets will show the "
                f"new names too — the audio is shared, and only one copy of it "
                f"goes into the bank.\n\n" + "\n".join(lines) + more +
                f"\n\nRename them anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return                 # back to the table, edits intact
        super().accept()

    def renames(self) -> dict:
        """{original name: new name}, only for rows actually changed.

        Only changed rows, so an untouched dialog assembles a byte-identical
        bank -- and so a name that merely LOOKS like the scheme's output is
        not frozen in as a deliberate choice.
        """
        return {r["name"]: r["new_name"] for r in self._rows if r["new_name"]}

    @staticmethod
    def get_renames(rows: list, fmt: str = "E4B", octave_offset: int = 2,
                     existing: Optional[dict] = None,
                     parent=None) -> Optional[dict]:
        """None if cancelled, otherwise the (possibly empty) rename map."""
        dialog = SampleRenameDialog(rows, fmt=fmt, octave_offset=octave_offset,
                                     existing=existing, parent=parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.renames()
