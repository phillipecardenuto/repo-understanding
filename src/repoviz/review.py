"""Review of AI-agent work ("waves").

``build_review`` turns a comparison (a work session, a past wave, a branch or
any range) into a review report designed for supervising coding agents:

* **where the agent went**: touched components and modules, with lines changed
  and the agreed scope (allowed / protected globs) evaluated for every file;
* **what it changed**: per file, the changed symbols ("key changes") with
  before/after signatures, dependency changes and the code diff;
* **what deserves attention**: *findings* -- heuristic signals such as protected
  or out-of-scope files, new cycles or forbidden dependencies, broken imports,
  calls to removed functions, changed signatures whose callers were not
  updated, untested changes, weakened tests, debugger statements, swallowed
  exceptions or possible secrets.  Findings are prompts for a human reviewer,
  not proof of a bug;
* **what to tell the agent**: reviewer notes (with verdicts such as "should
  not have been touched" or "logic error") are turned into a feedback prompt
  by :func:`feedback_markdown`.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from . import classify, globs
from .activity import _ImpactIndex, _tests_affected, nodes_by_path
from .config import DependencyRule
from .contracts import check as check_contracts
from .contracts import contracts_of, load_baseline
from .diff import symbol_changes
from .flow import affected_flow
from .gitutil import GitError
from .history import skip_companion
from .ids import content_hash as _blob_hash
from .ids import make_id, stable_hash
from .model import (
    ADDED,
    CATEGORY_MODULE,
    CATEGORY_SYMBOL,
    MODIFIED,
    REL_CALLS,
    REL_DEPENDS_ON,
    REL_IMPORTS,
    REMOVED,
    UNCHANGED,
    RepositoryDiff,
    RepositorySnapshot,
)
from .pipeline import utcnow
from .redact import contains_secret as _secret_in
from .redact import redact as _redact
from .risk import RiskContext
from .sources import TreeSource, is_binary
from .submodules import WithSubmoduleFiles, submodule_changes
from .wiring import unwired_code

if TYPE_CHECKING:  # pragma: no cover
    from .repo import Repository

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

#: Verdicts a reviewer can attach to a note; they shape the feedback prompt.
VERDICTS = {
    "should-not-touch": "Should not have been modified",
    "logic-error": "Logic error",
    "missed": "Missed / incomplete",
    "improve": "Should be improved",
    "question": "Question",
    "ok": "Looks good",
}

MAX_LINE_CHARS = 400


# --------------------------------------------------------------------------- scope


@dataclass
class ScopePolicy:
    """What the agent was allowed to change, and what it must not touch."""

    allowed: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    rules: list[DependencyRule] = field(default_factory=list)
    sensitive: bool = True
    origin: list[str] = field(default_factory=list)

    def classify(self, path: str) -> str:
        """``protected`` | ``allowed`` | ``out-of-scope`` | ``unscoped``."""
        if self.protected and globs.match_any(path, self.protected):
            return "protected"
        if self.allowed:
            return "allowed" if globs.match_any(path, self.allowed) else "out-of-scope"
        return "unscoped"

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "protected": self.protected, "sensitive": self.sensitive,
                "rules": [asdict(r) for r in self.rules], "origin": self.origin}


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


# --------------------------------------------------------------------------- targets


@dataclass
class ReviewTarget:
    id: str
    label: str
    base: str
    target: str
    kind: str  # session | past-session | preset | range
    key: str
    session_id: str | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Ends of a comparison that are not commits, so they have no merge base.
_NOT_COMMITS = ("WORKTREE", "WORKTREE-TRACKED", "INDEX", "EMPTY")
MAX_BRANCH_TARGETS = 5


def range_target(repo: "Repository", base: str | None, target: str | None, mode: str | None = None) -> ReviewTarget:
    """Review ``target`` against ``base``: any two branches, tags, commits or the working tree.

    ``mode="merge-base"`` reviews what ``target`` added since it left ``base``, like a pull request, so work that
    landed on ``base`` in the meantime does not show up as reverted.  ``"exact"`` (the default) compares the two
    trees as they are.  Both keep the key of the CLI spec (``range:A...B`` / ``range:A..B``), so notes are shared.
    """
    base = (base or "").strip() or "HEAD"
    target = (target or "").strip() or "WORKTREE"
    if mode not in (None, "", "exact", "merge-base"):
        raise ValueError(f"unknown comparison mode {mode!r} (use merge-base or exact)")
    if mode == "merge-base":
        if repo.git is None:
            raise ValueError("comparing since two revisions diverged needs a Git repository")
        if base.upper() in _NOT_COMMITS or base.startswith(("SESSION", "merge-base:")):
            raise ValueError(f"'since they diverged' needs a branch, tag or commit as the base, not {base!r}")
        spec = f"{base}...{target}"
        comp = repo.resolve_comparison(spec=spec)
        fork = repo.git.merge_base(base, "HEAD" if target.upper() in _NOT_COMMITS else target)
        label = f"{comp.target_label} since it left {base} (merge base {fork[:10]})"
    else:
        spec = f"{base}..{target}"
        comp = repo.resolve_comparison(base, target)
        label = comp.label + (" (exact difference)" if mode == "exact" else "")
    return ReviewTarget(f"range:{spec}", label, comp.base, comp.target, "range", f"range:{spec}")


def _branch_targets(repo: "Repository", default: str, current: str) -> list[ReviewTarget]:
    """Recently updated local branches with commits the default branch lacks (open work, like pull requests).

    Memoised on the state of the refs, since the review tab lists targets every time it is shown."""
    git = repo.git
    try:
        default_sha = git.resolve(default)
    except GitError:
        return []
    branches = git.recent_branches(20)
    key = (default, default_sha, current, tuple(branches))
    cached = repo.__dict__.get("_branch_targets")
    if cached is not None and cached[0] == key:
        return list(cached[1])
    out: list[ReviewTarget] = []
    for name, sha in branches:
        if name in (current, default) or sha == default_sha:
            continue
        ahead = git.ahead_count(default_sha, sha)
        if not ahead:
            continue  # merged, or nothing of its own
        out.append(ReviewTarget(f"range:{default}...{name}", f"Branch {name} vs {default} (since merge base)",
                                f"merge-base:{default}:{name}", name, "branch", f"range:{default}...{name}",
                                description=f"{ahead} commit(s) not in {default}"))
        if len(out) >= MAX_BRANCH_TARGETS:
            break
    repo.__dict__["_branch_targets"] = (key, out)
    return list(out)


def review_targets(repo: "Repository", limit_sessions: int = 20) -> list[ReviewTarget]:
    """Reviewable units of work: the active session, uncommitted work, the branch, the last commit, past waves
    and other recent branches."""
    out: list[ReviewTarget] = []
    active = repo.current_session()
    if active is not None:
        out.append(ReviewTarget("session", f"Current session: {active.label or active.id}", "SESSION", "WORKTREE",
                                "session", f"session:{active.id}", active.id,
                                f"started {active.started_at}; everything changed since then, including commits"))
    if repo.git is None:
        out.append(ReviewTarget("all", "Directory (no Git history)", "EMPTY", "WORKTREE", "preset", "all"))
        return out
    head = repo.git.head()
    branch = repo.git.branch() or "HEAD"
    if head:
        out.append(ReviewTarget("all", "Uncommitted changes (HEAD vs working tree)", "HEAD", "WORKTREE", "preset",
                                f"all@{branch}", description="staged + unstaged + untracked"))
        default = repo.git_info().get("default_branch")
        if default and default.split("/")[-1] != branch:
            try:
                repo.git.merge_base(default, "HEAD")
                out.append(ReviewTarget("branch", f"Branch {branch} vs {default} (since merge base)",
                                        f"merge-base:{default}:WORKTREE", "WORKTREE", "preset",
                                        f"branch:{branch}:{default}"))
            except Exception:
                pass
        if repo.git.try_run("rev-parse", "--verify", "--quiet", "HEAD~1^{commit}"):
            info = repo.git.commit_info(head)
            subject = f": {_redact(info.subject)}" if info else ""
            out.append(ReviewTarget("last-commit", f"Last commit{subject}"[:120], "HEAD~1", "HEAD", "preset",
                                    f"commit:{head}"))
    ended = [s for s in repo.state.list_sessions() if not s.active]
    for s in sorted(ended, key=lambda s: s.started_at, reverse=True)[:limit_sessions]:
        out.append(ReviewTarget(f"session:{s.id}", f"Wave: {s.label or s.id} ({s.started_at[:16]} → "
                                f"{(s.ended_at or '')[:16]})", f"SESSION@{s.id}", f"SESSION-END@{s.id}",
                                "past-session", f"session:{s.id}", s.id))
    default = repo.git_info().get("default_branch") if head else None
    if default:
        out += _branch_targets(repo, default, branch)
    return out


def resolve_target(repo: "Repository", target_id: str | None = None, base: str | None = None,
                   target: str | None = None, mode: str | None = None) -> ReviewTarget:
    """A listed target (by id), a past wave (``session:<id>``), a comparison spec (``A...B``, ``A..B``, a
    preset or one revision), or ``base`` / ``target`` with a ``mode`` (see :func:`range_target`)."""
    if base or target:
        return range_target(repo, base, target, mode)
    targets = review_targets(repo)
    if target_id is None:
        if not targets:
            raise ValueError("nothing to review")
        return targets[0]
    for t in targets:
        if t.id == target_id:
            return t
    if target_id.startswith("session:"):
        sid = target_id.split(":", 1)[1]
        s = repo.state.load_session(sid)
        if s is not None:
            if s.active:
                return ReviewTarget("session", f"Current session: {s.label or s.id}", "SESSION", "WORKTREE",
                                    "session", f"session:{s.id}", s.id)
            return ReviewTarget(target_id, f"Wave: {s.label or s.id}", f"SESSION@{s.id}", f"SESSION-END@{s.id}",
                                "past-session", target_id, s.id)
    from .repo import parse_comparison

    spec = target_id[len("range:"):] if target_id.startswith("range:") else target_id
    if "..." in spec:
        a, _, b = spec.partition("...")
        return range_target(repo, a, b, "merge-base")
    if ".." in spec:
        a, _, b = spec.partition("..")
        return range_target(repo, a, b)
    b, t, _mode = parse_comparison(target_id)
    comp = repo.resolve_comparison(b, t)
    return ReviewTarget(target_id, comp.label, comp.base, comp.target, "range", f"range:{target_id}")


def scope_for(repo: "Repository", target: ReviewTarget, allowed: list[str] | None = None,
              protected: list[str] | None = None) -> ScopePolicy:
    cfg = repo.config
    policy = ScopePolicy(list(cfg.review_allowed), list(cfg.review_protected), list(cfg.review_rules),
                         cfg.review_sensitive)
    if cfg.review_allowed or cfg.review_protected or cfg.review_rules:
        policy.origin.append("configuration")
    if target.session_id:
        s = repo.state.load_session(target.session_id)
        if s is not None and (s.allowed or s.protected):
            policy.allowed = _dedupe(policy.allowed + s.allowed)
            policy.protected = _dedupe(policy.protected + s.protected)
            policy.origin.append(f"session {s.label or s.id}")
    if allowed:
        policy.allowed = _dedupe(policy.allowed + allowed)
        policy.origin.append("command line")
    if protected:
        policy.protected = _dedupe(policy.protected + protected)
        if "command line" not in policy.origin:
            policy.origin.append("command line")
    return policy


# --------------------------------------------------------------------------- findings


@dataclass
class Finding:
    kind: str
    category: str  # scope | architecture | correctness | tests | hygiene | security
    severity: str  # high | medium | low | info
    title: str
    detail: str = ""
    path: str | None = None
    line: int | None = None
    excerpt: str | None = None
    symbol: str | None = None
    component: str | None = None
    suggestion: str = ""
    id: str = ""

    def finalize(self, key: str = "") -> "Finding":
        self.id = "f_" + stable_hash("finding", self.kind, self.path or "", key or self.detail or self.title, length=16)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", [])}


MAX_CHECKED_LINE = 1000

# (kind, category, severity, title, pattern, scope) where scope is "any" | "code" (non-test) | "test"
_LINE_CHECKS: list[tuple[str, str, str, str, re.Pattern[str], str]] = [
    ("debugger", "hygiene", "medium", "Debugger statement left in code",
     re.compile(r"\b(breakpoint\(\)|pdb\.set_trace\(\)|ipdb\.set_trace\(\)|debugger\s*;)"), "any"),
    ("debug-output", "hygiene", "low", "Debug output added",
     re.compile(r"^\s*(print\(|console\.(log|debug|dir|trace)\(|fmt\.Print(ln|f)?\(|System\.out\.print|dbg!\()"), "code"),
    ("todo", "hygiene", "low", "TODO / FIXME added", re.compile(r"\b(TODO|FIXME|XXX|HACK)\b"), "any"),
    ("suppression", "hygiene", "low", "Checker suppression added",
     re.compile(r"(#\s*type:\s*ignore|#\s*noqa|#\s*pragma:\s*no\s*cover|eslint-disable|@ts-ignore|@ts-expect-error|"
                r"//\s*nolint|@SuppressWarnings|#\[allow\()"), "any"),
    ("stub", "correctness", "medium", "Unimplemented stub added",
     re.compile(r"(raise\s+NotImplementedError|throw\s+new\s+Error\(\s*['\"`](not implemented|todo)|"
                r"\bunimplemented!\(|\btodo!\(|panic\(\s*\"(TODO|not implemented))", re.I), "code"),
    ("test-disabled", "tests", "high", "Test skipped, disabled or focused",
     re.compile(r"(@pytest\.mark\.(skip|xfail)|pytest\.(skip|xfail)\(|@unittest\.skip|\b(it|test|describe)\.(skip|only|todo)\(|"
                r"\b(xit|xdescribe|fit|fdescribe)\(|\bt\.Skip\(|@Disabled\b|@Ignore\b|#\[ignore\])"), "test"),
    ("trivial-assertion", "tests", "medium", "Trivial assertion",
     re.compile(r"(\bassert\s+(True|1)\s*$|expect\(\s*true\s*\)\.toBe\(\s*true\s*\)|assertTrue\(\s*true\s*\))"), "test"),
]
_ASSERT = re.compile(r"(\bassert\b|\bexpect\(|self\.assert\w*\(|\bt\.(Error|Fatal|Errorf|Fatalf)\(|assert_eq!|assert!\()")
_ERROR_HANDLING = re.compile(r"^\s*(raise\b|except\b|throw\b|\}\s*catch\b|catch\s*\(|return\s+err\b|panic\()")
_COMMENTED_CODE = re.compile(r"^\s*(#|//)\s*(def |class |import |from \w+ import|return\b|if .*:|for .*:|while |const |let |"
                             r"var |function |[\w.]+\(.*\)\s*;?\s*$)")
_SENSITIVE_DIRS = re.compile(r"(^|/)(migrations?|alembic|db/migrate|\.github|\.circleci|deploy|k8s|helm|terraform|infra)/")


# --------------------------------------------------------------------------- diffs


def file_hunks(before: str, after: str, context: int = 3) -> tuple[list[dict[str, Any]], list[tuple[int, str]],
                                                                    list[tuple[int, str]]]:
    """Unified-diff hunks plus the added ``(new line, text)`` and removed ``(old line, text)`` lines."""
    a = before.splitlines()
    b = after.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=len(a) + len(b) > 20000)
    hunks: list[dict[str, Any]] = []
    added: list[tuple[int, str]] = []
    removed: list[tuple[int, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed += [(i + 1, a[i]) for i in range(i1, i2)]
        if tag in ("replace", "insert"):
            added += [(j + 1, b[j]) for j in range(j1, j2)]
    for group in sm.get_grouped_opcodes(context):
        lines: list[str] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines += [" " + l[:MAX_LINE_CHARS] for l in a[i1:i2]]
                continue
            if tag in ("replace", "delete"):
                lines += ["-" + l[:MAX_LINE_CHARS] for l in a[i1:i2]]
            if tag in ("replace", "insert"):
                lines += ["+" + l[:MAX_LINE_CHARS] for l in b[j1:j2]]
        first, last = group[0], group[-1]
        hunks.append({"old_start": first[1] + 1, "old_len": last[2] - first[1], "new_start": first[3] + 1,
                      "new_len": last[4] - first[3], "lines": lines})
    return hunks, added, removed


def _symbol_at(symbols: list[Any], line: int) -> Any:
    best = None
    for s in symbols:
        if s.start_line and s.end_line and s.start_line <= line <= s.end_line:
            if best is None or (s.end_line - s.start_line) < (best.end_line - best.start_line):
                best = s
    return best


# --------------------------------------------------------------------------- the report


def _text(source: TreeSource, path: str, limit: int = 2_000_000) -> tuple[str | None, bool]:
    data = source.read_bytes(path)
    if data is None:
        return "", False
    if is_binary(data):
        return None, True
    if len(data) > limit:
        return None, False
    return data.decode("utf-8", "replace"), False


def _submodule_entry(ch: Any, files: list[dict[str, Any]], scope: ScopePolicy, component: tuple[str | None, str | None],
                     add: Any, moved_from: str | None = None) -> dict[str, Any]:
    """A review entry (and findings) for a submodule whose pointer or contents changed."""
    inner = {p for p, _b, _a in ch.files}
    inner_entries = [f for f in files if f["path"] in inner]
    cid, cname = component
    scope_status = scope.classify(ch.path)
    status = {"added": ADDED, "removed": REMOVED}.get(ch.status, MODIFIED)
    entry: dict[str, Any] = {
        "path": ch.path, "status": status, "language": None, "component_id": cid, "component": cname,
        "module_id": None, "is_test": False, "scope": scope_status, "binary": False, "config_kind": None,
        "kind": "submodule", "submodule": ch.to_dict(), "hunks": [], "symbols": [], "dependencies": [],
        "tests_affected": [], "diff_omitted": "a submodule: see its commits and the files changed inside it",
        # The files inside carry the line counts; the submodule entry only summarises them (not counted twice).
        "lines_added": None, "lines_removed": None,
        "inner_lines": [sum(f.get("lines_added") or 0 for f in inner_entries),
                        sum(f.get("lines_removed") or 0 for f in inner_entries)],
        "version": stable_hash("submodule", ch.new or "-", *(f"{p}:{_blob_hash(a) if a else '-'}"
                                                           for p, _b, a in ch.files), length=16),
    }
    short = lambda sha: sha[:10] if sha else "?"  # noqa: E731
    log = "\n".join(f"{c['sha']} {c['subject']}" for c in (ch.commits or [])[:8]) or None
    if moved_from and ch.status == "added":
        entry["status"] = "renamed"
        entry["previous_path"] = moved_from
        add(Finding("submodule-moved", "architecture", "medium", "Submodule moved",
                    f"The submodule at {moved_from} now lives at {ch.path} (at {short(ch.new)}).", ch.path,
                    component=cname, suggestion="Check build files, Dockerfiles and imports that use the old path."))
    elif ch.status == "added":
        add(Finding("submodule-added", "architecture", "medium", "Submodule added",
                    f"{ch.path} was added as a submodule (at {short(ch.new)}).", ch.path, excerpt=log,
                    component=cname, suggestion="Confirm the new dependency on this repository is intended."))
    elif ch.status == "removed":
        add(Finding("submodule-removed", "architecture", "medium", "Submodule removed",
                    f"{ch.path} (at {short(ch.old)}) was removed.", ch.path, component=cname,
                    suggestion="Check nothing still builds, runs or imports it."))
    if "updated" in ch.status:
        count = f"{ch.commit_count} commit(s)" if ch.commit_count is not None else "an unknown number of commits"
        add(Finding("submodule-updated", "architecture", "medium", "Submodule moved to another commit",
                    f"{ch.path} moved {short(ch.old)} → {short(ch.new)} ({count})." + (f" Note: {ch.note}." if ch.note else ""),
                    ch.path, excerpt=log, component=cname,
                    suggestion="Check the submodule's commits belong to this task and were tested together."))
    if ch.dirty:
        add(Finding("submodule-uncommitted", "correctness", "medium", "Uncommitted changes inside a submodule",
                    f"{len(ch.dirty)} file(s) changed inside {ch.path} are not committed in the submodule, so this "
                    "repository cannot record them.", ch.path, excerpt=", ".join(ch.dirty[:10]), component=cname,
                    suggestion="Commit (and push) inside the submodule, then update the pointer here, or revert."))
    if not inner:  # changed files inside are flagged one by one; otherwise flag the submodule itself
        if scope_status == "protected":
            add(Finding("protected-touched", "scope", "high", "Protected area modified",
                        f"{ch.path} matches a protected pattern ({_first_match(ch.path, scope.protected)}).", ch.path,
                        component=cname, suggestion="Revert this change unless it was explicitly requested."))
        elif scope_status == "out-of-scope":
            add(Finding("out-of-scope", "scope", "medium", "Change outside the agreed scope",
                        f"{ch.path} is not covered by the allowed patterns.", ch.path, component=cname,
                        suggestion="Confirm the change was necessary or revert it."))
    return entry


def build_review(repo: "Repository", target: ReviewTarget, *, scope: ScopePolicy | None = None,
                 max_file_diff_lines: int = 800, max_total_diff_lines: int = 40000,
                 sources: tuple[Any, Any] | None = None, commit: str | None = None) -> dict[str, Any]:
    """Review ``target``; with ``commit`` (a commit of the target's range, or ``"WORKTREE"`` for its uncommitted
    work), review that step alone while keeping the target's scope and notes."""
    scope = scope or scope_for(repo, target)
    crange = _commit_range(repo, target)
    exclude: set[str] = set()
    if commit:
        base_src, target_src, base_label, target_label, exclude, commit = _commit_sources(repo, target, commit, crange)
    else:
        base_src, target_src = sources or (repo.open_source(target.base), repo.open_source(target.target))
        base_label, target_label = repo._label(target.base), repo._label(target.target)
    base_snap = repo.snapshot_of(base_src, base_label)
    target_snap = repo.snapshot_of(target_src, target_label)
    diff = repo.diff(base_snap, target_snap)
    disabled = set(repo.config.review_disabled_checks)
    if "forbidden-dependency" in disabled:  # the signal's name before contracts
        disabled.add("contract-broken")
    b_idx, t_idx = base_snap.node_index(), target_snap.node_index()
    nodes = diff.nodes

    changed_paths = sorted(p for p in set(base_src.files()) | set(target_src.files())
                           if p not in exclude and base_src.content_hash(p) != target_src.content_hash(p))
    # Work inside submodules: pointer moves, and files changed inside them (committed or not).
    sub_changes = (submodule_changes(repo.root, base_src, target_src)
                   if base_src.submodules or target_src.submodules else [])
    if sub_changes:
        base_src = WithSubmoduleFiles(base_src, {p: b for ch in sub_changes for p, b, _a in ch.files})
        target_src = WithSubmoduleFiles(target_src, {p: a for ch in sub_changes for p, _b, a in ch.files})
        changed_paths = sorted(set(changed_paths) | {p for ch in sub_changes for p, _b, _a in ch.files})
    changed_set = set(changed_paths)
    impact = _ImpactIndex(diff, target_snap)
    sym_changes = symbol_changes(diff)
    changed_syms_by_path: dict[str, list[tuple[str, str]]] = {}
    for status in (ADDED, REMOVED, MODIFIED):
        for sid in sym_changes[status]:
            p = nodes[sid].node.path
            if p:
                changed_syms_by_path.setdefault(p, []).append((status, sid))
    symbols_by_path: dict[str, dict[str, list[Any]]] = {"base": {}, "target": {}}
    for which, snap in (("base", base_snap), ("target", target_snap)):
        for n in snap.symbols:
            if n.path in changed_set:
                symbols_by_path[which].setdefault(n.path, []).append(n)

    path_to_node: dict[str, Any] = {**nodes_by_path(base_snap), **nodes_by_path(target_snap)}

    submodule_paths = sorted(set(base_src.submodules) | set(target_src.submodules), key=len, reverse=True)

    def component_of(path: str) -> tuple[str | None, str | None]:
        n = path_to_node.get(path)
        if n is None:  # a file inside a submodule belongs to the submodule's component
            sub = next((s for s in submodule_paths if path.startswith(s + "/")), None)
            n = path_to_node.get(sub) if sub else None
        cid = (n.metadata.get("component_id") or (n.id if "component" in n.tags else None)) if n else None
        if cid is None:  # a file without its own node (e.g. docs): use the nearest directory
            parts = path.split("/")[:-1]
            for i in range(len(parts), -1, -1):
                did = make_id("dir", f"path:dir:{'/'.join(parts[:i])}")
                dnode = t_idx.get(did) or b_idx.get(did)
                if dnode is not None:
                    cid = dnode.metadata.get("component_id") or (did if "component" in dnode.tags else None)
                    if cid:
                        break
        comp = (t_idx.get(cid) or b_idx.get(cid)) if cid else None
        return cid, (comp.qualified_name if comp else None)

    findings: list[Finding] = []

    def add(f: Finding, key: str = "") -> None:
        if f.kind not in disabled:
            findings.append(f.finalize(key))

    files: list[dict[str, Any]] = []
    total_diff_lines = 0
    # Renamed or moved files are one entry at their new path, diffed against their old content.
    file_renames = {r["new_path"]: r["old_path"] for r in diff.renames
                    if r["kind"] == "file" and r["old_path"] and r["new_path"] and r["old_path"] != r["new_path"]
                    and r["new_path"] in changed_set and base_src.content_hash(r["old_path"]) is not None
                    and target_src.content_hash(r["old_path"]) is None}
    renamed_away = set(file_renames.values())
    for path in changed_paths:
        if path in renamed_away:
            continue
        old_path = file_renames.get(path)
        before, b_bin = _text(base_src, old_path or path)
        after, a_bin = _text(target_src, path)
        exists_before = base_src.content_hash(old_path or path) is not None
        exists_after = target_src.content_hash(path) is not None
        status = "renamed" if old_path else ADDED if not exists_before else REMOVED if not exists_after else MODIFIED
        node = path_to_node.get(path)
        cid, cname = component_of(path)
        lang, _kind = classify.language_of(path, repo.config.languages)
        is_test = bool(node and "test" in node.tags) or classify.is_test_path(path)
        scope_status = scope.classify(path)
        if old_path and scope.classify(old_path) == "protected":
            scope_status = "protected"  # moving a protected file away is touching it
        entry: dict[str, Any] = {"path": path, "status": status, "language": lang, "component_id": cid,
                                 "component": cname, "module_id": node.id if node else None, "is_test": is_test,
                                 "scope": scope_status, "binary": b_bin or a_bin, "config_kind": classify.config_kind(path),
                                 # identifies this state of the file, so "reviewed" marks expire when it changes again
                                 "version": (target_src.content_hash(path) or f"-{base_src.content_hash(path)}")[:16]}
        if old_path:
            entry["previous_path"] = old_path
        added_lines: list[tuple[int, str]] = []
        removed_lines: list[tuple[int, str]] = []
        if before is None or after is None:
            entry["hunks"] = []
            entry["diff_omitted"] = "binary file" if (b_bin or a_bin) else "file too large"
            entry["lines_added"] = entry["lines_removed"] = None
        else:
            hunks, added_lines, removed_lines = file_hunks(before, after)
            for hunk in hunks:  # never re-publish a committed secret
                hunk["lines"] = [line[0] + _redact(line[1:]) for line in hunk["lines"]]
            entry["lines_added"], entry["lines_removed"] = len(added_lines), len(removed_lines)
            n_lines = sum(len(h["lines"]) for h in hunks)
            if n_lines > max_file_diff_lines or total_diff_lines + n_lines > max_total_diff_lines:
                entry["hunks"] = []
                entry["diff_omitted"] = (f"{n_lines} diff lines (limit {max_file_diff_lines} per file)"
                                         if n_lines > max_file_diff_lines else "report diff budget exhausted")
            else:
                entry["hunks"] = hunks
                total_diff_lines += n_lines
        # --- key changes: changed symbols with line counts from the diff -------------------
        t_syms = symbols_by_path["target"].get(path, [])
        b_syms = symbols_by_path["base"].get(old_path or path, [])
        per_symbol: dict[str, list[int]] = {}
        for line, _t in added_lines:
            s = _symbol_at(t_syms, line)
            if s is not None:
                per_symbol.setdefault(s.id, [0, 0])[0] += 1
        for line, _t in removed_lines:
            s = _symbol_at(b_syms, line)
            if s is not None:
                per_symbol.setdefault(s.id, [0, 0])[1] += 1
        key_changes = []
        syms = list(changed_syms_by_path.get(path, []))
        if old_path:  # symbols deleted while the file moved still carry the old path
            syms += [(st, sid) for st, sid in changed_syms_by_path.get(old_path, []) if st == REMOVED]
        for st, sid in syms:
            ch = nodes[sid]
            n = ch.node
            if old_path and ch.reasons and all(r.startswith("moved from") for r in ch.reasons):
                continue  # moved along with its file, otherwise unchanged
            counts = per_symbol.get(sid, [0, 0])
            key_changes.append({
                "id": sid, "name": n.name, "qualified_name": n.qualified_name, "kind": n.component_type,
                "status": st, "reasons": ch.reasons, "line": n.start_line, "end_line": n.end_line,
                "signature": n.metadata.get("signature"), "signature_before": ch.before.get("signature"),
                "lines_added": counts[0], "lines_removed": counts[1],
                "public": n.metadata.get("public", n.metadata.get("exported", True)),
                "renamed_from": ch.before.get("name") if any(r.startswith("renamed from") for r in ch.reasons) else None,
            })
        key_changes.sort(key=lambda k: (-(k["lines_added"] + k["lines_removed"]), k["qualified_name"]))
        entry["symbols"] = key_changes
        # --- dependency changes originating in this file ---------------------------------------
        deps = []
        for ch in impact.edges_by_path.get(path, []):
            e = ch.edge
            src = nodes.get(e.source_id)
            if src is None or src.node.path != path:
                continue
            dst = nodes.get(e.target_id)
            deps.append({"status": ch.status, "relationship": e.relationship,
                         "target": dst.node.qualified_name if dst else e.target_id,
                         "target_id": e.target_id, "target_path": dst.node.path if dst else None,
                         "external": bool(dst and "external" in dst.node.tags),
                         "stdlib": bool(dst and "stdlib" in dst.node.tags),
                         "new_cycle": ch.in_target_cycle and not ch.in_base_cycle, "reasons": ch.reasons,
                         "line": (e.evidence or ch.base_evidence or [None])[0].start_line
                         if (e.evidence or ch.base_evidence) else None})
        entry["dependencies"] = deps
        module_id = node.id if node is not None and node.category == CATEGORY_MODULE else None
        entry["tests_affected"] = [path] if is_test else _tests_affected(module_id, impact) if exists_after else []
        files.append(entry)

        # --- per-file findings ------------------------------------------------------------------
        if scope_status == "protected":
            add(Finding("protected-touched", "scope", "high", "Protected area modified",
                        f"{path} matches a protected pattern ({_first_match(path, scope.protected)}).", path,
                        component=cname, suggestion="Revert this change unless it was explicitly requested."))
        elif scope_status == "out-of-scope":
            add(Finding("out-of-scope", "scope", "medium", "Change outside the agreed scope",
                        f"{path} is not covered by the allowed patterns.", path, component=cname,
                        suggestion="Confirm the change was necessary or revert it."))
        sens = _sensitive_kind(path)
        if scope.sensitive and sens and scope_status != "allowed":
            add(Finding("sensitive-file", "scope", "medium", f"Sensitive file changed ({sens})",
                        f"{path} is a {sens} file; changes here affect builds, deployments or data.", path,
                        component=cname))
        if is_test and status == REMOVED:
            add(Finding("test-deleted", "tests", "medium", "Test file deleted", f"{path} was removed.", path,
                        component=cname, suggestion="Check the deleted tests are obsolete, not inconvenient."))
        if added_lines or removed_lines:
            _line_findings(add, path, cname, is_test, added_lines, removed_lines, lang, t_syms)
        if (entry.get("lines_added") or 0) > 400:
            add(Finding("large-change", "hygiene", "info", "Large change",
                        f"{entry['lines_added']} lines added in one file; review in detail.", path, component=cname))

    sub_moves = {r["new_path"]: r["old_path"] for r in diff.renames if r["kind"] == "submodule"
                 and r["old_path"] and r["new_path"]}
    for ch in sub_changes:
        if ch.status == "removed" and ch.path in sub_moves.values():
            continue  # reported once, as a move, at the new path
        files.append(_submodule_entry(ch, files, scope, component_of(ch.path), add, sub_moves.get(ch.path)))

    # --- graph-based findings ---------------------------------------------------------------------
    _graph_findings(add, diff, base_snap, target_snap, target_src, scope, changed_set, path_to_node, component_of,
                    base_src)
    if not {"unwired-module", "unwired-symbol", "unreachable-from-entry"} <= disabled:
        _wiring_findings(add, diff, target_snap, target_src, component_of, repo.config.review_wiring_ignore)
    _test_coverage_findings(add, files, impact)
    contracts = contracts_of(repo.config)
    if contracts and not {"contract-broken", "contract-fixed", "contract-baseline-changed"} <= disabled:
        _contract_findings(add, repo.config.contracts_baseline, contracts, base_snap, target_snap, base_src, target_src,
                           component_of)
    _rename_findings(add, diff, target_snap, target_src, component_of)
    coupling = _coupling_for(repo, target)
    if coupling is not None:
        # One commit alone: the companion may be in another commit of the wave, so only mark partners as changed.
        wave_touched = changed_set | {f["path"] for c in (crange.listing or {}).get("commits", []) for f in c["files"]}
        _coupling_findings(add if not commit else (lambda f, key="": None), files, coupling,
                           wave_touched if commit else changed_set, target_src)
    session = repo.state.load_session(target.session_id) if target.session_id else None
    commits = _attach_commits(repo, target, crange, files, sub_changes, target_src, changed_set, session,
                              add if not commit else None, commit)
    if exclude:  # e.g. files already modified when the session started: not this step's work
        findings = [f for f in findings if f.path not in exclude]
    moved_to = {old: new for new, old in file_renames.items()}
    for f in findings:  # a signal about a moved file's old content belongs on the file's (new) card
        if f.path in moved_to:
            f.detail = f"{f.detail} (before the move: {f.path}{':' + str(f.line) if f.line else ''})".strip()
            f.path, f.line = moved_to[f.path], None

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.category, f.path or "", f.line or 0))
    per_file: dict[str, list[str]] = {}
    for f in findings:
        if f.path:
            per_file.setdefault(f.path, []).append(f.id)
    for entry in files:
        entry["findings"] = per_file.get(entry["path"], [])
    # --- risk: which files to read first, and why ---------------------------------------------------
    risk_ctx = RiskContext(diff, base_snap, target_snap,
                           [{"path": f.path, "severity": f.severity, "title": f.title} for f in findings],
                           weights=repo.config.review_risk_weights, thresholds=repo.config.review_risk_thresholds,
                           component_of=component_of, sensitive_of=_sensitive_kind if scope.sensitive else None,
                           changed_tests={f["path"] for f in files if f["is_test"]})
    for entry in files:
        entry["risk"] = risk_ctx.score_file(entry)
    risk = risk_ctx.wave(files)
    for item in commits["items"]:
        item["signals"] = sum(len(per_file.get(f["path"], [])) for f in item["files"])

    components = _component_summary(files, findings, nodes, t_idx, b_idx)
    comp_edges = _component_edges(diff, {c["id"] for c in components})
    flow = affected_flow(diff)
    severity_counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITY_ORDER}
    summary = {
        "files": len(files), "components": len(components),
        "lines_added": sum(f.get("lines_added") or 0 for f in files),
        "lines_removed": sum(f.get("lines_removed") or 0 for f in files),
        "symbols_changed": sum(len(f["symbols"]) for f in files),
        "tests_changed": sum(1 for f in files if f["is_test"]),
        "submodules_changed": len(sub_changes),
        "protected": sum(1 for f in files if f["scope"] == "protected"),
        "out_of_scope": sum(1 for f in files if f["scope"] == "out-of-scope"),
        "findings": severity_counts,
        "new_dependencies": len(diff.new_dependencies), "cycles_introduced": len(diff.introduced_cycles),
    }
    return {
        "target": target.to_dict(),
        "session": session.to_dict() if session else None,
        "base": diff.base.to_dict(), "head": diff.target.to_dict(),
        "generated_at": utcnow(),
        "scope": scope.to_dict(),
        "summary": summary,
        "risk": risk,
        "components": components,
        "component_edges": comp_edges,
        "files": files,
        "findings": [f.to_dict() for f in findings],
        "flow": flow.to_dict(),
        "new_dependencies": diff.new_dependencies,
        "introduced_cycles": [c.to_dict() for c in diff.introduced_cycles],
        "history": coupling.summary() if coupling is not None else None,
        "commits": commits,
        "commit": _commit_item(repo, commits, commit) if commit else None,
        "verdicts": VERDICTS,
    }


