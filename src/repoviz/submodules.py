"""What changed inside Git submodules between two repository states.

A superproject records only the commit each submodule points to, so a plain
diff of the superproject sees a submodule bump as one opaque line and does not
see uncommitted edits inside a submodule at all.  For review purposes both
matter: an agent may edit a vendored service or bump it to an untested commit.

:func:`submodule_changes` compares the submodule commits of two
:class:`~repoviz.sources.TreeSource` states and, where the submodule is checked
out locally, looks inside it:

* commit range ``old..new``: the commits and the files they changed, when the
  objects are available (shallow clones may lack the old commit);
* uncommitted files, when the target is the working tree.

Changed files are returned with their full superproject path
(``system_modules/cbir/src/config.py``) and their before/after bytes, so a
review can treat them like any other file: scope rules, signals and diffs.
Git runs inside submodules with the same hardened configuration as for the
superproject (see :mod:`repoviz.gitutil`).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .gitutil import Git, GitError
from .ids import content_hash
from .sources import is_binary

MAX_FILES_PER_SUBMODULE = 300
MAX_FILE_BYTES = 1_000_000
MAX_COMMITS = 20

_OPEN: dict[str, Git | None] = {}
_OPEN_LOCK = threading.Lock()


def gitmodules_urls(text: str) -> dict[str, str]:
    """``path -> url`` from a ``.gitmodules`` file (credentials in URLs are dropped)."""
    out: dict[str, str] = {}
    path = url = None
    for line in text.splitlines() + ["[end]"]:
        line = line.strip()
        if line.startswith("["):
            if path and url:
                out[path] = re.sub(r"//[^/@]+@", "//", url)
            path = url = None
        elif "=" in line:
            key, _, value = line.partition("=")
            key, value = key.strip().lower(), value.strip()
            if key == "path":
                path = value.strip("/")
            elif key == "url":
                url = value
    return out


def open_submodule(root: Path, path: str) -> Git | None:
    """A :class:`Git` for a checked-out submodule, or ``None`` when it is not initialised."""
    sub = (Path(root) / path).resolve()
    key = str(sub)
    with _OPEN_LOCK:
        if key in _OPEN:
            return _OPEN[key]
    git: Git | None = None
    # A checked-out submodule has a ``.git`` file (or directory); an empty directory means not initialised.
    if (sub / ".git").exists() and sub.is_relative_to(Path(root).resolve()):
        try:
            git = Git(sub)
            if Path(git.run("rev-parse", "--show-toplevel").strip()).resolve() != sub:
                git = None  # .git does not belong to this directory
        except GitError:
            git = None
    if git is not None:  # not cached when missing: it may be initialised later
        with _OPEN_LOCK:
            git = _OPEN.setdefault(key, git)
    return git


class WithSubmoduleFiles:
    """A source plus files that live inside submodules, addressed by their superproject path."""

    def __init__(self, source: Any, extra: dict[str, bytes | None]) -> None:
        self._source = source
        self._extra = extra

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def read_bytes(self, path: str) -> bytes | None:
        return self._extra[path] if path in self._extra else self._source.read_bytes(path)

    def content_hash(self, path: str) -> str | None:
        if path in self._extra:
            data = self._extra[path]
            return None if data is None else content_hash(data)
        return self._source.content_hash(path)

    def read_text(self, path: str, max_bytes: int | None = None) -> str | None:
        if path not in self._extra:
            return self._source.read_text(path, max_bytes)
        data = self._extra[path]
        return None if data is None or is_binary(data) else data.decode("utf-8", "replace")


@dataclass
class SubmoduleChange:
    path: str
    status: str  # added | removed | updated | modified (uncommitted work inside) | updated+modified
    old: str | None
    new: str | None
    commits: list[dict[str, str]] | None = None  # newest first; None when history is unavailable
    commit_count: int | None = None
    #: (superproject path, before bytes, after bytes) for every changed file inside the submodule.
    files: list[tuple[str, bytes | None, bytes | None]] = field(default_factory=list)
    dirty: list[str] = field(default_factory=list)  # uncommitted paths (relative to the submodule)
    note: str | None = None
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "status": self.status, "old": self.old, "new": self.new,
                "commits": self.commits, "commit_count": self.commit_count, "files": [f[0] for f in self.files],
                "dirty": self.dirty, "note": self.note, "truncated": self.truncated}


def _has_commit(git: Git, sha: str) -> bool:
    return bool(git.try_run("cat-file", "-e", f"{sha}^{{commit}}") is not None)


def _tree_blobs(git: Git, sha: str) -> dict[str, str]:
    return {path: obj for mode, typ, obj, _size, path in git.ls_tree(sha) if typ == "blob"}


#: Stands in for files too large to diff (the NUL byte makes reviews treat it as binary).
TOO_LARGE = b"\0(file too large to diff)"


def _read(git: Git, blob: str | None) -> bytes | None:
    if not blob or set(blob) == {"0"}:
        return None
    data = git.read_blob(blob)
    return data if data is None or len(data) <= MAX_FILE_BYTES else TOO_LARGE


def submodule_changes(root: Path, base: Any, target: Any) -> list[SubmoduleChange]:
    """Submodule differences between two sources (see the module docstring)."""
    before, after = base.submodule_commits(), target.submodule_commits()
    paths = sorted(set(base.submodules) | set(target.submodules) | set(before) | set(after))
    out: list[SubmoduleChange] = []
    for path in paths:
        old, new = before.get(path), after.get(path)
        base_dirty, target_dirty = base.submodule_dirty(path), target.submodule_dirty(path)
        in_base, in_target = path in base.submodules, path in target.submodules
        if in_base and in_target and old == new and not base_dirty and not target_dirty:
            continue
        change = SubmoduleChange(path, "", old if in_base else None, new if in_target else None)
        git = open_submodule(root, path)
        files: dict[str, tuple[bytes | None, bytes | None]] = {}
        old_blobs: dict[str, str] = {}
        new_blobs: dict[str, str] = {}
        if git is None:
            change.note = "not checked out locally: only the recorded commit is known"
        else:
            have_old = bool(change.old) and _has_commit(git, change.old or "")
            have_new = bool(change.new) and _has_commit(git, change.new or "")
            old_blobs = _tree_blobs(git, change.old) if have_old and change.old else {}
            new_blobs = _tree_blobs(git, change.new) if have_new and change.new else {}
            if change.old and change.new and change.old != change.new:
                if have_old and have_new:
                    log = git.try_run("log", f"--max-count={MAX_COMMITS}", "--format=%h%x1f%s", "--end-of-options",
                                      f"{change.old}..{change.new}") or ""
                    change.commits = [dict(zip(("sha", "subject"), line.split("\x1f", 1)))
                                      for line in log.splitlines() if "\x1f" in line]
                    count = (git.try_run("rev-list", "--count", f"{change.old}..{change.new}") or "").strip()
                    change.commit_count = int(count) if count.isdigit() else None
                    for rel in sorted(set(old_blobs) | set(new_blobs)):
                        if old_blobs.get(rel) != new_blobs.get(rel):
                            files[rel] = (_read(git, old_blobs.get(rel)), _read(git, new_blobs.get(rel)))
                else:
                    change.note = ("the previous commit is not available locally (shallow or not fetched): "
                                   "the commits and files of this update cannot be listed")
            elif change.new and not change.old and have_new:
                count = (git.try_run("rev-list", "--count", change.new) or "").strip()
                change.commit_count = int(count) if count.isdigit() else None
        # Uncommitted content on either side (session baselines record it too).
        for rel in sorted(set(base_dirty) | set(target_dirty)):
            if rel in base_dirty:
                prior = _cap(base.submodule_file(path, rel))
            elif rel in files:
                prior = files[rel][0]
            else:
                prior = _read(git, old_blobs.get(rel)) if git is not None else None
            if rel in target_dirty:
                current = _cap(target.submodule_file(path, rel))
            elif rel in files:
                current = files[rel][1]
            else:
                current = _read(git, new_blobs.get(rel)) if git is not None else None
            files[rel] = (prior, current)
        changed = {rel: ba for rel, ba in files.items() if ba[0] != ba[1]}
        change.dirty = [rel for rel in target_dirty if rel in changed]
        if in_base and in_target and old == new and not changed:
            continue  # uncommitted work that was already there at the baseline, unchanged
        change.status = "added" if not in_base else "removed" if not in_target else ""
        if not change.status:
            change.status = "updated" if old != new else ""
            if change.dirty:
                change.status = f"{change.status}+modified" if change.status else "modified"
            change.status = change.status or "modified"
        items = sorted(changed.items())
        if len(items) > MAX_FILES_PER_SUBMODULE:
            items, change.truncated = items[:MAX_FILES_PER_SUBMODULE], True
        change.files = [(f"{path}/{rel}", b, a) for rel, (b, a) in items]
        out.append(change)
    return out


def _cap(data: bytes | None) -> bytes | None:
    return data if data is None or len(data) <= MAX_FILE_BYTES else TOO_LARGE
