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
suite, precisely so this cannot drift. ``_contract_data()`` reads it from the
configured checkout; ``load_contract()`` is the test-only setter. The
provisional statuses below are the fallback for a checkout without the file,
and they are deliberately more conservative than the contract -- they come from
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

#: The input shape VinSamLib actually hands mpc2emu. We assemble a program
#: file and convert that; we never hand over a disk image. A path
#: implemented only for images is therefore unusable here however green the
#: contract's `status` reads -- measured the hard way on 2026-09-22.
REQUIRED_INPUT_SHAPE = "program_file"


@dataclass(frozen=True)
class PathStatus:
    available: bool
    reason: str


#: Keyed (source format, target format), using our own format labels.
# **A FALLBACK, AND DELIBERATELY PESSIMISTIC.** These fire only when the
# generated contract cannot be read at all. They no longer try to track what
# mpc2emu has built -- the 2026-09-22 entries here said AKAI->E4B was still
# being wired hours after it shipped -- because a second table that drifts is
# exactly what the generated file exists to replace. Without the contract we
# cannot confirm a path, so we offer none.
_PROVISIONAL = {
    ("AKAI", "KRZ"): PathStatus(
        False, "this mpc2emu checkout carries no firmware-simulation "
               "contract, so no path can be confirmed"),
    ("AKAI", "E4B"): PathStatus(
        False, "this mpc2emu checkout carries no firmware-simulation "
               "contract, so no path can be confirmed"),
    ("Roland", "E4B"): PathStatus(False, "not started upstream (considered feasible)"),
    ("Roland", "KRZ"): PathStatus(False, "not started upstream (considered feasible)"),
    ("EPS", "KRZ"): PathStatus(
        False, "not started upstream — the device's write list has not been "
               "read yet"),
    ("EPS", "E4B"): PathStatus(
        False, "blocked upstream — a struct nobody has been able to locate"),
}

#: Upstream having a path working is necessary but NOT sufficient: we also
#: need something to call. True since 2026-09-22, when their parser took
#: `firmware_sim` and their KRZ writer already did -- convert.py threads both
#: from `match_device_import`, and BOTH halves or neither, since a simulating
#: writer over a normally-parsed source is a meaningless hybrid.
#:
#: Kept separate from the per-path table on purpose: "they can do it" and "we
#: can ask for it" are different facts, and conflating them is how the AKAI
#: arm nearly shipped hollow.
INVOCABLE_HERE = True

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

    **A failed read is cached for the life of the process.** Pointing
    Settings at a checkout that has the file will not take effect until a
    restart -- which is the model Settings already states for the mpc2emu
    path itself, so this does not add a new surprise, but it is the reason
    a freshly-configured checkout still shows every path unavailable.
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


#: Jan's wording, carried in the contract's `modes` block so mpc2emu's CLI,
#: their README and this dialog all say the same two things. Fallback only --
#: the contract's own strings win.
_MODE_LABELS = {"firmware": "convert as the firmware would",
                "best": "convert as good as possible"}


def mode_label(mode: str) -> str:
    data = _contract_data() or {}
    return (data.get("modes") or {}).get(mode) or _MODE_LABELS.get(mode, mode)


def modes_offered(source_format: str, target_format: str) -> list:
    """Which of `firmware` / `best` this path can actually offer.

    **A different axis from ``status``, and conflating them misleads.** The
    four Roland and Ensoniq paths offer one mode, and that mode is currently
    not implemented -- so such a disc has exactly one thing to offer and it
    is not ready. Gate on ``status``; use this only to decide whether a
    CHOICE exists to draw.
    """
    entry = _entry(source_format, target_format)
    if entry is None:
        return []
    return list(entry.get("modes_offered") or [])


def offers_a_choice(source_format: str) -> bool:
    """Is there any target where this source offers both modes?

    False means the user is not being denied anything and no chooser should
    be drawn: mpc2emu's Roland and Ensoniq readers extract no filter,
    envelope or LFO fields at all -- measured -- because everything known
    about those disc formats was read out of the samplers' own import
    routines to begin with. A "convert as good as possible" mode would
    differ only in our writer's defaults, which convert nothing. AKAI is the
    exception: its reader carries laws measured on a real S3000XL that both
    samplers discard, so there the two modes are genuinely different
    products.
    """
    data = _contract_data()
    if not data:
        return source_format.upper() == "AKAI"
    for entry in data.get("paths", []):
        if _norm(entry.get("source")) == _norm(source_format):
            if len(entry.get("modes_offered") or []) > 1:
                return True
    return False


def _entry_for_source(source_format: str) -> Optional[dict]:
    """Any contract path with this source, regardless of target."""
    data = _contract_data()
    if not data:
        return None
    for entry in data.get("paths", []):
        if _norm(entry.get("source")) == _norm(source_format):
            return entry
    return None


def target_has_any_simulation(target_format: str) -> bool:
    """Is there ANY implemented simulation writing this target format?

    The source-free half of the question, for the runtime guard in
    convert.py: a caller that sets device matching with an EIII or AKAI
    target is asking for something no path can deliver, and that must be
    refused where the conversion runs rather than only in the dialog.
    """
    data = _contract_data()
    if not data:
        return target_format.upper() in ("E4B", "KRZ")
    return any(_norm(e.get("target")) == _norm(target_format)
               and e.get("status") == "implemented"
               for e in data.get("paths", []))


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
            # **Honour input_shapes.** mpc2emu added it on 2026-09-22 after
            # this exact trap: `status: implemented` was true of the disc
            # image and false of the program file, and believing the status
            # alone would have shipped an ordinary conversion under a
            # device-fidelity promise. VinSamLib converts PROGRAM FILES --
            # bank_pane assembles one and convert.py parses it -- so that is
            # the shape to require, and requiring it by name means a future
            # path implemented for discs only refuses here by itself.
            shapes = entry.get("input_shapes")
            if shapes is not None and REQUIRED_INPUT_SHAPE not in shapes:
                return PathStatus(
                    False, f"upstream implements this path for "
                           f"{', '.join(shapes) or 'another input shape'}, "
                           f"but VinSamLib converts a {REQUIRED_INPUT_SHAPE}")
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
    if _entry_for_source(source_format) is not None:
        # The source IS one a sampler imports -- there is simply no
        # simulation for this target. Saying "AKAI is not a format any of
        # these samplers can import" was flatly wrong and rendered verbatim
        # in the dialog on every target change.
        return PathStatus(
            False, f"there is no firmware simulation that writes "
                   f"{target_format} from {source_format}")
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
