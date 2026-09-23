"""Graph algorithms over the normalized model (language independent)."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Callable, Iterable

from .ids import cycle_id
from .model import Cycle, DependencyEdge


def strongly_connected_components(nodes: Iterable[str], adjacency: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Tarjan: returns SCCs (each sorted) in a deterministic order."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0
    for root in sorted(set(nodes)):
        if root in index:
            continue
        work = [(root, iter(sorted(adjacency.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(adjacency.get(nxt, ())))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                result.append(sorted(comp))
    return sorted(result, key=lambda c: (-len(c), c))


def shortest_cycle(members: list[str], adjacency: dict[str, set[str]]) -> list[str]:
    """A shortest cycle through the first member, restricted to the SCC."""
    allowed = set(members)
    start = members[0]
    if start in adjacency.get(start, set()):
        return [start, start]
    parent: dict[str, str] = {}
    queue = deque([start])
    seen = {start}
    while queue:
        cur = queue.popleft()
        for nxt in sorted(adjacency.get(cur, ())):
            if nxt not in allowed:
                continue
            if nxt == start:
                path = [cur]
                while path[-1] != start:
                    path.append(parent[path[-1]])
                return list(reversed(path)) + [start]
            if nxt not in seen:
                seen.add(nxt)
                parent[nxt] = cur
                queue.append(nxt)
    return members + [start]


def find_cycles(edges: Iterable[DependencyEdge], level: str, relationship: str,
                include: Callable[[DependencyEdge], bool] | None = None) -> list[Cycle]:
    """Detect cycles among ``edges`` and mark each participating edge's ``cycle_ids``."""
    selected = [e for e in edges if include is None or include(e)]
    adjacency: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set()
    for e in selected:
        adjacency[e.source_id].add(e.target_id)
        nodes.add(e.source_id)
        nodes.add(e.target_id)
    cycles: list[Cycle] = []
    for comp in strongly_connected_components(nodes, adjacency):
        if len(comp) == 1 and comp[0] not in adjacency.get(comp[0], set()):
            continue
        members = set(comp)
        cid = cycle_id(level, comp)
        cycle_edges = []
        for e in selected:
            if e.source_id in members and e.target_id in members:
                if cid not in e.cycle_ids:
                    e.cycle_ids.append(cid)
                cycle_edges.append(e.id)
        cycles.append(Cycle(id=cid, level=level, relationship=relationship, members=comp,
                            edge_ids=sorted(cycle_edges), example_path=shortest_cycle(comp, adjacency)))
    return cycles


def aggregate(edges: Iterable[DependencyEdge], group_of: Callable[[str], str | None],
              max_evidence: int = 20) -> dict[tuple[str, str], dict[str, object]]:
    """Group edges by ``(group_of(source), group_of(target))``, dropping self-edges."""
    out: dict[tuple[str, str], dict[str, object]] = {}
    for e in edges:
        s, t = group_of(e.source_id), group_of(e.target_id)
        if s is None or t is None or s == t:
            continue
        agg = out.setdefault((s, t), {"occurrences": 0, "evidence": [], "edges": [], "flags": {}, "confidence": 0.0})
        agg["occurrences"] = int(agg["occurrences"]) + e.occurrences  # type: ignore[operator]
        agg["edges"].append(e.id)  # type: ignore[union-attr]
        agg["confidence"] = max(float(agg["confidence"]), e.confidence)  # type: ignore[arg-type]
        evidence = agg["evidence"]
        for ev in e.evidence:
            if len(evidence) < max_evidence:  # type: ignore[arg-type]
                evidence.append(ev)  # type: ignore[union-attr]
        flags = agg["flags"]
        for flag in ("type_checking_only", "conditional_only", "lazy_only", "dynamic_only", "test_only"):
            flags[flag] = flags.get(flag, True) and bool(e.metadata.get(flag))  # type: ignore[union-attr]
    return out


def reachable(starts: Iterable[str], adjacency: dict[str, set[str]], max_depth: int | None = None) -> dict[str, int]:
    """Breadth-first distances from ``starts``."""
    dist: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque()
    for s in starts:
        dist[s] = 0
        queue.append((s, 0))
    while queue:
        cur, d = queue.popleft()
        if max_depth is not None and d >= max_depth:
            continue
        for nxt in adjacency.get(cur, ()):
            if nxt not in dist:
                dist[nxt] = d + 1
                queue.append((nxt, d + 1))
    return dist


def adjacency_of(edges: Iterable[DependencyEdge], reverse: bool = False) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        if reverse:
            adj[e.target_id].add(e.source_id)
        else:
            adj[e.source_id].add(e.target_id)
    return adj
