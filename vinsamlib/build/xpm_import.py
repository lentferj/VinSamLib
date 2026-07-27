"""
Imports an Akai MPC XPM program (Instruments > Keygroup > Layer, XML,
referencing external WAV/AIFF sample files -- MPC 2.x/X/Live/One; see
mpc2emu's parsers/xpm_parser.py) into a real, native E4B or KRZ bank file,
via mpc2emu's own parse_xpm -> Bank -> [resample/reduce] -> write_e4b /
write_krz pipeline. This is the only way an XPM ever becomes usable here:
VinSamLib has no XPM reader of its own and never will (all format-writing
code in this project comes from mpc2emu; VinSamLib deliberately never
edits mpc2emu, only wraps it).

Once written, the resulting file is a completely ordinary E4B/KRZ bank --
it gets no special treatment anywhere else in VinSamLib (Explorer, New
Bank, banks.e4b/krz.assemble()) beyond being reachable like any other bank
once its destination folder is a library root.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..banks.summary import ZoneSummary
from ..mpc2emu_bridge import e4b_writer, krz_writer, resampler, xpm_parser, zone_reducer

_XPM_IMPORT_TEMP_PREFIX = "vinsamlib_xpm_import_"
# mpc2emu's models.common.LoopType values (0-3) match banks/summary.py's own
# _LOOP_NAMES keys exactly (both ultimately describe the same E4B/KRZ loop-
# flag convention) -- kept as its own tiny copy here rather than reaching
# into that module's private name, since build/ and banks/ stay separate
# layers otherwise.
_LOOP_NAMES = {0: "none", 1: "forward", 2: "alternating", 3: "forward (release)"}


class XpmImportError(RuntimeError):
    """Raised for any failed XPM import; message is safe to show the user."""


@dataclass(frozen=True)
class XpmImportOptions:
    target_format: str = "E4B"                    # "E4B" | "KRZ"
    resample_profile: Optional[str] = None         # "emulator2" | "emax1" | None (off)
    no_bandpass: bool = False
    resample_keep_gain: bool = False
    max_sample_rate: Optional[int] = None          # Hz; None/0 means don't apply this step
    reduce_key_zones_pct: float = 0.0
    reduce_velocity_layers_pct: float = 0.0


@dataclass(frozen=True)
class XpmSummary:
    preset_name: str
    sample_count: int
    total_sample_bytes: int
    zones: list = field(default_factory=list)   # list[ZoneSummary]


def _run_captured(fn: Callable, *args, **kwargs) -> Any:
    # Same shape as build/convert.py's own _run_captured: mpc2emu prints
    # progress to stdout, which would otherwise leak into VinSamLib's own
    # console; captured text rides along on any raised XpmImportError.
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            return fn(*args, **kwargs)
    except Exception as ex:
        raise XpmImportError(f"{ex}\n\n{buf.getvalue()}".strip()) from ex


def _apply_max_sample_rate(bank: Any, hz: int) -> None:
    # Same simple flat downsample as build/convert.py's own helper (not
    # convert.py's fancier KRZ "headroom-aware" auto-downsample -- that's
    # inline main()-only logic upstream and a bigger lift to reproduce
    # faithfully; deferred, see docs/mpc2emu_conversion_integration_plan.md).
    # XpmImportDialog nudges this to a sane 24000 Hz default for a KRZ
    # target instead, which covers the common case without it.
    for i, sample in enumerate(bank.samples):
        bank.samples[i] = resampler.resample_to_rate(sample, hz, verbose=False)


def summarize_xpm(xpm_path: str, wav_dir: Optional[str] = None) -> XpmSummary:
    """Read-only preview for Explorer's Detail pane -- parses via mpc2emu's
    own xpm_parser (same as import_xpm(), just never writes anything) to
    report the one preset's zones (same ZoneSummary shape banks/summary.py
    uses for E4B/KRZ presets, so the Detail pane can show the same
    sample/key/vel/root/loop table either way) and total referenced sample
    data size, without doing a full import. Same wav_dir default as
    import_xpm()."""
    if wav_dir is None:
        wav_dir = str(Path(xpm_path).resolve().parent)
    bank = _run_captured(xpm_parser.parse_xpm, xpm_path, wav_dir)
    preset = bank.presets[0]
    zones: list[ZoneSummary] = []
    for voice in preset.voices:
        for z in voice.zones:
            sample = bank.find_sample(z.sample_name)
            zones.append(ZoneSummary(
                sample_name=z.sample_name,
                lo_key=z.lo_key, hi_key=z.hi_key,
                lo_vel=z.lo_vel, hi_vel=z.hi_vel,
                root_key=z.root_key,
                loop=_LOOP_NAMES.get(int(sample.loop_type), "?") if sample else "?",
                sample_rate=sample.sample_rate if sample else None,
            ))
    total_bytes = sum(len(sample.data) for sample in bank.samples)
    return XpmSummary(
        preset_name=preset.name.strip(),
        sample_count=len(bank.samples),
        total_sample_bytes=total_bytes,
        zones=zones,
    )


def import_xpm(xpm_path: str, opts: XpmImportOptions, wav_dir: Optional[str] = None) -> str:
    """Parses an XPM program (via mpc2emu's own parse_xpm) and writes it
    out as a real E4B or KRZ bank file in a fresh temp dir, applying
    whatever resample/reduce options were chosen along the way -- same
    shape as build/convert.py's apply_conversion(), just starting from a
    foreign input format instead of an already-native E4B. Returns the
    new file's path; never touches xpm_path itself.

    wav_dir: directory to search for the XPM's referenced WAV/AIFF sample
    files if they aren't sitting right next to the .xpm (defaults to the
    XPM's own directory, mpc2emu's own convention, when None)."""
    if wav_dir is None:
        wav_dir = str(Path(xpm_path).resolve().parent)

    bank = _run_captured(xpm_parser.parse_xpm, xpm_path, wav_dir)

    if opts.reduce_key_zones_pct > 0 or opts.reduce_velocity_layers_pct > 0:
        _run_captured(zone_reducer.reduce_bank, bank,
                      opts.reduce_key_zones_pct, opts.reduce_velocity_layers_pct)

    if opts.resample_profile:
        _run_captured(resampler.resample_bank, bank, opts.resample_profile,
                      bandpass=not opts.no_bandpass,
                      restore_level=not opts.resample_keep_gain)

    if opts.max_sample_rate:
        _run_captured(_apply_max_sample_rate, bank, opts.max_sample_rate)

    tmp_dir = Path(tempfile.mkdtemp(prefix=_XPM_IMPORT_TEMP_PREFIX))
    name = Path(xpm_path).stem
    if opts.target_format == "KRZ":
        out_path = tmp_dir / f"{name}.krz"
        _run_captured(krz_writer.write_krz, bank, str(out_path))
    else:
        out_path = tmp_dir / f"{name}.e4b"
        _run_captured(e4b_writer.write_e4b, bank, str(out_path))
    return str(out_path)
