"""
The library tree: a single lazy QAbstractItemModel spanning three different
data sources (plain filesystem, vfs.Volume.list(), banks.*.parse()) so a node
expands directory -> image -> in-image folder -> bank -> preset/program, and
stops there (see the M3 plan: sample-level content is the Detail pane / the
Samples pane's job, never further tree rows).

Nothing here runs parsing/IO on the GUI thread: every fetch goes through
ui.workers.Worker on the shared thread pool, and results come back via a
queued Qt signal connection.
"""

from __future__ import annotations

import gzip
import os
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import (QAbstractItemModel, QMimeData, QModelIndex,
                            QSortFilterProxyModel, Qt, Signal)
from PySide6.QtGui import QColor

from . import dnd, workers
from ..banks import akai, e4b, eiii, krz
from ..build import foreign_import, xpm_import
from ..build.convert import ConvertOpError
from ..vfs.base import EntryKind
from ..vfs.detect import open_volume, sniff
from ..vfs.localdir import LocalDirVolume

EXPANDABLE_KINDS = {"directory", "volume_root", "folder", "bank", "mpc_project",
                    "foreign_bank"}

_KIND_ICON = {
    "directory": "\U0001F4C1",     # 📁
    "volume_root": "\U0001F4BF",   # 💿
    "folder": "\U0001F4C1",        # 📁
    "bank": "\U0001F4E6",          # 📦
    "preset": "\U0001F3B9",        # 🎹
    "xpm": "\U0001F39B",           # 🎛
    "mpc_project": "\U0001F5C2",   # 🗂
    "mpc_program": "\U0001F39B",   # 🎛
    "foreign_bank": "\U0001F4DA",    # 📚 -- one file, many instruments
    "foreign_preset": "\U0001F3BC",  # 🎼 -- one instrument, not a preset yet
    "unsupported": "\U00002753",   # ❓
}

_BANK_EXT_FORMAT = {".e4b": "E4B", ".krz": "KRZ", ".k25": "KRZ", ".k26": "KRZ",
                     ".e3x": "EIII", ".esi": "EIII", ".e3b": "EIII"}
# The MPC's three containers for one and the same keygroup program (see
# build/xpm_import.py, which owns the mapping): the leaf ones hold exactly
# one, a project holds one per track and is browsed like a bank.
MPC_FORMATS = frozenset(xpm_import.MPC_EXT_FORMAT.values())
# One "MPC" entry in the format dropdown covers all three -- three chips for
# what a user thinks of as one kind of file would be noise, and .xty/.xpj are
# far rarer than .xpm.
MPC_FILTER = "MPC"


# The two row kinds a soundfont-style source produces: a file holding many
# instruments, and one instrument (either a child of such a file, or a
# whole single-instrument file like an .sfz).
_FOREIGN_KINDS = ("foreign_bank", "foreign_preset")

# Everything that reaches New Bank by being CONVERTED rather than added --
# the soundfont-style sources and the MPC's containers alike. They all drag
# the same way, carrying a request instead of a preset (see ui/dnd.py).
#
# The MPC rows were deliberately not draggable at first, on the reasoning
# that only real E4B/KRZ/EIII content should be: an MPC program has to go
# through a conversion before it is a preset at all. That reasoning was
# right about the mechanism and wrong about the user -- once the soundfont
# sources could be dragged, a `.xpm` that refused to be was just an
# inconsistency, and the conversion is the same asynchronous round trip in
# both cases.
_IMPORT_DRAG_KINDS = _FOREIGN_KINDS + ("xpm", "mpc_project", "mpc_program")

#: Rows that hold other rows rather than being content themselves. A `bank`
#: is deliberately NOT one: it has presets under it, but the bank row is
#: itself actionable (favourites) and gets its own empty_reason when it holds
#: no preset.
_CONTAINER_KINDS = ("directory", "volume_root", "folder")

#: Formats whose FILE SIZE says nothing about what importing them costs,
#: because the audio lives beside the file rather than inside it.
#:
#: A 999 KB MPC keygroup program referenced 19 stereo WAVs of ~1 MB each and
#: added 20 MB to a bank -- the row said "999.2 KB" the whole time, while
#: every other row in the tree shows a size that does mean "this is what you
#: are adding". Showing a number that reads as the cost and is not it is
#: worse than showing none, so these rows show none, and the Detail pane's
#: "Total sample size" (which parses the program, so the figure is free
#: there) is the number that answers the question.
#:
#: `.talsmpl` is the worst of them and was missed on the first pass: TAL
#: stores sample paths the way Windows wrote them (`url="..\\Folder\\x.wav"`)
#: and a measured 21.8 KB preset referenced 80.6 MB of audio -- a factor of
#: 3 800, against the MPC program's 20.
#:
#: SF2 and GIG are NOT here: they embed their samples, so their file size is
#: exactly what it appears to be.
_AUDIO_LIVES_ELSEWHERE = {".xpm", ".xty", ".xpj", ".sfz", ".exs", ".talsmpl"}


def _import_request(node: TreeNode) -> dict:
    """The drag payload / context-menu argument for one import-source row.

    Two payload shapes, across five node kinds. A row addressing ONE entry
    of a multi-entry file carries `(path, ordinal)`; a row addressing a
    whole file carries just the path and imports everything in it -- an
    ordinal of None. `mpc_program` is the MPC's version of the former,
    `foreign_preset` the soundfont one.
    """
    if node.kind in ("mpc_program", "foreign_preset"):
        path, ordinal = node.payload
    else:
        path, ordinal = node.payload, None
    return {"path": str(path), "format": node.format_label,
            "ordinal": ordinal, "name": node.label}


def format_matches_filter(format_label: str, wanted: Optional[str]) -> bool:
    """Shared by the tree's filter proxy and the search-results filter, so
    both read one definition of what the dropdown's entries mean."""
    if wanted is None:
        return True
    if wanted == MPC_FILTER:
        return format_label in MPC_FORMATS
    return format_label == wanted


