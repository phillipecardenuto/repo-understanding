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
from .ids import content_hash, stable_hash
from .sources import GitRevisionSource, OverlaySource, TreeSource, WorkingTreeSource, is_binary

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


# --------------------------------------------------------------------------- nested analysis (#21)


class NestedSource(TreeSource):
    """A superproject state plus the files of its checked-out submodules, under their superproject paths.

    Each submodule is read at the commit the state records (with the uncommitted files a session captured),
    or, for the working tree, from its own working tree.  Content hashes stay Git blob hashes, so caches and
    diffs work as for any other file.  ``skipped`` says why a submodule is not included (not checked out,
    excluded, too large, commit not available locally)."""

    def __init__(self, base: TreeSource, inner: dict[str, TreeSource], skipped: dict[str, str]) -> None:
        super().__init__(base.label)
        self.base, self.inner, self.skipped = base, inner, skipped
        self.kind = base.kind
        self.submodules = list(base.submodules)
        self._prefixes = sorted(inner, key=len, reverse=True)
        files = list(base.files())
        for path, src in sorted(inner.items()):
            files += [f"{path}/{f}" for f in src.files()]
        self._files = sorted(files)

    def __getattr__(self, name: str) -> Any:  # git, root, sha, include_untracked… of the superproject state
        if name.startswith("__") or name in ("base", "inner", "skipped", "_prefixes"):
            raise AttributeError(name)
        return getattr(self.base, name)

    def _split(self, path: str) -> tuple[TreeSource, str]:
        for p in self._prefixes:
            if path.startswith(p + "/"):
                return self.inner[p], path[len(p) + 1:]
        return self.base, path

    def files(self) -> list[str]:
        return list(self._files)

    def read_bytes(self, path: str) -> bytes | None:
        src, rel = self._split(path)
        return src.read_bytes(rel)

    def content_hash(self, path: str) -> str | None:
        src, rel = self._split(path)
        return src.content_hash(rel)

    def size(self, path: str) -> int | None:
        src, rel = self._split(path)
        return src.size(rel)

    def mtime(self, path: str) -> float | None:
        src, rel = self._split(path)
        return src.mtime(rel)

    def submodule_commits(self) -> dict[str, str]:
        return self.base.submodule_commits()

    def submodule_dirty(self, path: str) -> list[str]:
        return self.base.submodule_dirty(path)

    def submodule_file(self, path: str, rel: str) -> bytes | None:
        return self.base.submodule_file(path, rel)

    def submodule_recorded(self, path: str) -> str | None:
        return self.base.submodule_recorded(path)

    @property
    def revision_id(self) -> str:
        return "nested:" + stable_hash(self.base.revision_id, *(f"{p}\0{s.revision_id}" for p, s in
                                                                sorted(self.inner.items())),
                                       *(f"{p}\0{r}" for p, r in sorted(self.skipped.items())))


def _total_bytes(src: TreeSource) -> int:
    return sum(src.size(f) or 0 for f in src.files())


def nested_source(root: Path, source: TreeSource, *, exclude: list[str] | None = None, max_files: int = 5000,
                  max_mb: float = 50.0) -> TreeSource:
    """``source`` with the content of its checked-out submodules (``source`` itself when it has none)."""
    from . import globs

    if not source.submodules or isinstance(source, NestedSource):
        return source
    commits = source.submodule_commits()
    inner: dict[str, TreeSource] = {}
    skipped: dict[str, str] = {}
    for path in sorted(source.submodules):
        if exclude and (path in exclude or globs.match_any(path, exclude)):
            skipped[path] = "excluded by [submodules] exclude"
            continue
        git = open_submodule(root, path)
        if git is None:
            skipped[path] = "not checked out (git submodule update --init)"
            continue
        src: TreeSource
        if source.kind == "worktree":
            src = WorkingTreeSource(git, include_untracked=bool(getattr(source, "include_untracked", True)))
        else:
            commit = commits.get(path)
            if not commit or not _has_commit(git, commit):
                skipped[path] = f"commit {(commit or '?')[:10]} is not available in the local clone"
                continue
            src = GitRevisionSource(git, commit, label=f"{path}@{commit[:10]}")
            dirty = source.submodule_dirty(path)
            if dirty:  # a session baseline remembers the submodule's uncommitted files
                src = OverlaySource(src, {d: source.submodule_file(path, d) for d in dirty}, label=src.label)
        n = len(src.files())
        if n > max_files:
            skipped[path] = f"too large: {n} files (more than [submodules] max_files = {max_files})"
            continue
        size = _total_bytes(src)
        if size > max_mb * 1_000_000:
            skipped[path] = f"too large: {size / 1_000_000:.0f} MB (more than [submodules] max_mb = {max_mb:g})"
            continue
        inner[path] = src
    return NestedSource(source, inner, skipped)


def commits_behind(git: Git) -> tuple[int, str] | None:
    """How many commits the checked-out commit is behind its remote's default branch, from the local
    remote-tracking refs only (never fetched): ``(count, "origin/main")``, or ``None`` when unknown."""
    head = (git.try_run("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD") or "").strip()
    candidates = [head.removeprefix("refs/remotes/")] if head else []
    candidates += ["origin/main", "origin/master"]
    for ref in candidates:
        if ref and git.try_run("rev-parse", "--verify", "--quiet", f"refs/remotes/{ref}") is not None:
            out = (git.try_run("rev-list", "--count", f"HEAD..refs/remotes/{ref}") or "").strip()
            if out.isdigit():
                return int(out), ref
    return None
