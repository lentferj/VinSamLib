"""
Amp and filter envelope generation.

Generated at **control rate** (1 ms) and linearly interpolated to audio rate:
an audio-rate ``depth_at()`` call per frame is the single biggest avoidable
cost in the render. The control grid is deliberately coarse because the
quantities it feeds are themselves coarse -- these machines' envelopes are
measured in milliseconds and dB, not sample-accurate curves.

Stage by stage, and why each is what it is (spec §4.4):

* **Attack** -- linear in *amplitude* over ``env.attack``. Nothing measures
  attack *curvature* on any of these machines, so a curve here would be
  invention.
* **Decay** -- peak -> ``sustain``, shape from ``env.curve.depth_at()``.
  ``decay_rate_db_per_s`` is preferred when set: the model records that at
  full sustain the seconds are 0 and the byte is unrecoverable from them.
* **Sustain** -- held until note-off at ``hold_s``.
* **Release** -- a rate where the model gives one (AKAI, K2000), seconds
  otherwise (MPC, XPM, E4B). See §6.3.
* **``plays_whole_sample``** -- no gate at all; the note runs the sample out
  and ``release`` is ignored entirely.
"""

from __future__ import annotations

import math
from typing import List, Optional  # noqa: UP035

#: Control-rate grid, Hz. 1 ms; the renderer interpolates between points.
CONTROL_RATE = 1000.0

#: Longest tail a single note's release may be rendered for. A release longer
#: than this is truncated and caveated -- an 8-second tail per note would make
#: a four-note audition a 40-second wait.
AUDITION_MAX_TAIL = 8.0

#: Control-grid points of fade at the end of a ``plays_whole_sample`` voice.
#: 12 ms: long enough to take the step out, short enough to sit inside the
#: 50 ms `_voice_seconds` already budgets past the end of such a voice.
WHOLE_TAIL_FRAMES = 12

#: Where "silence" is taken to be on the duration formats. The two samplers
#: disagree by 37 dB (60.07 against 97.82) and *neither figure was measured*
#: (models/common.py:4490). This is only used by the seconds branch, which is
#: itself UNMEASURED; the rate branch never needs a silence point.
SILENCE_DB = 60.07

_EPS = 1e-12


def _db_to_gain_env(db: float) -> float:
    """dB -> linear gain, with NO floor.

    NOT THE SAME FUNCTION as `render._db_to_gain_floored`, and the difference
    is the point. That one refuses to answer below -96 dB; this one does not,
    because an envelope that falls into denormals is *information* -- it says
    the release ran past the point where the model says silence is. Clamping
    here would silence a long release early and nothing downstream could tell.
    Two functions that differ by one clamp, both called `_db_to_gain`, is how
    the wrong one gets used; they are named for what they do instead.
    """
    return 10.0 ** (db / 20.0)


def _amp_span_db(sustain: float) -> float:
    """dB between the envelope peak and its sustain level.

    A sustain of 0 is silence, so the span is the silence point -- not an
    unbounded fall into denormals.
    """
    if sustain <= 0.0:
        return SILENCE_DB
    if sustain >= 1.0:
        return 0.0
    return min(-20.0 * math.log10(sustain), SILENCE_DB)


def release_seconds(env, *, current_db: float = SILENCE_DB) -> float:
    """How long the release runs, in seconds, for tail budgeting.

    Uses the model's rate when it has one, and the stated seconds otherwise.
    Both branches are honest about which ran -- see the caveat in render.py.
    """
    rate = getattr(env, "release_rate_db_per_s", None)
    if rate:
        return abs(current_db) / float(rate)
    return max(0.0, float(getattr(env, "release", 0.0)))


def _decay_seconds(env) -> float:
    """The decay's duration, preferring the measured rate over the seconds.

    models/common.py records that at full sustain the seconds are 0 and the
    byte is unrecoverable from them, so a rate is the only usable figure then.
    """
    span = _amp_span_db(getattr(env, "sustain", 1.0))
    rate = getattr(env, "decay_rate_db_per_s", None)
    if rate and span > 0.0:
        return span / float(rate)
    return max(0.0, float(getattr(env, "decay", 0.0)))


def _build_grid(total_s: float) -> tuple[int, float]:
    n = max(2, int(math.ceil(total_s * CONTROL_RATE)) + 1)  # noqa: RUF046
    return n, 1.0 / CONTROL_RATE


