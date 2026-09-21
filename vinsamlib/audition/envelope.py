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
from typing import List, Optional

#: Control-rate grid, Hz. 1 ms; the renderer interpolates between points.
CONTROL_RATE = 1000.0

#: Longest tail a single note's release may be rendered for. A release longer
#: than this is truncated and caveated -- an 8-second tail per note would make
#: a four-note audition a 40-second wait.
AUDITION_MAX_TAIL = 8.0

#: Where "silence" is taken to be on the duration formats. The two samplers
#: disagree by 37 dB (60.07 against 97.82) and *neither figure was measured*
#: (models/common.py:4490). This is only used by the seconds branch, which is
#: itself UNMEASURED; the rate branch never needs a silence point.
SILENCE_DB = 60.07

_EPS = 1e-12


def _db_to_gain(db: float) -> float:
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


def _curve_depth(env, frac: float, span_db: float) -> float:
    """dB below peak at ``frac`` of the decay, from the model's own curve."""
    curve = getattr(env, "curve", None)
    if curve is None:
        return frac * span_db
    try:
        return float(curve.depth_at(frac, span_db))
    except Exception:
        return frac * span_db


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
    n = max(2, int(math.ceil(total_s * CONTROL_RATE)) + 1)
    return n, 1.0 / CONTROL_RATE


def _amp_shape(env, *, hold_s: float, total_s: float,
               attack_scale: float = 1.0, whole: bool = False) -> List[float]:
    """Linear gain on the control grid, 0..1 (1.0 == envelope peak)."""
    n, dt = _build_grid(total_s)
    sustain = min(max(float(getattr(env, "sustain", 1.0)), 0.0), 1.0)
    span_db = _amp_span_db(sustain)
    attack = max(0.0, float(getattr(env, "attack", 0.0))) * attack_scale
    decay = _decay_seconds(env)

    values: List[float] = []
    for i in range(n):
        t = i * dt
        if t < attack:
            g = (t / attack) if attack > _EPS else 1.0
        elif t < attack + decay:
            frac = (t - attack) / decay if decay > _EPS else 1.0
            below = _curve_depth(env, frac, span_db)
            g = _db_to_gain(-below)
        else:
            g = sustain
        # Note-off only matters when the source is gated at all.
        if not whole and t >= hold_s:
            rt = t - hold_s
            rate = getattr(env, "release_rate_db_per_s", None)
            if rate:
                # Fall at the measured rate from wherever the note is now.
                level_db = -20.0 * math.log10(max(g, _EPS))
                g = _db_to_gain(-(level_db + abs(float(rate)) * rt))
            else:
                rel = max(0.0, float(getattr(env, "release", 0.0)))
                frac = min(1.0, rt / rel) if rel > _EPS else 1.0
                below = _curve_depth(env, frac, SILENCE_DB)
                g *= _db_to_gain(-below)
        values.append(g)
    return values


def _filter_shape(env, *, hold_s: float, total_s: float) -> List[float]:
    """The filter envelope, 0..1 -- a *normalised* shape, not dB.

    The depth lives in ``voice.filter_env_cents``; this only says where on the
    shape the note is.
    """
    n, dt = _build_grid(total_s)
    sustain = min(max(float(getattr(env, "sustain", 1.0)), 0.0), 1.0)
    span_db = _amp_span_db(sustain)
    attack = max(0.0, float(getattr(env, "attack", 0.0)))
    decay = _decay_seconds(env)
    release = max(0.0, float(getattr(env, "release", 0.0)))

    values: List[float] = []
    for i in range(n):
        t = i * dt
        if t < attack:
            v = (t / attack) if attack > _EPS else 1.0
        elif t < attack + decay:
            frac = (t - attack) / decay if decay > _EPS else 1.0
            below = _curve_depth(env, frac, span_db)
            frac_down = (below / span_db) if span_db > _EPS else 1.0
            v = 1.0 - frac_down * (1.0 - sustain)
        else:
            v = sustain
        if t >= hold_s:
            rt = t - hold_s
            rate = getattr(env, "release_rate_db_per_s", None)
            if rate:
                level_db = -20.0 * math.log10(max(v, _EPS))
                v = _db_to_gain(-(level_db + abs(float(rate)) * rt))
            else:
                frac = min(1.0, rt / release) if release > _EPS else 1.0
                v *= 1.0 - frac
        values.append(min(max(v, 0.0), 1.0))
    return values


def amp_envelope(env, *, hold_s: float, tail_s: float, rate: int,
                 attack_scale: float = 1.0, whole: bool = False) -> List[float]:
    """Linear-gain amp envelope on the control grid.

    ``rate`` is accepted for signature symmetry with the audio rate but the
    grid is control-rate; the renderer interpolates. ``tail_s`` is the
    allowed time after note-off. ``whole`` is ``VoiceLayer.plays_whole_sample``
    -- when set, note-off is ignored entirely.
    """
    del rate  # control-rate by construction; kept for the documented API
    return _amp_shape(env, hold_s=hold_s,
                      total_s=max(hold_s + tail_s, 1.0 / CONTROL_RATE),
                      attack_scale=attack_scale, whole=whole)


def filter_envelope(env, *, hold_s: float, tail_s: float,
                    rate: int) -> List[float]:
    """Normalised (0..1) filter envelope on the control grid."""
    del rate
    return _filter_shape(env, hold_s=hold_s,
                         total_s=max(hold_s + tail_s, 1.0 / CONTROL_RATE))


def interpolate(values: List[float], pos: float) -> float:
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


def attack_scale_for(voice, velocity: int) -> Optional[float]:
    """Velocity scaling of the attack time, or None when it is not modelled.

    ``velocity_to_amp_attack_span``/``_pivot`` make the attack *longer* with
    velocity on some formats. The model carries the span and pivot but no
    direction law measured on these machines, so this is a plain linear
    interpolation and the caller caveats it when it is not 1.0.
    """
    span = getattr(voice, "velocity_to_amp_attack_span", None)
    if span is None:
        return None
    pivot = getattr(voice, "velocity_to_amp_attack_pivot", None)
    if pivot is None:
        pivot = 64
    scale = 1.0 + float(span) * (velocity - int(pivot)) / 126.0
    return max(0.0, scale)