def _guess_format(name: str, meta_format: str = "") -> str:
    """Best format label available *before* a bank is actually opened —
    accurate for EMU3 entries (meta already carries a magic-sniffed value),
    a plausible guess from the extension otherwise (corrected once the bank
    node is actually fetched and its own magic bytes are checked)."""
    if meta_format in ("E4B", "EIII", "AKAI"):
        return meta_format
    if meta_format and meta_format != "system":
        return ""   # an unrecognised detected format — not one this app shows as a bank
    return _BANK_EXT_FORMAT.get(Path(name).suffix.lower(), "")


def human_size(n: int) -> str:
    if n <= 0:
        return ""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@dataclass
class TreeNode:
    kind: str                                  # 'directory' | 'volume_root' | 'folder' | 'bank' | 'preset'
                                                # | 'xpm' | 'mpc_project' | 'mpc_program'
    label: str
    parent: Optional["TreeNode"]
    payload: Any                                # Path | (Volume, Entry) | (BankFile, preset_obj)
                                                # | (Path, preset index) for 'mpc_program'
    children: Optional[list["TreeNode"]] = None  # None == not yet fetched
    handle: Any = None                          # opened Volume (volume_root/folder) or parsed BankFile (bank)
    size: int = 0
    #: Loadable audio this row costs, in bytes -- what a sampler has to find
    #: room for, which is the question the browser is actually asked. None
    #: means "not worked out", which is NOT zero: a ROM-only program really
    #: does reference no audio and stores 0.
    #:
    #: Shown INSTEAD of `size` where it is known. A bank's figure is deduped
    #: (what loading it costs); a preset's is what that one alone needs, so
    #: the children do not sum to the parent and are not meant to -- presets
    #: share samples.
    audio_bytes: Optional[int] = None
    format_label: str = ""
    fetching: bool = False
    error: Optional[str] = None
    note: str = ""                              # tooltip/Detail-pane reason for an
                                                # 'unsupported' row, when the generic
                                                # "no reader for this format" is wrong
    empty_reason: str = ""                      # read fine, holds nothing to import --
                                                # a different thing from `error`, and
                                                # the row must not claim it broke

    def display_text(self) -> str:
        icon = _KIND_ICON.get(self.kind, "")
        bits = [icon, self.label] if icon else [self.label]
        text = " ".join(bits)
        if self.format_label:
            text += f"  [{self.format_label}]"
        if self.audio_bytes is not None:
            # human_size(0) is "" -- it was written for `if self.size:`, where
            # zero never reaches it. Here zero is a real and interesting
            # answer, so it needs words of its own: a KRZ bank whose programs
            # reference only the sampler's ROM holds no audio at all, and
            # rendering that as a bare "audio" with nothing in front of it is
            # how it first appeared.
            text += ("   no audio" if self.audio_bytes == 0
                     else f"   {human_size(self.audio_bytes)} audio")
        elif self.size:
            text += f"   {human_size(self.size)}"
        if self.error:
            text += "   (failed to open)"
        elif self.empty_reason:
            text += "   (nothing to import)"
        return text


def _size_would_mislead(node: TreeNode) -> bool:
    """True for a row whose size has been suppressed on purpose."""
    if node.kind in ("xpm", "mpc_project", "mpc_program"):
        return True
    path = node.payload
    if isinstance(path, tuple) and path:
        path = path[0]
    return (isinstance(path, Path)
            and path.suffix.lower() in _AUDIO_LIVES_ELSEWHERE)


def _container_path_of(node: TreeNode) -> str:
    """The path the INDEX knows this row by, or "".

    Two shapes reach here and only one is a Path. A row read out of a real
    volume carries `(volume, entry)` and the file's own path is the entry's
    `ref`; an image row carries the Path directly. Assuming the Path form is
    what made the first version of this look up nothing at all and report no
    sizes, silently, which is exactly the failure the figure exists to end.
    """
    payload = node.payload
    if isinstance(payload, Path):
        return str(payload)
    if isinstance(payload, tuple) and len(payload) == 2:
        ref = getattr(payload[1], "ref", None)
        if ref:
            return str(ref)
        # A foreign row carries (Path, ordinal). The path IS the container
        # only for a row that stands on its own -- a .talsmpl or a lone .sfz.
        # A preset INSIDE a SoundFont carries the same path, and answering
        # with it would stamp the whole file's figure onto every preset row.
        if isinstance(payload[0], Path):
            parent = node.parent
            if parent is None or parent.kind not in _FOREIGN_KINDS:
                return str(payload[0])
    return ""


def _preset_audio_bytes(bank, obj) -> Optional[int]:
    """Loadable audio for one preset, or None if it cannot be worked out.

    Free at this point and only at this point: the bank has just been parsed
    to list its presets at all, and summarize_preset walks zone references
    without touching PCM. It already dedupes samples a preset reaches through
    more than one keymap, which is the figure wanted -- what taking this
    preset ALONE would cost, not its share of the bank.
    """
    try:
        from ..banks import summary
        return summary.summarize_preset(bank, obj).total_sample_bytes
    except Exception:
        return None


def _container_empty_reason(node: TreeNode) -> str:
    """Why this folder holds nothing to import, or "" if it does (or we do
    not know yet).

    THE "DO NOT KNOW YET" CASE IS THE WHOLE DIFFICULTY. This tree is lazy --
    a folder's children are read when it is expanded -- so a collapsed
    subfolder could hold anything. Greying a folder on the strength of the
    part we happen to have read would put "nothing here" on rows that have
    real content one level down, which is worse than leaving them plain:
    the grey is a claim, and an unread folder supports no claim.

    So an unread container child makes the answer "" no matter what its
    siblings look like. The consequence is that a folder greys when its
    subtree has been opened, not before, which is the honest version of
    "recursive up to the top".
    """
    kids = node.children
    if kids is None:
        return ""                      # not read yet
    if not kids:
        return f"{node.label} is empty."
    for k in kids:
        if k.kind in _CONTAINER_KINDS and k.children is None:
            return ""                  # unread subfolder -- unknown
        if k.error:
            return ""                  # a row that FAILED is not a row that is empty
        if not k.empty_reason and k.kind != "unsupported":
            return ""                  # something in here can be acted on
    return f"nothing under {node.label} can be imported."


