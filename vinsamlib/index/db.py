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

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
    native_id: Optional[str]


@dataclass
class SearchResult:
    item_id: int
    kind: str
    name: str
    format: str
    container_path: str
    chain: list[ItemChainEntry]   # root -> ... -> this item (exclusive of the container itself)


class IndexDB:
    def __init__(self, path: Path):
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
        self._migrate()
        self._conn.commit()

    #: Bumped whenever a scan would now record something it did not before.
    #: The index is a DERIVED CACHE -- its own docstring says File > Rescan
    #: Library rebuilds it from scratch and nothing here is a source of truth
    #: -- so an old one is emptied rather than migrated in place. Migrating
    #: would leave rows that are structurally current and factually blank,
    #: which is the state hardest to tell from a real zero.
    SCHEMA_VERSION = 2

    def _migrate(self) -> None:
        """Add columns an older file lacks, and empty it if it predates them.

        `CREATE TABLE IF NOT EXISTS` does nothing to a table that already
        exists, so a new column has to be added by hand -- silently, since a
        file created by this version already has it.
        """
        for table, column, decl in (("container", "audio_bytes", "INTEGER"),
                                     ("item", "audio_bytes", "INTEGER")):
            cols = {r[1] for r in self._conn.execute(
                f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        have = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if have < self.SCHEMA_VERSION:
            # Everything scanned before this version has NULL audio_bytes and
            # would show no size for the rest of its life, because
            # needs_rescan() only looks at size and mtime and nothing about
            # the file changed. Clearing container cascades to item and makes
            # the next scan repopulate.
            self._conn.execute("DELETE FROM container")
            self._conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    # -- scanning support -----------------------------------------------------

    def needs_rescan(self, path: str, size: int, mtime: float) -> bool:
        row = self._conn.execute(
            "SELECT size, mtime FROM container WHERE path = ?", (path,)).fetchone()
        if row is None:
            return True
        return row[0] != size or row[1] != mtime

    def begin_container(self, path: str, kind: str, format: str, size: int, mtime: float) -> int:
        """(Re)register a container and wipe its previous items — the
        scanner rebuilds them fresh on every rescan rather than diffing."""
        cur = self._conn.execute(
            "INSERT INTO container(path, kind, format, size, mtime, scanned_at, error) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL) "
            "ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, format=excluded.format, "
            "size=excluded.size, mtime=excluded.mtime, scanned_at=NULL, error=NULL",
            (path, kind, format, size, mtime))
        container_id = cur.lastrowid or self._conn.execute(
            "SELECT id FROM container WHERE path = ?", (path,)).fetchone()[0]
        self._conn.execute("DELETE FROM item WHERE container_id = ?", (container_id,))
        return container_id

    def add_item(self, container_id: int, parent_id: Optional[int], kind: str,
                 name: str, native_id: Optional[str] = None, format: str = "",
                 size: int = 0, ordinal: int = 0,
                 audio_bytes: Optional[int] = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO item(container_id, parent_id, kind, name, native_id, format, "
            "size, ordinal, audio_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (container_id, parent_id, kind, name, native_id, format, size, ordinal,
             audio_bytes))
        return cur.lastrowid

    def set_item_audio_bytes(self, item_id: int, audio_bytes: int) -> None:
        """Fill in a row's audio total once its children have been walked.

        A bank's own figure is only known after its presets have been read,
        and the row has to exist first to be their parent -- so it is written
        in two steps rather than held back until the end.
        """
        self._conn.execute("UPDATE item SET audio_bytes = ? WHERE id = ?",
                            (audio_bytes, item_id))

    def set_container_audio_bytes(self, container_id: int, audio_bytes: int) -> None:
        """The container's own deduped audio total, for the tree's row."""
        self._conn.execute("UPDATE container SET audio_bytes = ? WHERE id = ?",
                            (audio_bytes, container_id))

    def finish_container(self, container_id: int, error: Optional[str] = None) -> None:
        self._conn.execute("UPDATE container SET scanned_at = ?, error = ? WHERE id = ?",
                            (time.time(), error, container_id))
        self._conn.commit()

    def forget_container(self, path: str) -> None:
        self._conn.execute("DELETE FROM container WHERE path = ?", (path,))
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
            "DELETE FROM container WHERE path = ? OR path LIKE ?",
            (root, prefix + "%"))
        self._conn.commit()

    def all_container_paths(self) -> list[str]:
        return [r[0] for r in self._conn.execute("SELECT path FROM container")]

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
            chunk = paths[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for path, total in self._conn.execute(
                    f"SELECT path, audio_bytes FROM container "
                    f"WHERE path IN ({marks}) AND audio_bytes IS NOT NULL",
                    chunk):
                out[path] = total
        return out

    def search(self, query: str, limit: int = 200) -> list[SearchResult]:
        query = query.strip()
        if not query:
            return []
        fts_query = _fts_query(query)
        try:
            rows = self._conn.execute(
                "SELECT item.id, item.kind, item.name, item.format, container.path "
                "FROM item_fts JOIN item ON item.id = item_fts.rowid "
                "JOIN container ON container.id = item.container_id "
                "WHERE item_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, limit)).fetchall()
        except sqlite3.OperationalError:
            # A background scan's writer connection briefly held the file
            # (WAL keeps this rare — see __init__) — better to show no
            # results for one keystroke than to crash the search box.
            return []
        out = []
        for item_id, kind, name, fmt, container_path in rows:
            out.append(SearchResult(item_id=item_id, kind=kind, name=name, format=fmt or "",
                                     container_path=container_path, chain=self._chain_for(item_id)))
        return out

    def _chain_for(self, item_id: int) -> list[ItemChainEntry]:
        chain: list[ItemChainEntry] = []
        current = item_id
        while current is not None:
            row = self._conn.execute(
                "SELECT kind, name, native_id, parent_id FROM item WHERE id = ?",
                (current,)).fetchone()
            if row is None:
                break
            kind, name, native_id, parent_id = row
            chain.append(ItemChainEntry(kind=kind, name=name, native_id=native_id))
            current = parent_id
        chain.reverse()
        return chain

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
    sample names like "kit:Beatnik'sKit")."""
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
    return " AND ".join(parts) if parts else user_text
