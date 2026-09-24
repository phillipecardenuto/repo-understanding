"""Architecture contracts: rules about who may import whom, checked on the module import graph.

Contract types (``[[contracts]]`` in the configuration; ``[[review.rules]]`` are ``forbidden`` contracts):

* **layers** – high → low: a lower layer may not import a higher one (optionally inside each container);
* **independence** – the listed modules (or each match of ``pkg.*``) never import each other;
* **forbidden** – modules matching ``from`` may not import modules matching ``to``;
* **public-interface** – code outside ``module`` imports it only through ``public``;
* **acyclic** – no import cycle between the listed modules (or each match of ``pkg.*``);
* **required** – every module matching ``from`` imports at least one module matching ``to``.

A pattern is a qualified name (``app.services``: that package and everything in it; ``app.features.*``: one
unit per feature) or, when it contains ``/``, a path glob anchored at the root (``src/features/*``), so
contracts work for every analyzed language.  Imports from test modules and ``TYPE_CHECKING``-only imports are
not checked.  Direct imports are checked by default; ``allow_indirect = false`` also follows chains of
imports (``models → util → routes``), bounded by :data:`MAX_CHAIN` hops and :data:`MAX_VISITS` steps.

**Baseline.**  Legacy code often breaks a new contract.  ``repoviz contracts --baseline`` prints the current
violations as JSON; the user saves it as ``.repoviz-known-violations.json`` and commits it.  repoviz never
writes into the repository: it only reads that file (from the tree being checked), then reports only the
violations not in it, and the known ones that are gone ("fixed").
"""

from __future__ import annotations

import fnmatch
import json
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from . import globs
from .config import Contract
from .model import REL_IMPORTS, RepositorySnapshot
from .redact import redact

MAX_CHAIN = 8  # hops followed for indirect imports
MAX_VISITS = 200_000  # graph steps per contract when following indirect imports
MAX_VIOLATIONS = 500  # per contract
BASELINE_FORMAT = "repoviz-known-violations"
SEVERITY_LEVEL = {"high": "error", "medium": "warning", "low": "note"}


# --------------------------------------------------------------------------- patterns


def _is_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def matches(q: str, path: str | None, pattern: str) -> bool:
    """Does the module with qualified name ``q`` at ``path`` fall under ``pattern``?"""
    pattern = pattern.strip()
    if not pattern:
        return False
    if "/" in pattern:  # a path glob, anchored at the repository root
        return bool(path) and globs.match(path, pattern if pattern.startswith("/") else "/" + pattern)
    if _is_glob(pattern):
        return fnmatch.fnmatchcase(q, pattern)
    return q == pattern or q.startswith(pattern + ".") or q.startswith(pattern + "/")


def unit_of(q: str, path: str | None, pattern: str) -> str | None:
    """The unit a module belongs to under ``pattern``: ``pkg.*`` / ``dir/*`` make one unit per child."""
    if not matches(q, path, pattern):
        return None
    p = pattern.strip()
    if p.endswith(".*") and "/" not in p and not _is_glob(p[:-2]):
        prefix, key, sep = p[:-2], q, "."
    elif p.endswith(("/*", "/**")) and not _is_glob(p.rstrip("*").rstrip("/")):
        prefix, key, sep = p.rstrip("*").rstrip("/").lstrip("/"), path or "", "/"
    else:
        return p
    rest = key[len(prefix) + 1:] if key.startswith(prefix + sep) else ""
    return prefix + sep + rest.split(sep)[0] if rest else None


def _join(container: str, layer: str) -> str:
    return f"{container.rstrip('/')}/{layer}" if "/" in container else f"{container}.{layer}"


# --------------------------------------------------------------------------- the import graph


