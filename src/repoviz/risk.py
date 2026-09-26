"""Explainable risk score per changed file and per wave.

A small deterministic model, not a verdict: it orders a review so the files most likely
to hide a problem come first, and says why.  Each factor contributes points, and every
factor that contributes is listed with its reason (``called from 14 places in 4
components``).

=================  ==========================================================  =======
Factor             What counts                                                 Default
=================  ==========================================================  =======
``signals``        highest signal severity on the file (high, medium, low)     30
``fan_in``         places that call (or import) the changed code, log-scaled   20
``entry_points``   entry points that reach the changed code, log-scaled        15
``tests``          no test reaches the change, or none of them was updated     10
``sensitive``      protected, security-related, sensitive or out-of-scope path 10
``churn``          a churn hotspot (see :func:`hotspot_threshold`)             5
``size``           lines added and removed, log-scaled                         10
=================  ==========================================================  =======

Weights come from ``[review.risk]`` and are normalised so that the maximum score is 100.
The wave's risk is its riskiest file.  Following callers is bounded (:data:`MAX_WALK`
nodes per review); files beyond the budget get no ``entry_points`` points, and the wave
says so in ``notes``.
"""

from __future__ import annotations

import math
import re
from collections import deque
from typing import Any, Callable, Iterable

from .model import ADDED, MODIFIED, REL_CALLS, REL_IMPORTS, REL_INVOKES, REMOVED, RepositoryDiff, RepositorySnapshot

FACTORS = ("signals", "fan_in", "entry_points", "tests", "sensitive", "churn", "size")
DEFAULT_WEIGHTS: dict[str, float] = {"signals": 30, "fan_in": 20, "entry_points": 15, "tests": 10,
                                     "sensitive": 10, "churn": 5, "size": 10}
#: Score at or above which a file (or wave) is "high" / "medium" risk.
DEFAULT_THRESHOLDS: dict[str, float] = {"high": 40, "medium": 20}
LEVELS = ("high", "medium", "low")

SEVERITY_SHARE = {"high": 1.0, "medium": 0.5, "low": 1 / 6}
FAN_IN_FULL = 32  # places for full fan-in points
ENTRY_POINTS_FULL = 8
SIZE_FULL = 400  # lines added + removed for full size points
HOT_PERCENTILE = 0.8  # a churn hotspot is at or above the 80th percentile of commits among modules,
MIN_HOT_COMMITS = 2  # and changed at least twice (a file committed once is not churn)
MAX_WALK = 200_000  # nodes visited, over all files of a review, when following callers
MAX_WALK_PER_FILE = 5_000

_SECURITY_PATH = re.compile(
    r"(^|/)(auth|authn|authz|authentication|authorization|oauth|security|permissions?|rbac|acl|crypto|"
    r"login|passwords?|credentials?|secrets?)(/|[._-]|$)", re.IGNORECASE)
_COSMETIC = (["formatting or comments only"], ["contents changed"])


def _reverse_graphs(diff: RepositoryDiff) -> tuple[dict[str, set[str]], dict[str, set[str]], set[str]]:
    """Who calls (or invokes) each node, who imports (or invokes) each module, over both states.

    Diffs are cached by the repository, so this is memoised on the diff object."""
    cached = diff.__dict__.get("_risk_graphs")
    if cached is not None:
        return cached
    callers: dict[str, set[str]] = {}
    importers: dict[str, set[str]] = {}
    call_sources: set[str] = set()
    for ch in diff.edges.values():
        e = ch.edge
        rel = e.relationship
        if not e.direct or rel not in (REL_CALLS, REL_INVOKES, REL_IMPORTS):
            continue
        if rel != REL_IMPORTS:
            callers.setdefault(e.target_id, set()).add(e.source_id)
            if rel == REL_CALLS:
                call_sources.add(e.source_id)
        if rel != REL_CALLS:
            importers.setdefault(e.target_id, set()).add(e.source_id)
    nodes = diff.nodes
    languages = {nodes[s].node.language for s in call_sources if s in nodes and nodes[s].node.language}
    result = (callers, importers, languages)
    diff.__dict__["_risk_graphs"] = result
    return result