def _amp_shape(
    env,
    *,
    hold_s: float,
    total_s: float,
    attack_scale: float = 1.0,
    whole: bool = False,
) -> List[float]:  # noqa: UP006
    """Linear gain on the control grid, 0..1 (1.0 == envelope peak).

    EVERYTHING THE LOOP DOES NOT CHANGE IS DERIVED BEFORE IT. This used to run
    3-4 `getattr` calls on `env` per grid point -- and the curve lookup was a
    second `getattr` plus a `try`/`except`, in a helper called twice per point
    -- on a grid where the only thing that moves is `t`. MEASURED at a 1.5 s
    hold and an 8 s tail: 25.7 ms for this grid and 10.9 ms for the filter one,
    36.6 ms per voice, so a 6-layer chord over 5 notes spends 1.1 s reading
    five attributes and doing nothing else. Hoisted below, with the ARITHMETIC
    UNTOUCHED: `t / attack` is still a division and not a multiply by a
    reciprocal, because that would move the last bit and this grid feeds both
    render paths.
    """
    n, dt = _build_grid(total_s)
    sustain = min(max(float(getattr(env, "sustain", 1.0)), 0.0), 1.0)
    span_db = _amp_span_db(sustain)
    attack = max(0.0, float(getattr(env, "attack", 0.0))) * attack_scale
    decay = _decay_seconds(env)

    curve = getattr(env, "curve", None)
    depth_at = curve.depth_at if curve is not None else None
    to_gain = _db_to_gain_env
    rate = getattr(env, "release_rate_db_per_s", None)
    abs_rate = abs(float(rate)) if rate else 0.0
    release = max(0.0, float(getattr(env, "release", 0.0)))
    log = math.log10
    # A one-shot voice runs its sample out and then simply STOPS, at whatever
    # amplitude the sample happens to end on. A drum one-shot ending on a
    # non-zero crossing is a click the same size as a loop seam, and it sits at
    # the end of every one of them. Raised cosine rather than a straight line
    # so the fade starts with zero slope as well as zero change in value.
    fade_from = n - WHOLE_TAIL_FRAMES if whole else n
    fade_span = float(WHOLE_TAIL_FRAMES - 1)
    values: List[float] = []  # noqa: UP006
    append = values.append
    for i in range(n):
        t = i * dt
        if t < attack:
            g = (t / attack) if attack > _EPS else 1.0
        elif t < attack + decay:
            frac = (t - attack) / decay if decay > _EPS else 1.0
            if depth_at is None:
                below = frac * span_db
            else:
                try:
                    below = float(depth_at(frac, span_db))
                except Exception:  # noqa: BLE001
                    below = frac * span_db
            g = to_gain(-below)
        else:
            g = sustain
        # Note-off only matters when the source is gated at all.
        if not whole and t >= hold_s:
            rt = t - hold_s
            if rate:
                # Fall at the measured rate from wherever the note is now.
                level_db = -20.0 * log(max(g, _EPS))
                g = to_gain(-(level_db + abs_rate * rt))
            else:
                frac = min(1.0, rt / release) if release > _EPS else 1.0
                if depth_at is None:
                    below = frac * SILENCE_DB
                else:
                    try:
                        below = float(depth_at(frac, SILENCE_DB))
                    except Exception:  # noqa: BLE001
                        below = frac * SILENCE_DB
                g *= to_gain(-below)
        if i >= fade_from:
            u = (i - fade_from) / fade_span
            g *= 0.5 * (1.0 + math.cos(math.pi * u))
        append(g)
    return values


def _filter_shape(env, *, hold_s: float, total_s: float) -> List[float]:  # noqa: UP006
    """The filter envelope, 0..1 -- a *normalised* shape, not dB.

    The depth lives in ``voice.filter_env_cents``; this only says where on the
    shape the note is. Loop-invariants hoisted exactly as in `_amp_shape`, and
    for the same measured reason.
    """
    n, dt = _build_grid(total_s)
    sustain = min(max(float(getattr(env, "sustain", 1.0)), 0.0), 1.0)
    span_db = _amp_span_db(sustain)
    attack = max(0.0, float(getattr(env, "attack", 0.0)))
    decay = _decay_seconds(env)
    release = max(0.0, float(getattr(env, "release", 0.0)))

    curve = getattr(env, "curve", None)
    depth_at = curve.depth_at if curve is not None else None
    rate = getattr(env, "release_rate_db_per_s", None)
    abs_rate = abs(float(rate)) if rate else 0.0
    to_gain = _db_to_gain_env
    log = math.log10
    values: List[float] = []  # noqa: UP006
    append = values.append
    for i in range(n):
        t = i * dt
        if t < attack:
            v = (t / attack) if attack > _EPS else 1.0
        elif t < attack + decay:
            frac = (t - attack) / decay if decay > _EPS else 1.0
            if depth_at is None:
                below = frac * span_db
            else:
                try:
                    below = float(depth_at(frac, span_db))
                except Exception:  # noqa: BLE001
                    below = frac * span_db
            frac_down = (below / span_db) if span_db > _EPS else 1.0
            v = 1.0 - frac_down * (1.0 - sustain)
        else:
            v = sustain
        if t >= hold_s:
            rt = t - hold_s
            if rate:
                level_db = -20.0 * log(max(v, _EPS))
                v = to_gain(-(level_db + abs_rate * rt))
            else:
                frac = min(1.0, rt / release) if release > _EPS else 1.0
                v *= 1.0 - frac
        append(min(max(v, 0.0), 1.0))
    return values


