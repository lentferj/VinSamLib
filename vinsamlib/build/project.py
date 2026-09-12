"""Save the work in progress, and load it back.

WHAT A PROJECT IS. Everything staged but not yet written: New Bank's presets
and their edits, the Pending queue with its per-bank conversion options and
partition breaks. It is not a bank and not an image -- those are the outputs,
and Save as… / Build Image already write them.

WHAT IT CARRIES, AND WHAT IT ONLY POINTS AT. Jan's rule, and it is the right
one:

  * A preset that came out of a library FILE and has not been altered is a
    REFERENCE -- path, plus the identity of the preset inside it. Copying a
    118 MB bank into a project file to record "and this preset from it" would
    make saving cost more than the work being saved.
  * A preset that came out of a CONVERSION or an import is CARRIED. Its bytes
    exist only in a session temp directory that is deleted when the program
    exits, so a reference to it would be dead by the time anyone reloaded --
    and re-running the conversion is not the same thing: mpc2emu's laws are
    still being corrected week to week, so a rebuild months later can produce
    audibly different audio from the same source. What was staged is what
    gets saved.

THE FILE. A zip, because it is one file that can hold both the manifest and
those blobs, every platform opens it, and a curious user can look inside
without this project shipping a reader. `project.json` is the manifest;
`blobs/` holds the carried banks under content-addressed names, so two staged
presets out of one converted bank store its bytes once.

A REFERENCE CAN GO STALE and the loader says so rather than guessing. Each one
records the source's size and mtime; if the file has moved, changed or gone,
that entry is reported and skipped, and the rest of the project still loads.
A project that refuses entirely because one folder moved would be worse than
no project file at all.
"""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

FORMAT = "vinsamlib-project"
#: 1: New Bank + Pending, references and carried blobs.
VERSION = 1
SUFFIX = ".vslproj"


@dataclass
class LoadReport:
    """What came back, and what did not."""
    banks: list = field(default_factory=list)      # New Bank items
    pending: list = field(default_factory=list)    # Pending entries
    bank_name: str = ""
    bank_format: Optional[str] = None
    partition_breaks: set = field(default_factory=set)
    #: {"path": ..., "kind": ...} for the image column, or None.
    image: Optional[dict] = None
    #: {"expanded": [path, ...], "current": path} for the library tree.
    explorer: Optional[dict] = None
    sample_renames: dict = field(default_factory=dict)
    zone_placement: dict = field(default_factory=dict)
    voice_velocity: dict = field(default_factory=dict)
    #: One sentence per thing that could not be restored. Shown to the user;
    #: an empty list means everything came back.
    problems: list = field(default_factory=list)


def _source_stamp(path: Path) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtime": st.st_mtime}


def _stamp_matches(path: Path, stamp: dict) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    # mtime compared loosely: a copy between filesystems can shift it by a
    # rounding, and refusing a project over a microsecond would be absurd.
    return (st.st_size == stamp.get("size")
            and abs(st.st_mtime - float(stamp.get("mtime", 0))) < 2.0)


#: A staged bank whose path starts with one of these came out of a temp
#: directory this session made, so its bytes must travel WITH the project.
#: Everything else is a file in the user's library and is referenced.
_EPHEMERAL_PREFIXES = ("vinsamlib_convert_", "vinsamlib_pending_",
                       "vinsamlib_import_", "vinsamlib_stage_")


def _is_ephemeral(bank_path: str) -> bool:
    """True when this bank exists only for as long as the program runs.

    Judged on the path's own components rather than by asking tempdirs what
    it handed out: a bank can reach New Bank through several routes and the
    registry is not the thing that survives into a saved project. A false
    NEGATIVE carries bytes that did not need carrying, which costs disk; a
    false POSITIVE writes a reference that will be dead on reload, which
    costs the user their work. The default leans to carrying.
    """
    parts = Path(bank_path).parts
    return any(part.startswith(_EPHEMERAL_PREFIXES) for part in parts)