def hotspot_threshold(counts: Iterable[int]) -> int | None:
    """The commit count from which a module is a churn hotspot.  The one definition shared by the risk score, the
    Structure tab (mirrored in ``app.js``) and its code-changes drawer (:func:`repoviz.filechanges.hotspots`)."""
    ordered = sorted(c for c in counts if c > 0)
    return max(MIN_HOT_COMMITS, ordered[int(len(ordered) * HOT_PERCENTILE)]) if ordered else None


def _churn(snap: RepositorySnapshot) -> tuple[dict[str, int], int | None]:
    """Commits per file path in the churn window, and the count from which a file is a hotspot (memoised)."""
    cached = snap.__dict__.get("_risk_churn")
    if cached is not None:
        return cached
    churn: dict[str, int] = {}
    for n in (*snap.components, *snap.modules):
        c = n.metadata.get("churn")
        if n.path and c and n.component_type != "directory" and "commits" in c and "last_commit" in c:
            churn[n.path] = max(churn.get(n.path, 0), int(c["commits"]))
    hot = hotspot_threshold(int(n.metadata["churn"]["commits"]) for n in snap.modules
                            if n.path and isinstance(n.metadata.get("churn"), dict) and n.metadata["churn"].get("commits"))
    snap.__dict__["_risk_churn"] = (churn, hot)
    return churn, hot


def _log_share(n: int, full: int) -> float:
    return min(1.0, math.log2(1 + n) / math.log2(1 + full)) if n > 0 else 0.0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def level_of(score: float, thresholds: dict[str, float] | None = None) -> str:
    t = thresholds or DEFAULT_THRESHOLDS
    return "high" if score >= t["high"] else "medium" if score >= t["medium"] else "low"


