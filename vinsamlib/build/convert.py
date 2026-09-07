"""
Wraps mpc2emu's own parse -> Bank -> process -> write round trip for the
vintage resample/reduce conversion options panel. Operates on an
already-assembled, on-disk bank file (the output of banks.e4b.assemble()/
banks.krz.assemble()/banks.eiii.assemble()) -- never touches assemble()
itself, which must stay byte-verbatim (see its own docstring: going
through models.common.Bank would silently degrade real commercial banks).
This is therefore always a full pre/post-processing pass on a whole bank
file, producing a NEW temp file -- the input file itself is never mutated.

E4B, KRZ and EIII are all readable *inputs*: mpc2emu's parsers.krz_parser
(added 2026-07-27, corpus-verified against 593 real .KRZ files) made KRZ
a real source format, and parsers.eiii_parser (added 2026-07-28,
corpus-verified against 1118 real EIII/EIIIX/ESI bank images, all parsing
cleanly -- 1017 of them are the directory-listed banks of those discs, the
rest deleted banks still physically present in free space, per mpc2emu's
2026-08-01 filesystem audit) does the same for EIII. The *output* format is a free, independent choice (target_format)
regardless of source -- any of the three can go to any of the three, all
through the identical reduce/resample/write pipeline. _sniff_format()
below reads the real on-disk magic bytes to pick the right parser rather
than trusting a file extension.

Applying this re-encodes a bank through mpc2emu's own Bank model; a few
advanced parameters not covered by that model may reset to defaults --
an unavoidable cost of using mpc2emu's DSP at all, but worth disclosing
to the user (see ui/convert_options_dialog.py).
"""

from __future__ import annotations

import array
import contextlib
import io
import math
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .. import tempdirs
from ..filenames import safe_filename
from ..mpc2emu_bridge import (bank_splitter, diagnostics, e4b_parser, e4b_writer,
                                eiii_parser, eiii_writer, krz_parser, krz_writer,
                                models_common, resampler, start_trim, tail_trim,
                                zone_reducer)

_CONVERT_TEMP_PREFIX = "vinsamlib_convert_"


def _sanitize_stem(name: str) -> str:
    """A real preset name is free-form (e.g. "CL EspHdFst/Sld" -- "/" used
    literally as part of the name, not a separator) but has to survive as a
    single path component once used as a temp filename stem: `tmp_dir /
    f"{stem}.e4b"` silently turns an embedded "/" into an extra directory
    level that was never created, so writing to it raises FileNotFoundError.

    Delegates to filenames.safe_filename, which is an allowlist. This used to
    strip only the characters Windows forbids, and 30 names in a real 7 931-name
    library got through it -- 21 with control characters and 9 ending in a dot."""
    return safe_filename(name, fallback="preset")


class ConvertOpError(RuntimeError):
    """Raised for any failed conversion operation; message is safe to show the user."""


@dataclass(frozen=True)
class ConversionOptions:
    target_format: str = "E4B"                    # "E4B" | "KRZ" | "EIII" -- the OUTPUT format
    resample_profile: Optional[str] = None        # "emulator2" | "emax1" | None (off)
    no_bandpass: bool = False
    resample_keep_gain: bool = False
    max_sample_rate: Optional[int] = None          # Hz; None/0 means don't apply this step
    reduce_key_zones_pct: float = 0.0
    reduce_velocity_layers_pct: float = 0.0
    mono: Optional[str] = None                     # None (keep stereo) | "mix" | "left" | "right"
    pan_law: str = "hardware"                      # "hardware" | "constant-power"; E4B only
    # Trim thresholds are stored the way mpc2emu's CLI accepts them -- a
    # POSITIVE depth below peak (72 = "silence only", 45 = into the attack/
    # release) -- and negated at the call site, exactly as convert.py's own
    # `-abs(args.trim_start)` does. None means the step is off.
    trim_start_db: Optional[float] = None
    trim_start_fade_ms: float = 5.0
    trim_start_keep_loops: bool = False
    trim_tail_db: Optional[float] = None
    trim_tail_fade_ms: float = 5.0
    trim_tail_keep_loops: bool = False
    # KRZ only, and both are mpc2emu's own flags (--krz-faithful,
    # --krz-drum-program) rather than anything this project invents.
    #
    # WHY THEY ARE HERE AT ALL. A K2000 program with more than three SPLIT
    # layers is a drum program, and a drum program is silent on every normal
    # MIDI channel. mpc2emu shipped faithful output as the only behaviour,
    # which meant a four-layer piano converted cleanly, passed every check,
    # and made no sound. Since 2026-08-24 their default is the opposite: fuse
    # disjoint layers until three remain, averaging the continuous fields.
    # That default arrives here whether or not anything asks for it, so the
    # choice has to be reachable from the GUI and the change has to be
    # visible -- see _collect_diagnostics below for the visible half.
    krz_faithful_layers: bool = False
    krz_drum_program: bool = False

    def is_noop(self, source_format: str = "E4B") -> bool:
        """`source_format` matters now that KRZ can be a source too: a
        KRZ->KRZ request with no other options set is just as much a
        genuine no-op as an E4B->E4B one -- skipping the round trip in
        that case avoids needlessly losing fidelity on any advanced
        parameter mpc2emu's Bank model doesn't carry, for zero benefit.
        Defaults to "E4B" for existing callers that only ever process
        E4B sources (the Pending-pane per-bank feature, the HW test
        matrix) and don't pass this explicitly."""
        # krz_faithful_layers is deliberately NOT in this list. On a KRZ->KRZ
        # request it asks for the layers the source already has, and skipping
        # the round trip delivers exactly that while also keeping every
        # advanced parameter mpc2emu's Bank model does not carry -- forcing a
        # rewrite to honour it would lose fidelity in the name of preserving
        # it. krz_drum_program IS in the list: asking for a drum program is
        # asking for a file the source is not.
        return (self.target_format == source_format
                and self.resample_profile is None
                and not self.max_sample_rate
                and self.reduce_key_zones_pct <= 0
                and self.reduce_velocity_layers_pct <= 0
                and self.mono is None
                and self.pan_law == "hardware"
                and not self.krz_drum_program
                and self.trim_start_db is None
                and self.trim_tail_db is None)