# ── background fetch functions (run on a worker thread — no Qt here) ───────

def _fetch_children(node: TreeNode) -> list[TreeNode]:
    if node.kind == "directory":
        return _fetch_directory(node)
    if node.kind == "volume_root":
        return _fetch_volume_root(node)
    if node.kind == "folder":
        return _fetch_folder(node)
    if node.kind == "bank":
        return _fetch_bank(node)
    if node.kind == "mpc_project":
        return _fetch_mpc_project(node)
    if node.kind == "foreign_bank":
        return _fetch_foreign_bank(node)
    return []


def _fetch_directory(node: TreeNode) -> list[TreeNode]:
    out = _list_directory(node.payload, node)
    # A subdirectory that expands to nothing is pure noise: an MPC project's
    # data folder holding only WAVs, a folder of .rar archives, a spreadsheet
    # next to the discs it describes. Those rows are dropped -- the user still
    # sees every library root, just not dead ends below it.
    budget = [_PROBE_DIR_BUDGET]
    return [n for n in out
            if n.kind != "directory" or _dir_has_content(n.payload, budget)]


_PROBE_DIR_BUDGET = 400   # directories a single listing may look into before
                          # it stops judging and just shows the rows


def _dir_has_content(path: Path, budget: list[int]) -> bool:
    """True if *path* holds something this browser can show, at any depth.

    Deciding that needs exactly the rules _list_directory applies, so it calls
    it rather than growing a second copy of them, and stops at the first row it
    finds. Running out of budget answers True: showing a row that turns out
    empty is a far smaller wrong than hiding real content behind a walk we
    gave up on -- which is also why an unreadable directory stays visible.
    """
    budget[0] -= 1
    if budget[0] < 0:
        return True
    children = _list_directory(path, None)
    if not children:
        # LocalDirVolume.list() answers [] both for an empty directory and for
        # one it could not read at all. Tell those apart here: a directory
        # whose contents we never got to see keeps its row.
        try:
            with os.scandir(path) as it:
                next(it, None)
        except OSError:
            return True
        return False
    subdirs = []
    for n in children:
        if n.kind != "directory":
            return True
        subdirs.append(n.payload)
    return any(_dir_has_content(p, budget) for p in subdirs)


#: Extensions a loose AKAI program file can carry. `.P3`/`.P1` is the
#: sampler's own (the directory-entry type byte is derived from it, so it is
#: not decoration); `.a3p`/`.s3p` is what several extraction tools emit.
_AKAI_PROGRAM_EXTS = {".p3", ".p1", ".a3p", ".s3p"}


def _list_directory(path: Path, node: Optional[TreeNode]) -> list[TreeNode]:
    vol = LocalDirVolume(str(path))
    out: list[TreeNode] = []
    entries = vol.list()

    # A folder of loose AKAI files is one volume's worth of content: the
    # programs and the samples they name, side by side, exactly as they sat
    # on the disk they were extracted from. So the FOLDER is the bank row,
    # not each .P3 in it -- which is also the only affordable shape, since
    # every program in such a folder resolves against the same samples and
    # one row per program would re-read all of them once per row.
    if any(os.path.splitext(e.name)[1].lower() in _AKAI_PROGRAM_EXTS
           for e in entries if e.kind != EntryKind.DIRECTORY):
        out.append(TreeNode("bank", path.name, node, (None, path),
                             format_label="AKAI"))

    for e in entries:
        if e.kind == EntryKind.DIRECTORY:
            out.append(TreeNode("directory", e.name, node, Path(e.ref)))
        elif e.kind == EntryKind.BANK:
            out.append(TreeNode("bank", e.name, node, (vol, e), size=e.size,
                                 format_label=_guess_format(e.name)))
        elif e.kind == EntryKind.OTHER_FILE and e.meta.get("is_image"):
            if sniff(e.ref) is not None:
                out.append(TreeNode("volume_root", e.name, node, Path(e.ref), size=e.size))
        elif e.kind == EntryKind.OTHER_FILE and Path(e.name).suffix.lower() == xpm_import.PROJECT_EXT:
            # An MPC project holds one keygroup program per track, so it
            # browses like a bank -- expandable into its programs. Its own
            # kind, not "bank": _fetch_bank parses E4B/KRZ/EIII magic bytes
            # and would only fail on it.
            out.append(TreeNode("mpc_project", e.name, node, Path(e.ref), size=e.size,
                                 format_label=xpm_import.MPC_EXT_FORMAT[xpm_import.PROJECT_EXT]))
        elif e.kind == EntryKind.OTHER_FILE and Path(e.name).suffix.lower() in xpm_import.PROGRAM_EXTS:
            # One program per file: importable (see build/xpm_import.py),
            # with nothing to browse into -- a leaf row. But a project's data
            # folder holds one .xpm per track, and only a keygroup program
            # converts; see that module for what the other kinds are and why
            # each is treated the way it is here.
            # trust_name: a listing must not open every file it shows.
            kind = xpm_import.program_kind(e.ref, trust_name=True)
            if kind is None or kind in xpm_import.CONVERTIBLE_KINDS:
                label = xpm_import.MPC_EXT_FORMAT[Path(e.name).suffix.lower()]
                # A drum program reaching here is always MPC 2.x XML -- an
                # MPC 3 one is gzipped and reports kind None -- and 2.x is
                # exactly the case whose pad->key map is missing.
                out.append(TreeNode(
                    "xpm", e.name, node, Path(e.ref), size=0,
                    format_label=f"{label} drum kit" if kind == xpm_import.DRUM else label,
                    note=xpm_import.DRUM_2X_PAD_MAP_NOTE if kind == xpm_import.DRUM else ""))
        elif e.kind == EntryKind.OTHER_FILE:
            foreign = _foreign_node(Path(e.ref), e.name, node, e.size)
            if foreign is not None:
                out.append(foreign)
        # plain OTHER_FILE (WAVs, docs, ...): out of scope for this browser
    out.sort(key=lambda n: (n.kind not in ("directory", "volume_root"), n.label.lower()))
    return out


