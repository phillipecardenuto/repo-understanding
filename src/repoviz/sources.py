"""Tree sources: uniform read access to a repository state.

A :class:`TreeSource` lists files and returns their content for one state of
the repository: a commit, the index (staged content), the working tree, a
session baseline, or a plain directory when Git is unavailable.  Analyzers only
ever talk to a ``TreeSource``, which is what lets any two states be compared
without checking anything out.

Content hashes are Git blob hashes everywhere, so equality can be tested across
sources without reading file contents twice.
"""

from __future__ import annotations

import os
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .gitutil import Git, RevisionError
from .ids import content_hash, stable_hash

# Tokens accepted wherever a revision is expected.
WORKTREE = "WORKTREE"
WORKTREE_TRACKED = "WORKTREE-TRACKED"
INDEX = "INDEX"
SESSION = "SESSION"
EMPTY = "EMPTY"

_ALIASES = {
    "WORKTREE": WORKTREE, "WORKING": WORKTREE, "WORKDIR": WORKTREE, "WORKING-TREE": WORKTREE, ".": WORKTREE,
    "WORKTREE-TRACKED": WORKTREE_TRACKED, "TRACKED": WORKTREE_TRACKED,
    "INDEX": INDEX, "STAGED": INDEX, "CACHED": INDEX,
    "SESSION": SESSION, "BASELINE": SESSION,
    "EMPTY": EMPTY,
}


@dataclass(frozen=True)
class RevSpec:
    """A parsed revision: a special token or a git revision expression."""

    kind: str  # worktree | worktree-tracked | index | session | empty | git
    rev: str = ""

    @classmethod
    def parse(cls, text: str) -> "RevSpec":
        token = text.strip()
        past = re.fullmatch(r"(?i)session(-end)?@([0-9A-Za-z_-]+)", token)
        if past:  # SESSION@<id> = baseline of a (past) session, SESSION-END@<id> = its end state
            return cls("session-end" if past.group(1) else "session-at", past.group(2))
        alias = _ALIASES.get(token.upper())
        if alias == WORKTREE:
            return cls("worktree")
        if alias == WORKTREE_TRACKED:
            return cls("worktree-tracked")
        if alias == INDEX:
            return cls("index")
        if alias == SESSION:
            return cls("session")
        if alias == EMPTY:
            return cls("empty")
        if not token:
            raise RevisionError("empty revision")
        return cls("git", token)

    @property
    def label(self) -> str:
        return {
            "worktree": "working tree",
            "worktree-tracked": "working tree (tracked files)",
            "index": "index (staged)",
            "session": "session baseline",
            "empty": "empty tree",
            "session-at": f"baseline of session {self.rev}",
            "session-end": f"end of session {self.rev}",
        }.get(self.kind, self.rev)

    def __str__(self) -> str:
        return {
            "worktree": WORKTREE, "worktree-tracked": WORKTREE_TRACKED, "index": INDEX, "session": SESSION,
            "empty": EMPTY,
            "session-at": f"SESSION@{self.rev}",
            "session-end": f"SESSION-END@{self.rev}",
        }.get(self.kind, self.rev)


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


