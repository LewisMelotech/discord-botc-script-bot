"""A local SQLite record of the scripts this bot has served, used for autocomplete.

Deliberately free of any Discord import so it can be exercised on its own, and
BLOCKING throughout: sqlite3 has no async API, so every method here must be called
through :func:`asyncio.to_thread` rather than inline, exactly like
:mod:`botcbot.rendering`.

Only the handful of fields autocomplete needs are stored. In particular the script's
character JSON — which ``ScriptVersion.content`` carries around in memory — is never
written here; it is orders of magnitude larger than the label it would help build.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# A DM has no guild, and SQLite treats NULLs as distinct in a UNIQUE index, so a
# nullable guild column would let the same script accumulate one row per invocation.
_NO_GUILD: Final = 0

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS served_scripts (
    id        INTEGER PRIMARY KEY,
    script_id INTEGER NOT NULL,
    guild_id  INTEGER NOT NULL,
    name      TEXT    NOT NULL,
    name_fold TEXT    NOT NULL,
    author    TEXT,
    slug      TEXT,
    used_at   REAL    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS served_scripts_key
    ON served_scripts (script_id, guild_id);
CREATE INDEX IF NOT EXISTS served_scripts_recent
    ON served_scripts (used_at DESC);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS is a no-op on a
# table that already exists, so anything added to _SCHEMA never reaches a database
# already on disk and every later statement naming it raises instead. Migrating keeps
# the suggestions the bot has accumulated rather than asking for the file to be deleted.
_ADDED_COLUMNS: Final = {"slug": "TEXT"}


@dataclass(frozen=True, slots=True)
class CachedScript:
    script_id: int
    name: str
    author: str | None
    guild_id: int | None
    used_at: float
    slug: str | None = None


class ScriptCache:
    """Recently served scripts, keyed by ``(script_id, guild_id)``.

    ``max_entries`` bounds the table: the oldest rows past that many are deleted on
    every write. Zero disables the cache entirely and the database file is never
    created.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_entries: int = 500,
        timeout: float = 5.0,
    ) -> None:
        self._path = Path(path)
        self._max_entries = max(0, max_entries)
        self._timeout = timeout
        # One connection shared across the thread pool, serialised here. sqlite3 objects
        # are not safe to use concurrently even with check_same_thread switched off.
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def enabled(self) -> bool:
        return self._max_entries > 0

    def record(
        self,
        *,
        script_id: int,
        name: str,
        author: str | None = None,
        guild_id: int | None = None,
        slug: str | None = None,
        used_at: float | None = None,
    ) -> None:
        """BLOCKING. Insert or refresh one script, then evict anything past the bound."""
        if not self.enabled:
            return
        moment = time.time() if used_at is None else used_at
        with self._lock:
            connection = self._connect()
            with connection:
                connection.execute(
                    """
                    INSERT INTO served_scripts
                        (script_id, guild_id, name, name_fold, author, slug, used_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (script_id, guild_id) DO UPDATE SET
                        name      = excluded.name,
                        name_fold = excluded.name_fold,
                        author    = excluded.author,
                        slug      = excluded.slug,
                        used_at   = excluded.used_at
                    """,
                    (
                        int(script_id),
                        _NO_GUILD if guild_id is None else int(guild_id),
                        name,
                        name.casefold(),
                        author,
                        slug,
                        moment,
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM served_scripts WHERE id NOT IN (
                        SELECT id FROM served_scripts ORDER BY used_at DESC, id DESC LIMIT ?
                    )
                    """,
                    (self._max_entries,),
                )

    def suggest(
        self, query: str, *, guild_id: int | None = None, limit: int = 25
    ) -> list[CachedScript]:
        """BLOCKING. Scripts whose name or custom id contains ``query``, this guild's first.

        An empty query returns the most recently served scripts, which is the useful
        thing to show before the user has typed anything.
        """
        if not self.enabled or limit <= 0:
            return []

        folded = query.strip().casefold()
        guild_key = _NO_GUILD if guild_id is None else int(guild_id)

        sql = ["SELECT script_id, name, author, slug, guild_id, used_at FROM served_scripts"]
        params: list[object] = []
        if folded:
            # A slug is already lowercase ASCII, so the folded query compares to it
            # directly. instr(NULL, ...) is NULL, so a script with no slug simply fails
            # that half rather than needing a COALESCE.
            sql.append("WHERE instr(name_fold, ?) > 0 OR instr(slug, ?) > 0")
            params.append(folded)
            params.append(folded)
        sql.append("ORDER BY (guild_id = ?) DESC, used_at DESC, id DESC LIMIT ?")
        params.append(guild_key)
        # One script can hold a row per guild, so over-fetch to leave room for the
        # de-duplication below without short-changing the caller's limit.
        params.append(limit * 4)

        with self._lock:
            connection = self._connect()
            rows = connection.execute(" ".join(sql), params).fetchall()

        found: dict[int, CachedScript] = {}
        for script_id, name, author, slug, row_guild, used_at in rows:
            if script_id in found:
                continue
            found[script_id] = CachedScript(
                script_id=int(script_id),
                name=str(name),
                author=author,
                guild_id=None if row_guild == _NO_GUILD else int(row_guild),
                used_at=float(used_at),
                slug=slug,
            )
            if len(found) >= limit:
                break
        return list(found.values())

    def count(self) -> int:
        """BLOCKING. Rows currently stored."""
        if not self.enabled:
            return 0
        with self._lock:
            connection = self._connect()
            (total,) = connection.execute("SELECT COUNT(*) FROM served_scripts").fetchone()
        return int(total)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection

        parent = self._path.parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)

        connection = sqlite3.connect(
            self._path, timeout=self._timeout, check_same_thread=False
        )
        # WAL lets the autocomplete read run while a delivery is writing, instead of the
        # two blocking each other; busy_timeout makes a contended write wait rather than
        # raise "database is locked" immediately. Both matter because several
        # interactions are dispatched as independent tasks.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.executescript(_SCHEMA)
        _migrate(connection)
        connection.commit()
        self._connection = connection
        return connection


def _migrate(connection: sqlite3.Connection) -> None:
    """Add any column an older version of this file created the table without."""
    existing = {row[1] for row in connection.execute("PRAGMA table_info(served_scripts)")}
    for column, sql_type in _ADDED_COLUMNS.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE served_scripts ADD COLUMN {column} {sql_type}")


__all__ = ["CachedScript", "ScriptCache"]