class _Graph:
    """Internal module-to-module imports of one snapshot (tests and TYPE_CHECKING-only imports left out)."""

    def __init__(self, snapshot: RepositorySnapshot) -> None:
        self.nodes = {m.id: m for m in snapshot.modules}
        self.tests = {nid for nid, n in self.nodes.items() if "test" in n.tags}
        self.out: dict[str, list[tuple[str, Any]]] = {}
        for e in snapshot.dependency_edges:
            if e.relationship != REL_IMPORTS or not e.direct or e.source_id == e.target_id:
                continue
            if e.source_id not in self.nodes or e.target_id not in self.nodes or e.source_id in self.tests:
                continue
            if e.metadata.get("type_checking_only") or e.metadata.get("test_only"):
                continue
            self.out.setdefault(e.source_id, []).append((e.target_id, e))
        for edges in self.out.values():
            edges.sort(key=lambda x: self.nodes[x[0]].qualified_name)

    def name(self, nid: str) -> str:
        return self.nodes[nid].qualified_name

    def modules(self) -> list[str]:
        return sorted((nid for nid in self.nodes if nid not in self.tests), key=self.name)


def graph_of(snapshot: RepositorySnapshot) -> _Graph:
    cached = snapshot.__dict__.get("_contracts_graph")
    if cached is None:
        cached = _Graph(snapshot)
        snapshot.__dict__["_contracts_graph"] = cached
    return cached


# --------------------------------------------------------------------------- violations


@dataclass
class Violation:
    contract: str
    type: str
    severity: str
    source: str  # qualified name of the importing module (or the unit, for acyclic)
    target: str
    detail: str
    path: str | None = None
    line: int | None = None
    excerpt: str | None = None
    chain: list[str] = field(default_factory=list)  # qualified names, source … target (more than 2: indirect)
    edge_ids: list[str] = field(default_factory=list)  # the snapshot's import edges along the chain
    source_id: str | None = None
    target_id: str | None = None

    @property
    def key(self) -> str:
        """Stable across revisions (qualified names, not IDs): what the baseline remembers."""
        return f"{self.contract}::{self.source}::{self.target}"

    @property
    def indirect(self) -> bool:
        return len(self.chain) > 2

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "key": self.key, "indirect": self.indirect}


def _evidence(edge: Any) -> tuple[str | None, int | None, str | None]:
    ev = (edge.evidence or [None])[0] if edge is not None else None
    if ev is None:
        return None, None, None
    return ev.path, ev.start_line, redact(ev.excerpt) if ev.excerpt else None


class _Checker:
    def __init__(self, contract: Contract, graph: _Graph) -> None:
        self.c, self.g = contract, graph
        self.out: list[Violation] = []
        self.seen: set[str] = set()
        self.visits = 0
        self.capped = False

    def add(self, source_id: str, target_id: str, why: str, path_ids: list[str], edges: list[Any]) -> None:
        g, c = self.g, self.c
        chain = [g.name(n) for n in path_ids]
        v = Violation(c.name, c.type, c.severity, g.name(source_id), g.name(target_id), "", chain=chain,
                      edge_ids=[e.id for e in edges], source_id=source_id, target_id=target_id)
        if v.key in self.seen:
            return
        if len(self.out) >= MAX_VIOLATIONS:
            self.capped = True
            return
        self.seen.add(v.key)
        v.path, v.line, v.excerpt = _evidence(edges[0] if edges else None)
        if v.path is None:
            v.path = g.nodes[source_id].path
        via = f" (through {' → '.join(chain[1:-1])})" if len(chain) > 2 else ""
        v.detail = (c.message + ": " if c.message else "") + f"{v.source} imports {v.target}{via}: {why}."
        self.out.append(v)

    def direct(self, bad: Any) -> None:
        """Report every direct import ``m → d`` for which ``bad(m, d)`` returns a reason."""
        for m in self.g.modules():
            for d, e in self.g.out.get(m, ()):
                why = bad(m, d)
                if why:
                    self.add(m, d, why, [m, d], [e])

    def indirect(self, starts: Iterable[str], bad: Any, through: Any) -> None:
        """From each start, the shortest chain of imports (2+ hops) to a module ``d`` where ``bad(start, d)``.

        Chains only pass through modules for which ``through(nid)`` is true (those outside the contract): a chain
        through another constrained module is either allowed or reported from that module already."""
        for m in starts:
            parent: dict[str, tuple[str, Any]] = {}
            depth = {m: 0}
            queue = deque([m])
            while queue:
                cur = queue.popleft()
                if depth[cur] >= MAX_CHAIN:
                    continue
                for d, e in self.g.out.get(cur, ()):
                    self.visits += 1
                    if self.visits > MAX_VISITS:
                        self.capped = True
                        return
                    if d in depth:
                        continue
                    depth[d], parent[d] = depth[cur] + 1, (cur, e)
                    why = bad(m, d)
                    if why:
                        if depth[d] >= 2:
                            ids, edges, x = [d], [], d
                            while x != m:
                                p, pe = parent[x]
                                ids.append(p)
                                edges.append(pe)
                                x = p
                            self.add(m, d, why, ids[::-1], edges[::-1])
                        continue  # stop at the violation
                    if through(d):
                        queue.append(d)


