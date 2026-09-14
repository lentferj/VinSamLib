# GUI test automation — where it stands and what is left

Written 2026-09-14 after a night's work with four sibling sessions
(mpc2emu, s3ked, k2kremote, eosed). Numbers here are measured, not
estimated; where something is unknown it says so.

The goal Jan set: **every input and output format, every conversion
option, no conversion at all, and the details of each format** — and at
the end, a set of images per device to verify by ear.

---

## The three numbers that matter

**1. The suite runs, and green now means green.**

    on this machine:      PASS 83   SKIP  0   FAIL 5   CRASH 1
    on another machine:   PASS 43   SKIP 10   FAIL 25  CRASH 11

The second row is the same suite with `HOME` pointed at an empty
directory — a real absence of Jan's library, not a simulated one.

**43 of 88 tests are genuinely portable. 10 declare a precondition and
skip honestly. 36 need the library and say so by breaking.** Those 36
are the work: each needs a `need(...)` declaring what it requires, so
that a machine without a corpus reports "could not check" rather than
"broken".

**2. 36 of 76 matrix cells have a test that claims them.** The other 40
are either real gaps or unmapped tests, and telling those apart is a job
that must not be guessed at.

**3. `tests/` stays gitignored.** Jan's decision, 2026-09-14. All 100
files are local to this machine and travel with nothing, deliberately.
What follows from that is set out under "Untracked, and what that costs"
— the short version is that the *record* has to live in tracked files,
because the tests cannot.

---

## What was built

| Piece | What it does |
|---|---|
| `tests/_status.py` | PASSED 0 / CHECKED_NOTHING 77 / FAILED 1 / CRASHED 99 |
| `tests/run_manual.py` | runs all, classifies, fails on a STALE exemption |
| `tests/expected_failures.txt` | `name: reason`; an entry without a reason is refused |
| `tests/_fixtures.py` | builds a minimal AKAI volume instead of finding one |
| mpc2emu `fixtures/` | six synthetic inputs with known faults, as generators |
| mpc2emu `docs/diagnostics.json` | 38 codes with `detail` keys, generated and asserted |
| `tools/matrix_coverage.py` | cells the document declares vs. cells tests claim |
| `tools/suggest_coverage.py` | proposes claims from evidence, never writes one |
| s3ked `QT_ASSERTION_STYLE.md` | what to assert in a Qt pane, and what not to |
| s3ked `docs/SONIC_CHECKLIST.md` | the listening pass, per device |

---

## The rules this night produced

Each was paid for by a real failure, which is why they are written down
rather than assumed.

**A fixture that cannot fail is not a test.** (k2kremote) Every synthetic
input must reproduce the fault it was built for, asserted.

**A precondition that cannot fire is not a precondition.** Four tests
opened with a guard against a missing folder that was, by then, built
rather than found. The guard could never fire and still read as
considered.

**An exemption that cannot expire is a permanently excused test.** The
runner fails when a declared failure starts passing.

**A test claims a matrix cell when it would FAIL if that cell broke** —
not when it merely touches it. `manual_debug_calllog` sets a trim, builds
an EMU3 CD and imports an XPM while asserting none of them; it claims
nothing.

**Assert what is rendered, not the state that produced it.** (s3ked) Their
focus test passed while the pane showed the wrong thing. Ours asserted a
model that was right while a row showed a wrong number.

**Never compute the expected value with the function under test**, and
compare the whole rendered string. A literal defeats the shared-oracle
problem; only an exact comparison defeats substring matching. `"1.8 MB"`
is `in` `"1.80 MB"`.

**A check adjacent to the thing it guarantees will pass while that thing
is false.** `ast.parse` accepted six files that `compile()` rejects.

**Test hardest the part you are most confident in.** (eosed) They wrote 21
evasion cases for the check they had reasoned about and four for the one
they had not — and the second produced the false positive. Every failure
in this night's work has that shape: the carefully-keyed memo was fine
and the loose assertion beside it was not; the pinned literal was right
and the substring match around it was not; the perturbation was designed
and whether it perturbed anything was assumed. **Confidence is where the
untested reasoning is.**

**When two guards protect the same thing, a test must say which one it
is testing.** The AKAI directory-entry ceiling is enforced twice — once
here with a message that explains programs and samples share the entries
and tells you to split the folder, and once in mpc2emu's writer with its
own. A test asserting only "something raised and the text contains 510"
passed with ours deliberately disabled, because theirs caught it.