class TreeSource(ABC):
    """Read-only view of one repository state."""

    kind: str = "abstract"

    def __init__(self, label: str) -> None:
        self.label = label
        self._text_cache: dict[str, str | None] = {}
        self._lock = threading.Lock()

    @abstractmethod
    def files(self) -> list[str]:
        """Sorted POSIX paths relative to the repository root."""

    @abstractmethod
    def read_bytes(self, path: str) -> bytes | None:
        """File content, or ``None`` when the file does not exist in this state."""

    @abstractmethod
    def content_hash(self, path: str) -> str | None:
        """Git blob hash of the file's content."""

    @property
    @abstractmethod
    def revision_id(self) -> str:
        """Identifier of this exact state (commit SHA or a content digest)."""

    def size(self, path: str) -> int | None:
        data = self.read_bytes(path)
        return None if data is None else len(data)

    def mtime(self, path: str) -> float | None:
        return None

    def exists(self, path: str) -> bool:
        return path in self.file_set()

    def file_set(self) -> frozenset[str]:
        cached = self.__dict__.get("_file_set")
        if cached is None:
            cached = frozenset(self.files())
            self.__dict__["_file_set"] = cached
        return cached

    def read_text(self, path: str, max_bytes: int | None = None) -> str | None:
        """Decoded text, or ``None`` for missing, binary, or oversized files."""
        with self._lock:
            if path in self._text_cache:
                return self._text_cache[path]
        data = self.read_bytes(path)
        text: str | None
        if data is None or is_binary(data) or (max_bytes is not None and len(data) > max_bytes):
            text = None
        else:
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
            text = data.decode("utf-8", errors="replace")
        with self._lock:
            self._text_cache[path] = text
        return text

    #: Git submodule paths (gitlinks) present in this state.
    submodules: list[str] = []

    def submodule_commits(self) -> dict[str, str]:
        """Commit each submodule points to in this state (for a working tree: the checked-out commit)."""
        return {}

    def submodule_dirty(self, path: str) -> list[str]:
        """Files inside a submodule whose content differs from its commit in this state (relative paths)."""
        return []

    def submodule_recorded(self, path: str) -> str | None:
        """The commit the superproject records for a submodule (for a working tree: the index, which may
        differ from the checked-out commit)."""
        return self.submodule_commits().get(path)

    def submodule_file(self, path: str, rel: str) -> bytes | None:
        """Content of such a file (``None`` when it was deleted)."""
        return None

    def directories(self) -> list[str]:
        dirs: set[str] = set()
        for f in self.files():
            parts = f.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]))
        return sorted(dirs)

    def close(self) -> None:
        pass


class EmptySource(TreeSource):
    kind = "empty"

    def __init__(self, label: str = "empty tree") -> None:
        super().__init__(label)

    def files(self) -> list[str]:
        return []

    def read_bytes(self, path: str) -> bytes | None:
        return None

    def content_hash(self, path: str) -> str | None:
        return None

    @property
    def revision_id(self) -> str:
        return "empty"


class GitRevisionSource(TreeSource):
    """Content of a commit, read from the object database."""

    kind = "commit"

    def __init__(self, git: Git, sha: str, label: str | None = None) -> None:
        super().__init__(label or sha[:12])
        self.git = git
        self.sha = sha
        self._entries: dict[str, tuple[str, int | None]] = {}
        self.submodules: list[str] = []
        self._sub_commits: dict[str, str] = {}
        self.symlinks: list[str] = []
        for mode, typ, obj, size, path in git.ls_tree(sha):
            if typ == "commit":
                self.submodules.append(path)
                self._sub_commits[path] = obj
            elif typ == "blob":
                if mode == "120000":
                    self.symlinks.append(path)
                    continue
                self._entries[path] = (obj, size)
        self._files = sorted(self._entries)

    def files(self) -> list[str]:
        return list(self._files)

    def read_bytes(self, path: str) -> bytes | None:
        entry = self._entries.get(path)
        return None if entry is None else self.git.read_blob(entry[0])

    def content_hash(self, path: str) -> str | None:
        entry = self._entries.get(path)
        return entry[0] if entry else None

    def size(self, path: str) -> int | None:
        entry = self._entries.get(path)
        return entry[1] if entry else None

    def submodule_commits(self) -> dict[str, str]:
        return dict(self._sub_commits)

    @property
    def revision_id(self) -> str:
        return self.sha


class GitIndexSource(TreeSource):
    """Staged content (the index)."""

    kind = "index"

    def __init__(self, git: Git, label: str = "index (staged)") -> None:
        super().__init__(label)
        self.git = git
        self._entries: dict[str, str] = {}
        self.submodules: list[str] = []
        self._sub_commits: dict[str, str] = {}
        self.conflicted: list[str] = []
        for mode, obj, stage, path in git.ls_index():
            if mode == "160000":
                self.submodules.append(path)
                self._sub_commits[path] = obj
                continue
            if mode == "120000":
                continue
            if stage == 0:
                self._entries[path] = obj
            elif stage == 2:  # "ours" side of a conflict
                self._entries.setdefault(path, obj)
                self.conflicted.append(path)
        self._files = sorted(self._entries)
        self._id = stable_hash("index", *(f"{p}\0{o}" for p, o in sorted({**self._entries, **self._sub_commits}.items())))

    def submodule_commits(self) -> dict[str, str]:
        return dict(self._sub_commits)

    def files(self) -> list[str]:
        return list(self._files)

    def read_bytes(self, path: str) -> bytes | None:
        obj = self._entries.get(path)
        return None if obj is None else self.git.read_blob(obj)

    def content_hash(self, path: str) -> str | None:
        return self._entries.get(path)

    @property
    def revision_id(self) -> str:
        return f"index:{self._id}"


