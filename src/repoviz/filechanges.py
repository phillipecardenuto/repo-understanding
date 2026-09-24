"""Recent code changes of one file: what changed in a churn hotspot (the Structure tab's drawer).

Churn says *where* code keeps changing; this shows *what* changed: the file's last commits, newest first,
and the diff of one of them, or of its uncommitted edits.  Only this file is read (``git cat-file`` for its
blobs, the disk for the working copy), so a click stays fast on large repositories.  Nothing is checked out
or run, symbolic links are not followed, and every diff line and commit subject is redacted.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .redact import redact
from .sources import is_binary

if TYPE_CHECKING:  # pragma: no cover
    from .repo import Repository

MAX_COMMITS = 10
MAX_DIFF_LINES = 600
MAX_FILE_BYTES = 1_000_000
UNCOMMITTED = "WORKTREE"
#: Static reports: how many hotspots get their latest change embedded, and how many diff lines in total.
REPORT_HOTSPOTS = 25
REPORT_DIFF_LINES = 300
REPORT_DIFF_BUDGET = 6000

@dataclass(frozen=True)
class _Large:
    """Content over :data:`MAX_FILE_BYTES`, known by its size only (never read into memory)."""

    size: int


def checked_path(path: str) -> str:
    """A repository-relative file path, or ``ValueError`` (absolute, ``..``, ``.git``, option-like…)."""
    p = (path or "").strip()
    parts = p.split("/")
    if (not p or p.startswith(("/", "-")) or "\\" in p or "\x00" in p or "\n" in p
            or any(seg in ("", ".", "..") for seg in parts) or parts[0] == ".git"):
        raise ValueError(f"invalid file path {path!r}")
    return p


def _read_disk(root: Path, path: str) -> Any:
    """The working copy's bytes, ``None`` if absent (or reached through a symbolic link), or ``_Large``."""
    cur = root
    try:
        for part in path.split("/"):
            cur = cur / part
            if cur.is_symlink():
                return None
        if not cur.is_file():
            return None
        size = cur.stat().st_size
        return _Large(size) if size > MAX_FILE_BYTES else cur.read_bytes()
    except OSError:
        return None


def _text(data: Any) -> tuple[str | None, str | None]:
    """``(text, None)``, or ``(None, reason)`` when the content cannot be shown as a diff."""
    if isinstance(data, _Large) or (isinstance(data, bytes) and len(data) > MAX_FILE_BYTES):
        return None, "file too large"
    if data is None:
        return "", None
    if is_binary(data):
        return None, "binary file"
    return data.decode("utf-8", "replace"), None


def _blobs(git: Any, specs: list[tuple[str, str]]) -> list[Any]:
    """The ``(commit, path)`` blobs: bytes, ``None`` when absent, or ``_Large`` (checked before reading)."""
    return [None if size is None else _Large(size) if size > MAX_FILE_BYTES else git.show_file(rev, path)
            for (rev, path), size in zip(specs, git.blob_sizes(specs))]


