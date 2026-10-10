"""A stdout one THREAD can capture, without taking it from the others.

WHY THIS EXISTS. mpc2emu narrates its work to stdout, and three modules
here read that narration back -- ``_run_captured`` in build/convert.py and
in build/images.py, and ``calllog.traced``. All three used
``contextlib.redirect_stdout``, which is a PROCESS-GLOBAL mutation: its
``__enter__`` assigns ``sys.stdout`` and its ``__exit__`` restores whatever
``sys.stdout`` happened to be when THAT thread entered.

Conversions run on ``QThreadPool.globalInstance()``, whose ``maxThreadCount``
is 12 on a normal machine and which demonstrably starts a second worker while
the first is still blocked. So two overlapping captures interleave those two
assignments and the last one to LEAVE wins: ``sys.stdout`` ends up permanently
bound to a StringIO nobody reads again, and every later ``print()`` in the
process is silent for the rest of the session. Measured (code review
2026-10-09, M15) with two threads, A leaving before B:

    B-PROGRESS-LINE            <- the only survivor; it went into A's buffer
    (nothing further prints at all)

The captured progress is also recorded against the WRONG conversion, which is
the entire reason build/calllog.py exists.

So the global is not touched again. A proxy is installed ONCE, at import, and
it reads a ``threading.local()``: a thread that has installed a buffer sends
its ``write()`` there, and every other thread falls through to the real
stdout. A capture then consists of one attribute assignment on the capturing
thread's own local -- nothing any other thread can observe, and nothing to
restore, so there is no ordering to get wrong.

``__getattr__`` delegates everything else (``fileno``, ``isatty``,
``encoding``, ``errors``, ``name``, ``mode`` ...) to the real stdout, so the
proxy is invisible to code that asks. It is installed only when it is not
already in place, and only from module import, so the single global write
happens before any capture can exist rather than racing one.
"""

from __future__ import annotations

import contextlib
import io
import sys
import threading
from typing import IO, Iterator, Optional  # noqa: UP035

_lock = threading.Lock()
_local = threading.local()

#: The stdout this process started with, and the proxy standing in for it.
#: Both are module globals on purpose: there is exactly one of each for the
#: life of the process, and `install()` holds `_lock` while it publishes them.
_real: Optional[IO] = None  # noqa: UP045
_proxy: Optional[_ThreadLocalStdout] = None  # noqa: UP045


class _Sink:
    """Nowhere, but not an error. A GUI-launched process can have no stdout
    at all, and a library that then crashes on ``print()`` has traded a log
    line for a dead application."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None


class _ThreadLocalStdout:
    """Stands in for ``sys.stdout`` and routes ``write()`` per thread.

    The whole point is what this object does NOT do: it is never assigned per
    capture. Its identity is fixed at import, so a capture cannot interleave
    with another capture's cleanup.
    """

    def write(self, text: str) -> int:
        buf = getattr(_local, "buffer", None)
        if buf is not None:
            return buf.write(text)
        return _real.write(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        buf = getattr(_local, "buffer", None)
        if buf is not None:
            buf.flush()
        else:
            _real.flush()

    def __getattr__(self, name: str):
        # Only reached for what the three methods above do not define, so
        # `fileno`, `isatty`, `encoding`, `errors`, `name`, `mode` and
        # anything a library reaches for further down all come from the
        # stdout this process actually has.
        return getattr(_real, name)


def install() -> None:
    """Put the proxy in place of ``sys.stdout``. Once, and idempotently.

    Called from module import, before any capture exists, which is what makes
    the single global write safe: nothing is being redirected yet, so it
    cannot interleave with a capture's cleanup. Every later call is a no-op --
    a stdout someone else put there deliberately (a test's
    ``redirect_stdout``, a GUI console) is left exactly as they left it.
    """
    global _real, _proxy
    with _lock:
        if _proxy is not None:
            return
        _real = sys.stdout if sys.stdout is not None else _Sink()
        _proxy = _ThreadLocalStdout()
        sys.stdout = _proxy


install()


@contextlib.contextmanager
def capture() -> Iterator[io.StringIO]:
    """Send THIS thread's stdout to a fresh buffer for the duration.

        with captured_stdout.capture() as buf:
            result = fn(*args, **kwargs)

    Yields the buffer, so the caller reads the narration back the same way it
    did from ``redirect_stdout``'s own ``as`` target. Nothing global is
    written: the buffer lives on this thread's local and is restored to
    whatever it was (usually ``None``) on the way out, so nesting works and no
    other thread can observe the change.

    When the proxy is not in place -- a caller replaced ``sys.stdout``
    outright rather than redirecting it -- the capture still happens, but into
    a buffer the callee's output will not reach. That is the honest outcome:
    whoever owns ``sys.stdout`` owns where the text goes.
    """
    buf = io.StringIO()
    previous = getattr(_local, "buffer", None)
    _local.buffer = buf
    try:
        yield buf
    finally:
        _local.buffer = previous