class _DiskMixin:
    root: Path
    _hash_cache: dict[str, tuple[int, int, str]]

    def _abs(self, path: str) -> Path:
        return self.root / path

    def _read_disk(self, path: str) -> bytes | None:
        p = self._abs(path)
        try:
            if p.is_symlink() or not p.is_file():
                return None
            return p.read_bytes()
        except OSError:
            return None

    def _hash_disk(self, path: str) -> str | None:
        p = self._abs(path)
        try:
            st = p.stat()
        except OSError:
            return None
        cached = self._hash_cache.get(path)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2]
        data = self._read_disk(path)
        if data is None:
            return None
        digest = content_hash(data)
        self._hash_cache[path] = (st.st_mtime_ns, st.st_size, digest)
        return digest


# Stat-keyed hash cache shared by all working-tree sources of one process.
_WORKTREE_HASHES: dict[str, dict[str, tuple[int, int, str]]] = {}


class WorkingTreeSource(_DiskMixin, TreeSource):
    """Files on disk: tracked files plus (optionally) untracked, non-ignored files."""

    kind = "worktree"

    def __init__(self, git: Git, include_untracked: bool = True, label: str | None = None) -> None:
        super().__init__(label or ("working tree" if include_untracked else "working tree (tracked files)"))
        self.git = git
        self.root = git.root
        self.include_untracked = include_untracked
        self._hash_cache = _WORKTREE_HASHES.setdefault(str(self.root), {})
        files = []
        for path in git.ls_worktree(include_untracked=include_untracked):
            p = self.root / path
            if p.is_file() and not p.is_symlink():
                files.append(path)
        self._files = sorted(files)
        self._id: str | None = None
        # Submodules: the index records them; their checked-out commit and dirty files come from git inside them.
        self.submodules = []
        self._index_sub: dict[str, str] = {}
        self._sub_state: dict[str, tuple[str | None, list[str]]] = {}
        if (self.root / ".gitmodules").is_file():
            for mode, obj, _stage, path in git.ls_index():
                if mode == "160000":
                    self.submodules.append(path)
                    self._index_sub[path] = obj

    def _submodule_state(self, path: str) -> tuple[str | None, list[str]]:
        state = self._sub_state.get(path)
        if state is None:
            from .submodules import open_submodule  # local import: submodules imports this module

            sub = open_submodule(self.root, path)
            if sub is None:  # not initialised: the recorded commit is all we know
                state = (self._index_sub.get(path), [])
            else:
                try:
                    dirty = sorted({e.path for e in sub.status()})
                except Exception:
                    dirty = []
                state = (sub.head() or self._index_sub.get(path), dirty)
            self._sub_state[path] = state
        return state

    def submodule_commits(self) -> dict[str, str]:
        out = {}
        for path in self.submodules:
            commit = self._submodule_state(path)[0]
            if commit:
                out[path] = commit
        return out

    def submodule_dirty(self, path: str) -> list[str]:
        return list(self._submodule_state(path)[1]) if path in self.submodules else []

    def submodule_recorded(self, path: str) -> str | None:
        return self._index_sub.get(path)

    def submodule_file(self, path: str, rel: str) -> bytes | None:
        p = self.root / path / rel
        try:
            if p.is_symlink() or not p.is_file():
                return None
            return p.read_bytes()
        except OSError:
            return None

    def files(self) -> list[str]:
        return list(self._files)

    def read_bytes(self, path: str) -> bytes | None:
        if path not in self.file_set():
            return None
        return self._read_disk(path)

    def content_hash(self, path: str) -> str | None:
        if path not in self.file_set():
            return None
        return self._hash_disk(path)

    def size(self, path: str) -> int | None:
        try:
            return (self.root / path).stat().st_size if path in self.file_set() else None
        except OSError:
            return None

    def mtime(self, path: str) -> float | None:
        try:
            return (self.root / path).stat().st_mtime
        except OSError:
            return None

    @property
    def revision_id(self) -> str:
        if self._id is None:
            parts = [f"{p}\0{self.content_hash(p)}" for p in self._files]
            # Work inside submodules (commits or uncommitted edits) changes the state too.
            for sub in self.submodules:
                commit, dirty = self._submodule_state(sub)
                parts.append(f"{sub}\0{commit}")
                parts += [f"{sub}/{d}\0{self._hash_disk(f'{sub}/{d}')}" for d in dirty]
            self._id = f"worktree:{stable_hash('worktree', *parts)}"
        return self._id


