"""Graph queries over one snapshot: why A depends on B, and what a change can reach.

* :func:`why`: the shortest chains that make A depend on B: calls between two symbols, else imports between
  the modules A and B stand for (a component, a package, a module or a symbol's module), each hop with its
  evidence (file, line, code).
* :func:`blast_radius`: everything that uses a node, transitively: callers (reversed ``calls`` and
  ``invokes``) and importers (reversed ``imports``), ranked by distance and then by how many use them, with the
  entry points and tests reached.  For a function it walks the same edges as
  :func:`~repoviz.flow.affected_flow`, so both find the same entry points and tests.

Used by ``repoviz why`` / ``repoviz impact``, the server (``/api/path``, ``/api/impact``) and the MCP server.
``web/app.js`` mirrors both (``whyPaths``, ``blastRadius``) so a static report answers the same questions.
All walks are bounded: at most ``MAX_PATHS`` chains of ``MAX_LEN`` hops, and ``MAX_NODES`` nodes visited.
"""

from __future__ import annotations

import posixpath
import re
from collections import deque
from typing import Any

from .graph import shortest_paths
from .ids import make_id
from .model import CATEGORY_MODULE, CATEGORY_SYMBOL, REL_CALLS, REL_IMPORTS, REL_INVOKES, RepositorySnapshot
from .redact import redact

MAX_PATHS = 5
MAX_LEN = 8
MAX_NODES = 5_000
FILE_LIKE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
HOW = {REL_IMPORTS: "imports", REL_CALLS: "calls", REL_INVOKES: "runs"}


class QueryError(ValueError):
    """A name that matches nothing (or too much), or a question that has no answer in the analyzed code."""


def _loc(path: str | None, line: int | None) -> str | None:
    return f"{path}:{line}" if path and line else path


def evidence(edge: Any) -> dict[str, Any]:
    """The first place an edge comes from: ``{"evidence": "file:line", "code": excerpt}`` (redacted)."""
    ev = edge.evidence[0] if edge is not None and edge.evidence else None
    if ev is None:
        return {}
    out: dict[str, Any] = {"evidence": _loc(ev.path, ev.start_line)}
    if ev.excerpt:
        out["code"] = redact(ev.excerpt)[:200]
    return out


def describe(n: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"name": n.qualified_name, "kind": n.component_type}
    if n.path:
        out["at"] = _loc(n.path, n.start_line)
    return out


