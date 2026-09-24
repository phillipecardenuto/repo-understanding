"""Compare two snapshots.

Nodes and edges are matched by their stable IDs.  A node is *modified* when
its content fingerprint, type, location in the hierarchy or significant
metadata changed; containers whose descendants changed are marked modified
with the reason ``contents changed``.  An edge is *modified* when its
evidence (ignoring line numbers), occurrence count or flags changed.

Cycles are matched per level by their member sets: identical sets are
unchanged, overlapping sets are *changed*, and the rest are *introduced* (only
in the target) or *resolved* (only in the base).
"""

from __future__ import annotations

from typing import Any, Iterable

from .model import (
    ADDED,
    CATEGORY_SYMBOL,
    MODIFIED,
    REL_CONTAINS,
    REL_DEPENDS_ON,
    REL_IMPORTS,
    REMOVED,
    UNCHANGED,
    ComponentNode,
    Cycle,
    DependencyEdge,
    Diagnostic,
    EdgeChange,
    NodeChange,
    RepositoryDiff,
    RepositorySnapshot,
)
from .pipeline import utcnow
from .renames import detect_renames

#: Metadata keys whose change makes a node "modified".
SIGNIFICANT_NODE_METADATA = (
    "version", "role", "entry_kind", "target", "signature", "decorators", "base_images", "image", "ports", "jobs",
    "workspace", "ecosystem", "kind", "exported", "is_package", "async", "manifest", "go_package", "commit",
    "uncommitted_files",
)
EDGE_FLAGS = ("type_checking_only", "conditional_only", "lazy_only", "dynamic_only", "test_only", "scope", "spec")


def _node_changes(before: ComponentNode, after: ComponentNode) -> tuple[list[str], dict[str, Any]]:
    reasons: list[str] = []
    prior: dict[str, Any] = {}
    if before.fingerprint and after.fingerprint and before.fingerprint != after.fingerprint:
        b_sem = before.metadata.get("semantic_fingerprint")
        a_sem = after.metadata.get("semantic_fingerprint")
        if b_sem and a_sem and b_sem == a_sem:
            reasons.append("formatting or comments only")
        else:
            reasons.append("content changed")
    if before.component_type != after.component_type:
        reasons.append(f"type {before.component_type} → {after.component_type}")
        prior["component_type"] = before.component_type
    if before.parent_id != after.parent_id:
        reasons.append("moved")
        prior["parent_id"] = before.parent_id
    if before.qualified_name != after.qualified_name:
        reasons.append("renamed")
        prior["qualified_name"] = before.qualified_name
    for key in SIGNIFICANT_NODE_METADATA:
        b, a = before.metadata.get(key), after.metadata.get(key)
        if key == "signature" and "signature_id" in before.metadata and "signature_id" in after.metadata:
            b, a = before.metadata["signature_id"], after.metadata["signature_id"]  # display text may be truncated
        if b != a:
            reasons.append(f"{key} changed")
            prior[key] = before.metadata.get(key)
    roles = {"entry-point", "test", "generated", "unsupported", "project", "component"}
    changed_roles = sorted((set(before.tags) ^ set(after.tags)) & roles)
    if changed_roles:
        reasons.append("roles changed: " + ", ".join(changed_roles))
    return reasons, prior


def _edge_changes(before: DependencyEdge, after: DependencyEdge, evidence: bool = True) -> list[str]:
    reasons = []
    if evidence and before.evidence_signature() != after.evidence_signature():
        reasons.append("evidence changed")
    if before.occurrences != after.occurrences:
        reasons.append(f"occurrences {before.occurrences} → {after.occurrences}")
    for flag in EDGE_FLAGS:
        b, a = before.metadata.get(flag), after.metadata.get(flag)
        differs = bool(b) != bool(a) if isinstance(b, bool) or isinstance(a, bool) else b != a
        if differs:
            reasons.append(f"{flag}: {b!r} → {a!r}")
    return reasons


def _match_cycles(base: list[Cycle], target: list[Cycle],
                  renamed: dict[str, str] | None = None) -> tuple[list[Cycle], list[Cycle], list[dict[str, Any]]]:
    """Match cycles by members; base members are seen through ``renamed`` (old id → new id)."""
    renamed = renamed or {}
    members_of = {c.id: {renamed.get(m, m) for m in c.members} for c in base}
    introduced: list[Cycle] = []
    changed: list[dict[str, Any]] = []
    base_by_level: dict[str, list[Cycle]] = {}
    for c in base:
        base_by_level.setdefault(c.level, []).append(c)
    matched_base: set[str] = set()
    for t in target:
        candidates = base_by_level.get(t.level, [])
        same = next((c for c in candidates if c.id == t.id or members_of[c.id] == set(t.members)), None)
        if same is not None:
            matched_base.add(same.id)
            continue
        overlapping = [c for c in candidates if members_of[c.id] & set(t.members)]
        if not overlapping:
            introduced.append(t)
            continue
        before_members = set().union(*(members_of[c.id] for c in overlapping))
        matched_base.update(c.id for c in overlapping)
        changed.append({
            "level": t.level, "relationship": t.relationship, "target_cycle": t.id,
            "base_cycles": [c.id for c in overlapping], "members": t.members,
            "added_members": sorted(set(t.members) - before_members),
            "removed_members": sorted(before_members - set(t.members)),
            "example_path": t.example_path,
        })
    resolved = [c for c in base if c.id not in matched_base]
    return introduced, resolved, changed


