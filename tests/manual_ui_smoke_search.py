"""Manual smoke test for M4: background scan + search box, driven live."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from vinsamlib import mpc2emu_bridge
from vinsamlib.config import Config
from vinsamlib.ui.main_window import MainWindow

from _qtest_shim import qwait

OUT = Path(tempfile.gettempdir()) / "vinsamlib_manual_smoke"
OUT.mkdir(parents=True, exist_ok=True)


def grab(win, name):
    win.grab().save(str(OUT / name))
    print("saved", name)


def main():
    config = Config.load()
    mpc2emu_bridge.install(config)
    config.library_roots = [Path.home() / "disk-image" / "Monotanz-144" / "Woodwind"]

    # fresh index each run, so this test always exercises a real scan
    import os
    idx_path = Path.home() / ".local" / "share" / "vinsamlib" / "index.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(idx_path) + suffix)
        if p.exists():
            os.remove(p)

    app = QApplication(sys.argv)
    win = MainWindow(config)
    win.resize(1280, 800)
    win.show()
    qwait(300)
    grab(win, "search_01_scanning.png")

    # wait for the background scan to finish
    for _ in range(100):
        qwait(200)
        if win._scan_worker is None:
            break
    print("status bar:", win.statusBar().currentMessage())
    grab(win, "search_02_scanned.png")

    box = win._explorer._search_box
    box.setText("bass")
    qwait(600)   # debounce + query
    grab(win, "search_03_results.png")
    print("result count:", win._explorer._results.count())

    # select the first real result
    results = win._explorer._results
    if results.count() > 0 and results.item(0).flags() != 0:
        results.setCurrentRow(0)
        qwait(1000)
        grab(win, "search_04_result_selected.png")

    # clear search -> tree should reappear
    box.setText("")
    qwait(300)
    grab(win, "search_05_cleared.png")
    print("done")


if __name__ == "__main__":
    try:
        main()
    finally:
        # Drain the shared thread pool before the interpreter starts
        # tearing down Qt objects: under PySide6/Shiboken, a worker
        # still mid-flight at that point raises a hard "Signal source
        # has been deleted" RuntimeError from its own background
        # thread when it emits (PyQt5 tolerated the same race
        # silently) -- this is what closeEvent() also does for a real
        # run, but this script never gets that far.
        QThreadPool.globalInstance().waitForDone(5000)
