# TODO — VinSamLib

Findings and open items. `tests/` is gitignored by decision, so this file is
the tracked record for anything an external review or a fresh clone needs to
know. Resolution strategies live next to the code that needs them.

---

# External code review — GLM-5.3-Flash (2026-09-20)

**Status (2026-09-20, later the same day): every finding verified against
the code, eight fixed, TWO REFUTED.** The verification is recorded per
finding below; the two that do not hold are marked NOT CONFIRMED with the
evidence, because a review is a claim about the code and the code is the
authority. Anything touching an mpc2emu interface was relayed to them the
same evening.

**Original status:** findings only, no code changed. A full read of `vinsamlib/`,
`tools/` and the test plan, with every mpc2emu interface call verified
against the sibling checkout first-hand (the signatures quoted here were
checked against the live checkout, and mpc2emu's side of the same audit is
recorded in mpc2emu/TODO.md, same date). Ordered by risk: the first two
findings are the only ones that can mis-serve a user silently today.

## ER-1 — `lfo_sync_bpm` can silently stop working

**CONFIRMED and FIXED** (`build/xpm_import.py`). The guard was
`prev is not None`, so a checkout that renamed or dropped `SYNC_BPM` would
have taken the tempo, ignored it, and converted every synced LFO at 120 with
nothing said. Now a module-level `_MISSING` sentinel tells absence from a
legitimate `None`, and an unsupported checkout **raises** with a written-out
reason, matching the `chromatic_pads` path four lines up. Exercised both ways
by deleting the global and restoring it. Today upstream is `SYNC_BPM = 120.0`,
so the old form happened to work -- which is why nothing caught it.

`vinsamlib/build/xpm_import.py:428-437` sets mpc2emu's module global
`xpm_parser.SYNC_BPM` around each parse (restored in `finally` — correct
against leaks; unsafe under threads, fine for the single GUI worker). The
guard is `prev = getattr(xpm_parser, "SYNC_BPM", None)` plus
`prev is not None`. If a future mpc2emu renames or removes `SYNC_BPM`,
`prev` is `None` and a user-requested tempo is **silently ignored** — every
tempo-synced LFO then converts at the 120 BPM assumption with no risk entry
and no message. Contrast the `chromatic_pads` path four lines up, which
raises a clear "update mpc2emu" error. Make the quiet path loud: when
`lfo_sync_bpm is not None and prev is None`, emit a risk record (or raise,
like the pads path does).

Cross-ref: mpc2emu TODO ER-5 — the real fix is a `parse_xpm(sync_bpm=)`
parameter on their side; when it lands, delete the global mutation here.

**LANDED the same evening, and taken.** mpc2emu shipped
`parse_xpm(xpm_path, wav_dir=None, chromatic_pads=False, sync_bpm=None)`
(verified at the site: `parsers/xpm_parser.py:1561`, with `_mpc_sync_hz`
falling back to the global when it is None, so every existing caller is
unchanged). `parse_mpc` now passes the tempo as a PARAMETER when the probe
sees one -- no mutation, no restore, nothing to leak into the next import,
nothing to go silently wrong if the global is renamed. Verified with a
stand-in of their signature: the tempo arrives as `sync_bpm=138.0` and
`xpm_parser.SYNC_BPM` reads 120.0 during and after the call.

The global path stays for a checkout that predates the parameter, sentinel
and all, and raises when neither exists.

## ER-2 — diagnostics: the comment and the code disagree about `content_lost`

**CONFIRMED and FIXED** (`build/convert.py`). The comment promised an
attribute read; the code was `getattr(..., False)`. Neither of the review's
two options was taken: the comment was right about the intent and the code
was right not to raise after a bank is already written. `_content_lost()`
now reads the attribute and, when it is absent, files a visible
`VINSAMLIB_DIAGNOSTICS_INCOMPLETE` record once per conversion saying the
loss count cannot be trusted. Both branches exercised.

