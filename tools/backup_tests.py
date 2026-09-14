#!/usr/bin/env python3
"""Snapshot the untracked tests/ directory.

`tests/` is deliberately not in git (Jan, 2026-09-14), so nothing else
can bring a file back. That is fine for the tests themselves, which are
working material — and it is not fine for the moment a bulk edit goes
wrong across 88 files, which happened once already: k2kremote's manual
copy before converting every test tail was the only reason that pass was
safe to attempt.

A discipline that depends on remembering is not a net. This makes it a
command:

    python3 tools/backup_tests.py            # snapshot now
    python3 tools/backup_tests.py --list     # what exists
    python3 tools/backup_tests.py --restore <name>

Snapshots live OUTSIDE the repository, because a backup inside the thing
being edited is not a backup. They are plain zip files: no tooling of
ours is needed to read one, which matters most in the case where our
tooling is what went wrong.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

#: Outside the repo by default. Overridable so this is not another literal
#: home directory -- the fault the suite spent a night removing.
STORE = Path(os.environ.get("VINSAMLIB_TEST_BACKUPS",
                            str(Path.home() / ".vinsamlib-test-backups")))

#: Keep this many, newest first. Enough to cover "the edit two passes ago
#: was the good one", which is the case a single backup does not.
KEEP = 20

#: SOURCE ONLY BY DEFAULT, and the reasoning is what makes this usable.
#: tests/ is 37 MB, of which 28 MB is three Akai disc images and a pile of
#: .S3 samples. Twenty snapshots of that is half a gigabyte, and a backup
#: too expensive to take routinely is a backup nobody takes.
#:
#: What a bulk edit breaks is the PYTHON -- 88 tails converted at once is
#: the case this exists for, and that is about a megabyte. The binaries are
#: inputs: they do not change when a test is edited, and losing one is a
#: different accident needing a different answer. --with-data covers that.
SOURCE_SUFFIXES = {".py", ".txt", ".md", ".json", ".toml", ".cfg"}


def _snapshots() -> list[Path]:
    return sorted(STORE.glob("tests-*.zip"), reverse=True)


def take(with_data: bool = False) -> Path:
    if not TESTS.is_dir():
        raise SystemExit(f"no tests/ directory at {TESTS}")
    every = [p for p in TESTS.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts]
    files = every if with_data else [
        p for p in every if p.suffix.lower() in SOURCE_SUFFIXES]
    skipped = len(every) - len(files)
    if not files:
        raise SystemExit(f"{TESTS} holds no files; refusing to write an "
                         f"empty snapshot over the history of real ones")
    STORE.mkdir(parents=True, exist_ok=True)
    # A SECOND IS NOT UNIQUE ENOUGH, and the failure is the worst kind a
    # backup tool can have: two snapshots taken in the same second shared a
    # name and the second silently destroyed the first. Found within a
    # minute of writing this, by taking two -- which is exactly what anyone
    # does when they realise the first one left something out.
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    out = STORE / f"tests-{stamp}.zip"
    n = 2
    while out.exists():
        out = STORE / f"tests-{stamp}-{n}.zip"
        n += 1
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, p.relative_to(TESTS))
    for old in _snapshots()[KEEP:]:
        old.unlink()
    mb = out.stat().st_size / 1048576
    note = "" if not skipped else (
        f"; {skipped} data file(s) left out -- use --with-data for those")
    print(f"{out}  ({len(files)} file(s), {mb:.1f} MB{note})")
    return out


def restore(name: str) -> None:
    src = STORE / name if not name.endswith(".zip") else STORE / name
    if not src.exists():
        raise SystemExit(f"no snapshot {src}")
    # The CURRENT state is itself worth keeping: restoring is how you lose
    # the work you were in the middle of.
    print("snapshotting the current tests/ first:")
    take()
    with zipfile.ZipFile(src) as z:
        z.extractall(TESTS)
    print(f"restored {src.name} into {TESTS}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--restore", metavar="NAME")
    ap.add_argument("--with-data", action="store_true",
                    help="include the Akai images and samples too")
    args = ap.parse_args(argv[1:])
    if args.list:
        snaps = _snapshots()
        if not snaps:
            print(f"no snapshots in {STORE}")
        for s in snaps:
            with zipfile.ZipFile(s) as z:
                n = len(z.namelist())
            print(f"  {s.name}  {n} file(s)  "
                  f"{s.stat().st_size / 1048576:.1f} MB")
        return 0
    if args.restore:
        restore(args.restore)
        return 0
    take(args.with_data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
