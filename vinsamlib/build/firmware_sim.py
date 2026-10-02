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

**The table below is PROVISIONAL and exists to be deleted.** mpc2emu
publishes a JSON contract, which is what this reads instead of guessing.

⚠ **WHAT THAT CONTRACT IS, ACCURATELY.** Its own `note` says it is
"generated from the source by tools/firmware_sim_contract.py" and mpc2emu
assert it in their suite -- but `tools/` is gitignored in that repository
(verified here 2026-09-24: the artifact is tracked, the generator is not,
11 scripts on disk and 0 tracked). So the generator and the test that
asserts `on_disk == build()` exist in their working tree and in no clone.
We can read the artifact; we cannot reproduce it, diff it, or check that
what is published is what their code would produce.

**So treat it as ASSERTED BY mpc2emu, not as verifiable.** That is a weaker
guarantee than the file's own wording used to imply, and everything
downstream -- the availability gate, `input_shapes`, the caveats we render
verbatim, the `schema` pin -- rests on it. They told us rather than letting
us discover it, and on 2026-09-24 they put the caveat into the artifact's
own `note`, so it is now checkable against the file instead of resting on
this comment. `manual_firmware_matrix` asserts the invariant that survives
both futures: **either the generator the note credits is reachable, or the
note says it is not.** That passes the day they track it and delete the
caveat, and fails only if the caveat is dropped while the generator stays
invisible. ``_contract_data()`` reads it from the
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

#: Our labels for the two disc formats, as foreign_import uses them.
EPS_SOURCE = "EPS"
ROLAND_SOURCE = "Roland"

#: The input shape VinSamLib hands mpc2emu, WHICH DIFFERS BY SOURCE.
#:
#: For AKAI we assemble a program file out of a browsed volume and convert
#: that -- `convert._convert_akai_program` writes a `.p3`/`.a3p` into a temp
#: directory and parses it. For an Ensoniq or Roland disc we hand over the
#: IMAGE itself, because `foreign_import.parse_foreign` calls the registry's
#: `.iso` entry on the disc path; there is no loose-file form of a Roland
#: partial or an Ensoniq instrument to assemble.
#:
#: A single global value here was right while AKAI was the only implemented
#: source and WRONG the moment the other four landed -- it refused all of
#: them with "VinSamLib converts a program_file", which reads like an
#: upstream gap and is really us asking the wrong question.
_INPUT_SHAPE_BY_SOURCE = {"AKAI": "program_file"}
DEFAULT_INPUT_SHAPE = "disk_image"


def required_input_shape(source_format: str) -> str:
    return _INPUT_SHAPE_BY_SOURCE.get(source_format.upper(), DEFAULT_INPUT_SHAPE)


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
        False,
        "this mpc2emu checkout carries no firmware-simulation "
        "contract, so no path can be confirmed",
    ),
    ("AKAI", "E4B"): PathStatus(
        False,
        "this mpc2emu checkout carries no firmware-simulation "
        "contract, so no path can be confirmed",
    ),
    ("Roland", "E4B"): PathStatus(False, "not started upstream (considered feasible)"),
    ("Roland", "KRZ"): PathStatus(False, "not started upstream (considered feasible)"),
    ("EPS", "KRZ"): PathStatus(
        False, "not started upstream — the device's write list has not been " "read yet"
    ),
    ("EPS", "E4B"): PathStatus(
        False, "blocked upstream — a struct nobody has been able to locate"
    ),
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

#: The contract schema this code was written against. mpc2emu states the
#: rule in the artifact itself: read any key you know while `schema` equals
#: the number you were written against, and refuse rather than guess when it
#: is higher. Bumping this is a deliberate act after reading their changelog,
#: never a way to silence the refusal.
KNOWN_SCHEMA = 1

_contract: Optional[dict] = None  # noqa: UP045
_contract_tried = False
#: Why the contract was refused, if it was -- shown instead of the generic
#: "no contract" sentence, so a schema bump reads as a version mismatch
#: rather than as a missing file.
_contract_refusal = ""

#: Where mpc2emu generates it. Read from the configured checkout, never
#: copied here: a copy is a second source of truth that goes stale silently,
#: which is the whole failure the generated file exists to prevent.
CONTRACT_RELPATH = Path("docs") / "firmware_sim_contract.json"


def load_contract(data: Optional[dict]) -> None:  # noqa: UP045
    """Install mpc2emu's generated contract, replacing the provisional table.

    A setter as well as a file read, so tests can install one without a file.
    """
    global _contract, _contract_tried
    _contract = data
    _contract_tried = True