`vinsamlib/build/convert.py:655-659` reads
`"content_lost": bool(getattr(d, "content_lost", False))` under a comment
saying the field is read "as an attribute and not with a .get() that would
quietly read False on a checkout that does not have it" — but the getattr
default does exactly the quiet False the comment rules out. Today this
cannot bite (an older checkout degrades `collect()` to `[]` wholesale
before this code runs), but the comment documents a guarantee the code does
not make. Fix the comment, or drop the default and let the AttributeError
hit the same documented degradation path.

## ER-3 — `_krz_writer_kwargs` swallows everything

**CONFIRMED and FIXED** (`build/convert.py`). `except Exception: return {}`
is now `except AttributeError`, and an option the user actually ticked that
this checkout cannot honour raises `ConvertOpError` before the write rather
than silently fusing layers. Unticked options on an old checkout still
degrade quietly, which is correct: the default IS their behaviour. All three
paths exercised against a stand-in writer.

`vinsamlib/build/convert.py:672-678`: `except Exception: return {}`. Any
failure of the `co_varnames` probe — not just an old checkout — silently
drops `krz_faithful_layers` / `krz_drum_program`, so a user asking for a
drum program gets a fused, channel-silent program with no message. That
contradicts the project's own "content loss always reaches the user" rule
(which has a dedicated test). Narrow the except to AttributeError, or record
a risk entry on the way out.

## ER-4 — silent-degradation fallbacks on private-name reaches

**CONFIRMED; guarded as the review proposed.**
`tests/manual_upstream_parity.py` gained `check_private_names_we_reach_for`,
asserting `xpm_parser._safe_name`, `._unique_sample_name`, `._prefers_tail`
and `bank_splitter._VOICES_PER_NOTE` still exist, naming the file that reads
each. The runtime `getattr` fallbacks stay -- they are correct for "no
mpc2emu at all" -- and are now dead-defensive rather than load-bearing.