def diff_snapshots(base: RepositorySnapshot, target: RepositorySnapshot) -> RepositoryDiff:
    diff = RepositoryDiff(base=base.ref(), target=target.ref(), generated_at=utcnow())
    if base.repository_id != target.repository_id:
        diff.diagnostics.append(Diagnostic("warning", "different-repositories",
                                           "The snapshots come from different repositories.", "diff"))
    if base.metadata.get("config_fingerprint") != target.metadata.get("config_fingerprint"):
        diff.diagnostics.append(Diagnostic("warning", "different-configuration",
                                           "The snapshots were produced with different configurations; some "
                                           "differences may be caused by configuration rather than code.", "diff"))
    b_nodes = base.node_index()
    t_nodes = target.node_index()

    for nid in sorted(set(b_nodes) | set(t_nodes)):
        before, after = b_nodes.get(nid), t_nodes.get(nid)
        if before is None:
            diff.nodes[nid] = NodeChange(after, ADDED)  # type: ignore[arg-type]
        elif after is None:
            diff.nodes[nid] = NodeChange(before, REMOVED)
        else:
            reasons, prior = _node_changes(before, after)
            diff.nodes[nid] = NodeChange(after, MODIFIED if reasons else UNCHANGED, reasons, prior)

    # Removed + added pairs that are one thing renamed or moved become one modified node.
    id_map: dict[str, str] = {}
    for rn in detect_renames(base, target, {i for i, c in diff.nodes.items() if c.status == REMOVED},
                             {i for i, c in diff.nodes.items() if c.status == ADDED}):
        before, after = b_nodes[rn.old_id], t_nodes[rn.new_id]
        reasons, prior = _node_changes(before, after)
        # A qualified name changes with the file; "renamed" means the name itself changed.
        reasons = [f"renamed from {before.name}" if r == "renamed" and rn.renamed else
                   f"moved from {before.path}" if r == "moved" and before.path != after.path else r
                   for r in reasons if r != "renamed" or rn.renamed]
        if rn.renamed and not any(r.startswith("renamed") for r in reasons):
            reasons.insert(0, f"renamed from {before.name}")
        if before.path != after.path and not any(r.startswith("moved") for r in reasons):
            reasons.insert(0, f"moved from {before.path}")
        reasons = [r for r in reasons if r != "moved"]
        prior.update({"previous_id": rn.old_id, "qualified_name": before.qualified_name, "name": before.name})
        if before.path != after.path:
            prior["path"] = before.path
        diff.nodes[rn.new_id] = NodeChange(after, MODIFIED, reasons or [f"renamed from {before.name}"], prior)
        del diff.nodes[rn.old_id]
        id_map[rn.old_id] = rn.new_id
        diff.renames.append(rn.to_dict())

    # Containers of changed nodes are modified too ("contents changed").
    def parent_of(nid: str) -> str | None:
        node = t_nodes.get(nid) or b_nodes.get(nid)
        return node.parent_id if node else None

    for nid, change in list(diff.nodes.items()):
        if change.status == UNCHANGED:
            continue
        cur = parent_of(nid)
        seen: set[str] = set()
        while cur and cur not in seen:
            seen.add(cur)
            pc = diff.nodes.get(cur)
            if pc is None:
                break
            if pc.status == UNCHANGED:
                pc.status = MODIFIED
                pc.reasons = ["contents changed"]
            elif pc.status == MODIFIED and "contents changed" not in pc.reasons:
                pc.reasons.append("contents changed")
            cur = parent_of(cur)

    for nid, change in diff.nodes.items():
        {ADDED: diff.added_nodes, REMOVED: diff.removed_nodes, MODIFIED: diff.modified_nodes,
         UNCHANGED: diff.unchanged_nodes}[change.status].append(nid)

    b_edges = {e.id: e for e in base.edges() if e.relationship != REL_CONTAINS}
    t_edges = {e.id: e for e in target.edges() if e.relationship != REL_CONTAINS}
    for eid in sorted(set(b_edges) | set(t_edges)):
        before, after = b_edges.get(eid), t_edges.get(eid)
        if before is None:
            ch = EdgeChange(after, ADDED, in_target_cycle=after.in_cycle)  # type: ignore[union-attr,arg-type]
        elif after is None:
            ch = EdgeChange(before, REMOVED, in_base_cycle=before.in_cycle)
        else:
            reasons = _edge_changes(before, after)
            ch = EdgeChange(after, MODIFIED if reasons else UNCHANGED, reasons, before.in_cycle, after.in_cycle,
                            base_evidence=before.evidence if reasons else [],
                            base_flags={k: before.metadata[k] for k in EDGE_FLAGS if before.metadata.get(k)}
                            if reasons else {})
        diff.edges[eid] = ch

    # An edge whose endpoint was renamed continues as the target's edge between the renamed nodes.
    if id_map:
        added_by_ends = {(c.edge.relationship, c.edge.source_id, c.edge.target_id): eid
                         for eid, c in diff.edges.items() if c.status == ADDED}
        for eid, ch in list(diff.edges.items()):
            e = ch.edge
            if ch.status != REMOVED or (e.source_id not in id_map and e.target_id not in id_map):
                continue
            new_eid = added_by_ends.get((e.relationship, id_map.get(e.source_id, e.source_id),
                                         id_map.get(e.target_id, e.target_id)))
            if new_eid is None:
                continue
            cont = diff.edges[new_eid]
            reasons = _edge_changes(e, cont.edge, evidence=False)  # locations moved with the file
            cont.status = MODIFIED if reasons else UNCHANGED
            cont.reasons = reasons
            cont.in_base_cycle = e.in_cycle
            cont.previous_id = eid
            del diff.edges[eid]
    for eid, ch in diff.edges.items():
        {ADDED: diff.added_edges, REMOVED: diff.removed_edges, MODIFIED: diff.modified_edges,
         UNCHANGED: diff.unchanged_edges}[ch.status].append(eid)

    diff.introduced_cycles, diff.resolved_cycles, diff.changed_cycles = _match_cycles(base.cycles, target.cycles,
                                                                                      id_map)
    diff.new_dependencies = _dependency_summary(diff, (ADDED,), t_nodes, b_nodes)
    diff.new_dependencies += _became_runtime(diff, t_nodes, b_nodes)
    diff.removed_dependencies = _dependency_summary(diff, (REMOVED,), t_nodes, b_nodes)
    return diff


