"""
Caveats: what the audition approximated, and how honestly it can say so.

The whole feature is only safe to ship because it says what it is (see the
spec's §9). This module is that surface, and it has one design rule that
matters more than its shape:

    **Caveats are appended where the approximation is applied, never
    assembled from a per-format table.**

A table is a second thing to maintain and goes stale the day a branch
changes; a caveat raised inside ``corner_to_f0()`` cannot be wrong about
whether ``corner_to_f0()`` ran. That is the same reasoning ``build/convert.py``
gives for deleting a correction that outlived its fault.

Nothing here imports Qt, mpc2emu or numpy: it is pure data so that the engine
can be exercised headless.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Severity(enum.Enum):
    """How much of this number came from a machine rather than a guess.

    Ordered from best to worst so a report can group and sort on it, and so
    a new severity cannot be added without deciding where it belongs.
    """

    MEASURED = "measured on hardware"
    FITTED = "fitted or derived"
    UNMEASURED = "not measured"
    NOT_MODELLED = "not modelled at all"


#: Display order, best first. A tuple so the order is explicit rather than
#: inherited from enum definition order by accident.
SEVERITY_ORDER = (
    Severity.MEASURED,
    Severity.FITTED,
    Severity.UNMEASURED,
    Severity.NOT_MODELLED,
)


@dataclass(frozen=True)
class Caveat:
    """One approximation, said once.

    ``subject`` is the deduplication key as well as a heading: 'AKAI attack',
    'filter keytrack pivot', 'E4B Z-plane filter'. Two caveats with the same
    severity and subject are the same statement; the first is kept.
    """

    severity: Severity
    subject: str
    text: str


class AuditionReport:
    """The caveats raised while rendering one audition.

    Deduplicates on ``(severity, subject)`` -- a six-layer preset must not
    print the same sentence six times -- and preserves insertion order so the
    report reads in the order the approximations were met.
    """

    def __init__(self) -> None:
        self._seen: set[tuple[Severity, str]] = set()
        self._caveats: list[Caveat] = []

    def add(self, caveat: Caveat) -> None:
        key = (caveat.severity, caveat.subject)
        if key in self._seen:
            return
        self._seen.add(key)
        self._caveats.append(caveat)

    def note(self, severity: Severity, subject: str, text: str) -> None:
        """Convenience for the common one-liner at an approximation site."""
        self.add(Caveat(severity, subject, text))

    def __len__(self) -> int:
        return len(self._caveats)

    def __iter__(self):
        return iter(self._caveats)

    @property
    def caveats(self) -> list[Caveat]:
        return list(self._caveats)

    def of(self, severity: Severity) -> list[Caveat]:
        return [c for c in self._caveats if c.severity is severity]

    def lines(self) -> list[str]:
        """One line per caveat, prefixed with its severity, for a QLabel."""
        return [f"{c.severity.value}: {c.subject} — {c.text}"
                for c in self._caveats]

    def as_text(self) -> str:
        """The same content as the WAV sidecar: grouped by severity, with a
        header that restates what the file is."""
        out = [
            "VinSamLib audition — this is a model of the preset's parameters,",
            "not of the sampler, and it does not sound like the hardware.",
            "",
        ]
        if not self._caveats:
            out.append("No caveats were raised for this preset.")
            return "\n".join(out) + "\n"
        first = True
        for severity in SEVERITY_ORDER:
            group = self.of(severity)
            if not group:
                continue
            if not first:
                out.append("")
            first = False
            out.append(f"{severity.name} ({severity.value}):")
            for c in group:
                out.append(f"  - {c.subject}: {c.text}")
        return "\n".join(out) + "\n"
