"""
Which zones sound for a given (note, velocity).

Rule: **every** zone whose key and velocity windows contain the note and
velocity, across **every** voice -- not the first match. That rule comes from
``processors/shrink_planner.py:199 _sounding``, whose docstring carries the
measurement that forced it: a first-match selector silently drops every
overlapping layer, which is exactly the kind of defect audition exists to
surface.

``_sounding`` itself is deliberately **not** imported. It returns
``(sample_name, root_key)`` tuples and discards the zone and the voice --
everything the renderer needs -- so this reuses the rule and cites the source
rather than the function.

A zone whose sample is not in ``bank.find_sample()`` is **reported**, not
skipped silently: AKAI ``missing_samples`` is a normal condition for
floppy-split libraries and a silent skip would read as "this layer does not
exist".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .caveats import AuditionReport, Severity

#: Cap on simultaneous zones. A stacked KRZ program can name more layers than
#: any of these machines will play; a preset that is quiet because it blew a
#: budget should say so rather than just being quiet.
MAX_SOUNDING = 32

#: Per-format per-note ceilings, both measured. The hardware's own voice
#: budget is the number here; which layers it would DROP is its own choice and
#: is never simulated (spec §11 stage 5).
FORMAT_CEILING = {
    "E4B": 32,    # E4XT
    "EIII": 32,
    "KRZ": 24,    # K2000R
}


def ceiling_for(fmt: str) -> int:
    return FORMAT_CEILING.get((fmt or "").upper(), MAX_SOUNDING)


@dataclass(frozen=True)
class Sounding:
    voice: object      # mpc2emu VoiceLayer
    zone: object       # mpc2emu ZoneMapping
    sample: object     # mpc2emu SampleData


def _in_window(zone, note: int, velocity: int) -> bool:
    lo_key = getattr(zone, "lo_key", 0)
    hi_key = getattr(zone, "hi_key", 127)
    lo_vel = getattr(zone, "lo_vel", 0)
    hi_vel = getattr(zone, "hi_vel", 127)
    return lo_key <= note <= hi_key and lo_vel <= velocity <= hi_vel


def sounding(preset, note: int, velocity: int,
             bank=None, report: Optional[AuditionReport] = None,
             max_sounding: int = MAX_SOUNDING) -> List[Sounding]:
    """Every zone that sounds for ``(note, velocity)``.

    ``bank`` is optional only so this can be called on a hand-built preset in
    a test; without it a zone's sample cannot be resolved and the zone is
    returned with ``sample=None``. The renderer always passes it.
    ``max_sounding`` is the source format's own per-note ceiling (see
    ``ceiling_for``); it defaults to the widest of the machines.
    """
    found: List[Sounding] = []
    missing: List[str] = []
    for voice in getattr(preset, "voices", []) or []:
        env_report = report
        # A voice window folded into its zones at parse time (mpc2emu records
        # this as an open item) means there is nothing to check here; the zone
        # windows are all that exist.
        for zone in getattr(voice, "zones", []) or []:
            if not _in_window(zone, note, velocity):
                continue
            sample = None
            if bank is not None:
                sample = bank.find_sample(getattr(zone, "sample_name", ""))
                if sample is None:
                    name = getattr(zone, "sample_name", "?")
                    if name not in missing:
                        missing.append(name)
                    continue
            found.append(Sounding(voice=voice, zone=zone, sample=sample))
            if len(found) > max_sounding:
                if env_report is not None:
                    env_report.note(
                        Severity.NOT_MODELLED, "voice budget",
                        f"This preset names more than {max_sounding} layers "
                        f"sounding on one note. Only {max_sounding} are "
                        f"played; the hardware's own voice budget and which "
                        f"layers it would drop are not modelled.")
                found = found[:max_sounding]
                return found
    if missing and report is not None:
        shown = ", ".join(missing[:4]) + ("…" if len(missing) > 4 else "")
        report.note(
            Severity.NOT_MODELLED, "missing samples",
            f"{len(missing)} zone(s) on this note name a sample that is not in "
            f"the bank ({shown}), so they contribute no audio. This is normal "
            f"for a floppy-split AKAI library, where a program and its samples "
            f"live on different volumes.")
    return found
