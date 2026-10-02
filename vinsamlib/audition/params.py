"""
Source -> ``(Bank, Preset, SourceProvenance)``.

**Render from mpc2emu's parsed model. Never from VinSamLib's own readers.**
Every playback parameter comes from their parse (spec §3). This module owns
the routes that get there, reusing exactly what ``build/convert.py`` already
does rather than duplicating it.

One parameter the model erases and this module carries itself: **which machine
the preset came from**. The E4B cutoff is a −3 dB corner, the K2000's is
already f0, and the E4B Z-plane types have been collapsed onto XPM "Low 4" by
``parsers/e4b_parser.py``. ``SourceProvenance`` is where that knowledge lives.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field  # noqa: F401
from pathlib import Path
from typing import Any, Optional

from .. import mpc2emu_bridge, tempdirs
from ..config import Config

_AUDITION_TEMP_PREFIX = "vinsamlib_audition_"

#: Bounded to 4: a KRZ bank carries tens of MB of PCM.
_BANK_CACHE: "OrderedDict[Any, BankHolder]" = OrderedDict()  # noqa: UP037
_BANK_CACHE_MAX = 4

# Guards the OrderedDict itself, not the parse. Two audition renders overlap
# whenever a second is started before the first finishes, and `move_to_end`
# plus `popitem` on a shared OrderedDict from two threads is a reordering of
# a container mid-iteration, not an atomic swap. The parse stays outside the
# lock; a duplicated parse costs ~30 ms and yields an equal Bank.
_BANK_CACHE_LOCK = threading.Lock()


class AuditionError(RuntimeError):
    """A refusal the user should see by name, not a crash."""


@dataclass
class _Parsed:
    bank: Any
    preset: Any
    provenance: "SourceProvenance"  # noqa: UP037
    #: The VinSamLib source objects (or path) this was parsed from. Kept in
    #: the cached value so their id() cannot be reused by a later allocation
    #: while the entry is live -- an in-memory cache keyed on id() without
    #: pinning the keyed object is a wrong-audio bug waiting for a GC.
    source: Any = None

    def __iter__(self):
        """Unpackable as ``(bank, preset, provenance)``, matching the
        documented ``parameters_for_*`` return shape."""
        return iter((self.bank, self.preset, self.provenance))


@dataclass(frozen=True)
class SourceProvenance:
    format: str  # 'E4B' | 'KRZ' | 'EIII' | 'AKAI' | 'MPC' | 'SF2' | ...
    origin: str  # a path or a human label, for the report
    # voice index -> vpar[58]; the E4XT filter byte. Only E4B sources carry it.
    e4b_filter_bytes: Optional[dict] = None  # noqa: UP045


# Alias kept so type hints read naturally in render.py.
BankHolder = _Parsed


def _cache_key(kind: str, payload: Any, ident: Any) -> Any:
    p = payload if isinstance(payload, (str, Path)) else None
    stamp = None
    if p is not None:
        try:
            st = Path(p).stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
    return (kind, str(p) if p is not None else "", ident, stamp)


def _cache_get(key):
    with _BANK_CACHE_LOCK:
        got = _BANK_CACHE.get(key)
        if got is not None:
            _BANK_CACHE.move_to_end(key)
        return got


def _cache_put(key, value):
    with _BANK_CACHE_LOCK:
        _BANK_CACHE[key] = value
        _BANK_CACHE.move_to_end(key)
        while len(_BANK_CACHE) > _BANK_CACHE_MAX:
            _BANK_CACHE.popitem(last=False)


def clear_cache() -> None:
    with _BANK_CACHE_LOCK:
        _BANK_CACHE.clear()


# ── the routes, one per source format ────────────────────────────────────────


def _e4b_filter_bytes(preset_obj) -> Optional[dict]:  # noqa: UP045
    """Read the E4XT filter byte (vpar[58]) per voice from the verbatim body.

    By the time the model reaches audition a phaser and a 4-pole lowpass are
    the same object: ``_E4B_TO_XPM_FILTER_TYPE`` maps Phaser, Flanger and all
    four Morph types onto XPM type 3. This is the only place that distinction
    still exists, so it is read here and carried on the provenance.
    """
    from ..banks import e4b as vs_e4b

    body = getattr(preset_obj, "body", None)
    if body is None:
        return None
    try:
        num_voices = vs_e4b.struct.unpack_from(">H", body, 20)[0]
        out: dict[int, int] = {}
        for i, (v_start, _table, _n) in enumerate(
            vs_e4b._walk_voices(body, num_voices)
        ):
            if v_start + 58 < len(body):
                out[i] = body[v_start + 58]
        return out or None
    except Exception:  # noqa: BLE001
        return None


def _assemble_and_parse(
    bank,
    preset_obj,
    kind,
    module,
    suffix,
    parser,
    edits: Optional[dict] = None,  # noqa: UP045
) -> _Parsed:  # noqa: RUF100, UP045
    # `edits` are New Bank's staged renames, placement, velocity and loop
    # repairs, bound exactly as BankPane._assemble_fn binds them for the
    # meter, Save as… and Send to Image. Without them an audition of a staged
    # preset played the unedited original.
    data = module.assemble([(bank, preset_obj)], **(edits or {}))
    tmp_dir = tempdirs.session_temp_dir(_AUDITION_TEMP_PREFIX)
    stem = _sanitize(getattr(preset_obj, "name", "") or "preset")
    tmp_path = tmp_dir / f"{stem}{suffix}"
    n_samples = _sample_count(data, suffix)
    if not n_samples:
        raise AuditionError(
            f"{getattr(preset_obj, 'name', 'This program')!r} references only "
            f"samples held in the sampler's ROM -- the bank file contains no "
            f"audio for them, so there is nothing to audition. (Browsing and "
            f"inspecting the bank still works.)"
        )
    tmp_path.write_bytes(data)
    parsed = parser(tmp_path)
    preset = parsed.presets[0] if parsed.presets else None
    if preset is None:
        raise AuditionError(
            f"mpc2emu's parser read no preset from "
            f"{getattr(preset_obj, 'name', '?')!r}."
        )
    prov = SourceProvenance(format=kind, origin=str(getattr(bank, "path", "")))
    if kind == "E4B":
        prov = SourceProvenance(
            format=kind,
            origin=str(getattr(bank, "path", "")),
            e4b_filter_bytes=_e4b_filter_bytes(preset_obj),
        )
    return _Parsed(parsed, preset, prov, source=(bank, preset_obj))


def _sample_count(data: bytes, suffix: str) -> int:
    from ..banks import e4b as vs_e4b
    from ..banks import eiii as vs_eiii
    from ..banks import krz as vs_krz

    reader = {".krz": vs_krz, ".e3x": vs_eiii}.get(suffix.lower(), vs_e4b)
    try:
        return len(reader.parse_bytes(data, "audition").samples)
    except Exception:  # noqa: BLE001
        return -1


def _sanitize(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_()" else "_" for c in name)
    return (keep.strip() or "preset")[:48]


def _akai_route(bank, program, edits: Optional[dict] = None) -> _Parsed:  # noqa: UP045
    # TODO (spec stage 2, deliberately not done yet): the rule that the
    # program and its samples must land in SEPARATE directories lives here
    # AND in convert._convert_akai_program(). A real library disc converts to
    # noise if either copy drifts, so the two want one home --
    # convert.parse_preset(). Not extracted with the audition work because
    # that refactor is guarded by manual_akai_convert and
    # manual_hw_convert_matrix, which must be run either side of it, and
    # folding a conversion-path change into a new feature would make a
    # failure in those two ambiguous about which change caused it.
    from ..banks import akai as vs_akai

    ok, reason = Config.load().check_akai_read_support()
    if not ok:
        raise AuditionError(reason)
    tmp_dir = tempdirs.session_temp_dir(_AUDITION_TEMP_PREFIX)
    samples_dir = tmp_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    # AKAI's assemble() takes none of the placement or loop-repair arguments
    # (see BankPane._RENAMEABLE / _PLACEABLE / _LOOP_REPAIRABLE), so `edits`
    # is normally empty here. Passed through anyway rather than dropped, so
    # that a format gaining support upstream does not need this line found.
    files = vs_akai.assemble([(bank, program)], **(edits or {}))
    program_files = [(fn, d) for fn, d in files if fn.upper().endswith((".P3", ".P1"))]
    sample_files = [(fn, d) for fn, d in files if (fn, d) not in program_files]
    if not program_files:
        raise AuditionError(
            f"{getattr(program, 'name', '?')!r} produced no AKAI program file."
        )
    vs_akai.write_volume(sample_files, str(samples_dir))
    program_path = tmp_dir / program_files[0][0]
    program_path.write_bytes(program_files[0][1])
    parsed = mpc2emu_bridge.akai_parser.parse_akai_program(
        str(program_path), str(samples_dir)
    )
    preset = parsed.presets[0] if parsed.presets else None
    if preset is None:
        raise AuditionError(
            f"mpc2emu read no program from " f"{getattr(program, 'name', '?')!r}."
        )
    return _Parsed(
        parsed,
        preset,
        SourceProvenance(format="AKAI", origin=str(getattr(bank, "path", ""))),
        source=(bank, program),
    )


def _foreign_route(path, ordinal) -> _Parsed:
    from ..build import foreign_import

    fmt = foreign_import.format_for(path) or "foreign"
    bank = foreign_import.parse_foreign(
        path, max_presets=foreign_import._listed_count(path)
    )
    if not bank.presets:
        raise AuditionError(f"{Path(path).name} holds no preset to audition.")
    index = 0
    if ordinal is not None and foreign_import.is_container(path):
        listed = foreign_import.list_presets(path) or []
        # index_for_row, NOT resolve_ordinal. An Ensoniq disc parses to more
        # presets than its header lists -- 613 instruments become 2396,
        # because EOS writes each one's layer-mask variants separately -- and
        # resolve_ordinal refuses that outright with "import the whole file
        # rather than one preset of it", which is what Audition showed for
        # every EPS instrument. The first index of the run is the variant the
        # row names.
        index = foreign_import.index_for_row(bank, listed, ordinal, path)
    return _Parsed(
        bank,
        bank.presets[index],
        SourceProvenance(format=fmt, origin=str(path)),
        source=(path, ordinal),
    )


def _mpc_route(path, preset_index) -> _Parsed:
    from ..build import xpm_import

    bank = xpm_import.parse_mpc(str(path))
    if not bank.presets:
        raise AuditionError(f"{Path(path).name} holds no program to audition.")
    index = int(preset_index or 0)
    if not (0 <= index < len(bank.presets)):
        raise AuditionError(f"{Path(path).name} has no program {index + 1}.")
    return _Parsed(
        bank,
        bank.presets[index],
        SourceProvenance(format="MPC", origin=str(path)),
        source=(path, index),
    )


# ── the public entry points ──────────────────────────────────────────────────


def parameters_for_node(payload, kind: str) -> _Parsed:
    """Explorer TreeNode payload -> parsed mpc2emu Bank + Preset + provenance."""
    if kind == "preset":
        bank, preset_obj = payload
        from ..banks import akai as vs_akai
        from ..banks import e4b as vs_e4b
        from ..banks import eiii as vs_eiii
        from ..banks import krz as vs_krz

        key = _cache_key(kind, None, (id(bank), id(preset_obj)))
        cached = _cache_get(key)
        if cached is not None:
            return cached
        if isinstance(bank, vs_e4b.E4BFile):
            parsed = _assemble_and_parse(
                bank,
                preset_obj,
                "E4B",
                vs_e4b,
                ".e4b",
                mpc2emu_bridge.e4b_parser.parse_e4b,
            )
        elif isinstance(bank, vs_krz.KrzFile):
            parsed = _assemble_and_parse(
                bank,
                preset_obj,
                "KRZ",
                vs_krz,
                ".krz",
                mpc2emu_bridge.krz_parser.parse_krz,
            )
        elif isinstance(bank, vs_eiii.EIIIFile):
            parsed = _assemble_and_parse(
                bank,
                preset_obj,
                "EIII",
                vs_eiii,
                ".e3x",
                mpc2emu_bridge.eiii_parser.parse_eiii,
            )
        elif isinstance(bank, vs_akai.AkaiBank):
            # No `edits` here, and that is the point: this is the EXPLORER
            # path, which auditions a program as it sits on the disc. Staged
            # renames, placement and loop repairs belong to New Bank and
            # reach the renderer through `parameters_for_staged` instead.
            # This line said `edits` and there is no such name in this
            # function -- a NameError that took out AKAI audition entirely
            # while the other three formats worked.
            parsed = _akai_route(bank, preset_obj)
        else:
            raise AuditionError(f"not a recognised bank for audition: {type(bank)!r}")
        _cache_put(key, parsed)
        return parsed
    if kind == "xpm":
        key = _cache_key(kind, payload, None)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        parsed = _mpc_route(payload, None)
        _cache_put(key, parsed)
        return parsed
    if kind == "mpc_program":
        path, index = payload
        key = _cache_key(kind, path, index)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        parsed = _mpc_route(path, index)
        _cache_put(key, parsed)
        return parsed
    if kind == "foreign_preset":
        path, ordinal = payload
        key = _cache_key(kind, path, ordinal)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        parsed = _foreign_route(path, ordinal)
        _cache_put(key, parsed)
        return parsed
    if kind == "foreign_bank":
        key = _cache_key(kind, payload, None)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        parsed = _foreign_route(payload, None)
        _cache_put(key, parsed)
        return parsed
    raise AuditionError(f"cannot audition a {kind!r} node")


def _edits_fingerprint(edits: Optional[dict]) -> str:  # noqa: UP045
    """A stable, cheap identity for a set of staged edits.

    repr() of sorted items rather than a hash of the objects: the dicts hold
    plain names, ints and tuples, and two equal edit sets must produce one
    cache entry while any change produces another.
    """
    if not edits:
        return ""
    return repr(sorted((k, repr(v)) for k, v in edits.items()))


def parameters_for_staged(
    bank,
    preset_obj,
    name: str = "",
    edits: Optional[dict] = None,  # noqa: UP045
) -> _Parsed:  # noqa: RUF100, UP045
    """New Bank's staged ``(bank, preset_obj, name)`` tuple -> parsed model.

    Auditioning a staged preset is the one path that catches a bad build before
    media is written: renames, placement edits and loop repairs are applied by
    the assembler, so what is heard is what would be written.
    """
    from ..banks import akai as vs_akai
    from ..banks import e4b as vs_e4b
    from ..banks import eiii as vs_eiii
    from ..banks import krz as vs_krz

    # THE EDITS ARE PART OF THE KEY. Keyed on id() alone, a re-audition after
    # correcting a placement returned the first parse -- the pinned objects
    # keep their ids for the lifetime of the row, so nothing ever invalidated.
    key = _cache_key(
        "staged", None, (id(bank), id(preset_obj), _edits_fingerprint(edits))
    )
    cached = _cache_get(key)
    if cached is not None:
        return cached
    if isinstance(bank, vs_e4b.E4BFile):
        parsed = _assemble_and_parse(
            bank,
            preset_obj,
            "E4B",
            vs_e4b,
            ".e4b",
            mpc2emu_bridge.e4b_parser.parse_e4b,
            edits,
        )
    elif isinstance(bank, vs_krz.KrzFile):
        parsed = _assemble_and_parse(
            bank,
            preset_obj,
            "KRZ",
            vs_krz,
            ".krz",
            mpc2emu_bridge.krz_parser.parse_krz,
            edits,
        )
    elif isinstance(bank, vs_eiii.EIIIFile):
        parsed = _assemble_and_parse(
            bank,
            preset_obj,
            "EIII",
            vs_eiii,
            ".e3x",
            mpc2emu_bridge.eiii_parser.parse_eiii,
            edits,
        )
    elif isinstance(bank, vs_akai.AkaiBank):
        parsed = _akai_route(bank, preset_obj, edits)
    else:
        raise AuditionError(f"not a recognised bank for audition: {type(bank)!r}")
    _cache_put(key, parsed)
    return parsed