def _bank_bytes(bank: Any, fmt: str) -> Optional[bytes]:
    """The whole bank, as it would be written, so it can be carried."""
    from ..ui.bank_pane import _ASSEMBLE_FNS      # local: avoids a UI import cycle
    fn = _ASSEMBLE_FNS.get(fmt)
    if fn is None:
        return None
    try:
        source = Path(bank.path)
        if not _is_ephemeral(bank.path) and source.is_file():
            return source.read_bytes()
    except OSError:
        pass
    try:
        # An ephemeral bank's file may already be gone; re-assemble it whole
        # from the objects still in memory, which is what the user staged.
        presets = _all_presets(bank, fmt)
        return fn([(bank, p) for p in presets]) if presets else None
    except Exception:
        return None


def _all_presets(bank: Any, fmt: str) -> list:
    if fmt == "KRZ":
        return list(getattr(bank, "programs", {}).values())
    if fmt == "AKAI":
        return list(getattr(bank, "programs", []) or [])
    return list(getattr(bank, "presets", []) or [])


def _preset_ref(preset: Any, fmt: str) -> dict:
    """How to find this preset again inside its bank."""
    if fmt == "AKAI":
        return {"filename": getattr(preset, "filename", None),
                "name": getattr(preset, "name", None)}
    if fmt == "KRZ":
        return {"id": getattr(preset, "id", None)}
    return {"index": getattr(preset, "index", None)}


def _find_preset(bank: Any, fmt: str, ref: dict) -> Optional[Any]:
    if fmt == "AKAI":
        want_file = ref.get("filename")
        want_name = (ref.get("name") or "").strip()
        for p in _all_presets(bank, fmt):
            if want_file and getattr(p, "filename", None) == want_file:
                return p
            if want_name and (getattr(p, "name", "") or "").strip() == want_name:
                return p
        return None
    if fmt == "KRZ":
        return getattr(bank, "programs", {}).get(ref.get("id"))
    idx = ref.get("index")
    presets = _all_presets(bank, fmt)
    for p in presets:
        if getattr(p, "index", None) == idx:
            return p
    return presets[idx] if isinstance(idx, int) and 0 <= idx < len(presets) else None


# ── saving ───────────────────────────────────────────────────────────────────

def save(path: str, *, bank_items: list, bank_format: Optional[str],
         bank_name: str, sample_renames: dict, zone_placement: dict,
         voice_velocity: dict, pending: list, partition_breaks: set,
         image: Optional[dict] = None, explorer: Optional[dict] = None) -> str:
    """Write the whole staged state to `path`. Returns a one-line summary."""
    out = Path(path)
    if out.suffix.lower() != SUFFIX:
        out = out.with_suffix(SUFFIX)

    blobs: dict[str, bytes] = {}        # digest -> bytes, so one bank is stored once
    referenced = carried = 0

    def _bank_entry(bank: Any, fmt: str) -> dict:
        nonlocal referenced, carried
        bank_path = getattr(bank, "path", "") or ""
        src = Path(bank_path)
        if bank_path and not _is_ephemeral(bank_path) and src.is_file():
            referenced += 1
            return {"kind": "ref", "path": str(src.resolve()),
                    "stamp": _source_stamp(src), "format": fmt}
        data = _bank_bytes(bank, fmt)
        if data is None:
            return {"kind": "lost", "label": bank_path, "format": fmt}
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()
        blobs.setdefault(digest, data)
        carried += 1
        return {"kind": "blob", "digest": digest, "format": fmt,
                "label": Path(bank_path).name or "converted"}

    def _items_json(items: list, fmt: Optional[str]) -> list:
        rows = []
        for bank, preset, name in items:
            f = fmt or _guess_format(bank)
            rows.append({"bank": _bank_entry(bank, f),
                         "preset": _preset_ref(preset, f),
                         "name": name})
        return rows

    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "new_bank": {
            "name": bank_name,
            "format": bank_format,
            "items": _items_json(bank_items, bank_format),
            "sample_renames": {str(k): v for k, v in (sample_renames or {}).items()},
            "zone_placement": {str(k): v for k, v in (zone_placement or {}).items()},
            "voice_velocity": {str(k): v for k, v in (voice_velocity or {}).items()},
        },
        "pending": [
            {"name": e.get("name", ""), "format": e.get("format"),
             "items": _items_json(e.get("items", []), e.get("format")),
             # Convert options are a frozen dataclass of plain values.
             "convert_opts": _opts_json(e.get("convert_opts")),
             "sample_renames": {str(k): v for k, v in (e.get("sample_renames") or {}).items()},
             "zone_placement": {str(k): v for k, v in (e.get("zone_placement") or {}).items()},
             "voice_velocity": {str(k): v for k, v in (e.get("voice_velocity") or {}).items()}}
            for e in (pending or [])
        ],
        "partition_breaks": sorted(partition_breaks or ()),
        # Not work, but where the work was happening. Restoring these is what
        # makes a reopened project feel like coming back to a desk rather
        # than to a fresh install: the disc you were filling is open again
        # and the folder you were picking from is still unfolded.
        "image": image or None,
        "explorer": explorer or None,
    }

    tmp = out.with_suffix(out.suffix + ".part")
    # Written to a side file and moved into place: a project half-written over
    # the previous one is worse than no save at all, and this is the file the
    # user's unsaved work is being trusted to.
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project.json", json.dumps(manifest, indent=1))
        for digest, data in blobs.items():
            z.writestr(f"blobs/{digest}", data)
    tmp.replace(out)
    mb = out.stat().st_size / 1024 / 1024
    return (f"Saved {out.name}: {referenced} referenced, {carried} carried, "
            f"{mb:.1f} MB")


