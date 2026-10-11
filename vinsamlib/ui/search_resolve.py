"""
Turns a bare index.db.SearchResult (disconnected from any live Volume or
parsed bank) back into a real ui.models.TreeNode the existing Detail/Samples
panes can render unchanged — by re-opening the container and walking the
stored chain (folder/bank names, preset native ids) back down to the hit,
exactly as the scanner walked it when building the index.

Re-opening and re-parsing is the expensive thing this module does — a 30 MB
E4B measures 40 ms per resolve — so the parse is memoised against the
container's own identity rather than repeated. See `_LEVEL_CACHE`.
"""

from __future__ import annotations  # noqa: I001

from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional
from collections.abc import Callable

from .models import TreeNode
from ..banks import e4b, eiii, krz
from ..index.db import SearchResult
from ..vfs.detect import open_volume, sniff

#: How many CONTAINERS stay parsed at once. Bounded on purpose, and small:
#: one entry is one whole parsed bank held in RAM — the 30 MB E4B measured
#: here costs ~30 MB of that once resolved — so an unbounded map would be a
#: second leak to find in a year. Eight is what a working session touches
#: (the bank being dragged from, the bank being auditioned, a couple the user
#: keeps returning to), and it covers the case that actually hurt: sixty
#: presets of ONE bank, which is one entry.
#:
#: LRU by use: a container the user keeps coming back to stays, and one seen
#: once in passing is gone after eight others have arrived.
_MAX_CONTAINERS = 8

#: (container fingerprint, prefix signature) -> the TreeNode that prefix names.
#:
#: KEYED ON THE PREFIX, NOT ON THE HIT. The hit names one preset; the parse
#: produces the BANK it lives in, and the last step from bank to preset is a
#: dict lookup. Keying on the whole hit would cache sixty separate copies of
#: one 30 MB parse and still evict all but eight of them, which is the exact
#: freeze this exists to remove.
#:
#: The fingerprint is (st_mtime_ns, st_size) rather than a path string, so an
#: edited container re-parses instead of serving what it used to be.
_LEVEL_CACHE: OrderedDict[tuple, TreeNode | None] = OrderedDict()

#: How many times a container was actually READ AND PARSED since the last
#: `reset_cache()`. Not a cache entry count: it is the number that describes
#: the freeze, so it is what the measurement and the test both read.
_PARSE_COUNT = 0


def reset_cache() -> None:
    """Forget every parsed container, and the count of how many there were.

    Tests use both; nothing in the app has a reason to.
    """
    global _PARSE_COUNT
    _LEVEL_CACHE.clear()
    _PARSE_COUNT = 0


def cache_stats() -> tuple:
    """`(containers held, parses since the last reset)`."""
    return len(_LEVEL_CACHE), _PARSE_COUNT


