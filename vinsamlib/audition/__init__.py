"""
Audition: render a preset's parameters to audio, headless.

Public API. **This package imports no Qt** -- the engine is runnable from a
test with no ``QApplication``. All Qt lives under ``vinsamlib/ui/``.

The one sentence that governs everything here, from the spec's §9:

    This is a model of the preset's parameters. It is not a model of the
    sampler, and it will not sound like the hardware.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from ..notes import name_to_midi
from .caveats import AuditionReport, Caveat, Severity
from .params import AuditionError, SourceProvenance
from .voice import Sounding

NOTE_SEP = ","
CHORD_OPEN, CHORD_CLOSE, HOLD_SEP = "(", ")", "_"

#: The octave convention this project shows everywhere: C3 = MIDI 60.
OCTAVE_OFFSET = 2

#: Longest per-event hold accepted, in milliseconds. Not a musical limit --
#: a typo like `A4_80000` is 80 seconds for ONE note, and since the render
#: runs in a worker behind a status message, the only symptom is an audition
#: that appears to have hung. Refusing it names the number instead.
MAX_HOLD_MS = 60_000


@dataclass(frozen=True)
class NoteEvent:
    """One thing that is struck: a note, or several sounded together.

    ``hold_seconds`` of ``None`` means "use the list's default", which is
    ``AuditionOptions.hold_seconds``. It is NOT resolved at parse time: the
    default belongs to the options, and baking it in here would freeze
    whatever Settings happened to hold when the text was parsed.
    """

    notes: Tuple[int, ...]
    hold_seconds: Optional[float] = None

    @property
    def is_chord(self) -> bool:
        return len(self.notes) > 1

    def held_for(self, default_seconds: float) -> float:
        return (default_seconds if self.hold_seconds is None
                else self.hold_seconds)

    def __str__(self) -> str:
        body = (f"({'+'.join(str(n) for n in self.notes)})" if self.is_chord
                else str(self.notes[0]))
        if self.hold_seconds is None:
            return body
        return f"{body} {int(round(self.hold_seconds * 1000))} ms"


def as_events(notes: Sequence) -> Tuple[NoteEvent, ...]:
    """Accept either bare MIDI numbers or ``NoteEvent``s.

    Audition took a flat tuple of ints before chords existed, and plenty of
    callers -- tests especially -- still say ``notes=(60,)`` because that is
    the whole of what they are testing. Normalising here keeps one code path
    in the renderer without making every caller learn the richer type.
    """
    out = []
    for item in notes:
        if isinstance(item, NoteEvent):
            out.append(item)
        else:
            out.append(NoteEvent((int(item),)))
    return tuple(out)


def describe_events(events: Sequence[NoteEvent]) -> str:
    """The one-line summary Settings shows under the field."""
    events = as_events(events)
    struck = sum(len(e.notes) for e in events)
    chords = sum(1 for e in events if e.is_chord)
    what = f"{len(events)} event(s), {struck} note(s)"
    if chords:
        what += f", {chords} chord(s)"
    return f"{what}: " + ", ".join(str(e) for e in events)


@dataclass(frozen=True)
class AuditionOptions:
    notes: Tuple                    # NoteEvent, or bare MIDI ints (see as_events)
    velocity: int                   # 1..127
    hold_seconds: float             # note-on to note-off
    gap_seconds: float              # silence between notes
    render_rate: int                # Hz, negotiated from the device
    channels: int                   # 1 or 2
    headroom_db: float = -6.0


@dataclass
class Rendering:
    pcm: bytes                      # interleaved signed 16-bit LE
    rate: int
    channels: int
    peak_before_limit: float        # 0..N; >1.0 means the limiter engaged
    report: AuditionReport
    seconds: float

    @property
    def frame_count(self) -> int:
        if self.channels <= 0:
            return 0
        return len(self.pcm) // (2 * self.channels)


def _split_top_level(text: str) -> list:
    """Split on commas that are NOT inside parentheses.

    A plain ``split(",")`` was right until chords existed and is now exactly
    wrong: it cuts ``(A3,C4,E4)`` into three pieces, two of them unbalanced.
    """
    out, depth, current = [], 0, []
    for ch in text:
        if ch == CHORD_OPEN:
            depth += 1
        elif ch == CHORD_CLOSE:
            depth -= 1
            if depth < 0:
                raise ValueError(
                    "a ')' closes a chord that was never opened")
        if ch == NOTE_SEP and depth == 0:
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
    if depth != 0:
        raise ValueError("a '(' is never closed")
    out.append("".join(current))
    return out


def _parse_one_note(token: str) -> int:
    """``'C3'`` or ``'60'`` -> 60. Raises ``ValueError`` naming the token."""
    token = token.strip()
    if not token:
        raise ValueError("empty note in list")
    if HOLD_SEP in token:
        # `int("6_0")` is 60 in Python and `int("60_500")` is 60500 -- digit
        # separators. So a hold suffix MUST be stripped before this point, and
        # anything reaching here with an underscore left in it is a typo that
        # would otherwise be read as a completely different number, silently.
        raise ValueError(
            f"{token!r} still contains '{HOLD_SEP}': a hold goes after the "
            f"note or chord, as A4{HOLD_SEP}100 or (A3,C4){HOLD_SEP}100")
    try:
        note = int(token, 10)
    except ValueError:
        note = name_to_midi(token, OCTAVE_OFFSET)
    if note is None:
        raise ValueError(f"{token!r} is not a note name or MIDI number")
    if not (0 <= note <= 127):
        raise ValueError(
            f"{token!r} is MIDI {note}, outside the playable range 0–127")
    return note


def _split_hold(token: str) -> tuple:
    """``'A4_100'`` -> ``('A4', 0.1)``; ``'A4'`` -> ``('A4', None)``."""
    head, sep, tail = token.rpartition(HOLD_SEP)
    if not sep:
        return token, None
    ms_text = tail.strip()
    if not ms_text.isdigit():
        raise ValueError(
            f"{tail.strip()!r} is not a whole number of milliseconds "
            f"(after '{HOLD_SEP}' in {token.strip()!r})")
    ms = int(ms_text, 10)
    if ms <= 0:
        raise ValueError(f"a hold of {ms} ms in {token.strip()!r} plays nothing")
    if ms > MAX_HOLD_MS:
        raise ValueError(
            f"a hold of {ms} ms in {token.strip()!r} is longer than the "
            f"{MAX_HOLD_MS} ms limit")
    return head, ms / 1000.0


def parse_notes(text: str) -> list:
    """``'A2, A4_100, (A3,C4,E4)_2500'`` -> a list of :class:`NoteEvent`.

    The syntax, which is the whole of it::

        A4              one note, held for the default time
        A4_100          one note, held 100 ms instead
        (A3,C4,E4)      three notes struck together, default time
        (A3,C4,E4)_2500 the same chord, held 2500 ms

    Raises ``ValueError`` with the offending token. Used by BOTH the Settings
    validator and the renderer, so the field cannot accept what the renderer
    rejects.

    Note names use the C3 = 60 convention this project shows everywhere
    (``ui/note_naming.py``); bare MIDI integers work too. Out-of-range values
    (<0, >127) are a ``ValueError`` naming the token, not a clamp.
    """
    if text is None:
        raise ValueError("no notes given")
    events: list = []
    for raw in _split_top_level(str(text)):
        token = raw.strip()
        if not token:
            raise ValueError("empty note in list")
        body, hold_s = _split_hold(token)
        body = body.strip()
        if body.startswith(CHORD_OPEN):
            if not body.endswith(CHORD_CLOSE):
                raise ValueError(
                    f"{token!r} opens a chord that does not close before its "
                    f"hold; write (A3,C4){HOLD_SEP}100")
            inner = body[1:-1].strip()
            if not inner:
                raise ValueError("an empty chord '()' sounds nothing")
            notes = tuple(_parse_one_note(part)
                          for part in inner.split(NOTE_SEP))
        else:
            if CHORD_CLOSE in body:
                raise ValueError(f"{token!r} has a stray ')'")
            notes = (_parse_one_note(body),)
        events.append(NoteEvent(notes, hold_s))
    if not events:
        raise ValueError("no notes given")
    return events


def available(config) -> Tuple[bool, str]:
    """Whether the mpc2emu side can support audition.

    The device probe lives in ``ui/audition_player``; this checks only the
    model, parsers and the two processors audition reuses.
    """
    if config is None:
        from ..config import Config
        config = Config.load()
    return config.check_audition_support()


def acceleration_note() -> str:
    """'' when numpy is present; otherwise the one-line speed note.

    numpy is an accelerator, never a gate (spec §7.2). 'Install numpy to hear
    anything' is a worse product than 'that took four seconds; with numpy it
    takes one'.
    """
    from . import render as _render
    if _render.numpy_in_use():
        return ""
    if _render.numpy_available():
        # Installed but switched off -- say WHICH, or the note reads as "numpy
        # is missing" to someone who can see it in their site-packages.
        return ("Audition is running on the pure-Python renderer because "
                "VINSAMLIB_NO_NUMPY is set; unset it to use the accelerator.")
    return ("Audition is running on the pure-Python renderer; installing "
            "numpy makes long presets render several times faster.")


#: Rendered auditions, most-recently-used last. In this process only, and
#: gone when the app quits -- there is no file anywhere and nothing to
#: invalidate on the next run.
RENDER_CACHE_MAX = 20
_RENDER_CACHE: "OrderedDict" = OrderedDict()
_RENDER_CACHE_LOCK = threading.Lock()


def render_cache_key(kind: str, payload, opts: AuditionOptions,
                     extra=None) -> tuple:
    """A key that changes whenever the AUDIO would.

    The spec said never to cache rendered audio, and the reason it gave was
    right: velocity, the note list and the hold all change the result, and a
    stale audition is the one bug nobody would catch -- it sounds like a
    preset, just not this one. That is an argument about the KEY, not about
    caching, so the key carries every input the renderer reads: the whole
    `AuditionOptions` (notes with their per-entry holds, velocity, gap, rate,
    channels, headroom) and the identity of the source.

    A file-backed source is keyed by path, mtime and size, so editing a bank
    on disc misses. A live object pair -- an Explorer preset -- is keyed by
    `id()`, which is only safe because the cache ENTRY holds a reference to
    those objects (see `_cache_render`): without that, a dropped bank could be
    collected and a new one land on the same address, and the next audition
    of the new one would play the old one.
    """
    ident: tuple
    if isinstance(payload, tuple) and len(payload) == 2 \
            and not isinstance(payload[0], (str, Path)):
        ident = ("obj", id(payload[0]), id(payload[1]))
    elif isinstance(payload, tuple) and len(payload) == 2:
        path, index = payload
        ident = ("path", str(path), _stat_stamp(path), index)
    else:
        ident = ("path", str(payload), _stat_stamp(payload), None)
    return (kind, ident, repr(opts), repr(extra))


def _stat_stamp(path):
    try:
        st = Path(path).stat()
        return (st.st_mtime_ns, st.st_size)
    except (OSError, TypeError, ValueError):
        return None


def cached_render(key):
    """The rendering for `key`, or None. Safe to call from the GUI thread --
    that is the point, so a repeat costs no worker and no window."""
    with _RENDER_CACHE_LOCK:
        got = _RENDER_CACHE.get(key)
        if got is None:
            return None
        _RENDER_CACHE.move_to_end(key)
        return got[0]


def render_cache_retains(key) -> tuple:
    """The objects an entry keeps alive. See `render_cache_key` for why."""
    with _RENDER_CACHE_LOCK:
        got = _RENDER_CACHE.get(key)
        return tuple(got[1]) if got is not None else ()


def _cache_render(key, rendering, keep=()):
    # The keepalive rides in the VALUE, not the key: a key has to be hashable
    # and a parsed Bank is not. It is here for the id() reuse hazard that
    # `render_cache_key` describes -- while an entry lives, the objects its
    # identity was taken from cannot be collected, so nothing else can land
    # on their addresses and be mistaken for them.
    with _RENDER_CACHE_LOCK:
        _RENDER_CACHE[key] = (rendering, tuple(keep))
        _RENDER_CACHE.move_to_end(key)
        while len(_RENDER_CACHE) > RENDER_CACHE_MAX:
            _RENDER_CACHE.popitem(last=False)


def clear_render_cache() -> None:
    with _RENDER_CACHE_LOCK:
        _RENDER_CACHE.clear()


def render_cache_size() -> int:
    with _RENDER_CACHE_LOCK:
        return len(_RENDER_CACHE)


def render_node(payload, kind: str, opts: AuditionOptions,
                progress=None) -> Rendering:
    """Explorer TreeNode payload -> audio. Runs on a worker thread."""
    from . import params as _params
    from . import render as _render
    key = render_cache_key(kind, payload, opts)
    got = cached_render(key)
    if got is not None:
        return got
    parsed = _params.parameters_for_node(payload, kind)
    out = _render.render(parsed.bank, parsed.preset, parsed.provenance, opts,
                         progress=progress)
    _cache_render(key, out, payload if isinstance(payload, tuple) else ())
    return out


def render_staged(bank, preset_obj, opts: AuditionOptions,
                  name: str = "", edits=None, progress=None) -> Rendering:
    """New Bank's ``(bank, preset_obj, name)`` tuple -> audio.

    `edits` are the pane's staged renames, placement, velocity and loop
    repairs, so what is heard is what Save as… would write -- and they are in
    the cache key for the same reason: change a rename or a loop repair and
    this is a different sound.
    """
    from . import params as _params
    from . import render as _render
    key = render_cache_key("staged", (bank, preset_obj), opts,
                           (name, _params._edits_fingerprint(edits)))
    got = cached_render(key)
    if got is not None:
        return got
    parsed = _params.parameters_for_staged(bank, preset_obj, name, edits)
    out = _render.render(parsed.bank, parsed.preset, parsed.provenance, opts,
                         progress=progress)
    _cache_render(key, out, (bank, preset_obj))
    return out


__all__ = [
    "NOTE_SEP", "OCTAVE_OFFSET", "AuditionOptions", "Rendering",
    "parse_notes", "NoteEvent", "as_events", "describe_events",
    "render_cache_key", "cached_render", "clear_render_cache",
    "render_cache_size", "render_cache_retains", "RENDER_CACHE_MAX",
    "available", "acceleration_note", "render_node",
    "render_staged", "AuditionReport", "Caveat", "Severity",
    "AuditionError", "SourceProvenance", "Sounding",
]
