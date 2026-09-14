#!/usr/bin/env python3
"""Render the release matrix as a grid, with what is claimed marked.

`matrix_coverage.py` answers "which cells has nobody claimed"; this shows
the same data laid out as the matrix it comes from, because a list of 40
ids does not show that an entire axis is untouched while another is
nearly done. The shape is the information.

    python3 tools/matrix_grid.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix_coverage import claimed_cells, declared_cells   # noqa: E402

GROUPS = [
    ("I", "INPUT — a format this can read"),
    ("P", "IMPORT PATH — how it reaches New Bank"),
    ("O", "OUTPUT — where it ends up"),
    ("H", "CONVERSION OPTION — per target it applies to"),
    ("J", "FORMAT DETAIL — limits no cross-product reaches"),
]


def main() -> int:
    cells = declared_cells()
    claims = claimed_cells()
    covered = {c for c in claims if c in cells}

    width = 62
    for prefix, title in GROUPS:
        ids = sorted((c for c in cells if re.match(rf"^{prefix}\d+$", c)),
                     key=lambda c: int(c[len(prefix):]))
        if not ids:
            continue
        done = sum(1 for c in ids if c in covered)
        print(f"\n{title}")
        print(f"{'-' * width}  {done}/{len(ids)}")
        for c in ids:
            mark = "[x]" if c in covered else "[ ]"
            desc = re.sub(r"[`*]", "", cells[c])[:width - 10]
            by = ""
            if c in covered:
                names = [n.replace("manual_", "").replace(".py", "")
                         for n in claims[c]]
                by = "  " + ", ".join(names[:2])
                if len(names) > 2:
                    by += f" +{len(names) - 2}"
            print(f"  {mark} {c:4s} {desc:<{width - 10}}{by}")

    print(f"\n{'=' * width}")
    print(f"  {len(covered)} of {len(cells)} cells claimed by "
          f"{len({f for fs in claims.values() for f in fs})} test(s)")
    print(f"  A claim means: this test would FAIL if that cell broke.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