def _fingerprint(path: Path) -> Optional[tuple]:  # noqa: UP045
    """(mtime_ns, size), or None when the container cannot be stated at all.

    None disables caching for that lookup rather than poisoning it: a
    container whose stat fails has no identity to key on, and re-parsing it
    every time is the honest answer.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _prefix_signature(prefix) -> tuple:
    """Which chain entries the cached node stands for.

    The index's own `native_id` is the scanner's handle for that entry, so it
    identifies the entry across re-parses — and two entries with the same name
    at the same place are the same entry.
    """
    return tuple((e.kind, e.native_id) for e in prefix)


def _container_key(container_path: str, prefix) -> Optional[tuple]:  # noqa: UP045
    fp = _fingerprint(Path(container_path))
    if fp is None:
        return None
    return (container_path, fp, _prefix_signature(prefix))


def _cached(key: Optional[tuple], build: Callable[[], Any]) -> Any:  # noqa: UP045
    """`build()` once per key, with a remembered result.

    LRU by use: a container the user keeps coming back to stays, and one
    seen once in passing is gone after eight others have arrived.
    """
    global _PARSE_COUNT
    if key is not None and key in _LEVEL_CACHE:
        _LEVEL_CACHE.move_to_end(key)
        return _LEVEL_CACHE[key]
    _PARSE_COUNT += 1
    value = build()
    if key is not None:
        _LEVEL_CACHE[key] = value
        while len(_LEVEL_CACHE) > _MAX_CONTAINERS:
            _LEVEL_CACHE.popitem(last=False)
    return value


#: How far down the chain the cached node reaches: the last entry that is NOT
#: the hit itself. A preset hit stops below its bank, so the bank is shared.
def _cache_prefix_len(chain) -> int:
    n = len(chain)
    while n > 0 and chain[n - 1].kind == "preset":
        n -= 1
    return n


def resolve_result(result: SearchResult) -> Optional[TreeNode]:  # noqa: UP045
    if result.kind in ("xpm", "mpc_project"):
        # An MPC hit's container *is* the .xpm/.xty/.xpj file itself (see
        # index/scanner.py's _scan_xpm_container -- one lightweight
        # container per file, not parsed at scan time) -- no bytes to
        # re-read or re-parse, just enough to reconstruct the same
        # TreeNode the tree itself builds for it.
        #
        # A project comes back unparsed (handle None), so the Detail pane
        # shows its size and says to expand it in the tree: parsing here
        # would load every WAV just to render one search result, and the
        # results list has no rows to expand into anyway.
        return TreeNode(
            result.kind,
            result.name,
            None,
            Path(result.container_path),
            format_label=result.format or "XPM",
        )
    if result.kind == "mpc_program":
        return _resolve_project_program(result)
    if result.kind in ("foreign_bank", "foreign_preset"):
        return _resolve_foreign(result)
    container_path = result.container_path
    if result.format == "AKAI" and Path(container_path).is_dir():
        return _resolve_akai_dir(container_path, result)
    if sniff(container_path) is None:
        return _resolve_loose_bank(container_path, result)
    return _resolve_in_image(container_path, result)


def _resolve_foreign(result: SearchResult) -> Optional[TreeNode]:  # noqa: UP045
    """A soundfont-style hit: the file itself, or one instrument in it.

    The happier version of _resolve_project_program below. That one has to
    parse a whole project to find its row again; here the names come from a
    header read, so re-deriving the ordinal costs microseconds even for a
    1 GB SoundFont -- and it is re-derived by NAME rather than trusted from
    the index, so a file edited since the last scan resolves to the right
    instrument instead of its neighbour.
    """
    from ..build import foreign_import  # circular at import time  # noqa: I001, RUF100

    path = Path(result.container_path)
    verdict = foreign_import.inspect(path)
    if verdict is None:
        return None
    if result.kind == "foreign_bank":
        return TreeNode(
            "foreign_bank",
            path.name,
            None,
            path,
            format_label=verdict.format,
            note=verdict.note,
            empty_reason=verdict.empty_reason,
        )
    ordinal = None
    if foreign_import.is_container(path):
        listed = foreign_import.list_presets(path) or []
        # The indexed name is the instrument's own display name (scanner.py
        # stores it as both name and native_id), so it is the key -- and it
        # is matched afresh against the file rather than trusted from the
        # index, so an edited file resolves to the right instrument.
        wanted = result.name.strip()
        matches = [
            i
            for i, entry in enumerate(listed)
            if entry.display == wanted or entry.name.strip() == wanted
        ]
        if not matches:
            # Gone from the file since the scan. The container row is the
            # honest answer -- that is where the user can see what it holds.
            return TreeNode(
                "foreign_bank", path.name, None, path, format_label=verdict.format
            )
        ordinal = matches[0]
    return TreeNode(
        "foreign_preset",
        result.name,
        None,
        (path, ordinal),
        format_label=verdict.format,
        note=verdict.note,
        empty_reason=verdict.empty_reason,
    )


def _resolve_akai_dir(container_path: str, result: SearchResult) -> Optional[TreeNode]:  # noqa: UP045
    """A folder of loose AKAI files, which the index stores as one bank.

    The only container here that is a DIRECTORY rather than a file, so it
    needs its own branch: everything else either sniffs as an image or reads
    as a bank file, and a directory does neither -- `read_bytes()` on one
    raises, which is how this used to return None and drop the hit.

    The folder holds every program, so the whole parse is one cache entry
    and finding the hit's program is a dict lookup on top of it."""
    from ..banks import akai as vs_akai

    def _build():
        try:
            bank = vs_akai.parse_dir(container_path)
        except Exception:  # noqa: BLE001
            return None
        return TreeNode(
            "bank",
            Path(container_path).name,
            None,
            (None, Path(container_path)),
            handle=bank,
            format_label="AKAI",
        )

    bank_node = _cached(_container_key(container_path, ()), _build)
    if result.kind == "bank" or bank_node is None:
        return bank_node
    native_id = result.chain[-1].native_id if result.chain else None
    prog = _find_preset(bank_node.handle, "AKAI", native_id)
    if prog is None:
        return bank_node
    return TreeNode("preset", result.name, bank_node, (bank_node.handle, prog))


