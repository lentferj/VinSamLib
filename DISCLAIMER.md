# Disclaimer

## AI Assistance & Human Authorship

In the interest of transparency: VinSamLib was created by its **human
author** working together with Anthropic's **Claude**, an AI coding
assistant — the same working relationship documented in
[mpc2emu's own DISCLAIMER.md](https://github.com/lentferj/mpc2emu/blob/main/DISCLAIMER.md),
the sibling project this app is built on top of.

**The ideas, the direction, and every correction are human.** The
concept of a GUI librarian sitting on top of mpc2emu's conversion
engine, the feature set (the Explorer/search/library index, the New
Bank drag-and-drop builder, the Pending-for-Image queue, the Image
column's disk-image management, XPM import, per-preset "Import via
mpc2emu" conversion), the priorities, and the design decisions all came
from the human author — including a good number of corrections that
shaped the final shape of things: XPM import's destination moved twice
before landing on "New Bank, as a single preset" once the actual
workflow was tried; the New Bank info panel was condensed from a full
per-zone table down to two summary lines plus real bit-depth/sample-rate
after direct feedback; a real, invisible-text rendering bug was only
caught because the human took and inspected a live screenshot of the
running app; and a real mpc2emu behavioral bug (the velocity-layer
reducer not actually reducing the number of distinct layers for
entangled key/velocity presets) was found, reproduced, and confirmed
fixed against the human's own real sample library — not invented in the
abstract.

**Claude assisted with the execution:** writing and refactoring the
implementation code (the PySide6 UI, the byte-verbatim E4B/KRZ/EIII
readers, the EMU3/FAT/ISO9660 filesystem readers, the mpc2emu
integration layer), building and running the automated offscreen test
suite, and drafting this documentation.

**Verification rests on real use, not just automated tests.** Every pane
and workflow described in the manual below was exercised against the
author's own real, existing sample library (real commercial E4B/KRZ/EIII
banks, real XPM programs, real vintage disk images) — not synthetic
fixtures alone. Beyond that, on **2026-07-28** the author loaded actual
VinSamLib-built images onto real E-mu E4XT hardware (via ZuluSCSI) and
confirmed correct playback across the project's own hardware-
confirmation matrix — every vintage resample profile, every reduce
combination, and the new EIII-on-disk-image capability all load and
play as expected. **Kurzweil K2000R hardware confirmation is still
pending** for VinSamLib's own KRZ image-building path specifically —
considered very likely to work, since it goes entirely through
mpc2emu's own K2000 disk builders, which carry their own separate,
real K2000R/Gotek hardware confirmation (see
[mpc2emu's own DISCLAIMER.md](https://github.com/lentferj/mpc2emu/blob/main/DISCLAIMER.md))
— but this is an honest gap, not yet closed, until VinSamLib's own
K2000R test is
actually run. mpc2emu's own DSP claims that VinSamLib merely exposes
(vintage resample character, key-zone/velocity-layer reduction) carry
mpc2emu's own hardware-confirmation status more broadly, documented in
that project's README, not a separate claim made here.

## Real Data Risk — please read this one

**A real incident happened during this project's development**, and
it's worth stating plainly rather than glossing over: ad-hoc,
throwaway test scripts run during development corrupted the author's
real `~/.config/vinsamlib/config.toml` (wiping real library folder
entries down to test paths) and contaminated the real search index
database with unrelated test content, because two of VinSamLib's own
code paths default to the real, shared files with no override:
`Config.save()` always writes `user_config_dir()/config.toml`
regardless of what path `Config.load()` was given, and `MainWindow`
always opens `user_data_dir()/index.db`. Both files were fully
recovered in that instance, but the underlying facts remain true of the
shipped app, not just of test scripts:

- **VinSamLib can mutate real files in place.** Removing a library
  folder, renaming or deleting a bank on an already-open disk image, and
  every "Add/Remove Library Folder" action write to your real config
  file and your real search index immediately — there is no separate
  "save" step and no built-in undo for these specific actions (New
  Bank's own accidental-add popup **does** offer Keep-Anyway/Undo-Last-
  Add, but that's local to the current session, not a file-level
  history).
- **Appending to or modifying an existing disk image is in-place.**
  Delete/rename/export/append operations on an already-open image
  modify that file directly (via a temp-copy-then-replace, not a
  from-scratch rebuild) — a bug, a crash mid-operation, or an
  unanticipated edge case could damage that image.
- **Keep backups.** Before pointing VinSamLib at real, valuable media —
  original commercial banks, an SD card's worth of ZuluSCSI images, a
  Gotek floppy set — make a copy you haven't touched with this app.

## Proprietary File Formats

VinSamLib reads and writes file formats that are proprietary to their
respective manufacturers and have never been officially documented.
Its own format readers (the byte-verbatim E4B/KRZ/EIII container
readers, and the EMU3/FAT12/16/32/ISO 9660 filesystem readers) are
original implementations informed by public specifications and by
mpc2emu's own, separately-licensed reverse-engineering work — see
[`LICENSE`](LICENSE) for the specific attributions. All bank *writing*
and all signal processing is delegated entirely to mpc2emu at runtime;
VinSamLib carries none of that logic itself.

The authors of VinSamLib are not affiliated with, endorsed by, or
otherwise connected to any of the following companies or their
successors:

- **E-mu Systems / Creative Technology Ltd.** (E4B, EOS)
- **Kurzweil / Young Chang Co. Ltd.** (KRZ)
- **inMusic Brands Inc. / Akai Professional** (MPC XPM)

## No Warranty

This software is provided **as is**, without warranty of any kind,
express or implied, including but not limited to the warranties of
merchantability, fitness for a particular purpose, and
non-infringement.

In no event shall the authors or copyright holders be liable for any
claim, damages, or other liability, whether in an action of contract,
tort, or otherwise, arising from, out of, or in connection with the
software or the use or other dealings in the software.

## Hardware Risk (downstream, via mpc2emu)

VinSamLib itself never talks to hardware — it writes disk image files
to your local filesystem, the same files mpc2emu's own CLI would
produce. Loading any file onto real vintage sampling hardware carries
the same inherent risk described in
[mpc2emu's own DISCLAIMER.md](https://github.com/lentferj/mpc2emu/blob/main/DISCLAIMER.md#hardware-risk):
test on a ZuluSCSI/SCSI2SD emulator before connecting irreplaceable
equipment, and keep good backups of every existing bank on the target
device or media.

The authors accept **no responsibility** for data loss, hardware
damage, or any other adverse effects resulting from the use of this
software.

## Trademarks

All product names, trademarks, and registered trademarks mentioned in
this project are the property of their respective owners. Their use
here is for identification purposes only and does not imply
endorsement.
