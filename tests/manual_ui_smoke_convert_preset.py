"""Manual smoke test for "Import via mpc2emu..." on a single already-native
preset (Explorer's right-click, alongside "Add ... to New Bank" -- see
explorer_pane.py's convertPresetRequested, main_window.py's
_convert_preset_via_mpc2emu()/_on_preset_converted(), build/convert.py's
convert_preset()). This generalizes the exact same "assemble -> mpc2emu
convert -> re-parse -> add_presets()" pipeline XPM import already uses,
just starting from a real preset already in the user's library instead of
a foreign XPM file.

Both E4B and KRZ are real mpc2emu *input* formats now (parsers.krz_parser,
added 2026-07-27, corpus-verified against 593 real .KRZ files) -- this
test exercises all four source/target combinations:
1. E4B -> E4B, with resample/reduce applied -> lands in New Bank, smaller
   than an unconverted assemble() of the same preset.
2. E4B -> KRZ (cross-format) -- confirms a real VinSamLib KrzFile/KrzObject.
3. KRZ -> E4B (cross-format, the new direction) -- confirms a real
   VinSamLib E4BFile/E4BPreset.
4. KRZ -> KRZ, with reduce applied ("same format, with options", not
   just "Add") -- confirms it round-trips through mpc2emu's own model
   and still produces a real, playable KrzFile.

Same in-process-call approach as every other smoke test here (no X11
input automation, and QMenu.exec() can't be stubbed -- see
manual_ui_smoke_xpm_import.py and its sibling tests): the context menu's
own gating condition is asserted directly instead of driving a real
right-click, and _convert_preset_via_mpc2emu() is invoked as if the menu
action had already been chosen.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QCoreApplication, QThreadPool
from PySide6.QtWidgets import QApplication

from vinsamlib import mpc2emu_bridge
from vinsamlib.banks import e4b as vs_e4b
from vinsamlib.banks import krz as vs_krz
from vinsamlib.build.convert import ConversionOptions, convert_preset
from vinsamlib.config import Config
from vinsamlib.ui.main_window import MainWindow
from vinsamlib.ui.models import TreeNode
from vinsamlib.ui.xpm_import_dialog import XpmImportDialog

E4B_DIR = Path.home() / "Dokumente/SYNTHS/E4XT/E4Bs/Rob.Papen-Techno.Synth.Construction.Yard.E4/Techno Synths RP"
KRZ_DIR = Path.home() / "Dokumente/SYNTHS/K2000R/Soundsets"


def _wait(win, timeout=60):
    deadline_worker_attr = "_preset_convert_worker"
    import time
    deadline = time.time() + timeout
    while getattr(win, deadline_worker_attr) is not None and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.1)
    assert getattr(win, deadline_worker_attr) is None, "conversion never completed"


def _preset_node(bank, preset_obj, fmt: str, label: str) -> TreeNode:
    bank_node = TreeNode("bank", "TestBank", None, None, format_label=fmt)
    return TreeNode("preset", label, bank_node, (bank, preset_obj))


def main():
    if not E4B_DIR.is_dir():
        print(f"SKIPPED: {E4B_DIR} not found on this machine")
        return

    config = Config.load()
    mpc2emu_bridge.install(config)
    config.library_roots = []

    app = QApplication(sys.argv)
    win = MainWindow(config)
    win.statusBar().messageChanged.connect(lambda m: print("  [status]", m) if m else None)

    e4b_path = next(E4B_DIR.glob("*.e4b"))
    bank = vs_e4b.parse(str(e4b_path))
    preset = bank.presets[0]
    print(f"using real preset: {preset.name.strip()!r} from {e4b_path.name}")

    e4b_node = _preset_node(bank, preset, "E4B", preset.name.strip())
    assert e4b_node.parent.format_label == "E4B"
    krz_bank_for_gate = None
    krz_preset_for_gate = None
    if KRZ_DIR.is_dir():
        # Not just "the first .krz file with a nonzero zone count" -- many
        # real K2000 discs are either entirely ROM-sample-based (e.g.
        # KPOWER.KRZ: every program has keymap entries, but the referenced
        # samples are all K2000 ROM, never present in the file) or, less
        # obviously, program/keymap-only banks whose OWN samples are
        # present as metadata objects but carry zero actual PCM bytes
        # (e.g. LFOSET2.KRZ). VinSamLib's own summarize_krz_program()
        # doesn't check PCM presence, only object presence, so either
        # case silently passes a "has zones" filter without any real,
        # convertible audio. mpc2emu's own krz_parser -- which DOES
        # extract and require real PCM -- is the reliable ground truth:
        # search with it first, then look up the matching program by
        # name through VinSamLib's own reader for the actual test.
        mpc2emu_bridge.install(config)
        from parsers import krz_parser as mpc_krz_parser
        for krz_path in sorted(KRZ_DIR.rglob("*")):
            if krz_path.suffix.lower() != ".krz":
                continue
            try:
                mpc_bank = mpc_krz_parser.parse_krz(str(krz_path))
            except Exception:
                continue
            if sum(len(s.data) for s in mpc_bank.samples) == 0:
                continue
            real_preset = next((p for p in mpc_bank.presets
                                 if p.voices and any(v.zones for v in p.voices)), None)
            if real_preset is None:
                continue
            candidate_bank = vs_krz.parse(str(krz_path))
            match = next((prog for prog in candidate_bank.programs.values()
                          if prog.name.strip() == real_preset.name.strip()), None)
            if match is None:
                continue
            # Validate with a real round trip, not just "has PCM" -- a
            # preset needing writers.krz_writer's octave-slice-stack
            # "coverage remap" rebuild can crash on write (a real mpc2emu
            # bug, see mpc2emu/TODO.md's krz_writer entry, 2026-07-27);
            # skip to the next candidate rather than fail this whole test
            # on a bug outside VinSamLib's own code.
            try:
                convert_preset(candidate_bank, match, ConversionOptions(
                    target_format="KRZ", resample_profile="emax1"))
            except Exception as ex:
                print(f"  (skipping {krz_path.name!r} {match.name.strip()!r} -- "
                      f"doesn't survive a real round trip: {str(ex).splitlines()[0]})")
                continue
            krz_bank_for_gate = candidate_bank
            krz_preset_for_gate = match
            break

    # -- regression: a preset name containing "/" (a real, valid character
    # in vintage patch names, e.g. "CL EspHdFst/Sld") must not be used raw
    # as a temp filename stem -- `tmp_dir / f"{stem}.e4b"` would otherwise
    # silently treat the "/" as an extra, nonexistent directory level and
    # raise FileNotFoundError on write. See build/convert.py's _sanitize_stem().
    import dataclasses as _dc
    slash_preset = _dc.replace(preset, name="CL EspHdFst/Sld")
    slash_tmp = convert_preset(bank, slash_preset,
                                ConversionOptions(reduce_key_zones_pct=30.0))
    assert Path(slash_tmp).exists()
    print("preset name with '/' converted fine, temp file:", slash_tmp)

    # -- 1. E4B -> E4B with resample/reduce ------------------------------------
    baseline_bytes = vs_e4b.assemble([(bank, preset)])
    print("baseline (unconverted) single-preset size:", len(baseline_bytes))

    XpmImportDialog.get_import_options = staticmethod(
        lambda parent=None, initial=None, title="Import XPM", warning_text=None,
        locked_format=None: ConversionOptions(
            target_format="E4B", resample_profile="emax1", reduce_key_zones_pct=30.0))
    assert not win._bank_pane._items
    win._convert_preset_via_mpc2emu(e4b_node)
    _wait(win)
    items = win._bank_pane._items
    assert len(items) == 1, "converted preset should land as exactly one New Bank item"
    conv_bank, conv_preset, name = items[0]
    assert isinstance(conv_bank, vs_e4b.E4BFile) and isinstance(conv_preset, vs_e4b.E4BPreset)
    assert name == f"{preset.name.strip()} (mpc2emu)"
    converted_bytes = vs_e4b.assemble([(conv_bank, conv_preset)])
    print("converted (E4B->E4B) single-preset size:", len(converted_bytes))
    assert len(converted_bytes) < len(baseline_bytes), "reduce+resample should shrink the preset"

    # -- regression: converting the SAME preset again must not collide on
    # display name (content-based dedup deliberately gives each conversion
    # a fresh identity -- see _on_preset_converted()'s comment -- so all
    # copies get ADDED, and unique_name() must keep them distinguishable).
    win._convert_preset_via_mpc2emu(e4b_node)
    _wait(win)
    win._convert_preset_via_mpc2emu(e4b_node)
    _wait(win)
    names = [name for _b, _p, name in win._bank_pane._items]
    print("names after converting the same preset 3x:", names)
    assert names == [f"{preset.name.strip()} (mpc2emu)",
                      f"{preset.name.strip()} (mpc2emu) 2",
                      f"{preset.name.strip()} (mpc2emu) 3"], \
        "repeated conversions of the same preset must get distinct, numbered names"

    # -- 2. E4B -> KRZ (cross-format conversion) -------------------------------
    XpmImportDialog.get_import_options = staticmethod(
        lambda parent=None, initial=None, title="Import XPM", warning_text=None,
        locked_format=None: ConversionOptions(target_format="KRZ"))
    win._convert_preset_via_mpc2emu(e4b_node)
    _wait(win)
    items = win._bank_pane._items
    print("New Bank items after E4B->KRZ conversion:", len(items))
    # A KRZ result can't share New Bank with the earlier E4B one -- add_presets()
    # enforces one format per pane -- so this only proves conversion produced a
    # real KrzFile/KrzObject pair, checked directly instead.
    krz_tmp = convert_preset(bank, preset, ConversionOptions(target_format="KRZ"))
    krz_bank = vs_krz.parse(krz_tmp)
    assert isinstance(krz_bank, vs_krz.KrzFile)
    assert len(krz_bank.programs) == 1
    print("E4B->KRZ convert_preset() produced a real KrzFile with",
          len(krz_bank.programs), "program(s)")

    # -- 3. KRZ -> E4B (cross-format, the new direction) -----------------------
    if krz_preset_for_gate is not None:
        print(f"using real KRZ program: {krz_preset_for_gate.name.strip()!r} "
              f"from {krz_bank_for_gate.path}")
        krz_baseline = vs_krz.assemble([(krz_bank_for_gate, krz_preset_for_gate)])
        print("baseline (unconverted) KRZ single-program size:", len(krz_baseline))

        e4b_tmp = convert_preset(krz_bank_for_gate, krz_preset_for_gate,
                                  ConversionOptions(target_format="E4B"))
        converted_e4b = vs_e4b.parse(e4b_tmp)
        assert isinstance(converted_e4b, vs_e4b.E4BFile)
        assert len(converted_e4b.presets) == 1
        print("KRZ->E4B convert_preset() produced a real E4BFile with",
              len(converted_e4b.presets), "preset(s)")

        # -- 4. KRZ -> KRZ, with reduce applied ("same format, with options") --
        krz_reduced_tmp = convert_preset(krz_bank_for_gate, krz_preset_for_gate,
                                          ConversionOptions(target_format="KRZ",
                                                             reduce_key_zones_pct=30.0))
        reduced_krz = vs_krz.parse(krz_reduced_tmp)
        assert isinstance(reduced_krz, vs_krz.KrzFile)
        assert len(reduced_krz.programs) == 1
        print("KRZ->KRZ (reduced) convert_preset() produced a real KrzFile with",
              len(reduced_krz.programs), "program(s)")

        # -- Explorer context-menu path end to end: right-click a KRZ preset,
        # confirm "Import via mpc2emu..." works and defaults to same-format --
        krz_node = _preset_node(krz_bank_for_gate, krz_preset_for_gate, "KRZ",
                                 krz_preset_for_gate.name.strip())
        seen_initial = {}

        def _capture_initial(parent=None, initial=None, title="Import XPM", warning_text=None,
                              locked_format=None):
            seen_initial["target_format"] = initial.target_format if initial else None
            seen_initial["locked_format"] = locked_format
            return ConversionOptions(target_format="KRZ", reduce_velocity_layers_pct=30.0)

        XpmImportDialog.get_import_options = staticmethod(_capture_initial)
        win._bank_pane._clear()
        win._convert_preset_via_mpc2emu(krz_node)
        _wait(win)
        assert seen_initial["target_format"] == "KRZ", (
            "the dialog should default its target-format picker to the "
            "preset's OWN source format (KRZ here), not always E4B")
        assert seen_initial["locked_format"] is None, (
            "New Bank was just cleared -- no format lock yet, so the "
            "picker should still be a live, editable choice")
        items = win._bank_pane._items
        assert len(items) == 1
        conv_bank, conv_preset, name = items[0]
        assert isinstance(conv_bank, vs_krz.KrzFile) and isinstance(conv_preset, vs_krz.KrzObject)

        # -- regression: with New Bank now locked to KRZ (the item just
        # added), the picker must be forced to KRZ and disabled, no matter
        # what format was requested as a default -- picking E4B here would
        # only get rejected by add_presets() after a real conversion ran.
        seen_locked = {}

        def _capture_locked(parent=None, initial=None, title="Import XPM",
                             warning_text=None, locked_format=None):
            seen_locked["value"] = locked_format
            return ConversionOptions(target_format=locked_format or "E4B")

        XpmImportDialog.get_import_options = staticmethod(_capture_locked)
        win._convert_preset_via_mpc2emu(krz_node)
        _wait(win)
        assert seen_locked["value"] == "KRZ", (
            "New Bank already holds a KRZ preset -- the dialog must be "
            "told to lock the picker to KRZ, not offer E4B as a live choice")
        assert name == f"{krz_preset_for_gate.name.strip()} (mpc2emu)"
        print("Explorer 'Import via mpc2emu...' on a KRZ preset -> KRZ New Bank item:", name)
    else:
        print("(skipped KRZ-source checks: no .krz files found under", KRZ_DIR, ")")

    print("\nALL CONVERT-PRESET SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
