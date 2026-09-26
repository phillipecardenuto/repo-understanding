"""Work-session baselines and activity observations.

A *session* records the state of the working tree when an (AI or human)
work session starts, so the tool can later answer "what changed during this
session?" -- including changes that were committed along the way.

The baseline is stored compactly: the commit that was checked out plus copies
of the files that were already dirty at that moment.  Everything else is read
back from Git.  All state lives in a per-user cache directory (never inside
the analyzed repository):

    $REPOVIZ_STATE_DIR or $XDG_CACHE_HOME/repoviz or ~/.cache/repoviz
        repos/<repo-hash>-<name>/
            current                -> id of the active session
            sessions/<id>/session.json
            sessions/<id>/files/<blob-hash>
            sessions/<id>/observations.json
            sessions/<id>/timeline.json, checkpoints/<n>.json, statcache.json   (checkpoints.py)
            observations.json      (used when no session is active)
            reviews/<key-hash>.json, .verdict.json, .reviewed.json   notes, verdict and "reviewed" marks per review
            server.json            the running `repoviz serve` (url, pid), for `review --wait`
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .gitutil import Git
from .ids import content_hash, stable_hash
from .sources import EmptySource, GitRevisionSource, OverlaySource, TreeSource, WorkingTreeSource

MAX_BASELINE_FILE_BYTES = 5_000_000


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def default_state_dir(configured: str | None = None) -> Path:
    if configured:
        return Path(configured).expanduser()
    if os.environ.get("REPOVIZ_STATE_DIR"):
        return Path(os.environ["REPOVIZ_STATE_DIR"]).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "repoviz"


def _mkdir_private(path: Path) -> None:
    """Create ``path`` and missing parents readable by the current user only.

    The state directory holds copies of source files (session baselines) and
    review notes, which may be as sensitive as the repository itself.
    """
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            pass


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _atomic_write(path: Path, data: str) -> None:
    _mkdir_private(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")  # mkstemp files are 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class Session:
    """A work session ("wave"): a baseline, optionally an end state, and the agreed scope."""

    id: str
    started_at: str
    baseline_head: str | None
    baseline_branch: str | None
    label: str = ""
    ended_at: str | None = None
    # path -> stored blob hash, or None when the file was deleted at session start
    overrides: dict[str, str | None] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    # Scope agreed for the session: globs the agent may change / must not change.
    allowed: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    # What the plan says will change (plan.py): path globs, symbol:<name>, test / migration / docs / changelog;
    # and where they came from ({"source": "PLAN.md", "unresolved": [...]} after a plan import).
    expected: list[str] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    # End state (recorded when the session ends) so past waves can be reviewed later.
    end_head: str | None = None
    end_branch: str | None = None
    end_overrides: dict[str, str | None] = field(default_factory=dict)
    # Submodules: checked-out commit per submodule, and copies of files modified inside them
    # (superproject path -> stored blob hash, or None when deleted), at start and at end.
    submodules: dict[str, str] = field(default_factory=dict)
    submodule_overrides: dict[str, str | None] = field(default_factory=dict)
    end_submodules: dict[str, str] = field(default_factory=dict)
    end_submodule_overrides: dict[str, str | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("overrides", "end_overrides", "submodule_overrides", "end_submodule_overrides"):
            data.pop(key)
        data["dirty_files_at_start"] = len(self.overrides)
        data["dirty_files_at_end"] = len(self.end_overrides)
        data["active"] = self.active
        return data

    @property
    def active(self) -> bool:
        return self.ended_at is None


class StateStore:
    """Per-repository state directory."""

    _locks: dict[str, threading.Lock] = {}

    def __init__(self, repo_root: Path, repo_name: str, state_dir: str | None = None) -> None:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", repo_name)[:40] or "repo"
        self.dir = default_state_dir(state_dir) / "repos" / f"{stable_hash('root', str(repo_root), length=16)}-{safe}"
        self.lock = self._locks.setdefault(str(self.dir), threading.Lock())

    # -- sessions ------------------------------------------------------------

    def _session_dir(self, sid: str) -> Path:
        if not re.fullmatch(r"[0-9A-Za-z_-]+", sid):
            raise ValueError(f"invalid session id {sid!r}")
        return self.dir / "sessions" / sid

    def current_session(self) -> Session | None:
        pointer = self.dir / "current"
        try:
            sid = pointer.read_text().strip()
        except OSError:
            return None
        return self.load_session(sid) if sid else None

    def load_session(self, sid: str) -> Session | None:
        try:
            data = json.loads((self._session_dir(sid) / "session.json").read_text())
        except (OSError, ValueError):
            return None
        return Session(**{k: v for k, v in data.items() if k in Session.__dataclass_fields__})

    def list_sessions(self) -> list[Session]:
        out = []
        base = self.dir / "sessions"
        if base.is_dir():
            for d in sorted(base.iterdir()):
                s = self.load_session(d.name)
                if s:
                    out.append(s)
        return out

    def save_session(self, session: Session) -> None:
        _atomic_write(self._session_dir(session.id) / "session.json", json.dumps(asdict(session), indent=2))

    def _capture_dirty(self, git: Git, root: Path, sid: str) -> tuple[dict[str, str | None], list[str]]:
        """Copy every file that differs from HEAD (per ``git status``) into the session's file store."""
        files_dir = self._session_dir(sid) / "files"
        _mkdir_private(files_dir)
        overrides: dict[str, str | None] = {}
        skipped: list[str] = []
        for entry in git.status():
            for path in [entry.path] + ([entry.orig_path] if entry.orig_path else []):
                if path in overrides:
                    continue
                p = root / path
                if p.is_dir():  # a submodule whose checked-out commit moved: recorded separately
                    continue
                if p.is_file() and not p.is_symlink():
                    try:
                        if p.stat().st_size > MAX_BASELINE_FILE_BYTES:
                            skipped.append(path)
                            continue
                        data = p.read_bytes()
                    except OSError:
                        skipped.append(path)
                        continue
                    digest = content_hash(data)
                    target = files_dir / digest
                    if not target.exists():
                        _write_private(target, data)
                    overrides[path] = digest
                else:
                    overrides[path] = None
        return overrides, skipped

    def _capture_submodules(self, git: Git, root: Path, sid: str) -> tuple[dict[str, str], dict[str, str | None]]:
        """Checked-out commit of every submodule, and copies of the files modified inside them."""
        if not (root / ".gitmodules").is_file():
            return {}, {}
        worktree = WorkingTreeSource(git)
        files_dir = self._session_dir(sid) / "files"
        _mkdir_private(files_dir)
        overrides: dict[str, str | None] = {}
        for sub in worktree.submodules:
            for rel in worktree.submodule_dirty(sub):
                data = worktree.submodule_file(sub, rel)
                if data is None:
                    overrides[f"{sub}/{rel}"] = None
                    continue
                digest = content_hash(data)
                target = files_dir / digest
                if not target.exists():
                    _write_private(target, data)
                overrides[f"{sub}/{rel}"] = digest
        return worktree.submodule_commits(), overrides

    def start_session(self, git: Git | None, root: Path, label: str = "", allowed: list[str] | None = None,
                      protected: list[str] | None = None, expected: list[str] | None = None,
                      plan: dict[str, Any] | None = None) -> Session:
        with self.lock:
            previous = self.current_session()
            if previous is not None and previous.active:
                self._finish(previous, git, root)
            now = _dt.datetime.now(_dt.timezone.utc)
            sid = now.strftime("%Y%m%dT%H%M%SZ") + "-" + stable_hash(str(now.timestamp()), length=6)
            session = Session(id=sid, started_at=now.isoformat(timespec="seconds"),
                              baseline_head=git.head() if git else None,
                              baseline_branch=git.branch() if git else None, label=label,
                              allowed=list(allowed or []), protected=list(protected or []),
                              expected=list(expected or []), plan=dict(plan or {}))
            _mkdir_private(self._session_dir(sid))
            if git is not None:
                session.overrides, session.skipped = self._capture_dirty(git, root, sid)
                session.submodules, session.submodule_overrides = self._capture_submodules(git, root, sid)
            self.save_session(session)
            _atomic_write(self.dir / "current", sid)
            return session

    def _finish(self, session: Session, git: Git | None, root: Path) -> None:
        session.ended_at = utcnow()
        if git is not None:
            session.end_head = git.head()
            session.end_branch = git.branch()
            session.end_overrides, skipped = self._capture_dirty(git, root, session.id)
            session.end_submodules, session.end_submodule_overrides = self._capture_submodules(git, root, session.id)
            session.skipped = sorted(set(session.skipped) | set(skipped))
        self.save_session(session)

    def end_session(self, git: Git | None = None, root: Path | None = None) -> Session | None:
        """End the active session, recording its end state so the wave can be reviewed later."""
        with self.lock:
            session = self.current_session()
            if session is None:
                return None
            self._finish(session, git, root or Path("."))
            try:
                (self.dir / "current").unlink()
            except OSError:
                pass
            return session

    def update_scope(self, session: Session, allowed: list[str] | None, protected: list[str] | None,
                     expected: list[str] | None = None, plan: dict[str, Any] | None = None) -> Session:
        with self.lock:
            if allowed is not None:
                session.allowed = list(allowed)
            if protected is not None:
                session.protected = list(protected)
            if expected is not None:
                session.expected = list(expected)
                session.plan = dict(plan or {})
            self.save_session(session)
            return session

    def _overlay(self, session: Session, git: Git | None, head: str | None, overrides_map: dict[str, str | None],
                 label: str, kind: str, revision_id: str, submodules: dict[str, str] | None = None,
                 submodule_overrides: dict[str, str | None] | None = None) -> TreeSource:
        base: TreeSource
        if git is not None and head:
            try:
                base = GitRevisionSource(git, git.resolve(head), label=label)
            except Exception:
                base = EmptySource()
        else:
            base = EmptySource()
        files_dir = self._session_dir(session.id) / "files"

        def load(mapping: dict[str, str | None]) -> dict[str, bytes | None]:
            out: dict[str, bytes | None] = {}
            for path, digest in mapping.items():
                if digest is None:
                    out[path] = None
                else:
                    try:
                        out[path] = (files_dir / digest).read_bytes()
                    except OSError:
                        continue
            return out

        return OverlaySource(base, load(overrides_map), label=label, kind=kind, revision_id=revision_id,
                             submodule_commits=submodules or None, submodule_files=load(submodule_overrides or {}))

    def baseline_source(self, session: Session, git: Git | None) -> TreeSource:
        return self._overlay(session, git, session.baseline_head, session.overrides,
                             f"session baseline ({session.label or session.started_at})", "session-baseline",
                             f"session:{session.id}", session.submodules, session.submodule_overrides)

    def end_source(self, session: Session, git: Git | None) -> TreeSource:
        """State at the end of a finished session."""
        return self._overlay(session, git, session.end_head, session.end_overrides,
                             f"session end ({session.label or session.ended_at})", "session-end",
                             f"session-end:{session.id}", session.end_submodules, session.end_submodule_overrides)

    # -- review notes -----------------------------------------------------------------

    def _notes_path(self, key: str) -> Path:
        return self.dir / "reviews" / f"{stable_hash('review', key, length=20)}.json"

    def load_notes(self, key: str) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._notes_path(key).read_text())
        except (OSError, ValueError):
            return []
        notes = data.get("notes") if isinstance(data, dict) else None
        return notes if isinstance(notes, list) else []

    def save_notes(self, key: str, notes: list[dict[str, Any]]) -> None:
        _atomic_write(self._notes_path(key), json.dumps({"key": key, "updated_at": utcnow(), "notes": notes}, indent=1))

    # -- verdicts and "reviewed" marks (verdict.py) ------------------------------------

    def _review_file(self, key: str, suffix: str) -> Path:
        return self.dir / "reviews" / f"{stable_hash('review', key, length=20)}{suffix}"

    def load_verdict(self, key: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._review_file(key, ".verdict.json").read_text())
        except (OSError, ValueError):
            return None
        ok = isinstance(data, dict) and data.get("key") == key and data.get("verdict") in ("approve", "request-changes",
                                                                                          "reject")  # verdict.VERDICTS
        return data if ok else None

    def save_verdict(self, key: str, verdict: dict[str, Any]) -> None:
        _atomic_write(self._review_file(key, ".verdict.json"), json.dumps(verdict, indent=1))

    def load_reviewed(self, key: str) -> dict[str, str]:
        """Files marked reviewed: path → the file's version when it was marked (a newer change unmarks it)."""
        try:
            data = json.loads(self._review_file(key, ".reviewed.json").read_text())
        except (OSError, ValueError):
            return {}
        files = data.get("files") if isinstance(data, dict) else None
        return {str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else {}

    def save_reviewed(self, key: str, files: dict[str, str]) -> None:
        _atomic_write(self._review_file(key, ".reviewed.json"),
                      json.dumps({"key": key, "updated_at": utcnow(), "files": files}, indent=1))

    # -- the running live server (so `review --wait` can reuse it) --------------------

    def load_server(self) -> dict[str, Any] | None:
        try:
            data = json.loads((self.dir / "server.json").read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save_server(self, info: dict[str, Any] | None) -> None:
        path = self.dir / "server.json"
        try:
            if info is None:
                path.unlink()
            else:
                _atomic_write(path, json.dumps(info))
        except OSError:
            pass  # best effort: `review --wait` starts its own server when it finds none

    # -- observations ----------------------------------------------------------------

    def _observations_path(self, session: Session | None) -> Path:
        if session is not None:
            return self._session_dir(session.id) / "observations.json"
        return self.dir / "observations.json"

    def load_observations(self, session: Session | None) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._observations_path(session).read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_observations(self, session: Session | None, observations: dict[str, dict[str, Any]]) -> None:
        try:
            _atomic_write(self._observations_path(session), json.dumps(observations, indent=1, sort_keys=True))
        except OSError:
            pass  # observations are best effort (read-only home directories, etc.)
