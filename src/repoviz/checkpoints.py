"""Checkpoints and an edit timeline inside a work session.

Agents work for a long time without committing, and at the end the reviewer sees only the final state.  A
*checkpoint* records the working tree at one moment (the commit checked out plus copies of the files that differ
from it), so a wave can be reviewed step by step: checkpoint 2 → 3, or everything since checkpoint 3.
Checkpoint 0 is the session's baseline.

Checkpoints come from ``repoviz session checkpoint`` (by hand, or from an agent hook after each edit), the live
app's *Mark checkpoint* button, and the live server itself (an *auto* checkpoint when the working tree changed,
at most every ``[activity] checkpoint_seconds``).  ``repoviz session note`` adds a timeline event without
recording files.

Everything lives in the session's private directory (owner-only, never in the repository)::

    sessions/<id>/timeline.json          checkpoint metadata (files changed since the previous one, ±lines)
                                         and timeline notes
    sessions/<id>/checkpoints/<n>.json   the state of checkpoint n: HEAD and the files that differ from it
    sessions/<id>/files/<blob>           file copies, shared with the baseline and deduplicated by hash
    sessions/<id>/statcache.json         (size, mtime, inode) → hash, so unchanged files are not read again

A session keeps at most ``max_checkpoints``: the oldest automatic ones go first, and a removed checkpoint's
changes are folded into the next one.  Copies no checkpoint needs any more are deleted.

This module is imported by the ``repoviz session checkpoint`` fast path (see :mod:`repoviz.entry`), so it must
not import the analysis code: agent hooks call it after every edit.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Iterator

from .gitutil import Git
from .ids import content_hash, stable_hash
from .redact import redact
from .session import (MAX_BASELINE_FILE_BYTES, Session, StateStore, _atomic_write, _mkdir_private, _write_private,
                      utcnow)
from .sources import GitRevisionSource, TreeSource

MAX_CHECKPOINTS = 200
MAX_EVENTS = 1000
MAX_LISTED = 200  # changed files listed per checkpoint
MAX_DIFFED = 50  # files whose lines are counted per checkpoint
MAX_DIFF_BYTES = 300_000
RACY_SECONDS = 2.0  # a file modified this recently is read again even if its size and mtime look unchanged
GC_GRACE_SECONDS = 60.0  # copies this recent are never deleted (another process may be about to record them)
REWORKED = 3  # changed in this many checkpoints: "reworked"



def _clean(text: str, limit: int) -> str:
    """Labels and notes come from hooks and people: one line, bounded, credential-looking values redacted."""
    return redact(" ".join(str(text or "").split()))[:limit]


# --------------------------------------------------------------------------- storage


def _dir(state: StateStore, session: Session | str) -> Path:
    return state._session_dir(session if isinstance(session, str) else session.id)


@contextlib.contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """One writer at a time per session, across processes (the server and a hook can record at once)."""
    _mkdir_private(directory)
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows: the in-process lock of StateStore still applies
            pass
        yield
    finally:
        os.close(fd)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _load(directory: Path) -> dict[str, Any]:
    data = _read_json(directory / "timeline.json", {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("checkpoints", [])
    data.setdefault("events", [])
    data.setdefault("next", 1 + max((c["n"] for c in data["checkpoints"]), default=0))
    return data


def _save(directory: Path, data: dict[str, Any]) -> None:
    _atomic_write(directory / "timeline.json", json.dumps(data, indent=1))


def _state_path(directory: Path, n: int) -> Path:
    return directory / "checkpoints" / f"{int(n)}.json"


def _baseline_state(session: Session) -> dict[str, Any]:
    return {"head": session.baseline_head, "overrides": session.overrides, "submodules": session.submodules,
            "submodule_overrides": session.submodule_overrides}


def load_state(state: StateStore, session: Session, n: int) -> dict[str, Any]:
    """The recorded state of checkpoint ``n`` (0 is the baseline); ``ValueError`` when there is none."""
    if n == 0:
        return _baseline_state(session)
    data = _read_json(_state_path(_dir(state, session), n), None)
    if not isinstance(data, dict):
        raise ValueError(f"session {session.id} has no checkpoint {n} (see 'repoviz session timeline'; old "
                         "automatic checkpoints are removed after the limit)")
    return data


def _fingerprint(st: dict[str, Any]) -> str:
    return stable_hash("checkpoint", st.get("head") or "",
                       *(f"{p}\0{d or '-'}" for p, d in sorted((st.get("overrides") or {}).items())),
                       *(f"s\0{k}\0{v}" for k, v in sorted((st.get("submodules") or {}).items())),
                       *(f"S\0{p}\0{d or '-'}" for p, d in sorted((st.get("submodule_overrides") or {}).items())))


# --------------------------------------------------------------------------- capture


def _capture(state: StateStore, git: Git, root: Path, session: Session) -> dict[str, Any]:
    """HEAD and the files that differ from it, like a session baseline, reading only files that changed."""
    directory = _dir(state, session)
    files_dir = directory / "files"
    _mkdir_private(files_dir)
    cache = _read_json(directory / "statcache.json", {})
    fresh: dict[str, list[Any]] = {}
    now = time.time()
    overrides: dict[str, str | None] = {}
    skipped: list[str] = []
    for entry in git.status():
        for path in [entry.path] + ([entry.orig_path] if entry.orig_path else []):
            if path in overrides:
                continue
            p = root / path
            try:
                st = p.lstat()
            except OSError:
                overrides[path] = None
                continue
            if stat.S_ISDIR(st.st_mode):  # a submodule whose checked-out commit moved: recorded separately
                continue
            if not stat.S_ISREG(st.st_mode):
                overrides[path] = None
                continue
            if st.st_size > MAX_BASELINE_FILE_BYTES:
                skipped.append(path)
                continue
            key = [st.st_size, st.st_mtime_ns, st.st_ino]
            hit = cache.get(path) if isinstance(cache, dict) else None
            if isinstance(hit, list) and hit[:3] == key and now - st.st_mtime > RACY_SECONDS and \
                    (files_dir / str(hit[3])).exists():
                digest = str(hit[3])
            else:
                try:
                    data = p.read_bytes()
                except OSError:
                    skipped.append(path)
                    continue
                digest = content_hash(data)
                if not (files_dir / digest).exists():
                    _write_private(files_dir / digest, data)
            fresh[path] = key + [digest]
            overrides[path] = digest
    if fresh != cache:
        _atomic_write(directory / "statcache.json", json.dumps(fresh))
    submodules, sub_overrides = state._capture_submodules(git, root, session.id)
    return {"head": git.head(), "branch": git.branch(), "overrides": overrides, "skipped": skipped,
            "submodules": submodules, "submodule_overrides": sub_overrides}


# --------------------------------------------------------------------------- comparing two states


class _Reader:
    """Contents and hashes of paths in one recorded state (copies first, else the commit)."""

    def __init__(self, git: Git, files_dir: Path, st: dict[str, Any]) -> None:
        self.git, self.files_dir, self.st = git, files_dir, st
        self.overrides: dict[str, str | None] = st.get("overrides") or {}
        self.head: str | None = st.get("head")
        self._tree: GitRevisionSource | None = None
        self._ids: dict[str, str | None] = {}

    def tree(self) -> GitRevisionSource | None:
        if self._tree is None and self.head:
            self._tree = GitRevisionSource(self.git, self.head)
        return self._tree

    def ids(self, paths: list[str]) -> None:
        """Blob hashes at HEAD, in a few ``git ls-tree`` calls (content hashes in SHA-256 repositories)."""
        todo = [p for p in paths if p not in self.overrides and p not in self._ids]
        for p in todo:
            self._ids[p] = None
        if not todo or not self.head:
            return
        for i in range(0, len(todo), 200):
            out = self.git.try_run("ls-tree", "-z", "--full-tree", self.head, "--", *todo[i:i + 200]) or ""
            for rec in out.split("\0"):
                meta, _, path = rec.partition("\t")
                parts = meta.split()
                if len(parts) == 3 and parts[1] == "blob":
                    oid = parts[2]
                    if len(oid) != 40:  # another object format: compare contents instead
                        data = self.tree().read_bytes(path) if self.tree() else None
                        oid = content_hash(data) if data is not None else oid
                    self._ids[path] = oid

    def id(self, path: str) -> str | None:
        return self.overrides[path] if path in self.overrides else self._ids.get(path)

    def read(self, path: str) -> bytes | None:
        if path in self.overrides:
            digest = self.overrides[path]
            if digest is None:
                return None
            try:
                return (self.files_dir / digest).read_bytes()
            except OSError:
                return None
        tree = self.tree()
        return tree.read_bytes(path) if tree is not None else None


def _line_counts(a: bytes | None, b: bytes | None) -> tuple[int | None, int | None]:
    if any(x is not None and (len(x) > MAX_DIFF_BYTES or b"\0" in x[:8000]) for x in (a, b)):
        return None, None
    old = (a or b"").decode("utf-8", "replace").splitlines()
    new = (b or b"").decode("utf-8", "replace").splitlines()
    added = removed = 0
    for line in difflib.unified_diff(old, new, n=0, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def changed_between(git: Git, files_dir: Path, a: dict[str, Any], b: dict[str, Any],
                    count_lines: bool = True) -> list[dict[str, Any]]:
    """Files whose content differs between two recorded states, with ±lines for the first :data:`MAX_DIFFED`."""
    ra, rb = _Reader(git, files_dir, a), _Reader(git, files_dir, b)
    candidates = set(ra.overrides) | set(rb.overrides)
    if ra.head != rb.head:
        if ra.head and rb.head:
            candidates |= set(git.changed_paths(ra.head, rb.head))
        else:
            head = ra.head or rb.head
            out = git.try_run("ls-tree", "-r", "-z", "--name-only", "--full-tree", head) if head else ""
            candidates |= {p for p in (out or "").split("\0") if p}
    paths = sorted(candidates)
    ra.ids(paths)
    rb.ids(paths)
    out: list[dict[str, Any]] = []
    for path in paths:
        ia, ib = ra.id(path), rb.id(path)
        if ia == ib:
            continue
        item: dict[str, Any] = {"path": path, "status": "added" if ia is None else "removed" if ib is None else "modified"}
        if count_lines and len(out) < MAX_DIFFED:
            item["added"], item["removed"] = _line_counts(ra.read(path), rb.read(path))
        out.append(item)
    return out


# --------------------------------------------------------------------------- recording


def _meta_totals(changed: list[dict[str, Any]]) -> dict[str, Any]:
    return {"files": len(changed), "lines_added": sum(c.get("added") or 0 for c in changed),
            "lines_removed": sum(c.get("removed") or 0 for c in changed), "changed": changed[:MAX_LISTED]}


def create(state: StateStore, git: Git, root: Path, session: Session, *, label: str = "", origin: str = "manual",
           max_checkpoints: int = MAX_CHECKPOINTS) -> tuple[dict[str, Any] | None, bool]:
    """Record a checkpoint of the working tree: ``(its metadata, True)``, or ``(the latest checkpoint or None,
    False)`` when no file changed since the previous one, so calling it again is harmless."""
    directory = _dir(state, session)
    with _locked(directory):
        tl = _load(directory)
        cur = _capture(state, git, root, session)
        fp = _fingerprint(cur)
        last = tl["checkpoints"][-1] if tl["checkpoints"] else None
        if last is not None and last.get("state") == fp:
            return last, False
        try:
            prev = load_state(state, session, last["n"]) if last else _baseline_state(session)
        except ValueError:
            prev = _baseline_state(session)
        if last is None and _fingerprint(prev) == fp:
            return None, False
        changed = changed_between(git, directory / "files", prev, cur)
        if not changed:
            if last is not None:  # same contents, recorded differently (e.g. just committed): remember it
                last["state"] = fp
                _atomic_write(_state_path(directory, last["n"]), json.dumps(cur))
                _save(directory, tl)
            return last, False
        n = int(tl["next"])
        _mkdir_private(directory / "checkpoints")
        _atomic_write(_state_path(directory, n), json.dumps(cur))
        meta = {"n": n, "at": utcnow(), "label": _clean(label, 200), "origin": origin, "head": cur["head"],
                "branch": cur["branch"], "previous": last["n"] if last else 0, "state": fp, **_meta_totals(changed)}
        if cur["skipped"]:
            meta["skipped"] = cur["skipped"][:20]
        tl["checkpoints"].append(meta)
        tl["next"] = n + 1
        if _prune(directory, tl, max(2, max_checkpoints)):
            _gc(directory, session, tl)
        _save(directory, tl)
        return meta, True


def _prune(directory: Path, tl: dict[str, Any], limit: int) -> bool:
    """Keep ``limit`` checkpoints: drop the oldest automatic ones first; a dropped checkpoint's changes are folded
    into the next one, so the timeline still adds up."""
    cps = tl["checkpoints"]
    pruned = False
    while len(cps) > limit:
        i = next((i for i, c in enumerate(cps[:-1]) if c.get("origin") == "auto"), 0)
        gone, after = cps.pop(i), cps[i]
        merged = {c["path"]: dict(c) for c in gone.get("changed", [])}
        for c in after.get("changed", []):
            if c["path"] in merged:
                earlier = merged[c["path"]]
                both = {k: (earlier.get(k) or 0) + (c.get(k) or 0) if earlier.get(k) is not None and c.get(k)
                        is not None else None for k in ("added", "removed")}
                status = "added" if earlier["status"] == "added" and c["status"] != "removed" else c["status"]
                merged[c["path"]] = {**c, **both, "status": status}
            else:
                merged[c["path"]] = c
        after.update(previous=gone.get("previous", 0), merged=after.get("merged", 0) + 1 + gone.get("merged", 0),
                     **_meta_totals(sorted(merged.values(), key=lambda c: c["path"])))
        with contextlib.suppress(OSError):
            _state_path(directory, gone["n"]).unlink()
        pruned = True
    return pruned


def _gc(directory: Path, session: Session, tl: dict[str, Any]) -> int:
    """Delete file copies that neither the baseline, the end state nor any checkpoint needs."""
    keep = {d for d in list(session.overrides.values()) + list(session.end_overrides.values())
            + list(session.submodule_overrides.values()) + list(session.end_submodule_overrides.values()) if d}
    for c in tl["checkpoints"]:
        st = _read_json(_state_path(directory, c["n"]), {})
        keep |= {d for d in list((st.get("overrides") or {}).values())
                 + list((st.get("submodule_overrides") or {}).values()) if d}
    removed = 0
    now = time.time()
    files_dir = directory / "files"
    for f in files_dir.iterdir() if files_dir.is_dir() else []:
        try:
            if f.name not in keep and now - f.stat().st_mtime > GC_GRACE_SECONDS:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def note(state: StateStore, session: Session, *, tool: str = "", file: str = "", message: str = "") -> dict[str, Any]:
    """Add an event to the session's timeline (what an agent says it did), without recording files."""
    directory = _dir(state, session)
    with _locked(directory):
        tl = _load(directory)
        last = tl["checkpoints"][-1]["n"] if tl["checkpoints"] else 0
        event = {"at": utcnow(), "tool": _clean(tool, 40), "file": _clean(file, 300), "message": _clean(message, 500),
                 "after": last}
        tl["events"] = (tl["events"] + [event])[-MAX_EVENTS:]
        _save(directory, tl)
        return event