Ours is the one under test: it is what the user reads and the only one
that says what to DO. Theirs is the backstop, and **a backstop that
quietly becomes the only guard is how the message a user sees degrades
without anything failing.** Found by disabling ours, not by reading the
test.

**A warning that fires where nothing is wrong destroys the ones that
matter.** (mpc2emu) They shipped `KRZ_NULL_STAGE_SPACED` with
`content_lost: true` on reasoning that turned out wrong in both halves,
and corrected it to `false` the same day — not because the flag was
harmless, but because "a `content_lost` that fires where nothing is lost
trains people to ignore the ones where something is".

That is the cost side of every rule above. A fixture that cannot fail, a
precondition that cannot fire, an exemption that cannot expire and a
coverage claim nothing defends all waste attention quietly; a false
alarm spends it loudly and takes the true alarms with it.

**A suite that only compares itself to itself cannot see a common-mode
error.** (s3ked, the hard way) They ran a hardcoded 44100 against a JACK
server at 48000; every check they had compared one of their own
measurements against another, so the error was invisible to all of them
and cost five withdrawn sections. What broke it was an *a priori* value
— the probe note's own frequency — sitting in the same capture the whole
time, recorded under a different question.

**This project has exactly that exposure and it is already written down.**
`roundtrip_krz_corpus.py` says so in its own second line: mpc2emu is
write-only for KRZ, so there is no independent reader to cross-validate
against and the test checks self-consistency instead — parse, assemble,
re-parse, compare. Every KRZ claim we make is our own reader agreeing
with our own writer. The E4B round trip is not in this position (it
validates against mpc2emu's independent parser) and the EIII one is
partly out of it.

So the KRZ strand is single, it is known to be single, and the only
external evidence is roughly six programs heard on hardware. That is not
a defect to fix today; it is the thing to remember before believing a
green KRZ result.

**Carry a comparison whose answer you already know.** (eosed) Then the
method's error is measured as a by-product, and a method too coarse for
the question announces itself rather than waiting to be asked about.
Their two knowns — two runs that must reach the same level, and a
no-attack control with nothing to measure — between them disposed of a
1.655× effect that did not exist, and neither required anyone to be
clever at the right moment.

Deliberately not "state your uncertainty first", which is what this
project first wrote down: **that version requires knowing to, which is
precisely the discipline an interesting result erodes.** The redundant
version works when nobody remembers the rule. It is the same instrument
as reintroducing a real bug to prove a check can fail, and as pinning a
literal rather than asking the code under test what it expects.

**Verify that a control actually removes what it claims to remove.**
Three attempts at "run without the corpus" did not remove the corpus:
only 3 of 88 tests read `$VINSAMLIB_CORPUS`, while 62 call `Path.home()`
directly. Two runs were reported as evidence before this was noticed.

---

## Driving the GUI as a user, not as a caller

Asked on 2026-09-14 whether the automation had actually been run, the
honest answer was no. The suite exercises GUI code — 20 tests build a
real `MainWindow` — but it does not operate the application:

    simulate real input (click/key):   2 of 88
    call a private handler directly:  30 of 88

Calling the handler skips the button, so **a button in the wrong state is
invisible**: the handler runs whether or not a user could have reached
it. That is exactly how Delete came to be enabled on a volume whose class
cannot delete — every test called `_delete_selected()`, which was guarded
and returned cleanly.

Two pieces answer it. `tests/_click.py` presses things and traps the
modals a synthetic click cannot otherwise answer. And
`manual_every_button_survives_a_press` is the generalisation: it knows
what no button means, and asserts the one property all of them share —
**a user can press it and the application does not raise** — across four
states and with confirmations both declined and accepted.

**Both were validated by reintroducing the real bug**, which is the only
way to know a check works:

    delete routed back through the VFS, as before mpc2emu's eac5d11
      manual_image_pane_clicks             FAIL  'AkaiVolume' has no 'delete'
      manual_every_button_survives_a_press FAIL  [yes] same

The sweep covers the image pane, New Bank and Pending; a fourth test
does the same for the main window, whose verbs are menu actions rather
than buttons and so need a different driver. Four tests, not thirty
conversions.

Each was validated by planting a real fault and watching it fail — a
delete routed back through the VFS, an `IndexError` in Remove Selected,
an `AttributeError` in a checkable menu action. **A check that has never
caught anything is not known to work**, and two of these did not until
they were made to.

The menu sweep's **skip list is its most important part**, and the
reasons in it are of exactly two kinds: the action waits for a human (a
native file dialog no synthetic trigger can answer) or it ends the
process. "Might do something inconvenient" is not a reason and must
never appear there — that is how an exemption list becomes the place
where inconvenient truths are filed. 9 of 15 actions are skipped, and
that ratio is itself the argument for the dialog seam: two thirds of
this window's verbs are untestable because of where they raise a
modal.

**Two flaws were found in the sweep itself, both by planting a bug
rather than by reading it.** Its first version **passed** the test. It declined every
confirmation, so it pressed the destructive button and stopped at the
question — the path the bug lived on never ran. A check that presses
everything and confirms nothing looks thorough and exercises the safe
half. It now answers both ways, and the `[yes]`/`[no]` tag says which
pass found a fault.

And the second flaw was worse: **an exception raised inside a Qt slot
does not propagate to the caller.** PySide6 prints the traceback and
routes it to `sys.excepthook`, so a `try/except` around
`QTest.mouseClick` never sees it. Planting an `IndexError` in "Remove
Selected" produced a traceback on stderr and a **PASSED** verdict. A
sweep whose entire purpose is "pressing this must not raise", which
cannot observe a raise, is decoration. `_click.SlotErrors` installs the
hook; with it the same plant reports:

    FAIL: 4 button press(es) raised:
       [no/newbank empty]        'Remove Selected' raised IndexError
       [no/newbank/no selection] 'Remove Selected' raised IndexError
       [yes/newbank empty]       ...
       [yes/newbank/no selection] ...

— naming the button, the exception and the states, and correctly staying
silent in the one state where a selection exists.

## An intermittent hang, observed once

`manual_ui_smoke_convert_preset` reported **TIMEOUT at 300.1 s** in the
run of 2026-09-14 12:05. It takes **3.4–3.5 s** in every other run
before and since, and passed three isolated runs and a full suite
afterwards. Recorded rather than dismissed, with what is known:

* **Not machine load.** Every other test in that run was within noise of
  the previous one — 41.6 s against 41.6, 71 against 70, 173 against
  140. Only this one moved, and it moved by a factor of 88.
* **Not obviously the day's change.** It converts to KRZ, and
  `content_lost` had just been un-filtered, which lets
  `KRZ_LPGATE_APPROXIMATED` reach the risk box — a modal, which in a
  headless run nobody answers. That is a plausible mechanism and it is
  **not evidence**; the same code passed four runs.
* **The shape has form here.** Worker threads plus a modal is exactly
  the combination that makes `QTest.qWait` segfault under PySide6, which
  is why this project has its own `qwait` shim.

It is written down because an intermittent hang in worker-and-modal code
is the kind of thing that gets called flaky and turns out to be a race.
k2kremote's TIMEOUT classification is what made it visible at all: as a
plain non-zero exit it would have been indistinguishable from a failed
assertion, and at 300 s it would have been read as a slow test.

**If it recurs, the next thing to do is capture a stack** rather than
reason about it further — `faulthandler.dump_traceback_later()` in the
harness would name the frame it is blocked in, which is the one fact
nobody has.

## What is left, in order

1. **Extend the press-sweep to the other panes** — New Bank, Pending
   and the main window. One test per pane, not 30 conversions: the
   sweep covers buttons nobody thought to test, which is where this
   class of bug lives.
2. **Declare preconditions for the 36.** Evidence-driven: the
   another-machine run names them exactly.
3. **Map the remaining 40 matrix cells.** `suggest_coverage.py` proposes;
   a person decides. Cells with no test at all become new tests.
4. **Fill Matrix H and J.** Every option at its boundaries, per target;
   the format details no cross-product reaches.
5. **The image set per device**, and the listening pass — the only stage
   that wants hardware, and deliberately outside the automation.

## A finding that needs a decision of its own

**48 modals are raised from 8 files, 24 of them in `main_window.py`.** A
modal reachable from anywhere is a modal no test can intercept — which is
exactly why the Delete-on-an-AKAI-volume bug could ask "this cannot be
undone", be answered yes, and then raise AttributeError with no test able
to get in front of it.

eosed's `manual_dialog_seam.py` measures it. (The 48 is the figure from
their pattern and from the narrowed one alike — narrowing `QDialog` out
changed the raise-site count by exactly zero, and an earlier note here
saying otherwise was a miscount. A base class is an `ast.Name` in
`ClassDef.bases`, never a `Call`, so it could not have been counted; the
false positive was in the IMPORT check, which cannot tell a modal
imported to be raised from one imported to be subclassed.) Routing all 48 through one
`vinsamlib/ui/dialogs.py` is a refactor with real regression risk and no
reported fault behind it, so it is not something to start unasked. Until
then the test is a **ratchet**: a new modal outside the seam fails it, and
so does removing one without lowering the baseline.