def _opts_json(opts) -> Optional[dict]:
    if opts is None:
        return None
    from dataclasses import asdict, is_dataclass
    return asdict(opts) if is_dataclass(opts) else None


def _guess_format(bank: Any) -> str:
    if hasattr(bank, "programs") and hasattr(bank, "keymaps"):
        return "KRZ"
    if hasattr(bank, "samples") and hasattr(bank, "programs"):
        return "AKAI"
    if hasattr(bank, "e4ma_body"):
        return "E4B"
    return "EIII"


# ── loading ──────────────────────────────────────────────────────────────────

def load(path: str) -> LoadReport:
    """Read a project back. Never raises for a stale reference -- it reports.

    Everything that CAN come back does, and each thing that cannot gets a
    sentence naming it. A project that refused to open because one library
    folder had moved would be worse than no project file at all: the work is
    still in there, and the user is the one who knows where the folder went.
    """
    rep = LoadReport()
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("project.json"))
        if manifest.get("format") != FORMAT:
            raise ValueError(f"{Path(path).name} is not a VinSamLib project.")
        if int(manifest.get("version", 0)) > VERSION:
            rep.problems.append(
                f"This project was written by a newer VinSamLib "
                f"(format {manifest.get('version')} against {VERSION}); "
                f"anything it does not recognise has been skipped.")
        cache: dict = {}

        def _bank_for(entry: dict):
            key = json.dumps(entry, sort_keys=True)
            if key in cache:
                return cache[key]
            bank = _restore_bank(z, entry, rep)
            cache[key] = bank
            return bank

        def _restore_items(rows: list, where: str) -> list:
            out = []
            for row in rows:
                bank = _bank_for(row["bank"])
                if bank is None:
                    continue
                fmt = row["bank"].get("format")
                preset = _find_preset(bank, fmt, row.get("preset") or {})
                if preset is None:
                    rep.problems.append(
                        f"{where}: \"{row.get('name')}\" is no longer in "
                        f"{Path(getattr(bank, 'path', '?')).name} — it was "
                        f"found by position and the bank has changed.")
                    continue
                out.append((bank, preset, row.get("name") or ""))
            return out

        nb = manifest.get("new_bank") or {}
        rep.bank_name = nb.get("name") or ""
        rep.bank_format = nb.get("format")
        rep.banks = _restore_items(nb.get("items") or [], "New Bank")
        rep.sample_renames = _int_keys(nb.get("sample_renames"))
        rep.zone_placement = _int_keys(nb.get("zone_placement"))
        rep.voice_velocity = _int_keys(nb.get("voice_velocity"))

        for e in manifest.get("pending") or []:
            items = _restore_items(e.get("items") or [], f"Pending \"{e.get('name')}\"")
            rep.pending.append({
                "name": e.get("name") or "",
                "format": e.get("format"),
                "items": items,
                "convert_opts": _opts_from_json(e.get("convert_opts")),
                "sample_renames": _int_keys(e.get("sample_renames")),
                "zone_placement": _int_keys(e.get("zone_placement")),
                "voice_velocity": _int_keys(e.get("voice_velocity")),
            })
        rep.partition_breaks = set(manifest.get("partition_breaks") or ())
        rep.image = manifest.get("image") or None
        rep.explorer = manifest.get("explorer") or None
    return rep


