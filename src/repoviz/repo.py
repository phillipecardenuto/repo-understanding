"""High-level entry point: open a repository, take snapshots, compare them."""

from __future__ import annotations

import dataclasses
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .analyzers import supported_languages
from .config import Config, load_config
from .discovery import RepositoryProfile, discover
from .gitutil import Git, GitError, RevisionError, probe_repository
from .ids import stable_hash
from .model import Diagnostic, RepositoryDiff, RepositorySnapshot
from .pipeline import build_snapshot, enabled_analyzer_classes
from .session import Session, StateStore
from .sources import (
    EmptySource,
    FilesystemSource,
    GitIndexSource,
    GitRevisionSource,
    RevSpec,
    TreeSource,
    WorkingTreeSource,
)

#: Named comparisons.  Values are (base, target) revision specs.
PRESETS: dict[str, tuple[str, str, str]] = {
    "all": ("HEAD", "WORKTREE", "HEAD vs working tree (staged + unstaged + untracked)"),
    "working": ("HEAD", "WORKTREE", "HEAD vs working tree (staged + unstaged + untracked)"),
    "staged": ("HEAD", "INDEX", "Staged changes only (HEAD vs index)"),
    "unstaged": ("INDEX", "WORKTREE-TRACKED", "Unstaged changes only (index vs working tree, tracked files)"),
    "session": ("SESSION", "WORKTREE", "Current work session (session baseline vs working tree)"),
}


class RepositoryError(RuntimeError):
    pass


@dataclass
class Comparison:
    """A resolved comparison request."""

    base: str
    target: str
    label: str
    base_label: str
    target_label: str
    mode: str = "custom"

    def to_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


def parse_comparison(text: str) -> tuple[str, str, str]:
    """Parse ``preset`` | ``A..B`` | ``A...B`` (merge base of A and B, vs B) | ``A`` (A vs working tree)."""
    text = text.strip()
    if text.lower() in PRESETS:
        return PRESETS[text.lower()][0], PRESETS[text.lower()][1], text.lower()
    if "..." in text:
        a, _, b = text.partition("...")
        return f"merge-base:{a or 'HEAD'}:{b or 'WORKTREE'}", b or "WORKTREE", "merge-base"
    if ".." in text:
        a, _, b = text.partition("..")
        return a or "HEAD", b or "WORKTREE", "range"
    return text, "WORKTREE", "revision-vs-worktree"


