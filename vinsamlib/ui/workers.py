"""
Shared background-work idiom for the whole UI: anything that touches disk or
parses a bank (opening a directory/image, parsing an E4B/KRZ file, building a
summary) runs off the GUI thread through one shared thread pool, and reports
back via a signal — the standard `QRunnable` + `QObject`-signals-bridge
pattern, needed because `QRunnable` itself cannot emit signals.
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class WorkerSignals(QObject):
    finished = Signal(object)   # the callable's return value
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

    def __init__(self, fn: Callable[..., Any], *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = WorkerSignals()
        # Qt's thread pool auto-deletes a QRunnable's C++ side as soon as
        # run() returns (QRunnable.autoDelete() defaults to True). Every
        # caller in this codebase already manages a Worker's lifetime itself
        # (a `self._live_workers` list, or a plain `self._scan_worker`
        # attribute) specifically so it survives long enough to deliver its
        # signal — under PySide6/Shiboken that race turns into a hard
        # "Signal source has been deleted" RuntimeError from run() itself
        # (not reliably hit under PyQt5's looser ownership handling), so
        # ownership needs to be Python's alone, never Qt's.
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        else:
            self.signals.finished.emit(result)


def run(worker: Worker) -> None:
    """Start an already-wired Worker on the shared global thread pool."""
    QThreadPool.globalInstance().start(worker)