def amp_envelope(
    env,
    *,
    hold_s: float,
    tail_s: float,
    rate: int,
    attack_scale: float = 1.0,
    whole: bool = False,
) -> List[float]:  # noqa: UP006
    """Linear-gain amp envelope on the control grid.

    ``rate`` is accepted for signature symmetry with the audio rate but the
    grid is control-rate; the renderer interpolates. ``tail_s`` is the
    allowed time after note-off. ``whole`` is ``VoiceLayer.plays_whole_sample``
    -- when set, note-off is ignored entirely.
    """
    del rate  # control-rate by construction; kept for the documented API
    return _amp_shape(
        env,
        hold_s=hold_s,
        total_s=max(hold_s + tail_s, 1.0 / CONTROL_RATE),
        attack_scale=attack_scale,
        whole=whole,
    )


def filter_envelope(env, *, hold_s: float, tail_s: float, rate: int) -> List[float]:  # noqa: UP006
    """Normalised (0..1) filter envelope on the control grid."""
    del rate
    return _filter_shape(
        env, hold_s=hold_s, total_s=max(hold_s + tail_s, 1.0 / CONTROL_RATE)
    )


def interpolate(values: List[float], pos: float) -> float:  # noqa: UP006
    """Linear interpolation of a control-rate list at fractional index ``pos``.

    ``pos`` is in control-grid units (one unit == 1/CONTROL_RATE seconds), so
    the renderer advances it by ``render_rate / CONTROL_RATE`` per audio frame.
    """
    if not values:
        return 0.0
    if pos <= 0.0:
        return values[0]
    last = len(values) - 1
    if pos >= last:
        return values[last]
    i = int(pos)
    frac = pos - i
    return values[i] + (values[i + 1] - values[i]) * frac


def attack_scale_for(voice, velocity: int) -> Optional[float]:  # noqa: UP045
    """Velocity scaling of the attack time, or None when it is not modelled.

    ``velocity_to_amp_attack_span`` is ``t(velocity 1) / t(velocity 127)``.
    mpc2emu states the unit at `models/common.py:6254` and backs it with a
    measured table (`:6280`, s3ked §47):

        V_ATT1   vel 1     vel 64    vel 127     span = t1/t127
          -50    2.955     0.555     0.010          295.5
          -25    2.955     0.545     0.025          118.2

    so **velocity 127 gets the SHORTEST attack and velocity 1 the LONGEST**,
    and the authored time applies at the pivot (64 on the AKAI, 127 for the
    E4XT's `Vel<`). Greater than 1 is the common case by a wide margin.

    Still an interpolation -- no per-format SHAPE between the two endpoints is
    measured -- but the endpoints, the pivot and the direction are now the
    ones the unit names, and the caller caveats it when the result is not 1.0.
    """
    span = getattr(voice, "velocity_to_amp_attack_span", None)
    if span is None:
        return None
    pivot = getattr(voice, "velocity_to_amp_attack_pivot", None)
    if pivot is None:
        pivot = 64
    span = float(span)
    pivot = int(pivot)
    if span <= 0.0:
        return None
    if span == 1.0:
        return 1.0  # neutral at every velocity; skip the algebra

    # Linear in the attack TIME (not in log time), anchored at both ends:
    #   scale(v) = 1 + m * (v - pivot)
    # so the authored time applies AT the pivot, and the two ends satisfy the
    # unit's own definition -- scale(1) / scale(127) == span:
    #   1 + m*u = span * (1 + m*w),   u = 1 - pivot, w = 127 - pivot
    #   =>  m = (span - 1) / (u - span*w)
    #
    # The previous form, `1.0 + span * (velocity - pivot) / 126.0`, had the
    # sign backwards AND did not pass through the pivot: at span 2.0 it gave
    # scale(64) = 2.0 (not 1.0), scale(127) = 2.5 (faster-note-slower) and
    # scale(1) = 1.5. Since ~96% of the AKAI corpus keygroups that set this
    # make hard notes FASTER, that was wrong on nearly every voice carrying
    # the field, by up to the full span.
    u = 1 - pivot
    w = 127 - pivot
    denom = u - span * w
    if abs(denom) < 1e-12:
        # Only reachable for a pivot outside 1..127 by a wide margin, where
        # the two velocities named by the unit are on the same side of the
        # anchor and no line can join them. Fall back to the pivot's own
        # side rather than guessing a slope.
        return 1.0
    m = (span - 1.0) / denom
    return max(0.0, 1.0 + m * (velocity - pivot))
