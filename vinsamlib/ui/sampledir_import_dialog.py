"""
Sample-folder import dialog: FormatConvertDialog's target-format picker plus
resample/reduce sections, with two additions specific to a bare WAV
folder -- an explicit "middle C" octave-convention picker, and an
"Adjust Sample Placement..." button for manually overriding the
auto-computed key range/root of individual samples (see
sample_placement_dialog.py). An XPM's own zones already carry real MIDI
key numbers; a folder of loose WAVs doesn't, so mpc2emu's
parse_sample_dir() has to guess where "middle C" falls from the
filenames themselves (majority vote, its own CLI default) unless told
directly, and its key-range split (midpoints between adjacent roots) is
usually right but not always what the user actually wants.
"""

from __future__ import annotations  # noqa: I001

from pathlib import Path
from typing import Any, Callable, Optional  # noqa: UP035

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from . import workers
from .format_convert_dialog import FormatConvertDialog
from .sample_names_widget import SampleNamesWidget
from .sample_placement_dialog import SamplePlacementDialog, vel_window
from ..build import sampledir_import
from ..build.convert import ConversionOptions
from ..build.sample_names import names_from_base

# Fallback note-name display convention (C4=60) when the user left "Middle
# C is:" on Auto-detect -- parse_sample_dir() resolves its own real octave
# offset internally in that case but doesn't return it, so there is no
# single concrete offset to display note names in; this only affects how
# Sample Placement's fields are LABELED, never the actual key numbers.
_DISPLAY_OCTAVE_OFFSET_FALLBACK = 1

# The offset used to build the KEY SUFFIX of a generated sample name when the
# picker is on Auto-detect. Must stay equal to main_window._start_sample_import's
# own fallback, which is what the import actually applies -- these two disagreeing
# is how a previewed name and an imported name end up an octave apart.
# NOT the same value as the display fallback above, and deliberately separate:
# one labels spin boxes, this one goes into a name written to hardware.
_NAME_OCTAVE_FALLBACK = 2

# QComboBox row index -> parsers.sampledir_parser.parse_sample_dir()'s own
# octave_offset convention (2=C3, 1=C4, 0=C5; None lets it auto-detect).
_OCTAVE_CHOICES: list[tuple[str, Optional[int]]] = [  # noqa: UP045
    ("Auto-detect", None),
    ("C3 (K2000/vintage convention)", 2),
    ("C4 (general MIDI convention)", 1),
    ("C5", 0),
]

_DEFAULT_WARNING = (
    "Importing goes through mpc2emu's own model, same as any other "
    "conversion here. Each WAV is auto-mapped to the keys nearest its "
    "filename's root note, key-tracked, into one multisampled preset. "
    "Resample/reduce below are optional and off by default for either "
    "target format."
)


def _preview_note(bank) -> str:
    """What the placement editor is about to edit, in one line."""
    try:
        zones = bank.presets[0].voices[0].zones
    except (AttributeError, IndexError):
        return "Auto-computed placement (default)"
    return f"{len(zones)} sample(s) placed — Adjust Sample Placement… to change them"


#: How many parse previews to keep. ONE would do for the shape of the gesture
#: this exists for -- open the dialog, open the placement editor, maybe change
#: the octave -- but the octave picker's four choices are all cheap to hold and
#: a second pick costs nothing then. Each entry is a whole parsed mpc2emu Bank
#: holding every sample's key range, so it is not unbounded.
_MAX_PREVIEWS = 8


def _preview_key(dir_path: str, octave_offset: Optional[int]) -> tuple:  # noqa: UP045
    """(path, mtime_ns, size, octave) -- the identity of ONE parse.

    mtime and size, not just the path, because the whole point of a cache on a
    folder is that an edited folder must re-parse rather than offer what it
    used to be. `st_mtime_ns` on a directory changes when its entries do.
    """
    try:
        st = Path(dir_path).stat()
    except OSError:
        return (dir_path, None, None, octave_offset)
    return (dir_path, st.st_mtime_ns, st.st_size, octave_offset)


