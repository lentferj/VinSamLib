"""Temporary directories that actually go away again.

Almost every write path here has to stage a real file on disk before it can
hand it anywhere: mpc2emu's parsers and writers take paths, not buffers, so
converting a preset, assembling a pending queue, or previewing a bank's
samples all mean "write it somewhere first". Each of those used to call
`tempfile.mkdtemp()` and never look at it again.

That is a **leak of one whole bank per operation**. Converting fifty banks in
a session left fifty bank-sized directories under /tmp until the machine was
rebooted, and a session of automated testing filled a 4.7 GB /tmp partition
outright — which is how it was noticed at all, rather than by anyone
watching disk usage.

Three lifetimes, because the paths genuinely have three:

- **Immediate** — the file exists only to be read straight back (a sample
  preview, or an intermediate handed to a parser). `temp_dir()` is a context
  manager; the directory goes at the end of the block.
- **Session** — the file IS the result and a caller holds the path (a
  converted bank on its way to New Bank, an assembled queue on its way to an
  image builder). Nothing here can know when the last reader is done, so
  `session_temp_dir()` registers it and `cleanup_session()` removes the lot
  at shutdown.
- **Previous runs** — a crash, a kill, or a debugger leaves session
  directories behind with nobody to remove them. `reap_stale()` clears those
  on startup, by age, so yesterday's crash does not accumulate forever.

Deliberately not `TemporaryDirectory` with a finalizer for the session case:
those fire on garbage collection, and the whole point is that the caller
still holds the path.
"""

from __future__ import annotations

import atexit
import contextlib
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterator  # noqa: UP035

#: Every prefix this project stages under. Listed in one place so
#: reap_stale() cannot miss one that some module invented on its own.
#:
#: `vinsamlib_tal_` was missing until 2026-10-05: foreign_import.py's TAL
#: staging directory used a prefix nothing here knew about, so a crash
#: during a TAL import left it in /tmp forever -- not even the age sweep
#: could reach it. A prefix that is not in this tuple is a permanent leak,
#: which is the whole reason the tuple is exhaustive rather than sampled.
PREFIXES = (
    "vinsamlib_convert_",
    "vinsamlib_pending_",
    "vinsamlib_rebuild_",
    "vinsamlib_samples_",
    "vinsamlib_preview_",
    "vinsamlib_audition_",
    "vinsamlib_tal_",
    # A single FILE, not a directory: audition_player.py hands a rendered WAV
    # to an external player. Noted because the sweeper below has to treat
    # files as well as directories to reach it, which it did not until this
    # was found -- an external-player audition leaked on every run, not only
    # on a crash.
    "vinsamlib-audition-",
)

#: Session directories awaiting cleanup_session().
_session: list[Path] = []

#: How old a leftover has to be before reap_stale() will remove it. Hours,
#: not minutes: a second VinSamLib running alongside this one is using its
#: own fresh directories, and deleting a sibling's staging area mid-build
#: would be a far worse bug than the leak this fixes.
STALE_AFTER_SECONDS = 12 * 3600


def session_temp_dir(prefix: str) -> Path:
    """A staging directory that lives until the application exits.

    For a path handed onward to a caller that will read it later. Registered
    for `cleanup_session()`.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix))
    _session.append(path)
    return path


@contextlib.contextmanager
def temp_dir(prefix: str) -> Iterator[Path]:
    """A staging directory for the duration of a `with` block."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def forget(path: str | Path) -> None:
    """Remove one session directory early, once its consumer is finished.

    Worth doing wherever the last read is known: it keeps a long session
    from holding every bank it ever converted."""
    p = Path(path)
    target = p if p.is_dir() else p.parent
    shutil.rmtree(target, ignore_errors=True)
    for i, known in enumerate(list(_session)):
        if known == target:
            _session.pop(i)
            break


def cleanup_session() -> int:
    """Remove everything `session_temp_dir()` handed out. Returns the count."""
    n = 0
    for path in _session:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            n += 1
    _session.clear()
    return n


#: Also run at interpreter exit, so a *script* using this library cleans up
#: too -- not only the GUI, which additionally wires cleanup_session() to
#: QApplication.aboutToQuit for the ordinary quit path. Without this, every
#: test run and every one-off script left its staging behind until the next
#: reap_stale(), which is how 60 directories survived a single suite run.
atexit.register(lambda: cleanup_session())


def cleanup_all_on_exit() -> None:
    """Our own staging, then everything left by EARLIER runs.

    Wired to `aboutToQuit` in place of a bare `cleanup_session()`, because
    sweeping at startup alone is not enough. Measured 2026-10-05: a session
    killed outright leaves every directory it staged behind, `reap_stale()`
    only runs when the application NEXT starts, and it refuses anything
    younger than `STALE_AFTER_SECONDS` so it cannot touch a concurrently
    running sibling. A kill at hour 11 therefore sat untouched until the
    launch after hour 12 -- and one library of auditions left ~200
    directories of 2-34 MB behind, several GB.

    Order matters and is the reason this is one function: cleanup_session()
    first, so our own fresh directories are gone by name rather than by age,
    and reap_stale() second for the older debris.

    The 12-hour threshold is deliberately NOT shortened here. It is what
    keeps a second VinSamLib's staging area from being deleted mid-build,
    and this project has already traded a rare disk leak for a real
    data-loss bug once (see CLAUDE.md). Collecting yesterday's leftovers as
    soon as this session ends is the safe half of that trade.
    """
    cleanup_session()
    reap_stale()


def reap_stale(older_than: float = STALE_AFTER_SECONDS) -> int:
    """Remove staging left behind by earlier runs -- directories AND files.

    Only by age, and only under this project's own prefixes -- a crash
    leaves these with nobody to clean them, but a *concurrently running*
    VinSamLib owns fresh ones and must not have them pulled out from under
    it. Returns the count removed.

    FILES AS WELL AS DIRECTORIES. This skipped anything that was not a
    directory, which is what made the external-player WAV a permanent leak:
    it is `mkstemp`ed rather than staged in a directory, so no age sweep
    could ever reach it, and a leaked one survived even when backdated well
    past the threshold. Verified before the fix: one such file, aged two
    days, `reap_stale()` returned 0 and the file was still there.

    The two shapes need different removals and that is the only difference
    between them -- same age test, same prefix test, same reason for having
    either. `ignore_errors` throughout, because an entry that vanished
    between `iterdir()` and the removal is not a failure to report.
    """
    cutoff = time.time() - older_than
    n = 0
    root = Path(tempfile.gettempdir())
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(PREFIXES):
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue
        n += 1
    return n
