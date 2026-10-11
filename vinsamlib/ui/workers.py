"""
Shared background-work idiom for the whole UI. A Worker's signal source
(WorkerSignals) is owned by the receiver pane (passed as Qt parent), while
the runnable (Worker) is owned by the QThreadPool (autoDelete=True). This
removes the old manual `self._live_workers` lifetime dance: the pane keeps
the QObject alive until torn down, and the pool drops the QRunnable after
run() delivers its signal. The shiboken6.isValid() guard stays, because a
pane can still be collected between `run()` finishing and `.emit()` running.
"""

from __future__ import annotations  # noqa: I001

import re
import shiboken6
import traceback
from typing import Any, Callable  # noqa: UP035

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

# The last line of a traceback is "some.module.SomeError: the real message",
# and only the second half means anything to a user. Matches a dotted
# identifier and nothing else before the colon, so a message that merely
# contains one ("Foo.xpj is an MPC project...") is left alone.
_EXC_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*: ")


def last_error_line(message: str) -> str:
    """The human-readable tail of a Worker's error payload (a formatted
    traceback), for status bars, tooltips and message boxes."""
    line = message.strip().splitlines()[-1] if message and message.strip() else "error"
    return _EXC_PREFIX.sub("", line, count=1).strip() or line


class WorkerSignals(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    finished = Signal(object)  # the callable's return value
    error = Signal(str)


class Worker(QRunnable):
    """Runs `fn(*args, **kwargs)` on a thread-pool thread.

    Usage — connect the signals *before* starting, never after:

        worker = Worker(fn, arg1, arg2)
        worker.signals.finished.connect(self._on_done)
        worker.signals.error.connect(self._on_error)
        run(worker)

    Starting the pool task before the caller connects its slots would race:
    the background thread could call `.emit()` before `.connect()` has run,
    and a Qt signal emitted with no listeners yet attached is just dropped,
    not queued — so there's no `submit(fn, ...)` one-liner here that starts
    immediately; `run()` below only starts a `Worker` you built and wired
    yourself.
    """

    def __init__(
        self, fn: Callable[..., Any], *args, parent: QObject | None = None, **kwargs
    ):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        # The receiver pane (the parent QObject) owns the signal source; the
        # pool owns the runnable. The parent keeps the C++ side alive until
        # the pane is torn down, and setAutoDelete(True) lets Qt clean up the
        # QRunnable once run() finishes. See docstring above for the contract.
        self.signals = WorkerSignals(parent)
        # Pool owns the runnable; parent owns the signal source.
        # No manual lifetime management needed — the pane's parent keeps the
        # QObject alive and the pool drops the QRunnable after delivery.

    def run(self) -> None:
        try:
            payload, signal_name = self._fn(*self._args, **self._kwargs), "finished"
        except Exception:  # noqa: BLE001
            payload, signal_name = traceback.format_exc(), "error"

        # Deliver only if there is still someone to deliver to.
        #
        # MainWindow.closeEvent waits a BOUNDED three seconds for the pool to
        # drain, on purpose, so quitting never hangs on a slow scan. A worker
        # that outruns that bound — parsing a 939 MB GigaSampler file takes
        # three seconds on its own — comes back to a widget tree that is
        # already being torn down: its WorkerSignals has been collected with
        # the pane that owned it, and emitting into the deleted C++ object
        # raises "Signal source has been deleted" from the pool thread, where
        # nothing can catch it. Seen at exit after closing the window mid-scan.
        #
        # isValid() is the precise question — "is the C++ side still there" —
        # and it is asked before touching `self.signals` at all, since the
        # attribute access is itself what raises. The except is a backstop for
        # the object dying in the gap between the two.
        if not shiboken6.isValid(self.signals):
            return
        try:
            getattr(self.signals, signal_name).emit(payload)
        except RuntimeError:
            pass  # torn down mid-emit; the result has no receiver


def run(worker: Worker) -> None:
    """Start an already-wired Worker on the shared global thread pool."""
    QThreadPool.globalInstance().start(worker)
