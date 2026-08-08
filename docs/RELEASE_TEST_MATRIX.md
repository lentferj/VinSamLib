# Release test matrix

**Before calling a release ready, every combination of input × import path ×
output must have been run at least once.** Not "the builders are
content-agnostic so it must be fine" — that reasoning is exactly what let a
whole class of AKAI faults survive weeks of testing. A path nobody has
walked is a path nobody knows about.

The matrix is large. It is also mostly cheap: one call per cell, and the
existing manual tests already cover much of it. What this file is for is
knowing *which* cells are covered and by what, so the uncovered ones are a
decision rather than an oversight.

`tests/` is gitignored (local paths, real libraries), so the test names below
name files that exist only on a development machine.

**AKAI is not in this file.** Its inputs (hard-disk/CD3000/floppy images,
loose `.P3`/`.S3` folders), its refused output cells and its own end-to-end
row live on `feat/akai-s3000xl`, which is unmerged and gated on hardware.
They rejoin this matrix when that branch does — the version there is the one
to extend while it stays separate.

This file lived on that branch first, and that was a mistake worth recording:
a release-process document on an unmerged feature branch does not get
consulted during work on `master`, so three changes shipped in one day
without ever reaching it. It belongs wherever releases are cut.

---

## The three axes

**Input** — where content comes from:

| # | Input | Notes |
|---|---|---|
| I1 | E4B bank file, loose | |
| I2 | E4B inside an EMU3 CD/HD image | |
| I3 | KRZ bank file, loose | |
| I4 | KRZ inside FAT12/16/32 media | Gotek floppy, EOS FAT `.hda` |
| I5 | KRZ inside an ISO 9660 CD | |
| I6 | EIII/ESI bank file, loose | |
| I7 | EIII inside an EMU3 image | shares E4B's container |
| I12 | MPC `.xpm` program | keygroup and drum |
| I13 | MPC `.xty` track | |
| I14 | MPC `.xpj` project | 2.x and MPC 3 differ |
| I15 | Folder of WAVs | Sample Folder import |
| I16 | Hand-picked WAV files | multi-select import |

**Import path** — how it reaches New Bank:

| # | Path |
|---|---|
| P1 | Direct add / drag (native, no conversion) |
| P2 | "Import via mpc2emu…", same format, no options (a real no-op) |
| P3 | "Import via mpc2emu…", **cross-format** |
| P4 | …with resample / reduce / trim / mono / pan-law options |
| P5 | MPC import dialog (single program, and whole project) |
| P6 | Sample Folder import, incl. name scheme and placement overrides |
| P7 | Per-bank Convert Options in Pending for Image |

**Output** — where it ends up:

| # | Output |
|---|---|
| O1 | Save as… a bank file (`.e4b` / `.krz` / `.e3x`) |
| O3 | Image: `emu3_cd` |
| O4 | Image: `emu3_hd_emu` |
| O5 | Image: `emu3_hd_fat` (needs ≥512 MB to reach the FAT16 range) |
| O6 | Image: `k2000_fat16` |
| O7 | Image: `k2000_iso9660` (create-only) |
| O8 | Image: `fat12_floppy` (create-only, ~1.4 MB) |
| O9 | **Append** to an existing image, per appendable kind |
| O10 | Export a single entry out of an image |

---

## Matrix A — every input browses, summarizes and indexes

For each input: the tree lists it, the Detail pane summarizes it, the Samples
pane fills, and the scanner indexes it so search finds it and a hit resolves
back to a row that exists.

| Input | Browse | Detail/Samples | Search + resolve |
|---|---|---|---|
| I1–I2 E4B | ✅ | ✅ | ✅ |
| I3–I5 KRZ | ✅ | ✅ | ✅ |
| I6–I7 EIII | ✅ | ✅ | ✅ |
| I12–I14 MPC | ✅ `manual_ui_smoke_xpj_project` | ✅ | ✅ |
| I15–I16 WAV | n/a (import only) | n/a | n/a |