def _check_one(c: Contract, g: _Graph) -> _Checker:
    ck = _Checker(c, g)
    info = lambda nid: (g.name(nid), g.nodes[nid].path)  # noqa: E731
    if c.type == "layers":
        stacks = [[_join(ctr, layer) for layer in c.layers] for ctr in c.containers] or [list(c.layers)]

        def layer_of(nid: str) -> tuple[int, int] | None:
            q, p = info(nid)
            for si, stack in enumerate(stacks):
                for li, pattern in enumerate(stack):
                    if matches(q, p, pattern):
                        return si, li
            return None

        layers = {nid: layer_of(nid) for nid in g.nodes}

        def bad(m: str, d: str) -> str | None:
            lm, ld = layers.get(m), layers.get(d)
            if lm and ld and lm[0] == ld[0] and ld[1] < lm[1]:
                return (f"layer {stacks[lm[0]][lm[1]]!r} is below {stacks[ld[0]][ld[1]]!r} "
                        "and may not import it")
            return None
        ck.direct(bad)
        if not c.allow_indirect:
            ck.indirect([m for m in g.modules() if layers.get(m)], bad, lambda nid: not layers.get(nid))
    elif c.type in ("independence", "acyclic"):
        units = {nid: next((u for pat in c.modules if (u := unit_of(*info(nid), pat))), None) for nid in g.nodes}
        if c.type == "independence":
            def bad(m: str, d: str) -> str | None:
                um, ud = units.get(m), units.get(d)
                return f"{um} and {ud} must stay independent" if um and ud and um != ud else None
            ck.direct(bad)
            if not c.allow_indirect:
                ck.indirect([m for m in g.modules() if units.get(m)], bad, lambda nid: not units.get(nid))
        else:
            _acyclic(ck, units)
    elif c.type == "forbidden":
        src = {nid for nid in g.nodes if any(matches(*info(nid), p) for p in c.source)}
        dst = {nid for nid in g.nodes if any(matches(*info(nid), p) for p in c.target)}

        def bad(m: str, d: str) -> str | None:
            return "this dependency is forbidden" if m in src and d in dst and d not in src else None
        ck.direct(bad)
        if not c.allow_indirect:
            ck.indirect(sorted(src, key=g.name), bad, lambda nid: nid not in src and nid not in dst)
    elif c.type == "public-interface":
        inside = {nid for nid in g.nodes if matches(*info(nid), c.module)}
        public = {nid for nid in inside if any(matches(*info(nid), p) for p in c.public)}

        def bad(m: str, d: str) -> str | None:
            if d in inside and m not in inside and d not in public:
                return f"code outside {c.module} must go through its public interface ({', '.join(c.public)})"
            return None
        ck.direct(bad)
    elif c.type == "required":
        dst = {nid for nid in g.nodes if any(matches(*info(nid), p) for p in c.target)}
        for m in g.modules():
            if any(matches(*info(m), p) for p in c.source) and m not in dst \
                    and not any(d in dst for d, _e in g.out.get(m, ())):
                v = Violation(c.name, c.type, c.severity, g.name(m), " | ".join(c.target),
                              (c.message + ": " if c.message else "") +
                              f"{g.name(m)} must import one of {', '.join(c.target)} but imports none of them.",
                              path=g.nodes[m].path, chain=[g.name(m)], source_id=m)
                if len(ck.out) < MAX_VIOLATIONS:
                    ck.out.append(v)
                else:
                    ck.capped = True
    return ck


