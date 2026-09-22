"""Whether a source→target path can be converted the way the DEVICE would.

**Two different things share the word "firmware" and must not be merged.**
Getting this wrong is the ``E4BFile``-vs-``Bank`` mistake again: the same
name over two layers, failing as if the app were broken.

1. **The disc READER** (``foreign_import``'s EPS and Roland support). An
   Ensoniq or Roland disc has no documented layout, so mpc2emu's parsers are
   derived from the sampler's own firmware -- position tables, volume tables,
   key maps. That is how the disc is read at all; there is no alternative
   reader to choose between. The bank that comes out is then written by
   mpc2emu's NORMAL writer, with this project's usual laws.

2. **The device-matching WRITER** (this module). mpc2emu is wiring a mode
   that reproduces, byte for byte, what the sampler's own disk importer
   would have written. It is **deliberately worse output than the normal
   converter**: its value is that a bank built this way can be diffed
   against a real device import, and any difference is a defect in the
   reading of the firmware. Their ``writers/e4b_writer.py`` warns that
   applying it on top of a normally-converted voice yields "a hybrid that is
   neither our conversion nor the device's, and whose diff against hardware
   would mean nothing" -- so it is a whole separate path, never a tweak.

A user who picks (2) expecting better results gets worse ones, which is why
it is never phrased as a quality setting and never sits beside one.

**The table below is PROVISIONAL and exists to be deleted.** mpc2emu is
generating a JSON contract from their code and asserting it in their test
suite, precisely so this cannot drift. ``load_contract()`` is where that
file gets read the day it lands; until then these statuses come from their
2026-09-22 message and are dated so a stale entry is visible rather than
silently authoritative. **The fidelity counts are deliberately absent** --
they asked us not to hardcode them because they move as work continues.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: When the statuses below were told to us. Shown in the UI reason string so
#: an out-of-date table identifies itself instead of being believed.
PROVISIONAL_AS_OF = "2026-09-22"


@dataclass(frozen=True)
class PathStatus:
    available: bool
    reason: str


#: Keyed (source format, target format), using our own format labels.
_PROVISIONAL = {
    ("AKAI", "KRZ"): PathStatus(True, "implemented and wired upstream"),
    ("AKAI", "E4B"): PathStatus(
        False, "the laws are complete upstream but the wiring is still in "
               "progress"),
    ("Roland", "E4B"): PathStatus(False, "not started upstream (considered feasible)"),
    ("Roland", "KRZ"): PathStatus(False, "not started upstream (considered feasible)"),
    ("EPS", "KRZ"): PathStatus(
        False, "not started upstream — the device's write list has not been "
               "read yet"),
    ("EPS", "E4B"): PathStatus(
        False, "blocked upstream — a struct nobody has been able to locate"),
}

#: Upstream having a path working is necessary but NOT sufficient: we also
#: need something to call. mpc2emu is planning a single CLI flag plus the
#: generated contract, and neither exists yet, so nothing is selectable here
#: however green their side goes. Flipping this to True is the whole of the
#: wiring change once the flag lands -- and it is deliberately separate from
#: the per-path table, so "they can do it" and "we can ask for it" can never
#: be confused for each other.
INVOCABLE_HERE = False

_contract: Optional[dict] = None
_contract_tried = False

#: Where mpc2emu generates it. Read from the configured checkout, never
#: copied here: a copy is a second source of truth that goes stale silently,
#: which is the whole failure the generated file exists to prevent.
CONTRACT_RELPATH = Path("docs") / "firmware_sim_contract.json"


def load_contract(data: Optional[dict]) -> None:
    """Install mpc2emu's generated contract, replacing the provisional table.

    A setter as well as a file read, so tests can install one without a file.
    """
    global _contract, _contract_tried
    _contract = data
    _contract_tried = True


def _contract_data() -> Optional[dict]:
    """The contract from the configured mpc2emu checkout, read once.

    Failure is silent and falls back to the provisional table, because a
    checkout without the file is the normal case for anyone not on their
    branch -- and the fallback is strictly more conservative than the
    contract, never less.
    """
    global _contract, _contract_tried
    if _contract_tried:
        return _contract
    _contract_tried = True
    try:
        from ..config import Config
        path = Config.load().mpc2emu_path / CONTRACT_RELPATH
        with open(path, "r", encoding="utf-8") as fh:
            _contract = json.load(fh)
    except Exception:
        _contract = None
    return _contract


def fidelity(source_format: str, target_format: str) -> str:
    """What this path does NOT reproduce, for the user, or "".

    Built from the contract at the moment it is asked and never stored:
    mpc2emu asked us not to hardcode the counts because they move, and they
    moved between their message and their generated file on the same evening
    (20 modelled cords became 21).

    **Their caveat strings are shown verbatim.** They are written to be
    displayed, and rewording them here would fork the explanation -- so the
    only thing added is the coverage ratio, which their caveats deliberately
    leave out and which they called the honest denominator: "21 modelled"
    reads better than it deserves without the 23.
    """
    entry = _entry(source_format, target_format)
    if not entry:
        return ""
    parts = []
    modelled = entry.get("modelled_cords")
    total = entry.get("cord_write_sites_in_firmware")
    if modelled and total:
        parts.append(f"Coverage: {modelled} of {total} modulation cord sites "
                     f"found in the firmware are reproduced.")
    parts.extend(str(c) for c in (entry.get("caveats") or []))
    return " ".join(parts)


def _entry(source_format: str, target_format: str) -> Optional[dict]:
    data = _contract_data()
    if not data:
        return None
    for entry in data.get("paths", []):
        if (_norm(entry.get("source")) == _norm(source_format)
                and _norm(entry.get("target")) == _norm(target_format)):
            return entry
    return None


def status(source_format: str, target_format: str) -> PathStatus:
    """Can this path be converted the way the device would?

    Unknown pairs are UNAVAILABLE with a reason, never available-by-default:
    a source the device cannot import at all (a SoundFont) has no device
    behaviour to match, and silently offering one would be the failure
    mpc2emu specifically asked us to avoid -- a user converting in
    "match the device" mode and receiving our normal output with no way to
    tell.
    """
    entry = _entry(source_format, target_format)
    if entry is not None:
        if entry.get("status") == "implemented":
            if source_format.upper() == "AKAI":
                # MEASURED 2026-09-22, not read: `--firmware-sim` on an AKAI
                # PROGRAM FILE produces output byte-identical to a normal
                # conversion (355054 bytes, cmp clean), exits 0 and warns
                # nothing. mpc2emu's registry lambdas for .a3p/.s3p/.p3/.p1
                # drop **kw, and their akai_s3000_parser has no simulation at
                # all; only the disc-IMAGE path carries it. Our conversion
                # calls parse_akai_program, i.e. the hollow shape.
                #
                # The contract says "implemented" for (akai, e4b) and does not
                # distinguish the two input shapes, so believing it here would
                # ship ordinary output under a device-fidelity promise -- the
                # one failure this whole module exists to prevent. Reported;
                # remove this branch when they answer, not before.
                return PathStatus(
                    False, "not usable from here yet: upstream's AKAI "
                           "simulation works on a disc image, but on a "
                           "program file — the shape VinSamLib converts — "
                           "the flag is silently ignored and produces an "
                           "ordinary conversion (measured 2026-09-22)")
            if not INVOCABLE_HERE:
                return PathStatus(
                    False, "implemented upstream, but VinSamLib has no way to "
                           "ask for it yet")
            return PathStatus(True, "")
        # Their reason strings are written to be shown as-is, and the
        # generator's own test refuses a non-implemented path with an empty
        # one -- so an empty reason here means the file is not what it
        # claims, and inventing a sentence would paper over that.
        return PathStatus(False, str(entry.get("reason")
                                     or entry.get("status") or "unavailable"))
    got = _PROVISIONAL.get((source_format, target_format))
    if got is not None and got.available and not INVOCABLE_HERE:
        return PathStatus(
            False, "implemented upstream, but VinSamLib has no way to ask "
                   "for it yet — mpc2emu's flag and generated contract are "
                   "still to come")
    if got is None:
        return PathStatus(
            False, f"{source_format or 'this source'} is not a format any of "
                   f"these samplers can import, so there is no device "
                   f"behaviour to match")
    return PathStatus(got.available,
                      f"{got.reason} (as of {PROVISIONAL_AS_OF})")


def _norm(name) -> str:
    """Their labels vs ours: they say "ensoniq", we say "EPS"."""
    return str(name or "").upper().replace("ENSONIQ", "EPS")
