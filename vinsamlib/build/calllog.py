"""A record of every mpc2emu call, for when a result needs explaining.

WHY THIS EXISTS. A converted bank carries no account of what produced it.
That is fine until someone asks, and then it is not recoverable by any
amount of thinking: on 2026-09-13 an AKAI volume turned up with every
loop stripped off a sustained pad and one sample 77 % shorter, and
neither project could say which options had done it. mpc2emu could not
reproduce the result from the source, and this side had kept nothing --
our project file stores conversion options per PENDING bank, and an
import into New Bank carries them nowhere at all.

So this is deliberately a log of CALLS, not of settings. A settings dump
answers "what did the dialog say", which is already the easy half; this
answers "what was actually run, in what order, against what, and what
did it print", which is the half that was missing. mpc2emu's processors
narrate their work to stdout and build/convert.py's _run_captured reads
that buffer only inside `except` -- so on every SUCCESSFUL conversion,
the running commentary explaining what was thrown away was discarded
unread. That commentary is the most valuable thing in here.

OFF BY DEFAULT, and off has to be genuinely free: `enabled` is checked
before anything is formatted, so a disabled log costs one attribute read
per mpc2emu call and allocates nothing.

NOTHING IN HERE IS AUDIO. Values are summarised to a short line each (a
bank renders as `<E4BFile path='x.e4b' presets=12 samples=130>`), both
because megabytes of PCM in a log helps nobody and because the log
travels inside the project file, which the user may well hand to someone
else.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

#: Where the log lands inside a .vslproj. Under a directory of its own so
#: that a reader scanning the archive can tell at a glance that it is
#: diagnostic material and not part of the staged work.
ARCHIVE_NAME = "debug/mpc2emu-calls.jsonl"

#: Caps, so that leaving the switch on through an overnight batch cannot
#: quietly turn a project file into a gigabyte. Both are generous for the
#: thing this is for -- one conversion is a handful of calls -- and the log
#: says so in-band when it hits either, rather than silently ending.
MAX_RECORDS = 20_000
MAX_OUTPUT_CHARS = 8_000

#: Where the log survives a restart. Beside the index and the crash file,
#: for the same reason: it is this program's own state, not the user's
#: document, and it must never land in a library folder and get indexed.
SPOOL_NAME = "mpc2emu-calls.jsonl"

#: Trim the spool to this many of its most recent lines when it outgrows
#: MAX_RECORDS. Reading and rewriting it is only done at startup and when a
#: project is saved, so a straightforward rewrite is affordable.
_TRIM_TO = MAX_RECORDS // 2

_lock = threading.Lock()
_records: list[dict] = []
_enabled = False
_truncated = False
_spool: Optional[Path] = None


def spool_path() -> Optional[Path]:
    """The on-disk log, or None if recording is off and none was opened."""
    return _spool


def set_enabled(on: bool) -> None:
    """Turn recording on or off. Turning it ON does not clear what is
    already held: someone switching it on mid-session wants the next
    conversion recorded, and someone switching it off and on again has no
    reason to lose the first half.

    ENABLING ALSO OPENS THE ON-DISK SPOOL, and that is not a detail. Held
    only in memory, the log covered one process -- so a user who switched it
    on, converted a volume, restarted, and then saved a project got a file
    with no conversions in it. Which is precisely the case the log exists
    for: the question "what produced this?" is asked LATER, and later is
    usually after a restart. Reported the same evening it shipped.
    """
    global _enabled, _spool
    with _lock:
        _enabled = bool(on)
        if not _enabled:
            return
        if _spool is None:
            try:
                from ..config import user_data_dir
                _spool = Path(user_data_dir()) / SPOOL_NAME
                _spool.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                _spool = None
    _trim_spool()


def is_enabled() -> bool:
    return _enabled


def clear() -> None:
    """Forget everything, in memory AND on disk.

    Both, deliberately: "clear the log" from a user who is about to
    reproduce something means start from nothing, and leaving a spool
    behind would put a previous run's calls in their next report.
    """
    global _truncated
    with _lock:
        _records.clear()
        _truncated = False
        spool = _spool
    if spool is not None:
        try:
            spool.unlink(missing_ok=True)
        except OSError:
            pass


def _trim_spool() -> None:
    """Keep the spool from growing without bound across sessions."""
    with _lock:
        spool = _spool
    if spool is None:
        return
    try:
        if not spool.exists():
            return
        lines = spool.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= MAX_RECORDS:
            return
        keep = lines[-_TRIM_TO:]
        note = json.dumps({"t": time.time(), "kind": "trimmed",
                           "note": f"older lines dropped; kept the most "
                                   f"recent {len(keep)}"})
        spool.write_text("\n".join([note] + keep) + "\n", encoding="utf-8")
    except OSError:
        pass


def count() -> int:
    with _lock:
        return len(_records)


def _brief(value: Any, depth: int = 0) -> Any:
    """One short, JSON-safe rendering of an argument.

    Paths and numbers go through verbatim -- they ARE the provenance.
    Everything large is described rather than dumped.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (str, Path)):
        s = str(value)
        return s if len(s) <= 500 else s[:500] + "…"
    if is_dataclass(value) and not isinstance(value, type):
        # ConversionOptions lands here, and it is the single most useful
        # record in the file: the RESOLVED options after defaults, which is
        # exactly what nobody can reconstruct afterwards.
        try:
            return {k: _brief(v, depth + 1) for k, v in asdict(value).items()}
        except Exception:
            pass
    if isinstance(value, dict):
        if depth >= 2:
            return f"<dict len={len(value)}>"
        return {str(k): _brief(v, depth + 1) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple, set, frozenset)):
        if depth >= 2:
            return f"<{type(value).__name__} len={len(value)}>"
        items = list(value)
        out = [_brief(v, depth + 1) for v in items[:10]]
        if len(items) > 10:
            out.append(f"… and {len(items) - 10} more")
        return out
    # A bank, a preset, a sample: describe it, never dump it.
    name = type(value).__name__
    bits = []
    for attr in ("path", "name", "filename"):
        got = getattr(value, attr, None)
        if isinstance(got, (str, Path)) and str(got):
            bits.append(f"{attr}={str(got)!r}")
            break
    for attr in ("presets", "programs", "samples", "keygroups", "zones"):
        got = getattr(value, attr, None)
        try:
            bits.append(f"{attr}={len(got)}")
        except Exception:
            pass
    return f"<{name} {' '.join(bits)}>".replace("  ", " ") if bits else f"<{name}>"


