"""Persistent parse cache: per-file results that outlive the process.

Parsing is the part of an analysis that depends on a file's content alone, so its result can be kept on disk:
running ``repoviz review`` twice, restarting ``repoviz serve``, or building snapshots at many revisions parses each
distinct file content once.  Keys are the in-memory cache keys (analyzer, analyzer version, Git blob hash…): the
same content at two revisions shares one entry, and bumping an analyzer's ``version`` makes its old entries
unreachable (they age out) without touching any other analyzer's.

* **Store.** SQLite (standard library) at ``<state dir>/repos/<repo>/parse-cache.sqlite``, in WAL mode with a busy
  timeout, so a running server and a CLI run can share it.  The directory is ``0700`` and the files ``0600``.
* **Payloads.** Compact JSON, zlib-compressed, decoded into plain dataclasses by each analyzer's ``from_json``.
  Nothing is imported, unpickled or executed when an entry is read.
* **Two levels.** :class:`TwoLevelCache` looks like the dict the analyzers already use: memory first, then disk.
  New entries are written in one short transaction after each snapshot (:meth:`TwoLevelCache.flush`).
* **Hygiene.** A size cap (``[cache] max_mb``, default 500) with least-recently-used eviction; a schema version
  (a mismatch rebuilds the store); a corrupt file is moved aside and rebuilt, with a warning in the next snapshot.
  ``REPOVIZ_NO_DISK_CACHE=1`` or ``[cache] disk = false`` turns it off; ``repoviz cache info|clear`` inspects it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
FILE_NAME = "parse-cache.sqlite"
#: Namespaces (the first part of a cache key) that go to disk, with their codecs: value -> JSON data -> value.
Codec = tuple[Callable[[Any], Any], Callable[[Any], Any]]


def _codecs() -> dict[str, Codec]:
    from .analyzers.javascript import JsFileInfo
    from .analyzers.python import PyFileInfo

    same: Codec = (lambda v: v, lambda d: d)
    return {
        "python": (lambda v: v.to_json(), PyFileInfo.from_json),
        "javascript": (lambda v: v.to_json(), JsFileInfo.from_json),
        "git-churn": same,  # {path: {...}}: plain JSON already
    }


def disabled_by_env() -> bool:
    return os.environ.get("REPOVIZ_NO_DISK_CACHE", "").strip().lower() not in ("", "0", "false", "no")


def _key_text(key: tuple[Any, ...]) -> str:
    return json.dumps(list(key), separators=(",", ":"), default=str)


class DiskCache:
    """The SQLite store.  Every method is safe to call from several threads, and never raises for cache trouble:
    a cache that cannot be read is a cache miss."""

    def __init__(self, directory: Path, max_bytes: int) -> None:
        self.path = Path(directory) / FILE_NAME
        self.max_bytes = max_bytes
        self.problem: str | None = None  # the last reset, for a diagnostic
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._broken = False
        self._resetting = False

    # -- connection ---------------------------------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection | None:
        if self._conn is not None or self._broken:
            return self._conn
        from .session import _mkdir_private

        try:
            _mkdir_private(self.path.parent)
            if not self.path.exists():  # created 0600 before SQLite opens it
                os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = conn.execute("SELECT value FROM meta WHERE name = 'schema'").fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                conn.execute("DROP TABLE IF EXISTS entries")
                conn.execute("INSERT OR REPLACE INTO meta (name, value) VALUES ('schema', ?)", (SCHEMA_VERSION,))
            conn.execute("CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, ns TEXT NOT NULL, "
                         "payload BLOB NOT NULL, size INTEGER NOT NULL, created_at INTEGER NOT NULL, "
                         "last_used_at INTEGER NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS entries_lru ON entries (last_used_at)")
            conn.execute("PRAGMA quick_check(1)").fetchone()
            for extra in ("-wal", "-shm"):
                p = Path(str(self.path) + extra)
                if p.exists():
                    os.chmod(p, 0o600)
            self._conn = conn
        except sqlite3.DatabaseError as exc:
            self._reset(f"the parse cache was unreadable ({exc}); it was moved aside and rebuilt")
        except OSError as exc:  # no writable state directory: run without a disk cache
            log.info("parse cache disabled: %s", exc)
            self._broken = True
        return self._conn

    def _reset(self, why: str) -> None:
        """Move a damaged store aside (kept for a post-mortem, never read again) and start empty, once."""
        self.problem = why
        log.warning(why)
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        if self._resetting:  # the fresh store failed too: go on without a disk cache
            self._broken = True
            return
        for extra in ("", "-wal", "-shm"):
            p = Path(str(self.path) + extra)
            try:
                if p.exists():
                    p.replace(p.with_name(p.name + ".corrupt"))
            except OSError:
                self._broken = True
                return
        self._resetting = True
        try:
            self._open()
        finally:
            self._resetting = False

    # -- entries --------------------------------------------------------------------------------------------------

    def get(self, key: tuple[Any, ...], decode: Callable[[Any], Any]) -> tuple[bool, Any]:
        """``(True, value)`` on a hit, ``(False, None)`` on a miss or an unreadable entry."""
        text = _key_text(key)
        with self._lock:
            conn = self._open()
            if conn is None:
                return False, None
            try:
                row = conn.execute("SELECT payload FROM entries WHERE key = ?", (text,)).fetchone()
            except sqlite3.OperationalError:  # busy beyond the timeout: a miss
                return False, None
            except sqlite3.DatabaseError as exc:
                self._reset(f"the parse cache was unreadable ({exc}); it was moved aside and rebuilt")
                return False, None
        if row is None:
            return False, None
        try:
            return True, decode(json.loads(zlib.decompress(row[0])))
        except (ValueError, KeyError, TypeError, zlib.error):  # stale or damaged entry: recompute
            return False, None

    def put_many(self, items: list[tuple[tuple[Any, ...], str, Any]], touched: list[tuple[Any, ...]]) -> None:
        """Store ``(key, namespace, data)`` items and mark ``touched`` keys as used, in one transaction."""
        now = int(time.time())
        rows = []
        for key, ns, data in items:
            try:
                blob = zlib.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"), 3)
            except (TypeError, ValueError):
                continue
            rows.append((_key_text(key), ns, blob, len(blob), now, now))
        with self._lock:
            conn = self._open()
            if conn is None or (not rows and not touched):
                return
            for attempt in range(3):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.executemany("INSERT OR REPLACE INTO entries (key, ns, payload, size, created_at, "
                                     "last_used_at) VALUES (?, ?, ?, ?, ?, ?)", rows)
                    conn.executemany("UPDATE entries SET last_used_at = ? WHERE key = ?",
                                     [(now, _key_text(k)) for k in touched])
                    conn.execute("COMMIT")
                    break
                except sqlite3.OperationalError:  # busy (another process writing): wait a little, retry
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    time.sleep(0.2 * (attempt + 1))
                except sqlite3.DatabaseError as exc:
                    self._reset(f"the parse cache was unreadable ({exc}); it was moved aside and rebuilt")
                    return
            self._evict(conn)

    def _evict(self, conn: sqlite3.Connection) -> None:
        """Least recently used entries go until the store is back under 90% of its cap."""
        try:
            total = conn.execute("SELECT COALESCE(SUM(size), 0) FROM entries").fetchone()[0]
            if total <= self.max_bytes:
                return
            target, freed, doomed = total - int(self.max_bytes * 0.9), 0, []
            for key, size in conn.execute("SELECT key, size FROM entries ORDER BY last_used_at, key"):
                doomed.append((key,))
                freed += size
                if freed >= target:
                    break
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("DELETE FROM entries WHERE key = ?", doomed)
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            log.info("parse cache eviction skipped: %s", exc)

    def info(self) -> dict[str, Any]:
        with self._lock:
            conn = self._open()
            out: dict[str, Any] = {"path": str(self.path), "max_mb": round(self.max_bytes / 1e6, 1), "entries": 0,
                                   "payload_mb": 0.0, "by_namespace": {}}
            if conn is None:
                out["note"] = "unavailable (no writable state directory)"
                return out
            for ns, n, size in conn.execute("SELECT ns, COUNT(*), SUM(size) FROM entries GROUP BY ns ORDER BY ns"):
                out["by_namespace"][ns] = {"entries": n, "payload_mb": round(size / 1e6, 2)}
                out["entries"] += n
                out["payload_mb"] = round(out["payload_mb"] + size / 1e6, 2)
            try:
                out["file_mb"] = round(sum(Path(str(self.path) + x).stat().st_size for x in ("", "-wal")
                                           if Path(str(self.path) + x).exists()) / 1e6, 2)
            except OSError:
                pass
            return out

    def clear(self) -> int:
        with self._lock:
            conn = self._open()
            if conn is None:
                return 0
            n = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            conn.execute("DELETE FROM entries")
            conn.execute("VACUUM")
            return n

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


class TwoLevelCache:
    """The analyzers' per-file cache: a dict in memory, backed by :class:`DiskCache` for the namespaces that have
    a codec (other keys stay in memory).  ``disk_misses`` counts entries computed because neither level had them."""

    def __init__(self, disk: DiskCache | None) -> None:
        self.memory: dict[Any, Any] = {}
        self.disk = disk
        self._codecs = _codecs() if disk is not None else {}
        self._pending: dict[tuple[Any, ...], Any] = {}
        self._touched: set[tuple[Any, ...]] = set()
        self._lock = threading.Lock()
        self.stats = {"memory_hits": 0, "disk_hits": 0, "misses": 0, "written": 0}

    def _codec(self, key: Any) -> Codec | None:
        return self._codecs.get(key[0]) if isinstance(key, tuple) and key and isinstance(key[0], str) else None

    def __contains__(self, key: Any) -> bool:
        if key in self.memory:
            self.stats["memory_hits"] += 1
            return True
        codec = self._codec(key)
        if codec is None or self.disk is None:
            return False
        hit, value = self.disk.get(key, codec[1])
        if hit:
            with self._lock:
                self.memory[key] = value
                self._touched.add(key)
            self.stats["disk_hits"] += 1
            return True
        self.stats["misses"] += 1
        return False

    def __getitem__(self, key: Any) -> Any:
        if key in self.memory:
            return self.memory[key]
        if key in self:  # loads it from disk
            return self.memory[key]
        raise KeyError(key)

    def get(self, key: Any, default: Any = None) -> Any:
        return self[key] if key in self else default

    def __setitem__(self, key: Any, value: Any) -> None:
        with self._lock:
            self.memory[key] = value
            if self._codec(key) is not None and self.disk is not None:
                self._pending[key] = value

    def __len__(self) -> int:
        return len(self.memory)

    def clear(self) -> None:
        """Forget the memory level (the disk keeps its entries)."""
        with self._lock:
            self.memory.clear()

    def flush(self) -> None:
        """Write new entries (and the use of old ones) to disk, in one short transaction."""
        if self.disk is None:
            return
        with self._lock:
            pending, touched = self._pending, list(self._touched)
            self._pending, self._touched = {}, set()
        if not pending and not touched:
            return
        items = []
        for key, value in pending.items():
            codec = self._codec(key)
            if codec is not None:
                try:
                    items.append((key, key[0], codec[0](value)))
                except Exception:  # noqa: BLE001 - an unencodable value simply stays in memory
                    continue
        self.disk.put_many(items, touched)
        self.stats["written"] += len(items)


def file_cache_for(state_dir: Path, config: Any) -> TwoLevelCache | dict[Any, Any]:
    """The per-file cache a repository uses: two levels, or a plain dict when the disk cache is off."""
    if disabled_by_env() or not getattr(config, "cache_disk", True):
        return {}
    return TwoLevelCache(DiskCache(state_dir, int(getattr(config, "cache_max_mb", 500.0) * 1e6)))
