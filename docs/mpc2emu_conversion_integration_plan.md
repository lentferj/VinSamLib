# mpc2emu Conversion Integration Plan (Resample / Reduce options panel)

## 0. Confirming what's already in place (mpc2emu availability check)

Read `vinsamlib/config.py` and `vinsamlib/mpc2emu_bridge.py` in full. Confirmed:

- `Config.mpc2emu_path` (default via `_default_mpc2emu_path()`, sibling-checkout convention) and `Config.validate_mpc2emu_path()` (checks `writers/iso_builder.py` exists) already exist exactly as described — **this part of the ask is done**.
- `mpc2emu_bridge.install()` calls `validate_mpc2emu_path()` then does the one-time `sys.path.insert(0, ...)`, idempotent via a module-level `_installed` flag. `_Lazy` wrapper objects defer the actual `importlib.import_module` until first attribute access, so importing `mpc2emu_bridge` itself never requires mpc2emu to be present.

**What's still missing for a robust Settings-panel "is it available, and where" check:**

1. **No non-raising check exists.** `validate_mpc2emu_path()` only raises `FileNotFoundError`. A Settings dialog needs a boolean/reason tuple to show a live "✓ found" / "✗ not found: &lt;reason&gt;" without wrapping every keystroke in try/except in the UI layer. Add something like `Config.check_mpc2emu_path() -> tuple[bool, str]` that wraps the existing validator.
2. **No "features present" check, only "checkout present" check.** The existing marker (`writers/iso_builder.py`) proves *a* mpc2emu checkout exists, not that the specific modules this new feature needs are there. Since `processors/resampler.py` and `processors/zone_reducer.py` are core, any real checkout will have them, but an old/partial checkout during development could plausibly be missing one — worth a second, more specific check (`processors/resampler.py`, `processors/zone_reducer.py`, `parsers/e4b_parser.py` exist) so a Convert-Options dialog can say "resample/reduce unavailable" distinctly from "mpc2emu not found at all."
3. **No re-validation path after the user edits the setting mid-session.** `install()`'s `_installed` flag never resets, and even if it did, Python's module cache (`sys.modules`) would still hold whichever mpc2emu modules were already imported from the old path — there is no clean "switch mpc2emu checkouts without restarting" story. Recommend the Settings dialog simply says "Restart VinSamLib to apply" after a path change, rather than trying to implement hot-reload.
4. **No Settings dialog UI exists at all yet.** `vinsamlib/ui/` has no `settings_dialog.py`, and `MainWindow._build_menu()` only has File/View/Help (no Preferences/Settings entry). This needs to be built from scratch — small, self-contained (QLineEdit + Browse… + a status label + OK/Cancel), and is a light prerequisite so a broken mpc2emu path shows a friendly message rather than a traceback the first time someone opens the new Convert Options dialog.

## 1. convert.py's actual resample/reduce definitions (read directly, not guessed)

**Resample family** (`convert.py` argparse block, ~line 383-399; DSP in `mpc2emu/processors/resampler.py`):

- `--resample {emulator2,emax1}` — `PROFILES = {"emulator2": EMULATOR_II, "emax1": EMAX_I}` (`processors/resampler.py:152-155`). Each is a `VintageProfile` dataclass: `emulator2` = "EMU Emulator II (8-bit µ-law / 27.777 kHz)"; `emax1` = "EMU Emax I (12-bit / 27.5 kHz)". Signal chain: AA-filter → decimate → gain-stage → quantize (µ-law companded for EII, linear for Emax) → bandpass color → restore level (`resample_vintage()`, `resampler.py:354-491`).
- `--no-bandpass` (store_true) — skips step 5/6 (bandpass output coloring). Only meaningful once `--resample` is set.
- `--resample-keep-gain` (store_true) — keeps the gain-staged "hot" level instead of restoring the source's original peak level afterward. Only meaningful once `--resample` is set.
- `--max-sample-rate HZ` (int, default `-1`) — **this is NOT gated on `--resample` in the actual code** (convert.py:786-829). It's a separate pipeline stage that runs unconditionally after resample: `HZ > 0` blanket-downsamples every sample above HZ (uses `resample_to_rate`, a clean linear-interp resample, not vintage coloring); `HZ == -1` (default, unset) triggers a KRZ-only "headroom-aware" auto-downsample; `HZ == 0` disables entirely. The task brief asks for it to be shown as a dependent of Resample in the UI anyway — that's a legitimate UX grouping choice, but it's worth flagging that convert.py itself treats it as independent, so the wrapper function must apply it as its own step regardless of whether `--resample` was requested.

