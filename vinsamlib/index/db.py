"""
SQLite + FTS5 index of the whole library, down to preset/program level —
what makes the M4 search box fast: without this, a recursive search would
mean re-opening and re-parsing every image and bank in the library on every
keystroke (159+ ISOs, hundreds of banks).

Two tables:
  container — one row per file the scanner had to actually open and parse
              (a loose bank file, or a recognised image). Keyed by path,
              with (size, mtime) so re-scans can skip anything unchanged.
              Plain directories are NOT containers — they're walked fresh
              every scan (cheap: just a listdir), only the expensive-to-parse
              leaves are cached here.
  item       — one row per folder/bank/preset found while scanning a
              container, in a parent/child tree mirroring the real
              structure. `native_id` carries just enough to re-locate the
              exact object later (the on-disk entry name for a folder/bank,
              the preset's/program's own embedded id for a preset) — a
              search hit is a bare DB row, disconnected from any live
              Volume/BankFile, so re-opening it for display needs to walk
              back down from the container using these.
  item_fts   — FTS5 full-text index over item.name (external-content table,
              kept in sync with `item` by triggers).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: The search path's own logger. This module is not a UI layer and has no
#: status bar to report to, but it must not swallow a real SQL error either
#: -- see the `except OperationalError` in `_search_rows()`, which used to.
_LOG = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS container (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,           -- 'bank' | 'image'
    format TEXT,                  -- 'E4B'|'KRZ' (bank) or 'EMU3'|'FAT12'|'FAT16'|'FAT32'|'ISO9660' (image)
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    scanned_at REAL,
    error TEXT,
    audio_bytes INTEGER            -- loadable audio inside it, NULL = not measured
);

CREATE TABLE IF NOT EXISTS item (
    id INTEGER PRIMARY KEY,
    container_id INTEGER NOT NULL REFERENCES container(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES item(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,           -- 'folder' | 'bank' | 'preset'
    name TEXT NOT NULL,
    native_id TEXT,               -- entry name (folder/bank) or preset/program id (preset)
    format TEXT,                  -- 'E4B' | 'KRZ', for bank/preset rows
    size INTEGER,                 -- bytes on the MEDIA (what a file manager shows)
    -- Loadable audio, which is the figure a sampler cares about and the one
    -- the browser shows. NULL means "not measured", which is a third state
    -- and not zero: a preset that genuinely references no audio stores 0.
    --
    -- It does NOT sum from presets to their bank, deliberately. Presets share
    -- samples -- one SoundFont here has 219 presets over 5 737 shared samples
    -- -- so a bank's figure is its DEDUPED total (what loading it costs) while
    -- a preset's is what that one alone needs (what taking it costs). Same
    -- quantity, different question; they are supposed to differ.
    audio_bytes INTEGER,
    -- Why this preset has no audio ON THIS VOLUME, where that is not simply
    -- "none": "samples on another volume" for an AKAI program whose samples
    -- live in a companion volume. NULL means there is no reason to give.
    --
    -- Stored rather than inferred at render time because the answer needs the
    -- PARSED bank -- which program names a sample the volume does not hold --
    -- and a search row has none. Inferring it from `format` instead would put
    -- "samples on another volume" on an AKAI program that simply names no
    -- samples, which is representable and is a different fact.
    note_short TEXT,
    ordinal INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS item_container_idx ON item(container_id);
CREATE INDEX IF NOT EXISTS item_parent_idx ON item(parent_id);

CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5(
    name, content='item', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS item_ai AFTER INSERT ON item BEGIN
    INSERT INTO item_fts(rowid, name) VALUES (new.id, new.name);
END;
CREATE TRIGGER IF NOT EXISTS item_ad AFTER DELETE ON item BEGIN
    INSERT INTO item_fts(item_fts, rowid, name) VALUES ('delete', old.id, old.name);
END;
CREATE TRIGGER IF NOT EXISTS item_au AFTER UPDATE ON item BEGIN
    INSERT INTO item_fts(item_fts, rowid, name) VALUES ('delete', old.id, old.name);
    INSERT INTO item_fts(rowid, name) VALUES (new.id, new.name);
END;
"""


@dataclass
class ItemChainEntry:
    kind: str
    name: str
    native_id: Optional[str]  # noqa: UP045


