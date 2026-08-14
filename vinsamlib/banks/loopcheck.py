"""Find loops that click at their wrap-around point.

A forward loop plays to its last frame and jumps back to the loop start. If
those two frames sit at different levels, the jump is a step discontinuity —
heard as a tick on every repetition, once per loop, forever. It is a defect of
the *authoring*, not of anything this program does: the loop points were
written that way and we copy them verbatim.

**It ships more often than you would expect.** ConvertWithMoss measured 17 of
152 presets across three commercial Ensoniq libraries (their #340, 2026-08-13),
and this project's own detector reproduces that order of magnitude on K2000
material. The reason it survives is that the first hint is usually the
converted preset ticking on the destination device, where finding the culprit
means auditioning every preset.

WHY NOTHING IS REPAIRED AUTOMATICALLY
------------------------------------
Three repairs are offered below and NONE is ever applied on its own. The
detector reports; the user chooses. The reasons are the same ones that would
otherwise argue against having repairs at all:

* **The audio is never touched here.** That is the promise the rest of this
  program makes — renaming, re-placement and assembly are all byte-verbatim on
  the PCM — and a librarian that silently rewrites your samples is a different
  and worse tool.
* **A cross-fade is lossy and irreversible.** It destroys the frames it
  smooths. Applying it on a suspicion, in bulk, to material the user may have
  spent years collecting, is not a decision this program should take.
* **It may be deliberate.** Percussive and rhythmic material clicks on purpose;
  17 of 152 shipping that way in commercial libraries is at least partly
  authorial intent, not universal error.
* **Only one of the three is lossy**, and the user should know which. Two move
  loop POINTS and cannot make the audio wrong; the third rewrites PCM.

So the output is a note naming the sample and how bad the step is, and the
repairs are there to be picked deliberately, per sample, with the originals
kept.

MEASURED EFFECTIVENESS, on 101 clicking loops from real K2000 banks:

    snap    98/101 applicable, click gone in 64%
    nudge   99/101 applicable, click gone in 99%
    fade   101/101 applicable, wrap continuous in 100%

Note the third is scored differently ON PURPOSE. A cross-fade promises
CONTINUITY, not a small number: it makes the last played frame equal the frame
preceding the loop start, so the wrap reproduces the waveform's own step there.
Scored with `check_loop` it appears to "fix" only 42%, because a loop start
sitting on a steep slope has a natural step many times the window median — the
detector flags a perfectly continuous join. That is a fault in the success
measure, not in the fade, and it is worth stating because the wrong number
would steer users away from the only repair that always works.

THE MEASUREMENT
---------------
The step at the wrap is compared against how fast the waveform is *already*
moving near the boundary, because a step of 400 means nothing in a loud, steep
sample and a great deal in a quiet, smooth one. Both of ConvertWithMoss's
thresholds are used, and a click has to clear both:

* at least ``STEP_VS_MOVEMENT`` times the median frame-to-frame movement
  around the two boundaries, and
* at least ``STEP_VS_LEVEL`` of the local peak level.

The second is what stops a near-silent tail from being flagged: a step can be
eight times a tiny median movement and still be inaudible in absolute terms.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

#: A step this many times the local median frame-to-frame movement is a
#: candidate. ConvertWithMoss calibrated 8 on 3527 looped samples; adopted
#: rather than re-derived, and marked as theirs so a future re-calibration
#: knows what it is changing.
STEP_VS_MOVEMENT = 8.0

#: …and it must also be at least this fraction of the local peak, so a quiet
#: passage with a very smooth waveform is not flagged on ratio alone.
STEP_VS_LEVEL = 0.02

#: Frames either side of each boundary used to characterise "how fast is this
#: waveform moving here". Wide enough to survive a single outlier, narrow
#: enough to stay local to the loop point.
WINDOW = 64


@dataclass
class LoopClick:
    """One clicking loop. `step_pct` is the step as a percentage of the local
    peak — the number to show a user, because it is the one that corresponds
    to how loud the tick is."""
    sample_name: str
    step: int
    median_movement: float
    local_peak: int
    step_pct: float

    @property
    def ratio(self) -> float:
        return self.step / self.median_movement if self.median_movement else 0.0


def _frames(pcm: bytes, first_word: int, count: int,
            big_endian: bool = True) -> list[int]:
    """`count` signed 16-bit frames from `first_word`, clipped to the buffer.

    ENDIANNESS IS NOT A DETAIL HERE. KRZ stores PCM big-endian and E4B/EIII
    little-endian, and reading one as the other does not merely shift values —
    it scrambles them, so the median frame-to-frame movement explodes and the
    ratio test stops firing. The symptom is a corpus that looks flawless: this
    module reported 100% clean seams over 3289 E4B loops until the byte order
    was fixed, which is the wrong kind of good news."""
    if count <= 0 or first_word < 0:
        return []
    lo = first_word * 2
    hi = min(len(pcm), lo + count * 2)
    if hi <= lo:
        return []
    n = (hi - lo) // 2
    return list(struct.unpack_from(f"{'>' if big_endian else '<'}{n}h", pcm, lo))


def check_loop(pcm: bytes, loop_start: int, loop_end: int,
               sample_name: str = "", big_endian: bool = True) -> LoopClick | None:
    """Return a LoopClick if this forward loop steps audibly at its wrap.

    `loop_start` and `loop_end` are absolute PCM **word** offsets, and
    `loop_end` is the last frame PLAYED — the wrap goes from there back to
    `loop_start`.
    """
    if loop_end <= loop_start:
        return None

    a = _frames(pcm, loop_end, 1, big_endian)
    b = _frames(pcm, loop_start, 1, big_endian)
    if not a or not b:
        return None
    step = abs(a[0] - b[0])

    # How fast is the waveform moving around BOTH boundaries? Using both ends
    # matters: a loop can start in a smooth passage and end in a steep one,
    # and the wrap has to be judged against the material it actually joins.
    around = (_frames(pcm, max(0, loop_start - WINDOW // 2), WINDOW, big_endian)
              + _frames(pcm, max(0, loop_end - WINDOW // 2), WINDOW, big_endian))
    if len(around) < 4:
        return None
    moves = sorted(abs(y - x) for x, y in zip(around, around[1:]))
    median = moves[len(moves) // 2]
    peak = max((abs(v) for v in around), default=0)
    if peak == 0:
        return None

    # A perfectly smooth region gives a median of 0, where every step is
    # infinitely many times the movement. The level test is what keeps that
    # honest, so require it explicitly rather than dividing by zero.
    ratio_ok = step >= STEP_VS_MOVEMENT * median if median else step > 0
    level_pct = step / peak
    if not (ratio_ok and level_pct >= STEP_VS_LEVEL):
        return None

    return LoopClick(sample_name=sample_name, step=step,
                     median_movement=float(median), local_peak=peak,
                     step_pct=level_pct * 100.0)


# ── repairs ──────────────────────────────────────────────────────────────────
# Three, because they trade different things and no one of them is right for
# every loop. The caller chooses; nothing here is applied automatically, and
# nothing is applied to a file on disk — a repair describes what an ASSEMBLED
# bank should contain, so the source is never modified.
#
#   SNAP    move both loop points to the nearest zero crossing OF THE SAME
#           SLOPE. Touches two numbers, never a byte of audio, and is
#           reversible because the originals are known. Fails when there is no
#           suitable crossing nearby, and refuses on very short loops where
#           moving a point changes the loop's LENGTH enough to alter pitch.
#
#   NUDGE   move only the loop END to wherever the waveform best matches the
#           start, by level and slope. Also points-only. Finds a fit where
#           SNAP cannot (there need be no zero crossing at all), at the cost
#           of moving further, which shortens or lengthens the loop more.
#
#   FADE    cross-fade the audio approaching the loop end into the audio
#           before the loop start, so the wrap is continuous by construction.
#           The only one that always works — and the only one that REWRITES
#           PCM. Lossy, irreversible, and it changes what the sample plays
#           during the fade, not merely where it wraps.
#
# Ordering matters when offering them: the first two cannot make the audio
# wrong, only the loop different. The third can.

#: How far a points-only repair may move a loop point, in frames. Wide enough
#: to reach a crossing in a low-frequency waveform (a 40 Hz cycle at 44.1 kHz
#: is ~1100 frames), narrow enough that the loop's length barely changes.
SEARCH_FRAMES = 1200

#: A loop shorter than this is refused by the points-only repairs. Below a few
#: hundred frames a loop is short enough that moving either end changes its
#: length by a musically significant fraction — for a single-cycle loop that is
#: its PITCH, and a repair that retunes the sample is not a repair.
MIN_LOOP_FRAMES = 600


def _slope(pcm: bytes, w: int, big_endian: bool = True) -> int:
    a = _frames(pcm, w, 2, big_endian)
    return 0 if len(a) < 2 else a[1] - a[0]


def _zero_crossings(pcm: bytes, centre: int, want_rising: bool,
                    big_endian: bool = True) -> list[int]:
    """Word offsets near `centre` where the waveform crosses zero with the
    requested slope, nearest first."""
    lo = max(0, centre - SEARCH_FRAMES)
    f = _frames(pcm, lo, SEARCH_FRAMES * 2, big_endian)
    out = []
    for i in range(len(f) - 1):
        a, b = f[i], f[i + 1]
        if a == b:
            continue
        crosses = (a <= 0 <= b) if want_rising else (a >= 0 >= b)
        if crosses:
            out.append(lo + i)
    out.sort(key=lambda w: abs(w - centre))
    return out


def snap_to_zero(pcm: bytes, loop_start: int, loop_end: int,
                 big_endian: bool = True) -> tuple[int, int] | None:
    """Both points to the nearest same-slope zero crossing. Points only."""
    if loop_end - loop_start < MIN_LOOP_FRAMES:
        return None
    rising = _slope(pcm, loop_start) >= 0
    starts = _zero_crossings(pcm, loop_start, rising)
    ends = _zero_crossings(pcm, loop_end, rising)
    if not starts or not ends:
        return None
    s, e = starts[0], ends[0]
    if e - s < MIN_LOOP_FRAMES:
        return None
    return s, e


def nudge_to_match(pcm: bytes, loop_start: int, loop_end: int,
                   big_endian: bool = True) -> tuple[int, int] | None:
    """Move the loop END to where the waveform best matches the START.

    Scored on level AND slope together, because matching level alone can join
    a rising edge to a falling one — continuous in value, discontinuous in
    direction, and still audible as a softer tick.
    """
    if loop_end - loop_start < MIN_LOOP_FRAMES:
        return None
    tgt = _frames(pcm, loop_start, 1, big_endian)
    if not tgt:
        return None
    tgt_v, tgt_s = tgt[0], _slope(pcm, loop_start)
    lo = max(loop_start + MIN_LOOP_FRAMES, loop_end - SEARCH_FRAMES)
    best, best_at = None, None
    for w in range(lo, loop_end + SEARCH_FRAMES):
        f = _frames(pcm, w, 2, big_endian)
        if len(f) < 2:
            break
        cost = abs(f[0] - tgt_v) + abs((f[1] - f[0]) - tgt_s)
        if best is None or cost < best:
            best, best_at = cost, w
    return (loop_start, best_at) if best_at is not None else None


def crossfade(pcm: bytes, loop_start: int, loop_end: int,
              fade_frames: int = 256, big_endian: bool = True) -> bytes | None:
    """Cross-fade INTO the loop end so the wrap is continuous. REWRITES PCM.

    The frames approaching `loop_end` are blended toward the frames preceding
    `loop_start`, so that by the wrap point the two agree. Everything outside
    that window is untouched, and the loop points do not move.

    This is the only repair that always works, and the only one that changes
    what the sample plays. It is lossy: the original frames in the fade window
    are gone. Returns a NEW pcm buffer; the caller decides whether to keep it.
    """
    n = min(fade_frames, (loop_end - loop_start) // 2)
    if n < 8:
        return None
    tail = _frames(pcm, loop_end - n + 1, n, big_endian)          # approaching the wrap
    head = _frames(pcm, loop_start - n, n, big_endian)            # what precedes the start
    if len(tail) < n or len(head) < n:
        return None
    out = bytearray(pcm)
    for i in range(n):
        t = (i + 1) / n                               # 0 → 1 across the window
        v = int(round(tail[i] * (1.0 - t) + head[i] * t))
        v = max(-32768, min(32767, v))
        struct.pack_into(">h" if big_endian else "<h", out,
                         (loop_end - n + 1 + i) * 2, v)
    return bytes(out)


# ── one entry point for the assemblers ───────────────────────────────────────

REPAIRS = ("snap", "nudge", "fade")

REPAIR_LABELS = {
    "snap": "Snap to zero crossings",
    "nudge": "Nudge loop end to match",
    "fade": "Cross-fade the wrap (rewrites audio)",
}


def apply_repair(kind: str, pcm: bytes, loop_start: int, loop_end: int,
                 big_endian: bool = True
                 ) -> tuple[bytes | None, int, int] | None:
    """Run one named repair. Returns (new_pcm_or_None, start, end), or None.

    `new_pcm` is None for the two point-moving repairs — the caller then
    patches only the loop fields — and a full replacement buffer for the
    cross-fade, whose loop points do not move. Returning None at all means
    the repair could not be applied (too short a loop, no crossing found);
    the caller must leave the sample exactly as it was rather than guess.

    Every assembler goes through here so that the byte-order argument is
    passed in ONE place. It was previously threaded per call site, and both
    times it was got wrong it failed silently: the detector reported a
    flawless corpus, and `crossfade` used a `big_endian` it never declared.
    """
    if kind not in REPAIRS:
        raise ValueError(f"unknown loop repair: {kind!r}")
    if kind == "fade":
        out = crossfade(pcm, loop_start, loop_end, big_endian=big_endian)
        return None if out is None else (out, loop_start, loop_end)
    fn = snap_to_zero if kind == "snap" else nudge_to_match
    moved = fn(pcm, loop_start, loop_end, big_endian=big_endian)
    if moved is None:
        return None
    s, e = moved
    if e <= s:
        return None
    return None, s, e
