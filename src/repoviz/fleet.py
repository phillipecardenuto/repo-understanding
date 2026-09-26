"""Parallel agents: one view over the repository's Git worktrees, and where their work overlaps.

Several agents often work at once, each in its own ``git worktree`` on its own branch.  This module lists the
repository's worktrees (``git worktree list``), keeping only those that exist, pass Git's ownership check and
share this repository's common directory, and describes each one: branch, commits ahead of the default branch,
uncommitted files, active session, last activity and review verdict.

It also compares the work of every pair of worktrees, each measured from its merge base with the default branch:

* ``overlap-file`` (medium): the same file changed in both;
* ``overlap-symbol`` (high): the same function, method or class changed in both (Python, JavaScript and
  TypeScript, parsed as text);
* ``overlap-contract`` (high): one side changes a function's signature while the other side's new code calls it.

Read-only: files are read from disk and from Git objects; nothing is executed or written.
"""

from __future__ import annotations

import datetime as _dt
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import classify
from .gitutil import Git, GitError, probe_repository
from .session import StateStore

if TYPE_CHECKING:  # pragma: no cover
    from .repo import Repository

MAX_WORKTREES = 20
MAX_PARSED_FILES = 200  # files parsed per side for symbols, signatures and calls
MAX_FILE_BYTES = 1_000_000
MAX_ITEMS = 50  # overlaps listed per pair
MAX_STAT = 500  # uncommitted files whose modification time counts towards "last activity"
KINDS = ("overlap-symbol", "overlap-contract", "overlap-file")
SEVERITY = {"overlap-symbol": "high", "overlap-contract": "high", "overlap-file": "medium"}
_CODE_LANGS = ("python", "javascript", "typescript")


# --------------------------------------------------------------------------- worktrees


@dataclass
class Worktree:
    path: str
    head: str | None
    branch: str | None
    detached: bool = False
    locked: bool = False
    prunable: bool = False
    current: bool = False

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def label(self) -> str:
        return self.branch or f"detached at {(self.head or '?')[:10]}"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "name": self.name, "label": self.label}


def list_worktrees(git: Git) -> tuple[list[Worktree], list[dict[str, str]]]:
    """This repository's worktrees that can be read, in Git's order (the main one first), and why the others
    cannot."""
    _gitdir, common = git.git_dirs()
    out: list[Worktree] = []
    notes: list[dict[str, str]] = []
    for rec in git.worktree_list():
        if rec["bare"]:
            continue
        path = Path(rec["path"])
        wt = Worktree(str(path), rec["head"], rec["branch"], rec["detached"], rec["locked"], rec["prunable"])
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved == git.root:
            wt.path, wt.current = str(git.root), True
            out.append(wt)
            continue
        if not path.is_dir():
            notes.append({"path": str(path), "reason": "the directory is gone" + (" (`git worktree prune` forgets it)"
                                                                                  if rec["prunable"] else "")})
            continue
        root, reason = probe_repository(path)
        if root is None:  # e.g. Git's "dubious ownership" check: respected, never bypassed
            notes.append({"path": str(path), "reason": reason or "not readable"})
            continue
        other = Git(root)
        try:
            same = other.git_dirs()[1] == common
        finally:
            other.close()
        if root != resolved or not same:
            notes.append({"path": str(path), "reason": "not a worktree of this repository"})
            continue
        wt.path = str(root)
        out.append(wt)
    if len(out) > MAX_WORKTREES:
        notes.append({"path": "", "reason": f"{len(out) - MAX_WORKTREES} more worktree(s) not shown (limit "
                                            f"{MAX_WORKTREES})"})
        out = out[:MAX_WORKTREES - 1] + [next((w for w in out[MAX_WORKTREES - 1:] if w.current), out[MAX_WORKTREES - 1])]
    return out, notes


def default_of(repo: "Repository") -> tuple[str | None, str | None]:
    """The default branch and its commit."""
    if repo.git is None:
        return None, None
    name = repo.git.default_branch(repo.config.default_branch)
    try:
        return name, repo.git.resolve(name) if name else None
    except GitError:
        return name, None


# --------------------------------------------------------------------------- one worktree's work