class RiskContext:
    """Lookups shared by every file of one review, built once."""

    def __init__(self, diff: RepositoryDiff, base: RepositorySnapshot, target: RepositorySnapshot,
                 findings: Iterable[dict[str, Any]], *, weights: dict[str, float] | None = None,
                 thresholds: dict[str, float] | None = None,
                 component_of: Callable[[str], tuple[str | None, str | None]] | None = None,
                 sensitive_of: Callable[[str], str | None] | None = None,
                 changed_tests: set[str] | None = None) -> None:
        self.diff = diff
        self.nodes = diff.nodes
        w = {**DEFAULT_WEIGHTS, **(weights or {})}
        total = sum(w[k] for k in FACTORS) or 1.0
        self.scale = {k: w[k] * 100.0 / total for k in FACTORS}  # normalised: the maximum score is 100
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._component_of = component_of or (lambda _p: (None, None))
        self._components: dict[str, str | None] = {}
        self.sensitive_of = sensitive_of or (lambda _p: None)
        self.changed_tests = changed_tests or set()
        self.findings_by_path: dict[str, list[dict[str, Any]]] = {}
        for f in findings:
            if f.get("path"):
                self.findings_by_path.setdefault(f["path"], []).append(f)
        self.callers, self.importers, self.call_languages = _reverse_graphs(diff)
        # Calls to a class resolve to its constructor: a changed (or renamed) class reaches the constructor's callers.
        self.constructors = {c.node.parent_id: nid for nid, c in self.nodes.items()
                             if c.node.name in ("__init__", "constructor") and c.node.parent_id}
        # Churn before the wave (the wave's own commits do not make a file a hotspot).
        self.window = base.metadata.get("churn_window_commits") or target.metadata.get("churn_window_commits")
        self.churn, self.hot_threshold = _churn(base)
        if not self.churn:
            self.churn, self.hot_threshold = _churn(target)
        self.budget = MAX_WALK
        self.skipped: list[str] = []

    # -- helpers -------------------------------------------------------------------------------------------------
    def _component(self, path: str) -> str | None:
        if path not in self._components:
            self._components[path] = self._component_of(path)[0]
        return self._components[path]

    def _is_test(self, nid: str) -> bool:
        ch = self.nodes.get(nid)
        return ch is not None and "test" in ch.node.tags

    def _is_entry(self, nid: str) -> bool:
        ch = self.nodes.get(nid)
        return ch is not None and "entry-point" in ch.node.tags and "test" not in ch.node.tags

    def _walk(self, roots: list[str], graph: dict[str, set[str]]) -> tuple[set[str], set[str], bool]:
        """Nodes that reach ``roots`` through ``graph``: (entry points, test paths, complete?)."""
        seen = set(roots)
        queue = deque(roots)
        entries = {r for r in roots if self._is_entry(r)}
        tests: set[str] = set()
        limit = min(MAX_WALK_PER_FILE, self.budget)
        while queue:
            cur = queue.popleft()
            for nxt in sorted(graph.get(cur, ())):
                if nxt in seen:
                    continue
                if len(seen) >= limit:
                    self.budget -= len(seen)
                    return entries, tests, False
                seen.add(nxt)
                if self._is_entry(nxt):
                    entries.add(nxt)
                ch = self.nodes.get(nxt)
                if ch is not None and "test" in ch.node.tags and ch.node.path:
                    tests.add(ch.node.path)
                queue.append(nxt)
        self.budget -= len(seen)
        return entries, tests, True

    def _factor(self, factors: list[dict[str, Any]], name: str, share: float, text: str) -> None:
        points = round(self.scale[name] * share)
        if points > 0:
            factors.append({"factor": name, "points": points, "text": text})

    # -- scoring ---------------------------------------------------------------------------------------------------
    def score_file(self, entry: dict[str, Any]) -> dict[str, Any]:
        """``{"score": 0-100, "level": "low|medium|high", "factors": [{"factor", "points", "text"}]}``."""
        path = entry["path"]
        factors: list[dict[str, Any]] = []

        # Signals: the most severe one on this file.
        found = [f for f in self.findings_by_path.get(path, []) if f.get("severity") in SEVERITY_SHARE]
        if found:
            found.sort(key=lambda f: -SEVERITY_SHARE[f["severity"]])  # stable: the report's order within a severity
            top = found[0]
            more = f" and {_plural(len(found) - 1, 'more signal')}" if len(found) > 1 else ""
            self._factor(factors, "signals", SEVERITY_SHARE[top["severity"]],
                         f"{top['severity']} signal: {top.get('title', top.get('kind', ''))}{more}")

        # Blast radius: callers (or importers) of the changed code, and the entry points and tests reaching it.
        code = entry.get("kind") != "submodule" and not entry.get("is_test")
        roots = [s["id"] for s in entry.get("symbols") or [] if s["id"] in self.nodes]
        roots += [self.constructors[r] for r in roots if r in self.constructors and self.constructors[r] not in roots]
        module_id = entry.get("module_id")
        module = self.nodes.get(module_id) if module_id else None
        if not roots and module is not None and module.status in (ADDED, MODIFIED, REMOVED) \
                and module.reasons not in _COSMETIC:
            roots = [module_id]
        lang = module.node.language if module is not None else None
        by_calls = bool(roots) and (lang in self.call_languages or any(r in self.callers for r in roots))
        graph = self.callers if by_calls else self.importers
        if not by_calls and module_id:
            roots = [module_id] if roots else []
        reached_tests: set[str] = set()
        if code and roots:
            # Callers inside the file (a subclass calling ``super().__init__``) pass the change on: count the
            # first callers outside it.
            inside, frontier = set(roots), list(roots)
            while frontier and len(inside) < MAX_WALK_PER_FILE:
                for src in graph.get(frontier.pop(), ()):
                    if src not in inside and src in self.nodes and self.nodes[src].node.path == path:
                        inside.add(src)
                        frontier.append(src)
            places = {src for r in inside for src in graph.get(r, ())
                      if src in self.nodes and self.nodes[src].node.path != path and not self._is_test(src)}
            if places:
                comps = {self._component(self.nodes[p].node.path) for p in places if self.nodes[p].node.path}
                verb = "called from" if by_calls else "imported by"
                where = f" in {_plural(len(comps), 'component')}" if len(comps) > 1 else ""
                self._factor(factors, "fan_in", _log_share(len(places), FAN_IN_FULL),
                             f"{verb} {_plural(len(places), 'place')}{where}")
            if self.budget > 0:
                entries, reached_tests, complete = self._walk(roots, graph)
                if entries:
                    names = sorted(self.nodes[e].node.qualified_name for e in entries)
                    shown = ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
                    self._factor(factors, "entry_points", _log_share(len(entries), ENTRY_POINTS_FULL),
                                 f"reached from {_plural(len(entries), 'entry point')} ({shown})"
                                 + ("" if complete else ", at least"))
                if not complete:
                    self.skipped.append(path)
            else:
                self.skipped.append(path)

        # Tests: code whose behaviour changed, with no test reaching it or none updated.
        if code and entry.get("status") != REMOVED and entry.get("language") is not None \
                and any(s.get("status") in (ADDED, MODIFIED) for s in entry.get("symbols") or []):
            tests = set(entry.get("tests_affected") or []) | reached_tests
            measured = entry.get("coverage") or {}
            if measured.get("fresh") and measured.get("executable"):  # a coverage report beats the static guess
                missing = measured["executable"] - measured["covered"]
                if missing:
                    self._factor(factors, "tests", missing / measured["executable"],
                                 f"{missing} of {measured['executable']} changed lines not run by any test "
                                 f"({measured['report']})")
            elif not tests:
                self._factor(factors, "tests", 1.0, "no test imports or calls this code")
            elif not tests & self.changed_tests:
                verb = "covers" if len(tests) == 1 else "cover"
                self._factor(factors, "tests", 0.5, f"{_plural(len(tests), 'test file')} {verb} it; none was updated")

        # Sensitive places.
        shares = []
        if entry.get("scope") == "protected":
            shares.append((1.0, "protected area"))
        kind = self.sensitive_of(path)
        if kind:
            shares.append((0.7, f"sensitive file ({kind})"))
        if _SECURITY_PATH.search(path):
            shares.append((0.7, "security-related path"))
        if entry.get("scope") == "out-of-scope":
            shares.append((0.5, "outside the agreed scope"))
        if shares:
            share, text = max(shares, key=lambda s: s[0])
            self._factor(factors, "sensitive", share, text)

        # Churn hotspot (history before the wave).
        commits = self.churn.get(entry.get("previous_path") or path) or self.churn.get(path)
        if commits and self.hot_threshold is not None and commits >= self.hot_threshold:
            window = f" of the last {self.window}" if self.window else ""
            self._factor(factors, "churn", 1.0,
                         f"churn hotspot: changed in {commits}{window} commits (hotspots: {self.hot_threshold} "
                         f"or more, the busiest {round((1 - HOT_PERCENTILE) * 100)}% of modules)")

        # Size of the change.
        if entry.get("kind") == "submodule":
            lines = sum(entry.get("inner_lines") or [0, 0])
        else:
            lines = (entry.get("lines_added") or 0) + (entry.get("lines_removed") or 0)
        if entry.get("lines_added") is None and entry.get("diff_omitted") == "file too large":
            self._factor(factors, "size", 1.0, "file too large to diff")
        elif lines:
            self._factor(factors, "size", _log_share(lines, SIZE_FULL), f"{_plural(lines, 'line')} changed")

        factors.sort(key=lambda f: (-f["points"], FACTORS.index(f["factor"])))
        score = min(100, sum(f["points"] for f in factors))
        return {"score": score, "level": level_of(score, self.thresholds), "factors": factors}

    def wave(self, files: list[dict[str, Any]]) -> dict[str, Any]:
        """The wave's risk: its riskiest file, with the top files and a one-line reason."""
        out = wave_risk(files)
        if self.skipped:
            out["notes"].append(f"Entry points were not (fully) followed for {_plural(len(self.skipped), 'file')}: "
                                "the work cap for following callers was reached.")
        return out


def wave_risk(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Risk of a set of scored files: the riskiest one, the top three and how many per level."""
    scored = sorted((f for f in files if f.get("risk")), key=lambda f: (-f["risk"]["score"], f["path"]))
    counts = {lv: sum(1 for f in scored if f["risk"]["level"] == lv) for lv in LEVELS}
    top = [{"path": f["path"], "score": f["risk"]["score"], "level": f["risk"]["level"],
            "factors": [x["text"] for x in f["risk"]["factors"]]} for f in scored[:3]]
    if not scored:
        return {"score": 0, "level": "low", "path": None, "summary": "No changed files.", "top": [],
                "counts": counts, "notes": []}
    first = scored[0]
    reasons = "; ".join(x["text"] for x in first["risk"]["factors"][:2])
    summary = f"{first['risk']['level']} risk, because of {first['path']}" + (f" ({reasons})" if reasons else "")
    return {"score": first["risk"]["score"], "level": first["risk"]["level"], "path": first["path"],
            "summary": summary, "top": top, "counts": counts, "notes": []}
