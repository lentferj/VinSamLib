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

**3. `tests/` is gitignored.** All 100 files are local to one machine and
travel with nothing. This is the largest structural hole and it gates
much of the rest — see "The decision that is Jan's".

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

**Verify that a control actually removes what it claims to remove.**
Three attempts at "run without the corpus" did not remove the corpus:
only 3 of 88 tests read `$VINSAMLIB_CORPUS`, while 62 call `Path.home()`
directly. Two runs were reported as evidence before this was noticed.

---

## What is left, in order

1. **Declare preconditions for the 36.** Evidence-driven: the
   another-machine run names them exactly.
2. **Map the remaining 40 matrix cells.** `suggest_coverage.py` proposes;
   a person decides. Cells with no test at all become new tests.
3. **Fill Matrix H and J.** Every option at its boundaries, per target;
   the format details no cross-product reaches.
4. **The image set per device**, and the listening pass — the only stage
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

## The decision that is Jan's

Tracking `tests/` puts ~100 files in the repository. Everything above
assumes it: eosed's repo-invariants scan reads `git ls-files` and would
otherwise pass cleanly over a tree containing none of the tests;
k2kremote's backup directory exists only because there is no other net;
and a test improvement made tonight could not be committed at all.

Nothing is pushed. 16 commits are local and unreviewed.