@dataclass
class Side:
    """The work in one worktree: its changes since the merge base with the default branch, committed or not."""

    worktree: Worktree
    git: Git
    base: str | None
    committed: list[str]
    dirty: list[str]
    ahead: int | None
    _texts: dict[tuple[str, str], str | None] = field(default_factory=dict)
    _symbols: dict[str, dict[str, dict[str, Any]] | None] = field(default_factory=dict)
    _sigs: dict[str, dict[str, Any]] | None = None

    @property
    def files(self) -> set[str]:
        return set(self.committed) | set(self.dirty)

    def text(self, path: str) -> str | None:
        """The file as it is now in this worktree (on disk)."""
        key = ("now", path)
        if key not in self._texts:
            p = Path(self.worktree.path) / path
            data = None
            try:
                if not p.is_symlink() and p.is_file() and p.stat().st_size <= MAX_FILE_BYTES:
                    data = p.read_bytes()
            except OSError:
                data = None
            self._texts[key] = _decode(data)
        return self._texts[key]

    def base_text(self, path: str) -> str | None:
        """The file at the merge base with the default branch."""
        key = ("base", path)
        if key not in self._texts:
            data = self.git.show_file(self.base, path) if self.base else None
            self._texts[key] = _decode(data if data is None or len(data) <= MAX_FILE_BYTES else None)
        return self._texts[key]

    def changed_symbols(self, path: str) -> dict[str, dict[str, Any]] | None:
        """Functions, methods and classes this side added, removed or modified in ``path`` (``None``: a language
        not parsed, or a file that does not parse)."""
        if path not in self._symbols:
            old, new = _symbols(path, self.base_text(path)), _symbols(path, self.text(path))
            changed: dict[str, dict[str, Any]] | None = None
            if old is not None and new is not None:
                changed = {}
                for q in set(old) | set(new):
                    a, b = old.get(q), new.get(q)
                    if a and b and a["fingerprint"] == b["fingerprint"]:
                        continue
                    changed[q] = {"status": "added" if a is None else "removed" if b is None else "modified",
                                  "line": (b or a)["line"], "before": (a or {}).get("signature") or "",
                                  "after": (b or {}).get("signature") or ""}
                # a class (or function) that changed only through a nested definition is not a change of its own
                for q in [q for q in changed if any(o.startswith(q + ".") for o in changed if o != q)]:
                    del changed[q]
            self._symbols[path] = changed
        return self._symbols[path]

    def code_files(self) -> list[str]:
        return [p for p in sorted(self.files) if classify.language_of(p)[0] in _CODE_LANGS][:MAX_PARSED_FILES]

    def signature_changes(self) -> dict[str, dict[str, Any]]:
        """Functions whose parameters changed on this side, by name."""
        if self._sigs is None:
            self._sigs = {}
            for path in self.code_files():
                for q, c in (self.changed_symbols(path) or {}).items():
                    name = q.split(".")[-1].split("#")[0]
                    if (c["status"] == "modified" and c["before"] and c["after"] and c["before"] != c["after"]
                            and len(name) >= 3 and not name.startswith("__")):
                        self._sigs.setdefault(name, {"symbol": q, "path": path, **c})
        return self._sigs

    def added_lines(self, path: str) -> list[tuple[int, str]]:
        """Lines of ``path`` that are not in its merge-base version (by content)."""
        now = self.text(path)
        if now is None:
            return []
        pool = Counter((self.base_text(path) or "").splitlines())
        out = []
        for i, line in enumerate(now.splitlines(), 1):
            if pool[line] > 0:
                pool[line] -= 1
            else:
                out.append((i, line))
        return out


def _decode(data: bytes | None) -> str | None:
    if data is None or b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", errors="replace")


def _js_signature(text: str, start: int, end: int) -> str:
    m = re.search(r"\((?:[^()]|\([^()]*\))*\)", text[start:min(end, start + 1000)])
    return " ".join(m.group(0).split()) if m else ""


def _symbols(path: str, text: str | None) -> dict[str, dict[str, Any]] | None:
    """qualified name → fingerprint, signature and line, for a Python or JavaScript / TypeScript file."""
    if text is None:
        return {}
    lang = classify.language_of(path)[0]
    if lang == "python":
        from .analyzers.python import parse_python

        info = parse_python(text, path)
        if info.error:
            return None
        return {s.qualname: {"fingerprint": s.fingerprint, "signature": s.signature, "line": s.line}
                for s in info.symbols if s.kind != "main-block"}
    if lang in ("javascript", "typescript"):
        from .analyzers.javascript import parse_js

        return {s.qualname: {"fingerprint": s.fingerprint, "signature": _js_signature(text, s.start, s.end),
                             "line": s.line} for s in parse_js(text).symbols}
    return None