def _foreign_node(path: Path, name: str, node: Optional[TreeNode],
                  size: int) -> Optional[TreeNode]:
    """A row for a soundfont-style import source, or None if this is not one.

    Gated on mpc2emu being present (foreign_import.available()): these
    formats have no reader of their own here, so without it every row would
    be one that can only fail. Better not to offer them at all.

    Never parses -- the verdict comes from a header read (see
    vinsamlib/foreign_names.py), which is what makes listing a folder of
    1 GB SoundFonts as quick as listing any other folder.
    """
    verdict = foreign_import.inspect(path)
    if verdict is None:
        return None
    kind = "foreign_bank" if verdict.container else "foreign_preset"
    # An SFZ or EXS24 instrument is a text/plist file naming WAVs elsewhere,
    # so its own size is a few KB whatever the instrument weighs. SF2 and GIG
    # embed their audio and keep theirs.
    if path.suffix.lower() in _AUDIO_LIVES_ELSEWHERE:
        size = 0
    return TreeNode(kind, name, node,
                    (path, None) if kind == "foreign_preset" else path,
                    size=size, format_label=verdict.format,
                    note=verdict.note, empty_reason=verdict.empty_reason)


def _fetch_foreign_bank(node: TreeNode) -> list[TreeNode]:
    """One row per preset in a SoundFont or GIG file.

    The one place this deliberately departs from _fetch_mpc_project: it does
    NOT parse, and leaves node.handle as None. An MPC project has to be
    parsed to be listed, so that function caches the Bank on the node; a
    SoundFont names its presets in a header chunk that sits nowhere near the
    audio, so expanding one is a header read whatever its size. Parsing here
    instead would mean 2.7 s and ~3 GB of RSS to expand a single 1 GB row.

    The ordinal on each child is its position in the FILE. That is not
    necessarily its position in the parsed bank -- see
    foreign_import.resolve_ordinal(), which is where the two are reconciled.
    """
    path: Path = node.payload
    listed = foreign_import.list_presets(path)
    if listed is None:
        raise ValueError(f"{path.name} does not read as a "
                         f"{node.format_label} file.")
    if not listed:
        node.empty_reason = f"{path.name} holds no preset."
        return []
    return [TreeNode("foreign_preset", entry.display, node, (path, i),
                     format_label=node.format_label)
            for i, entry in enumerate(listed)]


def _fetch_volume_root(node: TreeNode) -> list[TreeNode]:
    path: Path = node.payload
    if node.handle is None:
        vol = open_volume(str(path))
        if vol is None:
            node.error = "not a recognised image"
            return []
        node.handle = vol
        # An image can be readable and still be missing most of itself -- a
        # partially copied AKAI disc keeps its partition table and volume
        # directories, which live at the front, so it lists its whole
        # contents and can deliver only the beginning of them. Not an error:
        # the files that ARE there read correctly and are worth browsing.
        # Carried on the row so the Detail pane can say so.
        warn = getattr(vol, "truncation_warning", None)
        if callable(warn):
            try:
                node.note = warn() or ""
            except Exception:
                pass
    return _fetch_vfs_listing(node.handle, None, node)


def _fetch_folder(node: TreeNode) -> list[TreeNode]:
    vol, entry = node.payload
    return _fetch_vfs_listing(vol, entry, node)


def _fetch_vfs_listing(vol, folder_entry, parent_node: TreeNode) -> list[TreeNode]:
    out: list[TreeNode] = []
    for e in vol.list(folder_entry):
        if e.kind == EntryKind.FOLDER and e.meta.get("akai_volume"):
            # An AKAI volume IS the bank: it holds the programs and the
            # samples they name, and the sampler resolves a name within it
            # and nowhere else. So it becomes a bank row expanding to its
            # programs, not a folder row expanding to files -- samples are
            # never tree rows in this browser, in any format.
            out.append(TreeNode("bank", e.name, parent_node, (vol, e),
                                 format_label="AKAI"))
        elif e.kind == EntryKind.FOLDER:
            out.append(TreeNode("folder", e.name, parent_node, (vol, e)))
        elif e.kind == EntryKind.BANK:
            out.append(TreeNode("bank", e.name, parent_node, (vol, e), size=e.size,
                                 format_label=_guess_format(e.name, e.meta.get("format", ""))))
        elif e.kind == EntryKind.OTHER_FILE and e.meta.get("format"):
            # Real content VinSamLib has no reader for (e.g. EIII/ESI-32
            # banks living inside an EMU3-filesystem disc alongside real
            # E4B ones -- see vfs/emu3.py's own detected_format). Shown
            # greyed out with its detected format rather than silently
            # dropped, so the folder doesn't look mysteriously empty when
            # it actually holds real (just unsupported) content -- not
            # expandable/importable, there's nothing to read it with yet.
            out.append(TreeNode("unsupported", e.name, parent_node, None, size=e.size,
                                 format_label=e.meta["format"]))
        # Plain OTHER_FILE with no detected format at all (WAVs, docs,
        # ...): still genuinely out of scope, not listed.
    out.sort(key=lambda n: (n.kind != "folder", n.label.lower()))
    budget = [_PROBE_DIR_BUDGET]
    return [n for n in out
            if n.kind != "folder" or _vfs_folder_has_content(*n.payload, budget)]