def _resolve_project_program(result: SearchResult) -> Optional[TreeNode]:  # noqa: UP045
    """A program inside an MPC 3 project, indexed by name only.

    Unlike every other hit here this one has to parse -- a program only
    becomes importable once the project it lives in has been read, and the
    index deliberately stored no position, because a parse drops any program
    that holds no samples. So the project is expanded exactly as the tree
    expands it (same code, same caching, same row labels) and the row is
    found by name. If it is not there, the project row itself is the honest
    answer: that is where the user can see what it does hold.

    The EXPANSION is what costs — `xpm_import.parse_mpc` loads every WAV the
    project references — so the expanded project is the cache entry and the
    name walk runs fresh against it per hit, which is the only part that
    differs from one hit to the next.
    """
    from .models import (  # noqa: I001
        TreeNode as _TreeNode,
        _fetch_mpc_project,
    )  # circular at import time  # noqa: I001, RUF100

    def _build():
        project = _TreeNode(
            "mpc_project",
            Path(result.container_path).name,
            None,
            Path(result.container_path),
            format_label=result.format or "XPJ",
        )
        try:
            rows = _fetch_mpc_project(project)
        except Exception:  # noqa: BLE001
            rows = None  # the unexpanded project row is the honest answer below
        # The rows are held WITH the project, not re-derived per hit: the
        # expansion is the whole cost, and the labels come out of the parse.
        return project, rows

    project, rows = _cached(_container_key(result.container_path, ()), _build)
    wanted = result.name.strip()
    for row in rows or ():
        # A row is labelled with the program's own name; the preset inside it
        # carries a 16-character version of the same, and for an MPC 3
        # project that shortened form is what the row shows.
        if row.label == wanted or wanted.startswith(row.label):
            return row
    return project


def _resolve_loose_bank(
    container_path: str, result: SearchResult
) -> Optional[TreeNode]:  # noqa: UP045
    """The .e4b/.krz/.e3x case: read the file, parse it, find the hit in it.

    The read and the parse are one cache entry; the hit's own preset is found
    in the parsed bank per call, so every preset of one bank costs one parse
    rather than one parse each.
    """

    def _build():
        try:
            data = Path(container_path).read_bytes()
        except OSError:
            return None
        fmt, bank = _parse_bank_bytes(data, container_path)
        if bank is None:
            return None
        return TreeNode(
            "bank", Path(container_path).name, None, None, handle=bank, format_label=fmt
        )

    bank_node = _cached(_container_key(container_path, ()), _build)
    if result.kind == "bank" or bank_node is None:
        return bank_node
    preset_native_id = result.chain[-1].native_id if result.chain else None
    preset_obj = _find_preset(
        bank_node.handle, bank_node.format_label, preset_native_id
    )
    if preset_obj is None:
        return bank_node
    return TreeNode("preset", result.name, bank_node, (bank_node.handle, preset_obj))


