"""
Applying user-chosen sample names to a parsed mpc2emu Bank, in the gap
between the parse and the write -- the same place build/sampledir_import.py
applies the Sample Placement dialog's zone overrides, and for the same
reason: the Bank is ours alone at that moment, and nothing on disk has been
touched yet.

**A zone finds its audio by name.** That is not a display detail: mpc2emu's
own `cbe6f10` fixed a case where two samples shared a name and the second
became unreachable, silently, costing 5766 samples across a real library.
So a rename here is only ever applied through mpc2emu's own guarantees --
`_safe_name` for the 16-character ASCII field, `_unique_sample_name` to
keep every name distinct -- and every zone that referenced the old name is
moved with it. A rename that cannot keep those promises is refused, not
approximated.
"""

from __future__ import annotations

from typing import Any, Optional

from ..mpc2emu_bridge import xpm_parser
from ..notes import midi_to_name


def names_from_base(bank: Any, base: str, octave_offset: int,
                     with_key: bool = True) -> dict[str, str]:
    """{current sample name: `<base>-<key>`} for every sample the bank's zones
    reference, keyed by the note each one plays.

    Built AFTER the parse, because that is the first moment the root notes
    exist -- the dialog only collects the base name, and parsing a folder or
    program just to preview names would undo the laziness the import dialogs
    are built around.

    `with_key` off names every sample just `base`, which only makes sense
    for E4B: its writer keeps a SECOND name field of its own, built as
    `<base cut>_<note><octave>` (e4b_writer._sample_display_name), so the key
    is already there and ours only eats into the base. KRZ and EIII have no
    such field -- without a key suffix their samples would all be `base`,
    and _unique_sample_name would number them.

    The root comes from the ZONE, but the name belongs to the SAMPLE, and one
    sample can be spread across several zones (a drum-kit fill, a stacked
    layer). First zone wins, so a sample is named for the root it was recorded
    at rather than for whichever zone happens to be last.
    """
    base = (base or "").strip()
    if not base:
        return {}
    out: dict[str, str] = {}
    for preset in bank.presets:
        for voice in preset.voices:
            for zone in voice.zones:
                current = getattr(zone, "sample_name", "") or ""
                if not current or current in out:
                    continue
                root = getattr(zone, "root_key", None)
                # "#" is not one of the characters an E4B name field keeps --
                # _safe_name turns it into "_", so C#1 would read "C_1" and be
                # mistaken for C1. Spell the sharp instead: Cs1.
                key = (midi_to_name(int(root), octave_offset).replace("#", "s")
                       if with_key and root is not None else "")
                out[current] = f"{base}-{key}" if key else base
    return out


def apply_sample_names(bank: Any, names: dict[str, str]) -> dict[str, str]:
    """Rename samples in `bank` according to {current_name: wanted_name}.

    Returns what was actually applied, {current_name: final_name} -- the
    final name can differ from the wanted one, because it goes through the
    same shortening and uniquifying every other name in the bank does. A
    name not in the mapping is left exactly as it is.

    Zones are remapped in the same pass. Nothing is written; the caller
    writes the bank afterwards as usual.
    """
    if not names:
        return {}
    safe_name = getattr(xpm_parser, "_safe_name", None)
    unique_name = getattr(xpm_parser, "_unique_sample_name", None)

    # Every name in the bank, including the ones nobody is renaming: a new
    # name must not collide with one of those either.
    taken = {s.name for s in bank.samples if s.name not in names}
    applied: dict[str, str] = {}
    for sample in bank.samples:
        wanted = names.get(sample.name)
        if wanted is None:
            continue
        final = safe_name(wanted, tail=False) if safe_name else wanted[:16]
        if unique_name is not None:
            final = unique_name(final, taken)
        elif final in taken:                      # no helper: fall back rather
            continue                              # than create a collision
        taken.add(final)
        applied[sample.name] = final

    if not applied:
        return {}
    for sample in bank.samples:
        sample.name = applied.get(sample.name, sample.name)
    for preset in bank.presets:
        for voice in preset.voices:
            for zone in voice.zones:
                zone.sample_name = applied.get(zone.sample_name, zone.sample_name)
    return applied


def preview_conflicts(names: dict[str, str], existing: Optional[set] = None) -> list[str]:
    """Which wanted names cannot be given as asked -- for a dialog to show
    BEFORE anything is applied.

    Two shapes: a name too long for the field (it will be cut), and two
    samples asking for the same name (the second will be numbered). Neither
    loses audio -- apply_sample_names() guarantees uniqueness -- but a user
    who typed one name and got another deserves to see it coming."""
    safe_name = getattr(xpm_parser, "_safe_name", None)
    out, seen = [], set(existing or ())
    for current, wanted in names.items():
        final = safe_name(wanted, tail=False) if safe_name else wanted[:16]
        if final != wanted:
            out.append(f"{wanted!r} does not fit 16 characters — becomes {final!r}")
        if final in seen:
            out.append(f"{final!r} is asked for twice — the second will be numbered")
        seen.add(final)
    return out
