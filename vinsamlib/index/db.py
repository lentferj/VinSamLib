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
    error TEXT
);

CREATE TABLE IF NOT EXISTS item (
    id INTEGER PRIMARY KEY,
    container_id INTEGER NOT NULL REFERENCES container(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES item(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,           -- 'folder' | 'bank' | 'preset'
    name TEXT NOT NULL,
    native_id TEXT,               -- entry name (folder/bank) or preset/program id (preset)
    format TEXT,                  -- 'E4B' | 'KRZ', for bank/preset rows
    size INTEGER,
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
        self._conn.commit()

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
                 size: int = 0, ordinal: int = 0) -> int:
        cur = self._conn.execute(
            "INSERT INTO item(container_id, parent_id, kind, name, native_id, format, size, ordinal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (container_id, parent_id, kind, name, native_id, format, size, ordinal))
        return cur.lastrowid

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
    tokens = user_text.split()
    parts = []
    for t in tokens:
        safe = t.replace('"', '""')
        parts.append(f'"{safe}"*')
    return " AND ".join(parts) if parts else user_text
