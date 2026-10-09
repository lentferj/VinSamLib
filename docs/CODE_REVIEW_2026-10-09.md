# Full code review — 2026-10-09

Every finding below was read in source and, where marked **[P]**, reproduced
with a runnable check under `.venv/bin/python` before being believed. Where a
claim from the parallel deep-dives did **not** survive that check it is marked
**[R] (refuted)** with the evidence, because a review is a claim about the
code and the code is the authority.

Reproduction scripts were run against the working tree at `master` `0f858bc`
with an isolated `XDG_DATA_HOME`/`XDG_CONFIG_HOME` and `TMPDIR`, except where
noted. The real user index was never written; the `manual_ui_smoke_search`
guard described in H6 is what makes that safe.

**Status: findings only, no code changed in this pass.**

Reproductions live in `/tmp/opencode/v_*.py` (scratch, not tracked):

| script | proves |
|---|---|
| `v_beginc.py`    | C1 — `begin_container()` returns an item rowid |
| `v_e4b.py`       | H2 — stray/truncated `EMSt` drops a bank |
| `v_atk.py`       | H3 — velocity→attack polarity is inverted |
| `v_debug.py`, `v_min.py`, `v_variants*.py` | H1 — Worker/pane retention, and the fix that does *not* work |
| `v_images.py`    | H4 — `iso_builder.auto_hda_size_mb` does not exist |

---

## CRITICAL

### C1 — `index/db.py:267` — `begin_container()` returns a stale **item** rowid, not a container id

```python
cur = self._conn.execute(
    "INSERT INTO container(path, kind, format, size, mtime, scanned_at, error) "
    "VALUES (?, ?, ?, ?, ?, NULL, NULL) "
    "ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, format=excluded.format, "
    "size=excluded.size, mtime=excluded.mtime, scanned_at=NULL, error=NULL",
    (path, kind, format, size, mtime),
)
container_id = (
    cur.lastrowid
    or self._conn.execute("SELECT id FROM container WHERE path = ?", (path,)).fetchone()[0]
)
```

**Mechanism.** Python's `lastrowid` comes from `sqlite3_last_insert_rowid()`,
which an `INSERT … ON CONFLICT DO UPDATE` that takes the **UPDATE** path does
not touch. The previous statement on that connection is `add_item()`, so the
upsert returns the rowid of the last **item** inserted. The `or …` fallback
only fires when it is `None`/`0`, so a truthy stale item id is returned as the
container id.

**Reproduced** (`/tmp/opencode/v_beginc.py`). Two containers with one item
each, then both rescanned with changed size/mtime:

```
V pass1 containers: [(1, '/a.e4b'), (2, '/b.e4b')]
V pass1 items     : [(1, 1, 'P'), (2, 2, 'P')]
V re-registered '/a.e4b' -> begin_container returned 2
V re-registered '/b.e4b' -> begin_container returned 2
V final items: [(1, 1, 'P'), (2, 2, 'P2')]
V search('P')  : ['/a.e4b', '/b.e4b']
V search('P2') : ['/b.e4b']
```

`begin_container('/a.e4b')` returned **2** — `/b.e4b`'s id — so the
`DELETE FROM item WHERE container_id = 2` wiped **`/b.e4b`'s** items and
`/a.e4b`'s stale row was left in place. `search('P')` now returns the same
preset under two different files, and `/a.e4b`'s `scanned_at` stays `NULL`
forever.

**Consequence on the real library.** Item rowids run to 99 251 while container
ids stop at 8 167, so after the first ~8 167 item inserts every stale id
exceeds the container-id range and the mismatch raises
`sqlite3.IntegrityError: FOREIGN KEY constraint failed` from `add_item()`,
which `scan()` does not catch — the worker reports "Scan failed" and the index
is left half-built. Below that threshold it fails silently instead.

**Triggered by any rescan**, any touched file, or a schema bump — and the
`mtime = 0` migration shipped in `20cbc8d` makes *every* container take the
conflict path at once, so this is now on the ordinary post-bump start.

**Fix.** Never rely on `lastrowid` across an upsert:

```python
cur = self._conn.execute(
    "INSERT INTO container(path, kind, format, size, mtime, scanned_at, error) "
    "VALUES (?, ?, ?, ?, ?, NULL, NULL) "
    "ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, format=excluded.format, "
    "size=excluded.size, mtime=excluded.mtime, scanned_at=NULL, error=NULL "
    "RETURNING id",
    (path, kind, format, size, mtime),
)
container_id = cur.fetchone()[0]
```

SQLite ≥ 3.35 is required for `RETURNING`; this venv has 3.40. The fallback
for an older SQLite is to `SELECT id FROM container WHERE path = ?` first and
branch explicitly, asserting the row exists.

**A regression test is required**: scan once, mutate one file's size/mtime,
scan again, and assert every `item.container_id` resolves to the container
whose path matches. No test in the suite re-registers an existing container
today — verified by grep: `needs_rescan|begin_container|_migrate` appear in
zero files under `tests/`. That is why this shipped.

---

## HIGH

### H1 — `ui/workers.py:70` — every `Worker` that has run is retained for the process lifetime

```python
self.setAutoDelete(False)
```

**Two separate effects, and the first one is not the one reported.**

**(a) Every Worker object is retained.** A `Worker` with **no connections at
all** survives a full GC (`/tmp/opencode/v_min.py`):

```
J no connections at all -> worker alive: True  (want False)
```

`setAutoDelete(False)` means the pool never destroys the C++ `QRunnable`, and
under Shiboken the C++ object owns the Python wrapper, so nothing releases it.
This is independent of any closure.

**(b) The 16 completion lambdas additionally pin the owning pane.** The idiom
repeated verbatim at bank_pane 1251/1254/1292/1295, detail_pane 348/351,
models 1419/1422/1505/1508, pending_pane 860/863, samples_pane 239/242/346/349:

```python
lambda *_: self._live_workers.remove(w) if w in self._live_workers else None
```

`gc.get_referrers` on a retained pane shows exactly one referrer — a `cell`,
i.e. a closure cell of a lambda. The lambda is held by the C++ connection list
inside `WorkerSignals`, which is invisible to CPython's cyclic GC, so the
cycle `Worker → WorkerSignals(C++) → connection → lambda → cell[pane]` cannot
be collected. At `models.py:1419` and `models.py:1499` the closure *also*
captures `rows=list(todo)` — an entire fetched tree listing.

**Measured** (`/tmp/opencode/v_leak3.py`), 200 workers through the real
idiom:

```
L pane._live after all finished = 0
L reachable Pane objects, pane dropped : 1  (want 0)
L reachable Worker objects, dropped    : 200  (want 0)
```

The `_live_workers` list empties correctly and the cycle keeps everything
anyway. `tracemalloc` measures ~1.3 kB per retained Worker before counting
the captured closures.

**Consequence.** A session that expands folders and clicks through presets
irreversibly retains the whole `MainWindow` object graph — the five panes,
the node tree, the `Config`, and a live SQLite connection. Any path that
builds and discards a `MainWindow` (tests, `tools/make_screenshots.py`, a
future second window) leaks the entire window.

**The proposed one-line fix does not work.** I tested it:

```python
QMetaObject.invokeMethod(self.signals, "deleteLater", Qt.ConnectionType.QueuedConnection)
```

```
F reachable Pane objects  : 1  (want 0)
F reachable Worker objects: 200  (want 0)
```

Unchanged. `disconnect()` after the emit frees the **pane** (the C++→lambda
arrow is gone) but not the workers:

```
B disconnect() after emit          panes=  0 workers= 50
F weak pane only                   panes=  0 workers= 50
G disconnect only                  panes=  0 workers= 50
H weak pane + disconnect           panes=  0 workers= 50
```

None of the four reachable variants frees the workers, because the C++
`QRunnable` itself still exists.

**What I verified works structurally**: giving the signals bridge a parent in
the **receiver** and letting the pool own the runnable (`autoDelete(True)`)
frees the workers —

```
R owned-by-pane                            pane_alive=True  workers_alive=  0
```

That is the direction `workers.py`'s own ownership comment already gestures
at ("ownership needs to be Python's alone, never Qt's"), and it is the honest
fix — but it is a redesign of the shared worker idiom, not a one-liner, and
it must be checked against the documented `autoDelete(True)` race that
comment describes: a worker outrunning `closeEvent`'s bounded 3 s wait emits
into a deleted C++ object.

**Interim mitigation, proven**: stop capturing `self` strongly in the
completion slot (a `weakref.ref` beats nothing) — that alone releases the
pane and its `IndexDB`.

---

### H2 — `banks/e4b.py:667` — a 4-byte trailing `EMSt` removes the whole bank from the index

