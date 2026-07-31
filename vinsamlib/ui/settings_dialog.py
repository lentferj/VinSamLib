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

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
                             QVBoxLayout)

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
        self._restart_label.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        self._restart_label.setWordWrap(True)
        layout.addWidget(self._restart_label)

        layout.addSpacing(12)
        layout.addWidget(QLabel("New Bank size-warning thresholds:"))
        limits_form = QFormLayout()
        self._e4b_limit_spin = QSpinBox()
        self._e4b_limit_spin.setRange(1, 128)
        self._e4b_limit_spin.setSuffix(" MB")
        self._e4b_limit_spin.setValue(config.e4b_bank_limit_mb)
        limits_form.addRow("E4XT (E4B):", self._e4b_limit_spin)
        self._krz_limit_spin = QSpinBox()
        self._krz_limit_spin.setRange(1, 128)
        self._krz_limit_spin.setSuffix(" MB")
        self._krz_limit_spin.setValue(config.krz_bank_limit_mb)
        limits_form.addRow("K2000 (KRZ):", self._krz_limit_spin)
        layout.addLayout(limits_form)
        limits_hint = QLabel(
            "A soft warning in New Bank once a bank exceeds this size — the "
            "most common real RAM configuration, not the format's absolute "
            "technical maximum (128 MB for E4B; the K2000 has no hard byte "
            "ceiling). \"Keep Anyway\" is still offered if you actually have "
            "more RAM installed.")
        limits_hint.setStyleSheet("color: palette(placeholdertext); font-size: 11px;")
        limits_hint.setWordWrap(True)
        layout.addWidget(limits_hint)

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
            trim_ok, trim_reason = probe.check_trim_support()
            if conv_ok:
                text = f"✓ {reason} — {conv_reason}"
            else:
                text = f"✓ {reason}\n✗ {conv_reason}"
            # Only worth a line when it's MISSING: trim is an extra on top of
            # resample/reduce, so "available" is the unremarkable case and
            # saying so twice just crowds the status area.
            if not trim_ok:
                text += f"\n✗ {trim_reason}"
            self._status_label.setText(text)
        else:
            self._status_label.setText(f"✗ {reason}")
        changed = Path(text) != self._config.mpc2emu_path if text else False
        self._restart_label.setText(
            "Restart VinSamLib to apply the new path." if changed else "")

    def accept(self) -> None:
        new_path = Path(self._path_edit.text())
        path_changed = new_path != self._config.mpc2emu_path
        limits_changed = (self._e4b_limit_spin.value() != self._config.e4b_bank_limit_mb
                           or self._krz_limit_spin.value() != self._config.krz_bank_limit_mb)
        if path_changed:
            self._config.mpc2emu_path = new_path
            self._changed_path = new_path
        if limits_changed:
            self._config.e4b_bank_limit_mb = self._e4b_limit_spin.value()
            self._config.krz_bank_limit_mb = self._krz_limit_spin.value()
        if path_changed or limits_changed:
            self._config.save()
        super().accept()

    @property
    def path_changed(self) -> bool:
        return self._changed_path is not None