**Reduce family** (`convert.py:400-405`; logic in `mpc2emu/processors/zone_reducer.py`):

- `--reduce-key-zones PCT` (float 0-100, default 0.0) — removes PCT% of each *voice's* key-zone samples, redistributing survivors' key ranges to cover the gap (`thin_key_zones`).
- `--reduce-velocity-layers PCT` (float 0-100, default 0.0) — removes PCT% of each *preset's* velocity layers, same redistribution logic (`thin_velocity_layers`).
- Both go through `reduce_bank(bank, key_zone_pct, velocity_layer_pct)` (`zone_reducer.py:232-262`), which also prunes now-unreferenced samples afterward. The two axes are fully independent (0 = leave alone).
- Validated range check happens in `convert.py:595-599` (`sys.exit(1)` if outside 0-100) — the UI must do its own 0-100 clamp (e.g. via `QSlider`/`QSpinBox` range) since we won't be calling `main()`'s argparse validation.
- **`--auto-fit` / `--max-presets` are NOT meaningfully related** to these two flags for VinSamLib's purposes: they belong to `fit_oversized_presets()` and `split_into_banks()`, which are convert.py's own *batch, multi-bank-output* machinery for when a single preset doesn't fit one bank or a whole library needs splitting across many output banks. VinSamLib's own `banks/e4b.py::assemble()` / `banks/krz.py::assemble()` already do their own preset-count capping (`MAX_PRESETS`) and never call `split_into_banks`, so there is no "auto-fit" concept to wire up here — correctly out of scope.

## 2. The single most important technical question: is convert.py's logic importable?

**Yes — cleanly.** `convert.py`'s `main()` (lines 313-1030) is one big function, but it is *entirely* built out of already-importable, side-effect-scoped functions from `processors/`, `writers/`, and `parsers/`:

- `processors.resampler.resample_bank(bank, profile_name, bandpass, restore_level, workers)` and `processors.resampler.resample_to_rate(sample, dst_rate)` — pure functions/mutators on `models.common.Bank`/`SampleData`, no argparse, no stdin, no CLI coupling.
- `processors.zone_reducer.reduce_bank(bank, key_zone_pct, velocity_layer_pct)` — same.
- `parsers.e4b_parser.parse_e4b(path) -> Bank` and `writers.e4b_writer.write_e4b(bank, output_path)` — same.

None of this needs shelling out to `python convert.py ...` as a subprocess. A new `vinsamlib/build/convert.py` wrapper can import these directly, exactly like `build/images.py` already imports `iso_builder`/`hda_builder`/`fat12` via `mpc2emu_bridge`.

**One important gap:** the `--max-sample-rate`-only downsample path (KRZ headroom-aware branch, convert.py:806-829) is **inline code in `main()`, not a reusable function**. If the panel is to expose "limit max sample rate," the wrapper must re-implement that ~10-line loop itself (it's simple: iterate `bank.samples`, call the already-importable `resample_to_rate` on any sample above the threshold) rather than importing something that doesn't exist as a function.

**A second, more consequential gap — read `banks/e4b.py` / `banks/krz.py`'s `assemble()` docstrings carefully:**

- `banks/e4b.py::assemble()` (line 324) explicitly states: *"Each preset's original chunk bytes are copied verbatim... Going through `models.common.Bank` would silently degrade real commercial banks... [it] covers [only some parameters]."* `banks/krz.py::assemble()` (line 372) says the same for KRZ objects.
- This means **resampling/reducing cannot happen inside `assemble()`** — it operates on raw, format-specific chunk/object bytes and has zero DSP capability (no PCM decode, no per-sample rate). Confirms the brief's suspicion: **resample/reduce must be a separate pre/post-processing pass through mpc2emu's own `parse → Bank → process → write` pipeline**, not a parameter to `assemble()`.
- Consequence for fidelity: routing an *already-assembled* VinSamLib bank back through `parse_e4b → Bank → write_e4b` to apply resample/reduce re-introduces exactly the degradation `assemble()` was designed to avoid — any envelope/LFO/chorus parameter not modeled by `models.common.Bank` gets flattened to whatever `write_e4b` reconstructs. This is an unavoidable cost of using mpc2emu's DSP at all (you're deliberately rewriting PCM, so some loss of unrelated fidelity is a reasonable trade — but it must be disclosed to the user, e.g. a one-line warning in the dialog: *"Applying vintage resample/reduce re-encodes this bank through mpc2emu's own model; a few advanced parameters not covered by that model may reset to defaults."*)

