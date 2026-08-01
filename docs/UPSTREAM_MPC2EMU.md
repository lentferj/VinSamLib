# Upstream delta marker — mpc2emu

VinSamLib **wraps** mpc2emu and never edits it: every format-writing and
DSP capability here is imported in-process through `mpc2emu_bridge.py`
from whatever checkout the user configured. That has one consequence
worth a file of its own: most upstream work reaches VinSamLib's users
*for free* the moment they pull mpc2emu — but the part that doesn't
(new options with no control in the GUI, new warnings nothing surfaces,
findings that belong in this project's own README) reaches them **only**
when someone deliberately looks.

This file is that marker: it records how far the last look got, so the
next one is a diff rather than a re-read. Same idea as mpc2emu's own
practice of stamping each ConvertWithMoss cross-reference with the PR,
release or commit it was made against.

## The marker

```
Last reviewed mpc2emu commit:  d045323  2026-08-01  docs: validation results for the per-note voice budget
```

To start the next round:

```sh
git -C <mpc2emu checkout> log --oneline d045323..HEAD
```

(`<mpc2emu checkout>` is whatever **File → Settings… → mpc2emu checkout**
points at.) Advance the line above only after the whole range has been
looked at — a partial pass with an advanced marker is worse than no
marker, because the skipped commits become invisible.

## What a review round asks of each commit

1. **Does it change something VinSamLib re-exposes?** A new
   `convert.py` flag needs a control in Convert Options; a new warning
   needs a place in the GUI to appear. These are the commits that go
   silently missing otherwise: nothing breaks, the capability simply
   never shows up.
2. **Does it change a number this project's README states?** Measured
   hardware results get quoted here, and a result that was superseded
   upstream reads as a current claim here.
3. **Does it describe a bug in code VinSamLib has its own copy of?**
   VinSamLib's `banks/` readers and `vfs/` filesystems have no upstream
   equivalent, so an upstream fix in a similar area may or may not apply
   — check, and record the answer either way, so the next round doesn't
   re-derive it.
4. **Everything else is docs, and needs nothing.** Say so explicitly
   rather than leaving it unmentioned.

## Review log

Newest first. "No action" entries are kept deliberately: knowing that
something was checked and found irrelevant is worth as much as knowing
what was adopted.

### 2026-08-01 — reviewed `82b419b..d045323` (7 commits)

- **`6b12209` per-note voice budget → ADOPTED.** Upstream taught its
  CLI that a stereo sample costs two voices and that the ceiling is ~32
  per *note*; VinSamLib never calls `split_into_banks`, so nothing here
  would have surfaced it. Now `build/convert.py`'s `polyphony_risk()`
  calls upstream's own `peak_note_voices()` and reads its limit table,
  and all four mpc2emu-mediated paths warn (XPM import, sample-folder
  import, "Import via mpc2emu", Pending-pane per-bank conversion).
- **`82b419b` pan law verified on hardware → ADOPTED into the README.**
  The measurement, not code: worst deviation 0.47 dB against 4.32 dB
  uncompensated. The README described the *problem* as measured and the
  *fix* as unverified, which undersold a result we already had.
- **`f81a25e` (item 1) normalised-knob cutoff sources → APPLIES, no
  local fix.** `xpm_parser` assigns the MPC's own 0–1 knob into a field
  that contractually means a position on the 57 Hz–20 kHz curve, so
  MPC-sourced filter cutoffs shifted when the writer started
  interpreting that position precisely. VinSamLib's XPM import inherits
  this exactly. The fix is a measurement of the MPC's knob→Hz curve,
  upstream, in code this project does not own (mpc2emu's MPC 3.x
  checklist item A3). Nothing to do here but wait for it.
- **Corpus scan counting sampler OS files as banks (upstream's own open
  item) → CHECKED, does not apply.** mpc2emu finds banks by searching
  raw image bytes, which an `E3 Main Code` OS dump can fool. VinSamLib
  does not: `vfs/emu3.py` reads the real EMU3 directory and already
  knows `EMU3_FTYPE_SYS = 0x80`, treating only `STD`/`UPD` entries as
  banks.
- **`09f0655`, `f81a25e` (item 2), `4ab9584`, `48830f8`, `c8e08a5`,
  `d045323` → no action.** TODO indexing, the cutoff design-frequency
  question settled with no code change, the eosed parameter-spec
  cross-reference, and the voice-budget writeups whose *code* is the
  adoption above.

Range note: the previous round (`a5d4685`) demonstrably read through
`82b419b`, so this round restarted there rather than at that round's own
timestamp — four upstream docs commits landed in the same half hour and
it was no longer provable which side of the pass they fell on. That
ambiguity is the reason this file exists.

### 2026-08-01 (earlier) — mpc2emu's previous 24 h, 35 commits

Retro-recorded; predates this marker. Adopted `--trim-start` /
`--trim-tail` and `--pan-law` into Convert Options (`a5d4685`) and
followed upstream's naming scrub (`5c0adf4`). Most of the E4XT
recalibration round — cutoff table, linear zone-gain refit, pan
saturation, deterministic `bank_splitter`, MPC 3 XPM metadata — needed
**nothing**, because VinSamLib imports all of it in-process. Checked
specifically then: VinSamLib's own `banks/` readers are byte-level and
decode no cutoff/gain/pan, so there is no second copy of a hardware law
here that could drift. Worth re-checking only if those readers ever
start interpreting parameters.
