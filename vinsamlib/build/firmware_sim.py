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

from dataclasses import dataclass
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


def load_contract(data: Optional[dict]) -> None:
    """Install mpc2emu's generated contract, replacing the provisional table.

    Kept as a setter rather than a file read so that whatever shape the
    generated file arrives in is adapted in ONE place, and so the tests can
    install a contract without a file.
    """
    global _contract
    _contract = data


def status(source_format: str, target_format: str) -> PathStatus:
    """Can this path be converted the way the device would?

    Unknown pairs are UNAVAILABLE with a reason, never available-by-default:
    a source the device cannot import at all (a SoundFont) has no device
    behaviour to match, and silently offering one would be the failure
    mpc2emu specifically asked us to avoid -- a user converting in
    "match the device" mode and receiving our normal output with no way to
    tell.
    """
    if _contract is not None:
        for entry in _contract.get("paths", []):
            if (_norm(entry.get("source")) == source_format.upper()
                    and _norm(entry.get("target")) == target_format.upper()):
                ok = entry.get("status") == "implemented"
                return PathStatus(ok, entry.get("status", "unknown"))
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
    return str(name or "").upper().replace("ENSONIQ", "EPS")