**A second, format-scoping finding, from `mpc2emu/parsers/registry.py`:** `INPUT_EXTS`/`PARSERS` cover `.e4b .xpm .pgm .set .img .talsmpl .sfz .sf2 .exs .gig` — **there is no `.krz` input parser in mpc2emu at all** (KRZ is write-only in this project). This means the `parse → Bank → process → write` round trip described above is only possible for **E4B**-format pending/output. **The resample/reduce feature must be scoped to E4B only for this pass** — KRZ queues should have the Convert Options entry point disabled with a clear tooltip ("mpc2emu has no KRZ reader; vintage resample/reduce is E4B-only"), not silently no-op.

## 3. Where this lives in VinSamLib's architecture

New module: **`vinsamlib/build/convert.py`**, mirroring `build/images.py`'s shape and safety rules (no stdin, captured stdout via `contextlib.redirect_stdout`, exceptions turned into a project-specific `ConvertOpError`). It should expose one function:

```
apply_conversion(e4b_path: str, opts: ConversionOptions) -> str
```

Implementation sketch (no code, just the flow): `parsers.e4b_parser.parse_e4b(e4b_path) -> Bank`; if `opts.reduce_key_zones_pct or opts.reduce_velocity_layers_pct`: call `zone_reducer.reduce_bank(bank, ...)`; if `opts.resample_profile`: call `resampler.resample_bank(bank, opts.resample_profile, bandpass=not opts.no_bandpass, restore_level=not opts.keep_gain, workers=1-or-small-N)`; if `opts.max_sample_rate` set: replicate convert.py's small blanket-downsample loop calling `resampler.resample_to_rate`; finally `writers.e4b_writer.write_e4b(bank, new_tmp_path)`; return `new_tmp_path`. Never mutates `e4b_path` (same "never touch the original" discipline as `build/images.py`).

Add three new lazy entries to `mpc2emu_bridge.py`'s module list (currently missing): `resampler = _Lazy("processors.resampler")`, `zone_reducer = _Lazy("processors.zone_reducer")`, and reuse the already-present `e4b_parser`/`e4b_writer`. Run the whole `apply_conversion` call through the existing `ui/workers.Worker` pattern (it's synchronous/CPU-heavy — `resample_bank` already parallelizes internally via `ProcessPoolExecutor`, so the wrapper itself should run on a single `Worker` thread, not be further parallelized by VinSamLib).

**Integration point:** `vinsamlib/ui/pending_pane.py`'s `_assemble_all()` already does exactly the "write each pending entry's real bytes to its own temp E4B/KRZ file" step, right before `_on_build_assembled()` hands the paths to `ImagePane`. That is the natural, single insertion point: after `_assemble_all()` produces a temp `.E4B` path (and only when the queue's format is E4B), optionally run it through `build/convert.py::apply_conversion()` to produce a second, transformed temp file that replaces the first before `buildRequested.emit(paths, fmt)` fires.

Consequently, **recommend the Pending pane's "Build Image →" step**, not a per-preset New-Bank option and not a global Settings dialog: it operates on whole assembled bank files (which is what mpc2emu's pipeline actually processes), applies uniformly to everything about to become one or more real banks, and reuses existing plumbing almost unchanged. Concretely: add a small "Process before building…" affordance next to the existing `Build Image →` button — clicking it opens the Convert Options dialog (modal), stores the chosen `ConversionOptions` (or `None`) on the pane, and every subsequent `Build Image →` click runs `apply_conversion` per-entry (skipped when options are `None`, i.e. today's exact behavior is preserved by default). Disable/grey the "Process before building…" affordance whenever `self._format == "KRZ"`.

Known accepted limitation for v1: `bank_pane.py`'s live size meter (`_recompute`) is computed on the *unprocessed* assembly, so it won't reflect the final post-resample/reduce size. That's fine to leave undocumented-but-accepted for a first pass (mirrors how convert.py's own `--auto-fit` machinery already anticipates size changes happening *after* the fact); worth a one-line note in the dialog ("final bank size may differ after processing").

## 4. Widget-level design for conditional display

Two independent top-level toggles, each a `QGroupBox(checkable=True)` — this is the right widget because it gives the standard Qt "click the title checkbox to enable, everything inside auto-disables when off" behavior for free, without hand-wired `setEnabled` calls scattered across children. To get actual **hiding** (not just greying-out) as the brief asks for ("only appears once enabled"), wrap each group's real content in an inner plain `QWidget`/`QFormLayout` host and connect `toggled(bool)` to that host's `setVisible()` as well — the group box's title/checkbox row stays put, but the body only takes up layout space once checked. This collapse-on-toggle behavior is exactly how real DSP-plugin panels behave, and avoids a permanently tall dialog with a wall of greyed-out controls.

