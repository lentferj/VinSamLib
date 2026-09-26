"""
The filter: a TPT state-variable section, cascaded, plus the adapters that
turn each format's stated cutoff and resonance into the engine's own
quantities.

**TPT rather than a direct-form biquad** because the cutoff is modulated per
control block and TPT stays stable and well-behaved under modulation; a
direct-form biquad with time-varying coefficients does not. Nothing existing
is reusable: ``processors/resampler.py``'s ``_twopole_lowpass`` is two
one-poles with a ``* 1.2`` fudge and no resonance at all.

The engine's parameter is **f0, the section's natural frequency, never a
−3 dB corner** (spec §6.1). That dissolves the KRZ problem instead of guessing
at a conversion factor: the K2000 already hands us f0, while AKAI/E4B/MPC hand
a −3 dB corner that has to be converted at the nominal Q. Because the
conversion is taken at the nominal Q only, and every machine labels its corner
at *its own* resonance setting, the caveat is raised here -- at the site of
the approximation -- and nowhere else.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

from .caveats import AuditionReport, Severity

#: The measured −3 dB ratio of two cascaded 2-pole Butterworth sections at one
#: f0. **The K2000 documents 0.774 and −6.11 dB, and a plain cascade gives
#: 0.802 / −6.02 dB** -- the gap is small, but it means the topology the K2000
#: numbers describe is not exactly this one. The value here is the one this
#: implementation actually produces, so ``corner_to_f0`` is true of itself;
#: test 10.3 records the discrepancy rather than widening a tolerance.
CASCADE_3DB_RATIO = 0.802

_MIN_F0_HZ = 20.0


class SVF:
    """Topology-preserving-transform state-variable filter, one per channel.

    ``process_*`` are sequential and do not vectorise, which is why filter.py
    has exactly one implementation and the numpy fast path never touches it
    (spec §4.6).
    """

    __slots__ = ("rate", "_g", "_k", "_a1", "_a2", "_a3", "_ic1", "_ic2")

    def __init__(self, rate: int):
        self.rate = int(rate)
        self._g = 0.0
        self._k = 1.0
        self._ic1 = 0.0
        self._ic2 = 0.0
        self._recompute()

    def set(self, f0_hz: float, q: float) -> None:
        nyquist = 0.5 * self.rate
        f0 = min(max(float(f0_hz), _MIN_F0_HZ), 0.45 * self.rate)
        q = max(float(q), 0.05)
        self._g = math.tan(math.pi * f0 / self.rate)
        self._k = 1.0 / q
        self._recompute()

    def _recompute(self) -> None:
        """The coefficients, derived once per control block instead of once
        per SAMPLE.

        They depend only on ``_g`` and ``_k``, which only ``set()`` changes --
        yet ``process()`` used to recompute them, including a DIVISION, for
        every sample of every section: 8.8 million times in a 128 MB preset.
        Identical arithmetic on identical operands, so the output is
        bit-for-bit what it was; only the count changes.
        """
        g, k = self._g, self._k
        self._a1 = 1.0 / (1.0 + g * (g + k))
        self._a2 = g * self._a1
        self._a3 = g * self._a2

    def process(self, x: float) -> tuple[float, float, float, float]:
        """Returns ``(low, high, band, notch)`` for one input sample.

        Kept as the reference shape and as the four-tap answer; the renderer
        uses the per-tap methods below, which are this function with the three
        taps nobody asked for left out.
        """
        v3 = x - self._ic2
        v1 = self._a1 * self._ic1 + self._a2 * v3
        v2 = self._ic2 + self._a2 * self._ic1 + self._a3 * v3
        self._ic1 = 2.0 * v1 - self._ic1
        self._ic2 = 2.0 * v2 - self._ic2
        low = v2
        band = v1
        high = x - self._k * v1 - v2
        notch = high + low
        return low, high, band, notch

    # -- one method per tap ---------------------------------------------------
    #
    # FOUR COPIES OF THE SAME THREE LINES, ON PURPOSE. `process()` computed all
    # four taps, allocated a tuple and let the caller index one -- three
    # multiplies, two adds and an allocation thrown away per sample per
    # section. Factoring the shared part back into a helper would put the call
    # overhead straight back, which is the thing being removed.
    #
    # Duplication that cannot be refactored has to be PINNED instead:
    # manual_audition_filter_taps asserts each of these against `process()`
    # sample by sample, so the four cannot drift apart silently.

    def process_lp(self, x: float) -> float:
        v3 = x - self._ic2
        v1 = self._a1 * self._ic1 + self._a2 * v3
        v2 = self._ic2 + self._a2 * self._ic1 + self._a3 * v3
        self._ic1 = 2.0 * v1 - self._ic1
        self._ic2 = 2.0 * v2 - self._ic2
        return v2

    def process_hp(self, x: float) -> float:
        v3 = x - self._ic2
        v1 = self._a1 * self._ic1 + self._a2 * v3
        v2 = self._ic2 + self._a2 * self._ic1 + self._a3 * v3
        self._ic1 = 2.0 * v1 - self._ic1
        self._ic2 = 2.0 * v2 - self._ic2
        return x - self._k * v1 - v2

    def process_bp(self, x: float) -> float:
        v3 = x - self._ic2
        v1 = self._a1 * self._ic1 + self._a2 * v3
        v2 = self._ic2 + self._a2 * self._ic1 + self._a3 * v3
        self._ic1 = 2.0 * v1 - self._ic1
        self._ic2 = 2.0 * v2 - self._ic2
        return v1

    def process_notch(self, x: float) -> float:
        v3 = x - self._ic2
        v1 = self._a1 * self._ic1 + self._a2 * v3
        v2 = self._ic2 + self._a2 * self._ic1 + self._a3 * v3
        self._ic1 = 2.0 * v1 - self._ic1
        self._ic2 = 2.0 * v2 - self._ic2
        # notch = high + low, in that order -- float addition is not
        # associative and the test compares against process() exactly.
        return (x - self._k * v1 - v2) + v2


class Cascade:
    """``sections`` SVF sections at one f0, tapped by topology.

    A 2-pole section per two poles: 2 poles = one section, 4 = two, 6 = three,
    8 = four. One pole is honoured as a single section (there is no TPT
    one-pole here), which the caller caveats.
    """

    def __init__(self, rate: int, mode: str, sections: int):
        if mode not in ("lp", "hp", "bp", "notch", "bypass"):
            raise ValueError(f"unknown filter mode {mode!r}")
        self.mode = mode
        self.sections = [SVF(rate) for _ in range(max(0, sections))]
        self._idx = {"lp": 0, "hp": 1, "bp": 2, "notch": 3}[mode] \
            if mode != "bypass" else 0
        # Decided ONCE, here, rather than per sample. `active` was a property
        # evaluated 4.4 million times in one render, and the tap was chosen by
        # indexing a freshly built tuple every sample of every section; both
        # are answers that cannot change after construction.
        self._active = mode != "bypass" and bool(self.sections)
        self._chain = tuple(
            getattr(s, f"process_{mode}") for s in self.sections) \
            if self._active else ()

    @property
    def active(self) -> bool:
        return self._active

    def set(self, f0_hz: float, q: float) -> None:
        for s in self.sections:
            s.set(f0_hz, q)

    def process(self, x: float) -> float:
        if not self._active:
            return x
        for step in self._chain:
            x = step(x)
        return x


def topology_for(filter_type: int) -> Tuple[str, int]:
    """XPM FilterType (0–29) -> ``(mode, poles)``.

    Off is a genuine bypass. Types with no implementation here -- BandBoost
    (19–22), Model 1–3 (23–25), Vocal formant (26–28), MPC3000 LPF (29) --
    fall back to the nearest LP/HP/BP/notch and the caller raises a
    NOT MODELLED caveat naming the type. The enum is mpc2emu's own
    (``writers/e4b_writer.py``): Low1..Low8 = 1,2,3,4,5; High1..8 = 6–10;
    Band2..8 = 11–14; BS2P..8P = 15–18; BB2P..8P = 19–22; Model1–3 = 23–25;
    Vocal1–3 = 26–28; MPC3000 LPF = 29.
    """
    table = {
        0: ("bypass", 0),
        1: ("lp", 2), 2: ("lp", 2), 3: ("lp", 4),
        4: ("lp", 6), 5: ("lp", 8),
        6: ("hp", 2), 7: ("hp", 2), 8: ("hp", 4),
        9: ("hp", 6), 10: ("hp", 8),
        11: ("bp", 2), 12: ("bp", 4), 13: ("bp", 6), 14: ("bp", 8),
        15: ("notch", 2), 16: ("notch", 4), 17: ("notch", 6), 18: ("notch", 8),
        19: ("bp", 2), 20: ("bp", 4), 21: ("bp", 6), 22: ("bp", 8),
        23: ("lp", 4), 24: ("lp", 4), 25: ("lp", 4),
        26: ("bp", 2), 27: ("bp", 2), 28: ("bp", 2),
        29: ("lp", 2),
    }
    return table.get(int(filter_type), ("bypass", 0))


#: Types whose fallback is a lie worth naming. Type -> human name.
_NOT_MODELLED_TYPES = {
    19: "BandBoost 2-pole", 20: "BandBoost 4-pole",
    21: "BandBoost 6-pole", 22: "BandBoost 8-pole",
    23: "Model 1", 24: "Model 2", 25: "Model 3",
    26: "Vocal 1", 27: "Vocal 2", 28: "Vocal 3",
    29: "MPC3000 LPF",
}


def sections_for(poles: int) -> int:
    if poles <= 0:
        return 0
    return max(1, poles // 2)


def corner_to_f0(corner_hz: float, poles: int, fmt: str,
                 report: Optional[AuditionReport] = None) -> float:
    """A format's stated cutoff -> this filter's f0.

    ``KRZ`` is passed straight through: the K2000's displayed cutoff *is* f0
    (``models/common.py:4681-4686``). Everything else states a −3 dB corner,
    which is converted at the nominal Q. The conversion is taken at the
    nominal Q only -- every machine labels its corner at its own resonance
    setting and none of them was measured that way -- so it is FITTED and said
    so here.
    """
    corner = max(float(corner_hz), _MIN_F0_HZ)
    if corner_hz < _MIN_F0_HZ and report is not None:
        # The render loop's own clamp can never see this one: by the time f0
        # reaches it the floor has already been applied here, so a preset
        # authored below the floor would be silently opened up with nothing
        # said. A K2000 cutoff really does go this low.
        report.note(
            Severity.FITTED, "filter cutoff clamped",
            f"This voice states a cutoff of {float(corner_hz):.0f} Hz, below "
            f"the {_MIN_F0_HZ:.0f} Hz floor this renderer's filter can run. It "
            f"was raised to the floor, so the audition is more open than the "
            f"preset asks for. The machine has its own floor and it is not "
            f"this one.")
    if fmt == "KRZ":
        return corner
    sections = sections_for(poles)
    if sections <= 1:
        return corner
    if report is not None:
        report.note(
            Severity.FITTED, "filter corner → f0",
            f"The {fmt} cutoff is a −3 dB corner; it was converted to the "
            f"filter's natural frequency at the nominal Q, using the measured "
            f"{CASCADE_3DB_RATIO:.3f} ratio of a {sections}-section cascade. "
            f"Every machine labels its corner at its own resonance setting and "
            f"none was measured that way, so the conversion holds at nominal Q "
            f"only.")
    return corner / CASCADE_3DB_RATIO


def resonance_to_q(resonance01: float, fmt: str,
                   report: Optional[AuditionReport] = None) -> float:
    """A format's normalised resonance knob -> Q.

    **KRZ** carries a *measured* quantity: the byte is dB of peak boost,
    0..24 dB, inverted through ``krz_reson_byte_to_01`` by the parser. Solve
    ``Q / sqrt(1 − 1/(4Q²)) = 10^(dB/20)`` for Q. FITTED, and honestly so.

    **AKAI, E4B, MPC** are normalised knobs with **no measured Q law anywhere
    in either repo**. Map 0…1 onto Q 0.707…8 on a documented exponential
    curve. UNMEASURED -- and the report says in those words that this is the
    single most invented number in the chain.
    """
    r = min(max(float(resonance01), 0.0), 1.0)
    if fmt == "KRZ":
        db = r * 24.0
        target = 10.0 ** (db / 20.0)
        # Q / sqrt(1 - 1/(4 Q^2)) = target. Monotonic in Q above 1/sqrt(2);
        # bisect on [0.7071, 64].
        lo, hi = 0.70710678, 64.0
        for _ in range(80):
            mid = 0.5 * (lo + hi)
            denom = math.sqrt(max(1.0 - 1.0 / (4.0 * mid * mid), 1e-12))
            if mid / denom < target:
                lo = mid
            else:
                hi = mid
        if report is not None and r > 0.0:
            report.note(
                Severity.FITTED, "K2000 resonance",
                f"Resonance was derived from the K2000's measured "
                f"{db:.1f} dB of peak boost, solved for Q.")
        return 0.5 * (lo + hi)
    # Exponential 0.7071 .. 8. A conservative curve rather than a linear one:
    # Q = 4 where the machine does 1.2 is a whistle on every note, and the
    # mitigation here is honesty plus not overstepping.
    q = 0.70710678 * (8.0 / 0.70710678) ** r
    if report is not None and r > 0.0:
        report.note(
            Severity.UNMEASURED, "resonance → Q",
            "Resonance mapped from a normalised knob onto Q 0.707…8 with no "
            "measured Q law for this machine. This is the most invented number "
            "in the chain.")
    return q


def caveat_for_type(filter_type: int, report: AuditionReport) -> None:
    """Raise the NOT MODELLED caveat for a type that fell back."""
    name = _NOT_MODELLED_TYPES.get(int(filter_type))
    if name is None:
        return
    mode, _ = topology_for(filter_type)
    report.note(
        Severity.NOT_MODELLED, f"filter type {name}",
        f"This voice's filter is a {name}. The audition plays a {mode} in its "
        f"place, because nothing here models that type.")


#: Types whose resonance law is the invented one -- used by the report so the
#: 'most invented number' sentence is only shown when a resonant filter ran.
def is_resonant(filter_resonance: float) -> bool:
    return float(filter_resonance or 0.0) > 0.0