# --------------------------------------------------------------------------- reading


def timeline(state: StateStore, session: Session | None) -> dict[str, Any]:
    """The session's checkpoints (newest last) and notes, with the files reworked in several checkpoints."""
    if session is None:
        return {"session": None, "checkpoints": [], "events": [], "reworked": [], "version": "none"}
    tl = _load(_dir(state, session))
    counts: dict[str, int] = {}
    for c in tl["checkpoints"]:
        for ch in c.get("changed", []):
            counts[ch["path"]] = counts.get(ch["path"], 0) + 1
    reworked = sorted(({"path": p, "times": n} for p, n in counts.items() if n >= REWORKED),
                      key=lambda r: (-r["times"], r["path"]))[:50]
    checkpoints = [{k: v for k, v in c.items() if k != "state"} for c in tl["checkpoints"]]
    last_event = tl["events"][-1]["at"] if tl["events"] else ""
    return {"session": {"id": session.id, "label": session.label, "started_at": session.started_at,
                        "ended_at": session.ended_at, "active": session.active},
            "checkpoints": checkpoints, "events": tl["events"][-200:], "reworked": reworked,
            "version": stable_hash("timeline", session.id, str(tl["next"]), str(len(checkpoints)),
                                   str(len(tl["events"])), last_event, length=12)}


