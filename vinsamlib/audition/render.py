"""
The render pipeline: mpc2emu Bank + Preset -> interleaved 16-bit PCM.

Everything runs at one **render rate** -- the audio device's own, negotiated
before rendering -- so there is no post-render resample and no silent format
mismatch.

Nothing here imports Qt. The engine must be runnable headless, from a test,
with no ``QApplication``; all Qt lives under ``vinsamlib/ui/``.

numpy is selected once at import as an accelerator, never per call. It speeds
sample read, envelope application and mix; it does **not** accelerate the
filter, an IIR being sequential. When it is absent the pure-Python path runs
and is slower, never refused (spec §7.2).
"""

from __future__ import annotations

import contextlib
import math
import os
import threading
from collections import OrderedDict
from typing import List, Optional  # noqa: UP035

from .. import mpc2emu_bridge
from ..build import calllog
from . import envelope as env_mod
from . import filter as filter_mod
from . import voice as voice_mod
from .caveats import AuditionReport, Severity

try:  # accelerator only -- never a gate
    import numpy as _numpy
except ImportError:  # pragma: no cover - depends on the machine
    _numpy = None

#: The accelerator as the renderer sees it. Separate from `_numpy` so it can
#: be switched OFF while numpy is installed.
#:
#: WITHOUT THAT SWITCH THERE IS NOTHING TO COMPARE. The two render paths are
#: allowed to differ slightly, and the only way to know by how much is to run
#: BOTH over the same preset and subtract -- in one process, because a preset
#: is parsed and decoded once. An environment variable alone cannot do that;
#: it decides at import and the process then has one path for its lifetime.
#: So: the variable for running the whole app or a whole test suite on the
#: pure path, and `pure_python()` for holding the two side by side.
_np = None if os.environ.get("VINSAMLIB_NO_NUMPY") else _numpy


def numpy_available() -> bool:
    """Whether numpy is installed at all, regardless of the switch."""
    return _numpy is not None


def numpy_in_use() -> bool:
    """Whether the accelerator is actually being used right now."""
    return _np is not None


def numpy_version() -> str:
    """The installed numpy's version, or "" if there is none."""
    return getattr(_numpy, "__version__", "") if _numpy is not None else ""


def renderer_description() -> str:
    """One line naming the render path, for a menu, About, and the log.

    ONE SENTENCE, WRITTEN ONCE. Whether numpy is in use is now visible in
    three places, and three places that phrase it themselves is three places
    that can disagree -- which matters here more than usual, because the
    numpy and pure paths are no longer required to produce identical audio.
    Someone comparing two machines has to be able to tell which path each one
    took, and a menu that says one thing while the log says another is worse
    than neither.
    """
    if _np is not None:
        return f"numpy {numpy_version()} (accelerated)"
    if _numpy is not None:
        return (
            f"pure Python — numpy {numpy_version()} is installed but "
            f"VINSAMLIB_NO_NUMPY is set"
        )
    return "pure Python — numpy is not installed"


@contextlib.contextmanager
def pure_python():
    """Run the pure-Python path inside this block, even with numpy present.

    Not thread-safe, and deliberately not made so: it flips a module global,
    and a renderer running on a worker thread would see the flip. It is for
    tests and for a one-off comparison, never for the app.
    """
    global _np
    saved = _np
    _np = None
    try:
        yield
    finally:
        _np = saved


#: Velocity curve names, matching models.common's constants. Duplicated as
#: plain strings so this module can be imported without mpc2emu for a test.
_VELOCITY_CURVE_DB_LINEAR = "db-linear"
_VELOCITY_CURVE_AMPLITUDE_LINEAR = "amplitude-linear"

KEYTRACK_PIVOT = 60

_HARD_FLOOR_DB = -96.0


def _db_to_gain_floored(db: float) -> float:
    """dB -> linear gain, clamped at `_HARD_FLOOR_DB`.

    NOT THE SAME FUNCTION as `envelope._db_to_gain_env`, and the difference is
    the point: that one has no floor, because an envelope falling into
    denormals is information. THIS one refuses, because a level the user set at
    -100 dB would otherwise produce `10 ** -5 = 1e-5`-and-smaller values that
    cost real time to multiply and sound like nothing anyway. The two were
    both called `_db_to_gain`, which is how the wrong one gets picked.
    """
    return 10.0 ** (max(db, _HARD_FLOOR_DB) / 20.0)


class _Source:
    """A sample decoded once, with its per-channel float lists.

    Decoding costs more than the parse and repeats per note, so the caller
    caches these by ``(id(bank), name)``.

    ``arrays`` holds the numpy form of each channel's list, lazily. The lists
    are the pure path's format and the arrays are the accelerated path's, and
    `_read_block` used to convert between them **once per voice per
    channel** -- with L layers over M notes that is ``2 L M`` conversions of
    the same data. MEASURED on a real render: `np.asarray` on a 264 000-element
    list is 7.0 ms, and `numpy.asarray` was 0.409 s of a 1.314 s render (31 %)
    for 167 calls, of which the 30 `asarray(ch)` calls dominate.

    The arrays are built on demand and never invalidated: a `_Source` is
    immutable once built (the lists are not mutated anywhere), and the extra
    8 bytes per frame is cheaper than the ~32 bytes per frame the Python list
    of floats already costs.
    """

    __slots__ = (  # noqa: RUF023
        "channels",
        "arrays",
        "rate",
        "frames",
        "loop_start",
        "loop_end",
        "loop_type",
    )

    def __init__(
        self,
        channels: List[List[float]],  # noqa: UP006
        rate: int,  # noqa: RUF100, UP006
        loop_start: int,
        loop_end: int,
        loop_type: int,
    ):
        self.channels = channels
        self.arrays: list = [None] * len(channels)
        self.rate = rate
        self.frames = len(channels[0]) if channels else 0
        self.loop_start = loop_start
        self.loop_end = loop_end
        self.loop_type = loop_type

    def array(self, index: int):
        """Channel ``index`` as a float64 array, converted once.

        Returns None on the pure path -- there is no numpy module to speak of
        there, and the caller is never reached in that case.
        """
        a = self.arrays[index]
        if a is None:
            a = _np.asarray(self.channels[index], dtype="float64")
            self.arrays[index] = a
        return a


