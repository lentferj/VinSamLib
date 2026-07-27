<!--
SPDX-License-Identifier: GPL-2.0-or-later
SPDX-FileCopyrightText: Copyright (C) 2026  VinSamLib contributors
-->

# VinSamLib

A librarian and bank builder for vintage sampler content — E-mu E4B
(Emulator IV / E4XT / EOS) and Kurzweil KRZ (K2000 series). Browse a
whole library of banks, discs, and floppy images at once; drag any
preset or program straight into a new bank; queue several banks for a
build; write real, loadable disk images. Where mpc2emu is available,
you can also import an Akai MPC `.xpm` program directly, or run any
existing E4B preset through mpc2emu's vintage resample / sample-count
reduction pipeline on its way into a bank.

> **Legal:** [DISCLAIMER.md](DISCLAIMER.md) · [LICENSE](LICENSE)

---

## ⚠️ Use at your own risk — back up first

VinSamLib is provided **as is, with absolutely no warranty and no
liability** for lost data, damaged hardware, corrupted media, or any
other harm arising from its use. You assume all risk. (Full terms:
[DISCLAIMER.md](DISCLAIMER.md).)

**Before you use this software, make good, current backups** of your
config, your library, and any existing disk images or floppy sets you
point it at. VinSamLib can rename, delete, and append entries on an
already-open disk image **in place**, and every Add/Remove Library
Folder action writes to your real config and search index immediately.
This isn't a hypothetical caution — an early ad-hoc test script during
this project's own development corrupted its author's real config and
search index once; see [DISCLAIMER.md](DISCLAIMER.md#real-data-risk--please-read-this-one)
for the honest account. Always keep an untouched copy of anything
irreplaceable, and test unfamiliar images on a spare SD card / floppy
before touching real hardware.

---

## AI assistance & human authorship

VinSamLib was built by its human author together with Anthropic's
**Claude**. The **ideas, the feature set, and every design correction**
came from the human author, arrived at through real, iterative use —
not a spec written up front. Claude assisted with **writing the
implementation and the test suite**. Full account, including specific
examples of corrections that shaped the final design, in
[DISCLAIMER.md](DISCLAIMER.md).

---

## Features

VinSamLib is a **GUI librarian**, not a batch converter — it sits on
top of [mpc2emu](../mpc2emu) (a separate, sibling project) for every
disk-*image* writer and every DSP routine, and never edits or vendors
mpc2emu's code. **Without mpc2emu installed or configured, VinSamLib is
still a full E4B/KRZ bank builder**, not just a browser: parsing,
assembling, and saving banks (New Bank → Save as…) needs no mpc2emu at
all — `banks/e4b.py` and `banks/krz.py` are entirely self-contained —
and browsing *existing* E4XT (EMU3) discs/HD images and K2000 ISO 9660
discs works natively too. What genuinely needs mpc2emu: **creating or
appending to any disk image** (every image kind's writer lives there,
E4B and KRZ alike), browsing **FAT12/16/32** images specifically (K2000
floppies and FAT-based discs/HDs call into mpc2emu's own FAT code even
just to read), **E4B** preset-level zone/velocity/bit-depth detail in
the Detail pane (KRZ's own detail view is self-contained), XPM import,
and vintage conversion. Settings shows exactly which of these is
unavailable and why if mpc2emu isn't configured.

**Browse your whole library at once.** Point VinSamLib at any number of
folders — loose `.e4b`/`.KRZ` files, EMU3 CD/HD images, ISO 9660 discs,
FAT12/16/32 floppy or hard-disk images, folders of `.xpm` programs — and
it lazily walks the tree, showing banks, discs, folders, presets, and
programs in one unified Explorer. A background scanner indexes
everything into a local search database, so typing in the search box
finds a preset by name anywhere in the whole library, instantly, without
waiting for the tree to be expanded down to it.

**Build a new bank by dragging presets together.** The New Bank column
accepts presets/programs dragged from anywhere in the library (or added
via right-click), locks to whichever format the first one came from, and
shows a live, real size/count meter — computed by actually assembling
the selection, not an estimate. The E4XT's 128 MB / 1000-preset and the
K2000's 1000-program limits are hard, format-technical ceilings, always
enforced; a separate, lower, configurable-in-Settings byte threshold
(default 64 MB E4B / 32 MB KRZ) warns earlier, once a bank likely
exceeds *your own* hardware's actual RAM. Save the result directly to a
file, or queue it for image building.

**Queue several banks, then build.** The Pending for Image column holds
any number of banks-in-progress; each can be renamed, reordered, and
independently given its own mpc2emu conversion recipe before "Build
Image →" assembles all of them into the currently-open (or
about-to-be-created) disk image in one step.

**Manage disc/floppy images directly.** The Image column creates any of
mpc2emu's own image kinds (EMU3 CD, EMU-fs or FAT hard disk for the
E4XT; FAT16 CD/hard-disk or FAT12 Gotek floppy for the K2000), or opens
an existing one, and lets you append, rename, delete, and export
individual bank entries in place.