def _append(record: dict) -> None:
    global _truncated
    with _lock:
        spool = _spool
        if len(_records) < MAX_RECORDS:
            _records.append(record)
        elif not _truncated:
            _truncated = True
            _records.append({"t": time.time(), "kind": "truncated",
                             "note": f"in-memory log stopped at {MAX_RECORDS} "
                                     f"records; the spool continues"})
    # Written as it happens, outside the lock. A call takes seconds and this
    # takes microseconds, and appending per record rather than at exit is
    # what makes the log survive the case it is most needed for: a session
    # that did not get to exit cleanly.
    if spool is not None:
        try:
            with open(spool, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False,
                                    default=str) + "\n")
        except OSError:
            pass


def record_call(fn: Any, args: tuple, kwargs: dict, *,
                ok: bool, seconds: float, output: str = "",
                error: str = "") -> None:
    """One mpc2emu call: what was invoked, with what, and what it printed."""
    if not _enabled:
        return
    module = getattr(fn, "__module__", "") or ""
    qual = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))
    out = output or ""
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + f"\n… {len(output) - MAX_OUTPUT_CHARS} more characters"
    rec = {
        "t": time.time(),
        "kind": "call",
        "fn": f"{module}.{qual}" if module else qual,
        "args": [_brief(a) for a in args],
        "kwargs": {k: _brief(v) for k, v in kwargs.items()},
        "ok": ok,
        "seconds": round(seconds, 4),
    }
    # The captured stdout is the whole point -- it is mpc2emu explaining what
    # it did and what it discarded, and until this log existed it was read
    # only when the call RAISED.
    if out.strip():
        rec["output"] = out
    if error:
        rec["error"] = error[:2000]
    _append(rec)


def note(kind: str, **fields: Any) -> None:
    """Context that is not itself a call -- which source, which options,
    which output path. Cheap enough to leave at every interesting boundary."""
    if not _enabled:
        return
    rec = {"t": time.time(), "kind": kind}
    # An empty field is noise, not information -- a bank parsed out of an
    # XPM has no `path` of its own, and recording `source: ""` invites the
    # reader to conclude something was lost. The preceding parse call
    # carries the real source path in its own arguments.
    rec.update({k: _brief(v) for k, v in fields.items()
                if v is not None and v != ""})
    _append(rec)


def traced(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Record a call that is NOT already behind a _run_captured wrapper.

    The two _run_captured functions cover the conversion side and the image
    builders, but not everything on the path goes through them -- the AKAI
    image writers and the EMU3 appender are called directly, and they are
    where the partition layout, the object budget and the volume naming are
    decided. A log that stopped short of those would answer "how was this
    bank made" and not "how did it get onto the disc".

    Behaviour when DISABLED is bit-for-bit what it was before this existed:
    no capture, no redirection, the callee's exceptions propagate untouched.
    When enabled the callee's stdout is captured for the record and then
    written straight back out, so nothing a user would have seen on the
    console is swallowed by the act of logging it.
    """
    if not _enabled:
        return fn(*args, **kwargs)
    import contextlib
    import io
    buf = io.StringIO()
    started = time.monotonic()
    try:
        with contextlib.redirect_stdout(buf):
            result = fn(*args, **kwargs)
    except Exception as ex:
        record_call(fn, args, kwargs, ok=False,
                    seconds=time.monotonic() - started,
                    output=buf.getvalue(), error=str(ex))
        if buf.getvalue():
            print(buf.getvalue(), end="")
        raise
    record_call(fn, args, kwargs, ok=True,
                seconds=time.monotonic() - started, output=buf.getvalue())
    if buf.getvalue():
        print(buf.getvalue(), end="")
    return result


def as_jsonl() -> bytes:
    """The log as it is stored in a project file. Empty when nothing was
    recorded, so callers can skip writing the member entirely.

    Prefers the SPOOL, so a saved project carries everything recorded since
    the switch was turned on -- not merely since this process started.
    """
    with _lock:
        spool = _spool
        rows = list(_records)
    if spool is not None:
        try:
            if spool.exists():
                data = spool.read_bytes()
                if data.strip():
                    return data
        except OSError:
            pass
    if not rows:
        return b""
    lines = []
    for r in rows:
        try:
            lines.append(json.dumps(r, ensure_ascii=False, default=str))
        except Exception:
            lines.append(json.dumps({"kind": "unserialisable",
                                     "fn": str(r.get("fn", ""))}))
    return ("\n".join(lines) + "\n").encode("utf-8")


def summary() -> Optional[str]:
    """One line for a status bar or a load report, or None if empty."""
    rows = []
    raw = as_jsonl()
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    if not rows:
        return None
    calls = sum(1 for r in rows if r.get("kind") == "call")
    failed = sum(1 for r in rows if r.get("kind") == "call" and not r.get("ok"))
    span = rows[-1]["t"] - rows[0]["t"]
    bit = f"{calls} mpc2emu call(s) over {span:.0f}s"
    if failed:
        bit += f", {failed} failed"
    return bit