```python
emst_pos = data.rfind(EMST_TAG)
if emst_pos < 20 + toc_chunk_size:
    warnings.append("no EMSt (master setup) chunk found")
else:
    emst_size = struct.unpack_from(">I", data, emst_pos + 4)[0]   # unguarded
```

`rfind` is bounded only by `len(data) - 4`, so a stray 4-byte `EMSt` anywhere
at the tail passes the offset test and then reads 4 bytes **past EOF**. The
next line deliberately tolerates a truncated *body* (`emst_end = min(...)`),
so the missing guard on the *header* is an inconsistency rather than a design
choice — and the comment above explicitly names the population at risk:
*"a handful of real commercial banks in this project's library have trailing
bytes past the nominal end (CD/HD image padding)"*.

**Reproduced** (`/tmp/opencode/v_e4b.py`), a minimal valid `FORM E4B0` body
with the tail varied:

```
V clean                 OK  presets=0 warnings=["skipped unrecognised chunk tag b'E4MA'", 'no EMSt (master setup) chunk found']
V +stray 'EMSt'         CRASH error: unpack_from requires a buffer of at least 28 bytes for unpacking 4 bytes at offset 24 (actual buffer size is 24)
V +'EMSt'+1 byte        CRASH error: unpack_from requires a buffer of at least 28 bytes for unpacking 4 bytes at offset 24 (actual buffer size is 25)
V +truncated EMSt hdr    OK  presets=0 warnings=["chunk b'EMSt' at 20 claims size 16, which runs past the end of the file — stopping scan", ...]
```

The file was **4 bytes short of complete** — the partial-download and
image-padding shapes this parser already claims to handle for chunk bodies.

**Consequence.** `index/scanner.py:560-564` wraps `parse_bytes` in
`except Exception: return ("E4B", None)`, so the bank silently vanishes: no
row, no warning, no partial listing. `e4b.parse()` never reaches the graceful
`_walk_chunks_physical` "runs past the end of the file" path.

**Fix.**

```python
emst_body = b""
emst_pos = data.rfind(EMST_TAG)
if emst_pos < 20 + toc_chunk_size or emst_pos + 8 > len(data):
    warnings.append("no EMSt (master setup) chunk found")
else:
    ...
```

and additionally bound the `rfind` to the walked FORM extent so a coincidental
tag in trailing padding cannot be chosen at all.

---

### H3 — `audition/envelope.py:250` — velocity→attack has the sign backwards

```python
scale = 1.0 + float(span) * (velocity - int(pivot)) / 126.0
return max(0.0, scale)
```

The model's unit is `velocity_to_amp_attack_span = t(velocity 1) / t(velocity
127)`, and mpc2emu states it plainly at `models/common.py:6254`:

```
# THE UNIT IS THE FULL VELOCITY SWEEP: attack time at velocity 1 divided by
# attack time at velocity 127. Greater than 1 means hard notes attack FASTER,
# which is the common case by a wide margin.
```

confirmed by the measured table at `models/common.py:6280` (s3ked §47):

```
V_ATT1   vel 1     vel 64    vel 127     span = t1/t127
  -50    2.955     0.555     0.010          295.5
  -25    2.955     0.545     0.025          118.2
```

So **velocity 127 must get the shortest attack and velocity 1 the longest.**
The renderer does the exact opposite. Reproduced
(`/tmp/opencode/v_atk.py`) with `span=2.0, pivot=64`:

```
velocity   1 -> attack_scale  0.000  (attack time is 0.00x the authored)
velocity  32 -> attack_scale  0.492
velocity  64 -> attack_scale  1.000
velocity  96 -> attack_scale  1.508
velocity 127 -> attack_scale  2.000   ← must be 0.5x, is 2.0x
```

**Compounding defect at `render.py:696`.** The caller passes
`attack_scale=attack_scale if attack_scale else 1.0`, so the legitimate `0.0`
floor is silently replaced by the authored time — which is why velocity 1
above gets 50 ms rather than the 0 ms the (wrongly signed) model asked for.
The guard is also wrong on its own terms: a 0 ms attack is a legitimate
answer, and the caveat test three lines above already handles `None`.

**Fix.** Right-signed and monotone at both endpoints:

```python
scale = 1.0 + (float(span) - 1.0) * (int(pivot) - velocity) / 126.0
```

or better, import and use `akai_attack_time_at_velocity_127(attack, span)`
(`models/common.py:6471`) so the measured anchor points are used rather than
a re-invented line. And `attack_scale if attack_scale is not None else 1.0`.

**Regression test**: assert
`attack_scale_for(span=2.0, v=127) <= 1.0 <= attack_scale_for(span=2.0, v=1)`.

---

### H4 — `build/images.py:184`/`:191` — EMU3 hard-disk creation with "auto" size is unconditionally broken

```python
elif kind == "emu3_hd_emu":
    sz = size_mb or (iso_builder.auto_hda_size_mb(bank_paths, "emu") if bank_paths else 1024)
...
elif kind == "emu3_hd_fat":
    sz = size_mb or (iso_builder.auto_hda_size_mb(bank_paths, "fat") if bank_paths else 1024)
```

`auto_hda_size_mb` lives in `writers/hda_builder.py:551` and has only ever
lived there — `git log --all -S "def auto_hda_size_mb" -- writers/` resolves
to mpc2emu's initial commit, which added it to `hda_builder.py`, and there is
no re-export from `iso_builder`. Because `iso_builder` is a `_Lazy` proxy the
miss surfaces only at attribute access inside the arithmetic, never at import.

**Reproduced** (`/tmp/opencode/v_images.py`) against the configured checkout:

```
T iso_builder has auto_hda_size_mb : False
T hda_builder has auto_hda_size_mb : True
T images.py: iso_builder.auto_hda_size_mb(bank_paths, "emu") if bank_paths else 1024
T images.py: iso_builder.auto_hda_size_mb(bank_paths, "fat") if bank_paths else 1024
```

With `size_mb=None` ("auto") and at least one bank queued, both branches raise
`AttributeError`, caught at `images.py:221` and re-raised as
`ImageOpError("module 'writers.iso_builder' has no attribute 'auto_hda_size_mb'")`
— a raw Python module error shown to the user as the failure reason. No
partial file is left behind (`_cleanup_partial` runs first). `size_mb=None`
is the **initial** state of the spin box (`ui/image_pane.py:1032`), so this
is the most ordinary path, and both kinds are offered in the GUI at
`ui/image_pane.py:129-130`.

**Fix.** `hda_builder` is already imported on line 36 and used at 187/194, so
the fix is the two words after `iso_builder`.

---

## MEDIUM

### M1 — `audition/render.py:1318` — `spans` is computed and discarded; every forked child allocates the whole render bus

```python
spans = [min(total_frames - s, total_frames) for s in offsets]  # noqa: F841
_FORK_PLANS = plans
_FORK_ARGS = (total_frames, use_np)
```

`total` in `_render_event_task` is the length of the **whole audition**, not of
the event that child is rendering. Each of N children allocates
`2 × N_frames × 8` bytes, each is pickled back through a pipe, and the parent
then holds all N of them at once (`pool.map` collects before returning).

**Measured** (24 events, 3 s holds + 0.25 s gaps, 4 workers):

```
bus = 90 s of audio (3957975 frames)
  every child allocates the WHOLE bus: 24 x 2 x 3957975 x 8 B = 1.52 GB
  actually needed (each event sized to itself)          = 52 MB
parallel render 9.09s; peak child RSS seen 1590 MB
```

**1590 MB against 52 MB actually needed — a 30× blow-up, scaling as
O(events × bus length).** A 60-second note list of 30 notes is an OOM risk on
any modest machine, and the `except Exception: return False` fallback then
silently re-renders the whole thing serially, tripling wall-clock time.

**Fix.** `spans[i]` is exactly the room event *i* occupies, and it is already
computed on the line above and thrown away. Pass it in `_FORK_ARGS` and use
`total = spans[index]` in `_render_event_task`. `_mix_into` already handles
short buffers (`n = min(len(left), total - start)` at `render.py:1289`), so
nothing downstream changes. Delete the `# noqa: F841` with it.

### M2 — `audition/render.py:1319-1320` — `_FORK_PLANS`/`_FORK_ARGS` are unlocked module globals