**Import an Akai MPC `.xpm` program**, when mpc2emu is available —
double-click or right-click any `.xpm` file in the library, choose a
target format and optional vintage conversion, and it lands in New Bank
as a single preset, ready to combine with anything else.

**Run an existing preset through mpc2emu's vintage pipeline.**
Right-click any real preset or program in Explorer — E4B or KRZ — for a
second option, "Import via mpc2emu…", offering the exact same
resample/reduce dialog XPM import uses: apply the EMU Emulator II or
Emax I character, thin out an overly dense multisample, or convert to
the other format entirely, without leaving VinSamLib. The dialog
defaults its target format to the preset's own source format, so
"same format, with options" (apply processing without converting) is
one click away — the same "Add" a plain drag would do, plus optional
processing.

---

## Requirements

- Python 3.11 or later
- [PySide6](https://pypi.org/project/PySide6/) `6.11.1` (the only
  mandatory dependency — see `pyproject.toml`)
- A local checkout of [mpc2emu](../mpc2emu), for XPM import and vintage
  conversion. Without it, VinSamLib still runs as a browser/bank-builder;
  Settings will show exactly what's missing.

---

## Installation

```bash
git clone <this repo's URL> vinsamlib
cd vinsamlib
pip install -e .
```

Then, if you want XPM import and vintage conversion: clone
[mpc2emu](../mpc2emu) somewhere on the same machine, launch VinSamLib
(`vinsamlib` or `python -m vinsamlib.app`), open **File → Settings…**,
and point the "mpc2emu checkout" field at that directory. The status
line updates live as you type — it tells you separately whether the
path itself is a usable mpc2emu checkout, and whether the specific
modules the conversion feature needs are present. Changing the path
takes effect on the next restart (Python's own module cache holds
whichever mpc2emu modules were already imported from the old location).

---

![VinSamLib: library tree and Detail pane showing a multisampled preset](docs/screenshots/01_overview.png)

*Explorer (left) with a bank expanded and a preset selected; the Detail
pane (below it) shows its condensed key-zone/velocity-layer/bit-depth
summary. (Screenshots throughout this manual use a small synthetic demo
library, not real commercial content.)*

## Quick Start

**Browse and build your first bank:**

1. **File → Add Library Folder…** and pick a folder containing E4B/KRZ
   banks, disc images, or floppy images (the file dialog remembers where
   you last added a folder from).
2. Expand it in the Explorer tree — banks and discs open lazily, so a
   large library doesn't stall on first click. Presets/programs inside a
   bank show a 🎹 icon.
3. Drag a preset into the **New Bank** column (or right-click it →
   "Add… to New Bank"). Drag a few more — from anywhere in the library,
   any format, as long as they all match the first one's format.
4. Give the bank a name in the **Name:** field — that's the filename a
   real E4XT or K2000 will show as the bank's own name.
5. Either **Save as…** to write the assembled bytes straight to a file,
   or **Send to Image Column** to queue it in **Pending for Image**.
6. In **Pending for Image**, click **Build Image →**. If no image is
   open yet, a "New Image" dialog asks what kind to create (matching
   your target hardware) and how big; otherwise it appends to whatever's
   already open in the **Image** column.
7. The **Image** column now shows your new bank as an entry on the disk
   image — copy that `.hda`/`.iso`/`.img` file to your ZuluSCSI/Gotek
   media the same way you would one built by mpc2emu's own CLI.

**Import an Akai MPC program (needs mpc2emu):**

1. Add a library folder containing `.xpm` files, or use
   **File → Import XPM…** to pick one directly.
2. Double-click the `.xpm` (or right-click it → **Import…**). A dialog
   asks for the target format (E4B or KRZ) and, optionally, vintage
   resample/reduce options.
3. The imported preset lands directly in **New Bank** — an XPM always
   holds exactly one program, so there's nothing to choose between.

**Run an existing preset through mpc2emu (needs mpc2emu):**

