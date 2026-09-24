"""Read-only Git access through the ``git`` command-line client.

Every invocation runs with ``GIT_OPTIONAL_LOCKS=0`` so that even ``git
status`` does not refresh (i.e. rewrite) the index, and user-supplied revisions
are validated with ``--end-of-options`` so they can never be interpreted as
options.  No command used here writes to the repository, the index, or the
object database.

A repository's own ``.git/config`` can make read-only commands run programs:
``core.fsmonitor`` (``status``, ``ls-files``), clean/smudge filter drivers
(``status`` re-reads files whose stat data changed), GPG for
``log.showSignature`` and submodule recursion.  An archive of someone else's
repository therefore must not be trusted, so every command overrides these
settings (see :meth:`Git.safe_config`).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class GitError(RuntimeError):
    pass


class RevisionError(GitError):
    """A user-supplied revision could not be resolved."""


def git_available() -> bool:
    return shutil.which("git") is not None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
        "LANG": "C",
    })
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)
    return env


def probe_repository(path: str | Path) -> tuple[Path | None, str | None]:
    """Return ``(repository root, None)`` or ``(None, reason Git is not used)``."""
    if not git_available():
        return None, "the git executable was not found; analyzing as a plain directory (no history or comparisons)"
    path = Path(path).resolve()
    cwd = path if path.is_dir() else path.parent
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd, env=_env(), capture_output=True, text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"git could not be run ({exc}); analyzing as a plain directory"
    if out.returncode != 0:
        err = out.stderr.strip()
        if "dubious ownership" in err:
            # Respect Git's safety check instead of bypassing it for an untrusted repository.
            return None, ("Git refused to open this repository because it is owned by another user ('dubious "
                          "ownership'); analyzing as a plain directory. If you trust it, run: git config --global "
                          f"--add safe.directory {cwd}")
        return None, "not a Git repository; analyzing as a plain directory (no history or comparisons)"
    return Path(out.stdout.strip()).resolve(), None


def find_repository_root(path: str | Path) -> Path | None:
    return probe_repository(path)[0]


@dataclass
class StatusEntry:
    """One entry of ``git status --porcelain=v2``."""

    path: str
    index: str  # X: status in the index relative to HEAD ('.' = unchanged)
    worktree: str  # Y: status in the worktree relative to the index
    kind: str  # changed | renamed | unmerged | untracked
    orig_path: str | None = None

    @property
    def staged(self) -> bool:
        return self.kind != "untracked" and self.index not in (".", "?")

    @property
    def unstaged(self) -> bool:
        return self.kind == "untracked" or self.worktree not in (".", "?")

    @property
    def label(self) -> str:
        if self.kind == "untracked":
            return "untracked"
        if self.kind == "unmerged":
            return "conflicted"
        codes = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied", "T": "type-changed"}
        idx = codes.get(self.index)
        wt = codes.get(self.worktree)
        if idx and wt and idx != wt:
            if idx == "added" and wt == "modified":
                return "added"
            if wt == "deleted":
                return "deleted"
            return f"{idx}+{wt}"
        return idx or wt or "modified"


@dataclass
class CommitInfo:
    sha: str
    short: str
    subject: str
    author: str
    date: str
    refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {"sha": self.sha, "short": self.short, "subject": self.subject, "author": self.author,
                "date": self.date, "refs": self.refs}


#: Settings that make otherwise read-only commands execute programs, and their safe values.
SAFE_CONFIG = (
    "core.fsmonitor=false",
    "core.quotepath=off",
    "color.ui=false",
    "log.showSignature=false",
    "core.hooksPath=/dev/null",
)
_FILTER_KEYS = re.compile(r"^filter\.(.+)\.(clean|smudge|process|required)$", re.IGNORECASE)


def _filter_overrides(root: Path) -> list[str]:
    """``-c`` arguments that disable every filter driver configured for ``root``."""
    try:
        out = subprocess.run(["git", "-c", "core.fsmonitor=false", "config", "-z", "--get-regexp", r"^filter\."],
                             cwd=root, env=_env(), capture_output=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = set()
    for record in out.split(b"\0"):
        key = record.split(b"\n", 1)[0].decode("utf-8", errors="replace")
        m = _FILTER_KEYS.match(key)
        if m and "=" not in m.group(1):
            names.add(m.group(1))
    args: list[str] = []
    for name in sorted(names):
        args += ["-c", f"filter.{name}.clean=", "-c", f"filter.{name}.smudge=", "-c", f"filter.{name}.process=",
                 "-c", f"filter.{name}.required=false"]
    return args


class BlobReader:
    """Reads blobs through one long-lived ``git cat-file --batch`` process."""

    def __init__(self, root: Path, config: list[str] | None = None) -> None:
        self._root = root
        self._config = config or []
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> subprocess.Popen[bytes]:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(
                ["git", *self._config, "cat-file", "--batch"], cwd=self._root, env=_env(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        return self._proc

    def read(self, sha: str) -> bytes | None:
        with self._lock:
            proc = self._ensure()
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(sha.encode() + b"\n")
            proc.stdin.flush()
            header = proc.stdout.readline().decode(errors="replace").split()
            if len(header) < 3 or header[1] == "missing":
                return None
            size = int(header[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)  # trailing newline
            return data

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.close()
                    self._proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    self._proc.kill()
                self._proc = None

    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        try:
            self.close()
        except Exception:
            pass


class Git:
    """Thin wrapper around read-only git plumbing commands."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.safe_config = [a for kv in SAFE_CONFIG for a in ("-c", kv)] + _filter_overrides(self.root)
        self._blobs = BlobReader(self.root, self.safe_config)

    # -- low level ---------------------------------------------------------

    def run_bytes(self, *args: str, check: bool = True, input: bytes | None = None, timeout: float = 120) -> bytes:
        cmd = ["git", *self.safe_config, *args]
        try:
            proc = subprocess.run(cmd, cwd=self.root, env=_env(), input=input, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {' '.join(args)} timed out") from exc
        except OSError as exc:
            raise GitError(f"cannot run git: {exc}") from exc
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
        return proc.stdout

    def run(self, *args: str, check: bool = True, timeout: float = 120) -> str:
        return self.run_bytes(*args, check=check, timeout=timeout).decode("utf-8", errors="surrogateescape")

    def try_run(self, *args: str) -> str | None:
        try:
            return self.run(*args)
        except GitError:
            return None

    def close(self) -> None:
        self._blobs.close()

    # -- revisions -------------------------------------------------------------

    def head(self) -> str | None:
        out = self.try_run("rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        return out.strip() if out else None

    def branch(self) -> str | None:
        out = self.try_run("symbolic-ref", "--quiet", "--short", "HEAD")
        return out.strip() if out else None

    def resolve(self, rev: str) -> str:
        """Resolve a user-supplied revision to a commit SHA (never treats it as an option)."""
        rev = rev.strip()
        if not rev or rev.startswith("-") or "\x00" in rev or "\n" in rev:
            raise RevisionError(f"invalid revision {rev!r}")
        out = self.run_bytes("rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}", check=False)
        sha = out.decode().strip()
        if not sha:
            raise RevisionError(f"unknown revision {rev!r}")
        return sha

    def merge_base(self, a: str, b: str) -> str:
        out = self.run("merge-base", self.resolve(a), self.resolve(b), check=False).strip()
        if not out:
            raise RevisionError(f"no merge base between {a!r} and {b!r}")
        return out

    def is_shallow(self) -> bool:
        return (self.try_run("rev-parse", "--is-shallow-repository") or "").strip() == "true"

    def root_commits(self) -> list[str]:
        out = self.try_run("rev-list", "--max-parents=0", "HEAD")
        return sorted(out.split()) if out else []

    def remotes(self) -> list[str]:
        out = self.try_run("remote")
        return [r for r in (out or "").split() if r]

    def default_branch(self, configured: str | None = None) -> str | None:
        if configured:
            return configured
        for remote in self.remotes():
            out = self.try_run("symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
            if out and out.strip():
                return out.strip()
        branches = set(self.branches())
        init_default = (self.try_run("config", "--get", "init.defaultBranch") or "").strip()
        for candidate in [init_default, "main", "master", "trunk", "develop", "development"]:
            if candidate and candidate in branches:
                return candidate
        return None

    def branches(self) -> list[str]:
        out = self.try_run("for-each-ref", "--format=%(refname:short)", "refs/heads")
        return [b for b in (out or "").splitlines() if b]

    def remote_branches(self) -> list[str]:
        out = self.try_run("for-each-ref", "--format=%(refname:short)", "refs/remotes")
        return [b for b in (out or "").splitlines() if b and not b.endswith("/HEAD")]

    def tags(self, limit: int = 200) -> list[str]:
        out = self.try_run("for-each-ref", "--sort=-creatordate", f"--count={limit}", "--format=%(refname:short)",
                           "refs/tags")
        return [t for t in (out or "").splitlines() if t]

    def recent_commits(self, limit: int = 50, rev: str = "HEAD") -> list[CommitInfo]:
        if self.head() is None:
            return []
        fmt = "%H%x1f%h%x1f%s%x1f%an%x1f%cI%x1f%D%x1e"
        out = self.try_run("log", f"--max-count={limit}", f"--format={fmt}", "--end-of-options", rev) or ""
        commits: list[CommitInfo] = []
        for record in out.split("\x1e"):
            parts = record.strip("\n").split("\x1f")
            if len(parts) < 6:
                continue
            refs = [r.strip() for r in parts[5].split(",") if r.strip()]
            commits.append(CommitInfo(parts[0], parts[1], parts[2], parts[3], parts[4], refs))
        return commits

    def commit_info(self, sha: str) -> CommitInfo | None:
        commits = self.recent_commits(1, sha)
        return commits[0] if commits else None

    # -- trees -------------------------------------------------------------------

    def ls_tree(self, sha: str) -> list[tuple[str, str, str, int | None, str]]:
        """Return ``(mode, type, object, size, path)`` for every entry of a commit's tree."""
        out = self.run_bytes("ls-tree", "-r", "-l", "-z", "--full-tree", sha)
        entries = []
        for record in out.split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            fields = meta.decode().split()
            if len(fields) < 4:
                continue
            mode, typ, obj, size = fields[0], fields[1], fields[2], fields[3]
            entries.append((mode, typ, obj, int(size) if size.isdigit() else None,
                            path.decode("utf-8", errors="surrogateescape")))
        return entries

    def ls_index(self) -> list[tuple[str, str, int, str]]:
        """Return ``(mode, object, stage, path)`` for every index entry."""
        out = self.run_bytes("ls-files", "-s", "-z")
        entries = []
        for record in out.split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, obj, stage = meta.decode().split()
            entries.append((mode, obj, int(stage), path.decode("utf-8", errors="surrogateescape")))
        return entries

    def ls_worktree(self, include_untracked: bool = True) -> list[str]:
        args = ["ls-files", "-z", "--cached"]
        if include_untracked:
            args += ["--others", "--exclude-standard"]
        out = self.run_bytes(*args)
        seen: dict[str, None] = {}
        for p in out.split(b"\0"):
            if p:
                seen[p.decode("utf-8", errors="surrogateescape")] = None
        return list(seen)

    def untracked(self) -> list[str]:
        out = self.run_bytes("ls-files", "-z", "--others", "--exclude-standard")
        return [p.decode("utf-8", errors="surrogateescape") for p in out.split(b"\0") if p]

    def read_blob(self, sha: str) -> bytes | None:
        return self._blobs.read(sha)

    def show_file(self, rev_sha: str, path: str) -> bytes | None:
        try:
            return self.run_bytes("cat-file", "blob", f"{rev_sha}:{path}")
        except GitError:
            return None

    # -- status / diff ---------------------------------------------------------------

    def status(self) -> list[StatusEntry]:
        # Submodules are compared by commit only: "dirty" never runs git inside them.
        out = self.run_bytes("status", "--porcelain=v2", "-z", "--untracked-files=all", "--renames",
                             "--ignore-submodules=dirty")
        records = out.split(b"\0")
        entries: list[StatusEntry] = []
        i = 0
        while i < len(records):
            rec = records[i].decode("utf-8", errors="surrogateescape")
            i += 1
            if not rec:
                continue
            kind = rec[0]
            if kind == "1":
                parts = rec.split(" ", 8)
                entries.append(StatusEntry(parts[8], parts[1][0], parts[1][1], "changed"))
            elif kind == "2":
                parts = rec.split(" ", 9)
                orig = records[i].decode("utf-8", errors="surrogateescape") if i < len(records) else None
                i += 1
                entries.append(StatusEntry(parts[9], parts[1][0], parts[1][1], "renamed", orig))
            elif kind == "u":
                parts = rec.split(" ", 10)
                entries.append(StatusEntry(parts[10], parts[1][0], parts[1][1], "unmerged"))
            elif kind == "?":
                entries.append(StatusEntry(rec[2:], "?", "?", "untracked"))
        return entries

    def changed_paths(self, a: str, b: str | None = None) -> list[str]:
        args = ["diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", a]
        if b:
            args.append(b)
        out = self.run_bytes(*args, check=False)
        return [p.decode("utf-8", errors="surrogateescape") for p in out.split(b"\0") if p]

    def churn(self, rev: str = "HEAD", max_commits: int = 300) -> dict[str, dict[str, object]]:
        """Per-path commit counts over the last ``max_commits`` commits reachable from ``rev``."""
        if max_commits <= 0:
            return {}
        out = self.try_run("log", f"--max-count={max_commits}", "--no-renames", "--format=\x1e%ct",
                           "--name-only", "--end-of-options", rev)
        stats: dict[str, dict[str, object]] = {}
        for block in (out or "").split("\x1e"):
            lines = [l for l in block.splitlines() if l.strip()]
            if not lines:
                continue
            try:
                ts = int(lines[0])
            except ValueError:
                continue
            for path in lines[1:]:
                entry = stats.setdefault(path, {"commits": 0, "last_commit_ts": ts})
                entry["commits"] = int(entry["commits"]) + 1  # type: ignore[arg-type]
                entry["last_commit_ts"] = max(int(entry["last_commit_ts"]), ts)  # type: ignore[arg-type]
        return stats

    def commits_in_range(self, base: str | None, head: str, limit: int = 200) -> dict[str, object] | None:
        """Non-merge commits in ``base..head`` (all ancestors of ``head`` when ``base`` is None), oldest first.

        Each commit carries its files with status (A/M/D) and lines added / removed.  At most ``limit`` commits
        (the most recent) are returned; ``total`` counts all of them.  ``None`` when history is unavailable
        (e.g. objects missing from a shallow clone).
        """
        rng = [f"{base}..{head}"] if base else [head]
        count = self.try_run("rev-list", "--count", "--no-merges", "--end-of-options", *rng)
        merges = self.try_run("rev-list", "--count", "--merges", "--end-of-options", *rng)
        out = self.try_run("log", "--no-merges", f"--max-count={max(0, limit)}", "--no-renames", "--no-ext-diff",
                           "--no-textconv", "--raw", "--numstat", "--format=%x1e%H%x1f%P%x1f%an%x1f%at%x1f%s",
                           "--end-of-options", *rng)
        if count is None or out is None:
            return None
        commits: list[dict[str, object]] = []
        for block in out.split("\x1e")[1:]:
            lines = block.split("\n")
            head_fields = lines[0].split("\x1f")
            if len(head_fields) < 5:
                continue
            sha, parents, author, ts, subject = head_fields[:5]
            files: dict[str, dict[str, object]] = {}
            for line in lines[1:]:
                if line.startswith(":"):  # raw: ":100644 100644 abc def M\tpath"
                    meta, _, path = line.partition("\t")
                    status = meta.split()[-1][:1] if meta.split() else "M"
                    files.setdefault(path, {"path": path, "status": status, "added": None, "removed": None})
                    files[path]["status"] = status
                elif "\t" in line:  # numstat: "added\tremoved\tpath" ("-" for binary files)
                    added, removed, path = line.split("\t", 2)
                    entry = files.setdefault(path, {"path": path, "status": "M", "added": None, "removed": None})
                    entry["added"] = int(added) if added.isdigit() else None
                    entry["removed"] = int(removed) if removed.isdigit() else None
            commits.append({"sha": sha, "parents": parents.split(), "author": author, "time": int(ts or 0),
                            "subject": subject, "files": list(files.values())})
        commits.reverse()
        return {"commits": commits, "total": int(count.strip() or 0), "merges": int((merges or "0").strip() or 0)}

    def commit_file_sets(self, rev: str = "HEAD", max_commits: int = 300) -> list[list[str]]:
        """Paths changed by each of the last ``max_commits`` non-merge commits reachable from ``rev``."""
        if max_commits <= 0:
            return []
        out = self.try_run("log", f"--max-count={max_commits}", "--no-merges", "--no-renames", "--format=%x1e",
                           "--name-only", "--end-of-options", rev)
        return [[p for p in block.splitlines() if p.strip()] for block in (out or "").split("\x1e")[1:]]

    def submodules(self, sha: str | None) -> list[str]:
        if not sha:
            return []
        return [path for mode, typ, _obj, _size, path in self.ls_tree(sha) if typ == "commit"]


def unique(seq: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(seq))