def _contract_data() -> Optional[dict]:  # noqa: UP045
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
    global _contract, _contract_tried, _contract_refusal
    if _contract_tried:
        return _contract
    _contract_tried = True
    try:
        from ..config import Config

        path = Config.load().mpc2emu_path / CONTRACT_RELPATH
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        _contract = None
        return _contract
    # PIN THE SCHEMA. mpc2emu's rule: adding a key keeps the number,
    # removing or re-meaning one bumps it. So a number we do not know means
    # a key we read may be gone or may mean something else -- and finding
    # that out as a KeyError from inside the dialog's own display code is
    # the worst available outcome. Refuse, keep the reason, and fall back to
    # offering nothing.
    schema = data.get("schema")
    if schema is not None and schema > KNOWN_SCHEMA:
        _contract_refusal = (
            f"this mpc2emu checkout's firmware-simulation contract is "
            f"schema {schema} and VinSamLib was written against "
            f"{KNOWN_SCHEMA}; a higher number means a key was removed or "
            f"re-meant, so nothing here can be trusted to still mean what "
            f"it says"
        )
        _contract = None
        return _contract
    _contract = data
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
    # Their sentence, written to be shown: what this path was actually
    # checked against, which for these is the device's own output rather
    # than a reading of its code. Shown FIRST -- it is the strongest thing
    # on the row and it frames everything after it as a known residual.
    verified = entry.get("verified_against_device")
    if verified:
        parts.append(str(verified))
    modelled = entry.get("modelled_cords")
    total = entry.get("cord_write_sites_in_firmware")
    if modelled and total:
        parts.append(
            f"Coverage: {modelled} of {total} modulation cord sites "
            f"found in the firmware are reproduced."
        )
    parts.extend(str(c) for c in (entry.get("caveats") or []))
    return " ".join(parts)


#: Jan's wording, carried in the contract's `modes` block so mpc2emu's CLI,
#: their README and this dialog all say the same two things. Fallback only --
#: the contract's own strings win.
_MODE_LABELS = {
    "firmware": "convert as the firmware would",
    "best": "convert as good as possible",
}


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
    modes = list(entry.get("modes_offered") or [])
    if source_format.upper() in {m.upper() for m in NO_MEASURED_ALTERNATIVE}:
        # Never ADD a mode -- only drop the one we cannot justify offering.
        modes = [m for m in modes if m != "best"] or modes
    return modes


#: Sources with NO INSTRUMENT ON THE BENCH, where this project therefore has
#: no basis for a conversion it can call better than the device's own.
#:
#: **A VinSamLib policy, applied on top of the contract, and it exists
#: because of a mistake of ours.** We measured that disc->KRZ output differs
#: between the two modes (97 bytes on the EPS reference disc, 14 on the
#: Roland one) and reported the difference; mpc2emu read that as evidence
#: that both modes exist there and added `best` to `modes_offered`. But the
#: difference is our KRZ writer filling in program fields the K2000's own
#: importer leaves at template defaults -- in ten of those fourteen bytes we
#: write a value and the device writes zero -- using envelope and filter
#: laws measured on an E4XT and an S3000XL. Applying those to Roland
#: material is not an improvement, it is a guess with no reference, and
#: "convert as good as possible" promises the user something nobody here
#: can check.
#:
#: AKAI is deliberately absent: an S3000XL IS on the bench, so there the
#: better conversion is measured and the choice is real.
#:
#: This filter disappears of its own accord if mpc2emu drop `best` from
#: those paths again -- it removes a mode, never adds one, so it can only
#: ever agree with a narrower contract.
NO_MEASURED_ALTERNATIVE = frozenset({EPS_SOURCE, ROLAND_SOURCE})


def target_restriction(source_format: str) -> Optional[dict]:  # noqa: UP045
    """mpc2emu's `source_target_restriction` for this source, or None.

    **Binds EVERY conversion from that source, not just the simulated one.**
    Its `applies_to` lists both `firmware` and `best`, and that is the half a
    reader would assume away: it is not a simulation rule a user escapes by
    choosing the ordinary conversion.

    The reasoning is the one Jan settled about modes, carried a step: these
    readers extract no filter, envelope, LFO or velocity values, because
    everything known about the format came from the samplers' own importers.
    So the only thing that can tell us a conversion is right at all is a
    machine that imports the same disc and can be diffed against it. A real
    E4XT and a real K2000 do. Nothing imports an EPS or S-7xx disc and
    writes EIII, AKAI or TAL, so for those targets no evidence could exist.

    A limit on what can be JUSTIFIED, not on what could be produced -- we
    were producing them, and they were files nothing could check.
    """
    data = _contract_data()
    if not data:
        return None
    rule = data.get("source_target_restriction")
    if not rule:
        return None
    sources = {_norm(x) for x in rule.get("sources") or ()}
    return rule if _norm(source_format) in sources else None