def _restore_bank(z: zipfile.ZipFile, entry: dict, rep: LoadReport):
    fmt = entry.get("format")
    kind = entry.get("kind")
    if kind == "lost":
        rep.problems.append(
            f"{entry.get('label') or 'A bank'} could not be saved when this "
            f"project was written, so it cannot be restored.")
        return None
    if kind == "blob":
        try:
            data = z.read(f"blobs/{entry['digest']}")
        except KeyError:
            rep.problems.append(f"{entry.get('label')}: its audio is missing "
                                f"from the project file.")
            return None
        return _parse_bytes(data, entry.get("label") or "converted", fmt, rep)
    src = Path(entry.get("path") or "")
    if not src.exists():
        rep.problems.append(f"{src} is gone — the presets taken from it were "
                            f"skipped. Put it back and load the project again.")
        return None
    if not _stamp_matches(src, entry.get("stamp") or {}):
        rep.problems.append(f"{src.name} has changed since the project was "
                            f"saved — its presets were skipped rather than "
                            f"restored from a file that may not match.")
        return None
    try:
        return _parse_bytes(src.read_bytes(), str(src), fmt, rep)
    except OSError as ex:
        rep.problems.append(f"{src.name}: {ex}")
        return None


def _parse_bytes(data: bytes, label: str, fmt: Optional[str], rep: LoadReport):
    from ..banks import e4b, eiii, krz
    try:
        if fmt == "KRZ":
            return krz.parse_bytes(data, label)
        if fmt == "EIII":
            return eiii.parse_bytes(data, label)
        if fmt == "AKAI":
            from ..banks import akai
            return akai.parse_volume([(Path(label).name, data)], name=Path(label).stem,
                                      path=label)
        return e4b.parse_bytes(data, label)
    except Exception as ex:
        rep.problems.append(f"{Path(label).name} could not be read back: {ex}")
        return None


def _opts_from_json(d: Optional[dict]):
    if not d:
        return None
    from .convert import ConversionOptions
    known = {f for f in ConversionOptions.__dataclass_fields__}
    return ConversionOptions(**{k: v for k, v in d.items() if k in known})


def _int_keys(d: Optional[dict]) -> dict:
    """JSON object keys are strings; these dicts are keyed by index."""
    out = {}
    for k, v in (d or {}).items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            out[k] = v
    return out


# ── crash safety ─────────────────────────────────────────────────────────────

AUTOSAVE_NAME = "autosave" + SUFFIX


def autosave_path() -> Path:
    """Where the crash-safety copy lives.

    In the data directory beside the index, not next to any bank: it is not
    the user's document, it is this program's safety net, and it must not
    appear in a library folder and get indexed as content.
    """
    from ..config import user_data_dir
    return user_data_dir() / AUTOSAVE_NAME


def clear_autosave() -> None:
    """Remove the crash file. Called on a CLEAN exit only.

    That is the whole mechanism: the file's existence at startup means the
    last run did not reach its own shutdown. Nothing records a crash, because
    a crash is exactly the case where nothing gets to record anything.
    """
    try:
        autosave_path().unlink()
    except OSError:
        pass
