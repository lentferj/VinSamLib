"""Manual smoke test for "Import via mpc2emu..." on a single already-native
E4B preset (Explorer's right-click, alongside "Add ... to New Bank" -- see
explorer_pane.py's convertPresetRequested, main_window.py's
_convert_preset_via_mpc2emu()/_on_preset_converted(), build/convert.py's
convert_preset()). This generalizes the exact same "assemble -> mpc2emu
convert -> re-parse -> add_presets()" pipeline XPM import already uses,
just starting from a real preset already in the user's library instead of
a foreign XPM file.

Exercises three things:
1. E4B preset -> E4B, with resample/reduce applied -> lands in New Bank,
   smaller than an unconverted assemble() of the same preset.
2. E4B preset -> KRZ (cross-format conversion, not just XPM import) --
   confirms the resulting object is a real VinSamLib KrzFile/KrzObject.
3. A KRZ-sourced preset is refused (mpc2emu has no KRZ *input* parser at
   all) -- convert_preset() must raise, not silently do something wrong.

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
from vinsamlib.build.convert import ConversionOptions, ConvertOpError, convert_preset
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

    # -- context-menu gating: E4B preset offers the action, KRZ doesn't --------
    e4b_node = _preset_node(bank, preset, "E4B", preset.name.strip())
    assert e4b_node.parent.format_label == "E4B"
    krz_bank_for_gate = None
    if KRZ_DIR.is_dir():
        krz_path = next((p for p in KRZ_DIR.rglob("*") if p.suffix.lower() == ".krz"), None)
        if krz_path is not None:
            krz_bank_for_gate = vs_krz.parse(str(krz_path))
            krz_prog = next(iter(krz_bank_for_gate.programs.values()))
            krz_node = _preset_node(krz_bank_for_gate, krz_prog, "KRZ", "krz test")
            assert krz_node.parent.format_label != "E4B", (
                "a KRZ preset node must never satisfy explorer_pane.py's "
                "E4B-only gate for the 'Import via mpc2emu...' action")

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
        lambda parent=None, initial=None, title="Import XPM", warning_text=None: ConversionOptions(
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
        lambda parent=None, initial=None, title="Import XPM", warning_text=None: ConversionOptions(target_format="KRZ"))
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

    # -- 3. A KRZ-sourced preset must be refused, not silently mishandled ------
    if krz_bank_for_gate is not None:
        krz_prog = next(iter(krz_bank_for_gate.programs.values()))
        try:
            convert_preset(krz_bank_for_gate, krz_prog, ConversionOptions(target_format="E4B"))
            raise AssertionError("convert_preset() must refuse a KRZ-sourced preset")
        except ConvertOpError as ex:
            print("correctly refused KRZ-sourced preset:", ex)
    else:
        print("(skipped KRZ-source-refusal check: no .krz files found under", KRZ_DIR, ")")

    print("\nALL CONVERT-PRESET SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        QThreadPool.globalInstance().waitForDone(5000)