def _level(edge: DependencyEdge, nodes: dict[str, ComponentNode]) -> str:
    if not edge.direct:
        return str(edge.metadata.get("level", "component"))
    target = nodes.get(edge.target_id)
    if target is not None and "external" in target.tags:
        return "external"
    if edge.relationship == REL_DEPENDS_ON:
        return "project"
    return "module"


def _summarize(ch: EdgeChange, both: dict[str, ComponentNode], note: str = "") -> dict[str, Any]:
    e = ch.edge
    src, dst = both.get(e.source_id), both.get(e.target_id)
    item = {
        "edge_id": e.id, "relationship": e.relationship, "level": _level(e, both), "status": ch.status,
        "source_id": e.source_id, "source": src.qualified_name if src else e.source_id,
        "target_id": e.target_id, "target": dst.qualified_name if dst else e.target_id,
        "external": bool(dst and "external" in dst.tags), "stdlib": bool(dst and "stdlib" in dst.tags),
        "in_cycle": ch.in_target_cycle if ch.status != REMOVED else ch.in_base_cycle,
        "occurrences": e.occurrences, "evidence": [ev.location for ev in e.evidence[:5]],
        "scope": e.metadata.get("scope"), "type_checking_only": bool(e.metadata.get("type_checking_only")),
    }
    if note:
        item["note"] = note
    return item


def _dependency_summary(diff: RepositoryDiff, statuses: Iterable[str], t_nodes: dict[str, ComponentNode],
                        b_nodes: dict[str, ComponentNode]) -> list[dict[str, Any]]:
    out = []
    both = {**b_nodes, **t_nodes}
    for ch in diff.edges.values():
        if ch.status not in statuses or ch.edge.relationship not in (REL_IMPORTS, REL_DEPENDS_ON):
            continue
        out.append(_summarize(ch, both))
    order = {"component": 0, "project": 1, "module": 2, "external": 3}
    return sorted(out, key=lambda d: (order.get(d["level"], 9), d["source"], d["target"]))


def _became_runtime(diff: RepositoryDiff, t_nodes: dict[str, ComponentNode],
                    b_nodes: dict[str, ComponentNode]) -> list[dict[str, Any]]:
    out = []
    both = {**b_nodes, **t_nodes}
    for ch in diff.edges.values():
        if ch.status != MODIFIED or ch.edge.relationship != REL_IMPORTS:
            continue
        if any(r.startswith("type_checking_only: True") for r in ch.reasons):
            out.append(_summarize(ch, both, note="type-checking-only import became a runtime import"))
    return out


def symbol_changes(diff: RepositoryDiff) -> dict[str, list[str]]:
    """IDs of changed symbols keyed by status (innermost changes only for 'modified')."""
    changed = {nid for nid, c in diff.nodes.items() if c.node.category == CATEGORY_SYMBOL and c.status != UNCHANGED}
    parents = {c.node.parent_id for nid, c in diff.nodes.items() if nid in changed}
    out: dict[str, list[str]] = {ADDED: [], REMOVED: [], MODIFIED: []}
    for nid in sorted(changed):
        c = diff.nodes[nid]
        # A modified container symbol is explained by its changed children.
        if c.status == MODIFIED and (nid in parents or c.reasons == ["contents changed"]):
            continue
        out[c.status].append(nid)
    return out
