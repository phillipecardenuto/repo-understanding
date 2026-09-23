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
            observations.json      (used when no session is active)
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
from .sources import EmptySource, GitRevisionSource, OverlaySource, TreeSource

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


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
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
    id: str
    started_at: str
    baseline_head: str | None
    baseline_branch: str | None
    label: str = ""
    ended_at: str | None = None
    # path -> stored blob hash, or None when the file was deleted at session start
    overrides: dict[str, str | None] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["dirty_files_at_start"] = len(self.overrides)
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

    def start_session(self, git: Git | None, root: Path, label: str = "") -> Session:
        with self.lock:
            previous = self.current_session()
            if previous is not None and previous.active:
                previous.ended_at = utcnow()
                self.save_session(previous)
            now = _dt.datetime.now(_dt.timezone.utc)
            sid = now.strftime("%Y%m%dT%H%M%SZ") + "-" + stable_hash(str(now.timestamp()), length=6)
            head = git.head() if git else None
            session = Session(id=sid, started_at=now.isoformat(timespec="seconds"), baseline_head=head,
                              baseline_branch=git.branch() if git else None, label=label)
            files_dir = self._session_dir(sid) / "files"
            files_dir.mkdir(parents=True, exist_ok=True)
            if git is not None:
                for entry in git.status():
                    paths = [entry.path] + ([entry.orig_path] if entry.orig_path else [])
                    for path in paths:
                        if path in session.overrides:
                            continue
                        p = root / path
                        if p.is_file() and not p.is_symlink():
                            try:
                                size = p.stat().st_size
                                if size > MAX_BASELINE_FILE_BYTES:
                                    session.skipped.append(path)
                                    continue
                                data = p.read_bytes()
                            except OSError:
                                session.skipped.append(path)
                                continue
                            digest = content_hash(data)
                            target = files_dir / digest
                            if not target.exists():
                                target.write_bytes(data)
                            session.overrides[path] = digest
                        else:
                            session.overrides[path] = None
            self.save_session(session)
            _atomic_write(self.dir / "current", sid)
            return session

    def end_session(self) -> Session | None:
        with self.lock:
            session = self.current_session()
            if session is None:
                return None
            session.ended_at = utcnow()
            self.save_session(session)
            try:
                (self.dir / "current").unlink()
            except OSError:
                pass
            return session

    def baseline_source(self, session: Session, git: Git | None) -> TreeSource:
        base: TreeSource
        if git is not None and session.baseline_head:
            try:
                base = GitRevisionSource(git, git.resolve(session.baseline_head), label="session baseline")
            except Exception:
                base = EmptySource()
        else:
            base = EmptySource()
        overrides: dict[str, bytes | None] = {}
        files_dir = self._session_dir(session.id) / "files"
        for path, digest in session.overrides.items():
            if digest is None:
                overrides[path] = None
            else:
                try:
                    overrides[path] = (files_dir / digest).read_bytes()
                except OSError:
                    continue
        return OverlaySource(base, overrides, label=f"session baseline ({session.started_at})", kind="session-baseline",
                             revision_id=f"session:{session.id}")

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