1. Find a real E4B or KRZ preset/program anywhere in your library.
2. Right-click it → **Import via mpc2emu…** — the same dialog as XPM
   import. The target-format picker defaults to the preset's own
   format, so applying options without changing format is one click.
3. Pick your options (say, the Emulator II profile with a 30% key-zone
   reduction) and confirm; the converted result lands in New Bank
   alongside anything already there, labeled `"<name> (mpc2emu)"`.

---

## The Manual

### Library & Search

**File → Add Library Folder…** registers a folder as a library root;
VinSamLib remembers all your roots across restarts and lists them
alphabetically by path in the Explorer tree (not by the order you added
them). **File → Remove Library Folder…** picks one from a list to
un-register (right-click a root directly in Explorer for the same
action without the picker) — this only stops VinSamLib from tracking
it; no files on disk are touched. **File → Rescan Library** re-runs the
background indexer over every current root, useful after you've added
new banks to a folder outside the app.

Every root is scanned in the background into a local SQLite index
(`index.db` in your user data directory) so the **search box** above the
Explorer tree can find a preset/program by name anywhere in the whole
library — including inside banks you've never expanded — the instant
you type. Search is **word-prefix matching**: each space-separated word
you type must *start* a word somewhere in the item's name, and multiple
words are AND-ed together (so `bass str` matches "Bassoon Strings" but
not "Bassoon Trumpet"). The format dropdown next to the search box
(`All`/`E4B`/`KRZ`/`XPM`) filters both the live tree and search results
to just that format.

### Explorer

The Explorer pane shows either the lazy folder/bank tree (search box
empty) or a flat list of index-backed search hits (search box non-
empty) — both funnel into the same Detail pane and the same drag/context-
menu actions, so it doesn't matter which view you're using.

Right-click (or double-click) behavior depends on what you've selected:

| Item | Double-click | Right-click menu |
|---|---|---|
| Preset/program | Add to New Bank | "Add … to New Bank"; **"Import via mpc2emu…"** (E4B or KRZ) |
| `.xpm` file | Import (opens the conversion dialog) | "Import …" |
| Library root (top-level folder) | — | "Remove … from Library…" |

Content VinSamLib genuinely can't read yet — currently, real **EIII/
ESI-32** bank data sharing an EMU3-filesystem disc alongside readable
E4B content — is shown **greyed out with its detected format label**,
rather than hidden or shown as garbage. This is a deliberate choice:
older discs commonly mix formats on one volume, and hiding real content
would look like a broken or empty folder.

### Detail Pane

Selecting a preset, program, or `.xpm` shows a condensed summary rather
than a row-per-zone table (an earlier version showed the full table;
real presets can carry dozens of zones, and that much detail wasn't
actually useful at a glance): voice/keymap count, total unique sample
size, then two summary lines —

- **`N key zones with M samples`** — how many distinct key ranges the
  preset splits into, and how many distinct samples are referenced in
  total.
