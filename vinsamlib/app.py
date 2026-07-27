"""Entry point: python -m vinsamlib.app"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from . import mpc2emu_bridge
from .config import Config
from .ui.main_window import MainWindow


def main() -> int:
    config = Config.load()
    # Fail fast, with a clear message, rather than on the first tree expand —
    # every bank/image operation needs mpc2emu importable.
    mpc2emu_bridge.install(config)

    app = QApplication(sys.argv)
    app.setApplicationName("VinSamLib")
    window = MainWindow(config)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
