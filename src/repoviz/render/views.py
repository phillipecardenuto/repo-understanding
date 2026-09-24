"""Model -> view graphs (independent of Mermaid).

A view graph is a small list of nodes and edges, each with a status and a
role, ready to be serialized by :mod:`repoviz.render.mermaid`.  The browser
UI implements the same rules in JavaScript for interactive filtering.

Aggregation levels are generic, derived from the containment tree:

``module``    files / importable units
``package``   the directory or package that contains the module
``component`` the innermost discovered component (top-level package, project,
              workspace member, configured component, or top-level directory)
``project``   the innermost project (directory with a manifest)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..graph import strongly_connected_components
from ..model import (
    ADDED,
    CATEGORY_MODULE,
    CATEGORY_SYMBOL,
    MODIFIED,
    REL_DEPENDS_ON,
    REL_IMPORTS,
    REMOVED,
    UNCHANGED,
    ComponentNode,
    RepositoryDiff,
    RepositorySnapshot,
)

LEVELS = ("component", "package", "module", "project")
CONTAINER_TYPES = {"directory", "package", "namespace-package", "repository", "project", "workspace-member",
                   "workspace"}
DEFAULT_RELATIONSHIPS = (REL_IMPORTS, REL_DEPENDS_ON)


@dataclass
class VNode:
    id: str
    label: str
    sublabel: str = ""
    status: str = UNCHANGED
    kind: str = "module"  # style class for non-diff views
    shape: str = "box"  # box | round | stadium | hexagon | cylinder | subroutine
    parent: str | None = None  # subgraph id
    icon: str = ""
    reasons: list[str] = field(default_factory=list)


@dataclass
class VEdge:
    source: str
    target: str
    status: str = UNCHANGED
    cycle: bool = False
    cycle_introduced: bool = False
    count: int = 1
    relationship: str = REL_IMPORTS


@dataclass
class ViewGraph:
    title: str
    direction: str = "LR"
    nodes: list[VNode] = field(default_factory=list)
    edges: list[VEdge] = field(default_factory=list)
    subgraphs: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    truncated: int = 0
    mode: str = "diff"  # diff | kind | role


class Grouper:
    """Maps any node ID to its representative at an aggregation level."""

    def __init__(self, nodes: dict[str, ComponentNode], level: str, include_external: bool) -> None:
        if level not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")
        self.nodes = nodes
        self.level = level
        self.include_external = include_external
        self._memo: dict[str, str | None] = {}

    def module_of(self, nid: str) -> ComponentNode | None:
        node = self.nodes.get(nid)
        while node is not None and node.category == CATEGORY_SYMBOL and node.parent_id:
            node = self.nodes.get(node.parent_id)
        return node

    def group(self, nid: str) -> str | None:
        if nid in self._memo:
            return self._memo[nid]
        node = self.module_of(nid)
        result: str | None
        if node is None:
            result = None
        elif "external" in node.tags:
            result = node.id if self.include_external else None
        elif self.level == "module":
            result = node.id
        elif self.level == "package":
            result = node.parent_id if node.category == CATEGORY_MODULE and node.parent_id else node.id
        elif self.level == "component":
            result = node.metadata.get("component_id") or node.id
        else:
            result = node.metadata.get("project_id") or node.id
        if result is not None and result not in self.nodes:
            result = node.id if node else None
        self._memo[nid] = result
        return result


def default_icons() -> dict[str, str]:
    from .theme import theme

    return theme()["icons"]


def _icon(node: ComponentNode, icons: dict[str, str]) -> str:
    if "test" in node.tags and node.category != CATEGORY_SYMBOL:
        return "🧪"
    if "entry-point" in node.tags and node.component_type not in icons:
        return "🚀"
    return icons.get(node.component_type, "")


def _kind(node: ComponentNode) -> str:
    if "external" in node.tags:
        return "external"
    if "unsupported" in node.tags or node.metadata.get("dependency_details"):
        return "structural"
    if node.category == CATEGORY_MODULE or node.component_type in ("module", "file"):
        return "module"
    if "component" in node.tags or "project" in node.tags or node.component_type == "repository":
        return "component"
    return "package"


def _sublabel(node: ComponentNode, before: dict[str, Any] | None = None) -> str:
    parts = [node.component_type]
    if node.language:
        parts.append(node.language)
    if before and before.get("previous_id"):  # renamed or moved: say what it was
        if before.get("path") and before.get("path") != node.path:
            was = before.get("path")
        elif before.get("name") and before.get("name") != node.name:
            was = before.get("name")
        else:  # same name, new parent (its class or module was renamed)
            was = before.get("qualified_name") or before.get("name")
        parts.append(f"↦ was {was}")
    return " · ".join(parts)


def _scc_pairs(edges: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
    adj: dict[str, set[str]] = {}
    nodes: set[str] = set()
    for s, t in edges:
        adj.setdefault(s, set()).add(t)
        nodes |= {s, t}
    member: dict[str, int] = {}
    for i, comp in enumerate(strongly_connected_components(nodes, adj)):
        if len(comp) > 1:
            for n in comp:
                member[n] = i
    return {(s, t) for s, t in edges if s in member and member.get(s) == member.get(t)} | \
        {(s, t) for s, t in edges if s == t}


def changes_view(diff: RepositoryDiff, *, level: str = "component", scope: str = "neighbors",
                 relationships: Iterable[str] = DEFAULT_RELATIONSHIPS, include_external: bool = False,
                 max_nodes: int = 250, icons: dict[str, str] | None = None, hide_cosmetic: bool = True,
                 neighbor_limit: int = 25) -> ViewGraph:
    icons = default_icons() if icons is None else icons
    rels = set(relationships)
    nodes = {nid: c.node for nid, c in diff.nodes.items()}
    status = {nid: c.status for nid, c in diff.nodes.items()}
    reasons = {nid: c.reasons for nid, c in diff.nodes.items()}
    if hide_cosmetic:
        for nid, r in reasons.items():
            if status[nid] == MODIFIED and r == ["formatting or comments only"]:
                status[nid] = UNCHANGED
    grouper = Grouper(nodes, level, include_external)

    base_pairs: dict[tuple[str, str], int] = {}
    target_pairs: dict[tuple[str, str], int] = {}
    # Cycles follow the analysis default: type-checking-only imports do not form runtime cycles.
    base_runtime: set[tuple[str, str]] = set()
    target_runtime: set[tuple[str, str]] = set()
    changed_pairs: set[tuple[str, str]] = set()
    rel_of: dict[tuple[str, str], str] = {}
    for ch in diff.edges.values():
        e = ch.edge
        if not e.direct or e.relationship not in rels:
            continue
        s, t = grouper.group(e.source_id), grouper.group(e.target_id)
        if s is None or t is None or s == t:
            continue
        pair = (s, t)
        rel_of.setdefault(pair, e.relationship)
        base_flags = ch.base_flags if ch.status == MODIFIED else e.metadata
        if ch.status in (UNCHANGED, MODIFIED, REMOVED):
            base_pairs[pair] = base_pairs.get(pair, 0) + e.occurrences
            if not base_flags.get("type_checking_only"):
                base_runtime.add(pair)
        if ch.status in (UNCHANGED, MODIFIED, ADDED):
            target_pairs[pair] = target_pairs.get(pair, 0) + e.occurrences
            if not e.metadata.get("type_checking_only"):
                target_runtime.add(pair)
        if ch.status != UNCHANGED:
            changed_pairs.add(pair)
    base_cycles = _scc_pairs(base_runtime)
    target_cycles = _scc_pairs(target_runtime)

    edges: list[VEdge] = []
    for pair in sorted(set(base_pairs) | set(target_pairs)):
        in_b, in_t = pair in base_pairs, pair in target_pairs
        st = ADDED if in_t and not in_b else REMOVED if in_b and not in_t else (
            MODIFIED if pair in changed_pairs else UNCHANGED)
        cyc = pair in target_cycles if st != REMOVED else pair in base_cycles
        edges.append(VEdge(pair[0], pair[1], st, cyc, cyc and pair not in base_cycles and st != REMOVED,
                           target_pairs.get(pair, base_pairs.get(pair, 1)), rel_of[pair]))

    # Groups that at least one leaf maps to, plus edge endpoints (skips pass-through directories).
    groups = {grouper.group(nid) for nid, n in nodes.items()
              if n.category != CATEGORY_SYMBOL and n.component_type not in CONTAINER_TYPES} - {None}
    groups |= {x for e in edges for x in (e.source, e.target)}
    group_status: dict[str, str] = {}
    for g in groups:
        group_status[g] = status.get(g, UNCHANGED)  # type: ignore[index]
    if level != "module":
        # A group whose own node is unchanged but whose members changed is modified.
        for nid, st in status.items():
            if st != UNCHANGED and nodes[nid].category != CATEGORY_SYMBOL:
                g = grouper.group(nid)
                if g is not None and g != nid and group_status.get(g) == UNCHANGED:
                    group_status[g] = MODIFIED
    changed = {g for g, st in group_status.items() if st != UNCHANGED}
    changed |= {x for e in edges if e.status != UNCHANGED for x in (e.source, e.target)}
    hidden_neighbors = 0
    if scope == "all":
        visible = set(group_status)
    else:
        visible = set(changed)
        if scope == "neighbors":
            # Unchanged neighbours, strongest first; hubs can have hundreds, so cap and summarise the rest.
            weight: dict[str, float] = {}
            for e in edges:
                for a, b in ((e.source, e.target), (e.target, e.source)):
                    if a in changed and b not in changed:
                        weight[b] = weight.get(b, 0) + e.count + (1e6 if e.status != UNCHANGED else 0)
            ranked = sorted(weight, key=lambda k: (-weight[k], k))
            visible |= set(ranked[:neighbor_limit])
            hidden_neighbors = max(0, len(ranked) - neighbor_limit)
    visible = {v for v in visible if v in nodes}
    if not include_external:
        visible = {v for v in visible if "external" not in nodes[v].tags}
    # Only keep structural groups that participate in the view.
    if level == "module":
        with_edges = {x for e in edges for x in (e.source, e.target)}
        visible = {v for v in visible if nodes[v].category == CATEGORY_MODULE or v in with_edges or
                   (v in changed and nodes[v].component_type not in CONTAINER_TYPES)}
    order = sorted(visible, key=lambda v: (v not in changed, nodes[v].qualified_name))
    truncated = max(0, len(order) - max_nodes)
    keep = set(order[:max_nodes])

    view = ViewGraph(title=f"Changes: {diff.base.label} → {diff.target.label} ({level} level)", truncated=truncated)
    parents: dict[str, str] = {}
    if level in ("module", "package"):
        comp_grouper = Grouper(nodes, "component", include_external)
        for v in keep:
            c = comp_grouper.group(v)
            if c and c != v and c in nodes:
                parents[v] = c
                view.subgraphs[f"sg_{c}"] = (nodes[c].qualified_name, None)
    for v in order[:max_nodes]:
        node = nodes[v]
        st = group_status.get(v, status.get(v, UNCHANGED))
        change = diff.nodes.get(v)
        view.nodes.append(VNode(v, node.qualified_name or node.name, _sublabel(node, change.before if change else None),
                                st, _kind(node), "stadium" if "external" in node.tags else "box",
                                f"sg_{parents[v]}" if v in parents else None, _icon(node, icons), reasons.get(v, [])))
    view.edges = [e for e in edges if e.source in keep and e.target in keep]
    if hidden_neighbors:
        view.nodes.append(VNode("rv_more_neighbors", f"+{hidden_neighbors} more unchanged neighbours",
                                "use scope 'all' to list them", UNCHANGED, "structural", "stadium", None, "…"))
    return view


def dependency_view(snapshot: RepositorySnapshot, *, level: str = "component",
                    relationships: Iterable[str] = DEFAULT_RELATIONSHIPS, include_external: bool = False,
                    include_tests: bool = True, focus: str | None = None, depth: int = 1, max_nodes: int = 250,
                    icons: dict[str, str] | None = None) -> ViewGraph:
    icons = default_icons() if icons is None else icons
    rels = set(relationships)
    nodes = snapshot.node_index()
    grouper = Grouper(nodes, level, include_external)
    pairs: dict[tuple[str, str], int] = {}
    runtime: set[tuple[str, str]] = set()
    rel_of: dict[tuple[str, str], str] = {}
    for e in snapshot.dependency_edges + snapshot.call_edges:
        if not e.direct or e.relationship not in rels:
            continue
        if not include_tests and e.metadata.get("test_only"):
            continue
        s, t = grouper.group(e.source_id), grouper.group(e.target_id)
        if s is None or t is None or s == t:
            continue
        if not include_tests and ("test" in nodes[s].tags or "test" in nodes[t].tags):
            continue
        pairs[(s, t)] = pairs.get((s, t), 0) + e.occurrences
        rel_of.setdefault((s, t), e.relationship)
        if not e.metadata.get("type_checking_only"):
            runtime.add((s, t))
    cycles = _scc_pairs(runtime)
    visible: set[str] = {x for p in pairs for x in p}
    if focus:
        fg = grouper.group(focus) or focus
        visible = {fg}
        frontier = {fg}
        for _ in range(max(depth, 0)):
            nxt = set()
            for s, t in pairs:
                if s in frontier and t not in visible:
                    nxt.add(t)
                if t in frontier and s not in visible:
                    nxt.add(s)
            visible |= nxt
            frontier = nxt
    order = sorted(visible, key=lambda v: nodes[v].qualified_name if v in nodes else v)
    view = ViewGraph(title=f"Dependencies ({level} level)", mode="kind", truncated=max(0, len(order) - max_nodes))
    keep = set(order[:max_nodes])
    for v in order[:max_nodes]:
        node = nodes[v]
        view.nodes.append(VNode(v, node.qualified_name or node.name, _sublabel(node), UNCHANGED, _kind(node),
                                "stadium" if "external" in node.tags else "box", None, _icon(node, icons)))
    for (s, t), count in sorted(pairs.items()):
        if s in keep and t in keep:
            view.edges.append(VEdge(s, t, UNCHANGED, (s, t) in cycles, False, count, rel_of[(s, t)]))
    return view


def structure_view(snapshot: RepositorySnapshot, *, root: str | None = None, depth: int = 3,
                   include_files: bool = False, max_nodes: int = 250, icons: dict[str, str] | None = None) -> ViewGraph:
    icons = default_icons() if icons is None else icons
    nodes = snapshot.node_index()
    children = snapshot.children()
    start = root or next((n.id for n in snapshot.components if n.component_type == "repository"), None)
    view = ViewGraph(title="Structure", direction="LR", mode="kind")
    if start is None:
        return view
    count = 0
    queue = [(start, 0)]
    while queue:
        nid, d = queue.pop(0)
        node = nodes[nid]
        if count >= max_nodes:
            view.truncated += 1
            continue
        count += 1
        kids = [c for c in children.get(nid, []) if nodes[c].category != CATEGORY_SYMBOL
                and (include_files or nodes[c].category != CATEGORY_MODULE and nodes[c].component_type != "file"
                     or "entry-point" in nodes[c].tags)]
        sub = node.component_type
        if not include_files:
            n_mod = sum(1 for c in children.get(nid, []) if nodes[c].category == CATEGORY_MODULE)
            if n_mod:
                sub += f" · {n_mod} module(s)"
        view.nodes.append(VNode(nid, node.name if nid != start else node.qualified_name, sub, UNCHANGED, _kind(node),
                                "round" if node.category == CATEGORY_MODULE else "box", None, _icon(node, icons)))
        if d < depth:
            for c in sorted(kids, key=lambda k: nodes[k].name):
                queue.append((c, d + 1))
                view.edges.append(VEdge(nid, c, UNCHANGED, relationship="contains"))
        elif kids:
            view.nodes[-1].sublabel += f" · +{len(kids)} more"
    keep = {n.id for n in view.nodes}
    view.edges = [e for e in view.edges if e.source in keep and e.target in keep]
    return view


def flow_view(flow: dict[str, Any], icons: dict[str, str] | None = None) -> ViewGraph:
    view = ViewGraph(title="Affected flow", direction="LR", mode="role")
    shapes = {"entry": "stadium", "test": "hexagon", "changed": "box", "caller": "round", "callee": "round",
              "path": "round"}
    for n in flow.get("nodes", []):
        module = n.get("module_id")
        if module and module != n["id"]:
            view.subgraphs[f"sg_{module}"] = (n.get("module") or module, None)
        label = n["qualified_name"].rsplit(".", 1)[-1] if n.get("category") == CATEGORY_SYMBOL else n["qualified_name"]
        if n.get("category") == CATEGORY_SYMBOL and "." in n["qualified_name"]:
            parts = n["qualified_name"].split(".")
            label = ".".join(parts[-2:]) if n.get("kind") == "method" else parts[-1]
        view.nodes.append(VNode(n["id"], label, f"{n.get('kind', '')} · {n['role']}", n.get("status", UNCHANGED),
                                n["role"], shapes.get(n["role"], "box"),
                                f"sg_{module}" if module and module != n["id"] else None,
                                "🧪" if n["role"] == "test" else ("🚀" if n["role"] == "entry" else "")))
    for e in flow.get("edges", []):
        view.edges.append(VEdge(e["source"], e["target"], e.get("status", UNCHANGED),
                                relationship=e.get("relationship", "calls")))
    return view
