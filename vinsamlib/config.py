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
    # And for the two import dialogs, which start somewhere else entirely:
    # sample folders live with your samples, MPC programs with your MPC
    # backup, and neither is where you last added a library folder.
    last_sample_dir: Optional[Path] = None
    last_program_dir: Optional[Path] = None
    # New Bank's size-meter warning threshold, in MB, per format. This is
    # a soft, user-adjustable "will this fit MY hardware's RAM" warning,
    # separate from the hard format-technical ceiling banks/e4b.py always
    # enforces at assemble() time (128 MB, the E4XT container's actual
    # write limit) -- these defaults instead reflect the most common real
    # RAM configurations in the wild (64 MB E4XT, 32 MB K2000), which are
    # usually well under the format's own absolute maximum.
    e4b_bank_limit_mb: int = 64
    krz_bank_limit_mb: int = 32
    # The K2000's PRAM budget in KB — a SECOND, independent limit that the MB
    # figures above cannot express. Objects (programs, keymaps, sample headers)
    # live in PRAM, the audio in sample RAM, so a small bank can still be
    # unloadable: one keymap costs 688 bytes whatever its samples weigh.
    # 110 rather than the ~116 a stock machine has usable, matching mpc2emu's
    # `--pram` default, because PRAM also holds setups, effects and whatever is
    # already loaded. Raise it if your K2000 has the expansion (760 is common).
    krz_pram_kb: int = 110
    #: Resident objects (programs + keygroups + samples) an AKAI volume may
    #: use. A DEFAULT measured on one 32 MB S3000XL, not a format constant --
    #: see build/akai_image.RESIDENT_OBJECTS_DEFAULT for why it is neither
    #: fixed nor a hard limit.
    akai_max_objects: int = 1006
    #: Seconds between crash-safety autosaves of the staged work. 0 disables
    #: it. Sixty is a compromise: an autosave that carries converted banks
    #: writes real megabytes, and doing that every few seconds would be felt.
    autosave_seconds: int = 60
    # Main-window size, remembered on close. None until the first quit, so a
    # fresh install still gets the built-in default rather than a 0x0 window.
    # Size only, deliberately not position: a window restored onto a monitor
    # that is no longer attached is unreachable, and the fix for that is
    # fiddlier than the feature is worth.
    window_width: Optional[int] = None
    window_height: Optional[int] = None

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
        last_sample_dir_str = data.get("last_sample_dir")
        last_sample_dir = Path(last_sample_dir_str) if last_sample_dir_str else None
        last_program_dir_str = data.get("last_program_dir")
        last_program_dir = Path(last_program_dir_str) if last_program_dir_str else None
        defaults = cls()
        e4b_bank_limit_mb = data.get("e4b_bank_limit_mb", defaults.e4b_bank_limit_mb)
        krz_bank_limit_mb = data.get("krz_bank_limit_mb", defaults.krz_bank_limit_mb)
        krz_pram_kb = data.get("krz_pram_kb", defaults.krz_pram_kb)
        akai_max_objects = data.get("akai_max_objects", defaults.akai_max_objects)
        autosave_seconds = data.get("autosave_seconds", defaults.autosave_seconds)
        return cls(mpc2emu_path=mpc2emu_path, library_roots=roots,
                    last_image_dir=last_image_dir, last_library_dir=last_library_dir,
                    last_sample_dir=last_sample_dir, last_program_dir=last_program_dir,
                    e4b_bank_limit_mb=e4b_bank_limit_mb, krz_bank_limit_mb=krz_bank_limit_mb,
                    krz_pram_kb=krz_pram_kb,
                    akai_max_objects=akai_max_objects,
                    autosave_seconds=autosave_seconds,
                    window_width=data.get("window_width"),
                    window_height=data.get("window_height"))

    def save(self, path: Path | None = None, allow_empty_library: bool = False) -> None:
        """Writes the config file. Refuses to blank a non-empty library.

        `library_roots` is the only setting here that is real, irreplaceable
        user work -- a list of folders someone assembled by hand -- and every
        other setting rides in the same file, so any save is a chance to lose
        it. That is not hypothetical: a script that had set `library_roots =
        []` to keep itself from scanning wrote the file for an unrelated
        reason (remembering a dialog's directory) and took the library with
        it, twice in this project's history. The second time, the write was
        three call levels away from anything about libraries.

        So an empty list only reaches the file when the caller says it means
        it -- File > Remove Library Folder… removing the last one, which is
        the one place where empty is a decision rather than an accident."""
        path = path or (user_config_dir() / self.CONFIG_FILE)
        roots = self.library_roots
        if not roots and not allow_empty_library:
            # Kept out of the FILE without putting them back in memory: a
            # caller that emptied the list did so for its own reasons (a test
            # keeping itself from scanning), and handing them back would
            # start a scan it deliberately avoided.
            roots = Config.load(path).library_roots
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f'mpc2emu_path = "{self.mpc2emu_path.as_posix()}"']
        roots_str = ", ".join(f'"{p.as_posix()}"' for p in roots)
        lines.append(f"library_roots = [{roots_str}]")
        if self.last_image_dir is not None:
            lines.append(f'last_image_dir = "{self.last_image_dir.as_posix()}"')
        if self.last_library_dir is not None:
            lines.append(f'last_library_dir = "{self.last_library_dir.as_posix()}"')
        if self.last_sample_dir is not None:
            lines.append(f'last_sample_dir = "{self.last_sample_dir.as_posix()}"')
        if self.last_program_dir is not None:
            lines.append(f'last_program_dir = "{self.last_program_dir.as_posix()}"')
        lines.append(f"e4b_bank_limit_mb = {self.e4b_bank_limit_mb}")
        lines.append(f"krz_bank_limit_mb = {self.krz_bank_limit_mb}")
        lines.append(f"krz_pram_kb = {self.krz_pram_kb}")
        lines.append(f"akai_max_objects = {self.akai_max_objects}")
        lines.append(f"autosave_seconds = {self.autosave_seconds}")
        if self.window_width and self.window_height:
            lines.append(f"window_width = {int(self.window_width)}")
            lines.append(f"window_height = {int(self.window_height)}")
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

    def check_trim_support(self) -> tuple[bool, str]:
        """Start/tail trim needs mpc2emu's two trim processors. Kept out of
        check_conversion_support()'s `required` list on purpose: an older
        checkout missing these should lose only the Trim Silence group, not
        the whole resample/reduce feature -- same reasoning as
        check_xpm_import_support() below."""
        ok, reason = self.check_mpc2emu_path()
        if not ok:
            return False, reason
        required = [
            Path("processors") / "start_trim.py",
            Path("processors") / "tail_trim.py",
        ]
        missing = [str(rel) for rel in required if not (self.mpc2emu_path / rel).exists()]
        if missing:
            return False, f"mpc2emu checkout is missing: {', '.join(missing)}"
        return True, "Trim silence is available"

    def check_diagnostics_support(self) -> tuple[bool, str]:
        """Structured conversion warnings need mpc2emu's models/diagnostics.py.

        Optional by design, and the ONE capability whose absence must not be
        reported to the user as a missing feature: without it a conversion
        still runs and still produces the same file, it just cannot say what
        it changed on the way. Everything else here gates a feature the user
        asked for; this gates whether the program can explain itself.
        """
        ok, reason = self.check_mpc2emu_path()
        if not ok:
            return False, reason
        marker = self.mpc2emu_path / "models" / "diagnostics.py"
        if not marker.exists():
            return False, ("this mpc2emu checkout predates structured "
                           "diagnostics, so conversion warnings stay on its "
                           "stdout and cannot be shown here")
        return True, "Conversion warnings are available"

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

    def check_akai_read_support(self) -> tuple[bool, str]:
        """AKAI as a conversion SOURCE: reading an AKAI program into
        mpc2emu's Bank model so it can be written as E4B/KRZ/EIII.

        Browsing an AKAI disk needs none of this -- banks/akai.py and
        vfs/akai.py are VinSamLib's own -- and that is deliberate, because
        mpc2emu's AKAI support lives on an unmerged branch. A checkout of its
        main browses AKAI media perfectly and simply cannot convert it, which
        is a far better failure than an Explorer whose AKAI rows come and go
        with the configured checkout's branch."""
        ok, reason = self.check_mpc2emu_path()
        if not ok:
            return False, reason
        marker = self.mpc2emu_path / "parsers" / "akai_s3000_parser.py"
        if not marker.exists():
            return False, (
                "this mpc2emu checkout has no AKAI support "
                f"({marker.relative_to(self.mpc2emu_path)} is missing). "
                "Browsing AKAI discs works regardless; converting them needs "
                "a checkout that has it.")
        return True, "Converting AKAI programs is available"

    def check_akai_write_support(self) -> tuple[bool, str]:
        """AKAI as a conversion TARGET, and building AKAI media.

        Separate from reading on purpose. Reading an AKAI disc you own can
        only ever produce an E4B/KRZ/EIII bank, so a mistake there costs a
        conversion. Writing produces media a sampler is asked to mount, and
        the AKAI format was never published by Akai -- every byte of it is
        reconstructed. That asymmetry is why the two are gated apart."""
        ok, reason = self.check_akai_read_support()
        if not ok:
            return False, reason
        required = [
            Path("writers") / "akai_s3000_writer.py",
            Path("writers") / "akai_s3000_image.py",
        ]
        missing = [str(rel) for rel in required if not (self.mpc2emu_path / rel).exists()]
        if missing:
            return False, f"mpc2emu checkout is missing: {', '.join(missing)}"
        return True, "Writing AKAI programs and media is available"

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

    def check_foreign_import_support(self) -> tuple[bool, str]:
        """The non-hardware source formats (build/foreign_import.py) need
        mpc2emu's input-format registry and all five of its soft-sampler
        parsers -- own check, same reasoning as check_xpm_import_support().

        All-or-nothing rather than one check per format, because that is what
        the code actually does: parsers/registry.py imports every parser at
        module import, so a checkout missing any one of them cannot supply any
        of the others either. Nothing is gained by pretending otherwise, and a
        partial answer would put rows in the Explorer that cannot be
        imported."""
        ok, reason = self.check_mpc2emu_path()
        if not ok:
            return False, reason
        required = [Path("parsers") / name for name in (
            "registry.py", "sf2_parser.py", "sfz_parser.py",
            "exs24_parser.py", "talsmpl_parser.py", "gig_parser.py")]
        missing = [str(rel) for rel in required if not (self.mpc2emu_path / rel).exists()]
        if missing:
            return False, f"mpc2emu checkout is missing: {', '.join(missing)}"
        return True, "Soundfont/SFZ/EXS/TAL/GIG import is available"
