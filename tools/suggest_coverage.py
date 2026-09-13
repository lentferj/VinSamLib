#!/usr/bin/env python3
"""Propose matrix cells for a test, from what it actually touches.

Mapping 100 tests onto 76 cells by reading them is exactly the kind of
job that gets done by guessing at 2am. This does not guess: it reports
the EVIDENCE -- which extensions a test names, which image kinds it
builds, which ConversionOptions fields it sets -- and leaves the claim to
a person. A cell is only ever written into a test by hand.

Output is a proposal, never an edit.

    python3 tools/suggest_coverage.py                 # all unclaimed tests
    python3 tools/suggest_coverage.py manual_akai_*   # named ones
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

EXT_CELL = {
    ".e4b": "I1", ".krz": "I3", ".e3x": "I6", ".eiii": "I6",
    ".xpm": "I12", ".xty": "I13", ".xpj": "I14",
    ".sf2": "I17", ".sfz": "I18", ".exs": "I19",
    ".talsmpl": "I20", ".gig": "I21",
    ".p3": "I11", ".s3": "I11",
}
KIND_CELL = {
    "emu3_cd": "O3", "emu3_hd_emu": "O4", "emu3_hd_fat": "O5",
    "k2000_fat16": "O6", "k2000_iso9660": "O7", "fat12_floppy": "O8",
    "akai_hd": "O12", "akai_cd3000": "O13", "akai_floppy": "O14",
}
OPT_CELL = {
    "resample_profile": "H2", "no_bandpass": "H3",
    "resample_keep_gain": "H4", "max_sample_rate": "H5",
    "reduce_key_zones_pct": "H6", "reduce_velocity_layers_pct": "H7",
    "mono": "H8", "pan_law": "H9",
    "trim_start_db": "H10", "trim_start_keep_loops": "H11",
    "trim_tail_db": "H12", "trim_tail_keep_loops": "H13",
    "shrink_to_bytes": "H14", "shrink_by_pct": "H15",
    "krz_faithful_layers": "H16", "krz_drum_program": "H17",
}
CALL_CELL = {
    "append_banks": "O9", "append_volumes": "O9",
    "export_entry": "O10", "import_xpm": "P5",
    "import_sample_dir": "P6", "import_foreign": "P8",
}


def evidence(path: Path) -> tuple[set[str], list[str]]:
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set(), ["will not parse"]
    cells: set[str] = set()
    why: list[str] = []
    seen_claim = any(
        isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "COVERS" for t in n.targets)
        for n in tree.body)
    if seen_claim:
        return set(), ["already claims"]

    # STRONG evidence is an action: a call the app makes, an option set, an
    # image kind built. WEAK evidence is a filename extension appearing in a
    # string, which may be a real input or may be a temp file the test
    # happens to write. Only strong evidence justifies writing a claim
    # without reading the test, because a wrong claim does not merely miss
    # coverage -- it LOOKS like coverage, which is worse than a gap.
    strong: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            low = node.value.lower()
            for ext, cell in EXT_CELL.items():
                if ext in low:
                    cells.add(cell); why.append(f"names {ext}")
            for kind, cell in KIND_CELL.items():
                if kind == low or f"'{kind}'" in low:
                    cells.add(cell); strong.add(cell); why.append(f"builds {kind}")
        if isinstance(node, ast.keyword) and node.arg in OPT_CELL:
            cells.add(OPT_CELL[node.arg]); strong.add(OPT_CELL[node.arg])
            why.append(f"sets {node.arg}")
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name in CALL_CELL:
                cells.add(CALL_CELL[name]); strong.add(CALL_CELL[name])
                why.append(f"calls {name}")
    return strong, sorted(set(why))


def main(argv: list[str]) -> int:
    pats = argv[1:] or ["manual_*.py"]
    files: list[Path] = []
    for p in pats:
        files.extend(sorted(TESTS.glob(p if p.endswith(".py") else p + ".py")))
    proposed = 0
    for f in sorted(set(files)):
        cells, why = evidence(f)
        if why == ["already claims"] or not cells:
            continue
        proposed += 1
        print(f"{f.name}: COVERS = {sorted(cells)}   ({'; '.join(why[:4])})")
    print(f"\n{proposed} unclaimed test(s) have STRONG evidence. "
          f"Nothing was edited; extension-only hints are not reported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