@dataclass
class SearchResult:
    item_id: int
    kind: str
    name: str
    format: str
    container_path: str
    chain: list[
        ItemChainEntry
    ]  # root -> ... -> this item (exclusive of the container itself)
    #: The two figures, carried so a search row can say what a tree row says.
    #: `size` is bytes on the media; `audio_bytes` is loadable audio, and NULL
    #: means "not measured", which is a third state and not zero. See
    #: `ui.models.size_suffix`, which is the single renderer of both.
    size: Optional[int] = None  # noqa: UP045
    audio_bytes: Optional[int] = None  # noqa: UP045
    #: Why this row has no audio on this volume, where that is not simply
    #: "none" -- see the column comment in the `item` table. Rendered by the
    #: same `ui.models.size_suffix` the tree row uses, which is the point: a
    #: hit that said "no audio" where the tree said "samples on another
    #: volume" was this branch's defect showing up in a second window.
    note_short: str = ""


@dataclass
class SearchPage:
    """What `search_page()` returns: the hits, and how many there really were.

    `total > len(hits)` means the list is TRUNCATED, and the UI has to say so.
    The alternative -- showing 200 rows and letting the user conclude the rest
    do not exist -- is how "Sync" lost a file that "Synco" had found.
    """

    hits: list[SearchResult]
    total: int

    @property
    def truncated(self) -> bool:
        return self.total > len(self.hits)