## Matrix B — source format × target format

Every cell is one "Import via mpc2emu…" run that must produce a bank whose
zones all resolve and whose sample count matches the source.

| source ↓ / target → | E4B | KRZ | EIII |
|---|---|---|---|
| **E4B** | ✅ P2/P4 | ✅ P3 | ✅ |
| **KRZ** | ✅ P3 | ✅ P2/P4 | ✅ |
| **EIII** | ✅ | ✅ | ✅ |
| **MPC** | ✅ | ✅ | ✅ |
| **WAV folder** | ✅ | ✅ | ✅ |

## Matrix C — target format × output destination

| target ↓ / output → | O1 file | O2 folder | O3 cd | O4 hd_emu | O5 hd_fat | O6 k2000 | O7 iso9660 | O8 floppy | O9 append |
|---|---|---|---|---|---|---|---|---|---|
| **E4B** | ✅ | n/a | ✅ | ✅ | ✅ | n/a | n/a | n/a | ✅ |
| **KRZ** | ✅ | n/a | n/a | n/a | n/a | ✅ | ✅ | ✅ ¹ | ✅ |
| **EIII** | ✅ | n/a | ✅ | ✅ | ✅ | n/a | n/a | n/a | ✅ |

¹ a floppy holds ~1.4 MB; a bank larger than that is a real constraint, not a
failure — the check is that it is *reported* as one.

**Crucially, each of these must be run with converted material too**, not
only with banks that were already native. Content that came through a
conversion is ordinary E4B/KRZ/EIII by then, so it *should* behave
identically — but that is a prediction, and the prediction is what needs
testing.

## Matrix D — things that alter content on the way through

Each of these has to be exercised on at least one source per target format,
because they rewrite audio or mapping rather than passing it through:

| Option | Covered by |
|---|---|
| resample profile (emulator2 / emax1) | `manual_matrix_a_d` (mono source; a stereo one is refused, see below) |
| max sample rate | `manual_ui_smoke_convert` |
| key-zone / velocity-layer reduction | `manual_ui_smoke_convert` |
| stereo → mono (mix / left / right) | `manual_ui_smoke_stereo` |
| pan law (E4B only) | `manual_ui_smoke_convert` |
| start / tail trim | `manual_ui_smoke_convert` |
| sample naming scheme, per-row names | `manual_ui_smoke_sample_names` ², `manual_names_e2e` |
| placement overrides (key range / root) | `manual_ui_smoke_stereo`, `manual_names_e2e` |

² lives on the `feat/sample-names-*` branches.

---

## Matrix E — a NAME has to survive to the written file

Added 2026-08-08, after three name-related changes shipped in one day and
none of them was checked past the point where the name was decided. Every
one of them was verified in memory, or in a dialog, and none end to end.

A name is not carried the way audio is. It is re-encoded at least twice on
the way out — once into the bank's own field, once into whatever container
holds the bank — and each encoder can be lossy on its own. So this axis is
per **target format**, and the check is always the same shape: write the
file, re-read it with VinSamLib's own byte-level reader (never the writer's
own model, which cannot see its own loss), and compare against what the user
was shown.

| what | E4B | KRZ | EIII |
|---|---|---|---|
| naming scheme `<base>-<key>` reaches the written file | `manual_names_e2e` | `manual_names_e2e` | `manual_names_e2e` |
| a per-row typed name beats the scheme, in the file | `manual_names_e2e` | `manual_names_e2e` | `manual_names_e2e` |
| the written bank is still VALID — re-parses, sample count and zones intact | `manual_names_e2e` | `manual_names_e2e` | `manual_names_e2e` |
| a name byte above 0x7E survives assemble() | `manual_e4b_name_bytes` | n/a ⁴ | n/a ⁴ |
| a name byte above 0x7E survives CONVERSION | ⛔ known loss ³ | ⛔ known loss ³ | ⛔ known loss ³ |
| a bank NAME with such a byte reaching an image | ⚠ untested | ⚠ untested | ⚠ untested |