def _vfs_folder_has_content(vol, entry, budget: list[int]) -> bool:
    """_dir_has_content for a folder inside an image -- an empty 'New Folder'
    left on a disc is the same dead end as an empty directory, and hiding one
    but not the other would make the same tree behave two ways."""
    budget[0] -= 1
    if budget[0] < 0:
        return True
    try:
        children = vol.list(entry)
    except Exception:
        return True
    subfolders = []
    for e in children:
        if e.kind == EntryKind.FOLDER:
            subfolders.append(e)
        elif e.kind == EntryKind.BANK or (e.kind == EntryKind.OTHER_FILE
                                          and e.meta.get("format")):
            return True
    return any(_vfs_folder_has_content(vol, e, budget) for e in subfolders)


def _fetch_mpc_project(node: TreeNode) -> list[TreeNode]:
    """One row per keygroup program in an MPC project, the way a bank node
    lists its presets.

    The parsed mpc2emu Bank is cached on the node exactly as _fetch_bank
    caches a parsed E4B: parsing pulls every referenced WAV into memory
    (tens of MB for a real project), and re-doing that per Detail-pane click
    would make browsing crawl. A project with no keygroup program at all --
    only drum, MIDI or plugin tracks -- raises out of parse_mpc(), and the
    model's own fetch-error path shows that message on the row.

    A program does not drag the way a real preset does -- it only becomes an
    E4B/KRZ preset once it has been through a conversion, so there is no
    (bank, preset) pair to hand over. It drags as a *request* instead (see
    ui/dnd.py's IMPORT_MIME_TYPE), which opens the same Convert Options
    dialog its Import action does and delivers the presets when the
    conversion finishes."""
    path: Path = node.payload
    if node.handle is None:
        try:
            node.handle = xpm_import.parse_mpc(str(path))
        except ConvertOpError as ex:
            # Two very different things end up here: "this project holds
            # nothing convertible" -- a verdict on readable content -- and
            # "this file is broken". mpc2emu raises ValueError for both (for
            # the verdicts, and for a bad WAV header or a file that is not an
            # MPC document at all), so the exception cannot tell them apart.
            # The container itself can: if it still reads as an MPC project,
            # nothing failed and the row must not claim it did.
            if not (isinstance(ex.__cause__, ValueError)
                    and _reads_as_mpc_container(path)):
                raise
            node.empty_reason = workers.last_error_line(str(ex))
            return []
    presets = node.handle.presets
    if not presets:
        # mpc2emu skips a program that carries no sampled content (9a2c78b)
        # rather than emitting an empty preset, so a project whose programs
        # are ALL empty kits parses fine and returns nothing -- 5 projects in
        # the reference backup, where mpc2emu's own CLI prints "[SKIP] No
        # presets" and exits 1. Nothing failed; there is just nothing here --
        # as long as the file really is an MPC project. A document that is
        # neither also parses to nothing, and that one IS a failure.
        if not _reads_as_mpc_container(path):
            raise ValueError(f"{path.name} does not read as an MPC project.")
        node.empty_reason = (
            f"{path.name} holds no program with sampled content: every "
            f"program in it is an empty kit or track, so there is nothing "
            f"to import.")
        return []
    labels = _project_program_labels(path, presets)
    return [TreeNode("mpc_program",
                     (labels[i] if labels else preset.name).strip() or "(untitled)",
                     node, (path, i))
            for i, preset in enumerate(presets)]


_MPC_XML_ROOTS = {"Project", "MPCVObject"}


def _reads_as_mpc_container(path: Path) -> bool:
    """Whether *path* is still a readable MPC document, whatever mpc2emu made
    of its contents. Only run when a parse has already failed, to tell a
    verdict about real content from a file that is simply broken -- an .xpj
    of random bytes, a truncated one, or an X11 pixmap that happens to end
    .xpm. Structure only: nothing here judges what is inside."""
    try:
        with open(path, "rb") as f:
            gzipped = f.read(2) == b"\x1f\x8b"
        if gzipped:
            # MPC 3: gzip, then five header lines before the JSON payload --
            # 'ACVS' is the format's own magic (mpc2emu's _mpc3_read).
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as g:
                return g.readline().rstrip("\n") == "ACVS"
        return ET.fromstring(path.read_bytes()).tag in _MPC_XML_ROOTS
    except Exception:
        return False


def _project_program_labels(path: Path, presets: list) -> list[str]:
    """Full program names for an MPC 2.x project's rows, or [] to use the
    presets' own names.

    mpc2emu names a preset through its _safe_name(): ASCII, 16 characters,
    because that is what an E4B preset field holds. That is the right name for
    the preset an import produces and the wrong one for a browse row -- the
    same program listed from its data folder shows its whole filename, so a
    project row would call it 'XD- Jexus 193-Au' while the folder above calls
    it 'XD- Jexus 193-Auto sampled.Keygroup.xpm'.

    So rows are labelled from the files the project gathers, in the order
    mpc2emu gathers them (keygroups then drums, each sorted -- 9a2c78b), and
    each preset must land on a file whose _safe_name is exactly its name --
    checked through mpc2emu's own helper, so the two cannot drift apart.
    Files may be passed over on the way: a program carrying no sampled
    content is skipped rather than emitted as an empty preset, and 55 of the
    224 drum kits in the reference backup are empty ones.

    What is never allowed is a guess. If two of the project's programs share
    a truncated name, nothing here can say which one a preset came from, so
    the whole listing falls back to the preset names -- as it does if that
    helper is ever renamed, or the gathering changes again. Those names are
    never wrong, only short; a mislabelled row would name the program you did
    not import."""
    safe_name = getattr(xpm_import.xpm_parser, "_safe_name", None)
    if safe_name is None:
        return []
    try:
        with open(path, "rb") as f:
            if f.read(2) == b"\x1f\x8b":
                # MPC 3: gzipped payload, programs inside the .xpj. A data
                # folder can still sit beside it from an older save of the
                # same name -- one such project is in the reference backup --
                # and those files are not what was parsed.
                return []
    except OSError:
        return []
    data_dir = path.parent / f"{path.stem}_[ProjectData]"
    if not data_dir.is_dir():
        return []      # an MPC 3 project keeps its programs inside the .xpj
    stems = [p.name.rsplit(".Keygroup", 1)[0]
             for p in sorted(data_dir.glob("*.Keygroup.xpm"))]
    stems += [p.name.rsplit(".Drum", 1)[0]
              for p in sorted(data_dir.glob("*.Drum.xpm"))]
    short = [safe_name(s) for s in stems]
    have, want = Counter(short), Counter(p.name for p in presets)
    # Several programs can truncate to one name. That is still resolvable
    # while every one of them reached the bank -- order settles which is
    # which -- and unresolvable the moment one was skipped, because nothing
    # here can say which of them is missing.
    if any(have.get(name, 0) != n for name, n in want.items()):
        return []
    labels, at = [], 0
    for preset in presets:
        while at < len(stems) and short[at] != preset.name:
            at += 1    # a program that carried no samples -- skipped upstream
        if at == len(stems):
            return []
        labels.append(stems[at])
        at += 1
    return labels