The reaches into mpc2emu's underscore API are deliberate and well-argued
(`build/akai_image.py:316-335`: "an import that breaks is loud; a copied
rule that drifts is silent"). But two of them fail QUIETLY, violating that
stated principle:

- `xpm_parser._safe_name` / `_unique_sample_name` / `_prefers_tail`
  (`build/xpm_import.py:339-356`, `build/sample_names.py:81-119`,
  `build/foreign_import.py:433`): `getattr(..., None)` → graceful absence →
  worse name pairing with no signal. A rename upstream would degrade
  pairing (the 17-of-97 rows case) again with nothing in the log.
- `bank_splitter._VOICES_PER_NOTE` (`build/convert.py:363`): getattr
  default `{}` → the voice-budget check quietly turns itself off.

Fix: assert each consumed private name exists in
`manual_upstream_parity` (the check `manual_unbound_names.py` already
performs for a sibling fault class); the getattr fallbacks then become
dead-defensive and honest.

## ER-5 — dead duplicates and dead code

**ALL THREE CONFIRMED and FIXED.** The duplicate `akai_writer` binding is
gone (a note marks where it stood). The three `fatvol` `append()` methods now
raise `NotImplementedError` with the reason -- `isinstance(vol,
WritableVolume)` still holds, which is all `image_pane` needs, and "browsing
never needs mpc2emu" is true by construction instead of by nobody calling
them. `tests/_oracle.py`'s expired snapshot branch is deleted; with it goes
the last reference to `tests/akai_fixtures/mpc2emu_akai/` (2.4 MB, now
unreferenced -- deleting it is Jan's call).

- `mpc2emu_bridge.py:90` and `:124` both bind `akai_writer` — the second
  silently overwrites the first with an identical `_Lazy`. Delete one.
- `vfs/fatvol.py:560/700/805` — the three `append()` methods are dead code
  kept only so `isinstance(vol, WritableVolume)` holds; their own docstrings
  say so. They are the only browsing-path code that would need mpc2emu if
  ever called, contradicting the module's "browsing never needs mpc2emu"
  promise. Raise a clear NotImplementedError or move the capability flag
  elsewhere.
- `tests/_oracle.py:44-45` still carries the expired (2026-09-07) frozen
  snapshot `sys.path` fallback — the second `sys.path`-manipulation site in
  the repo. Its expiry is loudly announced; remove the branch.

## ER-6 — structure smells

**MIXED: two fixed, two deferred with reasons, TWO NOT CONFIRMED.**

- **`vinsamlib.egg-info/` is committed — NOT CONFIRMED.** `git ls-files |
  grep egg-info` returns nothing. The directory exists on disk and is
  ignored by `.gitignore:4` (`*.egg-info/`), which `git check-ignore -v`
  confirms. Nothing to do.
- **`_trim_spool()` outside its lock — NOT CONFIRMED, and the current code is
  the only correct form.** `_trim_spool` takes `_lock` itself
  (`build/calllog.py:125-127`); `threading.Lock` is not reentrant, so calling
  it from inside the `with _lock:` block at line 88 would deadlock. Calling
  it after the block is deliberate, not a race.
- **Undocumented bare `except Exception` — partly confirmed, four fixed.**
  `ui/main_window.py:241` already carried its reason ("never let remembering
  a size block a quit"). Comments added at `build/calllog.py`'s `asdict`
  fallback, `ui/explorer_pane.py`'s greying heuristic and both
  `ui/bank_pane.py` sites. `ui/models.py`'s eight remain OPEN.
- **Three `_run_captured`s — confirmed, deferred deliberately.** The two in
  `convert.py` and `images.py` already cross-reference each other and differ
  in contract on purpose: one folds the captured log into the raised error
  (and the ORDER of that message has already been fixed once, after a real
  failure surfaced as a progress line), the other hands the log back because
  its caller displays it. A merge wants a shared core plus two thin wrappers
  and a test that pins the error-message order; worth doing, not worth doing
  quickly.
- **`Config.load()` re-read in library code — confirmed, deferred.** Five
  sites. Threading a config through every call site is a wide change with no
  behavioural gain today; the honest version is a module-level accessor with
  one place to override, and it should land with the restart-to-apply
  coupling the review rightly links it to.
- **`vs_` alias inconsistency — confirmed, cosmetic.** Left alone.

- `_run_captured` exists three times with three return conventions:
  `build/convert.py:202` (result), `build/images.py:83` (`(result, log)`),
  `build/calllog.py` `traced` (result + stdout reprint). Consolidate in
  calllog; the comments already acknowledge the kinship.
- `Config.load()` is re-read inside library code (`build/convert.py:804`,
  `:1377`, `build/akai_image.py:71`, `build/foreign_import.py:125`,
  `build/calllog.py:94`) — a single-config assumption is implicit. Pass the
  config explicitly; the Settings dialog's restart-to-apply note (Python's
  module cache) is the same coupling in disguise.
- The `vs_` import-alias convention is applied inconsistently:
  `banks/summary.py:39` imports the bank modules bare, while
  `build/images.py` aliases its OWN `build.akai_image` as `vs_akai_image`
  and `build/akai_image.py` imports `banks.akai` as `vs_akai`. Cosmetic;
  pick one convention.
- `vinsamlib.egg-info/` is committed — build artifact in the tree.
- `build/calllog.py` calls `_trim_spool()` outside its lock — benign race,
  sloppy.
- Bare `except Exception:` without the project's usual explaining comment:
  `build/calllog.py:168`, `ui/main_window.py:241`, `ui/models.py` (×8),
  `ui/explorer_pane.py:653`, `ui/bank_pane.py:829,1608`. Most of the
  codebase sets the standard ("verification returns, never masks", each with
  a reason); bring these up to it.

## ER-7 — `ConversionOptions.is_noop` is a hand-maintained option list

**CONFIRMED; the existing guard is the answer and it has bitten.**
`tests/manual_noop_knows_every_option.py` failed on 2026-09-19 the moment
four new fields were added and passed only once each was either checked or
exempted with a written reason. Keeping the test mandatory is the standing
rule; the `asdict`-driven restructure stays an option rather than a
requirement.

`build/convert.py:184-199` omits `no_bandpass`, `resample_keep_gain`, the
trim fade/keep-loop fields (and deliberately `krz_faithful_layers`, which is
documented). Safe today by construction — each omitted field only matters
when another checked field is set — and one new option away from wrongly
skipping a genuine round trip. `tests/manual_noop_knows_every_option.py`
guards this (evidence it already bit once); keep that test mandatory for
every new `ConversionOptions` field, or restructure so the dataclass drives
the comparison (`asdict` minus a documented exclusion list).

## ER-8 — open mpc2emu items that land directly in VinSamLib's UI

**ALL THREE CONFIRMED; two already carried, one relayed.** The
`--trim-tail` note the review asks for is already in the README (Trim Silence
carries a measured ⚠️ block, added 2026-09-18). The E4B rate-law divergence
and the EB16 effects item need nothing here. mpc2emu was told the same
evening that their TODO and ours had been read against each other, and that
ER-1's real fix is their `parse_xpm(sync_bpm=)` parameter (their ER-5).

From mpc2emu's TODO.md, read against what this project exposes:

- **`--trim-tail` inert threshold / over-cut** (their OPEN row): the Convert
  Options panel exposes the tail depth slider directly. On samples without
  trailing silence the threshold is inert, and on some material the cut
  removes audible audio (their measured case: 49% of one sample at -8.5 dB
  below peak). Until fixed upstream, consider a README Known Limitations
  note so the slider's behaviour is not a mystery to the user.
- **E4B rate-law divergence above byte 110** (their 2026-09-20 entry):
  13.8% of AKAI→E4B-converted voices land in the divergent region; the fix
  is write-path and gated on their card rebuild. No action here — but banks
  converted before the fix will not match banks converted after, and
  `_verify_written` will re-read whichever bytes the writer of the day
  produced.
- **AKAI EB16 effects read by nothing** (their OPEN row): a converted
  program's reverb/FX assignment is dropped with no diagnostic today; their
  minimum useful fix (an emit) is still open. When it exists it will arrive
  through the existing `collect_diagnostics_into` plumbing untouched — that
  path was checked and needs no change.

## What was checked and found sound

For the record, since this is the part a future reviewer would redo:
every mpc2emu call signature in `build/convert.py`, `build/xpm_import.py`,
`build/foreign_import.py`, `build/sampledir_import.py`, `build/images.py`,
`build/akai_image.py`, `build/sample_names.py`, `vfs/emu3.py`,
`vfs/fatvol.py` and `banks/summary.py` matches the sibling checkout as of
2026-09-20 — including the trim kwargs, the `snap_bank_to_playback_rates`
dict contract (`snapped`/`failed`), the `write_krz` / `build_akai_volume` /
`parse_xpm` `co_varnames` probes, the `parsers.registry` normalised
callable shape, and the `models.diagnostics` `code`/`content_lost`
attributes. The `_Lazy` bridge, the copy-then-swap image mutation, the
verification-not-correction write-back check, and the diagnostics-keyed-on-
`code` contract are all sound designs worth keeping exactly as they are.

---

# OPEN — `manual_akai_real_discs` is RED on purpose (2026-09-20)

Not a regression here, and not to be "fixed" by loosening the assertion.

mpc2emu has **uncommitted** changes in `parsers/akai_s3000_parser.py` that
change what a sample's RATE means. On 30 of 483 samples across the real
discs the two readers now differ:

    a drum sample:  40000/61 here (stored 60, tune -169c)  vs  44100/61 theirs

-169 cents is exactly `1200·log2(40000/44100)`, so this is not a wrong value
on either side: ours reports **what the file declares**, theirs now reports
**what the machine will play** after resolving a declared rate the S3000XL
cannot produce. Two quantities, one name -- the same shape as "zones vs
keygroups" and "objects vs audio" earlier the same day.

`manual_akai_real_discs` asserts frame-for-frame agreement between the two
readers, so it fails until we agree what the comparison asserts. Proposed to
them: compare against their declared-rate accessor if one exists, or skip
the rate field with a written reason. Their answer decides the test.

**The other failure from that run is fixed**: `manual_hw_convert_matrix`
timed out at 300 s with no output because their new E4B playback-rate-ceiling
diagnostic reached `pending_pane._on_build_assembled`, which pops a modal
`QMessageBox.warning`. That test stubbed `.question` and not `.warning` --
half-guarded, which looks covered. It now routes every modal through
`_harness.stub_message_boxes()` and prints what was captured, so a new
upstream finding lands in the log instead of freezing an unattended run.
Worth remembering as a class: **any diagnostic they add can block an
automated consumer of ours that has a modal on the same path.**

# CLOSED — S1000 playback rate: measured the same evening (2026-09-20)

The `manual_akai_real_discs` divergence resolved to a real question, not a
units mismatch. **All 30 divergent samples are S1000** (`is_s3000=False`),
declaring 40000 Hz:

    a drum sample  declared 40000  ours "played" 40000  theirs resolved 44100

`AkaiSample.sample_rate` means "the rate the sampler plays at", measured by
s3ked on an **S3000** (the index byte at 0x01 wins over the declared field,
in both directions, so it is not a default). We exclude S1000 deliberately:
byte 0x01 reads 1 in all 35 990 `.S1` headers of that corpus, so it carries
no information there, and using it would be an inference from the other
generation's machine. So for S1000 we fall back to the DECLARED rate.

**Which means we report 40000 as the rate the machine plays, and no S1000
machine can produce 40000.** mpc2emu now resolves it to 44100 -- also an
inference from the S3000 rule. Two answers, neither measured on an S1000.

Second-order, and the reason this is worth an entry rather than a shrug:
`rate_is_unplayable` is `declared not in PLAYBACK_RATES and declared !=
sample_rate`, and for S1000 our sample_rate IS the declared rate -- so the
check can never fire on S1000 material. The Detail pane's "N sample(s)
declare a rate this sampler cannot play" is structurally silent for a whole
generation, which is exactly the class of thing that warning exists to catch.

**ANSWERED WITH AN INSTRUMENT, hours later.** mpc2emu had already taken the
deciding measurement that evening without realising it was the deciding
case: one `.S1` bass sample declaring SSRATE 30000, loaded on the
S3000XL and played --

    AKAI note 79, measured              48.93 Hz
    predicted if it plays at 44100      48.94 Hz   (0.4 cents)
    predicted if it honours the 30000   33.30 Hz

So "report the declared rate" is the option the machine rules out. Our
`sample_rate` now resolves an `.S1` sample with an IMPOSSIBLE declared rate
to 44100, and only that case -- a playable-but-mismatched declared rate
still returns the declared figure, because the index carries no information
on that generation and nothing has measured which real rate wins.

Limits recorded in the code rather than smoothed over: the mechanism is
undetermined (on `.S1` the index reads 1 everywhere, so "index wins" and
"default to 44100 when impossible" predict the same number and the
measurement cannot separate them); "clamp to the nearest playable" IS
excluded, since it would have given 22050 for 30000; one file, one declared
rate, on an S3000XL rather than an S1000.

Both consequences verified on real discs: the two readers agree again
(`manual_akai_real_discs` passes, was 30 of 483 divergent), and the
structurally-blind guard fires -- `manual_akai_rate_snap` asserts it on 24
of 24 `.S1` samples declaring an impossible rate. That test exists because
an expression test would have PASSED: the guard's second clause was
unsatisfiable while the reader underneath returned the declared rate.