def file_changes(repo: "Repository", path: str, commit: str | None = None, *, max_commits: int = MAX_COMMITS,
                 max_lines: int = MAX_DIFF_LINES) -> dict[str, Any]:
    """The last commits that changed ``path`` and the diff of one of them.

    ``commit`` is one of those commits (full or short SHA) or ``"WORKTREE"`` for the uncommitted edits; by
    default the uncommitted edits when there are any, else the latest commit.  At most ``max_lines`` diff
    lines are returned (``truncated`` says when some were left out)."""
    from .review import file_hunks  # the same hunk format as the Review tab

    path = checked_path(path)
    git = repo.git
    head = git.head() if git is not None else None
    commits = []
    for c in (git.file_log(path, max_commits) if head else []):
        commits.append({"sha": c["sha"], "short": c["sha"][:10], "subject": redact(c["subject"]),
                        "date": _dt.datetime.fromtimestamp(c["time"], _dt.timezone.utc).isoformat() if c["time"] else None,
                        "added": c["added"], "removed": c["removed"]})
    head_blob = _blobs(git, [(head, path)])[0] if head else None
    disk = _read_disk(repo.root, path)
    if head_blob is None and not commits and (disk is None or (git is not None and not git.is_untracked_visible(path))):
        raise ValueError(f"unknown file {path!r}")
    uncommitted = disk != head_blob  # two large versions compare by size
    result: dict[str, Any] = {"path": path, "commits": commits, "uncommitted": uncommitted, "shown": None,
                              "label": "", "added": 0, "removed": 0, "hunks": [], "truncated": False,
                              "total_lines": 0, "omitted": None, "max_commits": max_commits}
    shown = commit.strip() if commit and commit.strip() else (UNCOMMITTED if uncommitted else
                                                              (commits[0]["sha"] if commits else None))
    if shown is None:
        result["omitted"] = "no recorded change for this file"
        return result
    if shown == UNCOMMITTED:
        if not uncommitted:
            raise ValueError(f"{path} has no uncommitted changes")
        before, after = head_blob, disk
        result["label"] = "Uncommitted changes (working tree vs HEAD)"
    else:
        item = next((c for c in commits if shown in (c["sha"], c["short"])), None)
        if item is None:
            raise ValueError(f"{shown!r} is not one of the last {max_commits} commits that changed {path}")
        shown = item["sha"]
        parent = (git.try_run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{shown}^1") or "").strip()
        blobs = _blobs(git, ([(parent, path)] if parent else []) + [(shown, path)])
        before, after = (blobs[0], blobs[1]) if parent else (None, blobs[0])  # a root commit adds the file
        result["label"] = f"{item['short']} {item['subject']}"
    result["shown"] = shown
    before_text, why_before = _text(before)
    after_text, why_after = _text(after)
    if before_text is None or after_text is None:
        result["omitted"] = why_before or why_after
        return result
    hunks, added, removed = file_hunks(before_text, after_text)
    for hunk in hunks:  # never re-publish a committed secret
        hunk["lines"] = [line[0] + redact(line[1:]) for line in hunk["lines"]]
    result["added"], result["removed"] = len(added), len(removed)
    total = sum(len(hk["lines"]) for hk in hunks)
    kept, used = [], 0
    for hk in hunks:
        if used + len(hk["lines"]) > max_lines:
            if not kept:  # one huge hunk: show its beginning
                kept.append(dict(hk, lines=hk["lines"][:max_lines]))
                used = max_lines
            break
        kept.append(hk)
        used += len(hk["lines"])
    result.update(hunks=kept, total_lines=total, truncated=used < total)
    return result


def hotspots(snapshot: Any, limit: int = REPORT_HOTSPOTS) -> list[str]:
    """The paths the Structure tab marks as churn hotspots (see :func:`repoviz.risk.hotspot_threshold`), most
    changed first."""
    from .risk import hotspot_threshold

    churn = {n.path: int(n.metadata["churn"]["commits"]) for n in snapshot.modules
             if n.path and isinstance(n.metadata.get("churn"), dict) and n.metadata["churn"].get("commits")}
    hot = hotspot_threshold(churn.values())
    if hot is None:
        return []
    return [p for p, c in sorted(churn.items(), key=lambda kv: (-kv[1], kv[0])) if c >= hot][:limit]


def report_changes(repo: "Repository", snapshot: Any) -> dict[str, Any]:
    """The latest change of each hotspot, for a static report (capped per file and in total)."""
    out: dict[str, Any] = {}
    budget = REPORT_DIFF_BUDGET
    for path in hotspots(snapshot):
        if budget <= 0:
            break
        try:
            changes = file_changes(repo, path, max_lines=min(REPORT_DIFF_LINES, budget))
        except Exception:  # a broken file must not prevent the report
            continue
        budget -= sum(len(hk["lines"]) for hk in changes["hunks"])
        out[path] = changes
    return out