class FilesystemSource(_DiskMixin, TreeSource):
    """A plain directory (used when the target is not a Git repository)."""

    kind = "filesystem"

    def __init__(self, root: str | Path, excluded_dirs: set[str] | None = None, label: str = "directory") -> None:
        super().__init__(label)
        self.root = Path(root).resolve()
        self._hash_cache = _WORKTREE_HASHES.setdefault(str(self.root), {})
        skip = excluded_dirs or {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox"}
        files: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in skip and not os.path.islink(os.path.join(dirpath, d)))
            rel = os.path.relpath(dirpath, self.root)
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                files.append(name if rel == "." else f"{rel}/{name}".replace(os.sep, "/"))
        self._files = sorted(files)
        self._id: str | None = None

    def files(self) -> list[str]:
        return list(self._files)

    def read_bytes(self, path: str) -> bytes | None:
        return self._read_disk(path) if path in self.file_set() else None

    def content_hash(self, path: str) -> str | None:
        return self._hash_disk(path) if path in self.file_set() else None

    def mtime(self, path: str) -> float | None:
        try:
            return (self.root / path).stat().st_mtime
        except OSError:
            return None

    @property
    def revision_id(self) -> str:
        if self._id is None:
            self._id = "fs:" + stable_hash("fs", *(f"{p}\0{self.content_hash(p)}" for p in self._files))
        return self._id


class OverlaySource(TreeSource):
    """A base source with some files replaced, added (bytes) or deleted (``None``)."""

    kind = "overlay"

    def __init__(self, base: TreeSource, overrides: dict[str, bytes | None], label: str, kind: str = "overlay",
                 revision_id: str | None = None, submodule_commits: dict[str, str] | None = None,
                 submodule_files: dict[str, bytes | None] | None = None) -> None:
        super().__init__(label)
        self.base = base
        self.kind = kind
        self.overrides = dict(overrides)
        self.submodules = list(base.submodules)
        self._sub_commits = submodule_commits
        self._sub_files = dict(submodule_files or {})
        files = set(base.files())
        for path, data in self.overrides.items():
            if data is None:
                files.discard(path)
            else:
                files.add(path)
        self._files = sorted(files)
        self._revision_id = revision_id or "overlay:" + stable_hash(
            base.revision_id, *(f"{p}\0{content_hash(d) if d is not None else '-'}" for p, d in sorted(self.overrides.items())))

    def files(self) -> list[str]:
        return list(self._files)

    def read_bytes(self, path: str) -> bytes | None:
        if path in self.overrides:
            return self.overrides[path]
        return self.base.read_bytes(path)

    def content_hash(self, path: str) -> str | None:
        if path in self.overrides:
            data = self.overrides[path]
            return None if data is None else content_hash(data)
        return self.base.content_hash(path)

    def submodule_commits(self) -> dict[str, str]:
        return dict(self._sub_commits) if self._sub_commits is not None else self.base.submodule_commits()

    def submodule_dirty(self, path: str) -> list[str]:
        prefix = path + "/"
        return sorted(p[len(prefix):] for p in self._sub_files if p.startswith(prefix))

    def submodule_file(self, path: str, rel: str) -> bytes | None:
        return self._sub_files.get(f"{path}/{rel}")

    @property
    def revision_id(self) -> str:
        return self._revision_id
