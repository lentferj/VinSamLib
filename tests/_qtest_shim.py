"""
Stand-in for `QTest.qWait()` in these manual smoke tests.

`QTest.qWait()` itself crashes (segfault) under PySide6 6.11.1 specifically
when a QThreadPool worker thread delivers a cross-thread queued signal while
it's pumping — confirmed with a minimal repro outside this codebase; a real
`app.exec()` loop and a plain `app.processEvents()` loop both deliver the
same signal correctly, so this is a QTest-under-PySide6 issue, not a bug in
vinsamlib's own Worker/signal plumbing (which is exactly what these scripts
exist to exercise). The real app always runs via `app.exec()` and is
unaffected; only this qWait-based test-driving technique needed a
workaround.
"""
from __future__ import annotations

import time

from PySide6.QtWidgets import QApplication


def qwait(ms: int) -> None:
    deadline = time.monotonic() + ms / 1000.0
    app = QApplication.instance()
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
