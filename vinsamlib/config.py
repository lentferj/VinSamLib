"""
VinSamLib runtime configuration.

Resolves where to find the mpc2emu checkout (the format engine this project
builds on) and which directories make up the sample library. Config lives in
a TOML file under a per-platform user-config directory so the same code works
unmodified on Linux, Windows and macOS.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_APP_NAME = "vinsamlib"


def user_config_dir() -> Path:
    """Per-platform config directory (no external dependency)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / _APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / _APP_NAME


def user_data_dir() -> Path:
    """Per-platform data directory (index database, logs)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / _APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / _APP_NAME


def _default_mpc2emu_path() -> Path:
    """mpc2emu is a sibling checkout by convention (../mpc2emu relative to
    this repo). Used only as a fallback default; always overridable."""
    return Path(__file__).resolve().parent.parent.parent / "mpc2emu"


@dataclass
class Config:
    mpc2emu_path: Path = field(default_factory=_default_mpc2emu_path)
    library_roots: list[Path] = field(default_factory=list)
    # Last directory browsed to in the Image column's file dialogs (New…'s
    # Browse…, Open…) — persisted across restarts so each one doesn't start
    # back at some default location every time.
    last_image_dir: Optional[Path] = None
    # Same idea for File > Add Library Folder… — the parent of the last
    # folder added, so picking a sibling library folder next time doesn't
    # start back at the dialog's platform default every time.
    last_library_dir: Optional[Path] = None
    # New Bank's size-meter warning threshold, in MB, per format. This is
    # a soft, user-adjustable "will this fit MY hardware's RAM" warning,
    # separate from the hard format-technical ceiling banks/e4b.py always
    # enforces at assemble() time (128 MB, the E4XT container's actual
    # write limit) -- these defaults instead reflect the most common real
    # RAM configurations in the wild (64 MB E4XT, 32 MB K2000), which are
    # usually well under the format's own absolute maximum.
    e4b_bank_limit_mb: int = 64
    krz_bank_limit_mb: int = 32

    CONFIG_FILE = "config.toml"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or (user_config_dir() / cls.CONFIG_FILE)
        if not path.exists():
            return cls()
        with open(path, "rb") as f:
            data = tomllib.load(f)
        mpc2emu_path = Path(data.get("mpc2emu_path", _default_mpc2emu_path()))
        roots = [Path(p) for p in data.get("library_roots", [])]
        last_image_dir_str = data.get("last_image_dir")
        last_image_dir = Path(last_image_dir_str) if last_image_dir_str else None
        last_library_dir_str = data.get("last_library_dir")
        last_library_dir = Path(last_library_dir_str) if last_library_dir_str else None
        defaults = cls()
        e4b_bank_limit_mb = data.get("e4b_bank_limit_mb", defaults.e4b_bank_limit_mb)
        krz_bank_limit_mb = data.get("krz_bank_limit_mb", defaults.krz_bank_limit_mb)
        return cls(mpc2emu_path=mpc2emu_path, library_roots=roots,
                    last_image_dir=last_image_dir, last_library_dir=last_library_dir,
                    e4b_bank_limit_mb=e4b_bank_limit_mb, krz_bank_limit_mb=krz_bank_limit_mb)

    def save(self, path: Path | None = None) -> None:
        path = path or (user_config_dir() / self.CONFIG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f'mpc2emu_path = "{self.mpc2emu_path.as_posix()}"']
        roots_str = ", ".join(f'"{p.as_posix()}"' for p in self.library_roots)
        lines.append(f"library_roots = [{roots_str}]")
        if self.last_image_dir is not None:
            lines.append(f'last_image_dir = "{self.last_image_dir.as_posix()}"')
        if self.last_library_dir is not None:
            lines.append(f'last_library_dir = "{self.last_library_dir.as_posix()}"')
        lines.append(f"e4b_bank_limit_mb = {self.e4b_bank_limit_mb}")
        lines.append(f"krz_bank_limit_mb = {self.krz_bank_limit_mb}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def validate_mpc2emu_path(self) -> None:
        marker = self.mpc2emu_path / "writers" / "iso_builder.py"
        if not marker.exists():
            raise FileNotFoundError(
                f"mpc2emu checkout not found at {self.mpc2emu_path} "
                f"(expected {marker} to exist). Set mpc2emu_path in "
                f"{user_config_dir() / self.CONFIG_FILE}."
            )

    def check_mpc2emu_path(self) -> tuple[bool, str]:
        """Non-raising counterpart to validate_mpc2emu_path(), for a
        Settings dialog that wants a live "found"/"not found: <reason>"
        status without wrapping every keystroke in try/except."""
        try:
            self.validate_mpc2emu_path()
        except FileNotFoundError as ex:
            return False, str(ex)
        return True, f"Found mpc2emu checkout at {self.mpc2emu_path}"

    def check_conversion_support(self) -> tuple[bool, str]:
        """Stricter check for the vintage resample/reduce feature: proves
        the specific modules it needs are present, not just that *some*
        mpc2emu checkout exists (an old/partial checkout during
        development could plausibly have iso_builder.py but be missing
        one of these)."""
        ok, reason = self.check_mpc2emu_path()
        if not ok:
            return False, reason
        required = [
            Path("processors") / "resampler.py",
            Path("processors") / "zone_reducer.py",
            Path("parsers") / "e4b_parser.py",
            Path("writers") / "e4b_writer.py",
        ]
        missing = [str(rel) for rel in required if not (self.mpc2emu_path / rel).exists()]
        if missing:
            return False, f"mpc2emu checkout is missing: {', '.join(missing)}"
        return True, "Vintage resample/reduce is available"

    def check_xpm_import_support(self) -> tuple[bool, str]:
        """XPM import (build/xpm_import.py) needs mpc2emu's own Akai XPM
        program parser specifically -- a checkout could satisfy
        check_conversion_support() and still be missing this (or vice
        versa), so it gets its own check rather than being folded in."""
        ok, reason = self.check_mpc2emu_path()
        if not ok:
            return False, reason
        marker = self.mpc2emu_path / "parsers" / "xpm_parser.py"
        if not marker.exists():
            return False, f"mpc2emu checkout is missing {marker.relative_to(self.mpc2emu_path)}"
        return True, "XPM import is available"

    def check_sample_dir_import_support(self) -> tuple[bool, str]:
        """Sample-folder import (build/sampledir_import.py) needs mpc2emu's
        own WAV-folder-to-preset parser specifically -- a checkout could
        satisfy check_conversion_support() and still be missing this (or
        vice versa), so it gets its own check rather than being folded in."""
        ok, reason = self.check_mpc2emu_path()
        if not ok:
            return False, reason
        marker = self.mpc2emu_path / "parsers" / "sampledir_parser.py"
        if not marker.exists():
            return False, f"mpc2emu checkout is missing {marker.relative_to(self.mpc2emu_path)}"
        return True, "Sample folder import is available"