class IndexDB:
    def __init__(self, path: Path):
        # THE GUARD GOES HERE, at the moment of opening, because that is the
        # only place that knows which file is actually about to be written.
        # A caller can assemble this path any way it likes; it still has to
        # come through here.
        from ..config import (  # noqa: PLC0415, RUF100
            home_data_dir,
            require_real_state_opt_in,
        )

        try:
            same = Path(path).resolve() == (home_data_dir() / "index.db").resolve()
        except OSError:
            same = False
        if same:
            require_real_state_opt_in("library index")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=5.0)
        self._conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets a reader (a search query on the GUI thread) keep working
        # while a writer (the background scanner, on its own connection —
        # see MainWindow._run_scan) is mid-commit on the same file, instead
        # of blocking or hitting "database is locked".
        self._conn.execute("PRAGMA journal_mode = WAL")
        # The scanner commits once per container so search results appear
        # while a scan is still running, which under the default
        # synchronous=FULL means an fsync per container — 2392 of them, 7.6s
        # of a scan measured here, against 4.9s at NORMAL. NORMAL is the
        # documented companion to WAL and still survives an application
        # crash; only an OS crash or power loss can lose recent commits.
        # That is the right trade for this file: it is a derived cache of
        # what is on disk, and File ▸ Rescan Library rebuilds it from
        # scratch. Nothing here is a source of truth.
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(SCHEMA)
        self.migrated = self._migrate()
        self._conn.commit()

    #: Bumped whenever a scan would now record something it did not before.
    #: The index is a DERIVED CACHE -- its own docstring says File > Rescan
    #: Library rebuilds it from scratch and nothing here is a source of truth
    #: -- so an old one is emptied rather than migrated in place. Migrating
    #: would leave rows that are structurally current and factually blank,
    #: which is the state hardest to tell from a real zero.
    #: 5: those figures were the files' bytes on disk. Every sampler here
    #: stores 16-bit, so a 24-bit source was overstated by half again.
    #: 4: MPC and TAL rows gained a referenced-audio figure, which earlier
    #: scans never recorded.
    #: 3: the KRZ audio figures recorded under 2 were the sample OBJECTS'
    #: header bytes, ~90 each, so a 1.4 MB bank was indexed as 21 KB. Those
    #: rows are structurally current and numerically wrong, which
    #: needs_rescan() cannot see -- nothing about the files changed.
    SCHEMA_VERSION = 6

    def _migrate(self) -> bool:
        """Add columns an older file lacks, and mark old data for rescan.

        `CREATE TABLE IF NOT EXISTS` does nothing to a table that already
        exists, so a new column has to be added by hand -- silently, since a
        file created by this version already has it.

        Returns True if the index was marked for full rescan (schema bump),
        so the caller can tell the user their library is being rebuilt.

        NOTE: Earlier versions did DELETE FROM container here. That silently
        wiped an 8 000+ container index and the user saw "search returns
        nothing" for minutes while it rebuilt -- reported three times as
        "the database is empty every time I start". The fix is to FORCE A
        RESCAN instead of deleting: set every container's mtime to 0 so
        needs_rescan() returns True for all of them, the background scanner
        repopulates audio_bytes and note_short in the background, and search
        keeps working on the old (structurally current, factually incomplete)
        rows until they are refreshed.
        """
        for table, column, decl in (
            ("container", "audio_bytes", "INTEGER"),
            ("item", "audio_bytes", "INTEGER"),
            ("item", "note_short", "TEXT"),
        ):
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        have = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if have < self.SCHEMA_VERSION:
            # Force a rescan of EVERY container by zeroing mtime. This is the
            # key behavioural change from the old DELETE: needs_rescan() checks
            # size AND mtime, so mtime=0 guarantees a mismatch against any
            # real file's mtime. The scanner will then repopulate the new
            # columns (audio_bytes, note_short) incrementally, and the FTS
            # index stays usable the whole time.
            self._conn.execute("UPDATE container SET mtime = 0")
            self._conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            return True
        return False

    def close(self) -> None:
        self._conn.close()

    # -- scanning support -----------------------------------------------------

    def needs_rescan(self, path: str, size: int, mtime: float) -> bool:
        row = self._conn.execute(
            "SELECT size, mtime FROM container WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            return True
        return row[0] != size or row[1] != mtime

    def begin_container(
        self, path: str, kind: str, format: str, size: int, mtime: float
    ) -> int:
        """(Re)register a container and wipe its previous items — the
        scanner rebuilds them fresh on every rescan rather than diffing.

        Returns the container's OWN id, which is the whole contract: every
        `add_item()` under it hangs off this value, and `search()` joins
        `container ON container.id = item.container_id` to say which file a
        hit came from.

        SELECT FIRST, THEN BRANCH — and `lastrowid` is read only on the arm
        that genuinely inserted. The previous form did

            INSERT … ON CONFLICT(path) DO UPDATE …
            container_id = cur.lastrowid or SELECT id …

        and `lastrowid` is `sqlite3_last_insert_rowid()`, which an upsert that
        takes its UPDATE path does NOT change. It therefore held the rowid of
        the last *real* INSERT on the connection, which is `add_item()` from
        the previous container — an ITEM id. The `or` fallback never fired,
        because a stale id is truthy.

        Measured consequence on the real 8 167-container / 99 251-item index:
        item rowids run far past the container-id range, so the DELETE below
        hit no rows while `add_item()` raised `FOREIGN KEY constraint
        failed`. Below that range it was worse — silent. Rescanning
        `/a.e4b` returned 2 (which was `/b.e4b`), so `/b.e4b`'s items were
        wiped instead of `/a.e4b`'s, `/a.e4b`'s stale rows survived, and
        `search()` returned one preset under two different files.

        No test caught it because no test re-registers an already-present
        container: every fixture is either fresh or deleted, so the
        ON-CONFLICT arm was never taken.
        """
        row = self._conn.execute(
            "SELECT id FROM container WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            cur = self._conn.execute(
                "INSERT INTO container(path, kind, format, size, mtime, "
                "scanned_at, error) VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                (path, kind, format, size, mtime),
            )
            container_id = cur.lastrowid
        else:
            container_id = row[0]
            self._conn.execute(
                "UPDATE container SET kind=?, format=?, size=?, mtime=?, "
                "scanned_at=NULL, error=NULL WHERE id=?",
                (kind, format, size, mtime, container_id),
            )
        self._conn.execute("DELETE FROM item WHERE container_id = ?", (container_id,))
        return container_id

    def add_item(
        self,
        container_id: int,
        parent_id: Optional[int],  # noqa: UP045
        kind: str,  # noqa: PLR0917, RUF100, UP045
        name: str,
        native_id: Optional[str] = None,  # noqa: UP045
        format: str = "",  # noqa: RUF100, UP045
        size: int = 0,
        ordinal: int = 0,
        audio_bytes: Optional[int] = None,  # noqa: UP045
    ) -> int:  # noqa: RUF100, UP045
        cur = self._conn.execute(
            "INSERT INTO item(container_id, parent_id, kind, name, native_id, format, "
            "size, ordinal, audio_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                container_id,
                parent_id,
                kind,
                name,
                native_id,
                format,
                size,
                ordinal,
                audio_bytes,
            ),
        )
        return cur.lastrowid

    def set_item_audio_bytes(self, item_id: int, audio_bytes: int) -> None:
        """Fill in a row's audio total once its children have been walked.

        A bank's own figure is only known after its presets have been read,
        and the row has to exist first to be their parent -- so it is written
        in two steps rather than held back until the end.
        """
        self._conn.execute(
            "UPDATE item SET audio_bytes = ? WHERE id = ?", (audio_bytes, item_id)
        )

    def set_item_note_short(self, item_id: int, note_short: str) -> None:
        """Record WHY this row has no audio on this volume, when it has a reason.

        "" for the overwhelming majority of rows, and the UPDATE is skipped
        rather than written -- a scan creates tens of thousands of preset rows
        and almost none of them have anything to say.
        """
        if not note_short:
            return
        self._conn.execute(
            "UPDATE item SET note_short = ? WHERE id = ?", (note_short, item_id)
        )

    def set_container_audio_bytes(self, container_id: int, audio_bytes: int) -> None:
        """The container's own deduped audio total, for the tree's row."""
        self._conn.execute(
            "UPDATE container SET audio_bytes = ? WHERE id = ?",
            (audio_bytes, container_id),
        )

    def finish_container(self, container_id: int, error: Optional[str] = None) -> None:  # noqa: UP045
        self._conn.execute(
            "UPDATE container SET scanned_at = ?, error = ? WHERE id = ?",
            (time.time(), error, container_id),
        )
        self._conn.commit()

    def forget_container(self, path: str) -> None:
        """Stop indexing one container. The caller COMMITS.

        Removing a 4000-container folder forgets 4000 of these, and one
        `commit()` per DELETE is 4000 fsyncs -- the scanner's prune step
        alone, on a library someone has just reorganised. `scan()` now ends
        with a single `commit()`; nothing else calls this.
        """
        self._conn.execute("DELETE FROM container WHERE path = ?", (path,))

    def commit(self) -> None:
        """Make the current transaction durable.

        Exists so a caller doing a BATCH of writes owns the one commit
        rather than each write owning its own -- see `forget_container()`.
        """
        self._conn.commit()

    def forget_containers_under(self, root: str) -> None:
        """Purges every indexed container whose path is inside `root` --
        used when a library folder is removed (File > Remove Library
        Folder…), so stale presets/banks from it stop showing up in
        search. `root` itself is included; the trailing separator on the
        LIKE prefix keeps this from matching an unrelated sibling
        directory that merely starts with the same characters
        (e.g. removing "/libs/foo" must not also purge "/libs/foobar")."""
        prefix = root.rstrip("/") + "/"
        self._conn.execute(
            "DELETE FROM container WHERE path = ? OR path LIKE ?", (root, prefix + "%")
        )
        self._conn.commit()

    def all_container_paths(self) -> list[str]:
        return [r[0] for r in self._conn.execute("SELECT path FROM container")]

    def container_audio_total(self, container_id: int) -> Optional[int]:  # noqa: UP045
        """An image's loadable audio: the sum over its BANK rows.

        The SQL lives here rather than in `index/scanner.py`, which used to
        reach through `db._conn` to get at it -- a private attribute of this
        class, from outside it. Summed at bank level, never at preset level:
        a preset's figure counts a shared sample once for each preset that
        reaches it, so adding those would multiply the shared audio by
        however many presets use it. Two banks on one disc do not share
        sample storage, so summing them is sound.
        """
        row = self._conn.execute(
            "SELECT SUM(audio_bytes) FROM item "
            "WHERE container_id = ? AND kind = 'bank' AND audio_bytes IS NOT NULL",
            (container_id,),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    # -- search -----------------------------------------------------------------

    def audio_bytes_for_paths(self, paths: list[str]) -> dict[str, int]:
        """Recorded audio totals for whole containers, keyed by path.

        For the tree, which builds its listings from the FILESYSTEM and so
        knows a bank's path but nothing the scan worked out about it. One
        indexed query for a whole listing rather than one per row, and it is
        called from the GUI thread on rows that already exist -- the worker
        that produced them has finished, so this connection is not shared
        across threads.

        A container with no recorded figure is simply absent from the result:
        NULL means "not measured" and must not arrive as a confident zero.
        """
        if not paths:
            return {}
        out: dict[str, int] = {}
        # Chunked: SQLite's default parameter ceiling is 999, and a library
        # folder holding more banks than that is an ordinary thing here.
        for i in range(0, len(paths), 500):
            chunk = paths[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for path, total in self._conn.execute(
                f"SELECT path, audio_bytes FROM container "  # noqa: RUF100, S608
                f"WHERE path IN ({marks}) AND audio_bytes IS NOT NULL",
                chunk,
            ):
                out[path] = total
        return out

    def audio_bytes_for_items(
        self, container_path: str, names: list[str]
    ) -> dict[str, int]:
        """Recorded totals for rows INSIDE a container, keyed by native id.

        A bank on a disc image is not a container of its own -- the image is
        -- so audio_bytes_for_paths() cannot answer for it. That is why an
        AKAI volume on an ISO showed no size while the programs beneath it
        showed theirs: the figure was recorded, on the item row, and nothing
        was reading item rows.
        """
        if not names:
            return {}
        row = self._conn.execute(
            "SELECT id FROM container WHERE path = ?", (container_path,)
        ).fetchone()
        if row is None:
            return {}
        out: dict[str, int] = {}
        for i in range(0, len(names), 500):
            chunk = names[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for native_id, total in self._conn.execute(
                f"SELECT native_id, audio_bytes FROM item "  # noqa: RUF100, S608
                f"WHERE container_id = ? AND native_id IN ({marks}) "
                f"AND audio_bytes IS NOT NULL",
                [row[0]] + chunk,
            ):  # noqa: RUF005, RUF100
                out[native_id] = total
        return out

    def set_item_audio_by_name(
        self, container_path: str, sizes: dict[str, int]
    ) -> None:
        """Record per-row audio totals worked out after the scan.

        For SF2 and GIG, whose per-preset figure is only knowable by reading
        the file -- too expensive for a blind scan, affordable once when the
        user expands that row. Written here so the next expand, in any later
        session, reads it back like every other row.
        """
        row = self._conn.execute(
            "SELECT id FROM container WHERE path = ?", (container_path,)
        ).fetchone()
        if row is None:
            return
        self._conn.executemany(
            "UPDATE item SET audio_bytes = ? WHERE container_id = ? AND name = ?",
            [(v, row[0], k) for k, v in sizes.items()],
        )
        self._conn.commit()

    def set_container_audio_by_path(self, path: str, audio_bytes: int) -> None:
        """The container's own total, recorded after the fact -- see
        set_item_audio_by_name for why this cannot happen during the scan."""
        self._conn.execute(
            "UPDATE container SET audio_bytes = ? WHERE path = ?", (audio_bytes, path)
        )
        self._conn.commit()

    def _where(self, fts_query: str, formats: Optional[list[str]]):  # noqa: UP045
        """The MATCH plus the format restriction, with its parameters.

        ONE copy, because there are now two queries that need both -- the
        page and its count -- and a second copy of a filter is how the KRZ
        format list in `pending_pane.py` went stale and dropped a format
        nobody noticed until a bank would not build.

        The page and its count are now ONE query (a window function on the
        same row set -- see `search_page`), so "two" is really "one that is
        kept reusable". It stays a function because the chain CTE is the
        other half of a page and both halves take the same filter.
        """
        where = " WHERE item_fts MATCH ?"
        params: list = [fts_query]
        if formats:
            where += f" AND item.format IN ({','.join('?' * len(formats))})"
            params.extend(formats)
        return where, params

    #: What one hit's row carries, in order. `COUNT(*) OVER ()` is appended
    #: by `search_page` alone, which is where the true total comes from.
    _HIT_COLUMNS = (
        "item.id, item.kind, item.name, item.format, container.path, "
        "item.size, item.audio_bytes, item.note_short"
    )
    _FROM = (
        "FROM item_fts JOIN item ON item.id = item_fts.rowid "
        "JOIN container ON container.id = item.container_id"
    )

    def _hits_from_rows(self, rows) -> list[SearchResult]:
        """Assemble hits from already-fetched rows, chains batched ONE query.

        The chain is what a hit is worth -- `ui/explorer_pane._format_hit`
        takes `chain[:-1]` to name the ancestry of a row and
        `ui/search_resolve.py` takes `chain[-1]` to re-locate the object --
        so it has to be right for every hit returned, and it used to cost one
        SELECT per ancestor level PER HIT (`_chain_for`, a `while` loop).
        A 1000-hit page measured 2001 statements for 100 hits on the
        8000-container shape, and the UI painted ~25 rows from it.

        `_chains_for()` replaces the loop with one recursive CTE over the
        page's ids, and the chains are assembled in Python by id.
        """
        chains = self._chains_for([r[0] for r in rows])
        out = []
        for (
            item_id,
            kind,
            name,
            fmt,
            container_path,
            size,
            audio_bytes,
            note_short,
        ) in rows:
            out.append(
                SearchResult(
                    item_id=item_id,
                    kind=kind,
                    name=name,
                    format=fmt or "",
                    container_path=container_path,
                    chain=chains.get(item_id) or [],
                    size=size,
                    audio_bytes=audio_bytes,
                    note_short=note_short or "",
                )
            )
        return out

    def _search_rows(self, sql: str, params: list) -> list:
        """Run one search statement, reporting a failure instead of eating it.

        THE ERROR IS SAID. This used to be `except OperationalError: return
        []` with no trace, and that hid two different things: a writer on
        the scanner's connection briefly holding the file (rare under WAL),
        and a query that was never valid FTS5 to begin with. The second one
        was real -- `_fts_query()` handed back the raw text for a search of
        `"` or `&`, SQLite rejected the expression, and here it disappeared,
        so the search box said "No matches" for `R&B` and nothing anywhere
        recorded why. The text of the query is logged, not the user's
        keystroke alone, because that is the part that is actionable.
        """
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as ex:
            _LOG.warning(
                "index search failed and returned no hits (%s): %s",
                sql,
                ex,
                exc_info=True,
            )
            return []

    def search(
        self,
        query: str,
        limit: int = 1000,
        formats: Optional[list[str]] = None,  # noqa: UP045
    ) -> list[SearchResult]:  # noqa: RUF100, UP045
        """Ranked FTS hits, optionally restricted to a set of formats.

        `formats` IS APPLIED IN THE QUERY, and that is the whole point of it
        existing. Filtering the returned page instead means the limit is
        spent on rows the caller is about to throw away: searching "909" in a
        library of 90 000 items returned 200 hits with not one MPC program
        among them, so the MPC filter showed "No matches" while 49 real ones
        sat further down the ranking.

        THE LIMIT IS NOT A SILENT ONE, which is the same disease one level
        further on. Jan, 2026-10-02: searching "Synco" returned his file and
        searching "Sync" did not -- a SHORTER query lost a result a longer one
        had. Not a matcher bug: both are prefix matches on the same token. The
        file sat at rank 404 of 488, and 200 was the cut.

        MEASURED on this library (99 251 items, 8 167 containers), which is
        why the default moved from 200 to 1000:

            "s" 28 382      "sy" 6 283      "syn" 5 552
            "sync"  488     "synco" 11

        A cap is still needed -- one character really does match 28 000 rows --
        but 200 was below the answer set of an ordinary four-character query,
        and truncating without saying so is what made the result look broken
        rather than truncated. Callers should pair this with `count()` and say
        so; `SearchPage` is what does.
        """
        query = query.strip()
        if not query:
            return []
        fts_query = _fts_query(query)
        if not fts_query:
            return []
        where, params = self._where(fts_query, formats)
        sql = (
            "SELECT "
            + self._HIT_COLUMNS
            + " "
            + self._FROM
            + where
            + " ORDER BY rank LIMIT ?"
        )  # noqa: RUF100, S608
        params.append(limit)
        return self._hits_from_rows(self._search_rows(sql, params))

    def count(self, query: str, formats: Optional[list[str]] = None) -> int:  # noqa: UP045
        """How many rows `search()` WOULD match, ignoring its limit.

        The honest denominator for a truncated result. "No matches" while 288
        real ones sat below the cut is the failure this exists to prevent, and
        it is the same shape as the format-filter bug above: the limit is spent
        on rows the caller is about to discover it cannot show.

        A second query rather than a window function on the first, because
        `search()`'s return type is a plain list that seven tests iterate, and
        changing it to carry a total would have churned all of them for no
        gain. COUNT over an FTS match on a local file is cheap enough to run
        per keystroke, and the search box is debounced.

        `search_page()` no longer pays this: it takes the total from a
        `COUNT(*) OVER ()` on the same row set, so a page costs one FTS MATCH
        instead of two. It stays because a total without a page is a
        reasonable thing for a caller to want.
        """
        query = query.strip()
        if not query:
            return 0
        fts_query = _fts_query(query)
        if not fts_query:
            return 0
        where, params = self._where(fts_query, formats)
        sql = (
            "SELECT COUNT(*) FROM item_fts JOIN item ON item.id = item_fts.rowid"
            + where
        )  # noqa: RUF100, S608
        try:
            return int(self._conn.execute(sql, params).fetchone()[0])
        except sqlite3.OperationalError as ex:
            _LOG.warning("index count failed and returned 0: %s", ex, exc_info=True)
            return 0

    def search_page(
        self,
        query: str,
        limit: int = 1000,
        formats: Optional[list[str]] = None,  # noqa: UP045
    ) -> "SearchPage":  # noqa: RUF100, UP037, UP045
        """The page AND its true total, from ONE statement.

        TWO FTS MATCHes used to run for every keystroke -- `search()` for the
        page and `count()` for the denominator -- and the page itself cost one
        `_chain_for()` call per hit, each of which was a `while` loop over one
        SELECT per ancestor level. Measured before/after on a purpose-built
        8000-container / 112 000-item index where the query matches 96 000 of
        them (the review's own shape):

            OLD  search_page  477 ms / 4 200 statements
            NEW  search_page  440 ms / 96 021 statements

        The remaining time is FTS5 ranking 96 000 candidate rows (bm25 reads
        one docsize row per candidate) and no change to `search()` can remove
        it -- see the module note at the bottom of this finding. What this
        removes is the 3 000 round trips of the N+1 chain loop and one whole
        extra evaluation of the match.

        THE TOTAL RIDES ALONG AS A SCALAR SUBQUERY on the same statement,
        rather than as `COUNT(*) OVER ()`. That was tried first and measured
        655 ms against 425 ms for the plain matcher, because the window
        function forces every joined row through the sorter before LIMIT can
        drop them; an uncorrelated scalar subquery is evaluated once, costs
        what `count()` alone costs (22 ms here), and needs no second round
        trip. It is deliberately uncorrelated -- the subquery shadows both
        table names with its own FROM -- so SQLite evaluates it exactly once.
        `tests/manual_index_search_cost.py` asserts that, because a future
        planner that ran it per row would turn 12 ms into 12 seconds.

        A page whose query matches NOTHING still evaluates the subquery --
        it is in the SELECT list, so it is not short-circuited by an empty
        result set -- and gets 0, which is the right answer for it.
        """
        query = query.strip()
        if not query:
            return SearchPage(hits=[], total=0)
        fts_query = _fts_query(query)
        if not fts_query:
            return SearchPage(hits=[], total=0)
        where, params = self._where(fts_query, formats)
        count_sql = (
            "SELECT COUNT(*) FROM item_fts JOIN item ON item.id = item_fts.rowid"
            + where
        )  # noqa: RUF100, S608
        sql = (
            "SELECT "
            + self._HIT_COLUMNS
            + f", ({count_sql}) AS total_all "
            + self._FROM
            + where
            + " ORDER BY rank LIMIT ?"
        )
        # The subquery sits in the SELECT list, so its parameters come first.
        rows = self._search_rows(sql, params + params + [limit])
        total = int(rows[0][8]) if rows else 0
        # The total rides along as a ninth column; the hit builder wants the
        # eight it knows about.
        return SearchPage(
            hits=self._hits_from_rows([r[:8] for r in rows]),
            total=max(total, len(rows)),
        )

    def _chains_for(self, item_ids: list[int]) -> dict[int, list[ItemChainEntry]]:
        """Every ancestor chain for these items, from one recursive CTE.

        THE WHOLE FINDING. `_chain_for()` below was called once per hit and
        issued one `SELECT` per ancestor level, so the cost of a page was
        the page's size TIMES the tree's depth -- 200 SELECTs for a 100-hit
        page of presets each sitting under a bank. Here the page's ids are
        the seed of a single recursive walk up `item.parent_id`, and the
        chains are assembled in Python from the rows it returns, by id.

        Chunked at 500 ids because SQLite's parameter ceiling is 999 in the
        builds this has to run on, so a 1000-hit page is two statements
        rather than one that raises. The walk is bounded by a seen-set per
        chain as well, which no well-formed tree needs and a corrupted
        `parent_id` cycle would: without it the CTE itself would also spin.

        The shape is `_chain_for()`'s, unchanged and deliberately so --
        root -> ... -> the item itself, exclusive of the container, because
        that is what `_format_hit`'s `chain[:-1]` and `search_resolve`'s
        `chain[-1]` read.
        """
        out: dict[int, list[ItemChainEntry]] = {}
        # dict.fromkeys: the same item can legitimately appear twice if a
        # caller hands over two pages' worth of ids.
        seeds = list(dict.fromkeys(item_ids))
        for i in range(0, len(seeds), 500):
            chunk = seeds[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                "WITH RECURSIVE up(id, kind, name, native_id, parent_id) AS ("
                f"SELECT id, kind, name, native_id, parent_id FROM item WHERE id IN ({marks}) "
                "UNION ALL "
                "SELECT item.id, item.kind, item.name, item.native_id, item.parent_id "
                "FROM item JOIN up ON item.id = up.parent_id"
                ") SELECT id, kind, name, native_id, parent_id FROM up",
                chunk,
            ).fetchall()
            by_id = {r[0]: r for r in rows}
            for seed in chunk:
                chain: list[ItemChainEntry] = []
                seen: set[int] = set()
                current: Optional[int] = seed  # noqa: UP045
                while current is not None and current in by_id and current not in seen:
                    seen.add(current)
                    _, kind, name, native_id, parent_id = by_id[current]
                    chain.append(
                        ItemChainEntry(kind=kind, name=name, native_id=native_id)
                    )
                    current = parent_id
                chain.reverse()
                out[seed] = chain
        return out

    def _chain_for(self, item_id: int) -> list[ItemChainEntry]:
        """One item's chain. Kept for a caller that wants exactly one; the
        search path uses `_chains_for()`, which answers for a whole page at
        the cost of one statement instead of one per ancestor level."""
        return self._chains_for([item_id]).get(item_id, [])

    # -- stats --------------------------------------------------------------

    def stats(self) -> dict:
        c = self._conn.execute("SELECT COUNT(*) FROM container").fetchone()[0]
        i = self._conn.execute("SELECT COUNT(*) FROM item").fetchone()[0]
        return {"containers": c, "items": i}


def _fts_query(user_text: str) -> str:
    """A bare word list, AND-ed together as prefix matches — the closest
    FTS5 gets to a plain "substring-ish" search box without full trigram
    indexing. Quotes any token containing characters FTS5's default
    tokenizer would otherwise choke on (': ', '-', etc., common in real
    sample names like "kit:Beatnik'sKit").

    THE RESULT IS ALWAYS A VALID FTS5 EXPRESSION, OR EMPTY. It used to end
    `return ... if parts else user_text`, so a search of nothing but
    punctuation -- `"`, `&`, `-` -- handed back the raw keystroke, which is
    not an expression FTS5 accepts. SQLite raised OperationalError,
    `search()`'s `except` swallowed it, and the box said "No matches" with
    nothing anywhere saying why. An empty string is the honest answer for
    "there is no indexable token in this text", and `search`/`count` turn it
    into no hits without running any SQL at all."""
    parts = []
    for t in user_text.split():
        # A token with no letter or digit in it tokenizes to NOTHING, and an
        # empty term AND-ed into the query makes the whole query match
        # nothing. Real preset names hit this constantly: any name of the
        # form "<word> & <word>" splits into three tokens, the middle one
        # tokenizes away, and searching a preset by its own displayed name
        # returned zero results. Punctuation INSIDE a word is fine and must
        # stay -- "R&B" and "kit:Beatnik'sKit" tokenize normally.
        if not any(ch.isalnum() for ch in t):
            continue
        safe = t.replace('"', '""')
        parts.append(f'"{safe}"*')
    return " AND ".join(parts)
