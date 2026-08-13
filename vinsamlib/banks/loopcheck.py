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

WHY THIS ONLY REPORTS, AND NEVER REPAIRS
----------------------------------------
The obvious repair is a short cross-fade across the loop point, or snapping
the loop to a zero crossing. This module does neither, deliberately:

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
* **The repair already exists downstream.** mpc2emu's conversion path offers
  cross-fade and zero-snap as explicit options. A user who wants it fixed
  should get it there, having chosen it, on a copy — not from a scanner.

So the output is a note naming the sample and how bad the step is. What to do
about it is the user's call, and the note says where the tools are.

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


def _frames(pcm: bytes, first_word: int, count: int) -> list[int]:
    """`count` signed 16-bit frames from `first_word`, big-endian, clipped to
    what the buffer actually holds."""
    if count <= 0 or first_word < 0:
        return []
    lo = first_word * 2
    hi = min(len(pcm), lo + count * 2)
    if hi <= lo:
        return []
    n = (hi - lo) // 2
    return list(struct.unpack_from(f">{n}h", pcm, lo))


def check_loop(pcm: bytes, loop_start: int, loop_end: int,
               sample_name: str = "") -> LoopClick | None:
    """Return a LoopClick if this forward loop steps audibly at its wrap.

    `loop_start` and `loop_end` are absolute PCM **word** offsets, and
    `loop_end` is the last frame PLAYED — the wrap goes from there back to
    `loop_start`.
    """
    if loop_end <= loop_start:
        return None

    a = _frames(pcm, loop_end, 1)
    b = _frames(pcm, loop_start, 1)
    if not a or not b:
        return None
    step = abs(a[0] - b[0])

    # How fast is the waveform moving around BOTH boundaries? Using both ends
    # matters: a loop can start in a smooth passage and end in a steep one,
    # and the wrap has to be judged against the material it actually joins.
    around = (_frames(pcm, max(0, loop_start - WINDOW // 2), WINDOW)
              + _frames(pcm, max(0, loop_end - WINDOW // 2), WINDOW))
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
