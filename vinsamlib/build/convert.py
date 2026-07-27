"""
Wraps mpc2emu's own parse -> Bank -> process -> write round trip for the
vintage resample/reduce conversion options panel. Operates on an
already-assembled, on-disk bank file (the output of banks.e4b.assemble()/
banks.krz.assemble()) -- never touches assemble() itself, which must stay
byte-verbatim (see its own docstring: going through models.common.Bank
would silently degrade real commercial banks). This is therefore always
a full pre/post-processing pass on a whole bank file, producing a NEW
temp file -- the input file itself is never mutated.

Both E4B and KRZ are readable *inputs* now: mpc2emu's parsers.krz_parser
(added 2026-07-27, corpus-verified against 593 real .KRZ files) made KRZ
a real source format, alongside the E4B parser this module already used.
The *output* format is a free, independent choice (target_format) either
way -- same E4B source can go to E4B or KRZ, same KRZ source can go to
E4B or KRZ, all through the identical reduce/resample/write pipeline.
_sniff_format() below reads the real on-disk magic bytes to pick the
right parser rather than trusting a file extension.

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

from ..mpc2emu_bridge import e4b_parser, e4b_writer, krz_parser, krz_writer, resampler, zone_reducer

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


def _sniff_format(bank_path: str) -> str:
    """Real on-disk magic bytes, never the file extension (same convention
    as vfs/detect.py's own sniff())."""
    with open(bank_path, "rb") as f:
        head = f.read(16)
    if head[:4] == b"FORM" and head[8:12] == b"E4B0":
        return "E4B"
    if head[:4] == b"PRAM":
        return "KRZ"
    raise ConvertOpError(f"not a recognized E4B or KRZ bank: {bank_path}")


def _parse_by_format(bank_path: str, fmt: str) -> Any:
    if fmt == "KRZ":
        return _run_captured(krz_parser.parse_krz, bank_path)
    return _run_captured(e4b_parser.parse_e4b, bank_path)


def apply_conversion(bank_path: str, opts: ConversionOptions) -> str:
    """Runs mpc2emu's own parse -> Bank -> resample/reduce -> write round
    trip on an already-assembled E4B or KRZ file, producing a NEW temp
    file (the original is never touched). Returns the new file's path --
    or bank_path itself, unchanged, if every option is off and the target
    format matches the source (a genuine no-op)."""
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

    `bank`/`preset_obj` is a VinSamLib E4BFile/E4BPreset OR KrzFile/
    KrzObject pair (from a "preset" TreeNode's payload) -- both are real
    mpc2emu *input* formats now (parsers.krz_parser, added 2026-07-27).

    Assembles a temporary single-preset/program file via VinSamLib's own
    byte-verbatim banks.e4b.assemble()/banks.krz.assemble() (the same
    call BankPane's "Save As" and banks/summary.py's preset preview
    already make), then reuses apply_conversion() unchanged on that real
    file. Returns the new file's path."""
    from ..banks import e4b as vs_e4b
    from ..banks import krz as vs_krz
    tmp_dir = Path(tempfile.mkdtemp(prefix=_CONVERT_TEMP_PREFIX))
    stem = _sanitize_stem(getattr(preset_obj, "name", "") or "")
    if isinstance(bank, vs_e4b.E4BFile):
        data = vs_e4b.assemble([(bank, preset_obj)])
        tmp_path = tmp_dir / f"{stem}.e4b"
    elif isinstance(bank, vs_krz.KrzFile):
        data = vs_krz.assemble([(bank, preset_obj)])
        tmp_path = tmp_dir / f"{stem}.krz"
    else:
        raise ConvertOpError(f"not a recognized E4B or KRZ bank: {type(bank)!r}")
    tmp_path.write_bytes(data)
    return apply_conversion(str(tmp_path), opts)