# --------------------------------------------------------------------------- commits of a review

#: The pseudo-commit that stands for uncommitted work at the end of a review range.
UNCOMMITTED = "WORKTREE"
MAX_COMMITS = 200


@dataclass
class _CommitRange:
    base: str | None = None  # commit sha, or None for "all history up to head"
    head: str | None = None
    uncommitted: str | None = None  # label of the uncommitted pseudo-commit when the target includes such work
    listing: dict[str, Any] | None = None  # from Git.commits_in_range
    note: str | None = None
    session: Any = None


def _merge_base_of(repo: "Repository", spec: str) -> str | None:
    """The commit a ``merge-base:A:B`` spec resolves to: merge-base(A, B), with B = HEAD for working-tree specs
    (the same rule as :meth:`Repository.open_source`)."""
    from .sources import RevSpec

    _, a, b = spec.split(":", 2)
    try:
        return repo.git.merge_base(a, "HEAD" if RevSpec.parse(b).kind != "git" else b) if repo.git else None
    except Exception:
        return None


def _commit_range(repo: "Repository", target: ReviewTarget) -> _CommitRange:
    """The commits between the two ends of a review target, and whether uncommitted work follows them."""
    cr = _CommitRange()
    git = repo.git
    if git is None or git.head() is None:
        return cr

    def sha_of(rev: str) -> str | None:
        out = git.try_run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}")
        return out.strip() if out else None

    session = repo.state.load_session(target.session_id) if target.session_id else None
    cr.session = session
    base = target.base
    if base.startswith("SESSION"):
        cr.base = session.baseline_head if session else None
        if cr.base is None:
            return cr
    elif base.startswith("merge-base:"):
        cr.base = _merge_base_of(repo, base)
        if cr.base is None:
            return cr
    elif base in ("INDEX", "WORKTREE", "WORKTREE-TRACKED"):
        return cr
    elif base != "EMPTY":
        cr.base = sha_of(base)
        if cr.base is None:
            return cr
    tgt = target.target
    if tgt in ("WORKTREE", "WORKTREE-TRACKED", "INDEX"):
        cr.head = git.head()
        cr.uncommitted = "Staged changes" if tgt == "INDEX" else "Uncommitted changes"
    elif tgt.startswith("SESSION-END"):
        cr.head = session.end_head if session else None
        if session is not None and (session.end_overrides or session.end_submodule_overrides):
            cr.uncommitted = "Uncommitted at the end of the session"
    else:
        cr.head = sha_of(tgt)
    if cr.head and cr.head != cr.base:
        cr.listing = git.commits_in_range(cr.base, cr.head, MAX_COMMITS)
        if cr.listing is None:
            cr.note = "The commits of this range are not available (shallow clone or missing objects)."
    return cr