**Group A — "Vintage Resample" `QGroupBox(checkable=True, checked=False)`**
Governs (hidden until checked):
- `QComboBox` — profile picker, items = `PROFILES` keys shown as their `display_name` ("EMU Emulator II (8-bit µ-law / 27.777 kHz)", "EMU Emax I (12-bit / 27.5 kHz)"); maps to `--resample`.
- `QCheckBox` "Apply bandpass coloring" default **checked** (maps to `not --no-bandpass`).
- `QCheckBox` "Keep gain-staged (hot) level" default **unchecked** (maps to `--resample-keep-gain`).
- Nested `QCheckBox` "Limit maximum sample rate" (unchecked by default) governing its own `QSpinBox` (Hz, e.g. 4000–48000 step 1000, sensible default 24000) — maps to `--max-sample-rate`; sentinel `-1`/unset when the inner checkbox is off. Note in code comments that this maps to a flag convert.py treats as independent of `--resample`, grouped here for UX only (see §2).

**Group B — "Reduce Sample Count"** (non-checkable outer `QGroupBox`, always visible once the dialog is open — reduce is a distinct feature from resample, not a sub-option of it), containing two independent inner checkable groups (or two `QCheckBox` + `QSlider`/`QSpinBox` rows using the same collapse pattern):

- `QGroupBox(checkable=True)` "Reduce Key Zones" → governs a `QSlider`(0-100) + linked `QSpinBox` showing "%"; maps to `--reduce-key-zones`. Default when first checked: a non-zero starting value (e.g. 30%), since leaving it at 0 after enabling would be a silent no-op (`reduce_bank` early-returns when both pct's are ≤0).
- `QGroupBox(checkable=True)` "Reduce Velocity Layers" → same pattern, maps to `--reduce-velocity-layers`, independent default (e.g. 30%).

Governing/dependent relationships, summarized:

| Governor | Dependents |
|---|---|
| "Vintage Resample" checked | profile combo, bandpass checkbox, keep-gain checkbox, "limit max sample rate" checkbox |
| "Limit maximum sample rate" checked (nested inside Resample) | the Hz spinbox |
| "Reduce Key Zones" checked | its %-slider/spinbox |
| "Reduce Velocity Layers" checked | its %-slider/spinbox |

## 5. Data flow

`ConversionOptions` — a small frozen dataclass living in `build/convert.py` (mirrors how `Config` and other option-carrying objects in this codebase are plain dataclasses): `resample_profile: Optional[str]`, `no_bandpass: bool`, `resample_keep_gain: bool`, `max_sample_rate: Optional[int]`, `reduce_key_zones_pct: float`, `reduce_velocity_layers_pct: float`.

The Convert Options `QDialog`'s `exec()` produces either `None` (Cancel) or a populated `ConversionOptions` (OK) — pure data, no Qt objects leak out, consistent with the rest of the app's pane-boundary conventions (e.g. `pending_pane`'s `add_pending()` takes plain tuples/lists, not widgets).

Threading: it does **not** modify bytes before they reach `banks.e4b.assemble()` — that function must stay byte-verbatim, as established in §2. Instead, `apply_conversion()` operates on the *output* of `assemble()` (an already-valid, on-disk `.E4B` file, written to its own temp file exactly as `_assemble_all()` does today) via mpc2emu's own `parse_e4b → Bank → resample_bank/reduce_bank → write_e4b` round trip, producing a **new** temp file that replaces the pre-conversion one before it's handed to `ImagePane.receive_bank_files`. This is a full pre-processing pass on a whole (already-selected) bank file — never a partial, in-place byte patch — which is the only technically possible design given mpc2emu's model round-trip requirement.

## 6. Milestones

1. **Settings hardening (small, does the availability-check part of the ask).** Add `Config.check_mpc2emu_path() -> tuple[bool, str]` (non-raising wrapper around `validate_mpc2emu_path()`), plus an optional `Config.check_conversion_support()` that additionally checks `processors/resampler.py` and `processors/zone_reducer.py` exist. Add `ui/settings_dialog.py` (QLineEdit + Browse… + status label + OK/Cancel), wired from a new File ▸ Settings… menu entry in `main_window.py`. Note "restart to apply" in the dialog rather than attempting hot-reload of `mpc2emu_bridge`'s cached `sys.path`/`sys.modules` state.
2. **`build/convert.py` wrapper.** `ConversionOptions` dataclass + `apply_conversion(e4b_path, opts) -> str`, reusing `parsers.e4b_parser`/`writers.e4b_writer` (already lazy in `mpc2emu_bridge.py`) plus two newly-added lazy entries (`processors.resampler`, `processors.zone_reducer`). Include the small hand-rolled max-sample-rate downsample loop (no reusable function exists upstream for that case, see §2). Manual smoke test: round-trip a real E4B bank from the library through each option combination, confirm the output still opens cleanly via `banks/e4b.py`'s own reader (same self-consistency style already used for M2's 203/203 round-trip check).
3. **Convert Options dialog UI.** `ui/convert_options_dialog.py` implementing the `QGroupBox(checkable)` + collapsing-inner-widget tree from §4; returns `ConversionOptions | None`.
4. **Wire into Pending pane.** "Process before building…" affordance beside `Build Image →` in `pending_pane.py`; stores chosen options; `_assemble_all` (or a new step right after it) calls `apply_conversion` per E4B temp file through the existing `ui.workers.Worker`; the affordance is disabled with a tooltip whenever the pending queue's format is KRZ (no `.krz` input parser in mpc2emu — see §2).
5. **End-to-end manual smoke test** against the real library: pick a real E4B bank/preset from Explorer, send to New Bank → Pending, enable e.g. Emax I resample + 30% key-zone reduce, Build Image, confirm the GUI stays responsive (work happens on the thread pool), and confirm the resulting image's bank re-opens correctly and is audibly/measurably different (smaller PCM, lower rate) via `banks/e4b.py`.
6. **Later growth path (explicitly out of scope now):** the same governing-`QGroupBox` pattern extends cleanly to `--trim-start`/`--trim-tail`, `--auto-loop`, and `--single-cycle` once this first pass is proven — each is already one flag governing a cluster of `--<flag>-*` sub-flags in convert.py's own argparse layout, so the widget-tree recipe from §4 repeats directly. The KRZ input-parser gap (§2) remains a hard blocker for offering *any* of mpc2emu's conversion pipeline to KRZ queues until/unless mpc2emu itself grows a `.krz` reader — out of scope for VinSamLib, which never modifies mpc2emu.
7. **TODO, deferred: per-preset conversion options (not just per-bank).** Milestones 1–5 give each *pending bank* its own `ConversionOptions` (stored on that bank's entry dict in `pending_pane.py`, chosen via "Process before building…" for whichever row is selected) — every preset inside one bank still gets the same treatment, since `resampler.resample_bank()`/`zone_reducer.reduce_bank()` operate on mpc2emu's whole `Bank` uniformly (one profile, one reduction % for every sample/preset in it). True per-preset control isn't natively supported by those functions at all.

   A workable approach, reusing infrastructure that already exists rather than touching mpc2emu's model: for each preset that wants its own settings, first call `banks.e4b.assemble([(bank, preset)])` to produce a temporary *single-preset* E4B (this is exactly what `assemble()` already does when combining presets from different source banks in New Bank — nothing new needed there); run that single-preset E4B through `build/convert.py::apply_conversion()` with that preset's own `ConversionOptions`; re-parse the processed result with VinSamLib's own `banks.e4b.parse()` to get back a `(bank, preset)` pair in the same shape `assemble()` already expects; collect one such processed pair per originally-selected preset (untouched presets pass through with `convert_opts=None`, i.e. skip the temp-bank detour entirely); then call `banks.e4b.assemble()` **once more** over the full collected list to produce the final combined multi-preset bank. No new mpc2emu API needed — it's the existing multi-preset assemble path, just fed already-converted single-preset sources instead of raw ones. Costs: one extra assemble+parse round trip per distinctly-configured preset, and the UI would need a per-preset options affordance somewhere in New Bank or Pending's Contents list (not designed yet). Out of scope until per-bank granularity has been used for a while and per-preset is actually wanted.

### Critical files for implementation
- `mpc2emu/convert.py`
- `mpc2emu/processors/resampler.py`
- `mpc2emu/processors/zone_reducer.py`
- `mpc2emu/parsers/registry.py`
- `vinsamlib/mpc2emu_bridge.py`
- `vinsamlib/config.py`
- `vinsamlib/build/images.py`
- `vinsamlib/banks/e4b.py`
- `vinsamlib/ui/pending_pane.py`
- `vinsamlib/ui/workers.py`
