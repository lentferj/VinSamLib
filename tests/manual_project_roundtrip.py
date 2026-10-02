"""A saved project must come back, and must say what did not.

Run:  QT_QPA_PLATFORM=offscreen .venv/bin/python tests/manual_project_roundtrip.py

Jan's rule for what a project file holds: reference a source that has not
been altered, CARRY anything a conversion or import produced. The two halves
fail differently and both are checked here.

  * A REFERENCE keeps the file tiny -- a project over a 14 MB bank is under a
    kilobyte -- and goes stale when the source moves or changes. Stale must be
    REPORTED and skipped, never guessed at: restoring presets by position out
    of a file that has since changed is how you get the wrong sound with no
    error anywhere.
  * A CARRIED bank has to survive the thing it came from disappearing, which
    it will: conversion results live in a session temp directory that is
    deleted when the program exits. If carrying did not work, a project saved
    after an import would look fine and be empty tomorrow.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _status import run as _status_run  # noqa: E402

from _tmp import scratch_dir, use_big_temp_root  # noqa: E402

use_big_temp_root()

from vinsamlib.banks import e4b  # noqa: E402
from vinsamlib.build import project  # noqa: E402


def _source() -> Path | None:
    for root in (
        Path.home() / "Dokumente/SYNTHS/E4XT/E4Bs",
        Path.home() / "Dokumente/SYNTHS",
    ):
        if root.exists():
            for p in root.rglob("*.e4b"):
                if 200_000 < p.stat().st_size < 30_000_000:
                    return p
    return None


def _save(path, items, **kw):
    return project.save(
        str(path),
        bank_items=items,
        bank_format="E4B",
        bank_name=kw.pop("name", "X"),
        sample_renames={},
        zone_placement={},
        voice_velocity={},
        pending=kw.pop("pending", []),
        partition_breaks=kw.pop("breaks", set()),
    )


def main() -> int:
    src = _source()
    if src is None:
        print("no E4B corpus on this machine")
        return 77
    failures: list[str] = []
    work = scratch_dir()

    # -- 1. a reference: small file, everything back -----------------------
    copy = work / "src.e4b"
    shutil.copy(src, copy)
    bank = e4b.parse_bytes(copy.read_bytes(), str(copy))
    items = [
        (bank, p, (p.name or "").strip() or f"P{i}")
        for i, p in enumerate(bank.presets[:3])
    ]
    ref_proj = work / "ref.vslproj"
    print(
        "  "
        + _save(
            ref_proj,
            items,
            name="MyBank",
            pending=[
                {
                    "name": "Bank 1",
                    "format": "E4B",
                    "items": items,
                    "convert_opts": None,
                }
            ],
            breaks={1},
        )
    )
    kb = ref_proj.stat().st_size / 1024
    print(
        f"  project is {kb:.1f} KB over a "
        f"{copy.stat().st_size/1024/1024:.0f} MB bank"
    )
    if kb > 200:
        failures.append(
            f"a referencing project is {kb:.0f} KB — it is "
            f"copying audio it was supposed to point at"
        )
    rep = project.load(str(ref_proj))
    if len(rep.banks) != 3 or rep.bank_name != "MyBank":
        failures.append(
            f"New Bank came back as {len(rep.banks)} items / " f"{rep.bank_name!r}"
        )
    if len(rep.pending) != 1 or len(rep.pending[0]["items"]) != 3:
        failures.append("the pending queue did not come back")
    if rep.partition_breaks != {1}:
        failures.append(f"partition breaks came back as {rep.partition_breaks}")
    if rep.problems:
        failures.append(f"a clean load reported problems: {rep.problems}")
    print(
        f"  reloaded: {len(rep.banks)} presets, {len(rep.pending)} pending, "
        f"breaks={rep.partition_breaks}"
    )

    # -- 2. carried: survives its temp directory being deleted -------------
    eph = work / "vinsamlib_convert_zz" / "out.e4b"
    eph.parent.mkdir(parents=True)
    shutil.copy(src, eph)
    conv = e4b.parse_bytes(eph.read_bytes(), str(eph))
    carried = work / "carried.vslproj"
    print("  " + _save(carried, [(conv, conv.presets[0], "converted")]))
    shutil.rmtree(eph.parent)  # what happens when the program exits
    rep2 = project.load(str(carried))
    print(
        f"  after its temp dir vanished: {len(rep2.banks)} item(s), "
        f"problems={len(rep2.problems)}"
    )
    if len(rep2.banks) != 1:
        failures.append(
            "a CONVERTED preset did not survive its temp directory "
            "being deleted — which is what happens on every quit, "
            "so the project would be empty the next day"
        )
    if carried.stat().st_size < 1_000_000:
        failures.append("the carried project is too small to hold the bank")

    # -- 3. a stale reference is reported, not guessed ---------------------
    with open(copy, "r+b") as fh:
        fh.seek(0, 2)
        fh.write(b"\0")
    rep3 = project.load(str(ref_proj))
    print(
        f"  source changed -> {len(rep3.banks)} restored, "
        f"{len(rep3.problems)} problem(s)"
    )
    if rep3.banks:
        failures.append(
            "presets were restored from a source that has CHANGED "
            "— by position, out of a file that may no longer hold "
            "the same presets"
        )
    if not rep3.problems:
        failures.append("a changed source was skipped silently")

    copy.unlink()
    rep4 = project.load(str(ref_proj))
    if not rep4.problems or "gone" not in " ".join(rep4.problems):
        failures.append("a missing source was not named")
    print(
        f"  source gone    -> {rep4.problems[0][:70] if rep4.problems else '(silent)'}"
    )

    # -- 4. old-style list values survive round-trip --------------------------
    # A project saved by an earlier version stores placement/velocity values
    # as JSON lists. The new `_name_keys` normalises them to tuples so that
    # `(0, 24, 29) != [0, 24, 29]` does not make the same edit look different
    # across container types. This test pins that normalisation.
    import json
    import zipfile

    # Re-copy the source: test 3 deleted it to check the "gone" path.
    copy2 = work / "src2.e4b"
    shutil.copy(src, copy2)
    st = copy2.stat()
    old_style = work / "old_style.vslproj"
    manifest = {
        "format": "vinsamlib-project",
        "version": 2,
        "new_bank": {
            "name": "OldBank",
            "format": "E4B",
            "items": [
                {
                    "bank": {
                        "format": "E4B",
                        "kind": "reference",
                        "path": str(copy2),
                        "stamp": {"size": st.st_size, "mtime": st.st_mtime},
                    },
                    "preset": {"index": 0},
                    "name": "P0",
                    "zone_placement": {"kick": [0, 24, 29]},
                    "voice_velocity": {"snare": [1, 64, 127]},
                }
            ],
            "sample_renames": {},
            "zone_placement": {"hat": [2, 30, 40]},
            "voice_velocity": {"tom": [3, 20, 80]},
        },
        "pending": [],
        "partition_breaks": [],
    }
    with zipfile.ZipFile(str(old_style), "w") as zf:
        zf.writestr("project.json", json.dumps(manifest))
    rep5 = project.load(str(old_style))
    if rep5.problems:
        failures.append(f"old-style project reported problems: {rep5.problems}")
    if len(rep5.banks) != 1:
        failures.append(
            f"old-style project restored {len(rep5.banks)} banks, expected 1"
        )
    else:
        edits = rep5.banks[0][3]
        placement = edits.get("placement") or {}
        velocity = edits.get("velocity") or {}
        if placement.get("kick") != (0, 24, 29):
            failures.append(
                f"old-style placement 'kick' came back as {placement.get('kick')!r}, "
                f"expected (0, 24, 29) -- list-to-tuple normalisation failed"
            )
        if velocity.get("snare") != (1, 64, 127):
            failures.append(
                f"old-style velocity 'snare' came back as {velocity.get('snare')!r}, "
                f"expected (1, 64, 127) -- list-to-tuple normalisation failed"
            )
        # Bank-wide maps from v1 files are stored in the LoadReport and
        # applied to every item by MainWindow._restore_project
        if rep5.zone_placement.get("hat") != (2, 30, 40):
            failures.append(
                f"bank-wide zone_placement 'hat' came back as {rep5.zone_placement.get('hat')!r}, "
                f"expected (2, 30, 40)"
            )
        if rep5.voice_velocity.get("tom") != (3, 20, 80):
            failures.append(
                f"bank-wide voice_velocity 'tom' came back as {rep5.voice_velocity.get('tom')!r}, "
                f"expected (3, 20, 80)"
            )
    print(
        f"  old-style lists -> tuples: "
        f"{'OK' if not any('old-style' in f for f in failures) else 'FAIL'}"
    )

    if failures:
        print("\n" + "\n".join(f"  FAIL  {f}" for f in failures))
        return 1
    print("\nPROJECTS REFERENCE WHAT THEY CAN AND CARRY WHAT THEY MUST")
    return 0


if __name__ == "__main__":
    raise SystemExit(_status_run(main))