def _commit_sources(repo: "Repository", target: ReviewTarget, commit: str,
                    cr: _CommitRange) -> tuple[Any, Any, str, str, set[str], str]:
    """Sources for reviewing one commit of the range (or its uncommitted work) on its own."""
    git = repo.git
    if git is None:
        raise ValueError("commits need a Git repository")
    if commit == UNCOMMITTED:
        if not cr.head or not cr.uncommitted:
            raise ValueError("this review has no uncommitted work")
        exclude: set[str] = set()
        session = cr.session
        if session is not None and session.overrides and target.base.startswith("SESSION"):
            # Files already modified when the session started, and not changed since, are not the agent's work.
            start, now = repo.open_source(target.base), repo.open_source(target.target)
            exclude = {p for p in session.overrides if start.content_hash(p) == now.content_hash(p)}
        return (repo.open_source(cr.head), repo.open_source(target.target), f"HEAD ({cr.head[:8]})",
                repo._label(target.target), exclude, UNCOMMITTED)
    if not re.fullmatch(r"[0-9a-fA-F]{4,64}", commit):  # SHA-1 or SHA-256 ids, possibly abbreviated
        raise ValueError("commit must be a commit id")
    out = git.try_run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{commit}^{{commit}}")
    sha = out.strip() if out else None
    if sha is None or not _in_range(git, sha, cr):
        raise ValueError(f"commit {commit} is not part of this review")
    parent = git.try_run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{sha}^1^{{commit}}")
    base = repo.open_source(parent.strip()) if parent else repo.open_source("EMPTY")
    commit_src = repo.open_source(sha)
    exclude = set()
    session = cr.session
    if session is not None and session.overrides and target.base.startswith("SESSION"):
        # The commit only recorded an edit that was already there when the session started: not the agent's work.
        start = repo.open_source(target.base)
        exclude = {p for p in session.overrides if start.content_hash(p) == commit_src.content_hash(p)}
    return base, commit_src, f"{sha[:8]}^", sha[:8], exclude, sha