def _run_captured(fn: Callable, *args, **kwargs) -> Any:
    # Same shape as build/images.py's own _run_captured: mpc2emu's
    # processors print progress to stdout, which would otherwise leak
    # into VinSamLib's own console; captured text rides along on any
    # raised ConvertOpError so a failure is still diagnosable.
    #
    # The reason goes LAST, after the captured log, because every consumer of
    # this message shows the final line and nothing else (a status bar, a
    # tooltip, a message box -- see ui/workers.last_error_line). With the
    # order reversed, mpc2emu refusing a MIDI program with a written-out
    # sentence surfaced as "Parsing XPM: /some/path" -- its last progress
    # line -- and the actual reason was never shown to anyone.
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            return fn(*args, **kwargs)
    except Exception as ex:
        raise ConvertOpError(f"{buf.getvalue()}\n\n{ex}".strip()) from ex


def _apply_max_sample_rate(bank: Any, hz: int) -> None:
    """convert.py's own blanket-downsample loop (convert.py:786-829) is
    inline code in main(), not a reusable function -- this is that same
    handful of lines, re-implemented against the already-importable
    resampler.resample_to_rate(). Only ever downsamples (resample_to_rate
    itself is a no-op if dst_rate >= the sample's own rate)."""
    for i, sample in enumerate(bank.samples):
        bank.samples[i] = resampler.resample_to_rate(sample, hz, verbose=False)


# Below this per-sample Pearson correlation, averaging both sides (--mono
# mix) is considered likely to cancel signal rather than just narrow the
# image -- same threshold and rationale as mpc2emu's own convert.py CLI
# warning (models.common.channel_correlation's docstring): measured over 247
# real stereo E-mu samples, the median was 0.076 and none exceeded 0.9, so
# 0.3 sits well clear of ordinary decorrelated material while still catching
# it, rather than flagging everything.
MONO_MIX_RISK_THRESHOLD = 0.3


def _apply_pan_law(bank: Any) -> int:
    """Subtract the E4XT's pan-loudness excess from every panned zone, so
    total power stays put across pan instead of rising with it. Returns the
    number of zones touched.

    Panning the E4XT makes a voice LOUDER -- measured +2.88 dB at pan 0.5 and
    +4.32 dB at pan 1.0, and the curve is identical at every volume (spread
    0.00/0.00/0.21 dB across 0/-6/-12) and unaffected by the filter, which is
    what makes one correction curve valid at all (mpc2emu 413d84c, measured
    2026-08-01). SFZ and SF2 assume roughly constant power, so without this a
    hard-panned voice from such a source arrives ~4.5 dB hotter than its
    author intended relative to a centred one.

    What actually gets subtracted is mpc2emu's FIT of those measurements,
    4.54 * |pan|^0.75, not the measurements themselves -- so at pan 0.5 the
    correction is 2.70 dB rather than the 2.88 dB measured there, within the
    fit's stated 0.50 dB max residual. e4xt_pan_excess_db() owns that curve;
    don't second-guess it here.

    Applied here in the pipeline rather than left to mpc2emu's writer, and
    that placement is the point: unlike the cutoff and zone-gain corrections
    -- which fix a MAPPING, are always on, and which e4b_parser inverts
    exactly on read-back -- this alters the MATERIAL. It lands in the volume
    byte where it is indistinguishable from a volume the user chose, so the
    parser cannot undo it and an E4B->E4B pass would drift further every
    time. That makes it one-way and opt-in, belonging with mono/resample/
    trim rather than with the corrections."""
    n = 0
    for preset in bank.presets:
        for voice in preset.voices:
            for zone in voice.zones:
                excess = models_common.e4xt_pan_excess_db(zone.pan)
                if excess > 0.01:      # same negligible-excess floor as convert.py
                    zone.volume -= excess
                    n += 1
    return n


def _apply_mono(bank: Any, method: str) -> None:
    for sample in bank.samples:
        models_common.to_mono(sample, method)


def stereo_mono_risk(samples: list, method: str = "mix") -> dict:
    """Read-only pre-check for a stereo->mono reduction: does NOT modify
    `samples`. Only 'mix' (averaging both sides) carries a cancellation
    risk -- picking a side ('left'/'right') never can, so those always
    report no risk. Mirrors mpc2emu's own convert.py --mono mix warning,
    down to the 0.3 correlation threshold (see MONO_MIX_RISK_THRESHOLD).

    Returns {"stereo_count": int, "decorrelated": [(name, r), ...],
    "worst_r": float | None} -- `decorrelated` lists every stereo sample
    whose channel correlation fell below the threshold, worst first."""
    stereo = [s for s in samples if getattr(s, "channels", 1) == 2]
    if method != "mix" or not stereo:
        return {"stereo_count": len(stereo), "decorrelated": [], "worst_r": None}
    decorrelated = []
    for s in stereo:
        r = models_common.channel_correlation(s.data)
        if r < MONO_MIX_RISK_THRESHOLD:
            decorrelated.append((s.name, r))
    decorrelated.sort(key=lambda nr: nr[1])
    worst_r = decorrelated[0][1] if decorrelated else None
    return {"stereo_count": len(stereo), "decorrelated": decorrelated, "worst_r": worst_r}


