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
    only *using* it does."""

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
e4b_parser = _Lazy("parsers.e4b_parser")
e4b_writer = _Lazy("writers.e4b_writer")
krz_parser = _Lazy("parsers.krz_parser")
eiii_parser = _Lazy("parsers.eiii_parser")
eiii_writer = _Lazy("writers.eiii_writer")
resampler = _Lazy("processors.resampler")
zone_reducer = _Lazy("processors.zone_reducer")
xpm_parser = _Lazy("parsers.xpm_parser")
info_cmd = _Lazy("info_cmd")
iso_builder = _Lazy("writers.iso_builder")
hda_builder = _Lazy("writers.hda_builder")
krz_writer = _Lazy("writers.krz_writer")
fat12 = _Lazy("writers.fat12")
fat16 = _Lazy("writers.fat16")
fat32 = _Lazy("writers.fat32")
bank_splitter = _Lazy("writers.bank_splitter")


def mpc2emu_root() -> Path:
    _ensure_installed()
    assert _config is not None
    return _config.mpc2emu_path.resolve()