def _fetch_bank(node: TreeNode) -> list[TreeNode]:
    vol, entry = node.payload
    if node.handle is None and node.format_label == "AKAI":
        # AKAI has no bank FILE to sniff -- a bank is a volume on a disk, or
        # a folder of loose .P3/.S3 files, so the payload names one of those
        # rather than a file to read bytes from.
        node.handle = (akai.parse_dir(str(entry)) if vol is None
                       else vol.volume_bank(entry))
        if not node.handle.programs:
            node.empty_reason = (
                f"{node.label} holds {len(node.handle.samples)} sample(s) but "
                f"no program, so there is nothing to import as a preset.")
            return []
    if node.handle is None:
        data = vol.read(entry)
        if data[:4] == b"FORM" and data[8:12] == b"E4B0":
            node.handle = e4b.parse_bytes(data, entry.name)
            node.format_label = "E4B"
        elif data[:4] == b"PRAM":
            node.handle = krz.parse_bytes(data, entry.name)
            node.format_label = "KRZ"
        elif eiii.detect_format(data) is not None:
            node.handle = eiii.parse_bytes(data, entry.name)
            node.format_label = "EIII"
        else:
            node.error = "not an E4B, KRZ or EIII bank"
            return []

    bank = node.handle
    return [TreeNode("preset", (p.name.strip() or "(untitled)"), node, (bank, p),
                     audio_bytes=_preset_audio_bytes(bank, p))
            for p in bank_presets(bank)]
    # preset order preserved — it reflects the bank's own numbering


def parse_bank_node(node) -> bool:
    """Read a bank node's file if the tree has not already done so.

    A bank row is only parsed when it is EXPANDED, so anything acting on a
    collapsed one finds `handle` empty. The favourites action first treated
    that as a refusal -- "expand it first", in the status bar -- which made it
    look broken: the menu entry is there, you click it, and nothing opens.
    Clicking the row is how you say which bank you mean; having to expand it
    as well is a step with no purpose.
    """
    if node.kind != "bank":
        return False
    if node.handle is None:
        _fetch_bank(node)
    return node.handle is not None


def bank_presets(bank) -> list:
    """The bank's presets in its own order, whatever the format calls them.

    E4B and EIII keep a `presets` list; a KRZ keeps `programs`, a dict keyed
    by object id. Anything that needs "the Nth preset of this bank" has to
    know that, and the two callers that do -- the Explorer tree and the
    favourites-list handler -- must agree, because a favourites list refers to
    presets BY POSITION.

    Public and shared for that reason. The handler first grew its own
    `getattr(bank, "presets", [])`, which is empty for a KRZ, so the action
    silently did nothing on exactly the format those notes are mostly written
    for. That is the second time a KRZ has been missed by code reaching for an
    attribute only E4B has -- see `_samples_of` in bank_pane.py, where the
    rename dialog opened with zero rows for the same reason.
    """
    # AKAI keeps a LIST of programs where KRZ keeps a dict keyed by object
    # id -- three shapes for the same idea, which is the whole reason this
    # helper is the single place that knows.
    if isinstance(bank, akai.AkaiBank):
        return list(bank.programs)
    if isinstance(bank, e4b.E4BFile) or isinstance(bank, eiii.EIIIFile):
        return list(bank.presets)
    return list(bank.programs.values())


# ── the Qt model ─────────────────────────────────────────────────────────────