def _commit_item(repo: "Repository", commits: dict[str, Any], sha: str) -> dict[str, Any] | None:
    """The reviewed commit's entry, also for a commit beyond the listing cap."""
    item = next((c for c in commits["items"] if c["sha"] == sha), None)
    if item is None and repo.git is not None and sha != UNCOMMITTED:
        info = repo.git.commit_info(sha)
        if info is not None:
            item = {"sha": sha, "short": sha[:8], "subject": _redact(info.subject[:200]), "author": _redact(info.author),
                    "time": info.date, "parents": [], "files": [], "signals": 0}
    return item


def _in_range(git: Any, sha: str, cr: _CommitRange) -> bool:
    """Whether ``sha`` is one of the commits of the range (also those beyond the listing cap)."""
    if any(c["sha"] == sha for c in (cr.listing or {}).get("commits", [])):
        return True
    if cr.listing is None or not cr.head or cr.head == cr.base:
        return False  # no commits in this review (or its history is unavailable)
    reaches_head = git.try_run("merge-base", "--is-ancestor", sha, cr.head) is not None
    in_base = cr.base is not None and git.try_run("merge-base", "--is-ancestor", sha, cr.base) is not None
    return reaches_head and not in_base


def _attach_commits(repo: "Repository", target: ReviewTarget, cr: _CommitRange, files: list[dict[str, Any]],
                    sub_changes: list[Any], target_src: Any, changed: set[str], session: Any, add: Any,
                    commit: str | None) -> dict[str, Any]:
    """The review's commits (oldest first, then uncommitted work), and which commits touched each file.

    With ``add`` (whole-range reviews), files that commits changed and later changed back are signalled."""
    import datetime as _dt

    from .activity import _count_lines

    items: list[dict[str, Any]] = []
    for c in (cr.listing or {}).get("commits", []):
        items.append({"sha": c["sha"], "short": c["sha"][:8], "subject": _redact(c["subject"][:200]),
                      "author": _redact(c["author"]),
                      "time": _dt.datetime.fromtimestamp(c["time"], _dt.timezone.utc).isoformat(timespec="seconds"),
                      "parents": c["parents"], "files": c["files"]})
    touched: dict[str, list[str]] = {}
    for c in items:
        for f in c["files"]:
            touched.setdefault(f["path"], []).append(c["sha"])
    sub_of = {p: ch for ch in sub_changes for p, _b, _a in ch.files}
    uncommitted: list[dict[str, Any]] = []
    # Reviewing one commit: its files say nothing about the range's uncommitted work, so that entry is left out.
    if cr.uncommitted and cr.head and commit in (None, UNCOMMITTED):
        head_src = repo.open_source(cr.head)
        head_subs = head_src.submodule_commits()
        target_subs = target_src.submodule_commits()
        for entry in files:
            path = entry["path"]
            ch = sub_of.get(path)
            if entry.get("kind") == "submodule":
                dirty = head_subs.get(path) != target_subs.get(path) or bool((entry.get("submodule") or {}).get("dirty"))
                before = after = None
            elif ch is not None:
                dirty = path[len(ch.path) + 1:] in set(ch.dirty)
                before = after = None
            else:
                dirty = head_src.content_hash(path) != target_src.content_hash(path)
                before, after = (head_src.read_bytes(path), target_src.read_bytes(path)) if dirty else (None, None)
            if dirty:
                added, removed = _count_lines(before, after) if (before is not None or after is not None) \
                    else (entry.get("lines_added"), entry.get("lines_removed"))
                status = "A" if before is None and after is not None else "D" if after is None and before is not None \
                    else "M"
                uncommitted.append({"path": path, "status": status, "added": added, "removed": removed})
    for entry in files:
        path = entry["path"]
        ch = sub_of.get(path)
        shas = list(touched.get(path, [])) or (list(touched.get(ch.path, [])) if ch is not None else [])
        if any(u["path"] == path for u in uncommitted):
            shas.append(UNCOMMITTED)
        entry["commits"] = [commit] if commit else shas
    if uncommitted:
        items.append({"sha": UNCOMMITTED, "short": "", "subject": cr.uncommitted, "author": "", "time": None,
                      "parents": [cr.head], "files": uncommitted, "uncommitted": True})
    in_review = {e["path"] for e in files}
    for c in items:
        for f in c["files"]:
            f["in_review"] = f["path"] in in_review
    # Changed by the range's commits, yet identical at both ends: the agent went back and forth.  Git's own tree
    # difference decides (it also sees submodule pointers and symlinks, which file listings leave out).
    if add is not None and cr.base is not None and cr.head and items:
        skip = set(session.overrides) if session is not None else set()
        net = set(repo.git.changed_paths(cr.base, cr.head)) if repo.git is not None else set()
        submodules = set(target_src.submodules or ()) | set(cr.session.submodules if cr.session else ())
        subjects = {c["sha"]: c["subject"] for c in items}
        for path, shas in sorted(touched.items()):
            if path in net or path in changed or path in skip or path in submodules \
                    or any(path.startswith(p + "/") for p in submodules):
                continue
            listed = "; ".join(f"{sha[:8]} {subjects[sha][:60]}" for sha in shas[:3])
            add(Finding("reverted-within-wave", "hygiene", "info", "Changed, then changed back",
                        f"{path} was changed by {len(shas)} commit(s) ({listed}) but ends up as it started.", path,
                        suggestion="Check the back-and-forth was intended (for example an experiment the agent "
                                   "undid)."), key=path)
    listing = cr.listing or {}
    return {"items": items, "total": int(listing.get("total", 0)), "shown": len(listing.get("commits", [])),
            "merges": int(listing.get("merges", 0)), "note": cr.note,
            "base": cr.base, "head": cr.head}


