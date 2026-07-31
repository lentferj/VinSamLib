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
corpus-verified against 1118 real EIII/EIIIX/ESI banks) does the same for
EIII. The *output* format is a free, independent choice (target_format)
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

from ..mpc2emu_bridge import (e4b_parser, e4b_writer, eiii_parser, eiii_writer,
                                krz_parser, krz_writer, models_common, resampler,
                                start_trim, tail_trim, zone_reducer)

_CONVERT_TEMP_PREFIX = "vinsamlib_convert_"


def _sanitize_stem(name: str) -> str:
    """A real preset name is free-form (e.g. "CL EspHdFst/Sld" -- "/" used
    literally as part of the name, not a separator) but has to survive as
    a single path component once used as a temp filename stem: `tmp_dir /
    f"{stem}.e4b"` silently turns an embedded "/" into an extra directory
    level that was never created, so writing to it raises FileNotFoundError.
    Same character set ui/bank_pane.py's _sanitize_bank_name() strips for
    the same reason (a real bank/preset name becoming a real filename)."""
    name = name.strip()
    return re.sub(r'[\\/:*?"<>|]', "_", name) or "preset"


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

    def is_noop(self, source_format: str = "E4B") -> bool:
        """`source_format` matters now that KRZ can be a source too: a
        KRZ->KRZ request with no other options set is just as much a
        genuine no-op as an E4B->E4B one -- skipping the round trip in
        that case avoids needlessly losing fidelity on any advanced
        parameter mpc2emu's Bank model doesn't carry, for zero benefit.
        Defaults to "E4B" for existing callers that only ever process
        E4B sources (the Pending-pane per-bank feature, the HW test
        matrix) and don't pass this explicitly."""
        return (self.target_format == source_format
                and self.resample_profile is None
                and not self.max_sample_rate
                and self.reduce_key_zones_pct <= 0
                and self.reduce_velocity_layers_pct <= 0
                and self.mono is None
                and self.pan_law == "hardware"
                and self.trim_start_db is None
                and self.trim_tail_db is None)


def _run_captured(fn: Callable, *args, **kwargs) -> Any:
    # Same shape as build/images.py's own _run_captured: mpc2emu's
    # processors print progress to stdout, which would otherwise leak
    # into VinSamLib's own console; captured text rides along on any
    # raised ConvertOpError so a failure is still diagnosable.
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            return fn(*args, **kwargs)
    except Exception as ex:
        raise ConvertOpError(f"{ex}\n\n{buf.getvalue()}".strip()) from ex


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
    tmp_dir = Path(tempfile.mkdtemp(prefix=_CONVERT_TEMP_PREFIX))
    tmp_path = tmp_dir / f"preview.{_EXT[fmt]}"
    tmp_path.write_bytes(data)
    return load_samples_for_test(str(tmp_path))


def _apply_and_write(bank: Any, opts: ConversionOptions, out_stem: str) -> str:
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
    data, so the two don't interact."""
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

    # E4B only -- the law was measured on an E4XT and says nothing about
    # what a K2000 or an EIII does with pan, so applying it to those
    # targets would be guesswork baked irreversibly into the volume byte.
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

    tmp_dir = Path(tempfile.mkdtemp(prefix=_CONVERT_TEMP_PREFIX))
    if opts.target_format == "KRZ":
        out_path = tmp_dir / f"{out_stem}.krz"
        _run_captured(krz_writer.write_krz, bank, str(out_path))
    elif opts.target_format == "EIII":
        out_path = tmp_dir / f"{out_stem}.e3x"
        _run_captured(eiii_writer.write_eiii, bank, str(out_path))
    else:
        out_path = tmp_dir / f"{out_stem}.e4b"
        _run_captured(e4b_writer.write_e4b, bank, str(out_path))
    return str(out_path)


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


def apply_conversion(bank_path: str, opts: ConversionOptions) -> str:
    """Runs mpc2emu's own parse -> Bank -> resample/reduce -> write round
    trip on an already-assembled E4B, KRZ or EIII file, producing a NEW
    temp file (the original is never touched). Returns the new file's
    path -- or bank_path itself, unchanged, if every option is off and the
    target format matches the source (a genuine no-op)."""
    fmt = _sniff_format(bank_path)
    if opts.is_noop(fmt):
        return bank_path
    bank = _parse_by_format(bank_path, fmt)
    return _apply_and_write(bank, opts, Path(bank_path).stem)


def convert_preset(bank: Any, preset_obj: Any, opts: ConversionOptions) -> str:
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
    tmp_dir = Path(tempfile.mkdtemp(prefix=_CONVERT_TEMP_PREFIX))
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
    tmp_path.write_bytes(data)
    return apply_conversion(str(tmp_path), opts)
