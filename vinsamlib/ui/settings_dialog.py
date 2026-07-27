"""
Settings dialog: lets the user point VinSamLib at a different mpc2emu
checkout and see, live, whether it's usable -- both "found at all" and
"has the specific modules the vintage resample/reduce feature needs".

Changing the path here does not hot-reload mpc2emu_bridge's cached
sys.path/sys.modules state (the bridge installs itself once, and Python's
own module cache would still hold whichever mpc2emu modules were already
imported from the old location) -- so a changed path just tells the user
to restart, rather than pretending to apply it live.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QVBoxLayout)

from ..config import Config


class SettingsDialog(QDialog):
    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self._config = config
        self._changed_path: Path | None = None
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("mpc2emu checkout:"))
        path_row = QHBoxLayout()
        self._path_edit = QLineEdit(str(config.mpc2emu_path))
        self._path_edit.textChanged.connect(self._update_status)
        path_row.addWidget(self._path_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._restart_label = QLabel("")
        self._restart_label.setStyleSheet("color: palette(mid); font-size: 11px;")
        self._restart_label.setWordWrap(True)
        layout.addWidget(self._restart_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_status(str(config.mpc2emu_path))

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "mpc2emu Checkout", self._path_edit.text(),
            options=QFileDialog.Option.DontUseNativeDialog)
        if path:
            self._path_edit.setText(path)

    def _update_status(self, text: str) -> None:
        probe = Config(mpc2emu_path=Path(text) if text else Path())
        ok, reason = probe.check_mpc2emu_path()
        if ok:
            conv_ok, conv_reason = probe.check_conversion_support()
            if conv_ok:
                self._status_label.setText(f"✓ {reason} — {conv_reason}")
            else:
                self._status_label.setText(f"✓ {reason}\n✗ {conv_reason}")
        else:
            self._status_label.setText(f"✗ {reason}")
        changed = Path(text) != self._config.mpc2emu_path if text else False
        self._restart_label.setText(
            "Restart VinSamLib to apply the new path." if changed else "")

    def accept(self) -> None:
        new_path = Path(self._path_edit.text())
        if new_path != self._config.mpc2emu_path:
            self._config.mpc2emu_path = new_path
            self._config.save()
            self._changed_path = new_path
        super().accept()

    @property
    def path_changed(self) -> bool:
        return self._changed_path is not None