def _decode(sample) -> _Source:
    """Decode an mpc2emu ``SampleData`` into per-channel float lists.

    Uses the sibling project's own ``_pcm_to_float`` (a bulk ``array('h')``)
    so the pure-Python floor is the same one mpc2emu accepts, with a numpy
    path when it is present.
    """
    data = sample.data
    n_ch = int(getattr(sample, "channels", 1) or 1)
    n_ch = 2 if n_ch == 2 else 1
    if _np is not None:
        # TRIM TO WHOLE FRAMES FIRST. frombuffer raises on an odd byte count
        # where resampler._pcm_to_float drops the stray byte, so a sample with
        # one trailing byte auditioned fine WITHOUT numpy and crashed with it
        # -- an accelerator that changes behaviour is not an accelerator.
        data = data[: (len(data) // 2) * 2]
        arr = _np.frombuffer(data, dtype="<i2").astype("float64") / 32768.0
        # .tolist() IS DELIBERATE AND WAS MEASURED. A review proposed keeping
        # these as ndarrays to save the conversion; measured here on one
        # second at 44.1 kHz, that is a 2x REGRESSION, because `_read` indexes
        # per frame and a numpy scalar costs far more than a float:
        #     tolist() once                2.33 ms
        #     1 s of _read over a list    22.16 ms   -> 24.5 ms total
        #     1 s of _read over an array  47.67 ms   -> 47.7 ms total
        # The conversion is per SAMPLE and cached; the reads are per FRAME.
        if n_ch == 2:
            arr = arr[: (len(arr) // 2) * 2]
            channels = [arr[0::2].tolist(), arr[1::2].tolist()]
        else:
            channels = [arr.tolist()]
    else:
        flat = mpc2emu_bridge.resampler._pcm_to_float(data)
        if n_ch == 2:
            # TRIM TO WHOLE FRAMES, EXACTLY AS THE NUMPY ARM DOES ABOVE.
            # `_pcm_to_float` drops a trailing odd BYTE and nothing else, so a
            # stereo sample whose byte count is 2 or 3 past a frame boundary
            # leaves an odd number of 16-bit words behind -- and then
            # `flat[0::2]` has one element more than `flat[1::2]`. No crash,
            # but the channels are desynchronised by one sample at the tail
            # and `src.frames` disagrees with the accelerated path, so
            # whether numpy is installed decides how long the sample is.
            # The trim is what makes the two arms the same decoder.
            flat = flat[: (len(flat) // 2) * 2]
            channels = [flat[0::2], flat[1::2]]
        else:
            channels = [flat]
    loop_start = int(getattr(sample, "loop_start", 0) or 0)
    loop_end = int(getattr(sample, "loop_end", 0) or 0)
    n_frames = len(channels[0]) if channels else 0
    # A malformed loop point must be clamped, not indexed: mpc2emu gives real
    # ones, but loop_end out of range is a crash the report should carry as a
    # read, not as an exception.
    loop_end = min(max(0, loop_end), max(0, n_frames - 1))
    loop_start = min(max(0, loop_start), loop_end)
    loop_type = int(getattr(sample, "loop_type", 0) or 0)
    return _Source(channels, int(sample.sample_rate), loop_start, loop_end, loop_type)


def _decode_for_render(sample, report: AuditionReport) -> _Source:
    """Bake a ping-pong loop to forward *before* decoding (spec §5.2).

    ``loop_renderer.bake_alternating_loop`` returns a new SampleData for an
    ALTERNATING source and the same object otherwise, so this is a no-op for
    every other loop type.
    """
    if int(getattr(sample, "loop_type", 0) or 0) == 2:  # LoopType.ALTERNATING
        try:
            sample = mpc2emu_bridge.loop_renderer.bake_alternating_loop(sample)
        except Exception:  # noqa: BLE001, S110
            pass
    return _decode(sample)


#: Decoded samples, keyed by id(). Bounded by decoded bytes: decoding costs
#: more than the parse and repeats per note, but a KRZ bank carries tens of MB
#: of PCM and holding every sample of every preset would be worse than the
#: decode it saves. The sample object is kept ALIVE in the value, so its id
#: cannot be reused by a later allocation while it is cached.
_SAMPLE_CACHE: "OrderedDict[int, tuple]" = OrderedDict()  # noqa: UP037
_SAMPLE_CACHE_MAX_BYTES = 64 * 1024 * 1024
_sample_cache_bytes = 0

# Two auditions really can render at once: the generation counter in
# main_window discards the older render's RESULT, it does not cancel the
# QRunnable, which keeps decoding in the pool. `_sample_cache_bytes += size`
# is LOAD_GLOBAL/BINARY_OP/STORE_GLOBAL — three bytecodes with a preemption
# point between them, so a lost update is not hypothetical. The damage is
# silent and permanent: the running total drifts away from what the cache
# actually holds, and from then on the cache either evicts on every insert
# or never evicts at all. Nothing raises; it just stops being bounded.
_SAMPLE_CACHE_LOCK = threading.Lock()


def _decode_cached(sample, report: AuditionReport) -> _Source:
    global _sample_cache_bytes
    key = id(sample)
    with _SAMPLE_CACHE_LOCK:
        got = _SAMPLE_CACHE.get(key)
        if got is not None and got[0] is sample:
            _SAMPLE_CACHE.move_to_end(key)
            return got[1]
    # Decode OUTSIDE the lock: it is the expensive half and it calls into
    # mpc2emu. Two threads decoding the same sample duplicates work once and
    # produces equal results; holding the lock across it would serialise
    # every render behind the slowest decode.
    src = _decode_for_render(sample, report)
    size = src.frames * max(1, len(src.channels)) * 8  # float64 bytes
    with _SAMPLE_CACHE_LOCK:
        prior = _SAMPLE_CACHE.get(key)
        if prior is not None and prior[0] is sample:
            # The other thread got there first. Its entry is already counted,
            # so adding ours again is exactly the drift this lock exists to
            # prevent. Use theirs and charge nothing.
            _SAMPLE_CACHE.move_to_end(key)
            return prior[1]
        _SAMPLE_CACHE[key] = (sample, src)
        _sample_cache_bytes += size
        while _sample_cache_bytes > _SAMPLE_CACHE_MAX_BYTES and len(_SAMPLE_CACHE) > 1:
            _old_key, (_old_sample, old_src) = _SAMPLE_CACHE.popitem(last=False)
            _sample_cache_bytes -= old_src.frames * max(1, len(old_src.channels)) * 8
    return src


def clear_sample_cache() -> None:
    global _sample_cache_bytes
    with _SAMPLE_CACHE_LOCK:
        _SAMPLE_CACHE.clear()
        _sample_cache_bytes = 0


def _read(src: _Source, ch: List[float], pos: float, looping: bool) -> float:  # noqa: UP006
    """One interpolated read at fractional source-frame ``pos``."""
    n = len(ch)
    if n == 0:
        return 0.0
    # `pos > le` IS THE WHOLE CONDITION. Without it, a position BEFORE the
    # loop folded into it: Python's % returns a positive remainder for a
    # negative operand, so `(pos - ls) % span` mapped frame 0 of a sample
    # looping 1000..1400 to frame 1203. Every forward-looped multisample
    # auditioned with no attack transient, starting at an arbitrary phase
    # inside its own loop -- audible on everything with a pluck or a hit, and
    # exactly the class of defect this feature exists to catch. Found by an
    # external review 2026-09-21 and reproduced on a ramp before fixing.
    ls, le = src.loop_start, src.loop_end
    rel = -1.0
    if looping and le > ls:
        span = le - ls + 1
        if pos > le:
            rel = (pos - ls) % span
        elif pos >= ls:
            # Inside the loop already. The seam blend below has to be
            # reachable from here as well as after the wrap, or a position
            # step large enough to jump over the blend region would restore
            # the hard seam it exists to remove.
            rel = pos - ls
        if rel >= 0.0:
            xf = _loop_xfade_width(span)
            if rel >= span - xf:
                return _seam_blend(ch, ls, le, xf, rel - (span - xf))
            if pos > le:
                pos = ls + rel
    i = int(pos)
    if i >= n - 1:
        return 0.0 if not looping else ch[n - 1]
    if i < 0:
        return 0.0
    frac = pos - i
    return ch[i] + (ch[i + 1] - ch[i]) * frac


#: Frames of equal-power crossfade at a forward-loop seam. Chosen inside the
#: 16-32 band the review names: long enough that the seam's slope is divided by
#: roughly this factor instead of being concentrated in one frame, short enough
#: that a loop only a few hundred frames long is not mostly crossfade.
LOOP_XFADE_FRAMES = 24

_HALF_PI = math.pi / 2.0


def _loop_xfade_width(span: int) -> int:
    """How much of a loop of ``span`` frames may be spent crossfading.

    0 for a loop too short for it to mean anything -- below 8 frames the seam
    and the loop are the same thing -- and otherwise a quarter of the loop at
    most, so a 100-frame loop gets a 24-frame blend and a 3000-frame one the
    full 24.
    """
    if span < 8:
        return 0
    return min(LOOP_XFADE_FRAMES, span // 4)


def _interp(ch: List[float], p: float) -> float:  # noqa: UP006
    """Linear interpolation at ``p``, holding the last frame past the end."""
    last = len(ch) - 1
    if last < 1:
        return ch[0] if ch else 0.0
    i = int(p)
    if i >= last:
        return ch[last]
    if i < 0:
        i = 0
    return ch[i] + (ch[i + 1] - ch[i]) * (p - i)


def _seam_blend(
    ch: List[float],
    ls: int,
    le: int,
    xf: int,
    k: float,  # noqa: UP006
) -> float:
    """Equal-power crossfade across a forward loop's seam, over ``xf`` frames.

    The old blend was ONE frame: ``ch[le] * (1 - frac) + ch[ls] * frac``. That
    is value-continuous, which is why nothing noticed it, but it puts the whole
    seam step ``|ch[le] - ch[ls]|`` into a single frame's slope -- one tick per
    loop period, which is exactly the defect this feature exists to hear.

    WHY THE HEAD RUNS BACKWARD. A crossfade has to be C0-continuous at BOTH
    ends, because a residual step at either end is a click of the same size as
    the one being removed. The tail is the loop's LAST ``xf`` frames, read
    forward from ``le - xf + 1``; the frame before the blend is ``le - xf``, so
    the tail enters continuously. The head has to leave the blend at ``ch[ls]``,
    because the natural read resumes at ``ls`` the moment the position wraps --
    so it must arrive there, and it starts from the far side of the loop's own
    first ``xf`` frames. Read forward it would leave at ``ch[ls + xf]`` and
    step down to ``ch[ls]``; read backward it runs ``ls + xf - 1 -> ls`` and
    meets the wrap exactly.

    That is the only arrangement that works, and the reason is arithmetic
    rather than taste: a crossfade overlaps two segments, so it covers ``2*xf``
    source frames in ``xf`` frames of time. A loop of ``span`` frames must still
    advance ``span`` source frames per ``span`` frames of output, so the blend
    cannot consume NEW material -- it can only re-read material the loop has
    already played. The head qualifies; the loop's own start does.

    THE COST OF THE BACKWARD HEAD is a ``xf``-frame reversed copy of the loop's
    opening, faded in under the tail, once per loop period. Measured against
    the alternative -- a residual step of ``|ch[ls + xf - 1] - ch[ls]|``, which
    on a 1 kHz tone at 44.1 kHz is over 1.4 of full scale -- it is the better
    trade, and the fade-in means the reversed motion is only audible where the
    tail is already quiet.
    """
    u = k / xf
    tail = _interp(ch, min(le - xf + 1 + k, float(le)))
    head = _interp(ch, float(ls + xf - 1) - k)
    return tail * math.cos(u * _HALF_PI) + head * math.sin(u * _HALF_PI)


def _read_block(src: "_Source", ch_idx: int, pos, looping):  # noqa: UP006, UP037
    """`_read` for a whole array of positions at once.

    A transcription of `_read`, mask for branch, and it has to stay one: that
    function's comment records a real defect -- a position BEFORE the loop
    folding INTO it, so every forward-looped multisample started at an
    arbitrary phase with no attack transient. The same `pos > loop_end` guard
    is the second term of `fold` below, for the same reason.

    `looping` is an array because a release-triggered loop stops looping at
    note-off, which happens mid-buffer.

    Takes the channel INDEX rather than the channel, so the float list is
    converted to an array once per sample instead of once per voice per
    channel -- see `_Source.array`.

    NUMPY-ONLY, AND THE NAME NO LONGER PRETENDS OTHERWISE. This used to be
    `_read_block_np` and carried a full pure-Python branch behind an
    `if _np is None:` guard -- which sat BELOW the `_np.zeros(...)` at the top
    of the same function, so it could never run: with the accelerator off,
    an empty channel raised AttributeError on the first dereference and a
    populated one raised it in `_Source.array`. Every caller reaches this from
    `_render_voice_np`, which is itself only entered when `_np is not None`.
    The branch is deleted rather than repaired; the pure path is `_read`, and
    it is not a fallback for this one.
    """
    n = len(src.channels[ch_idx])
    if n == 0:
        return _np.zeros(pos.shape, dtype="float64")
    data = src.array(ch_idx)
    pos = _np.asarray(pos, dtype="float64")
    looping = _np.asarray(looping, dtype=bool)
    out = _np.zeros(pos.shape, dtype="float64")

    ls, le = src.loop_start, src.loop_end
    p = pos.copy()
    seam = _np.zeros(pos.shape, dtype=bool)
    seam_k = _np.zeros(pos.shape, dtype="float64")
    xf = 0
    if le > ls:
        span = le - ls + 1
        xf = _loop_xfade_width(span)
        fold = looping & (pos > le)
        # `rel` is the position within the loop cycle, or -1 before the loop
        # reaches it. Computed for every position and not only the folded ones,
        # because the seam blend has to be reachable from below the wrap as
        # well -- see `_read`.
        rel = _np.where(pos >= ls, pos - ls, -1.0)
        if fold.any():
            rel = _np.where(fold, _np.mod(pos - ls, span), rel)
        p = _np.where(fold, ls + rel, pos)
        if xf:
            seam = looping & (rel >= span - xf)
            seam_k = rel - (span - xf)

    i = _np.floor(p).astype(_np.int64)  # positions are >= 0, so == int()
    frac = p - i
    inside = (i >= 0) & (i < n - 1) & ~seam
    past = (i >= n - 1) & ~seam
    if inside.any():
        ii = i[inside]
        lo = data[ii]
        out[inside] = lo + (data[ii + 1] - lo) * frac[inside]
    # Past the end: silence, unless looping, where `_read` holds the last
    # frame rather than dropping to zero.
    if past.any():
        out[past] = _np.where(looping[past], data[n - 1], 0.0)
    if seam.any():
        k = seam_k[seam]
        theta = k * (_HALF_PI / xf)
        tail = _interp_np(data, _np.minimum(le - xf + 1 + k, float(le)))
        head = _interp_np(data, float(ls + xf - 1) - k)
        out[seam] = tail * _np.cos(theta) + head * _np.sin(theta)
    return out


def _interp_np(data, q):
    """``_interp`` over an array: linear, and the last frame past the end.

    A transcription, index for index: `_interp` returns `ch[last]` outright
    where the integer part has reached the end, and this does the same rather
    than interpolating to it, because `a + (b - a) * 1.0` is not always exactly
    `b` and `manual_audition_numpy_tolerance` measures the difference.
    """
    q = _np.asarray(q, dtype="float64")
    last = data.size - 1
    if last < 1:
        return _np.full(q.shape, float(data[0]) if data.size else 0.0)
    i = _np.floor(q).astype(_np.int64)
    i = _np.maximum(i, 0)
    at_end = i >= last
    i = _np.where(at_end, last - 1, i)
    val = data[i] + (data[i + 1] - data[i]) * (q - i)
    return _np.where(at_end, data[last], val)


def _env_block_np(values: List[float], t):  # noqa: UP006
    """`envelope.interpolate` for a whole array of control-grid positions.

    `np.interp` clamps at both ends, which is what `interpolate` does with its
    `pos <= 0` and `pos >= last` branches, and is linear between -- the same
    arithmetic in a different order, which the tolerance test measures.
    """
    if not values:
        return _np.zeros(_np.shape(t), dtype="float64")
    v = _np.asarray(values, dtype="float64")
    return _np.interp(
        _np.asarray(t, dtype="float64"), _np.arange(v.size, dtype="float64"), v
    )


# ── level / pan / velocity ───────────────────────────────────────────────────


def velocity_to_volume_db(
    voice,
    velocity: int,
    report: Optional[AuditionReport] = None,  # noqa: UP045
) -> float:  # noqa: RUF100, UP045
    """Velocity -> level in dB, branching on the model's own curve.

    Getting the branch wrong is worth ~14 dB RMS on MPC material, so it is not
    an optional refinement. ``velocity_to_volume_db is None`` means *not read*,
    distinct from ``0.0`` meaning *measured neutral*: None is treated as
    neutral **and caveated**.
    """
    swing = getattr(voice, "velocity_to_volume_db", None)
    curve = getattr(voice, "velocity_to_volume_curve", _VELOCITY_CURVE_DB_LINEAR)
    pivot = getattr(voice, "velocity_to_volume_pivot", None)
    if swing is None:
        if report is not None:
            report.note(
                Severity.UNMEASURED,
                "velocity → volume",
                "This voice's velocity-to-volume amount was not recorded by "
                "the source, so notes play at a neutral level rather than a "
                "velocity-scaled one.",
            )
        return 0.0
    if curve == _VELOCITY_CURVE_AMPLITUDE_LINEAR:
        s = min(max(float(swing), 0.0), 1.0)
        amp = (1.0 - s) + s * (velocity / 127.0)
        if amp <= 1e-6:
            return _HARD_FLOOR_DB
        return 20.0 * math.log10(amp)
    # dB-linear: the same arithmetic as models.common.velocity_pivot_offset_db
    p = 64 if pivot is None else int(pivot)
    return float(swing) * (velocity - p) / 126.0


def _velocity_to_pan(voice, velocity: int) -> float:
    amount = getattr(voice, "velocity_to_pan", 0.0) or 0.0
    return float(amount) * (velocity / 127.0)


def _key_to_pan(voice, note: int) -> float:
    amount = getattr(voice, "key_to_pan", 0.0) or 0.0
    return float(amount) * (note - 60) / 63.0


def _constant_power(pan: float) -> tuple[float, float]:
    angle = (min(max(pan, -1.0), 1.0) + 1.0) * math.pi / 4.0
    return math.cos(angle), math.sin(angle)


# ── filter modulation ────────────────────────────────────────────────────────


def velocity_to_filter_cents(voice, velocity: int) -> float:
    """Both ends carried: the polarity is the point of ``_min_cents``.

    Collapsing ``(0, +10800)`` and ``(-5400, +5400)`` to one scalar loses the
    polarity, and driving a cymbal's corner below a floor it never crosses is
    silence.
    """
    top = float(getattr(voice, "velocity_to_filter_cents", 0.0) or 0.0)
    bottom = float(getattr(voice, "velocity_to_filter_min_cents", 0.0) or 0.0)
    return bottom + (top - bottom) * velocity / 127.0


# ── LFO ──────────────────────────────────────────────────────────────────────
#
# WHICH DESTINATIONS, AND WHY THESE. Counted over this library rather than
# guessed: 32 558 E4B voices, 7 230 KRZ voices and all 453 064 MPC keygroups.
#
#     destination      E4B      KRZ      MPC
#     filter cutoff   7.51%    2.55%    6.69%
#     pan             1.67%    2.49%   14.84%
#     volume          0.00%    0.06%   12.07%
#     filter Q        0.10%    0.00%       --
#
# Cutoff is the most used on the hardware formats and is also the CHEAPEST to
# add: the filter already recomputes its coefficients once per control block
# for the filter envelope, so an LFO term is one more multiply in an
# expression that was going to run anyway. Volume is the one that looks
# obvious and is almost absent outside MPC.
#
# NOT MODELLED HERE, and the report still says so: delay, fade-in
# (`lfo1_delay`), key-sync, tempo sync, and LFO 2 where a format has one we
# do not read. The shapes below are the four every format here names; an
# unknown shape falls back to a sine rather than to silence.

#: The shapes this renderer knows, and the ONLY place they are listed. It used
#: to be read by nothing but `tests/manual_audition_lfo.py`, while `_lfo_terms`
#: re-implemented the table as a chain of `if shape == "..."` string
#: comparisons and `lfo_value` fell back to a sine for anything else -- so an
#: unrecognised name reached the oscillator unnormalised and the table could
#: have named a shape the renderer did not implement. It is now the thing
#: `_lfo_terms` normalises through.
LFO_SHAPES = ("sine", "triangle", "square", "sawtooth", "random")

#: Full LFO-to-volume depth, in dB either side of the note's own level.
#:
#: CHOSEN, NOT MEASURED, and the report says so. The model gives a 0..1
#: depth with no unit attached, and 12 dB is a tremolo that is clearly
#: audible without swamping the note -- erring shallow, because too much
#: movement is mistaken for a defect in the preset and too little is merely
#: understated.
LFO_VOLUME_DB = 12.0


def lfo_value(shape: str, phase: float) -> float:
    """One LFO cycle, -1..+1, at `phase` in turns (0..1 is one full cycle).

    `random` is a SAMPLE-AND-HOLD on a fixed sequence, not noise: an audition
    that sounded different every time it was rendered could not be compared
    against anything, which is the whole point of the feature.
    """
    ph = phase - math.floor(phase)
    if shape == "square":
        return 1.0 if ph < 0.5 else -1.0
    if shape == "sawtooth":
        return 2.0 * ph - 1.0
    if shape == "triangle":
        return 4.0 * ph - 1.0 if ph < 0.5 else 3.0 - 4.0 * ph
    if shape == "random":
        # A cheap deterministic hash of the step number.
        step = int(phase)
        x = (step * 1103515245 + 12345) & 0x7FFFFFFF
        return (x / float(0x3FFFFFFF)) - 1.0
    return math.sin(2.0 * math.pi * ph)


def _lfo_terms(voice):
    """(rate_hz, shape, to_cents, to_q, to_pan, to_volume) or None.

    Sums LFO 1 and LFO 2 into one oscillator when both route to the same
    place and share a rate; otherwise LFO 1 wins, because two independent
    oscillators would need two phases and the second is vanishingly rare
    (15 of 32 558 E4B voices route LFO 2 to the filter at all).
    """
    rate = float(getattr(voice, "lfo1_rate", 0.0) or 0.0)
    # Normalised through LFO_SHAPES, not compared against it one string at a
    # time: an unknown shape must land on a shape this renderer HAS, which is
    # what `lfo_value` would do anyway -- but doing it here means the shape the
    # report names and the shape the oscillator runs are the same string.
    shape = str(getattr(voice, "lfo1_shape", "") or "").lower()
    if shape not in LFO_SHAPES:
        shape = "sine"
    cents = float(getattr(voice, "lfo1_to_filter_cents", 0.0) or 0.0)
    q = float(getattr(voice, "lfo1_to_filter_q", 0.0) or 0.0)
    pan = float(getattr(voice, "lfo1_to_pan", 0.0) or 0.0)
    vol = float(getattr(voice, "lfo1_to_volume", 0.0) or 0.0)
    if not any((cents, q, pan, vol)):
        rate2 = float(getattr(voice, "lfo2_rate", 0.0) or 0.0)
        cents = float(getattr(voice, "lfo2_to_filter_cents", 0.0) or 0.0)
        q = float(getattr(voice, "lfo2_to_filter_q", 0.0) or 0.0)
        pan = float(getattr(voice, "lfo2_to_pan", 0.0) or 0.0)
        vol = float(getattr(voice, "lfo2_to_volume", 0.0) or 0.0)
        if not any((cents, q, pan, vol)):
            return None
        rate = rate2 or rate
        shape = str(getattr(voice, "lfo2_shape", "") or shape).lower()
        if shape not in LFO_SHAPES:
            shape = "sine"
    if rate <= 0.0:
        return None
    return (rate, shape, cents, q, pan, vol)


def _lfo_caveats(voice, terms, report) -> None:
    if report is None or terms is None:
        return
    rate, shape, cents, q, pan, vol = terms
    dests = [
        n for n, v in (("cutoff", cents), ("Q", q), ("pan", pan), ("volume", vol)) if v
    ]
    # LFOs LEFT THE "not modelled" LIST ON 2026-09-27, so what remains
    # missing has to be said where an LFO actually runs -- a preset with none
    # should hear nothing about them at all, which is why this is not in the
    # unconditional header any more.
    report.note(
        Severity.NOT_MODELLED,
        "LFO, the parts still missing",
        "An LFO's rate, shape and depth to cutoff, Q, pan and volume are "
        "modelled. Its delay and fade-in, key-sync and tempo-sync are not, "
        "and where a voice routes a second LFO to somewhere the first one "
        "already goes, only the first is heard.",
    )
    report.note(
        Severity.FITTED,
        "LFO",
        f"An LFO at {rate:.2f} Hz ({shape}) modulates "
        f"{', '.join(dests)}. The rate, shape and depth are read from the "
        f"file; the WAVEFORM is this renderer's own and the machines' are "
        f"not measured, so the movement is right and its exact contour is "
        f"not.",
    )
    if float(getattr(voice, "lfo1_delay", 0.0) or 0.0):
        report.note(
            Severity.NOT_MODELLED,
            "LFO delay",
            "This voice delays or fades its LFO in; that is not modelled, so "
            "the movement starts at full depth from the first note.",
        )
    if getattr(voice, "lfo1_sync", None):
        report.note(
            Severity.NOT_MODELLED,
            "LFO sync",
            "This voice syncs its LFO to tempo or to key-on; neither is "
            "modelled, so its phase here is simply zero at note-on.",
        )


# ── the per-voice render ─────────────────────────────────────────────────────


def _sample_play_seconds(sample, ratio: float, render_rate: int) -> float:
    """How long it takes to run ``sample`` out at pitch ratio ``ratio``.

    Factored out of `_voice_seconds` because the one-shot fade needs the same
    number: a whole-sample voice stops where the SAMPLE stops, so that is the
    instant its level has to have reached zero by.
    """
    return (
        len(sample.data)
        / 2
        / max(getattr(sample, "channels", 1), 1)
        / ratio
        / render_rate
    )


def _voice_seconds(
    snd: voice_mod.Sounding,
    opts,
    report: AuditionReport,
    ratio: Optional[float] = None,  # noqa: UP045
) -> float:  # noqa: RUF100, UP045
    """How long this voice needs, for note spacing and tail budgeting.

    ``ratio`` is the caller's already-computed pitch ratio. Recomputing it
    here would be cheap but would give the two a way to disagree, and the
    length of a whole-sample voice is derived from it.
    """
    voice, zone, sample = snd.voice, snd.zone, snd.sample
    if sample is None:
        return opts.hold_seconds
    amp_env = getattr(voice, "amp_env", None)
    whole = bool(getattr(voice, "plays_whole_sample", False))
    if ratio is None:
        ratio = _pitch_ratio(voice, zone, sample, opts)
    if whole and ratio > 0.0:
        return (
            max(
                opts.hold_seconds,
                _sample_play_seconds(sample, ratio, opts.render_rate),
            )
            + WHOLE_SAMPLE_ROOM_S
        )
    tail = 0.0
    if amp_env is not None:
        tail = min(env_mod.release_seconds(amp_env), env_mod.AUDITION_MAX_TAIL)
    return opts.hold_seconds + tail


#: Room kept past the end of a one-shot's sample, in seconds. The sample stops
#: and the voice keeps rendering briefly, so the level has time to reach zero
#: instead of stepping there.
WHOLE_SAMPLE_ROOM_S = 0.05


def _pitch_ratio(voice, zone, sample, opts) -> float:
    if getattr(voice, "non_transpose", False):
        semis = 0
    else:
        note = getattr(opts, "_note", 60)
        semis = (
            note
            + int(getattr(zone, "transpose", 0) or 0)
            - int(getattr(zone, "root_key", 60) or 0)
        )
    cents = (
        int(getattr(zone, "coarse_tune", 0) or 0) * 100
        + int(getattr(zone, "fine_tune", 0) or 0)
        + int(getattr(sample, "fine_tune", 0) or 0)
    )
    return (
        2.0 ** (semis / 12.0)
        * 2.0 ** (cents / 1200.0)
        * float(sample.sample_rate)
        / float(opts.render_rate)
    )


def render_voice(
    snd: voice_mod.Sounding, note: int, opts, report: AuditionReport
) -> tuple[List[float], List[float], float]:  # noqa: UP006
    """Render one sounding zone. Returns ``(left, right, seconds)``.

    The caller mixes the two channels into the shared bus.
    """
    voice, zone, sample = snd.voice, snd.zone, snd.sample
    if sample is None:
        return [], [], 0.0

    src = _decode_cached(sample, report)
    if src.frames == 0:
        return [], [], 0.0

    ratio = _pitch_ratio(voice, zone, sample, opts)
    seconds = _voice_seconds(snd, opts, report, ratio)
    n_frames = max(1, int(math.ceil(seconds * opts.render_rate)))  # noqa: RUF046

    amp_env = getattr(voice, "amp_env", None)
    whole = bool(getattr(voice, "plays_whole_sample", False))
    fmt = getattr(opts, "_fmt", "")
    if fmt == "AKAI":
        report.note(
            Severity.UNMEASURED,
            "AKAI attack",
            "AKAI amp attack: the law that produced this number was retracted "
            "and nothing replaced it, so the attack is the parser's default, "
            "not the file's.",
        )
        if float(getattr(voice, "filter_env_cents", 0.0) or 0.0) != 0.0:
            report.note(
                Severity.UNMEASURED,
                "AKAI filter-envelope sustain",
                "AKAI filter-envelope sustain (SUSTN2) has never been swept "
                "or read; this preset sweeps the filter, so that stage is a "
                "default.",
            )
    if amp_env is not None and not whole:
        if getattr(amp_env, "release_rate_db_per_s", None):
            report.note(
                Severity.MEASURED,
                "release",
                "Release taken from this machine's measured slew rate (dB/s), "
                "so it does not depend on where 'silence' is.",
            )
        else:
            report.note(
                Severity.UNMEASURED,
                "release",
                "Release taken from the source's stated seconds, because this "
                "format specifies a duration — the two samplers disagree about "
                "where silence is by 60.07 dB against 97.82 and neither figure "
                "was ever measured.",
            )
    if amp_env is not None:
        attack_scale = env_mod.attack_scale_for(voice, opts.velocity)
        if (
            attack_scale is not None
            and abs(attack_scale - 1.0) > 1e-9
            and report is not None
        ):
            report.note(
                Severity.UNMEASURED,
                "velocity → amp attack",
                "The attack time was scaled by velocity, but no SHAPE between "
                "the pivot and either end has been measured on these "
                "machines; it is a plain linear interpolation.",
            )
        # A whole-sample voice's grid ends where the SAMPLE ends, not where the
        # rendered buffer does: `_voice_seconds` keeps WHOLE_SAMPLE_ROOM_S past
        # it, and the read is already returning silence there. Without this the
        # fade at the tail of the grid would land in material nobody can hear
        # and the one-frame truncation at `play_s` would stand.
        if whole:
            tail_s = max(
                _sample_play_seconds(sample, ratio, opts.render_rate)
                - opts.hold_seconds,
                0.0,
            )
        else:
            tail_s = max(seconds - opts.hold_seconds, 0.0)
        amp = env_mod.amp_envelope(
            amp_env,
            hold_s=opts.hold_seconds,
            tail_s=tail_s,
            rate=opts.render_rate,
            # `is not None`, NOT truthiness: a scale of exactly 0.0 means
            # "the attack takes zero seconds" and is a legitimate answer
            # from attack_scale_for (it is the clamped floor). The old
            # `if attack_scale` silently substituted the authored time
            # instead, so some velocities got the model's value and others
            # got a different one for no stated reason.
            attack_scale=attack_scale if attack_scale is not None else 1.0,
            whole=whole,
        )
    else:
        amp = [1.0, 1.0]

    # Filter: built once per voice, per channel.
    f_type = int(getattr(voice, "filter_type", 0) or 0)
    mode, poles = filter_mod.topology_for(f_type)
    filt = None
    filt_env = None
    filt_q = 0.707  # only read when `filt` is not None; named so the
    # numpy path can take it as a plain argument
    f_base = 0.0
    keytrack = float(getattr(voice, "filter_keytrack", 0.0) or 0.0)
    vel_cents = velocity_to_filter_cents(voice, opts.velocity)
    if mode != "bypass":  # noqa: SIM102
        if filter_mod.sections_for(poles) > 0:
            filter_mod.caveat_for_type(f_type, report)
            f_base = filter_mod.corner_to_f0(
                float(getattr(voice, "filter_cutoff", 20000.0) or 20000.0),
                poles,
                getattr(opts, "_fmt", ""),
                report,
            )
            q = filter_mod.resonance_to_q(
                float(getattr(voice, "filter_resonance", 0.0) or 0.0),
                getattr(opts, "_fmt", ""),
                report,
            )
            filt_q = q
            # TWO, NOT opts.channels. The render is internally stereo and
            # _finish() downmixes at the end, so the per-frame loop below
            # always processes two channels. Sizing this by the OUTPUT count
            # raised IndexError on any device whose preferred format is mono
            # -- a traceback in a message box, for every filtered preset.
            filt = [
                filter_mod.Cascade(
                    opts.render_rate, mode, filter_mod.sections_for(poles)
                )
                for _ in range(2)
            ]
            fenv = getattr(voice, "filter_env", None)
            fenv_cents = float(getattr(voice, "filter_env_cents", 0.0) or 0.0)
            if fenv is not None and fenv_cents != 0.0:
                filt_env = env_mod.filter_envelope(
                    fenv,
                    hold_s=opts.hold_seconds,
                    tail_s=max(seconds - opts.hold_seconds, 0.0),
                    rate=opts.render_rate,
                )
            if keytrack != 0.0:
                report.note(
                    Severity.UNMEASURED,
                    "filter keytrack pivot",
                    "Filter keytracking hinges on which key, and no format "
                    f"here records it; MIDI {KEYTRACK_PIVOT} was assumed. A "
                    "wrong pivot is inaudible on the note you test and wrong "
                    "across the keyboard.",
                )

    # Level and pan.
    db = (
        float(getattr(getattr(opts, "_preset", None), "volume", 0.0) or 0.0)
        + float(getattr(zone, "volume", 0.0) or 0.0)
        + velocity_to_volume_db(voice, opts.velocity, report)
    )
    gain = _db_to_gain_floored(db)
    pan = (
        float(getattr(getattr(opts, "_preset", None), "pan", 0.0) or 0.0)
        + float(getattr(zone, "pan", 0.0) or 0.0)
        + _velocity_to_pan(voice, opts.velocity)
        + _key_to_pan(voice, note)
    )
    gl, gr = _constant_power(pan)

    lfo = _lfo_terms(voice)
    _lfo_caveats(voice, lfo, report)

    loop_type = src.loop_type
    forward_loop = loop_type == 1
    forward_rel = loop_type == 3
    n_ch = len(src.channels)

    gamma = opts.render_rate / env_mod.CONTROL_RATE
    block = max(1, int(round(gamma)))  # noqa: RUF046
    hold_frame = int(opts.hold_seconds * opts.render_rate)

    if _np is not None:
        return _render_voice_np(
            src,
            n_frames,
            ratio,
            amp,
            gamma,
            block,
            hold_frame,
            filt,
            filt_env,
            f_base,
            keytrack,
            vel_cents,
            voice,
            note,
            opts,
            report,
            gain,
            gl,
            gr,
            filt_q,
            forward_loop,
            forward_rel,
            n_ch,
            seconds,
            pan,
        )

    left: List[float] = [0.0] * n_frames  # noqa: UP006
    right: List[float] = [0.0] * n_frames  # noqa: UP006
    pos = 0.0
    clamped = False
    rate_hz = opts.render_rate

    for f in range(n_frames):
        looping = forward_loop or (forward_rel and f < hold_frame)
        g = env_mod.interpolate(amp, f / gamma)
        if lfo is not None:
            # Phase in turns from note-on. Zero at the first frame: no format
            # here tells us where its oscillator was, and starting every note
            # at the same place is at least reproducible.
            wave = lfo_value(lfo[1], f * lfo[0] / rate_hz)
            if lfo[5]:
                g *= _db_to_gain_floored(wave * lfo[5] * LFO_VOLUME_DB)
        else:
            wave = 0.0

        if filt is not None:  # noqa: SIM102
            if f % block == 0:
                t_frac = f / gamma
                f_cents = filt_env and env_mod.interpolate(filt_env, t_frac) or 0.0
                f0 = (
                    f_base
                    * 2.0 ** (keytrack * (note - KEYTRACK_PIVOT) / 12.0)
                    * 2.0 ** (vel_cents / 1200.0)
                    * 2.0 ** (wave * lfo[2] / 1200.0 if lfo else 0.0)
                    * 2.0
                    ** (
                        f_cents
                        * float(getattr(voice, "filter_env_cents", 0.0) or 0.0)
                        / 1200.0
                    )
                )
                lo, hi = filter_mod.f0_band(opts.render_rate)
                if (f0 < lo or f0 > hi) and not clamped:
                    clamped = True
                    report.note(
                        Severity.FITTED,
                        "filter cutoff clamped",
                        f"This voice's cutoff left the band this renderer can "
                        f"run ({lo:.0f} Hz to {hi:.0f} Hz at {opts.render_rate} "
                        f"Hz) once keytracking, velocity and the filter "
                        f"envelope were applied, and was held at the edge. The "
                        f"machine has its own ceiling and it is not this one.",
                    )
                f0 = min(max(f0, lo), hi)
                q_now = filt_q
                if lfo is not None and lfo[3]:
                    # Q rides the same oscillator. Clamped well away from the
                    # self-oscillation the SVF would show at very high Q,
                    # which is ours and not the machine's.
                    q_now = min(max(filt_q * (1.0 + wave * lfo[3]), 0.5), 16.0)
                for c in filt:
                    c.set(f0, q_now)

        if lfo is not None and lfo[4]:
            gl_f, gr_f = _constant_power(min(max(pan + wave * lfo[4], -1.0), 1.0))
        for c in range(2):
            if c < n_ch:
                x = _read(src, src.channels[c], pos, looping)
            else:
                x = _read(src, src.channels[0], pos, looping)
            if filt is not None:
                x = filt[c].process(x)
            v = x * g * gain
            if c == 0:
                left[f] += v * (gl_f if lfo is not None and lfo[4] else gl)
            else:
                right[f] += v * (gr_f if lfo is not None and lfo[4] else gr)
        pos += ratio

    return left, right, seconds


def _render_voice_np(
    src,
    n_frames,
    ratio,
    amp,
    gamma,
    block,
    hold_frame,
    filt,
    filt_env,
    f_base,
    keytrack,
    vel_cents,
    voice,
    note,
    opts,
    report,
    gain,
    gl,
    gr,
    filt_q,
    forward_loop,
    forward_rel,
    n_ch,
    seconds,
    pan=0.0,
):
    """The same voice, with the per-frame work done as whole arrays.

    WHAT IS NOT VECTORISED, AND CANNOT BE: the filter. It is an IIR -- each
    output sample depends on the state the previous one left -- so there is no
    array form of it, and `filter.py` says so where the class is defined. What
    this removes is everything AROUND it: the interpolated read, the envelope
    lookup and the gain/pan multiply, which were three Python calls per frame
    per channel.

    So an unfiltered voice becomes a handful of C operations, and a filtered
    one still pays a Python loop -- but only for the filter, over a value it
    no longer has to compute.

    NOT BIT-IDENTICAL to the loop above, on purpose and measurably: positions
    here are `arange(n) * ratio` where the loop ACCUMULATES `pos += ratio`, so
    the two drift apart by the accumulated rounding of a few hundred thousand
    additions -- about 1e-10 of a frame, which is far below one LSB but not
    zero. manual_audition_numpy_tolerance measures the whole difference and
    holds it to 80 dB down and 4 LSB.
    """
    positions = _np.arange(n_frames, dtype="float64") * ratio
    t_ctrl = _np.arange(n_frames, dtype="float64") / gamma
    if forward_loop:
        looping = _np.ones(n_frames, dtype=bool)
    elif forward_rel:
        looping = _np.arange(n_frames) < hold_frame
    else:
        looping = _np.zeros(n_frames, dtype=bool)

    env = _env_block_np(amp, t_ctrl)

    # The same LFO the pure path applies, as arrays. `lfo_value` is scalar by
    # design (the block loop below calls it once per control block), so the
    # per-frame waveform is built here with the identical arithmetic rather
    # than by calling it 400 000 times.
    lfo = _lfo_terms(voice)
    wave = None
    if lfo is not None:
        phase = _np.arange(n_frames, dtype="float64") * lfo[0] / opts.render_rate
        wave = _lfo_wave(lfo[1], phase)
        if lfo[5]:
            env = env * _np.power(10.0, (wave * lfo[5] * LFO_VOLUME_DB) / 20.0)

    chans = []
    for c in range(2):
        # Mono material is read from channel 0 for both hands, which is what
        # the `c < n_ch` guard below used to do by picking the list.
        idx = c if c < n_ch else 0
        chans.append(_read_block(src, idx, positions, looping))

    if filt is not None:
        # Coefficients change once per control block; step them there and run
        # the sections over the block. The per-sample work left is the filter
        # and nothing else.
        clamped = False
        env_cents = float(getattr(voice, "filter_env_cents", 0.0) or 0.0)
        lo_hz, hi_hz = filter_mod.f0_band(opts.render_rate)
        outs = [chans[0].tolist(), chans[1].tolist()]
        for start in range(0, n_frames, block):
            stop = min(start + block, n_frames)
            t_frac = start / gamma
            f_cents = env_mod.interpolate(filt_env, t_frac) if filt_env else 0.0
            # The block's own LFO sample, taken at its first frame -- which is
            # exactly what the pure path does with `f % block == 0`.
            w = float(wave[start]) if wave is not None else 0.0
            f0 = (
                f_base
                * 2.0 ** (keytrack * (note - KEYTRACK_PIVOT) / 12.0)
                * 2.0 ** (vel_cents / 1200.0)
                * 2.0 ** (w * lfo[2] / 1200.0 if lfo else 0.0)
                * 2.0 ** (f_cents * env_cents / 1200.0)
            )
            if (f0 < lo_hz or f0 > hi_hz) and not clamped:
                clamped = True
                report.note(
                    Severity.FITTED,
                    "filter cutoff clamped",
                    f"This voice's cutoff left the band this renderer can "
                    f"run ({lo_hz:.0f} Hz to {hi_hz:.0f} Hz at "
                    f"{opts.render_rate} Hz) once keytracking, velocity and "
                    f"the filter envelope were applied, and was held at the "
                    f"edge. The machine has its own ceiling and it is not "
                    f"this one.",
                )
            f0 = min(max(f0, lo_hz), hi_hz)
            q_now = filt_q
            if lfo is not None and lfo[3]:
                q_now = min(max(filt_q * (1.0 + w * lfo[3]), 0.5), 16.0)
            for c in filt:
                c.set(f0, q_now)
            for c in (0, 1):
                step = filt[c].process
                buf = outs[c]
                for i in range(start, stop):
                    buf[i] = step(buf[i])
        chans = [_np.asarray(outs[0]), _np.asarray(outs[1])]

    scaled = env * gain
    if lfo is not None and lfo[4]:
        # Constant-power pan, per frame, vectorised: the same
        # cos/sin(pi/4 * (p + 1)) the scalar `_constant_power` computes.
        pan_now = _np.clip(pan + wave * lfo[4], -1.0, 1.0)
        ang = (pan_now + 1.0) * (math.pi / 4.0)
        left = chans[0] * scaled * _np.cos(ang)
        right = chans[1] * scaled * _np.sin(ang)
    else:
        left = chans[0] * scaled * gl
        right = chans[1] * scaled * gr
    return left, right, seconds


def _lfo_wave(shape: str, phase):
    """`lfo_value` over an array of phases, same arithmetic per shape.

    NUMPY-ONLY, like `_read_block`: it used to be `_lfo_wave_np` with a
    pure-Python branch below its OWN `_np.floor(phase)` on line two, so the
    arm could not run -- with the accelerator off the first statement of the
    body already raised AttributeError. The suffix is gone so the name stops
    implying a paired implementation; `_lfo_wave_np` below is an alias for the
    one test that imports it by that name, not a second implementation.
    """
    ph = phase - _np.floor(phase)
    if shape == "square":
        return _np.where(ph < 0.5, 1.0, -1.0)
    if shape == "sawtooth":
        return 2.0 * ph - 1.0
    if shape == "triangle":
        return _np.where(ph < 0.5, 4.0 * ph - 1.0, 3.0 - 4.0 * ph)
    if shape == "random":
        step = _np.floor(phase).astype(_np.int64)
        x = (step * 1103515245 + 12345) & 0x7FFFFFFF
        return (x / float(0x3FFFFFFF)) - 1.0
    return _np.sin(2.0 * _np.pi * ph)


#: The spelling `tests/manual_audition_lfo.py` calls this by. tests/ is
#: gitignored by decision and is not part of this change, so the rename leaves
#: its old name reachable; it is the SAME function object, not a shim, and
#: nothing in the product reads it.
_lfo_wave_np = _lfo_wave


# ── rendering events in parallel ─────────────────────────────────────────────
#
# Each EVENT -- one note or one chord -- is independent: it has its own slice
# of the output and its own voices. Pure Python is GIL-bound, so threads buy
# nothing for the DSP loop; processes do.
#
# FORK ONLY, AND THAT IS THE WHOLE DESIGN. The child inherits the parsed bank
# and the decoded samples through copy-on-write, so nothing large is pickled
# on the way IN -- only the rendered buffers come back. Under `spawn` (Windows,
# and macOS since 3.8) the child starts empty and the bank would have to be
# pickled per task, which for a 128 MB multisample costs more than the render.
# So there is no spawn path: those platforms render serially, which is what
# they do today.
#
# NOTHING OUTLIVES THE RENDER. The pool is context-managed, so it is torn down
# whether the render returns, raises or is abandoned. Cancelling an audition
# does NOT kill it early -- the generation counter drops the result on arrival
# and the workers finish into nothing -- which is the same bargain the single
# threaded path already makes, and is why cancel is honest about being "stop
# caring" rather than "stop computing".

#: Set by the pool's initializer in each child; read by `_render_event_task`
#: through copy-on-write. Never sent anywhere, and never written by the parent
#: between the fork and the pool coming up -- see `_render_events_parallel`.
_FORK_PLANS = None
_FORK_ARGS = None


def _fork_init(plans, args):
    """Install the parent's snapshot in this child.

    `initargs` is passed directly to the child under fork, so nothing large is
    pickled and the copy-on-write design is preserved. It replaces writing the
    two module globals from the PARENT before `ctx.Pool(...)`, which was a race:
    two auditions really can render at once (this module says so at
    `render.py:215`, and `ui/main_window.py:_silence_previous_auditions`
    documents it), and with an unlocked module global a second render's
    assignment lands in the first render's children. Measured with two banks
    and a widened assignment->fork window: bank A's children inherited B's
    plans and rendered B's first three notes into A's bus, silently. Worse,
    plan COUNT mismatches raise IndexError in the child, and the fallback at
    `except Exception: return False` then re-renders the whole thing serially
    -- a silent 3x slowdown on top of the wrong audio.
    """
    global _FORK_PLANS, _FORK_ARGS
    _FORK_PLANS = plans
    _FORK_ARGS = args


#: Below this many output frames, forking costs more than it saves.
PARALLEL_MIN_FRAMES = 3 * 44100


def parallel_supported() -> bool:
    """Whether this machine can render events in parallel."""
    try:
        import multiprocessing

        return "fork" in multiprocessing.get_all_start_methods()
    except Exception:  # pragma: no cover - depends on the platform  # noqa: BLE001
        return False


def _render_event_task(index: int):
    """Render one event in a forked child. Returns (left, right, caveats).

    Caveats come back as a list and are merged by the parent IN EVENT ORDER,
    so the report reads the same as it would have rendered serially --
    `AuditionReport` dedupes on (severity, subject) and keeps insertion order,
    so a different merge order would be a different report.
    """
    voices, _span = _FORK_PLANS[index]
    # This child's OWN room, not the length of the whole audition. See
    # `_render_events_parallel`: the whole bus used to be handed here, so every
    # child allocated and pickled back the bus rather than its own event.
    total, mix_np = _FORK_ARGS[0][index], _FORK_ARGS[1]
    sub = AuditionReport()
    acc_l, acc_r = None, None
    for note, sounds, o in voices:
        for s in sounds:
            left, right, _secs = render_voice(s, note, o, sub)
            acc_l, acc_r = _accumulate(acc_l, acc_r, left, right, total, mix_np)
    return acc_l, acc_r, list(sub)


def _accumulate(acc_l, acc_r, left, right, total, mix_np):
    """Add one voice into an event's own buffer, trimmed to `total`."""
    n = min(len(left), total)
    if n <= 0:
        return acc_l, acc_r
    if mix_np:
        if acc_l is None:
            acc_l = _np.zeros(total, dtype="float64")
            acc_r = _np.zeros(total, dtype="float64")
        acc_l[:n] += _np.asarray(left[:n], dtype="float64")
        acc_r[:n] += _np.asarray(right[:n], dtype="float64")
        return acc_l, acc_r
    if acc_l is None:
        acc_l = [0.0] * total
        acc_r = [0.0] * total
    for i in range(n):
        acc_l[i] += left[i]
        acc_r[i] += right[i]
    return acc_l, acc_r


# ── the pipeline ─────────────────────────────────────────────────────────────


def render(bank, preset, prov, opts, progress=None) -> "Rendering":  # noqa: F821, UP037
    """Render a whole audition. See ``audition.render`` for the public API``.

    ``progress(percent, text)`` is called as the render advances, if given.
    It runs on WHATEVER THREAD render() is on -- a worker, in the app -- so a
    Qt caller must pass something that marshals, which is what emitting a
    signal from a GUI-thread object does. Called once per event rather than
    per voice: a chord of three notes over six layers would otherwise emit
    eighteen times for one line of text nobody can read that fast.
    """

    def _say(pct, text):
        if progress is not None:
            try:
                progress(int(pct), text)
            except Exception:  # noqa: BLE001, S110
                pass  # a broken progress hook must not lose a render

    # Late imports: __init__ owns the dataclasses, and imports this module.
    from . import Rendering
    from . import as_events as _as_events

    report = AuditionReport()
    _seed_report(report, prov, opts)
    # Which engine actually ran. Costs nothing when the debug log is off
    # (calllog.note returns immediately), and when it is on it is the first
    # thing you want to know about a render that sounded wrong or took too
    # long -- the two paths are allowed to differ now, so "which one" is a
    # question the log has to be able to answer after the fact.
    calllog.note(
        "audition_render",
        renderer=renderer_description(),
        numpy=numpy_in_use(),
        parallel=parallel_supported(),
        cores=os.cpu_count(),
        rate=opts.render_rate,
        channels=opts.channels,
    )
    fmt = getattr(prov, "format", "") or ""
    max_sounding = voice_mod.ceiling_for(fmt)

    # Pre-compute the per-EVENT spans so the bus can be sized in one pass.
    # An event is one note or a chord; every note in a chord starts at the
    # same frame, and the event is as long as its longest voice.
    events = _as_events(opts.notes)
    _say(2, "Reading the preset\u2026")
    plans = []
    for n_ev, ev in enumerate(events, 1):
        hold = ev.held_for(opts.hold_seconds)
        span = hold
        voices = []
        for note in ev.notes:
            o = _RenderOpts(opts, note=note, preset=preset, fmt=fmt, hold_seconds=hold)
            sounds = voice_mod.sounding(
                preset,
                note,
                opts.velocity,
                bank=bank,
                report=report,
                max_sounding=max_sounding,
            )
            if not sounds:
                report.note(
                    Severity.NOT_MODELLED,
                    "silent note",
                    f"Nothing sounds for MIDI note {note} at velocity "
                    f"{opts.velocity}: no zone covers it. The keymap is "
                    f"quieter than the note list.",
                )
            else:
                span = max(span, *(_voice_seconds(s, o, report) for s in sounds))
            voices.append((note, sounds, o))
        plans.append((voices, span))
        # 5..45%: planning walks every zone and is the half that scales with
        # the preset's size, which is why a big multisample feels slow here
        # long before a sample has been read.
        _say(
            5 + 40 * n_ev / max(len(events), 1),
            f"Preparing note {n_ev} of {len(events)}\u2026",
        )

    if any(ev.is_chord for ev in events):
        report.note(
            Severity.NOT_MODELLED,
            "chords",
            "Notes sounded together are summed, each with its own full set of "
            "layers. The sampler's voice budget and its stealing order are "
            "not modelled, so a chord that overruns the machine's polyphony "
            "will be heard here complete and on the hardware truncated.",
        )

    total_frames = 0
    offsets = []
    for _voices, span in plans:
        offsets.append(total_frames)
        total_frames += int(math.ceil((span + opts.gap_seconds) * opts.render_rate))  # noqa: RUF046
    # Trailing gap is not silence worth keeping in the file.
    total_frames = max(
        1,
        total_frames - int(math.ceil(opts.gap_seconds * opts.render_rate)),  # noqa: RUF046
    )  # noqa: RUF046, RUF100

    use_np = _np is not None
    if use_np:
        out_l = _np.zeros(total_frames, dtype="float64")
        out_r = _np.zeros(total_frames, dtype="float64")
    else:
        out_l = [0.0] * total_frames
        out_r = [0.0] * total_frames

    parallel = (
        len(plans) > 1 and total_frames >= PARALLEL_MIN_FRAMES and parallel_supported()
    )
    done = False
    if parallel:
        done = _render_events_parallel(
            plans, offsets, total_frames, out_l, out_r, report, use_np, _say
        )
    if not done:
        for n_ev, ((voices, _span), start) in enumerate(zip(plans, offsets), 1):
            _say(
                45 + 50 * (n_ev - 1) / max(len(plans), 1),
                f"Rendering note {n_ev} of {len(plans)}\u2026",
            )
            for note, sounds, o in voices:
                for s in sounds:
                    left, right, _secs = render_voice(s, note, o, report)
                    _mix_into(out_l, out_r, left, right, start, total_frames, use_np)

    _say(96, "Mixing\u2026")
    peak, pcm = _finish(out_l, out_r, opts)
    if peak * _db_to_gain_floored(opts.headroom_db) > 1.0:
        report.note(
            Severity.NOT_MODELLED,
            "output level",
            "This preset's layers sum above full scale; the audition is "
            "attenuated, and the hardware's own output stage is not modelled.",
        )
    return Rendering(
        pcm=pcm,
        rate=opts.render_rate,
        channels=opts.channels,
        peak_before_limit=peak,
        report=report,
        seconds=total_frames / opts.render_rate,
    )


def _mix_into(out_l, out_r, left, right, start, total, use_np) -> None:
    """Add one rendered buffer into the bus at `start`, trimmed to the end."""
    n = min(len(left), total - start)
    if n <= 0:
        return
    if use_np:
        out_l[start : start + n] += _np.asarray(left[:n], dtype="float64")
        out_r[start : start + n] += _np.asarray(right[:n], dtype="float64")
        return
    for i in range(n):
        out_l[start + i] += left[i]
        out_r[start + i] += right[i]


def _render_events_parallel(
    plans, offsets, total_frames, out_l, out_r, report, use_np, say
) -> bool:
    """Render each event in a forked child. False means "do it serially".

    Results are collected IN EVENT ORDER, not completion order, so the mix and
    the report are the same whatever order the children finish in -- float
    addition is not associative, and an audition that differed run to run
    would be useless for the comparison it exists to support.

    EACH CHILD ALLOCATES ONLY ITS OWN EVENT. `total_frames` here is the length
    of the WHOLE audition, which used to be handed to every child as the size
    of the buffer it accumulates into -- so N children each allocated
    `2 x total x 8` bytes, each was pickled back through a pipe, and the parent
    held all N of them at once. Measured on 24 events over a 90 s bus with 4
    workers: **1.52 GB allocated, peak child RSS 1590 MB, against the 52 MB the
    events actually need** -- a 30x blow-up scaling as O(events x bus length),
    and an OOM risk on a long note list. When the pool then failed, the
    fallback below silently re-rendered the whole thing serially.

    `spans[i]` is exactly the room event *i* occupies, and it was already
    computed on the next line and discarded (`# noqa: F841`). Passing it costs
    nothing: `_mix_into` already trims short buffers
    (`n = min(len(left), total - start)`), so nothing downstream changes.
    """
    try:
        import multiprocessing

        ctx = multiprocessing.get_context("fork")
    except Exception:  # pragma: no cover - platform dependent  # noqa: BLE001
        return False
    spans = [min(total_frames - s, total_frames) for s in offsets]
    workers = min(len(plans), (os.cpu_count() or 2))
    try:
        # Context-managed: the pool is gone when this block ends, whether it
        # returned, raised, or the caller stopped caring about the result.
        #
        # `initializer`/`initargs` rather than writing the two module globals
        # from this thread before forking: see `_fork_init`. The parent never
        # touches them at all now, so a concurrent audition cannot land in this
        # pool's children.
        with ctx.Pool(
            processes=workers,
            initializer=_fork_init,
            initargs=(plans, (spans, use_np)),
        ) as pool:
            results = pool.map(_render_event_task, range(len(plans)))
    except Exception:  # noqa: BLE001
        # A child that dies takes the audition with it otherwise. Falling back
        # to the serial path costs time and nothing else.
        return False
    for n_ev, ((left, right, caveats), start) in enumerate(zip(results, offsets), 1):
        say(
            45 + 50 * (n_ev - 1) / max(len(plans), 1),
            f"Mixing note {n_ev} of {len(plans)}\u2026",
        )
        for c in caveats:
            report.add(c)
        if left is None:
            continue
        _mix_into(out_l, out_r, left, right, start, total_frames, use_np)
    return True


def _finish(out_l: List[float], out_r: List[float], opts) -> tuple[float, bytes]:  # noqa: UP006
    headroom = _db_to_gain_floored(opts.headroom_db)
    # Report the pre-headroom peak: a quiet audition the user attributes to the
    # preset is a lie about the preset. The caller checks it against headroom.
    #
    # MEASURED BEFORE THIS WAS BRANCHED: a Python `for v in out_l: peak = max(
    # peak, abs(v))` over a numpy array runs element by element on numpy
    # scalars, and cost 85.9 ms per 200 000 frames against 1.4 ms for
    # `_np.abs(...).max()` -- 60x. On a real 8.5 s stereo render `_finish` was
    # 0.610 s of the 1.314 s total, ~0.27 s of it just this scan, and it ran
    # BEFORE the branch below so numpy never helped. Same value either way:
    # `max(abs(v))` over the concatenation is the same quantity, and the pure
    # path keeps the same expression it always had.
    if _np is not None:
        peak = float(
            max(
                _np.abs(out_l).max() if len(out_l) else 0.0,
                _np.abs(out_r).max() if len(out_r) else 0.0,
            )
        )
    else:
        peak = max(
            max((abs(v) for v in out_l), default=0.0),
            max((abs(v) for v in out_r), default=0.0),
        )
    if _np is not None:
        # Mirror the pure path EXACTLY: same soft-limit above full scale, same
        # 32768.0 scaling and truncation, same clip. An accelerator that
        # changed the audio depending on whether numpy is installed would be a
        # silent format mismatch of the kind risk 8 warns about.
        a = _soft_limit_np(_np.asarray(out_l) * headroom)
        b = _soft_limit_np(_np.asarray(out_r) * headroom)
        if opts.channels == 1:
            mono = _soft_limit_np((a + b) * 0.5)
            return peak, _np.clip(mono * 32768.0, -32768.0, 32767.0).astype(
                "<i2"
            ).tobytes()
        inter = _np.empty(a.size * 2, dtype="<i2")
        inter[0::2] = _np.clip(a * 32768.0, -32768.0, 32767.0).astype("<i2")
        inter[1::2] = _np.clip(b * 32768.0, -32768.0, 32767.0).astype("<i2")
        return peak, inter.tobytes()
    flat: List[float] = []  # noqa: UP006
    for i in range(len(out_l)):
        val_l = _soft_limit(out_l[i] * headroom)
        val_r = _soft_limit(out_r[i] * headroom)
        if opts.channels == 1:
            flat.append(_soft_limit((val_l + val_r) * 0.5))
        else:
            flat.append(val_l)
            flat.append(val_r)
    return peak, mpc2emu_bridge.resampler._float_to_pcm(flat)


def _soft_limit_np(a):
    """The vectorised ``_soft_limit``, bit-for-bit the same mapping."""
    mag = _np.abs(a)
    over = mag > _LIMIT_KNEE
    if not bool(over.any()):
        return a
    out = a.copy()
    m = mag[over]
    k = (m - _LIMIT_KNEE) / (1.0 - _LIMIT_KNEE)
    out[over] = _np.sign(a[over]) * (_LIMIT_KNEE + (1.0 - _LIMIT_KNEE) * _np.tanh(k))
    return out


#: Where the knee starts. Below this the mapping is exactly 1:1, so ordinary
#: material is untouched; above it the curve is continuous, monotonic and
#: asymptotic to full scale.
_LIMIT_KNEE = 0.7


def _soft_limit(x: float) -> float:
    """Soften what is over full scale, rather than flattening it.

    THE OLD EXPRESSION WAS A HARD CLIP WEARING A SOFT NAME: `a / (1 + (a - 1))`
    simplifies to `a / a`, i.e. exactly 1.0 for every a > 1. So everything over
    full scale came out flat-topped -- the harsh distortion `headroom_db` was
    added to avoid -- while the report told the user the audition had merely
    been "attenuated". Caught by an external review 2026-09-21.
    """
    a = abs(x)
    if a <= _LIMIT_KNEE:
        return x
    over = (a - _LIMIT_KNEE) / (1.0 - _LIMIT_KNEE)
    return math.copysign(_LIMIT_KNEE + (1.0 - _LIMIT_KNEE) * math.tanh(over), x)


def _seed_report(report: AuditionReport, prov, opts) -> None:
    """The statements that are true of every audition, said once."""
    fmt = getattr(prov, "format", "") or "unknown"
    origin = getattr(prov, "origin", "") or ""
    if fmt == "KRZ":
        report.note(
            Severity.MEASURED,
            "pitch and tuning",
            "K2000 tuning is 256 units per semitone and its cutoff is already "
            "a filter natural frequency (f0), so neither was converted.",
        )
    _e4b_zplane_caveats(report, prov)
    report.note(
        Severity.NOT_MODELLED,
        "output stage",
        "Chorus, delay, and the machines' own output stages, converters and "
        "anti-alias filters are not modelled.",
    )
    report.note(
        Severity.NOT_MODELLED,
        "interpolation",
        "The interpolation you are hearing is ours, not the sampler's — an "
        "E4XT and an S3000XL each have their own artefacts and neither is "
        "modelled.",
    )
    if origin:
        report.note(
            Severity.MEASURED,
            "source",
            f"Rendered from mpc2emu's parsed model of {origin} ({fmt}).",
        )


#: E4XT vpar[58] values whose filter has no XPM equivalent, and so was
#: collapsed onto XPM "Low 4" by parsers/e4b_parser.py. Reading the byte back
#: is the only place the distinction still exists -- without it a phaser and a
#: 4-pole lowpass are the same object, and audition plays a different
#: instrument while saying nothing (spec §6.2, risk 1).
_E4B_ZPLANE_BYTES = {
    0x40: "Phaser 1",
    0x41: "Phaser 2",
    0x42: "Bat Phaser",
    0x48: "Flanger Lite",
    0x60: "Dual EQ Morph",
    0x61: "2EQ+Lowpass Morph",
    0x62: "2EQMorph+Expression",
    0x68: "Peak/Shelf Morph",
}


def _e4b_zplane_caveats(report: AuditionReport, prov) -> None:
    bytes_by_voice = getattr(prov, "e4b_filter_bytes", None)
    if not bytes_by_voice:
        return
    names = sorted(
        {
            _E4B_ZPLANE_BYTES[b]
            for b in bytes_by_voice.values()
            if b in _E4B_ZPLANE_BYTES
        }
    )
    for name in names:
        report.note(
            Severity.NOT_MODELLED,
            f"E4B Z-plane filter ({name})",
            f"This voice's filter is a {name}. The audition plays a 4-pole "
            f"lowpass in its place, because nothing here models a Z-plane "
            f"filter.",
        )


# ── small mutable render-options view ────────────────────────────────────────
#
# AuditionOptions is frozen and deliberately carries only what the UI asks
# for. The renderer needs the current note, the preset and the source format
# at the point of use; rather than widen the public dataclass, they ride in
# this shallow wrapper, which delegates everything else to the frozen object.


class _RenderOpts:
    __slots__ = ("_opts", "_note", "_preset", "_fmt", "_hold")  # noqa: RUF023

    def __init__(
        self, opts, *, note: int = 60, preset=None, fmt: str = "", hold_seconds=None
    ):
        object.__setattr__(self, "_opts", opts)
        object.__setattr__(self, "_note", note)
        object.__setattr__(self, "_preset", preset)
        object.__setattr__(self, "_fmt", fmt)
        object.__setattr__(self, "_hold", hold_seconds)

    @property
    def hold_seconds(self) -> float:
        """The event's own hold, or the list default.

        Overridden HERE rather than at each of the seven read sites, because
        the envelope, the filter envelope, the tail budget and the note
        spacing all have to agree about where note-off is. Two of them
        disagreeing is a click at the release, which is the kind of defect
        this whole feature exists to hear.
        """
        hold = object.__getattribute__(self, "_hold")
        if hold is not None:
            return hold
        return object.__getattribute__(self, "_opts").hold_seconds

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_opts"), name)