class LibraryTreeModel(QAbstractItemModel):
    #: Something the model refused to do, in words. Qt gives a rejected drag
    #: no feedback at all -- mimeData() returning None just makes the drag
    #: not happen -- so the one case where that is a real decision (mixing
    #: presets and import sources in one drag) has to say so out loud.
    statusMessage = Signal(str)

    def __init__(self, roots: list[Path], parent=None, index_db=None):
        super().__init__(parent)
        #: Read-only, and touched ONLY from the GUI thread -- see
        #: _fill_in_audio_sizes for why that matters with sqlite.
        self._index_db = index_db
        sorted_roots = sorted(roots, key=lambda p: str(p).lower())
        self._roots: list[TreeNode] = [TreeNode("directory", str(p), None, p) for p in sorted_roots]
        self._live_workers: list[workers.Worker] = []   # keep references alive until done

    # -- growing/shrinking the tree from the outside (File > Add/Remove
    # Library Folder…) -----------------------------------------------------

    def add_root(self, path: Path) -> None:
        # Kept sorted alphabetically by path, not by insertion order --
        # otherwise a folder added later would always show up last
        # regardless of where it belongs alongside the others.
        key = str(path).lower()
        insert_at = 0
        while insert_at < len(self._roots) and str(self._roots[insert_at].payload).lower() < key:
            insert_at += 1
        self.beginInsertRows(QModelIndex(), insert_at, insert_at)
        self._roots.insert(insert_at, TreeNode("directory", str(path), None, path))
        self.endInsertRows()

    def remove_root(self, path: Path) -> bool:
        for i, node in enumerate(self._roots):
            if node.payload == path:
                self.beginRemoveRows(QModelIndex(), i, i)
                del self._roots[i]
                self.endRemoveRows()
                return True
        return False

    def is_empty(self) -> bool:
        return not self._roots

    # -- QAbstractItemModel plumbing -----------------------------------------

    def _node_for(self, index: QModelIndex) -> Optional[TreeNode]:
        if not index.isValid():
            return None
        return index.internalPointer()

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        node = self._node_for(parent)
        siblings = self._roots if node is None else (node.children or [])
        if row >= len(siblings):
            return QModelIndex()
        return self.createIndex(row, column, siblings[row])

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        node: TreeNode = index.internalPointer()
        if node.parent is None:
            return QModelIndex()
        grandparent = node.parent.parent
        siblings = self._roots if grandparent is None else grandparent.children
        row = siblings.index(node.parent)
        return self.createIndex(row, 0, node.parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        node = self._node_for(parent)
        if node is None:
            return len(self._roots)
        return len(node.children) if node.children is not None else 0

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        node = self._node_for(parent)
        if node is None:
            return bool(self._roots)
        if node.kind not in EXPANDABLE_KINDS:
            return False
        if node.children is None:
            return True   # not fetched yet — show the expand arrow optimistically
        return len(node.children) > 0

    def canFetchMore(self, parent: QModelIndex) -> bool:
        node = self._node_for(parent)
        if node is None or node.kind not in EXPANDABLE_KINDS:
            return False
        return node.children is None and not node.fetching

    def fetchMore(self, parent: QModelIndex) -> None:
        node = self._node_for(parent)
        if node is None or node.fetching:
            return
        node.fetching = True
        worker = workers.Worker(_fetch_children, node)
        worker.signals.finished.connect(
            lambda children, n=node, idx=QModelIndex(parent): self._on_fetched(n, children, idx))
        worker.signals.error.connect(lambda msg, n=node: self._on_fetch_error(n, msg))
        worker.signals.finished.connect(lambda *_: self._live_workers.remove(worker))
        worker.signals.error.connect(lambda *_: self._live_workers.remove(worker))
        self._live_workers.append(worker)
        workers.run(worker)

    def _on_fetched(self, node: TreeNode, children: list[TreeNode], parent_index: QModelIndex) -> None:
        node.fetching = False
        if not children:
            node.children = []
            # No rows to insert, but the row itself changed: it lost its
            # expander, and may now carry a reason it holds nothing.
            idx = self.node_index(node)
            if idx.isValid():
                self.dataChanged.emit(idx, idx)
            self._roll_up_emptiness(node)
            return
        self._fill_in_audio_sizes(children)
        self.beginInsertRows(parent_index, 0, len(children) - 1)
        node.children = children
        self.endInsertRows()
        self._roll_up_emptiness(node)

    #: Row kinds that ARE an indexed container, so the scan's recorded audio
    #: total is theirs. A preset row computes its own at expand time and a
    #: folder has no total of its own, so neither is looked up.
    _INDEXED_CONTAINER_KINDS = ("bank", "volume_root", "xpm", "mpc_project",
                                 "foreign_bank", "foreign_preset")

    def _fill_in_audio_sizes(self, children: list[TreeNode]) -> None:
        """Put the scan's recorded audio total onto rows that have one.

        The tree lists directories from the FILESYSTEM, so a bank row knows
        its path and nothing the scan worked out about it. Rather than parse
        every bank to size it -- which is what the lazy tree exists to avoid
        -- the figure is read back from the index in one query per listing.

        ON THE GUI THREAD, DELIBERATELY, and that is the whole reason it sits
        here rather than inside the worker that built these rows. The fetch
        functions run on a QThreadPool and this sqlite connection belongs to
        the GUI thread; the worker has already finished by the time
        _on_fetched runs, so the rows are in hand and nothing is shared. One
        indexed lookup over a whole listing is microseconds.

        A row the scan has not reached, or could not measure, keeps None and
        shows no figure -- an unmeasured row must not read as an empty one.
        """
        if self._index_db is None:
            return
        wanted = {}
        for n in children:
            if n.kind not in self._INDEXED_CONTAINER_KINDS:
                continue
            path = _container_path_of(n)
            if path:
                wanted.setdefault(path, []).append(n)
        if not wanted:
            return
        try:
            found = self._index_db.audio_bytes_for_paths(list(wanted))
        except Exception:
            return          # a stale or busy index must not break a listing
        for path, total in found.items():
            for n in wanted.get(path, ()):
                n.audio_bytes = total

    def _roll_up_emptiness(self, node: TreeNode) -> None:
        """Grey a folder whose whole READ subtree holds nothing to import,
        and carry that upward.

        Jan's ask: a folder of rows that all say "(nothing to import)" should
        say so itself, recursively. The rows already grey individually, which
        made the folder above them look like the one place worth opening.

        Stops at the first ancestor whose answer does not change: emptiness
        only propagates while it is newly true, so a parent that already knew
        cannot make its own parent change either. That keeps an expand from
        walking to the library root every time.
        """
        cur: Optional[TreeNode] = node
        while cur is not None and cur.kind in _CONTAINER_KINDS:
            before = cur.empty_reason
            after = _container_empty_reason(cur)
            if after == before:
                return
            cur.empty_reason = after
            idx = self.node_index(cur)
            if idx.isValid():
                self.dataChanged.emit(idx, idx)
            cur = cur.parent

    def _on_fetch_error(self, node: TreeNode, message: str) -> None:
        node.fetching = False
        node.error = workers.last_error_line(message)
        node.children = []
        idx = self.node_index(node)
        if idx.isValid():
            self.dataChanged.emit(idx, idx)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        node = self._node_for(index)
        if node is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return node.display_text()
        if role == Qt.ItemDataRole.UserRole:
            return node
        if role == Qt.ItemDataRole.ToolTipRole:
            if node.error:
                return node.error
            if node.empty_reason:
                return node.empty_reason
            if node.kind == "unsupported":
                return node.note or ("Real content, but VinSamLib has no reader "
                                      f"for this format ({node.format_label}) yet.")
            if (node.kind in _FOREIGN_KINDS and node.audio_bytes is None
                    and node.parent is not None
                    and node.parent.kind in _FOREIGN_KINDS):
                # A preset inside an SF2 or GIG. Its own audio total is
                # knowable but not cheaply: those formats embed their samples,
                # so the figure needs a real parse -- 2.7 s and about 3 GB of
                # RSS for the largest SoundFont here -- which has no business
                # running for every row of a listing. The Detail pane does
                # exactly that parse for the row you select, and shows it.
                return ("This format stores its samples inside the file, so "
                        "working out one preset's share means reading it. "
                        "Select the row and the Detail pane reports "
                        "\"Total sample size\".")
            if _size_would_mislead(node):
                # Not silence about a missing column: the row deliberately has
                # no size, and the reason is the useful half.
                return ("This format keeps its audio in separate files, so the "
                        "file's own size is not what importing it costs. "
                        "Select it and see \"Total sample size\" in the Detail "
                        "pane.")
        if role == Qt.ItemDataRole.ForegroundRole and (node.kind == "unsupported"
                                                        or node.empty_reason):
            # Same grey as unsupported content, and for the same reason: real,
            # readable, nothing here to act on.
            return QColor(Qt.GlobalColor.gray)
        return None

    # -- drag source (M5: presets drag into the New Bank column) ------------

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        node = self._node_for(index)
        if node is None:
            return base
        if node.kind == "preset":
            return base | Qt.ItemFlag.ItemIsDragEnabled
        if node.kind in _IMPORT_DRAG_KINDS and not node.empty_reason:
            # An import source drags too, but as a request rather than as a
            # preset (see ui/dnd.py). A row that already said it holds
            # nothing importable -- an all-encrypted TAL preset, a truncated
            # SoundFont, an MPC project of empty kits -- stays visible and
            # searchable and refuses the drag, rather than starting a
            # conversion with a foregone conclusion.
            return base | Qt.ItemFlag.ItemIsDragEnabled
        return base

    def mimeTypes(self) -> list[str]:
        return [dnd.DRAG_MIME_TYPE, dnd.IMPORT_MIME_TYPE]

    def mimeData(self, indexes: list[QModelIndex]) -> Optional[QMimeData]:
        seen: set[int] = set()
        items = []
        requests = []
        for idx in indexes:
            node = self._node_for(idx)
            if node is None or id(node) in seen:
                continue
            seen.add(id(node))
            if node.kind == "preset":
                bank, preset_obj = node.payload
                fmt = node.parent.format_label if node.parent else ""
                items.append((bank, preset_obj, fmt, node.label))
            elif node.kind in _IMPORT_DRAG_KINDS and not node.empty_reason:
                requests.append(_import_request(node))
        if items and requests:
            # Two different journeys -- one is already a preset, the other has
            # to be converted first -- and New Bank would have to run both a
            # plain add and a conversion queue off one drop. Refusing is
            # clearer than half-doing it.
            self.statusMessage.emit(
                "Presets and import sources can't be dragged together — "
                "drop one kind at a time")
            return None
        if requests:
            return dnd.build_import_mime_data(requests)
        return dnd.build_mime_data(items) if items else None

    # -- helpers used by the panes --------------------------------------------

    def node_index(self, node: TreeNode) -> QModelIndex:
        """Find the QModelIndex for a node we already have a reference to
        (used to refresh a row after an async error)."""
        parent_children = self._roots if node.parent is None else (node.parent.children or [])
        try:
            row = parent_children.index(node)
        except ValueError:
            return QModelIndex()
        parent_index = QModelIndex() if node.parent is None else self.node_index(node.parent)
        return self.index(row, 0, parent_index)


class BankFormatFilterProxy(QSortFilterProxyModel):
    """Sits between LibraryTreeModel and the tree view to implement the
    All/E4B/KRZ/EIII/XPM filter dropdown, without teaching the lazy tree model
    itself anything about filtering. Confirmed empirically (this project's
    running rule for anything PySide6-specific) that QSortFilterProxyModel
    correctly forwards canFetchMore()/fetchMore() *and* flags()/mimeData()
    to a custom lazy source model under PySide6/Qt6 -- both are needed
    here, since the tree only grows on demand and presets still need to
    stay draggable through the proxy.

    Only "bank", "xpm" and "mpc_project" nodes are ever actually filtered
    out. A directory/image/folder that turns out to contain zero matching
    rows is still shown (just ends up empty once expanded) rather than
    hidden pre-emptively -- knowing in advance which containers have
    matching content would mean scanning everything up front, which is
    exactly what this tree's lazy design exists to avoid. A project's own
    program rows are likewise never filtered: reaching one means its
    project already passed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._format_filter: Optional[str] = None

    def set_format_filter(self, fmt: Optional[str]) -> None:
        self._format_filter = fmt
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if self._format_filter is None:
            return True
        source_model = self.sourceModel()
        index = source_model.index(source_row, 0, source_parent)
        node = index.data(Qt.ItemDataRole.UserRole)
        if node is None or node.kind not in (
                "bank", "xpm", "mpc_project", "foreign_bank", "foreign_preset"):
            return True
        return format_matches_filter(node.format_label, self._format_filter)
