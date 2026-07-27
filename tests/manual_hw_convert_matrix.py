"""
Hardware-test bank image batch generator (E4B + KRZ).

Drives a REAL `MainWindow` instance (offscreen, PySide6) through the real GUI
code paths (BankPane -> PendingBanksPane -> ImagePane) to build 16 separate
disk images on local disk -- one image per mpc2emu conversion-option
combination -- so a human can load each one, one at a time, onto real E4XT
or K2000R hardware and confirm by ear/by eye that each option actually does
what it claims. This is an artifact-generator tool, not a pass/fail test,
though it still asserts basic sanity (each image builds, opens, and shows
exactly the expected one bank) -- same in-process-call approach as every
other manual_* script here (no X11 input automation available).

Two source presets:
  SRC_SIMPLE -- Dance Organ E4B (Rob Papen Techno Synth Construction Yard),
                all 4 presets, ~22 KB total. Simple/sustained tone, good for
                pure resample-character A/B (Group A, 6 combos; also reused
                unmodified by Group C, 4 combos).
  SRC_MULTI  -- Kirk Hunter Virtuoso Strings, preset "8VnEsHdMrcFat/SL"
                (78 voices, 45 key zones, 5 velocity bands, ~27.5 MB
                assembled). Needed to demonstrate reduce-key-zones/
                reduce-velocity-layers meaningfully (Group B, 6 combos).

Groups A and B (rows 01-12) each get a fresh, BLANK starter
(`images.create_image(kind, path, [], ...)`), so every deliverable contains
exactly the one bank under test, nothing else. Container: `emu3_hd_emu`
(`.hda`).

Group C (rows 13-16) targets KRZ/K2000R instead, on a `fat12_floppy` (1.44 MB
Gotek-style `.img`) deliverable -- SRC_SIMPLE only, since SRC_MULTI's ~27.5 MB
assembled size is far too big for a floppy. Floppies aren't appendable
(`images.APPENDABLE_KINDS` excludes `fat12_floppy`), so each combo is proven
twice: first built into a throwaway, blank `k2000_fat16` scratch `.hda` (this
exercises the exact same real `ImagePane` append/validate path as every other
combo here, confirming the real KRZ `PRAM` magic bytes are accepted), then
the actual floppy deliverable is built directly from the recorded temp
`.krz` path that step produced (`PendingBanksPane.buildRequested` is
recorded for this). The scratch `.hda` is deleted afterward -- it's not a
deliverable. A conservative 1.4 MB size guard skips the floppy build (and
notes it in the manifest) if a combo's assembled `.krz` would never fit --
not expected to trigger for these 4 small-source combos, but cheap insurance.

Output: `~/temp/VinSamLib_Test/NN_slug.hda` (Groups A/B) or `.img` (Group C)
+ `00_MANIFEST.txt` (a plain ASCII table: filename, source, options applied,
and what to listen/look for on real hardware).

Critical guard: `assert win._image_pane._path is not None` right before
every `_build_image()` call -- an unset image path routes into the modal
`_new_image()` dialog and hangs forever headlessly.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Must be set before any PySide6 import so this script is self-contained.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox

from vinsamlib import mpc2emu_bridge
from vinsamlib.banks import e4b as vs_e4b
from vinsamlib.build import images
from vinsamlib.build.convert import ConversionOptions
from vinsamlib.config import Config
from vinsamlib.ui.main_window import MainWindow
from vinsamlib.vfs.detect import open_volume

from _qtest_shim import qwait

# ImagePane's append-to-image path and BankPane's duplicate-confirm path both
# go through a real modal QMessageBox -- fine for a human, fatal (hangs
# forever) with no one to click it in this offscreen harness. Same stub
# manual_ui_smoke_convert.py and manual_ui_smoke_pending.py use for the same
# reason.
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)

SRC_SIMPLE = (Path.home() / "Dokumente/SYNTHS/E4XT/E4Bs/"
              "Rob.Papen-Techno.Synth.Construction.Yard.E4/Techno Synths RP/"
              "B.007-Dance Organ   RP.e4b")
SRC_MULTI = (Path.home() / "Dokumente/SYNTHS/E4XT/E4Bs/"
             "Kirk.Hunter.Virtuoso.Series.Strings1.E4/KH Violins/"
             "B.003-2_8Violins128MB.e4b")

DEFAULT_OUT_DIR = Path.home() / "temp" / "VinSamLib_Test"

GROUP_SIZE_MB = {"A": 32, "B": 128}
GROUP_SOURCE_LABEL = {
    "A": "Dance Organ E4B (SRC_SIMPLE, 4 presets, ~22KB)",
    "B": "KH Strings 8VnEsHdMrcFat/SL (SRC_MULTI, 1 preset, ~27.5MB)",
}
GROUP_SOURCE_LABEL["C"] = GROUP_SOURCE_LABEL["A"]   # Group C reuses SRC_SIMPLE, KRZ target

# fat12_floppy capacity is 1.44MB; conservative threshold to skip a floppy
# build (rather than let mpc2emu's fat12 writer raise a confusing error) --
# not expected to trigger for Group C's tiny SRC_SIMPLE-derived combos.
FLOPPY_SIZE_GUARD_BYTES = 1_400_000


@dataclass
class Row:
    num: int
    slug: str
    bank_name: str          # exact name expected on the device
    group: str               # "A" | "B"
    opts: ConversionOptions
    listen_for: str          # what to check by ear/eye on real E4XT hardware


ROWS: list[Row] = [
    # -- Group A: SRC_SIMPLE (Dance Organ, all 4 presets) --------------------
    Row(1, "baseline_unconverted", "01 BASELINE", "A", ConversionOptions(),
        "Byte-verbatim control, no mpc2emu round-trip -- should sound identical to source."),
    Row(2, "resample_emulator2", "02 EMU2", "A",
        ConversionOptions(resample_profile="emulator2"),
        "Listen for gritty, lo-fi 8-bit Emulator II character -- darker than #01."),
    Row(3, "resample_emax1", "03 EMAX1", "A",
        ConversionOptions(resample_profile="emax1"),
        "Listen for Emax I character -- different vintage flavor than #02 (brighter/mid-forward)."),
    Row(4, "emulator2_no_bandpass", "04 EMU2 NOBP", "A",
        ConversionOptions(resample_profile="emulator2", no_bandpass=True),
        "Compare vs #02: bandpass filtering off should sound less filtered / more full-range."),
    Row(5, "emax1_keep_gain", "05 EMAX1 HOT", "A",
        ConversionOptions(resample_profile="emax1", resample_keep_gain=True),
        "Compare vs #03: output gain kept hot (no level restore) -- louder/hotter, maybe clipping."),
    Row(6, "max_rate_22050", "06 RATE22K", "A",
        ConversionOptions(max_sample_rate=22050),
        "Listen for aliasing / high-end loss from the 22.05kHz downsample vs #01."),

    # -- Group B: SRC_MULTI (KH Strings, preset 0) ---------------------------
    Row(7, "multizone_baseline", "07 MULTI BASE", "B", ConversionOptions(),
        "Byte-verbatim control (45 key zones, 5 velocity layers) -- no mpc2emu round-trip."),
    Row(8, "reduce_keyzones_30", "08 KZ30", "B",
        ConversionOptions(reduce_key_zones_pct=30.0),
        "NO-OP here (mpc2emu log: 'removed 0 key zone(s)') -- 1 zone/voice in this preset; "
        "expect it to sound identical to #07."),
    Row(9, "reduce_keyzones_60", "09 KZ60", "B",
        ConversionOptions(reduce_key_zones_pct=60.0),
        "Same no-op as #08 (higher pct, still 0 effect) -- expect identical to #07."),
    Row(10, "reduce_velocity_40", "10 VL40", "B",
        ConversionOptions(reduce_velocity_layers_pct=40.0),
        "Fewer distinct velocity-triggered timbres across a volume swell vs #07."),
    Row(11, "reduce_velocity_75", "11 VL75", "B",
        ConversionOptions(reduce_velocity_layers_pct=75.0),
        "Near-single fixed timbre regardless of velocity -- verify it still loads and plays."),
    Row(12, "combo_emax1_kz40_vl50", "12 COMBO", "B",
        ConversionOptions(resample_profile="emax1", reduce_key_zones_pct=40.0,
                          reduce_velocity_layers_pct=50.0),
        "Confirm it still loads and plays after 3 stacked transforms (resample + kz + vl reduce)."),

    # -- Group C: SRC_SIMPLE -> KRZ, fat12_floppy (K2000R Gotek) -------------
    Row(13, "krz_plain", "K13PLAIN", "C", ConversionOptions(target_format="KRZ"),
        "Reference for KRZ. Confirm the K2000R loads K13PLAIN.KRZ from the Gotek and all "
        "4 programs are present, in order. Sound should match the E4B baseline (#01)."),
    Row(14, "krz_emulator2", "K14EMU2", "C",
        ConversionOptions(target_format="KRZ", resample_profile="emulator2"),
        "KRZ + Emulator II. Same 8-bit µ-law grit as #02, now on the K2000R. Confirm "
        "keymaps/programs survived the E4B->KRZ format conversion."),
    Row(15, "krz_emax1", "K15EMAX1", "C",
        ConversionOptions(target_format="KRZ", resample_profile="emax1"),
        "KRZ + Emax I. Same 12-bit character as #03, on the K2000R."),
    Row(16, "krz_max_rate_22050", "K16RATE", "C",
        ConversionOptions(target_format="KRZ", max_sample_rate=22050),
        "KRZ bandwidth-limited to 22.05 kHz. Confirm the K2000R reports a sane sample rate."),
]


def _opts_label(opts: ConversionOptions) -> str:
    if opts.is_noop():
        return "none (byte-verbatim)"
    parts = []
    if opts.target_format != "E4B":
        parts.append(f'target_format="{opts.target_format}"')
    if opts.resample_profile:
        parts.append(f"resample={opts.resample_profile}")
    if opts.no_bandpass:
        parts.append("no_bandpass")
    if opts.resample_keep_gain:
        parts.append("keep_gain")
    if opts.max_sample_rate:
        parts.append(f"max_rate={opts.max_sample_rate}Hz")
    if opts.reduce_key_zones_pct:
        parts.append(f"reduce_key_zones={opts.reduce_key_zones_pct:g}%")
    if opts.reduce_velocity_layers_pct:
        parts.append(f"reduce_velocity={opts.reduce_velocity_layers_pct:g}%")
    return ", ".join(parts)


def wait_workers(pane) -> None:
    while pane._live_workers:
        qwait(50)
    qwait(150)


def _image_filename(row: Row) -> str:
    ext = "img" if row.group == "C" else "hda"
    return f"{row.num:02d}_{row.slug}.{ext}"


def stage_group(win: MainWindow, group: str) -> None:
    """Stage a source group's presets into New Bank via the real Explorer
    context-menu entry point (add_presets()), then send it to Pending once.
    Only entry[0] ever exists in the pending queue afterwards -- both panes
    are cleared first, so a leftover entry from the previous group can never
    collide with it."""
    win._pending_pane._clear()
    win._bank_pane._clear()

    if group in ("A", "C"):
        # Group C reuses SRC_SIMPLE unmodified -- only convert_opts.target_format
        # (set per-row in build_one_floppy) changes what bytes get written; the
        # staged E4B source itself is identical to Group A's.
        bank = vs_e4b.parse(str(SRC_SIMPLE))
        items = [(bank, preset, "E4B", preset.name or f"preset{preset.index}")
                 for preset in bank.presets]
    else:
        bank = vs_e4b.parse(str(SRC_MULTI))
        preset = bank.presets[0]
        items = [(bank, preset, "E4B", preset.name or "preset0")]

    ok = win._bank_pane.add_presets(items)
    assert ok, f"failed to stage group {group} source preset(s) into New Bank"

    # Let the debounced size-meter worker start and finish so _last_bytes is
    # set (_send_to_pending() refuses to fire without it). Group B's ~27.5MB
    # assemble() is slower than Group A's ~22KB, hence the worker-poll rather
    # than a flat guess.
    qwait(300)
    wait_workers(win._bank_pane)
    assert win._bank_pane._last_bytes is not None, (
        f"size meter never resolved for group {group}")

    win._bank_pane._send_to_pending()
    qwait(200)
    assert len(win._pending_pane._pending) == 1, (
        f"expected exactly one staged pending entry for group {group}, "
        f"found {len(win._pending_pane._pending)}")


def build_one(win: MainWindow, out_dir: Path, row: Row) -> dict:
    """Set this combo's options/name directly on the already-staged pending
    entry (bypassing the modal Convert Options dialog, same as this
    project's own existing convert smoke test), build a blank starter image,
    then drive Build Image -> and verify what actually landed."""
    entry = win._pending_pane._pending[0]
    entry["convert_opts"] = None if row.opts.is_noop() else row.opts
    entry["name"] = row.bank_name

    img_path = out_dir / _image_filename(row)
    if img_path.exists():
        img_path.unlink()

    images.create_image("emu3_hd_emu", str(img_path), [], volume_label="EMU_DISK",
                         size_mb=GROUP_SIZE_MB[row.group])

    win._image_pane._close_image()
    win._image_pane._open_image(str(img_path), known_kind="emu3_hd_emu")

    # CRITICAL guard: an unset image path routes _build_image()'s eventual
    # receive_bank_files() call into the modal _new_image() dialog, which
    # hangs forever with no one to click it in this offscreen harness.
    assert win._image_pane._path is not None, (
        f"row {row.num}: image path unset before _build_image() -- would hang")

    win._pending_pane._build_image()
    wait_workers(win._pending_pane)
    while win._image_pane._busy:
        qwait(50)
    qwait(150)

    entries = win._image_pane._entries
    assert len(entries) == 1, (
        f"row {row.num} ({row.slug}): expected exactly one bank on {img_path.name}, "
        f"found {[e.name for e in entries]}")
    entry_on_disk = entries[0]
    device_name = entry_on_disk.name.strip()
    expected = row.bank_name.strip()
    assert device_name.upper().startswith(expected.upper()[:16]), (
        f"row {row.num}: expected bank name starting with {expected!r}, got {device_name!r}")

    vol = open_volume(str(img_path))
    try:
        data = vol.read(entry_on_disk)
    finally:
        vol.close()
    assert data[:4] == b"FORM" and data[8:12] == b"E4B0", (
        f"row {row.num}: built bank on {img_path.name} has no FORM...E4B0 magic")
    reopened = vs_e4b.parse_bytes(data, f"{row.slug}.e4b")
    assert len(reopened.presets) >= 1, f"row {row.num}: reopened bank has no presets"

    print(f"  [{row.num:02d}] {row.slug}: built {img_path.name} "
          f"({len(data):,} bytes on-disk bank, device name {device_name!r})")

    return {"path": img_path, "device_name": device_name, "built_bytes": len(data)}


def build_one_floppy(win: MainWindow, out_dir: Path, row: Row, recorder: dict) -> dict:
    """Group C (KRZ, SRC_SIMPLE, fat12_floppy): same per-combo option/name
    set as build_one(), but floppies aren't appendable (images.APPENDABLE_KINDS
    excludes fat12_floppy), so the deliverable can't be built the same way.
    Instead:
      1. Build into a throwaway, blank k2000_fat16 scratch .hda -- this proves
         the real ImagePane append/validate path genuinely accepts and
         correctly sniffs the real KRZ PRAM magic bytes, through the exact
         same code every other combo in this script uses.
      2. Recover the actual temp .krz file _assemble_all()/apply_conversion()
         produced for this combo via `recorder` (populated by a connection
         on PendingBanksPane.buildRequested made once in main()), since
         that's the only way to get the real bytes onto a floppy at all.
      3. Size-guard: skip the floppy build (but still report it) if the
         recorded .krz exceeds FLOPPY_SIZE_GUARD_BYTES.
      4. Build the real 1.44MB fat12_floppy deliverable directly from that
         recorded path, open it, and verify exactly one bank with the
         expected name and real PRAM magic landed on it.
      5. Delete the scratch .hda -- it was only a validation step.
    """
    entry = win._pending_pane._pending[0]
    entry["convert_opts"] = row.opts   # never a no-op here: target_format="KRZ"
    entry["name"] = row.bank_name

    scratch_path = out_dir / f"_scratch_{row.num:02d}.hda"
    if scratch_path.exists():
        scratch_path.unlink()
    images.create_image("k2000_fat16", str(scratch_path), [], volume_label="K2000")

    win._image_pane._close_image()
    win._image_pane._open_image(str(scratch_path), known_kind="k2000_fat16")

    # Same critical guard as build_one(): an unset image path routes
    # _build_image()'s eventual receive_bank_files() call into the modal
    # _new_image() dialog, which hangs forever headlessly.
    assert win._image_pane._path is not None, (
        f"row {row.num}: scratch image path unset before _build_image() -- would hang")

    recorder.clear()
    win._pending_pane._build_image()
    wait_workers(win._pending_pane)
    while win._image_pane._busy:
        qwait(50)
    qwait(150)

    assert "paths" in recorder and recorder["paths"], (
        f"row {row.num}: buildRequested never fired / recorded no paths")
    krz_tmp_path = recorder["paths"][0]
    krz_size = Path(krz_tmp_path).stat().st_size
    expected = row.bank_name.strip()

    # Verify the scratch .hda genuinely took the real KRZ bytes through the
    # real ImagePane append/validate path (same assertions as build_one()'s
    # E4B check, just against KRZ's PRAM magic instead of FORM...E4B0).
    scratch_entries = win._image_pane._entries
    assert len(scratch_entries) == 1, (
        f"row {row.num} ({row.slug}): expected exactly one bank on scratch image, "
        f"found {[e.name for e in scratch_entries]}")
    scratch_entry = scratch_entries[0]
    scratch_device_name = scratch_entry.name.strip()
    assert scratch_device_name.upper().startswith(expected.upper()), (
        f"row {row.num}: expected scratch bank name starting with {expected!r}, "
        f"got {scratch_device_name!r}")
    vol = open_volume(str(scratch_path))
    try:
        scratch_data = vol.read(scratch_entry)
    finally:
        vol.close()
    assert scratch_data[:4] == b"PRAM", (
        f"row {row.num}: scratch bank on {scratch_path.name} has no PRAM (KRZ) magic")

    scratch_path.unlink()

    img_path = out_dir / _image_filename(row)
    if img_path.exists():
        img_path.unlink()

    if krz_size > FLOPPY_SIZE_GUARD_BYTES:
        msg = (f"SKIPPED (too large for a 1.44 MB floppy: {krz_size:,} bytes "
               f"> {FLOPPY_SIZE_GUARD_BYTES:,})")
        print(f"  [{row.num:02d}] {row.slug}: {msg}")
        return {"path": None, "device_name": None, "built_bytes": krz_size,
                "skipped": True, "skip_reason": msg}

    images.create_image("fat12_floppy", str(img_path), [krz_tmp_path],
                         volume_label="K2000", floppy_kind="1440")

    win._image_pane._close_image()
    win._image_pane._open_image(str(img_path), known_kind="fat12_floppy")

    entries = win._image_pane._entries
    assert len(entries) == 1, (
        f"row {row.num} ({row.slug}): expected exactly one bank on {img_path.name}, "
        f"found {[e.name for e in entries]}")
    entry_on_disk = entries[0]
    device_name = entry_on_disk.name.strip()
    assert device_name.upper().startswith(expected.upper()), (
        f"row {row.num}: expected bank name starting with {expected!r}, got {device_name!r}")

    vol = open_volume(str(img_path))
    try:
        data = vol.read(entry_on_disk)
    finally:
        vol.close()
    assert data[:4] == b"PRAM", (
        f"row {row.num}: built floppy {img_path.name} has no PRAM (KRZ) magic")

    print(f"  [{row.num:02d}] {row.slug}: built {img_path.name} "
          f"({len(data):,} bytes on-disk bank, device name {device_name!r})")

    return {"path": img_path, "device_name": device_name, "built_bytes": len(data),
            "skipped": False}


MANIFEST_NOTES = """\
Notes:
- Rows 01 and 07 are byte-verbatim (no mpc2emu round-trip -- is_noop() short-
  circuits) -- the true "no processing" reference for each source.