def _acyclic(ck: _Checker, units: dict[str, str | None]) -> None:
    """One violation per cycle between units, with an example cycle and the import behind its first step."""
    g = ck.g
    adj: dict[str, dict[str, tuple[str, str, Any]]] = {}
    for m, edges in g.out.items():
        for d, e in edges:
            um, ud = units.get(m), units.get(d)
            if um and ud and um != ud:
                adj.setdefault(um, {}).setdefault(ud, (m, d, e))
    for scc in _sccs(adj):
        if len(scc) < 2:
            continue
        members = sorted(scc)
        start = members[0]
        # Shortest cycle through `start` inside the component.
        parent: dict[str, str] = {}
        queue, seen, end = deque([start]), {start}, None
        while queue and end is None:
            cur = queue.popleft()
            for nxt in sorted(adj.get(cur, {})):
                if nxt == start:
                    end = cur
                    break
                if nxt in scc and nxt not in seen:
                    seen.add(nxt)
                    parent[nxt] = cur
                    queue.append(nxt)
        cycle = [start]
        x = end
        while x is not None and x != start:
            cycle.insert(1, x)
            x = parent.get(x)
        cycle.append(start)
        m, d, e = adj[cycle[0]][cycle[1]]
        c = ck.c
        v = Violation(c.name, c.type, c.severity, " ⇄ ".join(members), "(cycle)",
                      (c.message + ": " if c.message else "") + f"import cycle between {' → '.join(cycle)}; "
                      f"for example {g.name(m)} imports {g.name(d)}.", chain=cycle,
                      edge_ids=[adj[a][b][2].id for a, b in zip(cycle, cycle[1:])], source_id=m, target_id=d)
        v.path, v.line, v.excerpt = _evidence(e)
        ck.out.append(v)