- **`N velocity layers with M samples each`** (or a range, `M–M2 samples
  each`, if it isn't uniform) — how many distinct velocity bands exist,
  and how many distinct samples fall within a single band.

— followed by bit depth and sample rate, read from the samples
themselves (not assumed): a single value if uniform across the preset,
a range if it mixes rates/depths. For KRZ, the sample rate is decoded
exactly from the K2000's own `samplePeriod` field
(`sample_rate = round(1e9 / samplePeriod)`), not guessed.

### Samples Pane

![Samples pane showing the per-zone sample/key-range/velocity-range/root/loop table](docs/screenshots/07_samples_pane.png)

Hidden by default (**View → Show Samples Column**) — this is the
uncondensed counterpart to the Detail pane's summary: one row per zone,
with the sample name, key range, velocity range, root key, and loop
type. Useful when you need the exact per-zone breakdown the Detail
pane's summary intentionally leaves out.

### New Bank

![New Bank column with three presets added and the selection info panel showing a condensed summary](docs/screenshots/02_new_bank.png)

The first preset you add locks the whole bank's format — E4B or KRZ —
shown right in the column header (`New Bank [E4B]`); a later drop of the
*other* format is rejected with a status message, matching mpc2emu's own
"no cross-format conversion in one step" rule (that's what "Import via
mpc2emu" is for).

**Duplicate detection** (View menu, both on by default): re-adding a
preset already in the bank is caught by content — the bank's file path
plus the preset's own index/id, not object identity, since a preset
reached through search is re-parsed fresh every time. **"Check for
Duplicate Presets"** turns the check off entirely if unchecked;
**"Prompt Before Skipping Duplicates"** switches a caught duplicate from
silently skipped to a yes/no confirmation (only meaningful while the
check itself is on).

The **size/count meter** below the name field recomputes by actually
assembling the current selection — not an estimate — debounced a
quarter-second after your last change. It enforces the real hardware
ceilings: **128 MB / 1000 presets** for E4B, **1000 programs** for KRZ.
Going over either pushes the meter red and disables **Save as…**.

**Adding a preset that pushes you over the limit** shows a warning
dialog with two choices: **Keep Anyway** (leave the new item in place,
deal with it later) or **Undo Last Add** (revert to exactly the state
before that specific add — whichever it was: a drag, "Add to New Bank",
an XPM import, or an "Import via mpc2emu" conversion). This only fires
once per crossing — adding still more while already over won't nag you
again until you drop back under the limit.

Selecting an item in the list shows the same condensed Detail-pane-style
summary described above, computed in the background so large presets
don't stall the UI.

**Save as…** writes the exact assembled bytes to a file you choose.
**Send to Image Column** hands the current (bank, preset list, name)
recipe to **Pending for Image** — the recipe stays editable there, it
isn't a frozen copy.

### Pending for Image

![Pending for Image column showing one queued bank and its contents](docs/screenshots/03_pending_for_image.png)

Each entry in the queue can be **renamed**, **reordered** (drag within
the list), and given its **own mpc2emu conversion options** —
deliberately per *bank*, not a single global setting for the whole
queue, so you can, say, apply vintage resampling to one bank and leave
another untouched in the same build. (Per-*preset* granularity — mixing
converted and unconverted presets *within* one bank — is a deferred
future enhancement, not yet built; the underlying technique, assembling
temporary single-preset banks, is proven and documented for when it's
picked up.)

The conversion button is currently disabled for a KRZ-format queue (a
scope decision, not a technical limitation any more — mpc2emu can read
KRZ now too; per-bank KRZ conversion here just hasn't been wired up
yet). Convert a KRZ preset individually via Explorer's "Import via
mpc2emu…" in the meantime.

**Build Image →** assembles every entry currently in the queue and hands
the results to the Image column. Building does **not** empty the queue
— the same recipe can be rebuilt as many times as you like (handy while
iterating on conversion options).

### Convert Options dialog

![Convert Options dialog with Vintage Resample and Reduce Sample Count both expanded](docs/screenshots/05_convert_options.png)

Shared by "Import via mpc2emu", Pending's per-bank conversion, and XPM
import — three independent, collapsible sections:

- **Vintage Resample** — pick `EMU Emulator II` (8-bit µ-law companded,
  27,777 Hz — the defining lo-fi grit) or `EMU Emax I` (12-bit linear,
  27,500 Hz — cleaner). **Apply bandpass coloring** (on by default)
  simulates the output filter stage; unchecking it isolates the raw
  bit/rate reduction. **Keep gain-staged (hot) level** skips restoring
  each sample to its original peak level afterward, leaving the louder,
  gain-staged level the DSP works at internally.
- **Limit Maximum Sample Rate** — an independent step (not gated behind
  Vintage Resample also being on): clean-downsamples anything above the
  chosen rate. Only ever downsamples, never up.
- **Reduce Sample Count** — **Reduce Key Zones by** / **Reduce Velocity
  Layers by**, each an independent percentage slider. The percentage is
  how much to **remove**, not a target to shrink *to* — 30% removes
  ~30%, keeping ~70% spread evenly across the range (matches mpc2emu's
  own CLI semantics and wording exactly). With small counts, rounding
  means the actual fraction removed won't always be exact.

The dialog auto-resizes as you check more sections, and the title/
wording adapts to which feature opened it (e.g. "Import via mpc2emu" vs.
"Import XPM") so it never says the wrong thing.

**XPM import and "Import via mpc2emu"** both add an **Import as:** /
target-format picker at the top (Pending's per-bank dialog doesn't —
it's still E4B-queue-only, see Pending for Image above). "Import via
mpc2emu" defaults this picker to the preset's own source format;
switching it to KRZ nudges (never forces) the max-sample-rate step to a
sane default of 24000 Hz if you haven't already set one yourself — the
K2000 only has +1.46 semitones of up-pitch headroom at 44.1 kHz before
wide key zones start clamping, while the E4XT has no such ceiling.

### Image Column

![Image column with an open EMU3 HD image showing one bank entry and its metadata](docs/screenshots/04_image_column.png)

**New…** creates a fresh image; the dialog offers every kind mpc2emu
itself can write:

| Kind | Format | Produces | Extension |
|---|---|---|---|
| EMU3 CD | E4B | ZuluSCSI CD-ROM image (EMU3 filesystem) | `.iso` |
| EMU3 HD (native EMU filesystem) | E4B | SCSI hard disk, all EOS versions | `.hda` |
| EMU3 HD (FAT) | E4B | SCSI hard disk, EOS 4.7+ only | `.hda` |
| K2000 FAT16 | KRZ | CD or hard disk (universally compatible) | `.hda`/`.iso` |
| K2000 ISO 9660 | KRZ | CD, needs K2000 OS v3.87+ | `.iso` |
| K2000 Gotek floppy | KRZ | FAT12 floppy for a Gotek/FlashFloppy | `.img` |

**Open…** opens an existing image (its kind is auto-detected from the
real bytes, not assumed from the file extension). Once open, dragging a
bank in (or a Pending build landing on it) **appends** to it in place —
no rebuild, no external tools — except floppy images, which aren't
appendable and must be built whole each time.

Right-click an entry for **Rename…**, **Delete**, or **Export…** (write
just that one bank back out to a standalone file) — all in-place
operations on the real image file (via a temp-copy-then-replace, never a
from-scratch rebuild), confirmed with a dialog before anything
destructive happens.

### XPM Import

An `.xpm` (Akai MPC Keygroup program) always holds **exactly one**
program — mpc2emu's own parser guarantees this — so importing one always
produces exactly one preset in New Bank, never a whole separate bank of
its own. The display name uses the **original filename**, not the
preset's internal name: E4B truncates preset names to 16 hardware
characters, so several distinctly-named XPMs sharing a long common
prefix would otherwise all show up under the same collapsed name.

### "Import via mpc2emu"

Generalizes XPM import's exact same pipeline to a preset or program you
already have natively in your library — E4B or KRZ, either can be the
source and either can be the target: right-click it, choose options,
and get a converted copy in New Bank — without exporting anything or
leaving the app. This covers exactly the cases a plain "Add" can't:
converting to the *other* format (source format ≠ New Bank's current
format), or applying resample/reduce while keeping the *same* format
(source format = target — the dialog defaults to this). Converting the
*same* source preset more than once (e.g. to compare two different
option sets) gives each result a distinguishable name —
`"<name> (mpc2emu)"`, then `"<name> (mpc2emu) 2"`, `"3"`, and so on —
since two different conversions of one preset are deliberately **not**
treated as duplicates by New Bank's dedup check (only an identical
repeat would be), so nothing else would otherwise keep them apart in
the list.

### Settings

![Settings dialog showing a found mpc2emu checkout and its live status line](docs/screenshots/06_settings.png)

**File → Settings…** — the mpc2emu checkout path, with a live status
line: whether the path itself is even a usable mpc2emu checkout, and
separately whether the specific modules the conversion feature needs
are present (a checkout could exist but be an incompatible or partial
version). Changing the path needs a restart to take effect.

Also here: **New Bank size-warning thresholds**, in MB, one each for
E4B and KRZ (defaults 64 MB / 32 MB — the most common real E4XT/K2000
RAM configurations, not the format's own absolute technical maximum:
128 MB for E4B, and the K2000 has no hard byte ceiling at all, only its
1000-program limit). This is a *soft* warning New Bank's size meter
uses to flag a bank that's probably too big for your actual hardware —
"Keep Anyway" is still offered in the over-limit dialog if you genuinely
have more RAM installed. Takes effect immediately, no restart needed.

### Keyboard Shortcuts

**Delete** removes the current selection wherever a "Remove"-style
action exists: New Bank's list, the Image column's list, and both the
main queue and the per-bank contents list in Pending for Image.

---

## Known Limitations

| Feature | Status |
|---|---|
| EIII / ESI-32 bank format | ❌ shown (greyed out, format-labeled), not readable — no reference implementation exists anywhere to build one from yet |
| KRZ as a conversion *source* | ✅ mpc2emu's own KRZ reader (added 2026-07-27, corpus-verified against 593 real files) made this possible — KRZ presets/programs can now be converted the same way E4B ones can, via Explorer's "Import via mpc2emu…" |
| Per-bank KRZ conversion in Pending for Image | ⚠️ per-preset conversion via Explorer works for KRZ now; the whole-bank "Process before building…" button in Pending is still E4B-only — a scope decision, not a technical limitation, since it hasn't been wired up for KRZ queues yet |
| Per-preset conversion granularity | ⚠️ conversion options are per-*bank* in Pending for Image; mixing converted/unconverted presets within one bank is a documented, not-yet-built enhancement |
| Some coverage-remapped KRZ presets can't be re-processed | ⚠️ a real mpc2emu bug (`writers/krz_writer.py`, tracked in mpc2emu's own TODO): a preset needing the octave-slice-stack "coverage remap" rebuild can crash on write when reprocessed; most real content is unaffected — VinSamLib surfaces the real error if it happens rather than silently failing |
| Gotek floppy images | ⚠️ create-only — not appendable (a real FAT12 floppy constraint, not a bug) |
| Real hardware confirmation of VinSamLib's own new UI features | This app's own UI/workflow is verified by real use against the author's library; the underlying DSP claims it exposes (vintage resample character, reduction behavior) carry mpc2emu's own separately-documented hardware confirmation |

---

## Project Structure

```
vinsamlib/
├── app.py                      # Entry point
├── config.py                   # Config load/save, mpc2emu path checks
├── mpc2emu_bridge.py            # Lazy sys.path proxies onto an external mpc2emu checkout
├── banks/
│   ├── e4b.py                  # Byte-verbatim E4B container reader/assembler
│   ├── krz.py                  # Byte-verbatim KRZ container reader/assembler
│   └── summary.py              # Zone/velocity/bit-depth/sample-rate summaries for the UI
├── build/
│   ├── convert.py              # mpc2emu resample/reduce wrapper (ConversionOptions)
│   ├── xpm_import.py           # XPM -> E4B/KRZ import, sharing convert.py's pipeline
│   └── images.py               # create_image()/append_banks() over mpc2emu's writers
├── vfs/                        # Read-side filesystem support mpc2emu itself never needed
│   ├── emu3.py                 # EMU3 filesystem (E4XT CD/HD images)
│   ├── fatvol.py               # FAT12/16/32 (K2000 floppy/HD images)
│   ├── iso9660.py               # Standard ISO 9660 (K2000 CD images)
│   └── detect.py               # Format sniffing/dispatch
├── index/
│   ├── db.py                   # SQLite + FTS5 search index
│   └── scanner.py               # Background library scanner
└── ui/
    ├── main_window.py           # Menu bar, pane wiring
    ├── explorer_pane.py          # Library tree + search
    ├── detail_pane.py            # Condensed preset/program info
    ├── samples_pane.py           # Per-preset sample list
    ├── bank_pane.py              # New Bank column
    ├── pending_pane.py           # Pending for Image column
    ├── image_pane.py             # Image column
    ├── convert_options_dialog.py # Shared resample/reduce dialog
    ├── xpm_import_dialog.py      # + target-format picker, subclasses the above
    └── settings_dialog.py        # mpc2emu path configuration
```

---

## License and Third-Party Sources

This project is released under the **GNU General Public License v2.0 or
later (GPL-2.0-or-later)** — see [`LICENSE`](LICENSE).

**mpc2emu is a runtime dependency, not vendored code.** VinSamLib
deliberately never edits or copies mpc2emu's source — every E4B/KRZ
bank *writer* and every DSP routine (vintage resample, key-zone/
velocity-layer reduction, XPM parsing) this app exposes is mpc2emu's own
code, loaded from a separate checkout at runtime. mpc2emu carries its
own, considerably longer credit chain for the format knowledge behind
those writers (emu3fs, emu3bm, KurzFiler, ConvertWithMoss, libgig, and
more) — see [`../mpc2emu/README.md`](../mpc2emu/README.md#license-and-third-party-sources)
directly rather than this file duplicating it.

VinSamLib's own format *readers* (the byte-verbatim E4B/KRZ container
readers, and the EMU3/FAT12/16/32/ISO 9660 filesystem readers) are
original code informed by public specifications and by mpc2emu's own
separately-licensed reverse-engineering work — see [`LICENSE`](LICENSE)
for the specific, per-file attributions.

The GUI itself is built on **[PySide6](https://pypi.org/project/PySide6/)**
(Qt for Python), © The Qt Company, licensed under the LGPL.

---

*E-mu, Emulator, EOS are trademarks of Creative Technology Ltd. ·
Kurzweil is a trademark of Young Chang Co. Ltd. · Akai MPC is a
trademark of inMusic Brands Inc.*