def _python_params(source: Any, path: str | None, name: str, line: int | None) -> list[str] | None:
    """Parameter names (with ``*``/``**`` markers) of the Python function ``name`` nearest to ``line``."""
    if source is None or not path or not path.endswith((".py", ".pyi")):
        return None
    text = source.read_text(path)
    if not text:
        return None
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    defs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if not defs:
        return None
    node = min(defs, key=lambda n: abs(n.lineno - (line or n.lineno)))
    a = node.args
    names = [x.arg for x in a.posonlyargs + a.args]
    names += [f"*{a.vararg.arg}"] if a.vararg else []
    names += [x.arg for x in a.kwonlyargs] + ([f"**{a.kwarg.arg}"] if a.kwarg else [])
    return names


def _parameter_change(base_src: Any, target_src: Any, ch: Any) -> str | None:
    """'parameters removed: limit; added: page_size' for a modified Python function, if it can be computed."""
    before = _python_params(base_src, ch.node.path, ch.node.name, ch.node.start_line)
    after = _python_params(target_src, ch.node.path, ch.node.name, ch.node.start_line)
    if before is None or after is None:
        return None
    removed = [p for p in before if p not in after]
    added = [p for p in after if p not in before]
    parts = ([f"parameters removed: {', '.join(removed)}"] if removed else []) + \
            ([f"added: {', '.join(added)}"] if added else [])
    if not parts and before != after:
        parts = ["parameters reordered"]
    return "; ".join(parts) or None


def _first_match(path: str, patterns: list[str]) -> str:
    return next((p for p in patterns if globs.match(path, p)), patterns[0] if patterns else "")