def _parse_preview(dir_path: str, octave_offset: Optional[int]) -> Any:  # noqa: UP045
    """`parse_sample_dir`, on a pool thread. See SampleDirImportDialog."""
    return sampledir_import.parse_preview(dir_path, octave_offset)


class SampleDirImportDialog(FormatConvertDialog):
    def __init__(
        self,
        parent=None,
        initial: Optional[ConversionOptions] = None,  # noqa: UP045
        title: str = "Import Sample Folder",
        warning_text: Optional[str] = None,  # noqa: UP045
        locked_format: Optional[str] = None,  # noqa: UP045
        sample_loader: Optional[Callable[[Optional[int]], list]] = None,  # noqa: UP045
        placement_loader: Optional[Callable[[Optional[int]], Any]] = None,  # noqa: UP045
        source_text: str = "",
        preview_path: Optional[str] = None,  # noqa: UP045
    ):
        super().__init__(
            parent,
            initial=initial,
            title=title,
            warning_text=warning_text or _DEFAULT_WARNING,
            locked_format=locked_format,
            source_text=source_text,
        )

        self._placement_loader = placement_loader
        #: The folder itself, which the callers above deliberately do not hand
        #: over: `placement_loader` is a closure and `source_text` is prose the
        #: user reads. Without the path the dialog cannot parse anything until
        #: asked, which is exactly the freeze this is here to remove -- see
        #: _prefill_preview.
        self._preview_path = preview_path
        self._preview_cache: dict[tuple, Any] = {}
        self._preview_inflight: set[tuple] = set()
        #: Clicks that arrived while a parse for their key was already in
        #: flight. They open the editor from that parse's finished slot rather
        #: than starting a second one.
        self._preview_waiting: dict[tuple, list] = {}
        self._preview_workers: list = []
        self._zone_overrides: Optional[dict] = None  # noqa: UP045
        self._velocity_overrides: Optional[dict] = None  # noqa: UP045
        self._name_overrides: dict = {}

        octave_row = QWidget()
        row_layout = QHBoxLayout(octave_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(QLabel("Middle C is:"))
        self._octave_box = QComboBox()
        self._octave_box.addItems([label for label, _offset in _OCTAVE_CHOICES])
        row_layout.addWidget(self._octave_box)
        row_layout.addStretch()
        # Relative to the format-picker row, not to a hardcoded 0: an optional
        # source header may sit above it (FormatConvertDialog._header_base).
        self.layout().insertWidget(self._header_base + 1, octave_row)

        placement_row = QWidget()
        placement_layout = QHBoxLayout(placement_row)
        placement_layout.setContentsMargins(0, 0, 0, 0)
        self._placement_button = QPushButton("Adjust Sample Placement…")
        self._placement_button.setEnabled(placement_loader is not None)
        self._placement_button.clicked.connect(self._on_adjust_placement_clicked)
        placement_layout.addWidget(self._placement_button)
        self._placement_status = QLabel("Auto-computed placement (default)")
        self._placement_status.setStyleSheet(
            "color: palette(placeholdertext); font-size: 11px;"
        )
        placement_layout.addWidget(self._placement_status, 1)
        self.layout().insertWidget(self._header_base + 2, placement_row)

        # Under placement, because it is the same kind of decision: what the
        # import writes, reviewed before it writes it.
        # _NAME_OCTAVE_FALLBACK, not the display one. This widget previews the
        # generated NAME, so on Auto-detect it has to use the offset the import
        # will use; with the display fallback it said C4 where the import wrote
        # C3. Its own set_octave_offset docstring already asks for exactly this
        # ("a preview that said C3 where the import wrote C4 would be worse
        # than no preview at all") -- it was the caller that did not comply.
        self._names = SampleNamesWidget(
            octave_offset=self.octave_offset() or _NAME_OCTAVE_FALLBACK
        )
        self.layout().insertWidget(self._header_base + 3, self._names)
        self._octave_box.currentIndexChanged.connect(
            lambda *_: self._names.set_octave_offset(
                self.octave_offset() or _NAME_OCTAVE_FALLBACK
            )
        )
        # Changing the middle-C convention changes every root note the parser
        # derives from the filenames, so it is a DIFFERENT parse -- re-parsed in
        # the background rather than on the next click.
        self._octave_box.currentIndexChanged.connect(
            lambda *_: self._request_preview(self.octave_offset())
        )

        # sample_loader/placement_loader both take the LIVE octave_offset
        # (this dialog's own choice), unlike ConvertOptionsDialog's plain
        # zero-arg bank_loader -- parse_sample_dir()'s root-note detection
        # depends on it, so both must re-read the combo box's current
        # value each time, not whatever it was when the dialog opened.
        # Wired up here (after the octave widget exists, after the base
        # constructor already built the Stereo group against a bank_loader
        # of None) rather than threaded through as a constructor arg to
        # __init__, since __init__ has no octave_offset() to close over yet.
        if sample_loader is not None:
            self._bank_loader = lambda: sample_loader(self.octave_offset())
            self._test_button.setEnabled(True)
            self._test_button.setToolTip(
                "Check the actual samples for stereo content and, for Mix, "
                "whether averaging would cancel signal on any of them."
            )

        # LAST, so the octave picker exists and the current offset is readable.
        self._prefill_preview()

    def octave_offset(self) -> Optional[int]:  # noqa: UP045
        return _OCTAVE_CHOICES[self._octave_box.currentIndex()][1]

    def zone_overrides(self) -> Optional[dict]:  # noqa: UP045
        return self._zone_overrides

    def velocity_overrides(self) -> Optional[dict]:  # noqa: UP045
        """{sample_name: (lo_vel, hi_vel)}, or None if the folder carried no
        layering or the editor was never accepted."""
        return self._velocity_overrides

    def sample_name_base(self) -> str:
        """Base name for `<base>-<key>` sample names, or "" to keep the names
        the conversion produced."""
        return self._names.base_name()

    def sample_name_with_key(self) -> bool:
        return self._names.with_key()

    def sample_name_overrides(self) -> dict:
        """Per-sample names typed in the placement editor, which win over the
        base scheme."""
        return dict(self._name_overrides)

    def _preview_key(self) -> tuple:
        """(source path, mtime_ns, size, octave) -- the identity of ONE parse.

        mtime and size, not just the path, because a cache over a folder is only
        worth having if an EDITED folder re-parses rather than offering what it
        used to be. `st_mtime_ns` on a directory changes when its entries do.

        The path is `_preview_path` when the caller gave one and the empty
        string when it did not -- the cache is per dialog, so "" is a perfectly
        good name for "whatever this dialog's own loader reads".
        """
        return _preview_key(self._preview_path or "", self.octave_offset())

    def _cached_preview(self):
        """The parse already in hand for the current octave, or None."""
        return self._preview_cache.get(self._preview_key())

    def _prefill_preview(self) -> None:
        """Parse the folder in the background, the moment the dialog opens.

        `parse_sample_dir` reads every WAV in the folder through mpc2emu's own
        parser. Measured on 400 mono 2-second WAVs: 160 ms per click, linear in
        the file count -- and a real multisample folder is 0.16-0.5 s and more,
        with tens of MB of PCM held while the window stops repainting. It used
        to run on the GUI thread inside `_on_adjust_placement_clicked`, so the
        button appeared to do nothing at all and then the whole application
        froze until it was done. It also re-ran on every octave change and again
        every time the editor was opened, for work that had not changed.

        A folder whose placement editor nobody opens is parsed for nothing,
        which is the only thing this costs. It is on a pool thread, and the
        cache is bounded, so that cost is not taken out of the window's time and
        does not grow.
        """
        self._request_preview()

    def _request_preview(self, on_done=None) -> bool:
        """Fill the preview cache for the current octave off the GUI thread.

        True if a parse was STARTED by this call, False if the answer is
        already in hand (in which case the caller should just use it) or one is
        already in flight (in which case `on_done` waits for it). A caller that
        has to ask at all is a click, and a click that has to wait is a click
        that does not freeze.

        `_placement_loader` is used when the caller gave no path: it is the same
        parse, and a dialog without a `preview_path` should not keep the GUI
        thread just because its caller did not hand over the folder.
        """
        key = self._preview_key()
        if key in self._preview_cache:
            return False
        if key in self._preview_inflight:
            if on_done is not None:
                self._preview_waiting.setdefault(key, []).append(on_done)
            return False
        if self._preview_path is not None:
            octave = self.octave_offset()

            def _parse():
                return sampledir_import.parse_preview(self._preview_path, octave)

        elif self._placement_loader is not None:
            octave = self.octave_offset()

            def _parse():
                return self._placement_loader(octave)

        else:
            return False  # nothing to parse from at all
        self._preview_inflight.add(key)
        w = workers.Worker(_parse)
        w.signals.finished.connect(lambda bank, k=key: self._on_preview(k, bank))
        w.signals.error.connect(lambda msg, k=key: self._on_preview_failed(k, msg))
        w.signals.finished.connect(
            lambda *_: (
                self._preview_workers.remove(w) if w in self._preview_workers else None
            )
        )
        w.signals.error.connect(
            lambda *_: (
                self._preview_workers.remove(w) if w in self._preview_workers else None
            )
        )
        self._preview_workers.append(w)
        workers.run(w)
        return True

    def _settle_preview(self, key: tuple) -> None:
        self._preview_inflight.discard(key)
        while len(self._preview_cache) > _MAX_PREVIEWS:
            self._preview_cache.pop(next(iter(self._preview_cache)))

    def _on_preview(self, key: tuple, bank) -> None:
        self._settle_preview(key)
        self._preview_cache[key] = bank
        self._placement_status.setText(_preview_note(bank))
        waiting = self._preview_waiting.pop(key, [])
        for on_done in waiting:
            on_done(bank)

    def _warn_unparseable(self, detail: str) -> None:
        """The ONE place this dialog says a folder would not parse.

        One raise-site, because `manual_dialog_seam` counts them: a modal
        reachable from anywhere is one no test can intercept, and a second
        phrasing of the same news does not earn a second site.
        """
        QMessageBox.warning(self, "Adjust Sample Placement", detail)

    def _on_preview_failed(self, key: tuple, message: str) -> None:
        self._settle_preview(key)
        self._placement_status.setText("Auto-computed placement (default)")
        self._warn_unparseable(
            "Couldn't parse the folder:\n\n" + workers.last_error_line(message)
        )
        # The clicks waiting on this parse are told there is nothing to edit.
        for on_done in self._preview_waiting.pop(key, []):
            on_done(None)

    def _on_adjust_placement_clicked(self) -> None:
        if self._placement_loader is None:
            return
        bank = self._cached_preview()
        if bank is not None:
            self._show_placement_editor(bank)
            return
        if not self._request_preview(on_done=self._on_placement_ready):
            # Nothing to parse from at any offset: the closure is the only
            # source there is, and the loader returns nothing usable.
            self._on_placement_ready(None)

    def _on_placement_ready(self, bank) -> None:
        if bank is None:
            self._placement_status.setText("Auto-computed placement (default)")
            return  # _on_preview_failed has already said so
        self._show_placement_editor(bank)

    def _show_placement_editor(self, bank) -> None:
        zones = bank.presets[0].voices[0].zones
        rows = [
            {
                "name": z.sample_name,
                "lo": z.lo_key,
                "root": z.root_key,
                "hi": z.hi_key,
                "lo_vel": z.lo_vel,
                "hi_vel": z.hi_vel,
            }
            for z in zones
        ]

        # Velocity columns only when the folder actually HAS layers. mpc2emu's
        # parser reads a velocity token in the filename (`-v40`, `-ff`) and
        # stacks a collided root by velocity instead of spreading it across
        # keys; where no filename says so, every zone is 0-127 and the columns
        # would be two identical numbers per row inviting an edit that means
        # nothing. This comment used to say the parser ALWAYS writes 0-127 --
        # true when the columns were first left out, wrong since mpc2emu
        # 50114de, and the reason the import could detect layering while the
        # editor still could not show it.
        layered = len({vel_window((z.lo_vel, z.hi_vel)) for z in zones}) > 1

        # Show the names the IMPORT will produce, not the ones the parse
        # produced. Same call the import itself makes (sampledir_import /
        # xpm_import), against the same bank, so the editor cannot drift from
        # the result: this is the only moment both the root notes and the
        # user's current base name exist together.
        #
        # Goes in `scheme_name`, never `new_name` -- see the placement
        # dialog's _baseline_name for why seeding the latter would turn every
        # untouched row into an override that outranks the scheme.
        base = self.sample_name_base()
        if base:
            # _NAME_OCTAVE_FALLBACK, not the display fallback: on Auto-detect
            # these two differ, and the one that decides the generated suffix
            # is the import's. Using the display value here would preview a
            # name the import does not produce, which is the very fault this
            # block exists to fix.
            octave = self.octave_offset()
            if octave is None:
                octave = _NAME_OCTAVE_FALLBACK
            scheme = names_from_base(bank, base, octave, self.sample_name_with_key())
            for row in rows:
                if row["name"] in scheme:
                    row["scheme_name"] = scheme[row["name"]]

        display_octave = self.octave_offset()
        if display_octave is None:
            display_octave = _DISPLAY_OCTAVE_OFFSET_FALLBACK
        dialog = SamplePlacementDialog(
            rows, octave_offset=display_octave, parent=self, show_velocity=layered
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._zone_overrides = dialog.overrides()
            before = {r["name"]: (r["lo_vel"], r["hi_vel"]) for r in rows}
            moved = {
                n: v
                for n, v in dialog.velocity_overrides().items()
                if v != before.get(n)
            }
            self._velocity_overrides = moved or None
            self._name_overrides = dialog.name_overrides()
            self._placement_status.setText(
                f"Custom placement set for {len(self._zone_overrides)} sample(s)"
            )
        # Cancel: whatever override (if any) was already set stays as-is.

    @staticmethod
    def get_import_options(
        parent=None,
        initial: Optional[ConversionOptions] = None,  # noqa: UP045
        title: str = "Import Sample Folder",
        warning_text: Optional[str] = None,  # noqa: UP045
        locked_format: Optional[str] = None,  # noqa: UP045
        sample_loader: Optional[Callable[[Optional[int]], list]] = None,  # noqa: UP045
        placement_loader: Optional[Callable[[Optional[int]], Any]] = None,  # noqa: UP045
        source_text: str = "",
        preview_path: Optional[str] = None,  # noqa: UP045
    ) -> tuple[
        Optional[ConversionOptions],  # noqa: UP045
        Optional[int],  # noqa: UP045
        Optional[dict],  # noqa: UP045
        str,
    ]:  # noqa: RUF100, UP045
        """Returns (opts, octave_offset, zone_overrides,
        (name_base, with_key, name_overrides)) -- the last tuple is the
        "Sample names" section plus any names typed per row in the placement
        editor. None, None, None, ("", True, {}) if cancelled. octave_offset isn't part of ConversionOptions (it
        only matters at parse time, before there's a Bank to apply
        resample/reduce options to at all); zone_overrides is the Sample
        Placement dialog's manual per-sample key-range/root override, or
        None if it was never opened or never accepted; name_base is the
        "Sample names" section's base name, or "" to keep whatever names the
        conversion produced."""
        dialog = SampleDirImportDialog(
            parent,
            initial=initial,
            title=title,
            warning_text=warning_text,
            locked_format=locked_format,
            sample_loader=sample_loader,
            placement_loader=placement_loader,
            source_text=source_text,
            preview_path=preview_path,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None, None, None, ("", True, {}), None
        return (
            dialog._to_options(),
            dialog.octave_offset(),
            dialog.zone_overrides(),
            (
                dialog.sample_name_base(),
                dialog.sample_name_with_key(),
                dialog.sample_name_overrides(),
            ),
            dialog.velocity_overrides(),
        )