def allowed_targets(source_format: str) -> Optional[list]:  # noqa: UP045
    """Target formats this source may convert to, or None if unrestricted.

    Returned in OUR labels, upper-cased, so a caller can compare against the
    picker's entries without knowing mpc2emu writes them lower-case.
    """
    rule = target_restriction(source_format)
    if not rule:
        return None
    return [str(t).upper() for t in rule.get("allowed_targets") or ()]


def refuse_target(source_format: str, target_format: str) -> str:
    """Why this pair is refused, or "" if it is allowed."""
    allowed = allowed_targets(source_format)
    if allowed is None or target_format.upper() in allowed:
        return ""
    rule = target_restriction(source_format) or {}
    return (
        f"{source_format} converts to {', '.join(allowed)} only. "
        f"{rule.get('why', '')}"
    ).strip()


def sole_mode(source_format: str, target_format: str) -> Optional[str]:  # noqa: UP045
    """The only mode this path offers, or None when it offers 0 or 2.

    A path offering exactly one mode is not a path with a choice withheld:
    it is a path where the other mode does not exist. Running anything else
    would be inventing a mode mpc2emu does not claim -- which for the disc
    formats means converting with our own defaults on material where nothing
    beyond the firmware's own reading was ever extracted.
    """
    modes = modes_offered(source_format, target_format)
    return modes[0] if len(modes) == 1 else None


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
    # Through modes_offered(), never the raw entry: NO_MEASURED_ALTERNATIVE
    # is applied there, and reading the contract directly here would draw a
    # chooser for a mode the policy has just withdrawn.
    for entry in data.get("paths", []):
        if _norm(entry.get("source")) == _norm(source_format):
            target = str(entry.get("target") or "")
            if len(modes_offered(source_format, target)) > 1:
                return True
    return False


def _entry_for_source(source_format: str) -> Optional[dict]:  # noqa: UP045
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
    return any(
        _norm(e.get("target")) == _norm(target_format)
        and e.get("status") == "implemented"
        for e in data.get("paths", [])
    )


def _entry(source_format: str, target_format: str) -> Optional[dict]:  # noqa: UP045
    data = _contract_data()
    if not data:
        return None
    for entry in data.get("paths", []):
        if _norm(entry.get("source")) == _norm(source_format) and _norm(
            entry.get("target")
        ) == _norm(target_format):
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
            want = required_input_shape(source_format)
            if shapes is not None and want not in shapes:
                return PathStatus(
                    False,
                    f"upstream implements this path for "
                    f"{', '.join(shapes) or 'another input shape'}, "
                    f"but VinSamLib hands it a {want}",
                )
            if not INVOCABLE_HERE:
                return PathStatus(
                    False,
                    "implemented upstream, but VinSamLib has no way to "
                    "ask for it yet",
                )
            return PathStatus(True, "")
        # Their reason strings are written to be shown as-is, and the
        # generator's own test refuses a non-implemented path with an empty
        # one -- so an empty reason here means the file is not what it
        # claims, and inventing a sentence would paper over that.
        return PathStatus(
            False, str(entry.get("reason") or entry.get("status") or "unavailable")
        )
    if _entry_for_source(source_format) is not None:
        # The source IS one a sampler imports -- there is simply no
        # simulation for this target. Saying "AKAI is not a format any of
        # these samplers can import" was flatly wrong and rendered verbatim
        # in the dialog on every target change.
        return PathStatus(
            False,
            f"there is no firmware simulation that writes "
            f"{target_format} from {source_format}",
        )
    if _contract_refusal:
        return PathStatus(False, _contract_refusal)
    got = _PROVISIONAL.get((source_format, target_format))
    if got is not None and got.available and not INVOCABLE_HERE:
        return PathStatus(
            False,
            "implemented upstream, but VinSamLib has no way to ask "
            "for it yet — mpc2emu's flag and generated contract are "
            "still to come",
        )
    if got is None:
        return PathStatus(
            False,
            f"{source_format or 'this source'} is not a format any of "
            f"these samplers can import, so there is no device "
            f"behaviour to match",
        )
    return PathStatus(got.available, f"{got.reason} (as of {PROVISIONAL_AS_OF})")


def _norm(name) -> str:
    """Their labels vs ours: they say "ensoniq", we say "EPS"."""
    return str(name or "").upper().replace("ENSONIQ", "EPS")