### Renaming a sample INSIDE an assembled bank

Separate from the naming scheme above, which names samples on the way IN. This
renames what is already in a bank New Bank is staging, and each format stores
the name differently enough that they are genuinely three implementations.

| what | E4B | KRZ | EIII |
|---|---|---|---|
| where the name lives | fixed 16 B at `body[2:18]` **and** a TOC entry | null-terminated, padded to 2 B, **no slack** — the block regrows | fixed 16 B at `body[0:16]` |
| rename leaves audio byte-identical | `manual_bank_sample_rename` | `manual_krz_sample_rename` | `manual_eiii_sample_rename` |
| rename leaves preset/program bodies untouched | ✅ | ✅ | ✅ |
| an empty rename map is a byte-identical no-op | ✅ | ✅ | ✅ |
| every name length 1–16 round-trips | — | ✅ (block regrows) | — |
| the rename reaches the IMAGE, not just Save as… | `manual_rename_reaches_image` | ⚠ untested ⁵ | ⚠ untested ⁵ |
| key-suffixed bulk rename `<base>-<key>` | ✅ root from zone byte 14 | ✅ root from Soundfilehead byte 0 | ✅ root from zone byte 0 (+21) |
| name length capped to the format's 16 | ✅ fixed field | ✅ enforced ⁶ | ✅ fixed field |
| colliding `<base>-<key>` names disambiguated | ✅ | ✅ | ✅ |
| the dialog is reachable and its cells editable | ✅ | ✅ | ✅ ⁷ |

⁴ **This footnote used to say KRZ and EIII need no high-byte handling. For
KRZ that was wrong, and the way it was wrong is worth keeping.** The scan
behind it walked only LOOSE `.KRZ` files — 201 banks, zero high bytes — while
nearly all real K2000 content in this library lives inside disc images.
Rescanned including them: **2 237 banks, 69 676 objects, 157 carrying a byte
above 0x7E**, `0x7F` alone **4 036 times**, authored rather than incidental —
it separates the name from the channel marker, `BRA:Sect.3.01 <7F> L`, the
same role 0xA5 plays in E4B. `banks/krz.py` now reads latin-1; the retraction
is in mpc2emu's handoff, since I had told them to leave their writer ASCII on
the strength of it.

EIII still stands: that scan DID walk images — **1 019 banks / 30 935 sample
names** out of EMU3 containers — and its six hits are control-character noise
from deleted banks in free space.

**The lesson is the release-gate one:** a zero from a corpus whose shape was
never checked is not evidence of absence. Any ✅ here resting on "we measured
and found none" should name what the measurement walked.

⁶ KRZ has no fixed name field, so nothing truncates for it — a rename wrote a
34-character name into a structurally valid bank before this was added.
Sixteen is the longest authored name across 9 700 real objects and the width
of the K2000's own display.

⁷ Both name columns were read-only in the GUI and nobody noticed, because
every test set the text with `setText()`, which bypasses the view.
`NoSelection` blocks editing outright, and the placement dialog additionally
set `NoEditTriggers` — its per-row rename had never been usable since the day
it shipped. **A UI feature needs a test that drives the UI**; asserting on the
model underneath will pass against a control the user cannot reach.

⁵ `manual_rename_reaches_image` drives E4B through Pending → build → re-read.
The other two share that code path and their format layers are separately
tested, but the combination has not been run.

**KRZ is the only one that changes a block's LENGTH**, and the only one with a
self-check: `assemble()` re-reads any bank it resized and refuses one that
will not parse or comes back short. The trap it avoids is that `size` is
measured to the 2-byte-aligned end while `blocksize` is computed after a
4-byte pad, so `size += delta` is right and `blocksize += delta` is wrong
about half the time. `manual_krz_sample_rename` builds the wrong version
deliberately and requires it to fail — it breaks 4 of 4.

