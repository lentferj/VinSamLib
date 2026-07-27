"""
Wraps mpc2emu's own parse -> Bank -> process -> write round trip for the
vintage resample/reduce conversion options panel. Operates on an
already-assembled, on-disk .E4B file (the output of banks.e4b.assemble())
-- never touches assemble() itself, which must stay byte-verbatim (see its
own docstring: going through models.common.Bank would silently degrade
real commercial banks). This is therefore always a full pre/post-
processing pass on a whole bank file, producing a NEW temp file --
e4b_path itself is never mutated.

E4B-only *input*: mpc2emu has no .krz *input* parser (parsers/registry.py's
INPUT_EXTS has no '.krz' -- KRZ is write-only in this project's own
pipeline), so there is no parse -> Bank round trip possible for KRZ banks
at all. Callers must not offer this for KRZ queues/presets. The *output*
format is a free choice (target_format), same as build/xpm_import.py --
an E4B source can be converted straight to a KRZ result in one step, which
is exactly what convert_preset() below is for.

Applying this re-encodes a bank through mpc2emu's own Bank model; a few
advanced parameters not covered by that model may reset to defaults --
an unavoidable cost of using mpc2emu's DSP at all, but worth disclosing
to the user (see ui/convert_options_dialog.py).
"""

from __future__ import annotations

import contextlib
import io
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from ..mpc2emu_bridge import e4b_parser, e4b_writer, krz_writer, resampler, zone_reducer

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
    target_format: str = "E4B"                    # "E4B" | "KRZ" -- the OUTPUT format
    resample_profile: Optional[str] = None        # "emulator2" | "emax1" | None (off)
    no_bandpass: bool = False
    resample_keep_gain: bool = False
    max_sample_rate: Optional[int] = None          # Hz; None/0 means don't apply this step
    reduce_key_zones_pct: float = 0.0
    reduce_velocity_layers_pct: float = 0.0

    def is_noop(self) -> bool:
        return (self.target_format == "E4B"
                and self.resample_profile is None
                and not self.max_sample_rate
                and self.reduce_key_zones_pct <= 0
                and self.reduce_velocity_layers_pct <= 0)


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


def _apply_and_write(bank: Any, opts: ConversionOptions, out_stem: str) -> str:
    """Shared tail end of apply_conversion() and build/xpm_import.py's
    import_xpm(): both start from a different parse step (an already-
    native E4B vs. a foreign XPM) but from an already-parsed mpc2emu Bank
    onward the pipeline is identical -- reduce, then resample, then the
    independent max-sample-rate pass, then write to whichever target
    format was chosen. Order matches convert.py's own pipeline: thinning
    first means the slower vintage-resample step has fewer surviving
    samples to process."""
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
    else:
        out_path = tmp_dir / f"{out_stem}.e4b"
        _run_captured(e4b_writer.write_e4b, bank, str(out_path))
    return str(out_path)


def apply_conversion(e4b_path: str, opts: ConversionOptions) -> str:
    """Runs mpc2emu's own parse -> Bank -> resample/reduce -> write round
    trip on an already-assembled E4B file, producing a NEW temp file (the
    original is never touched). Returns the new file's path -- or
    e4b_path itself, unchanged, if every option is off and the target
    format is still E4B (a genuine no-op)."""
    if opts.is_noop():
        return e4b_path
    bank = _run_captured(e4b_parser.parse_e4b, e4b_path)
    return _apply_and_write(bank, opts, Path(e4b_path).stem)


def convert_preset(bank: Any, preset_obj: Any, opts: ConversionOptions) -> str:
    """Applies mpc2emu resample/reduce/format-conversion to a SINGLE
    already-native preset, rather than a whole assembled bank -- the same
    "convert via mpc2emu" pipeline offered for XPM import and for whole
    Pending-pane banks, now also reachable from Explorer's right-click
    "Import via mpc2emu..." on an individual preset.

    `bank`/`preset_obj` must be a VinSamLib E4BFile/E4BPreset pair (from a
    "preset" TreeNode's payload) -- E4B only, for the same reason
    apply_conversion() is: mpc2emu has no KRZ *input* parser at all, so a
    KRZ-sourced preset can never be round-tripped through it. Callers must
    not offer this action for KRZ presets (see explorer_pane.py's
    context-menu gating).

    Assembles a temporary single-preset E4B via VinSamLib's own
    byte-verbatim banks.e4b.assemble() (the same call BankPane's "Save
    As" and banks/summary.py's preset preview already make), then reuses
    apply_conversion() unchanged on that real file. Returns the new
    file's path."""
    from ..banks import e4b as vs_e4b
    if not isinstance(bank, vs_e4b.E4BFile):
        raise ConvertOpError(
            "mpc2emu can only read E4B input -- this preset isn't from an E4B bank")
    data = vs_e4b.assemble([(bank, preset_obj)])
    tmp_dir = Path(tempfile.mkdtemp(prefix=_CONVERT_TEMP_PREFIX))
    stem = _sanitize_stem(preset_obj.name or "")
    tmp_path = tmp_dir / f"{stem}.e4b"
    tmp_path.write_bytes(data)
    return apply_conversion(str(tmp_path), opts)
