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

import math
from collections import OrderedDict
from typing import List, Optional, Sequence

from .. import mpc2emu_bridge
from . import envelope as env_mod
from . import filter as filter_mod
from . import voice as voice_mod
from .caveats import AuditionReport, Severity

try:  # accelerator only -- never a gate
    import numpy as _np
except ImportError:  # pragma: no cover - depends on the machine
    _np = None

#: Velocity curve names, matching models.common's constants. Duplicated as
#: plain strings so this module can be imported without mpc2emu for a test.
_VELOCITY_CURVE_DB_LINEAR = "db-linear"
_VELOCITY_CURVE_AMPLITUDE_LINEAR = "amplitude-linear"

KEYTRACK_PIVOT = 60

_HARD_FLOOR_DB = -96.0


def _db_to_gain(db: float) -> float:
    return 10.0 ** (max(db, _HARD_FLOOR_DB) / 20.0)


class _Source:
    """A sample decoded once, with its per-channel float lists.

    Decoding costs more than the parse and repeats per note, so the caller
    caches these by ``(id(bank), name)``.
    """

    __slots__ = ("channels", "rate", "frames", "loop_start", "loop_end", "loop_type")

    def __init__(self, channels: List[List[float]], rate: int,
                 loop_start: int, loop_end: int, loop_type: int):
        self.channels = channels
        self.rate = rate
        self.frames = len(channels[0]) if channels else 0
        self.loop_start = loop_start
        self.loop_end = loop_end
        self.loop_type = loop_type


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
        arr = _np.frombuffer(data, dtype="<i2").astype("float64") / 32768.0
        if n_ch == 2:
            arr = arr[: (len(arr) // 2) * 2]
            channels = [arr[0::2].tolist(), arr[1::2].tolist()]
        else:
            channels = [arr.tolist()]
    else:
        flat = mpc2emu_bridge.resampler._pcm_to_float(data)
        if n_ch == 2:
            channels = [flat[0::2], flat[1::2]]
        else:
            channels = [flat]
    loop_start = int(getattr(sample, "loop_start", 0) or 0)
    loop_end = int(getattr(sample, "loop_end", 0) or 0)
    loop_type = int(getattr(sample, "loop_type", 0) or 0)
    return _Source(channels, int(sample.sample_rate), loop_start, loop_end,
                   loop_type)


def _decode_for_render(sample, report: AuditionReport) -> _Source:
    """Bake a ping-pong loop to forward *before* decoding (spec §5.2).

    ``loop_renderer.bake_alternating_loop`` returns a new SampleData for an
    ALTERNATING source and the same object otherwise, so this is a no-op for
    every other loop type.
    """
    if int(getattr(sample, "loop_type", 0) or 0) == 2:  # LoopType.ALTERNATING
        try:
            sample = mpc2emu_bridge.loop_renderer.bake_alternating_loop(sample)
        except Exception:
            pass
    return _decode(sample)


#: Decoded samples, keyed by id(). Bounded by decoded bytes: decoding costs
#: more than the parse and repeats per note, but a KRZ bank carries tens of MB
#: of PCM and holding every sample of every preset would be worse than the
#: decode it saves. The sample object is kept ALIVE in the value, so its id
#: cannot be reused by a later allocation while it is cached.
_SAMPLE_CACHE: "OrderedDict[int, tuple]" = OrderedDict()
_SAMPLE_CACHE_MAX_BYTES = 64 * 1024 * 1024
_sample_cache_bytes = 0


def _decode_cached(sample, report: AuditionReport) -> _Source:
    global _sample_cache_bytes
    key = id(sample)
    got = _SAMPLE_CACHE.get(key)
    if got is not None and got[0] is sample:
        _SAMPLE_CACHE.move_to_end(key)
        return got[1]
    src = _decode_for_render(sample, report)
    size = src.frames * max(1, len(src.channels)) * 8  # float64 bytes
    _SAMPLE_CACHE[key] = (sample, src)
    _sample_cache_bytes += size
    while _sample_cache_bytes > _SAMPLE_CACHE_MAX_BYTES and len(_SAMPLE_CACHE) > 1:
        _old_key, (_old_sample, old_src) = _SAMPLE_CACHE.popitem(last=False)
        _sample_cache_bytes -= old_src.frames * max(1, len(old_src.channels)) * 8
    return src


def clear_sample_cache() -> None:
    global _sample_cache_bytes
    _SAMPLE_CACHE.clear()
    _sample_cache_bytes = 0


def _read(src: _Source, ch: List[float], pos: float, looping: bool) -> float:
    """One interpolated read at fractional source-frame ``pos``."""
    n = len(ch)
    if n == 0:
        return 0.0
    if looping and src.loop_end > src.loop_start:
        ls, le = src.loop_start, src.loop_end
        span = le - ls + 1
        rel = (pos - ls) % span
        if rel >= (span - 1):
            # At the wrap: blend loop_end -> loop_start, the forward-loop seam.
            frac = rel - (span - 1)
            return ch[le] * (1.0 - frac) + ch[ls] * frac
        pos = ls + rel
    i = int(pos)
    if i >= n - 1:
        return 0.0 if not looping else ch[n - 1]
    if i < 0:
        return 0.0
    frac = pos - i
    return ch[i] + (ch[i + 1] - ch[i]) * frac


# ── level / pan / velocity ───────────────────────────────────────────────────

def velocity_to_volume_db(voice, velocity: int,
                          report: Optional[AuditionReport] = None) -> float:
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
                Severity.UNMEASURED, "velocity → volume",
                "This voice's velocity-to-volume amount was not recorded by "
                "the source, so notes play at a neutral level rather than a "
                "velocity-scaled one.")
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


# ── the per-voice render ─────────────────────────────────────────────────────

def _voice_seconds(snd: voice_mod.Sounding, opts, report: AuditionReport) -> float:
    """How long this voice needs, for note spacing and tail budgeting."""
    voice, zone, sample = snd.voice, snd.zone, snd.sample
    if sample is None:
        return opts.hold_seconds
    amp_env = getattr(voice, "amp_env", None)
    whole = bool(getattr(voice, "plays_whole_sample", False))
    ratio = _pitch_ratio(voice, zone, sample, opts)
    if whole and ratio > 0.0:
        play_s = (len(sample.data) / 2 / max(getattr(sample, "channels", 1), 1)
                  / ratio / opts.render_rate)
        return max(opts.hold_seconds, play_s) + 0.05
    tail = 0.0
    if amp_env is not None:
        tail = min(env_mod.release_seconds(amp_env), env_mod.AUDITION_MAX_TAIL)
    return opts.hold_seconds + tail


def _pitch_ratio(voice, zone, sample, opts) -> float:
    if getattr(voice, "non_transpose", False):
        semis = 0
    else:
        note = getattr(opts, "_note", 60)
        semis = note + int(getattr(zone, "transpose", 0) or 0) \
            - int(getattr(zone, "root_key", 60) or 0)
    cents = (int(getattr(zone, "coarse_tune", 0) or 0) * 100
             + int(getattr(zone, "fine_tune", 0) or 0)
             + int(getattr(sample, "fine_tune", 0) or 0))
    return (2.0 ** (semis / 12.0) * 2.0 ** (cents / 1200.0)
            * float(sample.sample_rate) / float(opts.render_rate))


def render_voice(snd: voice_mod.Sounding, note: int, opts,
                 report: AuditionReport) -> tuple[List[float], List[float], float]:
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
    seconds = _voice_seconds(snd, opts, report)
    n_frames = max(1, int(math.ceil(seconds * opts.render_rate)))

    amp_env = getattr(voice, "amp_env", None)
    whole = bool(getattr(voice, "plays_whole_sample", False))
    fmt = getattr(opts, "_fmt", "")
    if fmt == "AKAI":
        report.note(
            Severity.UNMEASURED, "AKAI attack",
            "AKAI amp attack: the law that produced this number was retracted "
            "and nothing replaced it, so the attack is the parser's default, "
            "not the file's.")
        if float(getattr(voice, "filter_env_cents", 0.0) or 0.0) != 0.0:
            report.note(
                Severity.UNMEASURED, "AKAI filter-envelope sustain",
                "AKAI filter-envelope sustain (SUSTN2) has never been swept "
                "or read; this preset sweeps the filter, so that stage is a "
                "default.")
    if amp_env is not None and not whole:
        if getattr(amp_env, "release_rate_db_per_s", None):
            report.note(
                Severity.MEASURED, "release",
                "Release taken from this machine's measured slew rate (dB/s), "
                "so it does not depend on where 'silence' is.")
        else:
            report.note(
                Severity.UNMEASURED, "release",
                "Release taken from the source's stated seconds, because this "
                "format specifies a duration — the two samplers disagree about "
                "where silence is by 60.07 dB against 97.82 and neither figure "
                "was ever measured.")
    if amp_env is not None:
        attack_scale = env_mod.attack_scale_for(voice, opts.velocity)
        if attack_scale is not None and abs(attack_scale - 1.0) > 1e-9 and report is not None:
            report.note(
                Severity.UNMEASURED, "velocity → amp attack",
                "The attack time was scaled by velocity, but no direction law "
                "for that has been measured on these machines; it is a plain "
                "linear interpolation.")
        amp = env_mod.amp_envelope(
            amp_env, hold_s=opts.hold_seconds, tail_s=max(seconds - opts.hold_seconds, 0.0),
            rate=opts.render_rate,
            attack_scale=attack_scale if attack_scale else 1.0, whole=whole)
    else:
        amp = [1.0, 1.0]

    # Filter: built once per voice, per channel.
    f_type = int(getattr(voice, "filter_type", 0) or 0)
    mode, poles = filter_mod.topology_for(f_type)
    filt = None
    filt_env = None
    f_base = 0.0
    keytrack = float(getattr(voice, "filter_keytrack", 0.0) or 0.0)
    vel_cents = velocity_to_filter_cents(voice, opts.velocity)
    if mode != "bypass":
        if filter_mod.sections_for(poles) > 0:
            filter_mod.caveat_for_type(f_type, report)
            f_base = filter_mod.corner_to_f0(
                float(getattr(voice, "filter_cutoff", 20000.0) or 20000.0),
                poles, getattr(opts, "_fmt", ""), report)
            q = filter_mod.resonance_to_q(
                float(getattr(voice, "filter_resonance", 0.0) or 0.0),
                getattr(opts, "_fmt", ""), report)
            filt_q = q
            filt = [filter_mod.Cascade(opts.render_rate, mode,
                                       filter_mod.sections_for(poles))
                    for _ in range(opts.channels)]
            fenv = getattr(voice, "filter_env", None)
            fenv_cents = float(getattr(voice, "filter_env_cents", 0.0) or 0.0)
            if fenv is not None and fenv_cents != 0.0:
                filt_env = env_mod.filter_envelope(
                    fenv, hold_s=opts.hold_seconds,
                    tail_s=max(seconds - opts.hold_seconds, 0.0),
                    rate=opts.render_rate)
            if keytrack != 0.0:
                report.note(
                    Severity.UNMEASURED, "filter keytrack pivot",
                    "Filter keytracking hinges on which key, and no format "
                    f"here records it; MIDI {KEYTRACK_PIVOT} was assumed. A "
                    "wrong pivot is inaudible on the note you test and wrong "
                    "across the keyboard.")
            # Report clamps that bit.
            _ = (vel_cents, q)

    # Level and pan.
    db = (float(getattr(getattr(opts, "_preset", None), "volume", 0.0) or 0.0)
          + float(getattr(zone, "volume", 0.0) or 0.0)
          + velocity_to_volume_db(voice, opts.velocity, report))
    gain = _db_to_gain(db)
    pan = (float(getattr(getattr(opts, "_preset", None), "pan", 0.0) or 0.0)
           + float(getattr(zone, "pan", 0.0) or 0.0)
           + _velocity_to_pan(voice, opts.velocity)
           + _key_to_pan(voice, note))
    gl, gr = _constant_power(pan)

    loop_type = src.loop_type
    forward_loop = loop_type == 1
    forward_rel = loop_type == 3
    n_ch = len(src.channels)

    left: List[float] = [0.0] * n_frames
    right: List[float] = [0.0] * n_frames
    gamma = opts.render_rate / env_mod.CONTROL_RATE
    block = max(1, int(round(gamma)))
    pos = 0.0
    hold_frame = int(opts.hold_seconds * opts.render_rate)

    for f in range(n_frames):
        looping = forward_loop or (forward_rel and f < hold_frame)
        g = env_mod.interpolate(amp, f / gamma)

        if filt is not None:
            if f % block == 0:
                t_frac = f / gamma
                f_cents = (filt_env and env_mod.interpolate(filt_env, t_frac) or 0.0)
                f0 = (f_base
                      * 2.0 ** (keytrack * (note - KEYTRACK_PIVOT) / 12.0)
                      * 2.0 ** (vel_cents / 1200.0)
                      * 2.0 ** (f_cents * float(getattr(voice, "filter_env_cents", 0.0) or 0.0) / 1200.0))
                f0 = min(max(f0, 20.0), 0.45 * opts.render_rate)
                for c in filt:
                    c.set(f0, filt_q)

        for c in range(2):
            if c < n_ch:
                x = _read(src, src.channels[c], pos, looping)
            else:
                x = _read(src, src.channels[0], pos, looping)
            if filt is not None:
                x = filt[c].process(x)
            v = x * g * gain
            if c == 0:
                left[f] += v * gl
            else:
                right[f] += v * gr
        pos += ratio

    return left, right, seconds


# ── the pipeline ─────────────────────────────────────────────────────────────

def render(bank, preset, prov, opts) -> "Rendering":
    """Render a whole audition. See ``audition.render`` for the public API."""
    from . import Rendering  # late import: __init__ owns the dataclass

    report = AuditionReport()
    _seed_report(report, prov, opts)
    fmt = getattr(prov, "format", "") or ""
    max_sounding = voice_mod.ceiling_for(fmt)

    # Pre-compute the per-note spans so the bus can be sized in one pass.
    plans = []
    for note in opts.notes:
        o = _RenderOpts(opts, note=note, preset=preset, fmt=fmt)
        sounds = voice_mod.sounding(preset, note, opts.velocity, bank=bank,
                                    report=report,
                                    max_sounding=max_sounding)
        if not sounds:
            report.note(
                Severity.NOT_MODELLED, "silent note",
                f"Nothing sounds for MIDI note {note} at velocity "
                f"{opts.velocity}: no zone covers it. The keymap is quieter "
                f"than the note list.")
        span = max(opts.hold_seconds,
                   *( _voice_seconds(s, o, report) for s in sounds)) if sounds \
            else opts.hold_seconds
        plans.append((note, sounds, span, o))

    total_frames = 0
    offsets = []
    for _, _, span, _o in plans:
        offsets.append(total_frames)
        total_frames += int(math.ceil((span + opts.gap_seconds) * opts.render_rate))
    # Trailing gap is not silence worth keeping in the file.
    total_frames = max(
        1, total_frames - int(math.ceil(opts.gap_seconds * opts.render_rate)))

    out_l = [0.0] * total_frames
    out_r = [0.0] * total_frames

    for (note, sounds, _span, o), start in zip(plans, offsets):
        for s in sounds:
            left, right, _secs = render_voice(s, note, o, report)
            for i, v in enumerate(left):
                j = start + i
                if j >= total_frames:
                    break
                out_l[j] += v
            for i, v in enumerate(right):
                j = start + i
                if j >= total_frames:
                    break
                out_r[j] += v

    peak, pcm = _finish(out_l, out_r, opts)
    if peak * _db_to_gain(opts.headroom_db) > 1.0:
        report.note(
            Severity.NOT_MODELLED, "output level",
            "This preset's layers sum above full scale; the audition is "
            "attenuated, and the hardware's own output stage is not modelled.")
    return Rendering(pcm=pcm, rate=opts.render_rate, channels=opts.channels,
                     peak_before_limit=peak, report=report,
                     seconds=total_frames / opts.render_rate)


def _finish(out_l: List[float], out_r: List[float], opts) -> tuple[float, bytes]:
    headroom = _db_to_gain(opts.headroom_db)
    peak = 0.0
    for v in out_l:
        peak = max(peak, abs(v))
    for v in out_r:
        peak = max(peak, abs(v))
    # Report the pre-headroom peak: a quiet audition the user attributes to the
    # preset is a lie about the preset. The caller checks it against headroom.
    from .. import mpc2emu_bridge
    if _np is not None:
        # Mirror the pure path EXACTLY: same soft-limit above full scale, same
        # 32768.0 scaling and truncation, same clip. An accelerator that
        # changed the audio depending on whether numpy is installed would be a
        # silent format mismatch of the kind risk 8 warns about.
        a = _soft_limit_np(_np.asarray(out_l) * headroom)
        b = _soft_limit_np(_np.asarray(out_r) * headroom)
        if opts.channels == 1:
            mono = _soft_limit_np((a + b) * 0.5)
            return peak, _np.clip(mono * 32768.0, -32768.0, 32767.0) \
                .astype("<i2").tobytes()
        inter = _np.empty(a.size * 2, dtype="<i2")
        inter[0::2] = _np.clip(a * 32768.0, -32768.0, 32767.0).astype("<i2")
        inter[1::2] = _np.clip(b * 32768.0, -32768.0, 32767.0).astype("<i2")
        return peak, inter.tobytes()
    flat: List[float] = []
    for i in range(len(out_l)):
        l = _soft_limit(out_l[i] * headroom)
        r = _soft_limit(out_r[i] * headroom)
        if opts.channels == 1:
            flat.append(_soft_limit((l + r) * 0.5))
        else:
            flat.append(l)
            flat.append(r)
    return peak, mpc2emu_bridge.resampler._float_to_pcm(flat)


def _soft_limit_np(a):
    """The vectorised ``_soft_limit``, bit-for-bit the same mapping."""
    mag = _np.abs(a)
    over = mag > 1.0
    if not bool(over.any()):
        return a
    out = a.copy()
    m = mag[over]
    out[over] = _np.sign(a[over]) * (m / (1.0 + (m - 1.0)))
    return out


def _soft_limit(x: float) -> float:
    a = abs(x)
    if a <= 1.0:
        return x
    return math.copysign(a / (1.0 + (a - 1.0)), x)


def _seed_report(report: AuditionReport, prov, opts) -> None:
    """The statements that are true of every audition, said once."""
    fmt = getattr(prov, "format", "") or "unknown"
    origin = getattr(prov, "origin", "") or ""
    if fmt == "KRZ":
        report.note(
            Severity.MEASURED, "pitch and tuning",
            "K2000 tuning is 256 units per semitone and its cutoff is already "
            "a filter natural frequency (f0), so neither was converted.")
    _e4b_zplane_caveats(report, prov)
    report.note(
        Severity.NOT_MODELLED, "output stage",
        "LFOs, chorus, delay, and the machines' own output stages, converters "
        "and anti-alias filters are not modelled.")
    report.note(
        Severity.NOT_MODELLED, "interpolation",
        "The interpolation you are hearing is ours, not the sampler's — an "
        "E4XT and an S3000XL each have their own artefacts and neither is "
        "modelled.")
    if origin:
        report.note(
            Severity.MEASURED, "source",
            f"Rendered from mpc2emu's parsed model of {origin} ({fmt}).")


#: E4XT vpar[58] values whose filter has no XPM equivalent, and so was
#: collapsed onto XPM "Low 4" by parsers/e4b_parser.py. Reading the byte back
#: is the only place the distinction still exists -- without it a phaser and a
#: 4-pole lowpass are the same object, and audition plays a different
#: instrument while saying nothing (spec §6.2, risk 1).
_E4B_ZPLANE_BYTES = {
    0x40: "Phaser 1", 0x41: "Phaser 2", 0x42: "Bat Phaser",
    0x48: "Flanger Lite",
    0x60: "Dual EQ Morph", 0x61: "2EQ+Lowpass Morph",
    0x62: "2EQMorph+Expression", 0x68: "Peak/Shelf Morph",
}


def _e4b_zplane_caveats(report: AuditionReport, prov) -> None:
    bytes_by_voice = getattr(prov, "e4b_filter_bytes", None)
    if not bytes_by_voice:
        return
    names = sorted({_E4B_ZPLANE_BYTES[b] for b in bytes_by_voice.values()
                    if b in _E4B_ZPLANE_BYTES})
    for name in names:
        report.note(
            Severity.NOT_MODELLED, f"E4B Z-plane filter ({name})",
            f"This voice's filter is a {name}. The audition plays a 4-pole "
            f"lowpass in its place, because nothing here models a Z-plane "
            f"filter.")


# ── small mutable render-options view ────────────────────────────────────────
#
# AuditionOptions is frozen and deliberately carries only what the UI asks
# for. The renderer needs the current note, the preset and the source format
# at the point of use; rather than widen the public dataclass, they ride in
# this shallow wrapper, which delegates everything else to the frozen object.

class _RenderOpts:
    __slots__ = ("_opts", "_note", "_preset", "_fmt")

    def __init__(self, opts, *, note: int = 60, preset=None, fmt: str = ""):
        object.__setattr__(self, "_opts", opts)
        object.__setattr__(self, "_note", note)
        object.__setattr__(self, "_preset", preset)
        object.__setattr__(self, "_fmt", fmt)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_opts"), name)