def side_of(wt: Worktree, default: str | None, default_sha: str | None) -> Side:
    git = Git(wt.path)
    head = wt.head or git.head()
    base = None
    if head and default:
        try:
            base = git.merge_base(default, head)
        except GitError:
            base = None
    try:
        committed = git.changed_paths(base, head) if base and head and base != head else []
        dirty = sorted({p for e in git.status() for p in (e.path, e.orig_path) if p})
    except GitError:  # e.g. the worktree was removed meanwhile
        committed, dirty = [], []
    ahead = git.ahead_count(default_sha, head) if default_sha and head else None
    return Side(wt, git, base or head, committed, dirty, ahead)


# --------------------------------------------------------------------------- comparing two worktrees


def _params(sig: str) -> list[str]:
    """Parameter names of ``(a, b: int = 1, *, c) -> X`` (``*`` and ``/`` kept as markers)."""
    start = sig.find("(")
    if start < 0:
        return []
    depth, part, parts = 0, "", []
    for ch in sig[start + 1:]:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(part)
            part = ""
        else:
            part += ch
    parts.append(part)
    names = []
    for x in parts:
        m = re.match(r"\s*(\*{1,2}\s*[A-Za-z_$][\w$]*|[A-Za-z_$][\w$]*|\*|/|\.\.\.[A-Za-z_$][\w$]*)", x)
        if m:
            names.append(m.group(1).replace(" ", ""))
    return names


def signature_delta(before: str, after: str) -> str:
    """What changed between two signatures, in words: ``added offset, * (keyword-only)``."""
    a, b = _params(before), _params(after)
    word = {"*": "* (keyword-only)", "/": "/ (positional-only)"}
    added = [word.get(x, x) for x in b if x not in a]
    removed = [word.get(x, x) for x in a if x not in b]
    parts = ([f"added {', '.join(added)}"] if added else []) + ([f"removed {', '.join(removed)}"] if removed else [])
    if not parts:
        parts.append("parameters reordered" if a != b else "annotations or defaults changed")
    return "; ".join(parts)


def short_signatures(o: dict[str, Any]) -> str:
    """``before → after``, or what changed when the signatures are long."""
    if len(o["before"]) + len(o["after"]) <= 100:
        return f"{o['before']} → {o['after']}"
    return o.get("delta") or signature_delta(o["before"], o["after"])


_DEFINITION = re.compile(r"^\s*(export\s+)?(default\s+)?(async\s+)?(def|function|class)\b")


def _calls(side: Side, names: dict[str, dict[str, Any]]) -> list[tuple[str, int, str, str]]:
    """New code on ``side`` that calls one of ``names``: ``(path, line, text, name)``."""
    if not names:
        return []
    pattern = re.compile(r"(?<![\w$])(" + "|".join(re.escape(n) for n in sorted(names)) + r")\s*\(")
    out = []
    for path in side.code_files():
        for line_no, line in side.added_lines(path):
            if _DEFINITION.match(line):
                continue
            for m in pattern.finditer(line):
                # `name(args) {` at the start of a line defines a JavaScript method rather than calling it
                if not line[:m.start()].strip() and line.rstrip().endswith("{"):
                    continue
                out.append((path, line_no, line.strip()[:200], m.group(1)))
                break
            if len(out) >= MAX_ITEMS:
                return out
    return out


def compare(a: Side, b: Side) -> list[dict[str, Any]]:
    """Where the work of ``a`` and ``b`` overlaps (most severe first).  Contract items say which side changed the
    signature (``changed_by``: ``a`` or ``b``)."""
    out: list[dict[str, Any]] = []
    common = sorted(a.files & b.files)
    for i, path in enumerate(common):
        sa = a.changed_symbols(path) if i < MAX_PARSED_FILES else None
        sb = b.changed_symbols(path) if i < MAX_PARSED_FILES else None
        both = sorted(q for q in set(sa or {}) & set(sb or {})
                      if not (sa[q]["status"] == sb[q]["status"] == "removed"))  # type: ignore[index]
        for q in both:
            out.append({"kind": "overlap-symbol", "path": path, "symbol": q, "line_a": sa[q]["line"],  # type: ignore[index]
                        "line_b": sb[q]["line"], "status_a": sa[q]["status"], "status_b": sb[q]["status"]})  # type: ignore[index]
        if not both:
            out.append({"kind": "overlap-file", "path": path})
    for x, y, who in ((a, b, "a"), (b, a, "b")):
        sigs = x.signature_changes()
        for path, line, text, name in _calls(y, sigs):
            s = sigs[name]
            out.append({"kind": "overlap-contract", "changed_by": who, "path": s["path"], "symbol": s["symbol"],
                        "line": s["line"], "before": s["before"], "after": s["after"],
                        "delta": signature_delta(s["before"], s["after"]), "call_path": path,
                        "call_line": line, "call": text})
    out.sort(key=lambda o: KINDS.index(o["kind"]))
    return out