def _resolve_in_image(container_path: str, result: SearchResult) -> Optional[TreeNode]:  # noqa: UP045
    """The same walk, inside a volume: open it, list down the chain, read the
    bank, find the hit's preset in it.

    Split at `_cache_prefix_len()`: everything down to and including the BANK
    is the cache entry, because `open_volume` + `vol.list` + `vol.read` +
    parse is the whole cost and none of it depends on which preset was hit.
    The descent from bank to preset then runs per call against the shared
    bank, exactly as for a loose file.
    """
    chain = list(result.chain or ())
    n = _cache_prefix_len(chain)
    head = _cached(
        _container_key(container_path, chain[:n]),
        lambda: _walk_in_image(container_path, chain[:n]),
    )
    if head is None:
        return None
    for entry in chain[n:]:
        if entry.kind != "preset" or head.handle is None:
            # A chain that stops at a folder, or a bank whose parse never
            # produced a handle: the node reached is the honest answer.
            break
        preset_obj = _find_preset(head.handle, head.format_label, entry.native_id)
        if preset_obj is None:
            break
        head = TreeNode("preset", entry.name, head, (head.handle, preset_obj))
    return head


def _walk_in_image(container_path: str, prefix) -> Optional[TreeNode]:  # noqa: UP045
    """Open the volume and walk `prefix` down it, or None if it won't open."""
    vol = open_volume(container_path)
    if vol is None:
        return None
    current_entry = None
    node: Optional[TreeNode] = None  # noqa: UP045
    for chain_entry in prefix:
        if chain_entry.kind in ("folder", "bank"):
            found = next(
                (e for e in vol.list(current_entry) if e.name == chain_entry.native_id),
                None,
            )
            if found is None:
                return node
            current_entry = found
            node = TreeNode(
                chain_entry.kind, found.name, node, (vol, found), size=found.size
            )
            if chain_entry.kind == "bank" and found.meta.get("akai_volume"):
                # An AKAI bank is a whole volume, not a file to read bytes
                # from -- see banks/akai.py. Same branch the tree takes.
                try:
                    node.handle = vol.volume_bank(found)
                    node.format_label = "AKAI"
                except Exception:  # noqa: BLE001, S110
                    pass
                continue
            if chain_entry.kind == "bank":
                try:
                    data = vol.read(found)
                except Exception:  # noqa: BLE001, S112
                    continue
                fmt, bank = _parse_bank_bytes(data, found.name)
                if bank is not None:
                    node.handle = bank
                    node.format_label = fmt
    return node


def _find_preset(bank, fmt: str, native_id: Optional[str]):  # noqa: UP045
    if native_id is None:
        return None
    if fmt == "AKAI":
        # An AKAI program has neither index nor id -- it is a file in a
        # volume, so its filename is what identifies it. Falls back to the
        # name for a program that reached the index without one.
        return next(
            (
                p
                for p in getattr(bank, "programs", [])
                if (p.filename or p.name) == native_id
            ),
            None,
        )
    try:
        if fmt == "E4B" or fmt == "EIII":
            idx = int(native_id)
            return next((p for p in bank.presets if p.index == idx), None)
        if fmt == "KRZ":
            return bank.programs.get(int(native_id))
    except (ValueError, AttributeError):
        return None
    return None


def _parse_bank_bytes(data: bytes, label: str):
    if data[:4] == b"FORM" and data[8:12] == b"E4B0":
        try:
            return "E4B", e4b.parse_bytes(data, label)
        except Exception:  # noqa: BLE001
            return "E4B", None
    if data[:4] == b"PRAM":
        try:
            return "KRZ", krz.parse_bytes(data, label)
        except Exception:  # noqa: BLE001
            return "KRZ", None
    if eiii.detect_format(data) is not None:
        try:
            return "EIII", eiii.parse_bytes(data, label)
        except Exception:  # noqa: BLE001
            return "EIII", None
    return "", None
