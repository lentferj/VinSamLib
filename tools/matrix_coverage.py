#!/usr/bin/env python3
"""Which cells of the release matrix any test actually claims.

WHY A TOOL AND NOT A COLUMN IN THE DOCUMENT. The matrix already had a
"covered by" column, kept by hand, and on 2026-09-13 its whole AKAI
section still said the media were withheld pending hardware -- months
after they were written, and hours after a volume could be deleted from
an image. A release run following it would have skipped the rows that
mattered most. A hand-kept coverage claim decays into a hand-kept lie.

So: the document owns the CELLS, the tests own the CLAIMS, and this
reports the difference. A test declares what it covers with a module-level

    COVERS = ["I12", "P5", "O1", "H11"]

and nothing else has to be kept in step. An unknown id is an error, not a
warning -- a typo silently covering nothing is exactly the failure this
exists to catch.

Usage:
    python3 tools/matrix_coverage.py            # gaps only
    python3 tools/matrix_coverage.py --full     # every cell, claimed or not
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "RELEASE_TEST_MATRIX.md"
TESTS = ROOT / "tests"

#: A cell id is a letter-run plus digits at the start of a markdown table
#: row: "| I12 | MPC `.xpm` program |". Deliberately anchored to the table
#: column rather than found anywhere in the text, so prose mentioning I12
#: does not invent a cell.
_CELL_ROW = re.compile(r"^\|\s*\*?\*?([A-Z]{1,2}\d{1,3})\*?\*?\s*\|\s*(.+?)\s*\|")


def declared_cells() -> dict[str, str]:
    """{id: description} for every cell the document defines."""
    cells: dict[str, str] = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        m = _CELL_ROW.match(line)
        if m:
            cells.setdefault(m.group(1), m.group(2))
    return cells


def claimed_cells() -> dict[str, list[str]]:
    """{id: [test files claiming it]} from each test's COVERS list.

    Read with ast, not exec and not a regex: a test must not have to RUN to
    be counted, and a regex cannot tell a COVERS inside a string from one
    that is code -- which is the bug that cost an evening on 2026-09-13.
    """
    claims: dict[str, list[str]] = defaultdict(list)
    for f in sorted(TESTS.glob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:          # top level only
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "COVERS"
                       for t in node.targets):
                continue
            try:
                for cell in ast.literal_eval(node.value):
                    claims[str(cell)].append(f.name)
            except (ValueError, TypeError):
                claims["<unreadable>"].append(f.name)
    return claims


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    cells = declared_cells()
    if len(cells) < 20:
        print(f"FAIL: only {len(cells)} cell(s) parsed from {DOC.name}. The "
              f"table format has changed and this report would understate "
              f"the matrix rather than fail.")
        return 1

    claims = claimed_cells()
    unknown = {c: fs for c, fs in claims.items() if c not in cells}
    covered = {c for c in claims if c in cells}
    missing = sorted(set(cells) - covered,
                     key=lambda c: (re.sub(r"\d+", "", c), int(re.sub(r"\D", "", c))))

    print(f"matrix cells declared : {len(cells)}")
    print(f"cells with a claim    : {len(covered)}")
    print(f"cells with none       : {len(missing)}")
    print(f"tests declaring COVERS: "
          f"{len({f for fs in claims.values() for f in fs})} of "
          f"{len(list(TESTS.glob('*.py')))}")

    if unknown:
        print("\nUNKNOWN IDS -- a typo covers nothing and looks like coverage:")
        for c, fs in sorted(unknown.items()):
            print(f"   {c}: {', '.join(fs)}")

    if args.full:
        print("\nclaimed:")
        for c in sorted(covered, key=lambda x: (re.sub(r"\d+", "", x),
                                                int(re.sub(r"\D", "", x)))):
            print(f"   {c:5s} {cells[c][:56]:56s} {', '.join(claims[c])}")

    print("\nNO TEST CLAIMS THESE:")
    for c in missing:
        print(f"   {c:5s} {cells[c][:70]}")

    # Unknown ids are a failure; uncovered cells are a report, not an error --
    # the matrix is aspirational by design and a suite that refused to run
    # until it was complete would never run.
    return 1 if unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