# --------------------------------------------------------------------------- the fleet


def _iso(ts: float | None) -> str | None:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat(timespec="seconds") if ts else None


def _last_activity(side: Side) -> float | None:
    times = []
    out = side.git.try_run("show", "-s", "--format=%ct", side.worktree.head) if side.worktree.head else None
    if out and out.strip().isdigit():
        times.append(float(out.strip()))
    for rel in side.dirty[:MAX_STAT]:
        try:
            times.append((Path(side.worktree.path) / rel).lstat().st_mtime)
        except OSError:
            continue
    return max(times) if times else None


def _row(repo: "Repository", side: Side, default: str | None, repo_for: Callable[[str], "Repository"],
         with_risk: bool) -> dict[str, Any]:
    wt = side.worktree
    store = repo.state if wt.current else StateStore(Path(wt.path), Path(wt.path).name, repo.config.state_dir)
    session = store.current_session()
    session = session if session and session.active else None
    row = {**wt.to_dict(), "ahead": side.ahead, "dirty": len(side.dirty), "changed": len(side.files),
           "session": {"id": session.id, "label": session.label, "started_at": session.started_at} if session else None,
           "last_activity": _iso(_last_activity(side)), "verdict": None}
    # The worktree's wave, as `repoviz review` names it: its session, else its branch since it left the default
    # branch, else its uncommitted changes.  Its verdict is the first one found in that order.
    targets = [("session", f"session:{session.id}")] if session else []
    if wt.branch and default and wt.branch != default.split("/")[-1] and side.ahead:
        targets.append(("branch", f"branch:{wt.branch}:{default}"))
    targets.append(("all", f"all@{wt.branch or 'HEAD'}"))
    row["wave"] = targets[0][0]
    judged = next((tid for tid, key in targets if store.load_verdict(key) is not None), None)
    if judged or (with_risk and side.files):
        from .review import build_review, resolve_target
        from .verdict import current, view

        wrepo = repo_for(wt.path)
        try:
            if judged:
                target = resolve_target(wrepo, judged)
                v = wrepo.state.load_verdict(target.key)
                shown = view(v, current(wrepo, target)["fingerprint"]) or {}
                row["verdict"] = {k: val for k, val in shown.items()
                                  if k in ("verdict", "label", "reviewer", "at", "stale", "summary")}
                row["verdict"]["target"] = target.label
            if with_risk and side.files:
                risk = build_review(wrepo, resolve_target(wrepo, row["wave"]))["risk"]
                row["risk"] = {k: risk.get(k) for k in ("level", "score", "path")}
        except (ValueError, GitError) as exc:
            row["error"] = str(exc)[:200]
    return row


def fleet(repo: "Repository", *, with_risk: bool = False,
          repo_for: Callable[[str], "Repository"] | None = None) -> dict[str, Any]:
    """Every worktree of the repository with its state, and the overlaps between each pair."""
    started = time.monotonic()
    if repo.git is None:
        return {"worktrees": [], "overlaps": [], "diagnostics": [], "default_branch": None}
    from .repo import Repository

    opened: dict[str, Repository] = {str(repo.root): repo}

    def open_repo(path: str) -> "Repository":
        if repo_for is not None:
            return repo_for(path)
        if path not in opened:
            opened[path] = Repository(path)
        return opened[path]

    wts, notes = list_worktrees(repo.git)
    default, default_sha = default_of(repo)
    sides = [side_of(w, default, default_sha) for w in wts]
    try:
        rows = [_row(repo, s, default, open_repo, with_risk) for s in sides]
        pairs = []
        active = [s for s in sides if s.files]
        for i, a in enumerate(active):
            for b in active[i + 1:]:
                items = compare(a, b)
                if items:
                    pairs.append({"a": a.worktree.path, "b": b.worktree.path, "counts": dict(Counter(o["kind"] for o in items)),
                                  "items": items[:MAX_ITEMS], "more": max(0, len(items) - MAX_ITEMS)})
    finally:
        for s in sides:
            s.git.close()
        for path, r in opened.items():
            if r is not repo:
                r.close()
    return {"default_branch": default, "worktrees": rows, "overlaps": pairs, "diagnostics": notes,
            "elapsed_ms": int((time.monotonic() - started) * 1000)}