class GraphIndex:
    """Lookups over one snapshot, built once per snapshot."""

    def __init__(self, snap: RepositorySnapshot) -> None:
        from .activity import nodes_by_path

        self.snap = snap
        self.nodes = snap.node_index()
        self.by_path = nodes_by_path(snap)
        self.by_name: dict[str, list[Any]] = {}
        for n in snap.nodes():
            self.by_name.setdefault(n.qualified_name, []).append(n)
        # who uses X: X -> {user: edge}, over direct imports, calls and entry-point invocations; and apart, who
        # calls or runs X, and who imports X (the two walks of a blast radius)
        self.users: dict[str, dict[str, Any]] = {}
        self.callers: dict[str, dict[str, Any]] = {}
        self.importers: dict[str, dict[str, Any]] = {}
        # module imports (internal, direct) and symbol calls: the adjacency of `why`
        self.imports: dict[str, set[str]] = {}
        self.import_edge: dict[tuple[str, str], Any] = {}
        self.calls: dict[str, set[str]] = {}
        self.call_edge: dict[tuple[str, str], Any] = {}
        for e in snap.edges():
            if not e.direct or e.source_id == e.target_id or e.relationship not in (REL_IMPORTS, REL_CALLS, REL_INVOKES):
                continue
            self.users.setdefault(e.target_id, {}).setdefault(e.source_id, e)
            (self.importers if e.relationship == REL_IMPORTS else self.callers).setdefault(
                e.target_id, {}).setdefault(e.source_id, e)
            if e.relationship == REL_IMPORTS:
                if e.target_id in self.nodes and "external" not in self.nodes[e.target_id].tags:
                    self.imports.setdefault(e.source_id, set()).add(e.target_id)
                    self.import_edge.setdefault((e.source_id, e.target_id), e)
            else:
                self.calls.setdefault(e.source_id, set()).add(e.target_id)
                self.call_edge.setdefault((e.source_id, e.target_id), e)
        self.children: dict[str, list[str]] = {}
        for n in snap.nodes():
            if n.parent_id:
                self.children.setdefault(n.parent_id, []).append(n.id)

    # -- lookups ------------------------------------------------------------------------------------------------

    def dir_node(self, path: str) -> Any:
        return self.nodes.get(make_id("dir", f"path:dir:{path}"))

    def nearest_dir(self, path: str) -> Any:
        parts = path.split("/")[:-1]
        for i in range(len(parts), -1, -1):
            d = self.dir_node("/".join(parts[:i]))
            if d is not None:
                return d
        return None

    def component(self, node: Any) -> Any:
        for _ in range(64):
            if node is None:
                return None
            cid = node.metadata.get("component_id") or (node.id if "component" in node.tags else None)
            if cid and cid in self.nodes:
                return self.nodes[cid]
            node = self.nodes.get(node.parent_id or "")
        return None

    def module_of(self, node: Any) -> Any:
        for _ in range(64):
            if node is None or node.category != CATEGORY_SYMBOL:
                return node
            node = self.nodes.get(node.parent_id or "")
        return None

    def modules_under(self, node: Any) -> list[str]:
        """The modules a node stands for: itself, its module (a symbol), or every module below it."""
        if node.category == CATEGORY_MODULE:
            return [node.id]
        if node.category == CATEGORY_SYMBOL:
            m = self.module_of(node)
            return [m.id] if m is not None else []
        out, stack = [], [node.id]
        while stack and len(out) < MAX_NODES:
            for c in self.children.get(stack.pop(), ()):
                n = self.nodes[c]
                if n.category == CATEGORY_MODULE:
                    out.append(c)
                elif n.category != CATEGORY_SYMBOL:
                    stack.append(c)
        return sorted(out)

    def symbols_under(self, node_ids: list[str]) -> list[str]:
        """Every symbol inside the given nodes (methods of a class, functions of a module…)."""
        out, stack = [], list(node_ids)
        while stack and len(out) < MAX_NODES:
            for c in self.children.get(stack.pop(), ()):
                if self.nodes[c].category == CATEGORY_SYMBOL:
                    out.append(c)
                    stack.append(c)
        return out

    def resolve(self, text: str) -> Any:
        """A node from a qualified name, a repository-relative path, or a unique name suffix.  A name shared by a
        package and its ``__init__`` module means the package (everything in it)."""
        t = text.strip()
        if t in self.nodes:  # a node ID (what the web app sends)
            return self.nodes[t]
        exact = self.by_name.get(t)
        if exact:
            return sorted(exact, key=lambda n: ({"component": 0, "module": 1}.get(n.category, 2), n.id))[0]
        if "/" in t or FILE_LIKE.search(t) or t.startswith("."):
            p = posixpath.normpath(t.lstrip("/")) if t not in ("", ".", "./") else ""
            p = "" if p == "." else p
            if p.startswith("../"):
                raise QueryError(f"{text!r} is outside the repository")
            n = self.by_path.get(p) or self.dir_node(p)
            if n is not None:
                return n
            if "/" in t or t.startswith("."):
                raise QueryError(f"no file or directory {p!r} in the analyzed tree (excluded, ignored or new?)")
        suffix = [n for name, ns in self.by_name.items() if name.endswith("." + t) or name.endswith(":" + t)
                  or name.endswith("/" + t) for n in ns]
        if len({n.id for n in suffix}) == 1:
            return suffix[0]
        if suffix:
            names = sorted({n.qualified_name for n in suffix})
            raise QueryError(f"{t!r} is ambiguous ({len(names)} matches): " + ", ".join(names[:8])
                             + (" …" if len(names) > 8 else "") + "; pass the qualified name")
        raise QueryError(f"nothing named {t!r}; pass a repository-relative path or a qualified name such as "
                         "`package.module.function`")


# --------------------------------------------------------------------------- why A depends on B