- Group C (rows 13-16) intentionally has no reduce-key-zones/reduce-velocity-
  layers combos: the only real multi-zone fixture in this matrix (the Kirk
  Hunter Strings source used for Group B) assembles to ~27.5 MB, far too big
  for a 1.44 MB floppy. Rows 08-12 (E4B) are the relevant reduce-feature
  evidence, independent of target format, since mpc2emu applies reduce/
  resample to its internal Bank model before either writer (E4B or KRZ) runs.
- Group C targets KRZ/K2000R instead of E4B/E4XT, on a fat12_floppy (1.44 MB
  Gotek-style .img) deliverable -- SRC_SIMPLE only. Floppies can't be
  appended to, so each combo is proven via a throwaway scratch k2000_fat16
  .hda first (real ImagePane append/validate path against real KRZ PRAM
  magic), then rebuilt as the actual floppy from the recorded temp .krz path.
"""


def write_manifest(out_dir: Path, rows: list[Row], skip_reasons: Optional[dict] = None) -> str:
    """Simple ASCII text table -- plain columns, readable in a terminal or a
    plain editor. One row per image (16 rows), a header row, and the image
    filename as its own column."""
    skip_reasons = skip_reasons or {}
    headers = ["#", "Filename", "Input", "Import Options", "What to look out for in testing"]
    data_rows = []
    for row in rows:
        filename = _image_filename(row)
        listen_for = row.listen_for
        if row.num in skip_reasons:
            listen_for = f"{skip_reasons[row.num]} -- {listen_for}"
        data_rows.append([
            f"{row.num:02d}",
            filename,
            GROUP_SOURCE_LABEL[row.group],
            _opts_label(row.opts),
            listen_for,
        ])
    widths = [max(len(h), *(len(r[i]) for r in data_rows)) for i, h in enumerate(headers)]

    def fmt_row(cells: list[str]) -> str:
        return " | ".join(c.ljust(w) for c, w in zip(cells, widths))

    sep = "-+-".join("-" * w for w in widths)
    lines = [MANIFEST_NOTES, fmt_row(headers), sep] + [fmt_row(r) for r in data_rows]
    text = "\n".join(lines) + "\n"
    (out_dir / "00_MANIFEST.txt").write_text(text, encoding="utf-8")
    return text


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                    help="directory to write the 16 .hda/.img files + manifest into")
    p.add_argument("--only", default=None,
                    help="comma-separated row numbers to (re)build, e.g. 3,8,12")
    p.add_argument("--groups", default="a,b,c",
                    help="comma-separated groups to run, e.g. a or a,b,c")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args(sys.argv[1:])
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    only = None
    if args.only:
        only = {int(x) for x in args.only.split(",") if x.strip()}
    groups = {g.strip().upper() for g in args.groups.split(",") if g.strip()}

    selected_rows = [r for r in ROWS if r.group in groups and (only is None or r.num in only)]
    if not selected_rows:
        print("Nothing selected to build -- check --only/--groups")
        return 1

    config = Config.load()
    mpc2emu_bridge.install(config)
    # Never touch the real library roots and NEVER call config.save() anywhere
    # in this script -- ad-hoc scripts have corrupted the user's real
    # ~/.config/vinsamlib/config.toml before.
    config.library_roots = []

    app = QApplication(sys.argv)
    win = MainWindow(config)
    win._pending_pane.statusMessage.connect(lambda m: print("  [pending status]", m))
    win._image_pane.statusMessage.connect(lambda m: print("  [image status]", m))
    win._bank_pane.statusMessage.connect(lambda m: print("  [bank status]", m))
    win.resize(1360, 800)
    win.show()
    qwait(300)

    # Group C needs the actual temp .krz path _assemble_all()/apply_conversion()
    # produced for each combo (to build the real floppy deliverable directly,
    # since floppies aren't appendable) -- recorded via this extra connection
    # on the same signal MainWindow already wires to ImagePane.receive_bank_files.
    # Harmless for Groups A/B: they never read `recorder`.
    recorder: dict = {}
    win._pending_pane.buildRequested.connect(
        lambda paths, fmt: recorder.update(paths=paths, fmt=fmt))

    results = []
    skip_reasons: dict[int, str] = {}
    for group in ("A", "B", "C"):
        group_rows = [r for r in selected_rows if r.group == group]
        if not group_rows:
            continue
        print(f"\n=== Staging group {group} source ({GROUP_SOURCE_LABEL[group]}) ===")
        stage_group(win, group)
        for row in group_rows:
            print(f"--- Building #{row.num:02d} {row.slug} ---")
            if group == "C":
                result = build_one_floppy(win, out_dir, row, recorder)
                if result.get("skipped"):
                    skip_reasons[row.num] = result["skip_reason"]
            else:
                result = build_one(win, out_dir, row)
            results.append(result)

    manifest_text = write_manifest(out_dir, ROWS, skip_reasons)
    print("\n" + manifest_text)
    print(f"Built {len(results)} image(s) in {out_dir}")
    print("Manifest written to", out_dir / "00_MANIFEST.txt")
    print("\nALL HW CONVERT MATRIX IMAGES BUILT")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