def _sensitive_kind(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if name.startswith(".env") and name != ".env.example":
        return "environment/secrets"
    mk = classify.manifest_kind(path)
    if mk is not None and mk.lockfile:
        return "lock file"
    if classify.ci_provider(path):
        return "CI pipeline"
    if classify.container_kind(path) in ("dockerfile", "compose"):
        return "container"
    if classify.deployment_kind(path):
        return "deployment"
    if _SENSITIVE_DIRS.search(path) and "migration" in path.lower():
        return "database migration"
    return None


def _line_findings(add: Any, path: str, component: str | None, is_test: bool, added: list[tuple[int, str]],
                   removed: list[tuple[int, str]], lang: str | None, symbols: list[Any]) -> None:
    def symbol_name(line: int) -> str | None:
        s = _symbol_at(symbols, line)
        return s.qualified_name if s else None

    # Heuristics look at the start of each line only: minified or generated lines can
    # be megabytes long, and backtracking patterns must stay linear on them.
    added = [(n, t[:MAX_CHECKED_LINE]) for n, t in added]
    removed = [(n, t[:MAX_CHECKED_LINE]) for n, t in removed]
    for kind, category, severity, title, pattern, where in _LINE_CHECKS:
        if (where == "test" and not is_test) or (where == "code" and is_test):
            continue
        hits = [(n, t) for n, t in added if pattern.search(t)]
        for n, t in hits[:5]:
            add(Finding(kind, category, severity, title, t.strip()[:200], path, n, _redact(t.strip())[:300],
                        symbol_name(n), component), key=t.strip())
        if len(hits) > 5:
            add(Finding(kind, category, severity, f"{title} (+{len(hits) - 5} more)", f"{len(hits)} occurrences",
                        path, hits[5][0], component=component), key=f"more:{len(hits)}")
    for n, t in added:
        if _secret_in(t):
            add(Finding("secret", "security", "high", "Possible hard-coded secret",
                        "A credential-like value was added; move it to configuration or a secret store.", path, n,
                        _redact(t.strip())[:300], symbol_name(n), component), key=_redact(t.strip()))
    # Swallowed exceptions: an except/catch immediately followed by pass/empty body.
    for i, (n, t) in enumerate(added):
        stripped = t.strip()
        nxt = added[i + 1][1].strip() if i + 1 < len(added) and added[i + 1][0] == n + 1 else ""
        if (re.match(r"except\b.*:\s*$", stripped) and nxt in ("pass", "...", "continue")) or \
                re.match(r"except\b.*:\s*(pass|\.\.\.)\s*$", stripped) or \
                re.search(r"catch\s*(\([^)]*\))?\s*\{\s*\}", stripped) or \
                (re.search(r"catch\s*(\([^)]*\))?\s*\{\s*$", stripped) and nxt == "}"):
            add(Finding("swallowed-exception", "correctness", "medium", "Exception silently swallowed",
                        "Errors are caught and ignored; failures will go unnoticed.", path, n, stripped[:300],
                        symbol_name(n), component), key=stripped)
    # Commented-out code blocks.
    run: list[int] = []
    for n, t in added + [(-10, "")]:
        if _COMMENTED_CODE.match(t) and (not run or n == run[-1] + 1):
            run.append(n)
            continue
        if len(run) >= 3:
            add(Finding("commented-code", "hygiene", "low", "Commented-out code added",
                        f"{len(run)} consecutive commented-out code lines.", path, run[0], component=component),
                key=f"{run[0]}:{len(run)}")
        run = [n] if _COMMENTED_CODE.match(t) else []
    if is_test:
        rem = sum(1 for _, t in removed if _ASSERT.search(t))
        add_n = sum(1 for _, t in added if _ASSERT.search(t))
        if rem > add_n:
            add(Finding("assertions-removed", "tests", "medium", "Assertions removed from tests",
                        f"{rem} assertion line(s) removed, {add_n} added.", path, component=component,
                        suggestion="Make sure tests were not weakened to make them pass."), key=f"{rem}:{add_n}")
    else:
        rem = sum(1 for _, t in removed if _ERROR_HANDLING.match(t))
        add_n = sum(1 for _, t in added if _ERROR_HANDLING.match(t))
        if rem >= 2 and rem > add_n:
            add(Finding("error-handling-removed", "correctness", "low", "Error handling removed",
                        f"{rem} raise/except/throw/catch line(s) removed, {add_n} added.", path, component=component),
                key=f"{rem}:{add_n}")


def _graph_findings(add: Any, diff: RepositoryDiff, base: RepositorySnapshot, target: RepositorySnapshot,
                    target_src: TreeSource, scope: ScopePolicy, changed: set[str], path_to_node: dict[str, Any],
                    component_of: Any, base_src: Any = None) -> None:
    nodes = diff.nodes
    b_index = base.node_index()
    external_uses = {(b_index[e.source_id].metadata.get("component_id"), e.target_id)
                     for e in base.dependency_edges if e.source_id in b_index and e.target_id in b_index
                     and "external" in b_index[e.target_id].tags}
    name = lambda i: nodes[i].node.qualified_name if i in nodes else i  # noqa: E731

    def where(nid: str) -> tuple[str | None, int | None]:
        n = nodes.get(nid)
        return (n.node.path, n.node.start_line) if n else (None, None)

    # New analysis problems (syntax errors, broken imports, undeclared dependencies).
    before = {(d.code, d.path, d.message) for d in base.diagnostics}
    labels = {"parse-error": ("correctness", "high", "Syntax error"),
              "unresolved-internal-import": ("correctness", "high", "Broken import"),
              "unresolved-import": ("correctness", "high", "Broken import"),
              "unresolved-relative-import": ("correctness", "high", "Broken import"),
              "undeclared-dependency": ("architecture", "medium", "Undeclared dependency")}
    for d in target.diagnostics:
        if d.code in labels and (d.code, d.path, d.message) not in before:
            cat, sev, title = labels[d.code]
            add(Finding(d.code, cat, sev, title, d.message, d.path, d.line,
                        component=component_of(d.path)[1] if d.path else None), key=d.message)
    # Cycles.
    for c in diff.introduced_cycles:
        path, line = where(c.members[0])
        add(Finding("new-cycle", "architecture", "high", f"New {c.level}-level dependency cycle",
                    " → ".join(name(m) for m in (c.example_path or c.members)), path, line,
                    component=component_of(path)[1] if path else None,
                    suggestion="Break the cycle, e.g. by moving the shared code or inverting the dependency."),
            key=",".join(sorted(name(m) for m in c.members)))
    for c in diff.changed_cycles:
        if c["added_members"]:
            path, line = where(c["added_members"][0])
            add(Finding("cycle-grown", "architecture", "high", f"Dependency cycle grew ({c['level']} level)",
                        "New members: " + ", ".join(name(m) for m in c["added_members"]), path, line),
                key=",".join(sorted(name(m) for m in c["members"])))
    # New dependencies: between components, forbidden by rules, third-party.
    for dep in diff.new_dependencies:
        ch = diff.edges.get(dep["edge_id"])
        if ch is None:
            continue
        e = ch.edge
        src, dst = nodes.get(e.source_id), nodes.get(e.target_id)
        src_path = src.node.path if src else None
        ev = (e.evidence or [None])[0]
        loc_path, loc_line = (ev.path, ev.start_line) if ev else (src_path, None)
        if dep["level"] == "component":
            add(Finding("new-component-dependency", "architecture", "medium", "New dependency between components",
                        f"{dep['source']} → {dep['target']}", loc_path, loc_line,
                        ev.excerpt if ev else None, component=dep["source"],
                        suggestion="Confirm this coupling is intended."), key=f"{dep['source']}->{dep['target']}")
        elif dep["level"] == "module" and src_path and dst is not None and dst.node.path and \
                src_path.rsplit("/", 1)[0] != dst.node.path.rsplit("/", 1)[0] and e.relationship == REL_IMPORTS \
                and not e.metadata.get("test_only"):
            src_pkg, dst_pkg = src_path.rsplit("/", 1)[0], dst.node.path.rsplit("/", 1)[0]
            if not _package_dependency_exists(base, src_pkg, dst_pkg):
                add(Finding("new-package-dependency", "architecture", "low", "New dependency between packages",
                            f"{src_pkg} now depends on {dst_pkg} ({dep['source']} → {dep['target']})", loc_path,
                            loc_line, ev.excerpt if ev else None, component=component_of(src_path)[1],
                            suggestion="Check the layering: should this package know about that one?"),
                    key=f"{src_pkg}->{dst_pkg}")
        elif dep["level"] == "external" and not dep["stdlib"]:
            # Only news: a package the repository never used, or one this component never used.
            comp_id = src.node.metadata.get("component_id") if src else None
            if (comp_id, e.target_id) not in external_uses:
                new_to_repo = dst is None or dst.status == ADDED
                add(Finding("new-external-dependency", "architecture", "low", "New third-party dependency",
                            f"{dep['source']} now uses {dep['target']}"
                            + ("" if new_to_repo else f" (already used elsewhere in the repository, new to "
                                                      f"{component_of(loc_path)[1] or 'this component'})"),
                            loc_path, loc_line, ev.excerpt if ev else None,
                            component=component_of(loc_path)[1] if loc_path else None),
                    key=f"{dep['source']}->{dep['target']}")
    # Calls to removed symbols that are still present in the (unchanged or modified) caller.
    t_lines: dict[str, list[str]] = {}
    # A caller that now calls a *different* symbol of the same name was redirected (e.g. a renamed base
    # class reached through super().__init__), not left dangling.
    current_calls = {(c.edge.source_id, nodes[c.edge.target_id].node.name) for c in diff.edges.values()
                     if c.edge.relationship == REL_CALLS and c.status != REMOVED and c.edge.target_id in nodes}
    for ch in diff.edges.values():
        e = ch.edge
        if e.relationship != REL_CALLS or ch.status != REMOVED:
            continue
        callee, caller = nodes.get(e.target_id), nodes.get(e.source_id)
        if callee is None or caller is None or callee.status != REMOVED or caller.status == REMOVED:
            continue
        if (caller.node.id, callee.node.name) in current_calls:
            continue
        path = caller.node.path
        if path is None:
            continue
        if path not in t_lines:
            text = target_src.read_text(path)
            t_lines[path] = text.splitlines() if text else []
        lines = t_lines[path]
        start = caller.node.start_line or 1
        end = caller.node.end_line or len(lines)
        pattern = re.compile(r"\b" + re.escape(callee.node.name) + r"\s*\(")
        hit = next((i + 1 for i in range(start - 1, min(end, len(lines))) if pattern.search(lines[i])), None)
        if hit is not None:
            add(Finding("dangling-call", "correctness", "high", "Call to a removed function",
                        f"{caller.node.qualified_name} still calls {callee.node.qualified_name}, which was removed.",
                        path, hit, lines[hit - 1].strip()[:300], caller.node.qualified_name,
                        component_of(path)[1], "Update the caller or restore the function."),
                key=f"{caller.node.qualified_name}->{callee.node.qualified_name}")
    # Signature changes whose callers live in files this change did not touch.
    callers: dict[str, list[str]] = {}
    for e in target.call_edges:
        callers.setdefault(e.target_id, []).append(e.source_id)
    t_idx = target.node_index()
    for nid, ch in nodes.items():
        if ch.node.category != CATEGORY_SYMBOL or ch.status != MODIFIED or "signature changed" not in ch.reasons:
            continue
        stale = sorted({t_idx[c].qualified_name for c in callers.get(nid, [])
                        if c in t_idx and t_idx[c].path not in changed and t_idx[c].path != ch.node.path})
        if stale:
            change = _parameter_change(base_src, target_src, ch) or (
                f"{ch.before.get('signature') or ''} → {ch.node.metadata.get('signature') or ''}")
            add(Finding("stale-callers", "correctness", "medium", "Signature changed; callers not updated",
                        f"{ch.node.qualified_name}: {change}; called from unchanged code: "
                        + ", ".join(stale[:6]) + (f" (+{len(stale) - 6})" if len(stale) > 6 else ""),
                        ch.node.path, ch.node.start_line, symbol=ch.node.qualified_name,
                        component=component_of(ch.node.path)[1] if ch.node.path else None,
                        suggestion="Check every caller still passes the right arguments."), key=ch.node.qualified_name)
    # Public API removed.
    for sid in symbol_changes(diff)[REMOVED]:
        n = nodes[sid].node
        if n.metadata.get("public", n.metadata.get("exported", False)) and n.component_type in ("class", "function",
                                                                                                "method"):
            add(Finding("public-api-removed", "architecture", "medium", "Public symbol removed", n.qualified_name,
                        n.path, n.start_line, symbol=n.qualified_name,
                        component=component_of(n.path)[1] if n.path else None), key=n.qualified_name)


def _history_rev(repo: "Repository", target: ReviewTarget) -> str | None:
    """The commit whose history describes the code *before* the reviewed change."""
    git = repo.git
    if git is None:
        return None
    base = target.base
    if base.startswith("SESSION"):
        sid = target.session_id or (base.split("@", 1)[1] if "@" in base else None)
        session = repo.state.load_session(sid) if sid else repo.current_session()
        return (session.baseline_head if session else None) or git.head()
    if base.startswith("merge-base:"):
        return _merge_base_of(repo, base) or git.head()
    if base in ("EMPTY", "INDEX", "WORKTREE"):
        return git.head()
    out = git.try_run("rev-parse", "--verify", "--quiet", "--end-of-options", f"{base}^{{commit}}")
    return out.strip() if out else git.head()


def _coupling_for(repo: "Repository", target: ReviewTarget) -> Any:
    if repo.git is None or repo.config.history_commits <= 0:
        return None
    try:
        return repo.coupling(_history_rev(repo, target))
    except Exception:  # history is a bonus: never fail a review because of it
        return None


def _coupling_findings(add: Any, files: list[dict[str, Any]], coupling: Any, changed: set[str],
                       target_src: TreeSource) -> None:
    """Files that usually change together with a changed file but were left untouched ("missed companion")."""
    for entry in files:
        path = entry["path"]
        if entry.get("kind") == "submodule" or entry["status"] != MODIFIED:
            continue
        partners = coupling.of(path)
        if not partners:
            continue
        entry["usually_changes_with"] = [{**p.to_dict(), "changed": p.path in changed} for p in partners]
        missed = [p for p in partners if p.path not in changed and target_src.content_hash(p.path) is not None
                  and not skip_companion(p.path, path)]
        if not missed:
            continue
        first = missed[0]
        others = ", ".join(f"{p.path} ({p.shared} of {p.revs})" for p in missed[1:3])
        severity = "medium" if first.degree >= 0.8 and first.shared >= 8 else "low"
        add(Finding("missed-companion", "correctness", severity, "Usual companion change missing",
                    f"{path} changed together with {first.path} in {first.shared} of its last {first.revs} commits"
                    + (f"; also often with {others}" if others else "")
                    + f"; {'that file is' if len(missed) == 1 else 'those files are'} untouched in this change.",
                    path, component=entry.get("component"),
                    suggestion=f"Check whether {first.path} needs the matching change (a migration, test, client "
                               "or configuration update, for example)."), key=path)


MAX_STALE_FILES = 60
MAX_RENAME_CHECKS = 200  # renamed symbols checked for leftover references per review


_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')
_TRIPLE = re.compile(r'"""|\'\'\'')


def _code_only(path: str, lines: list[str]) -> list[str]:
    """The lines with comments, string literals and (Python) docstrings blanked, for searches by name."""
    py = path.endswith((".py", ".pyi"))
    out: list[str] = []
    in_triple: str | None = None
    for text in lines:
        code = ""
        while text:
            if in_triple:
                end = text.find(in_triple)
                if end < 0:
                    text = ""
                    break
                text, in_triple = text[end + 3:], None
                continue
            m = _TRIPLE.search(text) if py else None
            if m is None:
                code += text
                break
            code += text[:m.start()] + '""'
            in_triple, text = m.group(0), text[m.end():]
        code = _STRING.sub('""', code)
        out.append(code.split("#" if py else "//", 1)[0])
    return out


def _rename_findings(add: Any, diff: RepositoryDiff, target: RepositorySnapshot, target_src: TreeSource,
                     component_of: Any) -> None:
    """Renamed symbols: fine when every reference was updated, a problem when some code still uses the old name.

    *high* when a call the base resolved to the old symbol is still written with the old name; *medium* when
    the old name only appears as a word in the module or its importers (comments, strings, docstrings and
    ``obj.name`` attribute accesses are ignored).  Bounded: at most :data:`MAX_RENAME_CHECKS` renames are
    checked and :data:`MAX_STALE_FILES` files read, each once."""
    renames = [r for r in diff.renames if r["kind"] == "symbol" and r["renamed"] and r["new_id"] in diff.nodes]
    if not renames:
        return
    t_idx = target.node_index()
    new_of = {r["old_id"]: r["new_id"] for r in diff.renames}
    checked, unchecked = renames[:MAX_RENAME_CHECKS], renames[MAX_RENAME_CHECKS:]
    wanted = {r["new_id"] for r in checked} | {r["old_id"] for r in checked}
    removed_calls: dict[str, list[str]] = {}  # renamed symbol (new id) -> callers whose call was not resolved anymore
    for ec in diff.edges.values():
        e = ec.edge
        if ec.status == REMOVED and e.relationship == REL_CALLS and e.target_id in wanted:
            removed_calls.setdefault(new_of.get(e.target_id, e.target_id), []).append(e.source_id)
    importers: dict[str, set[str]] = {}
    for e in target.dependency_edges:
        if e.relationship == REL_IMPORTS and e.source_id in t_idx:
            importers.setdefault(e.target_id, set()).add(e.source_id)
    module_of_path = {n.path: n for n in target.modules if n.path}
    code_of: dict[str, list[str]] = {}

    def code(path: str) -> list[str]:
        if path not in code_of:
            code_of[path] = _code_only(path, (target_src.read_text(path) or "").splitlines())
        return code_of[path]

    stale: dict[str, dict[tuple[str, int], str]] = {r["new_id"]: {} for r in checked}
    evidence: set[str] = set()
    by_name: dict[str, list[tuple[dict[str, Any], Any]]] = {}  # old short name -> (rename, node)
    files: dict[str, set[str]] = {}  # file to search -> old names to look for
    searched: dict[str, set[str]] = {}  # rename (new id) -> files where its old name counts
    for rn in checked:
        node = diff.nodes[rn["new_id"]].node
        old = diff.nodes[rn["new_id"]].before.get("name") or rn["old_name"].rsplit(".", 1)[-1]
        # Calls the base resolved to the old symbol that the target no longer resolves (the caller kept the name).
        call = re.compile(r"(?<![\w$])" + re.escape(old) + r"\s*\(")
        for source_id in removed_calls.get(rn["new_id"], ()):
            caller = t_idx.get(source_id) or t_idx.get(new_of.get(source_id, ""))
            if caller is None or not caller.path:
                continue
            src = code(caller.path)
            start, end = caller.start_line or 1, caller.end_line or len(src)
            hit = next((i + 1 for i in range(start - 1, min(end, len(src))) if call.search(src[i])), None)
            if hit is not None:
                stale[rn["new_id"]][(caller.path, hit)] = caller.qualified_name
                evidence.add(rn["new_id"])
        # Other references by name (imports, uses as a value) in the module and the modules that import it.
        if node.component_type == "method":  # methods are called through objects: too ambiguous by name
            continue
        by_name.setdefault(old, []).append((rn, node))
        module = module_of_path.get(node.path or "")
        for path in [node.path] + sorted({t_idx[u].path for u in importers.get(module.id, ()) if t_idx[u].path}
                                         if module is not None else []):
            if path and (path in files or len(files) < MAX_STALE_FILES):
                files.setdefault(path, set()).add(old)
                searched.setdefault(rn["new_id"], set()).add(path)
    for path, names in files.items():
        word = re.compile(r"(?<![\w$.])(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
                          + r")(?![\w$])")
        raw = None
        for i, text in enumerate(code(path), 1):
            for name in {m.group(1) for m in word.finditer(text)}:
                for rn, node in by_name.get(name, ()):
                    if path not in searched.get(rn["new_id"], ()):
                        continue  # another symbol with the same old name, in a module this file does not use
                    if node.path == path and (node.start_line or 0) <= i <= (node.end_line or 0):
                        continue  # its own definition
                    raw = raw if raw is not None else (target_src.read_text(path) or "").splitlines()
                    stale[rn["new_id"]].setdefault((path, i), raw[i - 1].strip()[:80] if i <= len(raw) else "")
    for rn in renames:
        node = diff.nodes[rn["new_id"]].node
        old = diff.nodes[rn["new_id"]].before.get("name") or rn["old_name"].rsplit(".", 1)[-1]
        comp = component_of(node.path)[1] if node.path else None
        hits = sorted(stale.get(rn["new_id"], {}).items())
        if hits:
            where = ", ".join(f"{p}:{ln}" for (p, ln), _ in hits[:6]) + (f" (+{len(hits) - 6})" if len(hits) > 6 else "")
            sure = rn["new_id"] in evidence
            (path, line), excerpt = hits[0]
            add(Finding("renamed-symbol-stale-references", "correctness", "high" if sure else "medium",
                        "Renamed, but the old name is still used" if sure else "Renamed, but the old name may still be used",
                        f"{rn['old_name']} was renamed to {node.qualified_name}, but `{old}` is still "
                        f"{'called' if sure else 'written'} at {where}.", path, line, _redact(excerpt),
                        node.qualified_name, comp,
                        f"Update these references to `{node.name}` (or keep `{old}` as an alias)."), key=rn["new_id"])
        else:
            capped = rn in unchecked
            add(Finding("renamed-symbol", "architecture", "low", "Symbol renamed",
                        f"{rn['old_name']} → {node.qualified_name}; "
                        + (f"references were not checked (more than {MAX_RENAME_CHECKS} renames in this review)."
                           if capped else "no reference to the old name is left."),
                        node.path, node.start_line, symbol=node.qualified_name, component=comp), key=rn["new_id"])


_REGISTER_CALL = {"APIRouter": "app.include_router({var})", "Blueprint": "app.register_blueprint({var})",
                  "Router": "app.use('/path', {var})"}


def _wiring_findings(add: Any, diff: RepositoryDiff, target: RepositorySnapshot, target_src: TreeSource,
                     component_of: Any, ignore: list[str]) -> None:
    """New code nothing uses: modules nobody imports, routers never registered, functions never called."""
    for u in unwired_code(diff, target, target_src, ignore=ignore):
        comp = component_of(u.path)[1]
        if u.kind == "unwired-module" and u.router:
            stem = u.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            call = _REGISTER_CALL.get(u.router_kind or "", "app.include_router({var})").format(
                var=stem if u.router_kind == "Router" else f"{stem}.{u.router}")
            detail = (f"{u.path} defines {u.router_kind} `{u.router}`, but nothing registers it, so its routes are "
                      "never served." if not u.importers else
                      f"{u.path} defines {u.router_kind} `{u.router}`; {', '.join(u.importers[:3])} "
                      f"{'imports' if len(u.importers) == 1 else 'import'} it but never register"
                      f"{'s' if len(u.importers) == 1 else ''} it, so its routes are never served.")
            where = f" in `{u.register_in}`" if u.register_in else ""
            add(Finding("unwired-module", "correctness", "medium", "New router is never registered", detail, u.path,
                        component=comp, suggestion=f"Register it in the application (e.g. `{call}`{where})."),
                key=u.name)
        elif u.kind == "unwired-module":
            add(Finding("unwired-module", "correctness", "medium" if u.application else "low",
                        "New module is not wired in",
                        f"Nothing imports {u.path}; it is not an entry point and no code or configuration refers "
                        "to it." + ("" if u.application else " (Expected if it is new public API of a library; "
                                                             "then it deserves tests.)"), u.path, component=comp,
                        suggestion="Import it where it is needed or remove it. If a framework loads it by "
                                   "convention, add it to `review.wiring_ignore`."), key=u.name)
        elif u.kind == "unreachable-from-entry":
            tests_only = bool(u.importers) and all(classify.is_test_path(p) for p in u.importers)
            users = ", ".join(u.importers[:3]) + (f" (+{len(u.importers) - 3})" if len(u.importers) > 3 else "")
            add(Finding("unreachable-from-entry", "correctness", "info",
                        "New module only used by tests" if tests_only else "New module not reachable from the "
                        "application",
                        f"{u.path} is imported only by {users}, which no entry point or existing code reaches.",
                        u.path, component=comp,
                        suggestion="Wire the new code into the application, or confirm it is meant to stay "
                                   "unused for now."), key=u.name)
        else:
            add(Finding("unwired-symbol", "hygiene", "low", "New code is never used",
                        f"{u.name} is not called or referenced anywhere (its module, or the modules that import it).",
                        u.path, u.line, symbol=u.name, component=comp,
                        suggestion="Call it where it is needed or remove it."), key=u.name)


def _package_dependency_exists(snapshot: RepositorySnapshot, src_pkg: str, dst_pkg: str) -> bool:
    """Whether any module of ``src_pkg`` already imported a module of ``dst_pkg`` in ``snapshot``."""
    cache = snapshot.__dict__.setdefault("_package_pairs", None)
    if cache is None:
        idx = snapshot.node_index()
        cache = set()
        for e in snapshot.dependency_edges:
            if e.relationship != REL_IMPORTS or not e.direct:
                continue
            a, b = idx.get(e.source_id), idx.get(e.target_id)
            if a is not None and b is not None and a.path and b.path:
                cache.add((a.path.rsplit("/", 1)[0], b.path.rsplit("/", 1)[0]))
        snapshot.__dict__["_package_pairs"] = cache
    return (src_pkg, dst_pkg) in cache


def _contract_findings(add: Any, baseline_path: str, contracts: list[Any], base: RepositorySnapshot,
                       target: RepositorySnapshot, base_src: TreeSource, target_src: TreeSource,
                       component_of: Any) -> None:
    """Architecture contracts: violations this change introduces (not in the base, not in the baseline), the ones
    it fixes, and edits to the known-violations baseline itself."""
    known, problem = load_baseline(target_src.read_text(baseline_path) if baseline_path else None)
    before = [v for r in check_contracts(base, contracts) for v in r.violations]
    after = [v for r in check_contracts(target, contracts) for v in r.violations]
    before_keys, after_keys = {v.key for v in before}, {v.key for v in after}
    for v in after:
        if v.key in before_keys or v.key in known:
            continue
        chain = " → ".join(v.chain) if len(v.chain) > 2 else None
        add(Finding("contract-broken", "architecture", v.severity, f"Contract broken: {v.contract}", v.detail,
                    v.path, v.line, v.excerpt, component=component_of(v.path)[1] if v.path else None,
                    suggestion=("Remove the import chain " + chain if chain else "Remove this import") +
                    ", or record the exception with an `ignore` entry on the contract."), key=v.key)
    for v in before:
        if v.key not in after_keys:
            add(Finding("contract-fixed", "architecture", "info", f"Contract violation fixed: {v.contract}",
                        f"{v.source} no longer breaks it ({v.target}).", v.path if v.path and target_src.content_hash(v.path)
                        else None, component=component_of(v.path)[1] if v.path else None), key=v.key)
    if baseline_path and base_src.content_hash(baseline_path) != target_src.content_hash(baseline_path):
        accepted = known - load_baseline(base_src.read_text(baseline_path))[0]
        add(Finding("contract-baseline-changed", "architecture", "medium" if accepted else "info",
                    "Known-violations baseline changed",
                    f"{baseline_path} now accepts {len(accepted)} more violation(s)"
                    + (f": {', '.join(sorted(accepted)[:3])}{', …' if len(accepted) > 3 else ''}" if accepted else "")
                    + ". Violations in the baseline are no longer reported.", baseline_path,
                    suggestion="Check that each newly accepted violation was agreed on, not hidden."), key="baseline")
    if problem:
        add(Finding("contract-baseline-invalid", "architecture", "low", "Known-violations baseline ignored",
                    f"{baseline_path}: {problem}. Every violation is reported.", baseline_path), key="baseline-invalid")


def _test_coverage_findings(add: Any, files: list[dict[str, Any]], impact: _ImpactIndex) -> None:
    changed_tests = {f["path"] for f in files if f["is_test"]}
    for f in files:
        if f["is_test"] or f["status"] == REMOVED or not f["symbols"] or f["language"] is None:
            continue
        if not any(s["status"] in (ADDED, MODIFIED) for s in f["symbols"]):
            continue
        tests = set(f.get("tests_affected") or [])
        if not tests:
            add(Finding("untested-change", "tests", "medium", "No test covers this change",
                        f"No test imports {f['path']} (directly or indirectly).", f["path"],
                        component=f["component"], suggestion="Add or extend tests for the changed behaviour."),
                key="untested")
        elif not tests & changed_tests and any(s["status"] == ADDED and s["public"] for s in f["symbols"]):
            add(Finding("tests-not-updated", "tests", "low", "New public code, tests unchanged",
                        f"{len(tests)} test file(s) cover this module but none was updated.", f["path"],
                        component=f["component"]), key="tests-not-updated")


def _component_summary(files: list[dict[str, Any]], findings: list[Finding], nodes: dict[str, Any],
                       t_idx: dict[str, Any], b_idx: dict[str, Any]) -> list[dict[str, Any]]:
    comps: dict[str, dict[str, Any]] = {}
    for f in files:
        cid = f["component_id"] or "root"
        c = comps.setdefault(cid, {"id": cid, "name": f["component"] or "(repository root)", "files": 0,
                                   "lines_added": 0, "lines_removed": 0, "symbols_changed": 0, "tests_changed": 0,
                                   "scope": {}, "findings": {s: 0 for s in SEVERITY_ORDER}, "paths": []})
        c["files"] += 1
        c["lines_added"] += f.get("lines_added") or 0
        c["lines_removed"] += f.get("lines_removed") or 0
        c["symbols_changed"] += len(f["symbols"])
        c["tests_changed"] += int(f["is_test"])
        c["scope"][f["scope"]] = c["scope"].get(f["scope"], 0) + 1
        c["paths"].append(f["path"])
    comp_of_path = {p: cid for cid, c in comps.items() for p in c["paths"]}
    for f in findings:
        cid = comp_of_path.get(f.path) if f.path else None
        if cid is not None:
            comps[cid]["findings"][f.severity] += 1
    for cid, c in comps.items():
        ch = nodes.get(cid)
        node = t_idx.get(cid) or b_idx.get(cid)
        c["status"] = ch.status if ch is not None else MODIFIED
        c["type"] = node.component_type if node else "component"
        c["path"] = node.path if node else ""
    return sorted(comps.values(), key=lambda c: (-(c["lines_added"] + c["lines_removed"]), c["name"]))


def _component_edges(diff: RepositoryDiff, touched: set[str]) -> list[dict[str, Any]]:
    out = []
    for ch in diff.edges.values():
        e = ch.edge
        if e.direct or e.relationship not in (REL_IMPORTS, REL_DEPENDS_ON):
            continue
        if ch.status == UNCHANGED and not (e.source_id in touched and e.target_id in touched):
            continue
        if e.source_id not in touched and e.target_id not in touched:
            continue
        out.append({"source": e.source_id, "target": e.target_id, "status": ch.status, "count": e.occurrences,
                    "relationship": e.relationship, "in_cycle": ch.in_target_cycle,
                    "new_cycle": ch.in_target_cycle and not ch.in_base_cycle,
                    "source_name": diff.nodes[e.source_id].node.qualified_name if e.source_id in diff.nodes else e.source_id,
                    "target_name": diff.nodes[e.target_id].node.qualified_name if e.target_id in diff.nodes else e.target_id})
    return out


# --------------------------------------------------------------------------- feedback


def feedback_markdown(report: dict[str, Any], notes: list[dict[str, Any]], *, include_findings: bool = True,
                      min_severity: str = "medium") -> str:
    """Turn reviewer notes (and optionally untriaged findings) into a prompt for the coding agent."""
    findings = {f["id"]: f for f in report.get("findings", [])}
    triaged = {n.get("finding_id") for n in notes if n.get("finding_id")}
    # A "should not touch" note on a file covers that file's automated scope signals.
    reverted = {n.get("path") for n in notes if n.get("verdict") == "should-not-touch" and n.get("path")}
    triaged |= {f["id"] for f in report.get("findings", []) if f.get("category") == "scope" and f.get("path") in reverted}
    scope = report.get("scope") or {}
    target = report.get("target") or {}
    lines = [f"# Review feedback: {target.get('label', 'changes')}", ""]
    lines.append(f"Compared `{report.get('base', {}).get('label', '')}` with `{report.get('head', {}).get('label', '')}`.")
    if scope.get("allowed") or scope.get("protected"):
        lines.append("")
        if scope.get("allowed"):
            lines.append("Allowed scope: " + ", ".join(f"`{p}`" for p in scope["allowed"]))
        if scope.get("protected"):
            lines.append("Do not modify: " + ", ".join(f"`{p}`" for p in scope["protected"]))
    sections = [("should-not-touch", "Revert: changes that should not have been made"),
                ("logic-error", "Fix: logic errors"), ("missed", "Complete: missed or incomplete work"),
                ("improve", "Improve"), ("question", "Answer these questions")]
    n = 0
    for verdict, title in sections:
        group = [x for x in notes if x.get("verdict") == verdict]
        if not group:
            continue
        lines += ["", f"## {title}", ""]
        for note in group:
            n += 1
            lines += _note_lines(n, note, findings.get(note.get("finding_id") or ""))
    if include_findings:
        limit = SEVERITY_ORDER.get(min_severity, 1)
        rest = [f for f in report.get("findings", []) if f["id"] not in triaged
                and SEVERITY_ORDER.get(f["severity"], 9) <= limit]
        dismissed = {x.get("finding_id") for x in notes if x.get("verdict") == "ok"}
        rest = [f for f in rest if f["id"] not in dismissed]
        if rest:
            lines += ["", "## Automated review signals (verify each; fix or explain)", ""]
            for f in rest:
                n += 1
                loc = _loc(f.get("path"), f.get("line"))
                lines.append(f"{n}. [{f['severity']}] {f['title']}{' — ' + loc if loc else ''}: {f.get('detail', '')}"
                             + (f" Suggestion: {f['suggestion']}" if f.get("suggestion") else ""))
                if f.get("excerpt"):
                    lines += ["   ```", "   " + f["excerpt"], "   ```"]
    risky = [t for t in (report.get("risk") or {}).get("top", []) if t["level"] in ("high", "medium")]
    if include_findings and risky:
        lines += ["", "## Riskiest files (double-check them)", ""]
        lines += [f"- `{t['path']}`: {t['level']} risk ({t['score']}/100): {'; '.join(t['factors'])}" for t in risky]
    if n == 0:
        lines += ["", "No issues to report."]
    lines += ["", "Please address every numbered item, stay within the allowed scope, and reply with one line per "
              "item describing what you changed (or why no change was needed)."]
    return "\n".join(lines) + "\n"


def _loc(path: str | None, line: int | None) -> str:
    if not path:
        return ""
    return f"`{path}:{line}`" if line else f"`{path}`"


def _note_lines(n: int, note: dict[str, Any], finding: dict[str, Any] | None) -> list[str]:
    path = note.get("path") or (finding or {}).get("path")
    line = note.get("line") or (finding or {}).get("line")
    where = _loc(path, line)
    symbol = note.get("symbol") or (finding or {}).get("symbol")
    head = f"{n}. {where}" + (f" (`{symbol}`)" if symbol else "")
    comment = (note.get("comment") or "").strip()
    out = [f"{head} — {comment or VERDICTS.get(note.get('verdict', ''), '')}".rstrip()]
    if finding and not comment.startswith(finding["title"]):
        out.append(f"   Related signal: {finding['title']}: {finding.get('detail', '')}")
    excerpt = note.get("excerpt") or (finding or {}).get("excerpt")
    if excerpt:
        out += ["   ```", *("   " + l for l in str(excerpt).splitlines()[:8]), "   ```"]
    return out


def _commit_title(c: dict[str, Any]) -> str:
    return c["subject"] if c.get("uncommitted") else f"{c['short']} {c['subject']}"


def format_review_text(report: dict[str, Any], by_commit: bool = False) -> str:
    s = report["summary"]
    t = report["target"]
    one = report.get("commit")
    out = [f"Review: {t['label']}" + (f" · commit {_commit_title(one)}" if one else ""),
           f"  {report['base']['label']} → {report['head']['label']}",
           f"  {s['files']} file(s) in {s['components']} component(s), +{s['lines_added']} −{s['lines_removed']} lines, "
           f"{s['symbols_changed']} symbol(s) changed",
           "  findings: " + ", ".join(f"{v} {k}" for k, v in s["findings"].items() if v) if any(s["findings"].values())
           else "  findings: none"]
    scope = report["scope"]
    if scope["allowed"] or scope["protected"]:
        out.append(f"  scope: allowed {scope['allowed'] or '(any)'}; protected {scope['protected'] or '(none)'} "
                   f"→ {s['protected']} protected, {s['out_of_scope']} out-of-scope file(s)")
    risk = report.get("risk") or {}
    if risk.get("path"):
        out.append(f"  risk: {risk['level']} ({risk['score']}/100), because of {risk['path']}")
        out.append("\nReview first (riskiest files):")
        by_path = {f["path"]: f for f in report["files"]}
        for t in risk["top"]:
            out.append(f"  {t['score']:>3} {t['level']:<6}  {t['path']}")
            for x in (by_path.get(t["path"], {}).get("risk") or {}).get("factors", []):
                out.append(f"             +{x['points']:<3} {x['text']}")
        for note in risk.get("notes") or []:
            out.append(f"  note: {note}")
    out.append("\nTouched components:")
    for c in report["components"]:
        flags = []
        if c["scope"].get("protected"):
            flags.append(f"{c['scope']['protected']} protected")
        if c["scope"].get("out-of-scope"):
            flags.append(f"{c['scope']['out-of-scope']} out of scope")
        if c["findings"]["high"]:
            flags.append(f"{c['findings']['high']} high")
        out.append(f"  {c['name']:<40} {c['files']:>3} files  +{c['lines_added']} −{c['lines_removed']}"
                   + (f"  [{', '.join(flags)}]" if flags else ""))
    commits = report.get("commits") or {}
    items = commits.get("items") or []
    if items and not one and (len(items) > 1 or not items[0].get("uncommitted")):
        hidden = commits.get("total", 0) - commits.get("shown", 0)
        out.append(f"\nCommits ({len(items)}, oldest first"
                   + (f"; {hidden} earlier not shown" if hidden > 0 else "")
                   + (f"; {commits['merges']} merge commit(s) not listed" if commits.get("merges") else "") + "):")
        by_path: dict[str, list[dict[str, Any]]] = {}
        for f in report["findings"]:
            if f.get("path"):
                by_path.setdefault(f["path"], []).append(f)
        for c in items:
            added = sum(f.get("added") or 0 for f in c["files"])
            removed = sum(f.get("removed") or 0 for f in c["files"])
            out.append(f"  {_commit_title(c)[:60]:<60} {len(c['files']):>3} file(s) +{added} −{removed}"
                       + (f"  {c.get('signals', 0)} signal(s)" if c.get("signals") else ""))
            if by_commit:
                for f in c["files"]:
                    out.append(f"      {f['status']} {f['path']}" + ("" if f.get("in_review", True) else
                                                                     "  (unchanged overall)"))
                    for x in by_path.get(f["path"], []):
                        out.append(f"          [{x['severity']:<6}] {x['title']}")
    if commits.get("note"):
        out.append(f"\nnote: {commits['note']}")
    if report["findings"]:
        out.append("\nFindings:")
        for f in report["findings"]:
            loc = f"{f['path']}:{f['line']}" if f.get("path") and f.get("line") else f.get("path", "")
            out.append(f"  [{f['severity']:<6}] {f['title']}: {f.get('detail', '')}" + (f"  ({loc})" if loc else ""))
    return "\n".join(out) + "\n"