def _step(idx: GraphIndex, nid: str, edge: Any = None) -> dict[str, Any]:
    n = idx.nodes[nid]
    out: dict[str, Any] = {"id": nid, "name": n.qualified_name, "path": n.path, "line": n.start_line}
    if edge is not None:
        out["how"] = HOW.get(edge.relationship, edge.relationship)
        out.update(evidence(edge))
    return out


def why(idx: GraphIndex, a: Any, b: Any, *, max_paths: int = MAX_PATHS, max_len: int = MAX_LEN) -> dict[str, Any]:
    """Why ``a`` depends on ``b``: up to ``max_paths`` shortest chains of at most ``max_len`` hops.

    Two symbols are joined by calls when a call chain exists; otherwise (and for anything else) the chains run
    over imports between the modules each side stands for.  Each hop after the first carries its evidence.  With
    no chain, ``reverse_paths`` says whether ``b`` depends on ``a`` instead."""
    max_paths, max_len = max(1, min(max_paths, MAX_PATHS)), max(1, min(max_len, MAX_LEN))

    def chains(src: list[str], dst: list[str], adjacency: dict[str, set[str]],
               edges: dict[tuple[str, str], Any]) -> list[list[dict[str, Any]]]:
        return [[_step(idx, p[0])] + [_step(idx, t, edges.get((s, t))) for s, t in zip(p, p[1:])]
                for p in shortest_paths(src, dst, adjacency, max_paths=max_paths, max_depth=max_len)]

    level, found, back = "imports", [], []
    if a.category == CATEGORY_SYMBOL and b.category == CATEGORY_SYMBOL:
        found = chains([a.id], [b.id], idx.calls, idx.call_edge)
        level = "calls" if found else "imports"
    starts, goals = idx.modules_under(a), idx.modules_under(b)
    if not found:
        if not starts or not goals:
            raise QueryError("both ends must be (or contain) modules of the analyzed code")
        goals = [g for g in goals if g not in set(starts)] or goals
        found = chains(starts, goals, idx.imports, idx.import_edge)
        if not found:
            back = chains(goals, starts, idx.imports, idx.import_edge)
    data: dict[str, Any] = {"source": {**describe(a), "id": a.id}, "target": {**describe(b), "id": b.id},
                            "level": level, "paths": found, "max_paths": max_paths, "max_len": max_len}
    if found:
        hops = len(found[0]) - 1
        data["summary"] = (f"{a.qualified_name} depends on {b.qualified_name}: {len(found)} shortest chain(s) of "
                           f"{hops} {'call' if level == 'calls' else 'import'}{'s' if hops != 1 else ''}"
                           + (" (direct)" if hops == 1 else "") + ".")
    else:
        data["reverse_paths"] = back
        data["summary"] = (f"{a.qualified_name} does not depend on {b.qualified_name} (within {max_len} imports)"
                           + (f"; but {b.qualified_name} depends on {a.qualified_name}." if back else "."))
    return data


# --------------------------------------------------------------------------- blast radius

