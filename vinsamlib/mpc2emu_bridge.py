"""
Bridge into the mpc2emu checkout.

mpc2emu is not an installable package — it is a flat script-plus-packages
layout whose intra-project imports are absolute-from-root
(``from writers.fat16 import ...``). To import it unmodified, its *repo root*
(not its parent) must be on ``sys.path``. This module does that exactly once,
then re-exports the specific symbols the rest of VinSamLib needs, so no other
module has to know about the path trick.

VinSamLib deliberately never edits mpc2emu — see the project plan's
"mpc2emu dependency" decision. If mpc2emu's internal APIs move, this is the
one file that needs updating.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import Config

_installed = False
_config: Config | None = None


def install(config: Config | None = None) -> None:
    """Insert the configured mpc2emu checkout at sys.path[0]. Idempotent."""
    global _installed, _config
    if _installed:
        return
    _config = config or Config.load()
    _config.validate_mpc2emu_path()
    root = str(_config.mpc2emu_path.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    _installed = True


def _ensure_installed() -> None:
    if not _installed:
        install()


class _Lazy:
    """Defers the mpc2emu import until first attribute access, so importing
    vinsamlib.mpc2emu_bridge never requires mpc2emu to already be on disk —
    only *using* it does.

    WHAT THAT COSTS, and it is easy to forget when reading a green test run:
    **a clean import proves almost nothing about this surface.** Importing a
    module establishes that its module-level names resolve at import time. It
    says nothing about names used only inside a function, only on a branch —
    or, here, anything reached through one of these proxies, because none of
    them has touched mpc2emu yet.

    So `importlib.import_module("vinsamlib.build.convert")` succeeding does
    not mean `krz_parser.parse_krz` exists, or that the attribute a caller
    asks for is spelled correctly. The first evidence of either is the call
    itself. That is sharper for us than for a project importing eagerly, and
    it is why three missing-import bugs here survived import-time checks and
    one shipped — see tests/manual_unbound_names.py, which asks the question
    an import cannot."""

    def __init__(self, modname: str):
        self._modname = modname
        self._mod = None

    def __getattr__(self, name: str):
        if self._mod is None:
            _ensure_installed()
            import importlib

            self._mod = importlib.import_module(self._modname)
        return getattr(self._mod, name)


# ── mpc2emu modules used elsewhere in VinSamLib, imported lazily ────────────
models_common = _Lazy("models.common")
# Structured conversion warnings (mpc2emu 2026-09-05). Optional: an older
# checkout has no models/diagnostics.py, and build/convert.py degrades to
# the previous behaviour rather than refusing to convert -- see
# Config.check_diagnostics_support().
diagnostics = _Lazy("models.diagnostics")
e4b_parser = _Lazy("parsers.e4b_parser")
e4b_writer = _Lazy("writers.e4b_writer")
krz_parser = _Lazy("parsers.krz_parser")
eiii_parser = _Lazy("parsers.eiii_parser")
eiii_writer = _Lazy("writers.eiii_writer")
akai_writer = _Lazy("writers.akai_s3000_writer")
resampler = _Lazy("processors.resampler")
zone_reducer = _Lazy("processors.zone_reducer")
# Per-preset thinning aimed at a memory target (mpc2emu 5aa8cf7). It
# SEARCHES the key/velocity combinations rather than walking a fixed
# ordering, scoring both axes in one unit -- cents of spectral-centroid
# error -- which is what makes them comparable at all.
shrink_planner = _Lazy("processors.shrink_planner")
start_trim = _Lazy("processors.start_trim")
tail_trim = _Lazy("processors.tail_trim")
xpm_parser = _Lazy("parsers.xpm_parser")
sampledir_parser = _Lazy("parsers.sampledir_parser")
# The non-hardware source formats (SF2, SFZ, EXS24, TAL-Sampler, GIG) come in
# through mpc2emu's own input-format table rather than five separate parser
# imports: parsers/registry.py already normalises their differing signatures
# (parse_sf2 takes max_presets and no wav_dir, parse_exs24 takes a *list* of
# sample dirs, parse_gig takes max_instruments/max_samples) to one
# `callable(path, wav_dir, **kw) -> Bank` shape, and calls itself "the one
# source of truth" for it. Re-exporting the table keeps that true here too.
parser_registry = _Lazy("parsers.registry")
info_cmd = _Lazy("info_cmd")
iso_builder = _Lazy("writers.iso_builder")
hda_builder = _Lazy("writers.hda_builder")
krz_writer = _Lazy("writers.krz_writer")
fat12 = _Lazy("writers.fat12")
fat16 = _Lazy("writers.fat16")
fat32 = _Lazy("writers.fat32")
bank_splitter = _Lazy("writers.bank_splitter")
# AKAI S1000/S3000. Only needed to CONVERT: browsing an AKAI disk and
# assembling volumes from it are VinSamLib's own (banks/akai.py, vfs/akai.py),
# which is what keeps those working against an mpc2emu checkout that has no
# AKAI support at all -- as its main branch does not. Config.check_akai_*
# gate every use of these.
akai_parser = _Lazy("parsers.akai_s3000_parser")
akai_writer = _Lazy("writers.akai_s3000_writer")
akai_image = _Lazy("writers.akai_s3000_image")


def mpc2emu_root() -> Path:
    _ensure_installed()
    assert _config is not None
    return _config.mpc2emu_path.resolve()