³ mpc2emu's `parsers/e4b_parser._decode_name` reads the 16-byte field as
ASCII with `errors='replace'`, so 0xA5 becomes U+FFFD before the Bank model
ever sees it and whatever is written afterwards carries the damage. Reported
in that project's handoff with the patch; deliberately NOT worked around
here. The row stays ⛔ rather than being deleted: a known loss that nobody
can point at becomes a surprise again.

**What the ⚠ rows mean.** Not "probably fine". They are cells nobody has
walked, listed so the next person decides rather than assumes — which is the
entire premise of this file.

## Matrix F — the written bank is still PLAYABLE, not merely present

A bank can arrive with every sample intact and still be wrong, because
nothing in it points at them. Sample count and file size both pass in that
case. So each write is also checked for how many zones the file REFERENCES
against how many the Bank held.

| target | zones survive OVERLAPPING key ranges in one voice | found by |
|---|---|---|
| E4B | ✅ 13/13 — the engine stacks overlapping zones | `manual_names_e2e` |
| KRZ | ✅ (via keymap entries) | `manual_names_e2e` |
| EIII | ⚠ **was 1 of 13** — one key maps to one zone; fixed upstream `4b0dcde` | `manual_names_e2e` |

The column heading is the correction: the axis is **overlap**, not how many
zones a voice happens to hold. Test with a source that gives no note
information — a folder whose filenames carry no note names is the easiest
way to make every zone span the whole keyboard.

**The EIII zone collapse, measured 2026-08-08.** A folder of 13 WAVs imported
to EIII wrote all 13 samples and exactly **one** zone: twelve samples that
could never sound. Established the way this project settles things — the
in-memory Bank had all 13, the written file had 1, and two independent
readers agreed on the file. Reported, and fixed upstream in `4b0dcde`.

**The cause is key OVERLAP, and the first diagnosis here was wrong.** This
entry originally read "the writer emits one zone per voice and drops the
rest", with a table contrasting `1 voice × 13 zones` (lost) against
`40 voices × 1 zone` (intact). That is not what happens, and the wrong
version is the intuitive one, so it is recorded rather than quietly replaced:

- An EIII preset maps each key to exactly **one** note zone. A real format
  limit, not a writer oversight — E4B and KRZ stack overlapping zones in one
  layer, EIII cannot.
- The writer walked **every** zone, resolved overlapping key ranges the way
  the sampler's own panel does (later wins), and dropped whatever was left
  holding no keys at all.
- Our 13 WAVs had filenames carrying **no note names**, so every zone spanned
  the whole keyboard and twelve lost every key. Upstream ran the identical
  reproduction with note-named files and got 13 zones back.

**So neither shape is safe or unsafe.** A one-voice-many-zones bank is not
the hazard, and a many-voices-one-zone bank is not the clearance. What
decides it is whether any two zones in one voice share a key — the 40-voice
case would have lost zones too, had any two overlapped. Any bank previously
cleared as "converted fine because it came from another sampler" is not
cleared by that reasoning.

**Why the check is EIII-only, and the E4B bug it exposed.** Run for E4B it
reported "140 of 141 zones" on ordinary conversions — not the empty-zone
artefact it resembles, since `_parse_zone_refs` counts index 0 too. mpc2emu
identified the cause 2026-08-08 and it is **ours**: E4B voices pack
back-to-back with no terminator, and only the LAST voice in a preset carries
a trailer, two bytes of `00 00`. A walk that expects a terminator per voice,
or reads those two bytes as the head of a zone entry, lands one short exactly
once — which is what "140 of 141" is. The count is arithmetic, not a scan:

```
n_zones(voice) = (be16(vpar[2:4]) - 284) / 22      # VOICE_FIXED, ZONE_ENTRY
next_voice     = this_voice_start + be16(vpar[2:4])
```

**Still unfixed here** — `banks/e4b.py`'s `_walk_voices` needs it, and the
zone check can widen to E4B once it lands. Recorded rather than left as an
open mystery, because a guard that cries about one zone in 141 on every
conversion trains a user to dismiss it.