def blast_radius(idx: GraphIndex, node: Any, *, depth: int | None = None, max_items: int = 100) -> dict[str, Any]:
    """What may break when ``node`` changes: its dependents with their distance (1 = uses it directly), ranked
    by distance and then by fan-in (how many use them), plus the entry points and tests reached.

    A symbol (and the symbols inside it) is followed through callers; a module or a directory also through
    importers of its modules.  ``depth`` bounds the walk (``None``: as far as it goes, up to ``MAX_NODES``)."""
    if node.category == CATEGORY_SYMBOL:
        seed_syms, seed_mods = [node.id] + idx.symbols_under([node.id]), []
    else:
        seed_mods = idx.modules_under(node)
        seed_syms = idx.symbols_under(seed_mods)
    seeds = set(seed_syms) | set(seed_mods)
    dist: dict[str, int] = {s: 0 for s in seeds}
    via: dict[str, tuple[str, Any]] = {}  # dependent -> (the node it uses, the edge)
    capped = False
    for starts, users in ((seed_syms, idx.callers), (seed_mods, idx.importers)):
        queue = deque(sorted(starts))
        while queue:
            cur = queue.popleft()
            if depth is not None and dist[cur] >= depth:
                continue
            for user, edge in sorted(users.get(cur, {}).items()):
                if user not in idx.nodes:
                    continue
                if user not in dist:
                    if len(dist) >= MAX_NODES:
                        capped = True
                        break
                    dist[user], via[user] = dist[cur] + 1, (cur, edge)
                    queue.append(user)
    # a symbol's module is imported by code that may use it without a resolved call (modules already reached
    # through one of their functions are not repeated)
    module_importers: list[str] = []
    if node.category == CATEGORY_SYMBOL:
        m = idx.module_of(node)
        reached_modules = {mm.id for n in dist if (mm := idx.module_of(idx.nodes[n])) is not None}
        if m is not None:
            module_importers = sorted(u for u in idx.importers.get(m.id, {})
                                      if u not in dist and u not in reached_modules and u in idx.nodes)
    fan_in = {n: len(idx.users.get(n, {})) for n in dist}
    reached = sorted((n for n in dist if n not in seeds),
                     key=lambda n: (dist[n], -fan_in[n], idx.nodes[n].qualified_name))

    def chain(nid: str) -> list[str]:
        out = [nid]
        while out[-1] in via and len(out) < 64:
            out.append(via[out[-1]][0])
        return out

    def item(nid: str) -> dict[str, Any]:
        n = idx.nodes[nid]
        m = idx.module_of(n)
        comp = idx.component(m)
        out: dict[str, Any] = {"id": nid, "name": n.qualified_name, "kind": n.component_type, "category": n.category,
                               "path": n.path, "line": n.start_line, "distance": dist[nid], "fan_in": fan_in[nid],
                               "module": m.qualified_name if m is not None else None,
                               "component": comp.qualified_name if comp is not None else None}
        if nid in via:
            target, edge = via[nid]
            out["uses"] = target
            out["how"] = HOW.get(edge.relationship, edge.relationship)
            out.update(evidence(edge))
        return out

    entries = [n for n in reached if "entry-point" in idx.nodes[n].tags]
    entry_points = [n for n in entries if "test" not in idx.nodes[n].tags]
    tests = [n for n in reached if "test" in idx.nodes[n].tags
             and ("entry-point" in idx.nodes[n].tags or idx.nodes[n].category == CATEGORY_MODULE)]
    users = [n for n in reached if "test" not in idx.nodes[n].tags]
    seed_modules = {m.id for s in seeds if (m := idx.module_of(idx.nodes[s])) is not None}
    modules = {m.id for n in users if (m := idx.module_of(idx.nodes[n])) is not None} - seed_modules
    components = {c.id for mid in modules if (c := idx.component(idx.nodes[mid])) is not None}
    test_files = sorted({idx.nodes[n].path for n in tests if idx.nodes[n].path})
    totals = {"dependents": len(users), "modules": len(modules), "components": len(components),
              "entry_points": len(entry_points), "tests": len(tests), "test_files": len(test_files)}
    k = max(1, max_items)
    data: dict[str, Any] = {
        "target": {**describe(node), "id": node.id, "category": node.category},
        "depth": depth, "seeds": len(seeds), "totals": totals,
        "dependents": [item(n) for n in users[:k]],
        "entry_points": [{**item(n), "chain": chain(n)} for n in entry_points[:k]],
        "tests": [{**item(n), "chain": chain(n)} for n in tests[:k]],
        "test_files": test_files[:k],
        "importers_of_its_module": [describe(idx.nodes[n]) for n in module_importers
                                    if "test" not in idx.nodes[n].tags][:k],
    }
    if capped:
        data["capped"] = f"stopped after {MAX_NODES} nodes"
    if len(users) > k or len(entry_points) > k or len(tests) > k:
        data["truncated"] = f"lists cut at {k} items (totals count everything)"
    t = totals
    data["summary"] = (f"Changing {node.qualified_name} can affect {t['modules']} module{'s' if t['modules'] != 1 else ''}"
                       f" in {t['components']} component{'s' if t['components'] != 1 else ''}, "
                       f"{t['entry_points']} entry point{'s' if t['entry_points'] != 1 else ''}, "
                       f"{t['tests']} test{'s' if t['tests'] != 1 else ''}.")
    if not reached and not module_importers:
        data["summary"] += (" Nothing in the analyzed code uses it (dynamic uses, such as getattr or string imports, "
                            "are not seen).")
    return data