def _note_name(midi: int) -> str:
    names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    return f"{names[midi % 12]}{midi // 12 - 1}"


def polyphony_risk(bank: Any, target_format: str) -> list[dict]:
    """Presets that stack more voices on a SINGLE note than the hardware
    can sound. Over that ceiling the extra voices aren't merely quiet --
    they're stolen, and which layers survive is the hardware's choice, so
    this is silent damage a size check can never catch: a preset can be
    tiny in bytes and still over budget.

    Both facts behind it were measured on the E4XT on 2026-07-31 and reached
    mpc2emu's own sizing path in its 6b12209: a STEREO sample costs TWO
    voices, and the ceiling is ~32 voices per NOTE rather than the E4XT's
    128-voice global polyphony. The counting itself (bank_splitter's
    peak_note_voices(), which sweeps key x velocity, and knows that zones
    inside one voice layer don't stack) and the limit itself are read out of
    mpc2emu rather than reimplemented here -- VinSamLib holds no second copy
    of a measured hardware law it could drift away from.

    Which formats have a ceiling is mpc2emu's answer too, not a list kept
    here: whatever is in its _VOICES_PER_NOTE gets checked. That was E4B
    alone until 2026-08-02, when a K2000R measurement added 'krz': 24 -- a
    stereo sample plateaus at 12 simultaneous notes where the same material
    in mono reaches 24, identically at velocity 100, 45 and 25, so the
    plateau is voice allocation and not output clipping. EIII has no
    measured per-note limit and so is still not checked; it will be the day
    upstream measures one, with no change needed here.

    Returns one dict per over-budget preset: {"preset", "voices", "limit",
    "key" (note name), "velocity", "stereo", "samples"} -- empty when the
    format has no measured limit, or nothing is over it."""
    # Private in mpc2emu, deliberately read rather than copied: these are
    # measured numbers, and the one thing worse than reaching into a private
    # name is silently disagreeing with the measurement it came from. Absent
    # (an older mpc2emu checkout) simply means no check.
    limit = getattr(bank_splitter, "_VOICES_PER_NOTE", {}).get(
        target_format.lower())
    if not limit:
        return []

    out: list[dict] = []
    for preset in bank.presets:
        needed = bank_splitter.preset_needed_samples(preset, bank.samples)
        voices, key, vel = bank_splitter.peak_note_voices(preset, needed)
        if voices <= limit:
            continue
        out.append({
            "preset": preset.name,
            "voices": voices,
            "limit": limit,
            "key": _note_name(key),
            "velocity": vel,
            "stereo": sum(1 for s in needed
                          if bank_splitter.sample_voice_cost(s) > 1),
            "samples": len(needed),
        })
    return out


def polyphony_risk_lines(risks: list[dict]) -> list[str]:
    """polyphony_risk() rendered for a GUI message box -- one line per
    preset, naming the two Convert Options controls that actually fix it
    (mpc2emu's own warning names its CLI flags, which don't exist here)."""
    lines = []
    for r in risks:
        # A risk carrying its own sentence renders verbatim. Not every risk
        # this list now holds is a polyphony one -- _verify_written adds
        # written-file findings through the same channel, because they reach
        # the user by the same route and a second mechanism would just be a
        # second thing to forget to display.
        if r.get("message"):
            lines.append(r["message"])
            continue
        why = (f" ({r['stereo']} of {r['samples']} samples are stereo, and a "
               f"stereo sample costs two voices)") if r["stereo"] else ""
        lines.append(
            f"\"{r['preset']}\" stacks {r['voices']} voices on {r['key']} at "
            f"velocity {r['velocity']}, over the {r['limit']}-voice-per-note "
            f"limit{why} -- the extra layers will be stolen on playback.")
    return lines


