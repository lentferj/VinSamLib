"""Entry point: python -m vinsamlib.app"""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication

from . import mpc2emu_bridge, tempdirs
from . import config as config_mod
from .config import Config
from .ui.main_window import MainWindow


def main() -> int:
    # THE ONE PLACE THAT SAYS "I MEAN THE USER'S OWN DATA". Everything else
    # -- a test, a script, a tool -- has to point XDG_DATA_HOME somewhere
    # disposable or opt in explicitly. See config.require_real_state_opt_in.
    os.environ.setdefault(config_mod.REAL_STATE_ENV, "1")
    config = Config.load()
    # Fail fast, with a clear message, rather than on the first tree expand —
    # every bank/image operation needs mpc2emu importable.
    mpc2emu_bridge.install(config)

    # Anything a previous run left behind: a crash or a kill has no chance to
    # clean up after itself, and these are bank-sized. Only leftovers older
    # than half a day go, so a second VinSamLib running right now keeps its
    # own staging area.
    tempdirs.reap_stale()

    app = QApplication(sys.argv)
    app.setApplicationName("VinSamLib")
    # Every conversion, every assembled queue and every image rebuild stages
    # a real file on disk, because mpc2emu's parsers and writers take paths
    # rather than buffers. Those are the results callers hold, so they cannot
    # be freed as they are made -- they are freed here. Before this existed,
    # a session that converted fifty banks left fifty bank-sized directories
    # behind until the machine was rebooted.
    app.aboutToQuit.connect(tempdirs.cleanup_session)
    window = MainWindow(config)
    window.show()
    # AFTER show(): the recovery question is a modal dialog, and asking it
    # before there is a window behind it gives the user a prompt floating
    # over nothing, about work they cannot see the absence of.
    window.start_background_work()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