def checkpoint_source(state: StateStore, session: Session, git: Git | None, n: int) -> TreeSource:
    """The files of checkpoint ``n`` (0 is the session baseline)."""
    if n == 0:
        return state.baseline_source(session, git)
    st = load_state(state, session, n)
    meta = next((c for c in _load(_dir(state, session))["checkpoints"] if c["n"] == n), {})
    label = f"checkpoint {n}" + (f" ({meta['label']})" if meta.get("label") else "")
    return state._overlay(session, git, st.get("head"), st.get("overrides") or {}, label, "checkpoint",
                          f"checkpoint:{session.id}:{n}", st.get("submodules"), st.get("submodule_overrides"))


def checkpoint_head(state: StateStore, session: Session, n: int) -> tuple[str | None, bool]:
    """(commit checked out, whether files differed from it) at checkpoint ``n``."""
    st = load_state(state, session, n)
    return st.get("head"), bool(st.get("overrides") or st.get("submodule_overrides"))


def prune_sessions(state: StateStore, days: float) -> dict[str, int]:
    """Delete the checkpoints of sessions that ended more than ``days`` ago, and the copies only they needed
    (baselines and end states stay, so those waves can still be reviewed)."""
    import datetime as _dt

    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
    out = {"sessions": 0, "checkpoints": 0, "files": 0}
    for s in state.list_sessions():
        if s.active or not s.ended_at:
            continue
        try:
            ended = _dt.datetime.fromisoformat(s.ended_at)
        except ValueError:
            continue
        directory = _dir(state, s)
        if ended > cutoff or not (directory / "timeline.json").exists():
            continue
        with _locked(directory):
            tl = _load(directory)
            out["checkpoints"] += len(tl["checkpoints"])
            for c in tl["checkpoints"]:
                with contextlib.suppress(OSError):
                    _state_path(directory, c["n"]).unlink()
            tl["checkpoints"] = []
            _save(directory, tl)
            with contextlib.suppress(OSError):
                (directory / "statcache.json").unlink()
            files_dir = directory / "files"
            before = len(list(files_dir.iterdir())) if files_dir.is_dir() else 0
            _gc(directory, s, tl)
            out["files"] += before - (len(list(files_dir.iterdir())) if files_dir.is_dir() else 0)
            out["sessions"] += 1
    return out


def describe(meta: dict[str, Any] | None, created: bool) -> str:
    if meta is None:
        return "nothing changed since the session started: no checkpoint recorded"
    if not created:
        return f"nothing changed since checkpoint {meta['n']}"
    return (f"checkpoint {meta['n']} recorded: {meta['files']} file(s) changed since "
            f"{'the baseline' if not meta['previous'] else 'checkpoint ' + str(meta['previous'])}, "
            f"+{meta['lines_added']} −{meta['lines_removed']} lines")