def suggest_mono_side(samples: list) -> dict:
    """Best-effort LEFT/RIGHT suggestion for when Mix is risky, based on
    average per-sample RMS loudness across the stereo samples given.

    mpc2emu's own db5d599 investigated -- and explicitly declined to ship --
    an automatic side-picker in the library itself: every signal it measured
    (dead channel, high correlation, one-sided clipping) either never fired
    or came down to about 1 dB of RMS asymmetry, "a coin-flip dressed up as
    intelligence." This is that same rough signal, surfaced only as a
    secondary nudge in the confirmation dialog, never as a claim of
    correctness -- picking EITHER side already fully avoids Mix's
    cancellation risk, so getting this "wrong" costs a little level, not
    cancelled audio, unlike Mix itself.

    Returns {"side": "left"|"right", "avg_db": float, "n": int} -- avg_db is
    the average |left_rms/right_rms| dB difference across the `n` stereo
    samples actually measured (n=0, side="left" if none were measurable --
    an arbitrary tie-break, not a real signal)."""
    diffs = []
    for s in samples:
        if getattr(s, "channels", 1) != 2:
            continue
        data = s.data[: len(s.data) // 4 * 4]
        if not data:
            continue
        a = array.array("h")
        a.frombytes(data)
        L, R = a[0::2], a[1::2]
        l_rms = (sum(x * x for x in L) / len(L)) ** 0.5
        r_rms = (sum(x * x for x in R) / len(R)) ** 0.5
        if l_rms <= 0 or r_rms <= 0:
            continue
        diffs.append(20 * math.log10(l_rms / r_rms))
    if not diffs:
        return {"side": "left", "avg_db": 0.0, "n": 0}
    avg = sum(diffs) / len(diffs)
    return {"side": "left" if avg >= 0 else "right", "avg_db": abs(avg), "n": len(diffs)}


def load_samples_for_test(bank_path: str) -> list:
    """Read-only: parses an already-assembled E4B/KRZ/EIII file just far
    enough to list its samples, for the Convert Options dialog's stereo
    Test button -- same parse-without-writing cost class as
    xpm_import.summarize_xpm(), never touches bank_path itself."""
    fmt = _sniff_format(bank_path)
    bank = _parse_by_format(bank_path, fmt)
    return bank.samples


def load_sources_samples_for_test(sources: list, fmt: str) -> list:
    """Read-only: assembles the given (bank, preset_obj) pairs the same way
    Build Image / convert_preset() would (banks.e4b/krz/eiii.assemble()),
    then parses the result back via load_samples_for_test() -- the one
    throwaway temp file mpc2emu itself requires to parse from is the only
    thing written. Used by the Pending pane's per-bank Convert Options and
    by Explorer's multi-preset "Import via mpc2emu..." to preview stereo
    content across the whole selection, not just one preset."""
    from ..banks import e4b as vs_e4b
    from ..banks import eiii as vs_eiii
    from ..banks import krz as vs_krz
    _ASSEMBLE = {"E4B": vs_e4b.assemble, "KRZ": vs_krz.assemble, "EIII": vs_eiii.assemble}
    _EXT = {"E4B": "e4b", "KRZ": "krz", "EIII": "e3x"}
    fn = _ASSEMBLE[fmt]
    data = fn(sources, bank_name="TestPreview") if fmt == "EIII" else fn(sources)
    # The file exists only to be parsed straight back -- the SampleData
    # returned carries its own PCM in memory -- so it goes at the end of the
    # block rather than living until shutdown.
    with tempdirs.temp_dir(_CONVERT_TEMP_PREFIX) as tmp_dir:
        tmp_path = tmp_dir / f"preview.{_EXT[fmt]}"
        tmp_path.write_bytes(data)
        return load_samples_for_test(str(tmp_path))


# ── mpc2emu's structured conversion warnings ────────────────────────────────

#: Severities worth interrupting the user for. INFO records are the running
#: commentary a CLI prints as it works; a message box is not a log.
_DIAG_SEVERITIES = ("warning", "error")


@contextlib.contextmanager
def _collect_diagnostics():
    """Collect mpc2emu's structured diagnostics for the block, if it has them.

    WHY THIS EXISTS. _run_captured reads its captured stdout only inside
    `except`, so everything a SUCCESSFUL writer printed is discarded. That is
    not a small loss: mpc2emu's KRZ writer prints, correctly and completely,
    that a program had more split layers than a K2000 can play on a normal
    channel -- the difference between a patch that sounds and one that does
    not -- and no user of this program has ever seen that line. It cost three
    sessions of their time to rediscover a fact their own build log had been
    stating all along, and a GUI user would have had less to go on: a bank
    that loads, shows no error, and makes no sound.

    Their fix (2026-09-05) is a thread-local sink that does not travel through
    stdout, so redirecting stdout no longer swallows it. `emit()` still prints
    exactly what it printed before, so nothing about the existing behaviour
    changes.

    Degrades to an empty list on an older checkout rather than refusing to
    convert: without diagnostics a conversion still produces the same file, it
    just cannot say what it changed on the way.
    """
    try:
        manager = diagnostics.collect()
    except Exception:
        yield []
        return
    with manager as records:
        yield records


@contextlib.contextmanager
def collect_diagnostics_into(risks_out: Optional[list]):
    """Collect mpc2emu's diagnostics for the block into an existing risks list.

    Public because the PARSE steps live in the import modules, outside
    _apply_and_write, and some codes are emitted there rather than by a
    writer -- KRZ_ROM_ONLY comes from their KRZ parser, the two SF2 codes
    from their SoundFont one. A parse and a write are sequential rather than
    nested, so two of these blocks over one operation collect each record
    once; it is only NESTING that would see a record twice, and nothing here
    nests them.
    """
    with _collect_diagnostics() as records:
        yield records
    if risks_out is not None:
        risks_out.extend(_diagnostic_risks(records))


def _diagnostic_risks(records) -> list[dict]:
    """mpc2emu Diagnostic records as risk dicts for the existing warning box.

    They ride the `risks_out` channel rather than a second mechanism, for the
    reason polyphony_risk_lines already gives: these reach the user by the
    same route, and a separate path would just be a second thing to forget to
    display.

    Branching is on `code`, which mpc2emu treats as a published contract, and
    never on the wording of `message`, which they are free to reword.
    """
    out: list[dict] = []
    for d in records:
        if getattr(d, "severity", "") not in _DIAG_SEVERITIES:
            continue
        detail = dict(getattr(d, "detail", None) or {})
        parts = [str(getattr(d, "message", "")).strip()]
        # A drum program keeps every layer, so it is honestly NOT content
        # loss, and mpc2emu correctly reports content_lost False for it. It is
        # still the worst outcome either project can produce, so it carries
        # its own flag and gets its own sentence -- "this will not sound"
        # is a different thing to tell someone than "this lost a layer".
        if detail.get("silent_on_normal_channel"):
            parts.append("A K2000 plays a drum program only on a drum "
                         "channel, so this preset will be SILENT on a normal "
                         "one.")
        remedy = str(getattr(d, "remedy", "") or "").strip()
        if remedy:
            parts.append(remedy)
        # Each part is a separate sentence from a separate field, and none of
        # them is guaranteed to be punctuated -- mpc2emu's messages are
        # written to be read at the end of a printed line, where the newline
        # does the work. Joined raw, a message and a remedy run together into
        # one unreadable run-on ("...ordinal has shifted address presets by
        # name..."), which is what this looked like on the first real import.
        said = []
        for i, part in enumerate([p for p in parts if p]):
            if i:
                part = part[0].upper() + part[1:]
            said.append(part if part[-1] in ".!?:" else part + ".")
        text = " ".join(said)
        subject = str(getattr(d, "subject", "") or "").strip()
        out.append({
            "message": f'"{subject}": {text}' if subject else text,
            "code": getattr(d, "code", ""),
            # A required field on their side since 2026-09-05, so it is read
            # as an attribute and not with a .get() that would quietly read
            # False on a checkout that does not have it.
            "content_lost": bool(getattr(d, "content_lost", False)),
            "detail": detail,
        })
    return out


def _krz_writer_kwargs(opts: ConversionOptions) -> dict:
    """The layer-handling arguments this mpc2emu's write_krz actually takes.

    Asked of the function rather than assumed, the same way
    manual_akai_partition_breaks asks before using `partitions`: these
    arrived on 2026-08-24 and a checkout from before that raises TypeError on
    a keyword it has never heard of. An older checkout keeps its own
    behaviour, which was faithful output.
    """
    try:
        names = krz_writer.write_krz.__code__.co_varnames
    except Exception:
        return {}
    kw = {}
    if "faithful_layers" in names:
        kw["faithful_layers"] = opts.krz_faithful_layers
    if "drum_program" in names:
        kw["drum_program"] = opts.krz_drum_program
    return kw


def _apply_and_write(bank: Any, opts: ConversionOptions, out_stem: str,
                     risks_out: Optional[list] = None) -> str:
    """Run the pipeline, and collect what mpc2emu says about it on the way.

    The split exists only so the collection wraps the WHOLE pipeline in one
    place: `collect()` nests, so one context manager here sees what any of the
    eight processors and the writer emitted, where a per-call return value
    would be eight plumbing sites to keep in step.
    """
    with collect_diagnostics_into(risks_out):
        return _apply_and_write_pipeline(bank, opts, out_stem, risks_out)


def _apply_and_write_pipeline(bank: Any, opts: ConversionOptions, out_stem: str,
                              risks_out: Optional[list] = None) -> str:
    """Shared tail end of apply_conversion() and build/xpm_import.py's
    import_xpm(): both start from a different parse step (an already-
    native E4B vs. a foreign XPM) but from an already-parsed mpc2emu Bank
    onward the pipeline is identical -- start trim, tail trim, pan law,
    mono reduction, then reduce, then resample, then the independent
    max-sample-rate pass, then write to whichever target format was chosen.

    That order is mpc2emu's own convert.py CLI order: the trims run first so
    every later stage sees the shortened samples at their real sizes; mono
    before reduce/resample so those see the halved sizes; reduce before
    resample matches convert.py's existing comment that thinning first means
    the slower vintage-resample step has fewer surviving samples to process.
    Pan law sits where convert.py puts it, just before mono, though nothing
    depends on that: it rewrites zone volumes while mono rewrites sample
    data, so the two don't interact.

    `risks_out`, when a list is passed, collects polyphony_risk() dicts for
    the FINISHED bank -- an out-parameter rather than a second return value
    so the three callers that don't care (and the workers that thread their
    return value straight into the UI) keep the signature they have."""
    if opts.trim_start_db is not None:
        _run_captured(start_trim.trim_start_bank, bank,
                      thresh_db=-abs(opts.trim_start_db),
                      fade_ms=opts.trim_start_fade_ms,
                      drop_full_loop=not opts.trim_start_keep_loops)

    if opts.trim_tail_db is not None:
        _run_captured(tail_trim.trim_tail_bank, bank,
                      thresh_db=-abs(opts.trim_tail_db),
                      fade_ms=opts.trim_tail_fade_ms,
                      drop_full_loop=not opts.trim_tail_keep_loops)

    # E4B only, matching mpc2emu's own gate (its convert.py applies --pan-law
    # for 'e4b' alone). No longer for want of a measurement on the K2000: as
    # of 2026-08-02 its law is measured and IS constant power, but hard pan
    # raises the live channel +3.0 dB there against the E4XT's +4.5 dB, so
    # the two cannot share one correction -- applying the E4XT's here would
    # bake the wrong number irreversibly into the volume byte. EIII remains
    # simply unmeasured.
    if opts.pan_law == "constant-power" and opts.target_format == "E4B":
        _run_captured(_apply_pan_law, bank)

    if opts.mono is not None:
        _run_captured(_apply_mono, bank, opts.mono)

    if opts.reduce_key_zones_pct > 0 or opts.reduce_velocity_layers_pct > 0:
        _run_captured(zone_reducer.reduce_bank, bank,
                      opts.reduce_key_zones_pct, opts.reduce_velocity_layers_pct)

    if opts.resample_profile:
        _run_captured(resampler.resample_bank, bank, opts.resample_profile,
                      bandpass=not opts.no_bandpass,
                      restore_level=not opts.resample_keep_gain)

    if opts.max_sample_rate:
        _run_captured(_apply_max_sample_rate, bank, opts.max_sample_rate)

    # After every step, never before: --mono halves each stereo zone's voice
    # cost and the zone reducer removes layers outright, so the only voice
    # count worth reporting is the one the file being written actually has.
    if risks_out is not None:
        risks_out.extend(polyphony_risk(bank, opts.target_format))

    # Session-scoped: this file is the RESULT, and the caller reads it after
    # we return -- New Bank re-parses it, the image builders copy it. Nothing
    # here can know when the last reader is done, so it is registered and
    # removed at shutdown instead.
    tmp_dir = tempdirs.session_temp_dir(_CONVERT_TEMP_PREFIX)
    if opts.target_format == "KRZ":
        out_path = tmp_dir / f"{out_stem}.krz"
        _run_captured(krz_writer.write_krz, bank, str(out_path),
                      **_krz_writer_kwargs(opts))
    elif opts.target_format == "EIII":
        out_path = tmp_dir / f"{out_stem}.e3x"
        _run_captured(eiii_writer.write_eiii, bank, str(out_path))
    else:
        out_path = tmp_dir / f"{out_stem}.e4b"
        _run_captured(e4b_writer.write_e4b, bank, str(out_path))
    _verify_written(bank, out_path, opts)
    # After the refusal check, not before: there is no point warning about the
    # zones of a bank that is about to be thrown away for losing samples.
    if risks_out is not None:
        zone_risk = _zone_loss_risk(bank, out_path, opts)
        if zone_risk is not None:
            risks_out.append(zone_risk)
    return str(out_path)


def _verify_written(bank: Any, out_path: Path, opts: ConversionOptions) -> None:
    """Read the freshly-written bank back and refuse it if samples went missing.

    A writer is the one stage whose failures cannot be seen from the inside:
    the Bank in memory is still correct, and the file is what the hardware
    will load. Real case, measured 2026-08-07 -- a vintage resample profile
    leaves some STEREO samples a half-frame long (`len(data) % 4 == 2`), and
    mpc2emu's E4B writer then declares a chunk two bytes shorter than what it
    writes. Every following chunk is misaligned, so a 77-sample bank reads
    back as **1 sample with 77 orphaned zones** -- and nothing, at any layer,
    printed a word about it. KRZ and EIII survive the same input, so this is
    not something a caller could have predicted from the options alone.

    **Necessary, not sufficient.** This counts samples, which is what the
    E4B chunk misalignment destroys. It cannot see the other half of the
    same upstream fault: the vintage profiles processed a stereo sample as
    one long mono stream, so the samples that DID survive have their
    channels smeared, and a KRZ or EIII bank built the same way kept every
    sample and every one of them is wrong. No count check can catch that.
    See the README's "Fixed defects" entry.

    Deliberately a VERIFICATION and not a correction. A correction for
    someone else's bug has to guess when to stop applying itself, and this
    module has already had one outlive its fault and start doing damage of
    its own (see the AKAI pan note above). Reading back what was written
    stays true no matter who fixes what, costs one parse, and catches every
    writer-side loss rather than the one shape known today.

    Uses VinSamLib's own byte-level readers rather than mpc2emu's, so the
    check is independent of the code that produced the file.
    """
    from ..banks import e4b as vs_e4b
    from ..banks import eiii as vs_eiii
    from ..banks import krz as vs_krz
    expected = len(bank.samples)
    if not expected:
        return
    try:
        data = out_path.read_bytes()
        if opts.target_format == "KRZ":
            got = len(vs_krz.parse_bytes(data, out_path.name).samples)
        elif opts.target_format == "EIII":
            got = len(vs_eiii.parse_bytes(data, out_path.name).samples)
        else:
            got = len(vs_e4b.parse_bytes(data, out_path.name).samples)
    except Exception:
        # Unreadable for some other reason is a separate problem, and the
        # caller will meet it soon enough; do not mask it as sample loss.
        return
    if got >= expected:
        return
    raise ConvertOpError(
        f"the converted {opts.target_format} bank came back with {got} of "
        f"{expected} sample(s) after being written, so it was discarded "
        f"rather than handed on. This is a fault in the writer, not in the "
        f"material: the conversion itself completed and the samples were all "
        f"present in memory. If a vintage resample profile is switched on, "
        f"try it off -- a resampled stereo sample can end half a frame long, "
        f"which the E4B writer mis-sizes.")


def _zone_loss_risk(bank: Any, out_path: Path, opts: ConversionOptions) -> Optional[dict]:
    """Warn when the written bank references FEWER zones than the Bank held.

    The other half of the check above, and the half its own docstring said it
    could not do. Counting samples catches a bank that came back short;
    it cannot catch one that kept every sample and stopped POINTING at them.

    Measured 2026-08-08, which is why this exists: a folder of 13 WAVs
    imported to EIII wrote all 13 samples and exactly ONE zone, so twelve of
    them could never sound.

    THE TRIGGER IS OVERLAPPING KEY RANGES, not the voice/zone shape. That
    correction came from mpc2emu after this was first reported, and it is
    kept here because the wrong version is the intuitive one: it is NOT true
    that "many zones in one voice" is the unsafe shape and "one zone per
    voice" the safe one. An EIII preset maps each key to exactly ONE note
    zone -- a real format limit, not a writer oversight -- so the writer
    resolved overlaps the way the sampler's own panel does, later wins, and
    dropped whatever was left holding no keys. Our 13 WAVs had filenames
    carrying no note names, so every zone spanned the whole keyboard and
    twelve lost every key. A 40-voice bank would have lost zones too, had any
    two of them overlapped.

    Fixed upstream in mpc2emu `4b0dcde`, which spreads a colliding voice
    across the linked-preset chain instead. This check stays anyway: it is a
    verification, and it stays true whoever fixes what.

    A WARNING, not a refusal, and not a correction. The bank is real and its
    audio is intact -- a user who wants it should get it -- but nobody should
    receive it without being told, which is what happened until this ran.
    Returns a risk dict for the same list polyphony findings use, or None.
    """
    from ..banks import e4b as vs_e4b
    from ..banks import eiii as vs_eiii
    # E4B and EIII. It was EIII-only for a day because E4B reported "140 of
    # 141" on ordinary conversions and nobody had explained it.
    #
    # THE EXPLANATION, and it is mundane: a zone whose sample index is 0 is an
    # UNASSIGNED zone -- index 0 is not a sample, it means "nothing here". The
    # writer correctly omits such a zone; this check counted it. 813 of 26 979
    # zone entries across 40 real banks are unassigned, so an "Untitled Preset"
    # holding one empty zone reads as one lost zone every time.
    #
    # I twice asserted this was NOT the empty-zone artefact, reasoning that
    # _parse_zone_refs counts index 0. That is exactly WHY it happens: we count
    # it, the writer drops it. mpc2emu's own diagnosis -- that our voice walk
    # lands short on the last voice's trailer -- did not hold either; the
    # written file's vpar[2:4] and vpar[4] both say zero zones for that preset,
    # so nothing is being misread.
    #
    # Counting only zones that reference a REAL sample makes both sides
    # like-for-like, which is what the comparison needed all along. KRZ stays
    # out for the original reason: it reaches samples through keymaps, so an
    # equivalent count needs the keymap walk and is not guessed at here.
    if opts.target_format not in ("E4B", "EIII"):
        return None
    # Only zones that actually RESOLVE to a sample, matching what a writer
    # emits. A ZoneMapping refers to its sample by NAME -- there is no
    # sample_index on it, and asking for one returned None for every zone,
    # which drove `wanted` to zero and silently switched this whole check off.
    # It read as "the false positive is fixed" and was caught only because
    # manual_names_e2e asserts the EIII loss must still be reported.
    have = {s.name for s in bank.samples}
    wanted = sum(1 for p in bank.presets for v in p.voices for z in v.zones
                 if getattr(z, "sample_name", None) in have)
    if not wanted:
        return None
    try:
        data = out_path.read_bytes()
        if opts.target_format == "E4B":
            parsed = vs_e4b.parse_bytes(data, out_path.name)
            got = sum(1 for p in parsed.presets for _off, idx in p.zone_refs
                      if idx and idx in parsed.samples)
        else:
            parsed = vs_eiii.parse_bytes(data, out_path.name)
            got = sum(1 for p in parsed.presets for _off, raw in p.zone_refs
                      if (raw & 0x3FFF) and (raw & 0x3FFF) in parsed.samples)
    except Exception:
        return None
    if got >= wanted:
        return None
    return {
        "kind": "zone_loss",
        "message": (
            f"The written {opts.target_format} bank points at {got} of "
            f"{wanted} zone(s). Its samples are all present and its audio is "
            f"intact, but {wanted - got} of them cannot be played by the "
            f"instrument as written. An EIII preset maps each key to exactly "
            f"one zone, so where two zones claim the same keys only one "
            f"survives — most often when the source gives no note information "
            f"to place samples by, and they all end up spanning the whole "
            f"keyboard. Naming the samples after the keys they play, or "
            f"setting their ranges in Adjust Sample Placement, avoids the "
            f"collision. E4B and KRZ keep overlapping zones and are "
            f"unaffected."),
    }


def _sniff_format(bank_path: str) -> str:
    """Real on-disk magic bytes, never the file extension (same convention
    as vfs/detect.py's own sniff())."""
    with open(bank_path, "rb") as f:
        head = f.read(16)
    if head[:4] == b"FORM" and head[8:12] == b"E4B0":
        return "E4B"
    if head[:4] == b"PRAM":
        return "KRZ"
    if len(head) == 16 and head[15] == 0:
        from ..banks import eiii as vs_eiii
        if vs_eiii.detect_format(head) is not None:
            return "EIII"
    raise ConvertOpError(f"not a recognized E4B, KRZ or EIII bank: {bank_path}")


def _parse_by_format(bank_path: str, fmt: str) -> Any:
    if fmt == "KRZ":
        return _run_captured(krz_parser.parse_krz, bank_path)
    if fmt == "EIII":
        return _run_captured(eiii_parser.parse_eiii, bank_path)
    return _run_captured(e4b_parser.parse_e4b, bank_path)


def apply_conversion(bank_path: str, opts: ConversionOptions,
                     risks_out: Optional[list] = None) -> str:
    """Runs mpc2emu's own parse -> Bank -> resample/reduce -> write round
    trip on an already-assembled E4B, KRZ or EIII file, producing a NEW
    temp file (the original is never touched). Returns the new file's
    path -- or bank_path itself, unchanged, if every option is off and the
    target format matches the source (a genuine no-op).

    `risks_out` collects polyphony_risk() dicts (see _apply_and_write).
    Deliberately left empty by the no-op path: nothing was converted, so
    there is no conversion to warn about."""
    fmt = _sniff_format(bank_path)
    if opts.is_noop(fmt):
        return bank_path
    # Around the parse as well as the write: KRZ_ROM_ONLY is emitted by
    # mpc2emu's PARSER, and the ROM-only refusal below is this project's own
    # re-derivation of the same fact from the parsed bank -- written because
    # their [INFO] saying so was discarded by _run_captured.
    with collect_diagnostics_into(risks_out):
        bank = _parse_by_format(bank_path, fmt)
    # Same ROM-only case as convert_preset(), reached instead when a whole
    # already-assembled bank is converted rather than one program. Checked
    # against mpc2emu's parse here because that is what this path already
    # has in hand, and it is the object _apply_and_write would work from.
    if not getattr(bank, "samples", None):
        raise ConvertOpError(
            f"{Path(bank_path).name} holds no sample audio -- its programs "
            f"reference only samples in the sampler's ROM, so there is "
            f"nothing to convert.")
    return _apply_and_write(bank, opts, Path(bank_path).stem, risks_out)


def _assembled_sample_count(data: bytes, suffix: str) -> int:
    """How many sample objects an assembled bank actually holds.

    Read back with VinSamLib's own byte-level readers rather than trusting
    the assembler, the same independence rule `_verify_written` follows. A
    reader that cannot make sense of the bytes returns -1 rather than 0, so
    an unparseable bank is never mistaken for a ROM-only one and blocked on
    that ground -- it goes on to the real conversion and fails there, with
    that error."""
    from ..banks import e4b as vs_e4b
    from ..banks import eiii as vs_eiii
    from ..banks import krz as vs_krz
    reader = {".krz": vs_krz, ".e3x": vs_eiii}.get(suffix.lower(), vs_e4b)
    try:
        return len(reader.parse_bytes(data, "assembled").samples)
    except Exception:
        return -1


def convert_preset(bank: Any, preset_obj: Any, opts: ConversionOptions,
                   risks_out: Optional[list] = None) -> str:
    """Applies mpc2emu resample/reduce/format-conversion to a SINGLE
    already-native preset/program, rather than a whole assembled bank --
    the same "convert via mpc2emu" pipeline offered for XPM import and
    for whole Pending-pane banks, reachable from Explorer's right-click
    "Import via mpc2emu..." on an individual preset or program.

    `bank`/`preset_obj` is a VinSamLib E4BFile/E4BPreset, KrzFile/
    KrzObject, or EIIIFile/EIIIPreset pair (from a "preset" TreeNode's
    payload) -- all three are real mpc2emu *input* formats now
    (parsers.krz_parser added 2026-07-27, parsers.eiii_parser added
    2026-07-28).

    Assembles a temporary single-preset/program file via VinSamLib's own
    byte-verbatim banks.e4b.assemble()/banks.krz.assemble()/
    banks.eiii.assemble() (the same call BankPane's "Save As" and
    banks/summary.py's preset preview already make), then reuses
    apply_conversion() unchanged on that real file. Returns the new
    file's path."""
    from ..banks import e4b as vs_e4b
    from ..banks import eiii as vs_eiii
    from ..banks import krz as vs_krz
    tmp_dir = tempdirs.session_temp_dir(_CONVERT_TEMP_PREFIX)
    stem = _sanitize_stem(getattr(preset_obj, "name", "") or "")
    if isinstance(bank, vs_e4b.E4BFile):
        data = vs_e4b.assemble([(bank, preset_obj)])
        tmp_path = tmp_dir / f"{stem}.e4b"
    elif isinstance(bank, vs_krz.KrzFile):
        data = vs_krz.assemble([(bank, preset_obj)])
        tmp_path = tmp_dir / f"{stem}.krz"
    elif isinstance(bank, vs_eiii.EIIIFile):
        data = vs_eiii.assemble([(bank, preset_obj)])
        tmp_path = tmp_dir / f"{stem}.e3x"
    else:
        raise ConvertOpError(f"not a recognized E4B, KRZ or EIII bank: {type(bank)!r}")

    # A program that references ONLY the machine's ROM assembles into a valid
    # bank with no sample objects in it, and there is no audio anywhere to
    # convert -- the samples live in the sampler, not the file. Left to run,
    # the whole chain completes silently: mpc2emu parses 0 presets and prints
    # an explanatory [INFO] that _run_captured discards because nothing
    # raised, _verify_written and _zone_loss_risk both early-return on the
    # empty case, and the caller reads back an empty list without an
    # exception -- so nothing appears in New Bank, no dialog opens, and the
    # status bar sits on "Converting ...". 433 KRZ banks here hold programs
    # and zero samples, so this is a whole class of material, not a corner:
    # they are shipping products that play on a K2000 and cannot become an
    # E4B. Browsing and inspecting them still works; only conversion cannot.
    n_samples = _assembled_sample_count(data, tmp_path.suffix)
    if not n_samples:
        raise ConvertOpError(
            f"{getattr(preset_obj, 'name', 'This program')!r} references only "
            f"samples held in the sampler's ROM -- the bank file contains no "
            f"audio for them, so there is nothing to convert. (Browsing and "
            f"inspecting the bank still works.)")

    tmp_path.write_bytes(data)
    out = apply_conversion(str(tmp_path), opts, risks_out)
    # The intermediate was only ever input to apply_conversion, so it can go
    # now -- unless the options were a genuine no-op, in which case
    # apply_conversion handed the very same path back and it IS the result.
    if Path(out) != tmp_path:
        tempdirs.forget(tmp_dir)
    return out