class Repository:
    """A repository (or plain directory) opened for read-only analysis."""

    SNAPSHOT_CACHE_SIZE = 24

    def __init__(self, path: str | Path = ".", *, config: Config | None = None, config_file: str | None = None,
                 overrides: dict[str, Any] | None = None) -> None:
        start = Path(path).expanduser().resolve()
        if not start.exists():
            raise RepositoryError(f"{start} does not exist")
        git_root, self.git_warning = probe_repository(start)
        self.root = git_root or (start if start.is_dir() else start.parent)
        self.git = Git(self.root) if git_root else None
        self.config = config or load_config(self.root, config_file, overrides)
        self.name = self.root.name
        self.file_cache: dict[Any, Any] = {}
        self._snapshots: OrderedDict[tuple[str, ...], RepositorySnapshot] = OrderedDict()
        self._diffs: OrderedDict[tuple[str, ...], RepositoryDiff] = OrderedDict()
        self._couplings: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._graph_indexes: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._lock = threading.RLock()
        self._inflight: dict[tuple[Any, ...], threading.Lock] = {}
        self.state = StateStore(self.root, self.name, self.config.state_dir)

    # -- identity -------------------------------------------------------------

    @property
    def is_git(self) -> bool:
        return self.git is not None

    @property
    def repository_id(self) -> str:
        cached = self.__dict__.get("_repository_id")
        if cached:
            return cached
        roots = self.git.root_commits() if self.git else []
        # The root commit identifies a repository across clones and machines.
        ident = "repo_" + (stable_hash("roots", *roots, length=16) if roots else stable_hash("path", str(self.root),
                                                                                                length=16))
        self.__dict__["_repository_id"] = ident
        return ident

    def git_info(self) -> dict[str, Any]:
        if self.git is None:
            return {"is_git": False}
        return {
            "is_git": True,
            "branch": self.git.branch(),
            "head": self.git.head(),
            "default_branch": self.git.default_branch(self.config.default_branch),
            "remotes": self.git.remotes(),
            "shallow": self.git.is_shallow(),
        }

    def revisions(self, commits: int = 30) -> dict[str, Any]:
        info = self.git_info()
        if self.git is None:
            return {**info, "branches": [], "tags": [], "commits": [], "remote_branches": []}
        return {
            **info,
            "branches": self.git.branches(),
            "remote_branches": self.git.remote_branches()[:100],
            "tags": self.git.tags(100),
            "commits": [c.to_dict() for c in self.git.recent_commits(commits)],
            "has_staged": any(e.staged for e in self.git.status()),
            "presets": {k: v[2] for k, v in PRESETS.items() if k != "working"},
        }

    # -- sources -------------------------------------------------------------------

    def current_session(self) -> Session | None:
        session = self.state.current_session()
        return session if session and session.active else None

    def open_source(self, spec: str | RevSpec) -> TreeSource:
        rs = RevSpec.parse(spec) if isinstance(spec, str) else spec
        if rs.kind == "empty":
            return EmptySource()
        if self.git is None:
            if rs.kind in ("worktree", "worktree-tracked"):
                return FilesystemSource(self.root, label="directory")
            raise RepositoryError(f"'{rs}' requires a Git repository; {self.root} is a plain directory")
        if rs.kind == "worktree":
            return WorkingTreeSource(self.git, include_untracked=True)
        if rs.kind == "worktree-tracked":
            return WorkingTreeSource(self.git, include_untracked=False)
        if rs.kind == "index":
            return GitIndexSource(self.git)
        if rs.kind == "session":
            session = self.current_session()
            if session is None:
                raise RepositoryError("no active session; start one with 'repoviz session start'")
            return self.state.baseline_source(session, self.git)
        if rs.kind in ("session-at", "session-end"):
            session = self.state.load_session(rs.rev)
            if session is None:
                raise RepositoryError(f"unknown session {rs.rev!r} (see 'repoviz session list')")
            if rs.kind == "session-at":
                return self.state.baseline_source(session, self.git)
            if session.active:
                return WorkingTreeSource(self.git, include_untracked=True)
            return self.state.end_source(session, self.git)
        rev = rs.rev
        if rev.startswith("merge-base:"):
            _, a, b = rev.split(":", 2)
            b_rev = "HEAD" if RevSpec.parse(b).kind != "git" else b
            try:
                sha = self.git.merge_base(a, b_rev)
            except RevisionError as exc:
                raise RepositoryError(str(exc)) from exc
            return GitRevisionSource(self.git, sha, label=f"merge base of {a} and {b_rev} ({sha[:10]})")
        if rev.upper() == "HEAD" and self.git.head() is None:
            return EmptySource("HEAD (no commits yet)")
        try:
            sha = self.git.resolve(rev)
        except RevisionError as exc:
            raise RepositoryError(str(exc)) from exc
        label = rev if rev == sha else f"{rev} ({sha[:10]})"
        return GitRevisionSource(self.git, sha, label=label)

    def analysis_source(self, source: TreeSource) -> TreeSource:
        """What the analysis reads: ``source`` plus its checked-out submodules (``[submodules] analyze``)."""
        cfg = self.config
        if not cfg.submodules_analyze or self.git is None or not source.submodules:
            return source
        from .submodules import nested_source

        return nested_source(self.root, source, exclude=cfg.submodules_exclude, max_files=cfg.submodules_max_files,
                             max_mb=cfg.submodules_max_mb)

    def discover(self, source: TreeSource | None = None) -> RepositoryProfile:
        source = self.analysis_source(source or self.open_source("WORKTREE"))
        enabled = {cls.name for cls in enabled_analyzer_classes(self.config)}
        langs = {lang: [a for a in names if a in enabled] for lang, names in supported_languages().items()}
        profile = discover(source, self.config, root=str(self.root), name=self.name,
                           supported_languages={k: v for k, v in langs.items() if v}, git_info=self.git_info())
        if self.git_warning:
            profile.diagnostics.insert(0, Diagnostic("warning", "git-unavailable", self.git_warning, "repository"))
        return profile

    # -- snapshots -------------------------------------------------------------------

    def snapshot(self, spec: str | RevSpec = "WORKTREE", label: str | None = None) -> RepositorySnapshot:
        source = self.open_source(spec)
        return self.snapshot_of(source, label or source.label)

    def _cached(self, cache: OrderedDict, key: tuple[Any, ...]) -> Any:
        with self._lock:
            hit = cache.get(key)
            if hit is not None:
                cache.move_to_end(key)
            return hit

    def _single_flight(self, key: tuple[Any, ...]) -> threading.Lock:
        """One lock per cache key, so concurrent requests for the same result compute it once."""
        with self._lock:
            return self._inflight.setdefault(key, threading.Lock())

    def snapshot_of(self, source: TreeSource, label: str) -> RepositorySnapshot:
        # The label is cosmetic ("HEAD" vs "HEAD (1a2b3c)"): one analysis serves every label.
        key = (source.kind, source.revision_id, self.config.fingerprint())
        cached = self._cached(self._snapshots, key)
        if cached is not None:
            return self._relabel(cached, label)
        with self._single_flight(("snapshot", *key)):
            cached = self._cached(self._snapshots, key)
            if cached is not None:
                return self._relabel(cached, label)
            source = self.analysis_source(source)  # only on a miss: the key already covers the submodules' state
            profile = self.discover(source)
            snap = build_snapshot(source, profile, self.config, repository_id=self.repository_id,
                                  repository_name=self.name, root=str(self.root), label=label, git=self.git,
                                  file_cache=self.file_cache)
            with self._lock:
                self._snapshots[key] = snap
                while len(self._snapshots) > self.SNAPSHOT_CACHE_SIZE:
                    self._snapshots.popitem(last=False)
                if len(self.file_cache) > 200_000:
                    self.file_cache.clear()
                self._inflight.pop(("snapshot", *key), None)
        return snap

    def graph_index(self, spec: str | RevSpec = "WORKTREE") -> Any:
        """The query index (``query.GraphIndex``) of a snapshot: built once per snapshot, then reused by every
        "why" and "blast radius" question about it."""
        from .query import GraphIndex

        snap = self.snapshot(spec)
        key = (snap.kind, snap.revision_id, self.config.fingerprint())
        cached = self._cached(self._graph_indexes, key)
        if cached is not None:
            return cached
        with self._single_flight(("graph_index", *key)):
            cached = self._cached(self._graph_indexes, key)
            if cached is not None:
                return cached
            idx = GraphIndex(snap)
            with self._lock:
                self._graph_indexes[key] = idx
                while len(self._graph_indexes) > 2:
                    self._graph_indexes.popitem(last=False)
                self._inflight.pop(("graph_index", *key), None)
        return idx

    @staticmethod
    def _relabel(snap: RepositorySnapshot, label: str) -> RepositorySnapshot:
        if snap.label == label:
            return snap
        copy = dataclasses.replace(snap, label=label, revision=label)  # shares the (read-only) graph
        if "_node_index" in snap.__dict__:
            copy.__dict__["_node_index"] = snap.__dict__["_node_index"]
        return copy

    # -- comparisons ---------------------------------------------------------------------

    def resolve_comparison(self, base: str | None = None, target: str | None = None, *, mode: str | None = None,
                           spec: str | None = None) -> Comparison:
        if spec:
            base, target, mode = parse_comparison(spec)
        elif mode and mode in PRESETS:
            base, target = PRESETS[mode][0], PRESETS[mode][1]
        elif mode == "merge-base":
            ref = base or self.git_info().get("default_branch") or "HEAD"
            base, target = f"merge-base:{ref}:{target or 'WORKTREE'}", target or "WORKTREE"
        base = base or "HEAD"
        target = target or "WORKTREE"
        base_src_label = self._label(base)
        target_src_label = self._label(target)
        label = PRESETS[mode][2] if mode in PRESETS else f"{base_src_label} → {target_src_label}"
        return Comparison(base, target, label, base_src_label, target_src_label, mode or "custom")

    @staticmethod
    def _label(spec: str) -> str:
        if spec.startswith("merge-base:"):
            _, a, b = spec.split(":", 2)
            return f"merge base of {a} and {'HEAD' if RevSpec.parse(b).kind != 'git' else b}"
        return RevSpec.parse(spec).label

    def diff(self, base: RepositorySnapshot, target: RepositorySnapshot) -> RepositoryDiff:
        """Diff two snapshots (cached: snapshots with the same revision IDs give the same diff)."""
        from .diff import diff_snapshots

        key = (base.kind, base.revision_id, base.label, target.kind, target.revision_id, target.label,
               base.metadata.get("config_fingerprint"), target.metadata.get("config_fingerprint"))
        cached = self._cached(self._diffs, key)
        if cached is not None:
            return cached
        with self._single_flight(("diff", *key)):
            cached = self._cached(self._diffs, key)
            if cached is not None:
                return cached
            result = diff_snapshots(base, target)
            with self._lock:
                self._diffs[key] = result
                while len(self._diffs) > 8:
                    self._diffs.popitem(last=False)
                self._inflight.pop(("diff", *key), None)
        return result

    def coupling(self, rev: str | None = None) -> Any:
        """Which files usually change together, learned from the history up to ``rev`` (default ``HEAD``).

        Cached per commit and settings, so polling and repeated reviews cost one lookup.
        """
        from .history import HistorySettings, co_change

        settings = HistorySettings.from_config(self.config)
        sha: str | None = None
        if self.git is not None:
            if rev and len(rev) == 40 and all(c in "0123456789abcdef" for c in rev):
                sha = rev
            else:
                out = self.git.try_run("rev-parse", "--verify", "--quiet", "--end-of-options",
                                       f"{rev or 'HEAD'}^{{commit}}")
                sha = out.strip() if out else None
        key = (sha, settings)
        cached = self._cached(self._couplings, key)
        if cached is not None:
            return cached
        with self._single_flight(("coupling", *key)):
            cached = self._cached(self._couplings, key)
            if cached is not None:
                return cached
            result = co_change(self.git, sha, settings)
            with self._lock:
                self._couplings[key] = result
                while len(self._couplings) > 4:
                    self._couplings.popitem(last=False)
                self._inflight.pop(("coupling", *key), None)
        return result

    def compare(self, base: str | None = None, target: str | None = None, *, mode: str | None = None,
                spec: str | None = None) -> tuple[Comparison, RepositoryDiff]:
        comp = self.resolve_comparison(base, target, mode=mode, spec=spec)
        base_snap = self.snapshot(comp.base, comp.base_label)
        target_snap = self.snapshot(comp.target, comp.target_label)
        return comp, self.diff(base_snap, target_snap)

    def default_comparisons(self) -> list[Comparison]:
        """Comparisons worth precomputing for a static report."""
        comps = [self.resolve_comparison(mode="all")]
        if self.git is None or self.git.head() is None:
            return comps
        try:
            status = self.git.status()
        except GitError:
            status = []
        if any(e.staged for e in status):
            comps += [self.resolve_comparison(mode="staged"), self.resolve_comparison(mode="unstaged")]
        default = self.git_info().get("default_branch")
        branch = self.git.branch()
        if default and branch and default != branch and default.split("/")[-1] != branch:
            try:
                self.git.merge_base(default, "HEAD")
                comps.append(self.resolve_comparison(f"merge-base:{default}:WORKTREE", "WORKTREE", mode="merge-base"))
                comps[-1].label = f"Branch changes: merge-base({default}) vs working tree"
            except (GitError, RevisionError):
                pass
        if self.current_session() is not None:
            comps.append(self.resolve_comparison(mode="session"))
        return comps

    def close(self) -> None:
        if self.git is not None:
            self.git.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