VinSamLib **warns and does not refuse**: `_zone_loss_risk` re-reads every
written bank and reports through the same risk list polyphony findings use.
Not a correction — the bank is real and its audio is intact, and a user who
wants it should get it, but nobody should receive it unaware.

**This is exactly the gap `_verify_written` documented in its own docstring
and could not close.** It counts samples, and all 13 were there. Worth
remembering when reading a guard's stated limits: they are a list of the
faults it will let through, not a disclaimer.

---

## Gaps closed 2026-08-07 — `manual_matrix_gaps`

The five cells this file first listed as uncovered have been run. All pass;
none needed a code change. They are kept named here because a closed gap
that nobody can point at reopens quietly:

1. **EIII as a conversion source** → E4B, KRZ and EIII.
2. **MPC → KRZ and MPC → EIII**, each onward to an image and read back.
3. **EIII → `emu3_cd` and `emu3_hd_fat`**.
4. **KRZ → `fat12_floppy`**, and **append** for KRZ and EIII queues.
5. **Export-from-image** for KRZ and EIII, byte-identical to what is on the
   image.

**A trap worth knowing before writing any KRZ test:** a K2000 bank with zero
samples is perfectly valid — its programs reference the sampler's **ROM**
keymaps, and 21 of 26 banks in the reference library are like that. Even
inside a bank that does carry samples, an individual program may be ROM-only.
So "this bank has samples" does not mean "this program pulls any", and a
test that assembles one and asserts on its samples will fail for a reason
that is not a fault. Decide it by assembling the program and looking.

## How a release run goes

```bash
mkdir -p ~/temp/vinsamlib-tests
TMPDIR=~/temp/vinsamlib-tests QT_QPA_PLATFORM=offscreen \
    .venv/bin/python tests/manual_ui_smoke_<name>.py
```

**Keep the scratch space off `/tmp`.** On this machine `/tmp` is its own
4.7 GB volume shared with mpc2emu, and a full run of both suites has emptied
it twice — which does *not* present as a disk problem. It looks like a
cluster of unrelated test failures that comes and goes, and here it also
stopped the shell working entirely, because the tooling writes its own
output under `/tmp`. **If a suite ever fails in a cluster with no obvious
cause, check `df` before reading a diff.**

The product itself no longer contributes: `vinsamlib/tempdirs.py` frees every
staging directory (7 left after a full run, all belonging to the test scripts
rather than to any product path). `TMPDIR` above moves those last few too.

One trap, if this ever becomes a pytest suite: `--basetemp` makes pytest
**delete and recreate** the directory it is given at the start of every run.
Point it somewhere nothing else lives — never at a folder holding corpus or
fixtures.

Run every `tests/manual_ui_smoke_*.py`, then the format-specific suites
(`manual_akai_*`, `manual_bank_sample_rename`, `manual_hw_convert_matrix`).
A suite that prints `SKIPPED` because its material is not on the machine is
**not** a pass — note it and find the material, or the matrix cell stays
uncovered.

## Assert the content, not the arrival

The single most valuable thing this matrix turned up was not a missing cell.
It was a covered one that asserted the wrong thing.

`manual_ui_smoke_convert.py` built a bank with a vintage resample profile,
put it on an image, and checked that **exactly one bank row appeared**. It
passed for months while writing a bank that had lost **68 of its 81
samples** — mpc2emu's E4B writer mis-sizes a chunk when a resampled *stereo*
sample ends a half-frame long, so everything after the first sample
misaligns. Every layer was happy: the Bank in memory was correct, the file
existed, the row appeared, and the hardware would have loaded almost
nothing.

So: **"it landed" is not "it is intact".** Any test that produces a bank or
an image must read it back and check the content — sample count, and that
every zone still resolves. `build/convert.py` now does this itself after
every write (`_verify_written`) and refuses a short bank rather than passing
it on, but a test that only counts rows will still miss the next one of
these.

Two standing rules for anything added here: a test must never call
`Config.save()` against the real config, and must keep `config.library_roots
= []` so nothing scans. See `CLAUDE.md`.