def _sccs(adj: dict[str, dict[str, Any]]) -> list[set[str]]:
    """Strongly connected components (iterative Tarjan)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[set[str]] = []
    counter = 0
    for root in sorted(set(adj) | {d for ds in adj.values() for d in ds}):
        if root in index:
            continue
        work = [(root, iter(sorted(adj.get(root, {}))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            nxt = next(it, None)
            if nxt is None:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    comp = set()
                    while True:
                        w = stack.pop()
                        on.discard(w)
                        comp.add(w)
                        if w == v:
                            break
                    out.append(comp)
            elif nxt not in index:
                index[nxt] = low[nxt] = counter
                counter += 1
                stack.append(nxt)
                on.add(nxt)
                work.append((nxt, iter(sorted(adj.get(nxt, {})))))
            elif nxt in on:
                low[v] = min(low[v], index[nxt])
    return out


# --------------------------------------------------------------------------- results


@dataclass
class ContractResult:
    contract: Contract
    violations: list[Violation]
    stale_ignores: list[str]
    capped: bool

    def summary(self, known: set[str] | None = None) -> dict[str, Any]:
        known = known or set()
        new = [v for v in self.violations if v.key not in known]
        return {"name": self.contract.name, "type": self.contract.type, "severity": self.contract.severity,
                "origin": self.contract.origin, "violations": len(self.violations), "new": len(new),
                "known": len(self.violations) - len(new), "status": "fail" if new else "pass",
                "stale_ignores": self.stale_ignores, "capped": self.capped}


def check(snapshot: RepositorySnapshot, contracts: list[Contract]) -> list[ContractResult]:
    """Check every contract on ``snapshot`` (memoised per snapshot and contract set)."""
    if not contracts:
        return []
    memo_key = json.dumps([c.to_dict() for c in contracts], sort_keys=True)
    memo = snapshot.__dict__.setdefault("_contracts_results", {})
    if memo_key in memo:
        return memo[memo_key]
    g = graph_of(snapshot)
    results = []
    for c in contracts:
        ck = _check_one(c, g)
        ignores = [(entry, *[x.strip() for x in entry.split("->", 1)]) for entry in c.ignore]
        used: set[str] = set()
        kept = []
        for v in ck.out:
            hit = next((entry for entry, a, b in ignores if _ignored(v, a, b, g)), None)
            if hit:
                used.add(hit)
            else:
                kept.append(v)
        results.append(ContractResult(c, kept, [entry for entry, _a, _b in ignores if entry not in used], ck.capped))
    memo[memo_key] = results
    return results


def _ignored(v: Violation, a: str, b: str, g: _Graph) -> bool:
    src = g.nodes.get(v.source_id or "")
    dst = g.nodes.get(v.target_id or "")
    return bool(src and dst and matches(src.qualified_name, src.path, a) and matches(dst.qualified_name, dst.path, b))


def contracts_of(config: Any) -> list[Contract]:
    """``[[contracts]]`` plus the ``[[review.rules]]`` (forbidden contracts over path globs)."""
    out = list(config.contracts)
    for i, rule in enumerate(config.review_rules):
        out.append(Contract(name=f"review rule #{i + 1}" + (f": {rule.message}" if rule.message else ""),
                            type="forbidden", severity=rule.severity, source=list(rule.source),
                            target=list(rule.target), message=rule.message, origin="review.rules"))
    return out


# --------------------------------------------------------------------------- baseline


def load_baseline(text: str | None) -> tuple[set[str], str | None]:
    """The keys of the known violations, and a problem with the file (``None`` when fine or absent)."""
    if text is None:
        return set(), None
    try:
        data = json.loads(text)
    except ValueError as exc:
        return set(), f"not valid JSON ({exc})"
    if not isinstance(data, dict) or data.get("format") != BASELINE_FORMAT:
        return set(), f"not a repoviz baseline (expected \"format\": \"{BASELINE_FORMAT}\")"
    return {str(v.get("key")) for v in data.get("violations", []) if isinstance(v, dict) and v.get("key")}, None


def baseline_json(results: list[ContractResult]) -> str:
    """The baseline to commit (printed, never written by repoviz)."""
    items = sorted(({"key": v.key, "contract": v.contract, "type": v.type, "source": v.source, "target": v.target}
                    for r in results for v in r.violations), key=lambda x: x["key"])
    return json.dumps({"format": BASELINE_FORMAT, "version": 1, "violations": items}, indent=2) + "\n"


def report(results: list[ContractResult], known: set[str], baseline_problem: str | None = None,
           baseline_path: str | None = None) -> dict[str, Any]:
    """Everything the CLI, the API and the web app show: per contract, violations (new and known), fixed ones."""
    current = {v.key for r in results for v in r.violations}
    return {
        "contracts": [r.summary(known) for r in results],
        "violations": [dict(v.to_dict(), known=v.key in known) for r in results for v in r.violations],
        "fixed": sorted(k for k in known if k not in current and k.split("::", 1)[0] in {r.contract.name for r in results}),
        "baseline": {"path": baseline_path, "known": len(known), "problem": baseline_problem},
        "new": sum(1 for r in results for v in r.violations if v.key not in known),
    }


def layer_groups(snapshot: RepositorySnapshot, contracts: list[Contract]) -> list[dict[str, Any]]:
    """For the Dependencies tab: the first layers contract's layers, and which nodes (at any level) sit in each."""
    c = next((c for c in contracts if c.type == "layers" and not c.containers), None)
    if c is None:
        return []
    nodes: dict[str, int] = {}
    for n in (*snapshot.components, *snapshot.modules):
        if n.category == "symbol" or "external" in n.tags:
            continue
        for i, pattern in enumerate(c.layers):
            if matches(n.qualified_name, n.path, pattern):
                nodes[n.id] = i
                break
    return [{"contract": c.name, "layers": list(c.layers), "nodes": nodes}]