`render()` sets the globals and then forks. Nothing protects the window
between the assignment and `ctx.Pool(...)`, and two renders genuinely overlap
— the code says so itself at `render.py:215-222` (*"Two auditions really can
render at once"*) and `ui/main_window.py:_silence_previous_auditions`
documents it (*"An audition can be twenty seconds long … asking for the next
one while the last is still sounding"*), while `self._audition_worker` is
simply overwritten by the next request.

**Proved** with two different banks rendered concurrently (300 Hz vs 3000 Hz),
assignment→fork window widened to 1.2 s: `A == reference A : False` — bank A's
children inherited **B's** `_FORK_PLANS` and rendered B's first three notes
into A's bus. Because A had 3 plans and B 4, the mismatch can also raise
`IndexError` in the child, `pool.map` re-raises, `except Exception: return
False` — and the parent silently re-renders serially.

**Fix.** Give each pool its own snapshot instead of writing module globals:

```python
def _fork_init(plans, args):
    global _FORK_PLANS, _FORK_ARGS
    _FORK_PLANS, _FORK_ARGS = plans, args

with ctx.Pool(processes=workers, initializer=_fork_init,
              initargs=(plans, (total_frames, use_np))) as pool:
```

`initargs` is passed directly under fork, so nothing large is pickled and the
copy-on-write design is preserved. A module lock around the whole body is an
acceptable alternative and serialises concurrent parallel renders at no
audible cost.

### M3 — `audition/render.py:1349-1353` — `_finish` scans the bus with a Python `for` loop, 46 % of an unfiltered render

```python
peak = 0.0
for v in out_l:
    peak = max(peak, abs(v))
for v in out_r:
    peak = max(peak, abs(v))
```

This runs **before** the `if _np is not None` branch, so with numpy installed
it iterates a numpy array element by element — `max` and `abs` on numpy
scalars. Measured:

```
_finish peak loop (python over ndarray) 200000 frames:  85.9 ms
                          np.abs().max():               1.4 ms   (60x)
_finish peak, 30 s stereo:                           1139 ms
```

and on a real render (8.5 s of stereo audio, unfiltered): `_finish` cumulative
**0.610 s of the 1.314 s render = 46 %**, of which ~0.27 s is the peak scan.

**Fix.** Hoist the numpy branch above the peak, or branch on the container:

```python
if _np is not None:
    peak = float(max(_np.abs(out_l).max(), _np.abs(out_r).max()))
else:
    peak = max(max((abs(v) for v in out_l), default=0.0),
               max((abs(v) for v in out_r), default=0.0))
```

### M4 — `audition/render.py:309` — the cached sample is re-converted from a Python list on every voice, every channel

`_read_block_np` does `data = _np.asarray(ch, dtype="float64")` once **per
voice per channel**. `_Source.channels` is a list of Python floats by design
(see the measured comment at `render.py:160-167`), so with L layers over M
notes that is `2 · L · M` conversions of the same data.

**Measured**: `np.asarray` on a 264 000-element list is 7.0 ms; in the 8.5 s
render `numpy.asarray` was **0.409 s of 1.314 s = 31 %** for 167 calls.

**Fix.** Cache the array next to the lists, lazily, in `_Source.__slots__`.
The extra 8 bytes/frame is cheaper than the ~32 bytes/frame the Python list
already costs, and the pure path is untouched.

### M5 — `ui/explorer_pane.py:306` (and 81, 323, 502) — search results re-parse whole banks from disk on the GUI thread

`search_resolve.resolve_result(hit)` re-opens the container and re-parses it:
`_resolve_loose_bank` does `Path.read_bytes()` + a full E4B/KRZ parse,
`_resolve_in_image` does `open_volume` + `vol.read` + parse,
`_resolve_project_program` runs `xpm_import.parse_mpc`, whose own docstring
says *"loads every referenced WAV, so a project can pull tens of MB"*.
`_show_context_menu` (line 505) then walks every node again calling
`_krz_rom_only` / `_krz_missing_audio`.

**Proven** against a real `MainWindow` with a 31.6 MB / 60-preset E4B:

```
right-click with 60 search results selected: 2 145 ms of frozen window
clicking one result: 35 ms
drag-selecting 60 results and dragging them out: same 2.1 s before the drag starts
```

Commercial E4Bs are larger, so the freeze scales linearly. The
`mpc_program` path is code-verified but unverified at runtime — no `.xpj`
fixture could be produced.

`bank_pane.py:798-800` already documents the root cause: *"`search_resolve.resolve_result()` has no cache"*.

**Fix.** Add an LRU keyed on
`(container_path, st_mtime_ns, st_size, kind, native_id)`. Better, have
`_show_context_menu` ask `resolve_result` only for what it reads, and run it
in a `workers.Worker` like every other parse in this app.

### M6 — `index/db.py:535` — `search()` builds one chain per hit (N+1), and `search_page()` runs the match twice

`search()` does `chain=self._chain_for(item_id)` for every hit, and
`_chain_for` is a `while` loop of one `SELECT` per ancestor level. A 1000-hit
page costs ~2 000–3 000 SQLite round trips on the GUI thread, then `count()`
runs the identical FTS `MATCH` a second time.

**Proven** on a purpose-built 112 000-item / 8 000-container index (the README
documents the real one as 99 251 items / 8 167 containers):

```
db.search_page('bass') -> 1000 hits of 96000: 343 ms on the GUI thread
```

The Explorer search is debounced at 200 ms, so this recurs every ~0.5 s while
typing. The chains are consumed only by `_format_hit` (`chain[:-1]` names) and
by `search_resolve` for the *selected* hit — 1 000 of them are built so ~25
rows can be painted. Same finding as the N+1 in `search()`.

**Fix.** One recursive CTE over the page's `item_id`s, then assemble the
chains in Python by id. Expect 343 ms to fall into single-digit ms.

### M7 — `index/scanner.py:334` — `foreign_import.inspect()` runs before the `needs_rescan()` guard

`_scan_foreign_container` calls `inspect(path)` at line 334; the skip check is
at 342. `inspect()` opens and parses the file (for SF2/GIG it calls
`list_presets`). Measured 0.1–1.1 ms per file; the module's own figure is
*"the whole corpus of 5 977 files here classifies in about 1.4 s"*, so an
unchanged rescan repays **≈1.4 s** for containers it then skips.

**Fix.** Move the `mtime`/`needs_rescan` block above `inspect()`, keeping
`seen_paths.add()` after the verdict.

### M8 — `index/scanner.py:466`/`:476` — each AKAI volume is listed twice, and a file count is reported wrong

`_scan_vfs_listing` calls `vol.volume_programs(e)` (which lists the volume to
find programs) and then `vol.list(e)` again to build `sample_bytes`. Each
listing re-walks the FAT chain of every file; `volume_programs` additionally
opens the image **once per program** — measured 54 `read()` calls for 54
programs.

Per-file listing cost measured at 16.9 µs/file, so on the documented 49 984-file
AKAI corpus the duplicate pass costs ≈**0.84 s per scan**, and it is 39 % of
the `volume_programs` step's time.

The same root cause produces a **user-visible wrong number** at
`vfs/akai.py:504-505`: `self._truncated` accumulates and is never reset, so a
truncated-disc warning reports **92** files where the real count is 46.

**Fix.** Have `volume_programs()` return `(programs, sample_bytes)` from its
single existing listing — the sample sizes come from the directory entries it
already has — and reset the counters at the top of `_walk_chain`/`_list_volumes`.

### M9 — `ui/pending_pane.py:527` — `_refresh()` re-summarises every preset of every queued bank on the GUI thread

`_entry_audio` calls `summary.summarize_preset(...)` per item per entry with
**no memo**, and `_refresh` (line 386) runs on every add, rename, delete,
reorder, partition-break toggle and contents edit. The memoised `_preset_audio`
sits 22 lines above it (507) and is used only by the Contents list.

**Proven**, 5 queued banks of 60 presets / 30 MB of audio each:

```
>>> pending_pane._refresh() with 5 queued banks: 408 ms of frozen window
>>> one rename -> _refresh(): 410 ms
```

Renaming one bank in a 5-bank queue freezes the window ~0.4 s; a 20-bank queue
freezes it ~1.6 s. The work is identical every time — nothing about the queue
changed.

**Fix.** Memoise `_entry_audio` on `(id(bank), preset identity)` exactly as
`_preset_audio` does, and prune entries whose bank has left the queue.

### M10 — `ui/convert_options_dialog.py:713`/`:789` — the full parse runs on the GUI thread

`_on_test_clicked` and `_on_accept_clicked` call `self._bank_loader()`
synchronously. The loaders' own docstrings say not to do this:
`xpm_import.load_samples_for_test` → *"**Not cheap**: it loads every
referenced WAV, so a project can pull tens of MB"*;
`foreign_import.load_samples_for_test` → *"**Not cheap.** This loads every
sample: one 1 GB SoundFont here costs 2.7 s and about 3 GB of RSS. Browsing
must not call it -- ... callers that do should be on a worker."*

**Consequence.** Clicking **Test** — or just pressing **OK** with Mono=Mix and
no prior Test — blocks the GUI thread for a full parse, and for a SoundFont
source allocates ~3 GB RSS on the GUI thread's watch. There is no worker and
no busy indicator; the window simply stops repainting.

**Fix.** Move the loader onto `workers.Worker` with the button disabled and a
message shown while it runs, delivering samples back through a signal.
`_on_accept_clicked` needs the risk before it can accept, so that path
specifically must become async (disable OK, run the check, then `accept()`
from the finished slot).

### M11 — `vfs/localdir.py:69` + `index/scanner.py:131` — a symlink cycle crashes the whole scan with `RecursionError`

`child.is_dir()` follows symlinks, so `_scan_directory` recurses forever down
`/lib → /lib/sub/loop → /lib …`. **Proven** at `sys.setrecursionlimit(80)`:
`RecursionError` propagates out of `scan()`, which catches nothing.
`ui/models.py:484-490` already solves this — *"Following would make a
symlinked directory look like a file… which also means a loop cannot hang it"*
— by using `is_dir(follow_symlinks=False)`. `vfs/localdir.py` is the odd one
out, and the two subsystems now disagree on what a directory is.

**Fix.** `child.is_dir(follow_symlinks=False)` in `LocalDirVolume.list()`,
matching `ui/models.py`; or track real-paths in a `visited` set in
`_scan_directory`. Either way, widen `_scan_directory`'s `except OSError` so a
`RecursionError` does not kill the scan.

### M12 — `ui/main_window.py:837-850` — a second audition request orphans the first progress window, and its Cancel closes the wrong one

`_begin_audition_progress` assigns `self._audition_progress = dialog`
unconditionally, without closing the previous one. `_on_audition_ready` (913)
returns early for a superseded generation (914) — correctly, per its own
docstring — so nothing ever closes the previous dialog. The stale dialog is
still shown and still has a live `cancelled` signal wired to
`_cancel_audition(genA)`, which calls `_end_audition_progress()` — closing
**the current** window.

**Proven**, `_audition_node()` called twice while the first render is in
flight:

```
>>>   AuditionProgress instances alive: 2
>>>     window text: 'Preparing audition of BIG00…'   <- never closed, never updated
>>>     window text: 'Preparing audition of BIG01…'
>>>   self._audition_progress points at: 'Preparing audition of BIG01…'
>>> after both renders finished:
>>>   AuditionProgress instances still alive: 2
```

A stuck "Preparing audition of X…" window with a dead Cancel button that, when
clicked, closes the *other* audition's progress window and reports
"Audition cancelled" for the wrong render. `_audition_node`/`_audition_staged`
never check `self._audition_worker is not None`, so this is reachable by
simply right-clicking a second preset while the first is rendering.

**Fix.** Close/hide the outgoing dialog before creating the new one, and
either key the progress window by generation or disconnect `cancelled` from
the superseded one.

### M13 — `audition/render.py:489-515` — LFO→**pitch** is neither modelled nor reported

`_lfo_terms` reads only `cents, q, pan, vol`; `_lfo_caveats` enumerates
"delay, fade-in, key-sync, tempo-sync". `lfo1_to_pitch` / `lfo2_to_pitch` are
real fields on the model. A voice whose only LFO routing is pitch gets
`_lfo_terms() → None`, so the note is unmodulated **and** no caveat mentions
that pitch modulation is missing.

**Proved**: `lfo1_rate=5.0, lfo1_shape="sine", lfo1_to_pitch=100.0` produces
caveats `['output stage', 'interpolation', 'source', 'release', 'velocity → volume']`
— no LFO line at all. Given the package's stated rule ("caveats are appended
where the approximation is applied") this is an honesty gap, and the number of
voices affected is unknown and unmeasured.

**Fix.** Treat `lfo1_to_pitch`/`lfo2_to_pitch` as a destination that forces
`terms` non-`None` with zero audio effect, and add a `NOT_MODELLED` note naming
pitch. (Actually modelling it is a two-line `_pitch_ratio` multiply, but that
is a feature decision.)

### M14 — `banks/summary.py:155-165` and `:589-601` — E4B/EIII summary rebuilds the bank, writes a temp file and re-parses it, for every preset on every scan

```python
data = e4b.assemble([(bank, preset)])
with tempfile.NamedTemporaryFile(suffix=".e4b", delete=False) as tmp:
    tmp.write(data); tmp_path = tmp.name
parsed = e4b_parser.parse_e4b(tmp_path)
```

Callers that hit this per preset: `index/scanner.py:591`
(`_index_bank_presets`, every preset of every E4B/EIII bank in a scan),
`ui/models.py:381`, `ui/bank_pane.py:1111`, `ui/samples_pane.py:235`,
`ui/pending_pane.py:521/556`, `ui/detail_pane.py:114`,
`ui/main_window.py:1846`.

**Measured** (synthetic bank, `.venv`, mpc2emu on `sys.path`):

| bank | presets | result |
|---|---|---|
| E4B, 12.6 MB (24 samples × 512 KB) | 120 | **0.45 s**, 188 MB of audio touched = **15× the bank's own audio**, 120 temp files created/parsed/deleted |
| EIII, 1.6 MB | 30 | 0.066 s, 7 864 440 bytes = **5× the bank's own audio**, 30 temp files |

Extrapolating to a single real 75.8 MB / 700-preset bank: ~1.1 GB of
temp-file writes and parses, one file create+unlink per preset, for a figure
used to fill one column. mpc2emu's `e4b_parser` unconditionally `print`s
`Parsing E4B: <tmp>` per call, so a scan also prints one line per preset to
stdout.

**Fix.** Read the zone model straight out of the already-parsed container,
exactly as the KRZ and AKAI paths do. The data is all in `preset.zone_refs` /
`preset.body`: key range at `body[v_start + 14]`, `body[v_start + 17]`,
`body[eo + 2]`, `body[eo + 5]` (the `VOICE_LO_KEY`/`VOICE_HI_KEY`/
`ZONE_LO_KEY`/`ZONE_HI_KEY` constants already exist at e4b.py:94/72); root at
`body[eo + 14]`; velocity at `body[v_start+18]`/`body[v_start+21]`; sample name
from `bank.samples[idx].name`; rate from `e4b.sample_rate(samp)`; bit depth 16;
loops from the existing `e4b.sample_loops`. `zones_above_playback_ceiling`
(e4b.py:327) is already a correct direct walk and is the template to follow.
If the mpc2emu route must stay, memoise `(id(bank), preset.index) -> PresetSummary`
so the Detail pane and the scanner share one result.

### M14b — `banks/summary.py:157-165` and `:593-601` — the summary temp file leaks on write failure

```python
with tempfile.NamedTemporaryFile(suffix=".e4b", delete=False) as tmp:
    tmp.write(data)          # ENOSPC here -> raises
    tmp_path = tmp.name      # <-- only assigned AFTER the write
finally:
    if tmp_path is not None: Path(tmp_path).unlink(missing_ok=True)
```

**Reproduced** (real `NamedTemporaryFile`, `write` raising `ENOSPC`):

```
write failed as expected: 28 No space left on device
LEAKED temp files: ['/tmp/opencode/tmp0o4n8stb.e4b']  exists: True bytes: 5
```

A 128 MB assembled bank written to a small `/tmp` (a tmpfs is exactly the
wrong place for this) hits `ENOSPC` easily; every call then orphans a partial
file, which makes the disk-full worse, and the exception propagates into
`except Exception: return None` at the callers, so the audio figure silently
vanishes instead of reporting a real error.

**Fix.** Assign `tmp_path = tmp.name` before any write, inside the `with`.

### M15 — `build/convert.py:276` and `build/images.py:98` — `redirect_stdout` is a process-global mutation under a 12-thread pool

```python
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):   # thread A blocks for 40s here
        result = fn(*args, **kwargs)
```

`redirect_stdout.__enter__` does `sys.stdout = ...` and `__exit__` restores
whatever `sys.stdout` was *at that thread's* entry. Conversions run on
`QThreadPool.globalInstance()`, whose `maxThreadCount` is **12** on this
machine (nothing in the codebase sets it — `grep setMaxThreadCount` → no hits)
and which demonstrably starts a second `Worker` while the first is still
blocked. Several Workers reach `_run_captured`: `main_window.py:1055` (XPM
import), 1960/1970 (foreign imports), 2138 (preset conversions), 2533
(`models.parse_bank_node` → `foreign_import.parse_foreign`). So overlaps are
routine, not theoretical.

**Consequences.** (a) mpc2emu's captured progress is recorded against the
wrong conversion, so `calllog`'s provenance record — the entire reason it
exists — is wrong for any conversion that overlaps another.
(b) `sys.stdout` can be **permanently bound to a dead StringIO**, silencing
every later `print()` in the process for the rest of the session.

**Proved** (two threads, A exits before B):

```
B-PROGRESS-LINE            <- the only survivor; it went into A's buffer
(nothing further prints at all)
[ A done. B done. / sys.stdout is the real console: ... / RESULT: ... ]
```

Every statement after that point is invisible, because `sys.stdout` now points
at a discarded `StringIO`. The same applies to `calllog.traced`
(calllog.py:309-342) and `images._run_captured` (84-119).

**Fix.** Don't touch the global. Either a `threading.local()` daemon owning a
`sys.stdout` proxy installed once at import, dispatching `write()` to the
current thread's buffer or the real stdout; or (simplest for this codebase) a
module-level `threading.RLock` around the `with` block, so a conversion's
stdout is exclusive for its duration.

### M16 — `build/xpm_import.py:459-473` — `xpm_parser.SYNC_BPM` is a process-wide global, and the restore clobbers the other thread

```python
prev = getattr(xpm_parser, "SYNC_BPM", _MISSING)
...
finally:
    if prev is not _MISSING:
        xpm_parser.SYNC_BPM = prev
```

The module docstring is explicit that this is a stopgap: *"mpc2emu added
`parse_xpm(..., sync_bpm=)` on 2026-09-20 (their ER-5, asked for from here),
which removes the whole process-global dance"*. The parameter **exists in the
configured checkout** (`parsers/xpm_parser.py:1640`,
`parameters : ('xpm_path', 'wav_dir', 'chromatic_pads', 'sync_bpm')`, verified
by introspection) — but the global path is still reachable whenever two
parses overlap, because the `finally` restores the *saved* value, not the value
the other thread installed.

**Consequence.** Two concurrent XPM imports each get the other's tempo, and
neither gets its own. Worse, after both finish `SYNC_BPM` is left at whatever
the last thread saved, not at mpc2emu's default `120.0` — so every subsequent
parse in the session inherits a tempo nobody chose. That is precisely the leak
the `finally` was written to prevent.

**Proved** (two threads running the same save/set/sleep/restore sequence):

```
A asked for 90 BPM, B asked for 128 BPM (concurrent imports)
  A's parse would read SYNC_BPM = 120.0     <- A's own 90 was overwritten, then B's restore landed
  B's parse would read SYNC_BPM = 90.0      <- B's own 128 was overwritten by A
  SYNC_BPM after both:  128.0 (mpc2emu default 120.0)
```

**Fix.** Prefer the parameter unconditionally — call with `sync_bpm=` whenever
the parameter exists (passing `None` is a no-op upstream) — and keep the
global path only as the `getattr` fallback for an older checkout, guarded by a
lock if it stays.

### M17 — `build/convert.py:1464-1465` — `_verify_written`'s AKAI branch skips the byte check it exists to perform

```python
if opts.target_format == "AKAI":
    ...
    got = len(akai_written.samples)
    got_bytes = sum(len(s.pcm) for s in akai_written.samples.values())
    if got >= expected:
        return                        # <-- object count alone; got_bytes unused
    if got_bytes >= expected_bytes * 0.95:
        return
```

This is the exact early-return the function's own docstring says was removed —
*"An earlier version returned as soon as the object count was satisfied ...
That skipped the byte check on every healthy bank -- which is every bank,
since a writer that loses AUDIO usually keeps the object -- so a file carrying
a fifth of its samples passed. The two questions are independent and both are
cheap, so both are asked."* The file-target branch (1520-1538) does that; the
AKAI branch does not.

**Proved** (real `apply_conversion`, real `parse_dir` read-back, only
`build_akai_volume` made lossy):

```
(a) AKAI target with ~97% of the audio destroyed:
    VERIFICATION PASSED. volume = 3,128 bytes on disk; source = 283,598 bytes
    every sample object was kept, so the object count check short-circuited
```

The same fault on an E4B target **is** caught:
`REFUSED -> the converted E4B bank came back with 1,442 of 152,376 bytes of
audio after being written (7 of 7 sample objects)`.

**Fix.** Delete the `if got >= expected: return` and let the byte comparison
at 1473 decide, exactly as the file targets do.

**Fix both halves together.** A related detail found while proving this:
`AkaiSample.pcm` (`banks/akai.py:546-548`) is header-stripped, while
`expected_bytes` is the in-memory `len(s.data)` with no header allowance — yet
every `.S3` carries `_SAMPLE_HEADER_BYTES = 150` bytes that never reach RAM,
which `akai_image.py:119-123` documents as a measured fact. For 128 one-shots
of 100 frames each, only 25 % of the expectation survives into `got_bytes`
(measured). Today the buggy early return masks this; fixing H17 without also
subtracting the per-sample header from `expected_bytes` will start **falsely
refusing** short-sample AKAI banks.

### M18 — `build/convert.py:1508-1511` and `:1639-1640` — the verification turns any failure into a silent pass

```python
    except Exception:  # noqa: BLE001
        # Unreadable for some other reason is a separate problem, and the
        # caller will meet it soon enough; do not mask it as sample loss.
        return
```

The comment's claim is not true for a conversion inside a batch, and the except
is broad enough to swallow the *verification's own bugs*. `_zone_loss_risk`'s
`except Exception: return None` does the same for the zone-loss warning.

**Proved** (read-back patched to raise, everything else real):

```
(a) E4B target, real audio loss: REFUSED -> came back with 1,442 of 152,376 bytes of audio ...
(b) read-back raises:            accepted an UNREADABLE bank -> .../a.e4b (130,578 bytes)
```

And a bug in the check itself has the same effect with no user-visible trace:
during this review a patching attempt assigned to `E4BSample.pcm`, which is a
read-only `@property` (`banks/e4b.py:164`); the resulting `AttributeError` was
swallowed at 1508 and `_verify_written` reported success. A single stale
assumption inside that `try` disables the whole safety net, silently.

**Fix.** Don't swallow. Catch, append a risk record through the same
`risks_out` channel the rest of the module uses
(`{"code": "VINSAMLIB_VERIFY_SKIPPED", …}`), then re-raise as
`ConvertOpError` so the refusal is the default. Both functions run on the
worker thread — there is no reason either should swallow.

### M19 — `build/convert.py:1361`/`:1365` → `:1481` and `:1622` — the freshly written bank is read and fully re-parsed twice, per conversion

`_verify_written` does `data = out_path.read_bytes()` (1481) and
`vs_*.parse_bytes(data, ...)`; `_zone_loss_risk` then does
`data = out_path.read_bytes()` (1622) and `vs_*.parse_bytes(data, ...)` again.
Both decode every sample's PCM into memory.

**Measured** (36 MB bank, real conversions):

```
E4B->E4B (max_sample_rate=24000): 70.40s
  read_bytes of the result file : 2
  full re-parse of the result    : 1
  bytes pulled back off disk     : 55.6 MB (result file is 19.6 MB)
E4B->EIII (resample emulator2): 5.32s
  read_bytes of the result file : 2
  full re-parse of the result    : 2
  bytes pulled back off disk     : 58.7 MB (result file is 22.7 MB)
```

For the 118 MB bank `build/project.py`'s docstring talks about, this is ~0.2 s
of duplicate parse and ~240 MB of avoidable transient allocation per conversion.

**Fix.** Read once and parse once — pass the already-parsed written bank from
`_verify_written` into `_zone_loss_risk` (both take `out_path` today), or read
`data` once in `_apply_and_write_pipeline` and hand it to both. They need the
same reader anyway.

### M20 — `build/convert.py:1778` — every AKAI program conversion leaks its entire staged volume for the whole session

`convert_preset()` creates a session temp dir at line 1775, then:

```python
if isinstance(bank, vs_akai.AkaiBank):
    return _convert_akai_program(bank, preset_obj, opts, tmp_dir, risks_out)
```

`_convert_akai_program()` (1822-1930) writes the whole one-program volume into
it — `samples_dir` with **every sample the program references**, plus the
`.P3` program file (1913-1922). The `forget()` call at 1818 is on the *other*
branch, after `apply_conversion()` returns. `tempdirs.forget` is called from
exactly one place in the whole codebase (verified by grep), so nothing else
reclaims it.

**Measured** (E4B vs AKAI through the same `convert_preset` entry point):

```
=== E4B source, convert_preset ===
after 10 E4B conversions: 10 session dirs, 1.6 MB retained
=== AKAI source, convert_preset ===
after 10 AKAI conversions: 30 session dirs, 15.8 MB retained
```

A single AKAI conversion leaves two `vinsamlib_convert_*` dirs behind where
the E4B path leaves one; 20 conversions leave 40 dirs / 28.4 MB. A 8-sample
drum kit at 44.1 kHz/60 s is ~42 MB per conversion, so forty programs in a
session is ~1.7 GB held in `/tmp` until quit. `session_temp_dir` registers the
dir for `cleanup_session()`, i.e. it is removed **only at application exit** —
precisely the failure mode `tempdirs.py`'s module docstring was written to end.

**Fix.** Copy the `with staged:` pattern already proven in
`foreign_import.parse_foreign` (foreign_import.py:758-760): after
`_run_captured(akai_parser.parse_akai_program, …)` returns, mpc2emu has read
the audio into the `Bank`, so the staging dir can be forgotten before
`_apply_and_write` — which allocates its own session dir for the result.

### M21 — `build/convert.py:539-561` — `suggest_mono_side` is an uncapped pure-Python scan over every stereo sample, run on the GUI thread

```python
data = s.data[: len(s.data) // 4 * 4]
a = array.array("h"); a.frombytes(data)
L, R = a[0::2], a[1::2]
l_rms = (sum(x * x for x in L) / len(L)) ** 0.5
```

No frame cap, no `array`-level reduction — one Python generator step per sample
per channel, for every stereo sample in the bank. Its sibling
`stereo_mono_risk` is careful to cap at `max_frames=20000` (via
`models_common.channel_correlation`); this one is not.

**Measured** (20 stereo samples of 60 s each at 44.1 kHz — 212 MB of audio):

```
stereo_mono_risk : 0.30s  (decorrelated=20)
suggest_mono_side: 9.65s  -> {'side': 'left', 'avg_db': 0.002491828605…, 'n': 20}
```

~0.5 s per stereo sample. It is called from
`ConvertOptionsDialog._show_risk` (line 758) whenever *any* sample is
decorrelated — which, per the measured corpus quoted in the same dialog (the
median channel correlation was 0.076), is the common case — and again from
`_confirm_mono_risk` (824) on the OK path. **Both run on the GUI thread.**

**Fix.** Cap it the way its sibling does — `s.data[: 20000 * 4]` — and use
`array` reduction instead of a generator. Either change makes it O(1) in
sample length, which is what the sibling already establishes as the house rule.

### M22 — `banks/e4b.py:301-312` — the E4XT playback-ceiling warning is built on a single-edge model that mpc2emu has already corrected

`zones_above_playback_ceiling` computes one `safe` key and warns for every
zone whose `hi_key > safe`, so **every zone in the +50..+127 band is warned
even where the E4XT plays normally**. mpc2emu's current note at
`models/common.py:1030-1055` is explicit that this is **one edge of a band**:

```
#     +40..+45  normal        110..117  (+50..+57)  normal
#     +46..+49  FREE-RUNS      118..121 (+58..+61)  FREE-RUNS
#                              122..127 (+62..+67)  normal
#   The value and e4xt_max_transpose_semitones are LEFT UNCHANGED on purpose:
#   every `highest_safe_key` we ship is right about where trouble STARTS and
#   wrong to imply everything above it is unsafe.
```

This is user-visible: it is the detail that gates the "keep anyway" flow.

**Fix.** Either adopt the band shape (warn only when the zone's window
overlaps a free-run band) or, minimally, reword the record so it does not
describe a single limit, and mark the function as reporting "the first edge"
as mpc2emu does. `tests/manual_ceiling_check_agrees.py` asserts the constants
agree — keep that test green either way.

### M23 — `ui/sampledir_import_dialog.py:184` and `convert_options_dialog.py:713`/`:789` — Adjust Sample Placement and the Stereo Test button parse the whole source folder on the GUI thread

**Proven** on 400 mono 2-second WAVs:

```
>>> sampledir_import.parse_preview() on 400 WAVs: 160 ms
>>> sampledir_import.load_samples_for_test() on the same folder: 137 ms
```

Linear in file count; a real multisample folder (400–1 000 WAVs, several
seconds each) is 0.16–0.5 s+ per click, and the placement editor re-parses when
the octave choice changes.

**Fix.** Pre-parse into a cache keyed by `(path, mtime_ns, size, octave_offset)`
filled by a `Worker` when the dialog opens, and have the placement editor
reuse it.

---

## LOW

### L1 — Dead / unreachable code

- `audition/render.py:329-330` (`_read_block_np`) and `:1027-1053` (`_lfo_wave_np`) — unreachable pure-Python branches inside numpy-only helpers. Both are only ever called from `_render_voice_np`, which itself is only reached when `_np is not None`, and they **cannot** run: lines 308/309 dereference `_np.zeros`/`_np.asarray` before the guard. **Proved**: calling `_read_block_np` on an empty channel raises `AttributeError` on `_np.zeros`. Delete the branches and rename the helpers so the "np" suffix stops implying a paired implementation.
- `audition/filter.py:58` — `nyquist = 0.5 * self.rate  # noqa: F841`, unused local.
- `audition/filter.py:158-160` — `Cascade._idx` is never read anywhere in the repo. `Cascade.active` (172) is used only by `tests/manual_audition_filter_taps.py`.
- `audition/filter.py:374` — `is_resonant()`: unused.
- `audition/render.py:455` — `LFO_SHAPES`: only used by `tests/manual_audition_lfo.py`; `_lfo_terms` chains string comparisons instead.
- `build/foreign_import.py:370-386` — 17 lines of dead code after a `return`, a byte-identical copy of `_image_content_format_uncached()`'s body.
- `vfs/fatvol.py:655-666`, `:826-837`, `:956-966` — mpc2emu delegation after `raise NotImplementedError`.
- `build/convert.py:1901` — `+ (f"…" or "")`: an f-string is never empty, so `or ""` can never fire and the `# noqa: SIM222` exists to silence the lint that already flagged it.

### L2 — `banks/e4b.py:220-233` and `eiii.py:744-772` — loop reading ignores the right-channel field pair

Both read the **left** pair unconditionally: E4B at 38/46, EIII at 36/44. mpc2emu (`parsers/e4b_parser.py:350-357`) does:

```python
loop_start_off = 38 if has_left_channel else 42
loop_end_off   = 46 if has_left_channel else 50
```

where `has_left_channel = bool(options & 0x0020)`, and its comment records the corpus measurement: *"6 of 73 looped right-channel-only samples have a negative (nonsensical) loop_start under the unconditional left-field read"*. `banks/e4b.py`'s guard `if start_b < PCM_OFFSET or end_b < PCM_OFFSET: return []` (line 228) converts that into a **silent skip** — the sample is never reported or repaired, with nothing saying so. EIII has the same right-hand pair (`SAMPLE_LOOP_START_RIGHT = 0x28`, `SAMPLE_LOOP_END_RIGHT = 0x30` in `writers/eiii_writer.py:148-155`) and the same gap.

### L3 — `banks/akai.py` `sample_rate` masks byte 0x01; `summary._krz_zone` reports an unsnapped rate; the AKAI "sample size" figure overstates by ≥42 bytes

- `akai.py:446` — `self._PLAYBACK_RATES[1 if self.body[0x01] else 0]` vs mpc2emu's `_AKAI_PLAYBACK_RATES[1 if (data[0x01] & 1) else 0]`. Any byte 0x01 value with bit 0 clear but other bits set (e.g. `0x02`) reads as 44100 here and 22050 there. **UNVERIFIED** — no corpus available; worth one fixture check. This is the one place `banks/akai.py`'s cross-validated rate reading diverges from mpc2emu.
- `summary.py:544-550` — `sample_rate = round(1e9 / period)`. mpc2emu snaps first (`parsers/krz_parser.py:95-99`, nearest standard rate within ±2 Hz), so a 44100 Hz sample reads back as **44101** in the Detail pane.
- `summary.py:713-715` — `max(0, samp.size - akai.AkaiBank.SAMPLE_HEADER_BYTES)`. For an S3000 sample `samp.size = 192 + PCM_padded`, so `size - 150` = 42 + padded PCM, while the true audio is `len(samp.pcm)` = `frame_count * 2`. As a "sample size" figure in the Detail pane it is inaccurate by the block padding plus 42 bytes per sample.

### L4 — Two loop-point defects found while proving H2-family material

- `banks/krz.py:1113-1116`, `:1367-1421`, `:814-820`, `:759-760` — KRZ loop repair can write a loop point past the end of the audio that gets copied. `loopcheck.snap_to_zero`/`nudge_to_match`/`crossfade` search the **whole** `src.pcm` region, which for KRZ is shared by every sample in the file — there is no bound to the sample being repaired. `_repair_sample_loops`' docstring claims the opposite (*"Repairing first means the extent, the PCM slice and the rebias downstream are all computed from the repaired values and stay consistent"*), but `_all_sample_lengths()` computes every extent from `self.samples` — the bank's *original* objects — so the repaired fields are never seen. **Reproduced**: `nudge of A: (0, 1441)` against `A start=0 loop=(0,1441)` where A's own audio ends at word 1000 — the loop tail now plays B's audio, and the only check (`got_words < n_words`) passes. Take the extent **before** the repair and thread it through as a `limit`.
- `banks/krz.py:1234` — `owned = src.other_by_id()` is rebuilt inside `for src, prog in prog_list`, i.e. a dict over every FX object for **every program**. Hoist it above the loop. And `:1330-1342` re-parses the freshly built bank (`rom_keymap_refs(parse_bytes(out, "built"))`) just to count ROM references on every assemble with `warnings_out`.

### L5 — Various narrow fixes

- `vfs/emu3.py:214-221` — `_true_size()` can return a **negative** size. **Proved**: `_true_size(1, 0, 0, 524288) == -512`; `bytes(buf[:-512])` on a 2048-byte read returns 1 536 bytes. Reached by any dir-content entry with `n_clusters > 0, blks == 0`. Fix: `max(0, …)` plus a reject in `_bank_entries`.
- `index/db.py:518-524`, `:575-576` — `except sqlite3.OperationalError: return []` hides real SQL errors. **Proved**: `_fts_query('"')` and `_fts_query('&')` fall back to the raw text (db.py:632), which is not valid FTS5, so the error is swallowed and the search box reports "No matches" for a query that should match `R&B`.
- `index/scanner.py:66-81` — `db.all_container_paths()` inside a `for root in roots` loop (O(roots × containers)), and one `commit()` per forgotten container at db.py:344-346 (a removed 4 000-container folder = 4 000 commits).
- `vfs/iso9660.py:59-82` — a corrupt directory record is not rejected. **Proved** with a crafted PVD: a 0xFF length byte on a 40-byte record yields a phantom `Entry(name='', ref=(0,0))` rather than an error. Harmless today (the scanner ignores it), but a corrupted extent is indistinguishable from an empty one. Fix: `if length < 33: break`.
- `vinsamlib/ui/main_window.py:1770`/`:1776` — `last_instrument_dir` is `setattr`-ed onto `Config` but is not a field (`config.py:125-140`), so it is never persisted. **Proved**: `has field 'last_instrument_dir'? False`; it is absent from the written `config.toml` and from the reload. File ▸ Import Instrument… never remembers where the user keeps their soundfonts.
- `vinsamlib/ui/pending_pane.py:196` — `_audio_memo` is never pruned. `bank_pane` has `_prune_audio_memo` (1122) for precisely this hazard (an `id()` address reused by a different bank returns the first bank's size).
- `vinsamlib/ui/bank_pane.py:558` — `dropEvent` reports ceiling zones for the whole drop, including items skipped as duplicates; `add_presets` (696) passes only the added slice.
- `vinsamlib/ui/detail_pane.py:251-257` — `_render_kv` does not HTML-escape its values; `("Path", node.payload)` at line 79 means a path containing `&` renders as broken markup. `bank_pane._apply_preset_info` escapes the same kind of string.
- `vinsamlib/ui/detail_pane.py:111-115` — `show_node` calls `Config.load()` on every selection change on the GUI thread — a TOML open + parse per click, for one boolean.
- `vinsamlib/ui/image_pane.py:778`/`:802`/`:819` — `_run_confirmed_op`'s `on_done` re-opens the image on the GUI thread. Measured 16 ms for a 537 MB / 300-bank EMU3 HD, so latent rather than felt.
- `vinsamlib/ui/main_window.py:2678-2680` — `_start_scan` silently no-ops when a scan is running, so after "Add Library Folder…" the folders are added to the model but never scanned in that session, with no message saying so.
- `build/images.py:327` — `vinsamlib_akai_regrow_` is not in `tempdirs.PREFIXES`. Not a live leak (context-managed), but it breaks `reap_stale()`'s documented invariant — the same class of bug `vinsamlib_tal_` was on 2026-10-05.
- `build/images.py:255` — the staging copy's name is fixed, not unique (`src.name + ".vinsamlib-tmp"`). Two concurrent in-place mutations of the same image clobber each other. **UNVERIFIED as reachable** — no live path issues two mutations concurrently, but nothing in `_mutate_in_place` prevents it.
- `build/images.py:524-530` — the EMU3 rebuild fallback leaves a partial `.rebuild` file in the **user's own folder**. **Proved**: `dir after: ['mydisc.img', 'mydisc.img.vinsamlib-tmp.rebuild']`. `_mutate_in_place` only unlinks the `.vinsamlib-tmp` copy.
- `build/xpm_import.py:434`, `foreign_import.py:779`, `convert.py:897`/`:938` — capability probes read `fn.__code__.co_varnames`, which includes **locals**. A local named `faithful_layers`, `drum_program`, `firmware_sim`, `sync_bpm`, `chromatic_pads` or `ib304f` makes the probe answer "supported" when the keyword is not accepted, and the call then raises an opaque `TypeError`. **Verified safe today** (introspected all four against the configured checkout: no collisions), so this is latent. Fix: `inspect.signature(fn).parameters`.
- `build/convert.py:1917` — classify AKAI staged files by comparing full byte blobs (`if (fn, d) not in program_files`). O(n·m) tuple comparisons; if a sample file ever shares a name *and* bytes with a program file it is silently reclassified. One predicate, O(n).
- `build/convert.py:1344-1349` — a failed AKAI write leaves a half-written volume (compounds M20 rather than risking user data).
- `build/project.py:401-415` — a failed save leaves `*.part` beside the project.
- `audition/render.py:114` vs `envelope.py:47` — two `_db_to_gain`s, one clamping at `_HARD_FLOOR_DB = -96` and one not. Rename them (`_db_to_gain_preview` / `_db_to_gain_env`) so the difference is self-evident. Related: `filter.py:36` `_MIN_F0_HZ = 20.0` is repeated as bare literals at `render.py:848` and `:970`.
- `audition/render.py:174-178` — pure-path stereo decode with an odd byte count desynchronises the channels. The numpy branch trims to whole frames; the pure branch only drops the stray byte, so `flat[0::2]` has one more element than `flat[1::2]`. No crash, but the two paths differ and the stereo image is one sample short at the tail. Mirror the trim. (Only reachable with `VINSAMLIB_NO_NUMPY` set.)
- `audition/envelope.py:121-144`, `:160-183` — the envelope grid is built per voice with 3-4 `getattr`s per grid point, nothing in the loop changing except `t`. Measured: `amp_envelope` 25.7 ms + `filter_envelope` 10.9 ms = **36.6 ms per voice** at 1.5 s hold + 8 s tail.
- `audition/render.py:280-287` — the loop seam is a one-sample linear blend (value-continuous but a **slope** discontinuity of `|ch[ls] - ch[le]|` per loop period), and one-shot voices truncate to zero in one frame. Both are the "corner/tick" class the feature exists to hear, and no caveat names either.
- `audition/render.py:1083` — `PARALLEL_MIN_FRAMES = 3 * 44100` is a bus-length proxy for work. Fork + Pool measured **8 ms for 3 workers / ~30 ms for 12**, so a 20-zone chord spanning 3 s for ~0.5 s of render still pays 30 ms of fork for nothing.
- `audition/__init__.py:348-358` and `audition/params.py:93-98` — `id()`-keyed LRU caches release their keepalive on eviction, so those objects can be collected and a different bank can land on the same address. Mitigated only by the Explorer tree holding every bank.
- `banks/e4b.py:933-936` and `eiii.py:674-678` reach into mpc2emu's **private** names (`e4b_writer._build_e4ma()`, `._build_emst()`, `eiii_writer._create_empty_bank()`, `eiii_writer.BANK_FORMATS`). All four exist in the current checkout (verified), but one rename breaks `assemble()` at runtime — after the user has picked presets. There is an existing gating idiom in `Config.check_*_support()`; add one here.
- `banks/eiii.py:412-422` — ~1 257 individual `struct.unpack_from` calls for the two address tables; replace with one `struct.unpack_from(f"<{n}I", …)`. And `eiii.assemble` (610-650) is quadratic in (chain segments × zone refs) — real chains are 1-3 segments so harmless today.
- `index/scanner.py:436` — reaches through `db._conn` for a private attribute; add an `IndexDB.container_audio_total()` method.
- `vinsamlib/ui/models.py` — `fetchMore` inserts rows under a stale `QModelIndex` after `remove_root`. Verified: no Qt assertion in this build, but the removed root's node is left permanently holding 60 children. **UNVERIFIED as a crash.**

---

## Refuted — checked and does not hold

Recorded with the evidence, because a review is a claim about the code and the
code is the authority.

### R1 — `index/scanner.py:116` "missing `size_suffixes`" — a deliberate, documented default

`LocalDirVolume.list()`'s default of `None` ("stat everything") is not an
oversight. Its docstring states the reasoning:

> `size_suffixes` names the file suffixes whose SIZE and MTIME the caller
> actually uses; everything else is listed with size 0 and no mtime, and is
> never stat'ed. Default None means "stat everything", **which is what the
> index scanner wants -- it needs a size for every container it indexes.**

`ui/models.py:586` is the only caller that passes it, and the tree does not
need sizes for sample files. Narrowing it in the scanner *is* a valid
optimisation — the scanner only needs sizes for the bank/image extensions it
will index, not the `.wav` files in a sample folder — but it is a refinement
rather than the 3.4 s/folder bug it was reported as, and the default is
correct for today's callers.

### R2 — `audition/filter.py` "−3 dB at 0.803 vs 0.774 is a defect" — a documented open finding, not a bug

A plain two-section Butterworth cascade is **mathematically** 0.802: with
`H = (1 + (f/f₀)⁴)^-2`, the −3 dB point solves `(1 + r⁴)² = 2`, i.e.
`r = (√2 − 1)^¼ = 0.8022`. The measured 0.803 is the implementation agreeing
with the arithmetic to three digits. The K2000's 0.774 describes a topology
the cascade does not model; the discrepancy is recorded in
`tests/expected_failures.txt` as the finding it was designed to produce, and
`CASCADE_3DB_RATIO = 0.802` is honest about being what this implementation
produces. **No code change.**

### R3 — `build/` "subprocess / `shell=True` / pipe deadlock / missing timeout" — not applicable

There is **no subprocess and no shell anywhere** in `vinsamlib/`. mpc2emu is
imported **in-process** via `mpc2emu_bridge.install()`, which puts its repo
root on `sys.path`. Argument injection, `shell=True`, pipe-buffer deadlock and
missing timeouts therefore cannot occur, and I found no instances. What
replaces them is arguably worse — a process-global `redirect_stdout` and a
module-global in mpc2emu's parser, both racing under a shared 12-thread pool.
Those are M15 and M16, and they are the real findings in this area.

### R4 — `index/scanner.py` "silently dropped banks" — the inner `except` is real, the outer one is not defeated in the common case

The inner `except Exception: continue` at `:466`/`:476` does mean an
unreadable bank on a disc image produces a container row with `error=NULL`,
which reads as "scanned fine". But the outer `except Exception` at `:419`
still catches everything else, so the claim that it is "defeated" is too
strong. The fix (count the failures and pass the reasons to
`finish_container(cid, error=…)`) is still worth taking; the framing was not.

### R5 — `_verify_written` producing **false** refusals — does not happen

I ran a 14-case × 2-fixture-shape matrix across all four target formats and all
option families: **28/28 accepted**. The check's bound (0.95) does not
over-fire on this corpus. The AKAI rate-snap path also does not falsely refuse
its own necessary resample — `expected_bytes` is taken from the in-memory bank
*after* the snap, and 6 rates (44100/48000/27777/22050/16000/8000) all convert
correctly with `AKAI_RATE_SNAPPED` reported where expected.

---

## Verified sound — checked specifically and found correct

Recorded so the next review does not repeat the work.

### audition/

- **The two render paths agree.** A 200 000-point sweep of `_read` vs `_read_block_np`, `lfo_value` vs `_lfo_wave_np`, `envelope.interpolate` vs `_env_block_np`, `_soft_limit` vs `_soft_limit_np` gave **exactly 0.0 difference**; a 60-preset randomised fuzz (random loop types incl. alternating/forward-rel, 8-pole filters, all LFO destinations, stereo/mono, 4 formats, 3 render rates, keytrack/velocity/keyboard pan, rate-based release) produced **bit-identical 16-bit PCM in every trial, peak 0 LSB**. `manual_audition_numpy_tolerance` passes (worst 114 dB / 1 LSB on a real corpus preset). No divergence found — the documented contract holds.
- **The sample cache is correct.** The key is `id(sample)` **and** validated with `prior[0] is sample`, and the value pins the `SampleData`, so no address reuse can return stale audio; the byte accounting is under a lock in both branches including the double-insert loser path.
- **`_finish`'s reported peak is genuinely pre-headroom** (`peak=1.4` for input `[0.9, 1.4, -1.1]` with `headroom_db=-6`, i.e. the raw max), documented correctly.
- **`filter.py`'s four tap methods are bit-identical to `process()`**, and the hoisted `_a1/_a2/_a3` are bit-identical to the per-sample form (`manual_audition_filter_taps`).

### banks/

- **E4B loop offsets 38/46, PCM_OFFSET/PCM_START 92/94, the `+1` on the end field, options bit `0x0001`, sample rate at 54** — all match `mpc2emu/parsers/e4b_parser.py:290-376` exactly. `_repair_body_loops`' write-back `(new_end-1)*2+PCM_OFFSET` is the exact inverse.
- **EIII loop offsets 36/44, options at 58, `0x0001`, no `+1` convention** — matches `writers/eiii_writer.py:147-162`. `parse_bytes`' address-table arithmetic and `_extent_map`'s sorted-start approach are sound; `_build_bank`'s block accounting (including `preset_blocks = (sample_area_offset-1+511)//512`) is byte-identical to mpc2emu's own writer.
- **KRZ `_decode_hash`, the object-block layout, `_rename_block`'s `size += delta` vs recomputed `blocksize` trap, and `band_starts`' `12 + 2*j + Level[j]`** are correct — verified against the documented `[16,14,12,10,8,6,388,386] → (28, 412)` example.
- **`e4b._split_voices_by_velocity` round-trips correctly** — a 3-zone single-voice preset split in the middle, re-read by mpc2emu's own parser as 2 voices with windows (0,127) and (0,63) and the right zone-to-sample mapping.
- **No regex is compiled inside a loop** (`_NON_ZERO` is module-level, e4b.py:495); no `seek()`/short-`read()` patterns exist in any parser (all reads are bulk); no file handles leak.

### vfs/

- **No file-handle leaks.** Every `open()` in `vfs/` (32 call sites) is inside a `with` block — grep returns zero bare `open(`. Readers open/parse/close per call, so there is no persistent handle to leak on an error path. Readers are thread-safe for the background scanner, as claimed.
- **No path-traversal surface.** `Entry.ref` is either an absolute path from `scandir` (localdir) or opaque offsets/cluster tuples produced by the readers; nothing joins a caller-supplied string into a path.
- **No off-by-one in the FAT/dir-entry parsers.** `_iter_dir_entries`' `range(0, len(data) - 31, 32)` is correct; `_fat12_get/_fat12_set` packing, `_walk_chain`'s EOC/BAD handling, `_cluster_offset`, FAT32's FstClusHI/LO fold and the 28-bit mask were each checked against the spec and against the real fixtures.
- **`Emu3Volume._fat` cache is invalidated** by both mutators (emu3.py:333, 380) — no stale-FAT read path found.
- **`sniff()` ordering is sound** (EMU3 magic → AKAI → FAT with signature → FAT without → ISO PVD).
- **Corrupt inputs do not kill the scan.** A tray of garbage `.sf2/.sfz/.exs/.gig/.xpj/.krz/.e4b` plus an empty SF2 all produced clean container rows with no exception.

### build/

- **TAL and sample-folder staging are correct** — both use `with staged:` / an explicitly `cleanup()`ed `TemporaryDirectory` with connected `finished`/`error` slots (`foreign_import.py:761-762`, `main_window.py:1615-1661`). This is the pattern M20 should copy.
- **`tempdirs.forget` on the E4B and KRZ paths** — `convert_preset` forgets the staging dir on the non-no-op path (convert.py:1817-1818), and correctly *keeps* it on the true no-op path where the staged file *is* the result.
- **No option is silently dropped** by `_apply_and_write_pipeline`. Every `ConversionOptions` field is either consumed, deliberately stripped with a `DEVICE_MATCH_PROCESSING_SKIPPED` risk record, or listed with a reason in `_PROCESSING_FIELDS`' comment block. `pan_law == "constant-power"` is a genuine no-op for a KRZ/EIII target (the gate at line 1154 is E4B-only, as documented), and the option's own docstring says "E4B only" while the dialog greys it out on target change.
- **`build_akai_volume` returns a `list`** — the `for _fn, _data in _files` loop at convert.py:1348 iterates it directly. Safe.

---

## Recommended order of work

1. **C1** — `RETURNING id`, plus the first test that rescans a *changed* container. The only finding that can silently corrupt search results, and `20cbc8d`'s `mtime = 0` migration turned it from "on rescan" into "on every start after a bump".
2. **H4** — two-word fix; blocks a documented feature on the default path.
3. **H2** — two `len(data)` bounds; four trailing bytes currently delete a bank from the index.
4. **H3** — one-line sign flip and one-line test.
5. **H1** — the Worker redesign is the only item here I would not do as a drive-by. The `self`-freeing interim mitigation is worth taking immediately.
6. **M1–M4, M19–M21** — the speed cluster; together roughly halve an unfiltered audition render, remove a 30× memory blow-up, and stop two GUI-thread freezes.
7. **M5, M6, M9, M10, M23** — the GUI-thread and duplicated-work items, all cheap and independent.
8. **M15, M16** — both are the same root cause (process-global state + a shared 12-thread pool) and both corrupt the debug log that exists to explain exactly these conversions. Fix together; they are the only findings whose blast radius includes *other* conversions.
9. **M17, M18** — make the verification able to fail honestly.
