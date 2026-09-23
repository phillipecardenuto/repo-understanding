"""Affected execution flow.

Given a :class:`~repoviz.model.RepositoryDiff`, find the code whose behaviour
may be affected by the change:

* the innermost changed symbols (added, modified, removed);
* their callers, transitively, up to ``max_up`` levels (``calls`` edges
  reversed), and their direct callees;
* the entry points (``__main__`` blocks, console scripts, handlers, container
  commands) and tests that can reach a changed symbol, with one shortest path
  each.

For languages without call data the analysis falls back to module level:
changed modules and the modules that (transitively) import them.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .diff import symbol_changes
from .model import (
    ADDED,
    CATEGORY_MODULE,
    CATEGORY_SYMBOL,
    MODIFIED,
    REL_CALLS,
    REL_IMPORTS,
    REL_INVOKES,
    REMOVED,
    RepositoryDiff,
)


@dataclass
class FlowResult:
    mode: str  # symbols | modules | none
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    entry_points: list[dict[str, Any]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _is_entry(node: Any) -> bool:
    return "entry-point" in node.tags


def _is_test(node: Any) -> bool:
    return "test" in node.tags


def affected_flow(diff: RepositoryDiff, *, max_up: int = 4, max_down: int = 1, max_nodes: int = 160,
                  max_entry_points: int = 25) -> FlowResult:
    nodes = diff.nodes
    sym = symbol_changes(diff)
    changed_symbols = sym[ADDED] + sym[MODIFIED] + sym[REMOVED]
    changed_modules = [nid for nid, c in nodes.items() if c.node.category == CATEGORY_MODULE
                       and c.status in (ADDED, REMOVED, MODIFIED)
                       and c.reasons not in (["contents changed"], ["formatting or comments only"])]
    has_symbols = {c.node.parent_id for c in nodes.values() if c.node.category == CATEGORY_SYMBOL}

    call_edges = [c for c in diff.edges.values() if c.edge.relationship in (REL_CALLS, REL_INVOKES)]
    # Modules changed only at module level (no changed symbol inside) are flow roots themselves.
    modules_with_changed_symbols = set()
    for sid in changed_symbols:
        cur = nodes[sid].node
        while cur is not None and cur.category == CATEGORY_SYMBOL:
            cur = nodes[cur.parent_id].node if cur.parent_id in nodes else None
        if cur is not None:
            modules_with_changed_symbols.add(cur.id)
    call_sources = {c.edge.source_id for c in call_edges}
    module_roots = [m for m in changed_modules if m not in modules_with_changed_symbols
                    and (m in has_symbols or m in call_sources)]
    roots = changed_symbols + module_roots
    notes: list[str] = []
    if len(roots) > max_nodes:
        notes.append(f"{len(roots)} symbols changed; the flow graph shows the first {max_nodes}. Narrow the "
                     "comparison to see the complete affected flow.")
        roots = roots[:max_nodes]

    if not roots and not changed_modules:
        return FlowResult(mode="none", notes=["No code changes."])
    if not call_edges or not roots:
        return _module_flow(diff, changed_modules, max_up, max_nodes)

    forward: dict[str, set[str]] = {}
    reverse: dict[str, set[str]] = {}
    for c in call_edges:
        forward.setdefault(c.edge.source_id, set()).add(c.edge.target_id)
        reverse.setdefault(c.edge.target_id, set()).add(c.edge.source_id)

    # Full reverse BFS (bounded) to find entry points and tests reaching the change.
    dist: dict[str, int] = {r: 0 for r in roots}
    parent: dict[str, str] = {}
    queue = deque(roots)
    while queue and len(dist) < 20000:
        cur = queue.popleft()
        for caller in reverse.get(cur, ()):
            if caller not in dist:
                dist[caller] = dist[cur] + 1
                parent[caller] = cur
                queue.append(caller)

    result = FlowResult(mode="symbols", changed=list(roots), notes=notes, truncated=bool(notes))
    selected: dict[str, str] = {r: "changed" for r in roots}
    entries = sorted((nid for nid in dist if nid in nodes and _is_entry(nodes[nid].node)),
                     key=lambda n: (dist[n], nodes[n].node.qualified_name))
    for nid in entries:
        path = [nid]
        while path[-1] in parent:
            path.append(parent[path[-1]])
        info = {"id": nid, "name": nodes[nid].node.qualified_name, "distance": dist[nid], "path": path,
                "reaches": path[-1], "kind": nodes[nid].node.metadata.get("entry_kind", "entry point")}
        (result.tests if _is_test(nodes[nid].node) else result.entry_points).append(info)
    for info in (result.entry_points[:max_entry_points] + result.tests[:max_entry_points]):
        for i, nid in enumerate(info["path"]):
            if nid not in selected:
                selected[nid] = "test" if (i == 0 and _is_test(nodes[nid].node)) else ("entry" if i == 0 else "path")
    for nid, d in sorted(dist.items(), key=lambda kv: kv[1]):
        if 0 < d <= max_up and nid not in selected and len(selected) < max_nodes:
            selected[nid] = "caller"
    if len(dist) > len(selected):
        result.truncated = result.truncated or any(0 < d <= max_up for n, d in dist.items() if n not in selected)
    if max_down > 0:
        frontier = list(roots)
        for _ in range(max_down):
            nxt = []
            for cur in frontier:
                for callee in sorted(forward.get(cur, ())):
                    if callee not in selected and len(selected) < max_nodes:
                        selected[callee] = "callee"
                        nxt.append(callee)
            frontier = nxt
    for nid in list(selected):
        if selected[nid] in ("caller", "path", "callee") and _is_entry(nodes[nid].node):
            selected[nid] = "test" if _is_test(nodes[nid].node) else "entry"

    for nid, role in selected.items():
        if nid in nodes:
            result.nodes.append(_node_info(diff, nid, role, dist.get(nid)))
    for c in call_edges:
        if c.edge.source_id in selected and c.edge.target_id in selected:
            result.edges.append({"id": c.edge.id, "source": c.edge.source_id, "target": c.edge.target_id,
                                 "status": c.status, "relationship": c.edge.relationship,
                                 "confidence": round(c.edge.confidence, 2)})
    if module_roots:
        result.notes.append(f"{len(module_roots)} module(s) changed outside any function or class (module-level code).")
    return result


def _node_info(diff: RepositoryDiff, nid: str, role: str, distance: int | None) -> dict[str, Any]:
    change = diff.nodes[nid]
    node = change.node
    module_id = node.id
    cur = node
    while cur.category == CATEGORY_SYMBOL and cur.parent_id in diff.nodes:
        cur = diff.nodes[cur.parent_id].node
    module_id = cur.id
    return {"id": nid, "name": node.name, "qualified_name": node.qualified_name, "kind": node.component_type,
            "category": node.category, "path": node.path, "line": node.start_line, "status": change.status,
            "reasons": change.reasons, "role": role, "distance": distance, "module_id": module_id,
            "module": cur.qualified_name, "tags": node.tags}


def _module_flow(diff: RepositoryDiff, changed_modules: list[str], max_up: int, max_nodes: int) -> FlowResult:
    nodes = diff.nodes
    import_edges = [c for c in diff.edges.values() if c.edge.relationship == REL_IMPORTS and c.edge.direct
                    and c.edge.source_id in nodes and c.edge.target_id in nodes
                    and "external" not in nodes[c.edge.target_id].node.tags]
    reverse: dict[str, set[str]] = {}
    for c in import_edges:
        if c.status != REMOVED:
            reverse.setdefault(c.edge.target_id, set()).add(c.edge.source_id)
    result = FlowResult(mode="modules", changed=list(changed_modules))
    result.notes.append("Call-level data is not available for the changed code; showing modules that import the "
                        "changed modules instead.")
    dist = {m: 0 for m in changed_modules}
    queue = deque(changed_modules)
    while queue:
        cur = queue.popleft()
        if dist[cur] >= max_up:
            continue
        for importer in reverse.get(cur, ()):
            if importer not in dist:
                dist[importer] = dist[cur] + 1
                queue.append(importer)
    ordered = sorted(dist, key=lambda n: (dist[n], nodes[n].node.qualified_name))
    selected = ordered[:max_nodes]
    result.truncated = len(ordered) > len(selected)
    sel = set(selected)
    for nid in selected:
        node = nodes[nid].node
        role = "changed" if dist[nid] == 0 else ("test" if _is_test(node) else ("entry" if _is_entry(node) else "caller"))
        result.nodes.append(_node_info(diff, nid, role, dist[nid]))
        if dist[nid] and _is_test(node):
            result.tests.append({"id": nid, "name": node.qualified_name, "distance": dist[nid], "path": [nid],
                                 "reaches": None, "kind": "test module"})
        elif dist[nid] and _is_entry(node):
            result.entry_points.append({"id": nid, "name": node.qualified_name, "distance": dist[nid], "path": [nid],
                                        "reaches": None, "kind": node.metadata.get("entry_kind", "entry point")})
    for c in import_edges:
        if c.edge.source_id in sel and c.edge.target_id in sel:
            result.edges.append({"id": c.edge.id, "source": c.edge.source_id, "target": c.edge.target_id,
                                 "status": c.status, "relationship": REL_IMPORTS,
                                 "confidence": round(c.edge.confidence, 2)})
    return result