| file | raise-sites |
|---|---|
| `ui/main_window.py` | 24 |
| `ui/image_pane.py` | 11 |
| `ui/bank_pane.py` | 6 |
| `ui/pending_pane.py` | 2 |
| `ui/convert_options_dialog.py` | 2 |
| three others | 1 each |

## Untracked, and what that costs

Jan decided on 2026-09-14 that `tests/` stays ignored. Three things
follow, and they are not all bad.

**Any tree-scanning invariant must read the WORKING TREE, never
`git ls-files`.** eosed anticipated this and scoped theirs accordingly,
with the reason in the docstring; that choice is now permanent rather
than provisional. A tracked-set scan would pass cleanly over a tree
containing none of the 100 test files — the worst available result,
because it is indistinguishable from success.

**There is no net under the tests but the ones we make**, so it is a
command rather than a discipline: `tools/backup_tests.py` (tracked)
writes a zip outside the repository (untracked). Source only by default
— `tests/` is 37 MB of which 28 MB is Akai disc images that do not
change when a test is edited, and a backup too expensive to take is a
backup nobody takes. `--with-data` covers the other accident. k2kremote's
backup before their 88-file conversion was the only reason that pass was
safe to attempt.

**The tracked artifacts carry the record, so they have to be the honest
ones.** `docs/RELEASE_TEST_MATRIX.md`, this file, and `tools/` are what
survive a clone. That is why the matrix's stale AKAI section mattered
enough to correct rather than delete, and why `matrix_coverage.py`
reports a difference instead of a document claiming a number.