def overlaps_with_others(repo: "Repository") -> list[dict[str, Any]]:
    """What the current worktree's work shares with each other worktree's (each item names the other one)."""
    if repo.git is None:
        return []
    wts, _ = list_worktrees(repo.git)
    me = next((w for w in wts if w.current), None)
    if me is None or len(wts) < 2:
        return []
    default, default_sha = default_of(repo)
    mine = side_of(me, default, default_sha)
    sides = [mine]
    out: list[dict[str, Any]] = []
    try:
        if mine.files:
            for w in wts:
                if w.current:
                    continue
                other = side_of(w, default, default_sha)
                sides.append(other)
                if other.files:
                    out += [{**o, "other": w.to_dict()} for o in compare(mine, other)]
    finally:
        for s in sides:
            s.git.close()
    return out


def others_state(repo: "Repository") -> tuple[Any, ...]:
    """A key that changes whenever another worktree's work changes (its HEAD, or its uncommitted files and their
    modification times), so a cached review never shows outdated overlaps.  ``()`` without other worktrees."""
    if repo.git is None:
        return ()
    recs = repo.git.worktree_list()
    if len(recs) < 2:
        return ()
    out: list[Any] = []
    for rec in recs:
        path = Path(rec["path"])
        try:
            if rec["bare"] or path.resolve() == repo.git.root or not path.is_dir():
                continue
        except OSError:
            continue
        git = Git(path)
        try:
            stamp = []
            for e in git.status()[:MAX_STAT]:
                try:
                    st = (path / e.path).lstat()
                    stamp.append((e.path, e.index, e.worktree, st.st_mtime_ns, st.st_size))
                except OSError:
                    stamp.append((e.path, e.index, e.worktree))
        except GitError:
            stamp = []
        finally:
            git.close()
        out.append((str(path), rec["head"], rec["branch"], hash(tuple(stamp))))
    return tuple(out)


def format_fleet(res: dict[str, Any]) -> str:
    """``repoviz fleet`` as text."""
    rows = res["worktrees"]
    if not rows:
        return "Not a Git repository: no worktrees.\n"
    names = {r["path"]: r["name"] for r in rows}
    lines = [f"{len(rows)} worktree(s)" + (f"; default branch {res['default_branch']}" if res.get("default_branch") else ""), ""]
    for r in rows:
        bits = [f"{'*' if r['current'] else ' '} {r['name']:<24} {r['label']:<28}",
                f"ahead {r['ahead'] if r['ahead'] is not None else '?':>3}", f"uncommitted {r['dirty']:>3}"]
        if r["session"]:
            bits.append(f"session “{r['session']['label'] or r['session']['id']}” since {r['session']['started_at'][:16]}")
        if r["last_activity"]:
            bits.append(f"active {r['last_activity'][:16]}")
        if r["verdict"]:
            v = r["verdict"]
            bits.append(f"verdict: {v['label'].lower()}" + (" (stale)" if v.get("stale") else ""))
        if r.get("risk"):
            bits.append(f"risk {r['risk']['level']} ({r['risk']['score']})")
        flags = [f for f in ("locked", "prunable", "detached") if r.get(f)]
        if flags:
            bits.append("[" + ", ".join(flags) + "]")
        lines.append("  ".join(bits))
    lines += ["", "Overlaps between worktrees (each from its merge base with the default branch):"]
    if not res["overlaps"]:
        lines.append("  none")
    for p in res["overlaps"]:
        a, b = names.get(p["a"], p["a"]), names.get(p["b"], p["b"])
        c = p["counts"]
        lines.append(f"  {a} ↔ {b}: " + ", ".join(f"{c[k]} {k.removeprefix('overlap-')}" for k in KINDS if c.get(k)))
        for o in p["items"][:10]:
            lines.append("    " + describe(o, a, b))
        if len(p["items"]) > 10 or p["more"]:
            lines.append(f"    … {len(p['items']) - 10 + p['more']} more")
    for n in res["diagnostics"]:
        lines.append(f"Not listed: {n['path']}: {n['reason']}")
    return "\n".join(lines) + "\n"


def describe(o: dict[str, Any], a: str, b: str) -> str:
    sev = SEVERITY[o["kind"]]
    if o["kind"] == "overlap-symbol":
        return f"[{sev}] same symbol: {o['symbol']} in {o['path']} ({a} line {o['line_a']}, {b} line {o['line_b']})"
    if o["kind"] == "overlap-file":
        return f"[{sev}] same file: {o['path']}"
    changer, caller = (a, b) if o["changed_by"] == "a" else (b, a)
    return (f"[{sev}] {changer} changes the signature of {o['symbol']} ({o['path']}:{o['line']}): "
            f"{short_signatures(o)}; {caller} calls it at {o['call_path']}:{o['call_line']}")
