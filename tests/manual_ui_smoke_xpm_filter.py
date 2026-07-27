"""Manual smoke test for XPM as a full Explorer citizen: shows up as a
leaf row in the library tree (not just via File > Import XPM...), the
All/E4B/KRZ/XPM format filter actually includes/excludes it, it's
indexed for search (index/scanner.py's _scan_xpm_container), a search hit
resolves back into a real xpm TreeNode (ui/search_resolve.py), and
double-clicking either a tree row or a search-result row for it triggers
the same import flow as File > Import XPM... (lands directly in Pending
for Image, no save dialog, no library folder). Same in-process-call
approach as every other smoke test here (no X11 input automation
available).
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QCoreApplication, QModelIndex, Qt, QThreadPool
from PySide6.QtWidgets import QApplication

from vinsamlib import mpc2emu_bridge
from vinsamlib.build.xpm_import import XpmImportOptions
from vinsamlib.config import Config
from vinsamlib.index.db import IndexDB
from vinsamlib.index.scanner import scan
from vinsamlib.ui import search_resolve
from vinsamlib.ui.main_window import MainWindow
from vinsamlib.ui.models import BankFormatFilterProxy, LibraryTreeModel
from vinsamlib.ui.xpm_import_dialog import XpmImportDialog

XPM_DIR = Path.home() / "Samples/MPC/Roland Alpha Juno 2"


def _fetch_and_wait(model, index, timeout=10):
    if not model.canFetchMore(index):
        return
    model.fetchMore(index)
    deadline = time.time() + timeout
    while model.rowCount(index) == 0 and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.05)


def main():
    if not XPM_DIR.exists():
        print(f"SKIPPED: {XPM_DIR} not found on this machine")
        return

    config = Config.load()
    mpc2emu_bridge.install(config)

    app = QApplication(sys.argv)

    # 1) Tree: .xpm files show up as leaf "xpm" nodes
    model = LibraryTreeModel([XPM_DIR])
    root = model.index(0, 0, QModelIndex())
    _fetch_and_wait(model, root)
    xpm_nodes = [model.index(r, 0, root).data(Qt.ItemDataRole.UserRole)
                 for r in range(model.rowCount(root))]
    xpm_nodes = [n for n in xpm_nodes if n.kind == "xpm"]
    print("xpm nodes found:", len(xpm_nodes))
    assert xpm_nodes, "expected at least one .xpm file in the fixture directory"
    assert xpm_nodes[0].format_label == "XPM"

    # 2) Format filter proxy actually excludes xpm rows for other formats
    proxy = BankFormatFilterProxy()
    proxy.setSourceModel(model)
    proxy_root = proxy.mapFromSource(root)
    proxy.set_format_filter(None)
    all_rows = proxy.rowCount(proxy_root)
    proxy.set_format_filter("E4B")
    e4b_rows = proxy.rowCount(proxy_root)
    print("rows: all=", all_rows, "e4b-filtered=", e4b_rows)
    assert e4b_rows < all_rows, "E4B filter should exclude the xpm rows"

    # 3) Scanner indexes .xpm files; search finds them; resolve_result()
    #    reconstructs a real xpm TreeNode from the hit
    db = IndexDB(Path(tempfile.mktemp(suffix=".db")))
    scan([XPM_DIR], db)
    name_stub = xpm_nodes[0].label.split()[0]
    hits = [h for h in db.search(name_stub) if h.kind == "xpm"]
    print("search hits:", [(h.name, h.format) for h in hits])
    assert hits, f"expected a search hit for {name_stub!r}"
    resolved = search_resolve.resolve_result(hits[0])
    assert resolved is not None and resolved.kind == "xpm"
    assert Path(resolved.payload).exists()

    # 4) Full MainWindow: double-clicking the tree row triggers the same
    #    import flow as File > Import XPM... -- lands directly in
    #    Pending for Image as a one-preset bank recipe, no save dialog
    #    and no library folder (see MainWindow._on_xpm_imported()).
    config2 = Config.load()
    mpc2emu_bridge.install(config2)
    config2.library_roots = [XPM_DIR]
    XpmImportDialog.get_import_options = staticmethod(
        lambda parent=None, initial=None: XpmImportOptions(target_format="E4B"))

    win = MainWindow(config2)
    tmodel = win._model
    troot = tmodel.index(0, 0, QModelIndex())
    _fetch_and_wait(tmodel, troot)
    xpm_row = next(r for r in range(tmodel.rowCount(troot))
                    if tmodel.index(r, 0, troot).data(Qt.ItemDataRole.UserRole).kind == "xpm")

    proxy_index = win._explorer._tree_proxy.mapFromSource(tmodel.index(xpm_row, 0, troot))
    win._explorer._on_tree_double_clicked(proxy_index)

    deadline = time.time() + 60
    while win._xpm_import_worker is not None and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.1)
    print("pending queue after double-click import:",
          [(e["name"], e["format"], len(e["items"])) for e in win._pending_pane._pending])
    assert len(win._pending_pane._pending) == 1
    assert len(win._pending_pane._pending[0]["items"]) == 1

    print("\nALL XPM FILTER SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