**It also reframes the portability work.** Running the suite with `HOME`
emptied looked like a question about other machines, which no longer
arises. It is not: a test that needs the corpus without declaring it is
broken *here* too — it simply always finds what it never asked for, and
would pass over nothing the day the NFS share is slow. The 36 that fail
or crash on an empty home are 36 tests whose preconditions are unstated,
and that is a defect on this machine, today.

**The harness stays local too.** Asked separately and answered the same
way: everything under `tests/` is local, machinery included. So the
mitigation is not to move it but to make it **reconstructible** — the
contract below is tracked even though the code is not, and
`tools/backup_tests.py` makes the snapshot a command rather than a
discipline.

### The harness contract, recorded here because the code is not tracked

If `tests/` is ever lost, this is what has to be rebuilt. It is written
down rather than remembered for the same reason the matrix is: the thing
nobody can reconstruct afterwards is the part that was obvious at the
time.

**Exit codes**, one per outcome, because two states cannot express the
difference between "did not run" and "ran and was fine":

| code | meaning |
|---|---|
| 0 | PASSED — ran, checks held |
| 77 | CHECKED_NOTHING — a precondition was absent, nothing was verified |
| 1 | FAILED — ran, something is wrong |
| 99 | CRASHED — could not complete; a failure, kept distinct so a reader sees which kind of red |

77 because automake and most CI runners already read it as "skipped".

**`need(condition, what)`** declares a precondition and raises `Skipped`,
which must never be an `AssertionError` — it must not be caught by a
handler written for failures, and must never come from a check that ran.

**`require(n, ...)`** raises `Vacuous` when a check ran against a sample
of zero. **`Vacuous` is a FAILURE and must never map to 77.** A test that
never started is not a test that looked at nothing and said yes; merging
them rebuilds, inside the reporting layer, the bug that let four real
defects survive a green suite.

**`expected_failures.txt`** is `name: reason`, and an entry without a
reason is refused at load — "known failure" is precisely what it
replaces. The runner exits non-zero when a listed test **starts
passing**, naming it as stale.

**Corpus and scratch roots come from the environment**
(`$VINSAMLIB_CORPUS`, `$VINSAMLIB_SCRATCH`), never from a literal home
directory.

Nothing is pushed. 111 commits are local and unreviewed.