# --------------------------------------------------------------------------- SARIF and suggestions


def sarif(rep: dict[str, Any], version: str) -> dict[str, Any]:
    """SARIF 2.1.0 for code-scanning tools: one rule per contract, one result per new violation."""
    rules = [{"id": c["name"], "shortDescription": {"text": f"{c['type']} contract: {c['name']}"},
              "defaultConfiguration": {"level": SEVERITY_LEVEL.get(c["severity"], "warning")}}
             for c in rep["contracts"]]
    results = []
    for v in rep["violations"]:
        if v["known"]:
            continue
        loc = {"physicalLocation": {"artifactLocation": {"uri": v["path"] or ""}}}
        if v["line"]:
            loc["physicalLocation"]["region"] = {"startLine": v["line"]}
        results.append({"ruleId": v["contract"], "level": SEVERITY_LEVEL.get(v["severity"], "warning"),
                        "message": {"text": v["detail"]}, "locations": [loc] if v["path"] else [],
                        "partialFingerprints": {"repovizViolation": v["key"]}})
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": "repoviz", "version": version, "rules": rules}},
                      "results": results}]}


def suggest_layers(snapshot: RepositorySnapshot) -> str:
    """A ``layers`` contract (TOML) from the current imports between packages, for the user to review and paste.

    Groups modules by their name prefix at the shallowest depth that gives at least three groups (``app.routes``,
    ``app.services``…), orders them so each one only imports the ones after it, or says which ones form a cycle."""
    g = graph_of(snapshot)
    names = {nid: g.name(nid) for nid in g.modules()}

    def parts(q: str) -> list[str]:
        return q.split("/") if "/" in q else q.split(".")

    groups: dict[str, str] = {}
    for depth in (1, 2, 3, 4):
        groups = {nid: ("/" if "/" in q else ".").join(parts(q)[:depth]) for nid, q in names.items()
                  if len(parts(q)) > depth}
        if len(set(groups.values())) >= 3:
            break
    adj: dict[str, set[str]] = {grp: set() for grp in set(groups.values())}
    for m, edges in g.out.items():
        for d, _e in edges:
            a, b = groups.get(m), groups.get(d)
            if a and b and a != b:
                adj[a].add(b)
    if len(adj) < 2 or not any(adj.values()):
        return "# Not enough packages with imports between them to suggest layers.\n"
    cyclic = [sorted(x) for x in _sccs({k: dict.fromkeys(v) for k, v in adj.items()}) if len(x) > 1]
    if cyclic:
        return ("# These packages import each other in a cycle, so they cannot be layered yet:\n"
                + "".join(f"#   {' ⇄ '.join(c)}\n" for c in cyclic)
                + "# Break the cycle first, or start with an acyclic contract over them:\n"
                + "[[contracts]]\nname = \"No package cycles (suggested)\"\ntype = \"acyclic\"\n"
                + f"modules = {json.dumps(sorted({x for c in cyclic for x in c}))}\n")
    indeg = {grp: 0 for grp in adj}
    for a in adj:
        for b in adj[a]:
            indeg[b] += 1
    ready = sorted(grp for grp, d in indeg.items() if d == 0)
    order = []
    while ready:  # Kahn's algorithm: importers first (high), ties by name
        grp = ready.pop(0)
        order.append(grp)
        for b in sorted(adj[grp]):
            indeg[b] -= 1
            if indeg[b] == 0:
                ready.append(b)
                ready.sort()
    return ("# Suggested from the current imports: each package only imports the ones after it.\n"
            "# Packages that do not import each other were ordered by name; review the order before using it.\n"
            "[[contracts]]\nname = \"Layers (suggested)\"\ntype = \"layers\"\n"
            f"layers = {json.dumps(order)}  # high → low\n")
