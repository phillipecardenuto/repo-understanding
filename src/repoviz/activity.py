"""Current development activity.

``observe(repo)`` answers "which parts of the repository are being modified
right now, and what does it mean architecturally?".  The baseline is the
active work session (see :mod:`repoviz.session`) when there is one -- which
captures everything an AI coding agent changed since the session started,
including commits -- and ``HEAD`` otherwise.

Each changed file becomes an :class:`~repoviz.model.ActivityEvent` with its Git
status, owning component, line counts, architecture impact (dependencies
added/removed, cycles introduced, public symbols added/removed, manifest
changes), affected tests and whether configuration changed.  First/last
observation times are persisted in the state directory so they survive
restarts of the live server.
"""

from __future__ import annotations

import datetime as _dt
import difflib
from collections import deque
from typing import TYPE_CHECKING, Any

from . import classify
from .diff import diff_snapshots, symbol_changes
from .flow import affected_flow
from .model import (
    ADDED,
    CATEGORY_MODULE,
    CATEGORY_SYMBOL,
    MODIFIED,
    REL_DEPENDS_ON,
    REL_IMPORTS,
    REMOVED,
    ActivityEvent,
    RepositoryDiff,
    RepositorySnapshot,
)
from .sources import is_binary

if TYPE_CHECKING:  # pragma: no cover
    from .repo import Repository

MAX_DIFF_BYTES = 1_000_000


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _count_lines(before: bytes | None, after: bytes | None) -> tuple[int | None, int | None]:
    if (before is not None and (is_binary(before) or len(before) > MAX_DIFF_BYTES)) or \
            (after is not None and (is_binary(after) or len(after) > MAX_DIFF_BYTES)):
        return None, None
    a = before.decode("utf-8", "replace").splitlines() if before else []
    b = after.decode("utf-8", "replace").splitlines() if after else []
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


class _ImpactIndex:
    """Per-path lookups over one diff, computed once per observation."""

    def __init__(self, diff: RepositoryDiff, target: RepositorySnapshot) -> None:
        self.diff = diff
        nodes = diff.nodes
        self.modules_by_path: dict[str, set[str]] = {}
        for nid, c in nodes.items():
            if c.node.path is not None and c.node.category != CATEGORY_SYMBOL:
                self.modules_by_path.setdefault(c.node.path, set()).add(nid)
        self.underlying_to_agg: dict[str, list[str]] = {}
        self.edges_by_path: dict[str, list[Any]] = {}
        for eid, ch in diff.edges.items():
            e = ch.edge
            if not e.direct:
                for u in e.metadata.get("underlying_edges", []):
                    self.underlying_to_agg.setdefault(u, []).append(eid)
                continue
            if ch.status == "unchanged" or e.relationship not in (REL_IMPORTS, REL_DEPENDS_ON):
                continue
            paths = {ev.path for ev in e.evidence} | {ev.path for ev in ch.base_evidence}
            src = nodes.get(e.source_id)
            if src is not None and src.node.path is not None:
                paths.add(src.node.path)
            for path in paths:
                self.edges_by_path.setdefault(path, []).append(ch)
        self.symbols_by_path: dict[str, list[tuple[str, str]]] = {}
        for status, ids in symbol_changes(diff).items():
            for sid in ids:
                node = nodes[sid].node
                if node.path is not None:
                    self.symbols_by_path.setdefault(node.path, []).append((status, sid))
        self.reverse_imports: dict[str, set[str]] = {}
        for e in target.dependency_edges:
            if e.relationship == REL_IMPORTS and e.direct:
                self.reverse_imports.setdefault(e.target_id, set()).add(e.source_id)
        self.target_index = target.node_index()


def _impact_for(path: str, idx: _ImpactIndex) -> tuple[list[dict[str, Any]], str, list[str]]:
    items: list[dict[str, Any]] = []
    diff = idx.diff
    nodes = diff.nodes
    level = "none"
    rank = {"none": 0, "low": 1, "medium": 2, "high": 3}

    def bump(new: str) -> None:
        nonlocal level
        if rank[new] > rank[level]:
            level = new

    for nid in idx.modules_by_path.get(path, ()):
        c = nodes[nid]
        if c.status in (ADDED, REMOVED) and c.node.category == CATEGORY_MODULE:
            items.append({"kind": f"module-{c.status}", "detail": c.node.qualified_name, "severity": "medium"})
            bump("medium")
        elif c.status == MODIFIED and c.reasons == ["formatting or comments only"]:
            items.append({"kind": "cosmetic", "detail": "formatting or comments only", "severity": "none"})

    for ch in idx.edges_by_path.get(path, ()):
        e = ch.edge
        src = nodes.get(e.source_id)
        dst = nodes.get(e.target_id)
        label = f"{src.node.qualified_name if src else e.source_id} → {dst.node.qualified_name if dst else e.target_id}"
        external = bool(dst and "external" in dst.node.tags)
        if ch.status in (ADDED, REMOVED):
            sev = "low" if external and dst and "stdlib" in dst.node.tags else "medium"
            items.append({"kind": f"dependency-{ch.status}", "detail": label, "relationship": e.relationship,
                          "external": external, "severity": sev, "edge_id": e.id})
            bump(sev)
            for agg_id in idx.underlying_to_agg.get(e.id, []):
                agg = diff.edges[agg_id]
                if agg.status in (ADDED, REMOVED):
                    a_src, a_dst = nodes.get(agg.edge.source_id), nodes.get(agg.edge.target_id)
                    items.append({"kind": f"component-dependency-{agg.status}",
                                  "detail": f"{a_src.node.name if a_src else '?'} → {a_dst.node.name if a_dst else '?'}",
                                  "severity": "high", "edge_id": agg_id})
                    bump("high")
        elif ch.status == MODIFIED and any(r.startswith("type_checking_only") for r in ch.reasons):
            items.append({"kind": "dependency-kind-changed", "detail": f"{label} ({'; '.join(ch.reasons)})",
                          "severity": "medium", "edge_id": e.id})
            bump("medium")
        if ch.in_target_cycle and not ch.in_base_cycle:
            items.append({"kind": "cycle-introduced", "detail": label, "severity": "high", "edge_id": e.id})
            bump("high")
        elif ch.in_base_cycle and not ch.in_target_cycle:
            items.append({"kind": "cycle-resolved", "detail": label, "severity": "low", "edge_id": e.id})
            bump("low")
    changed_syms: list[str] = []
    for status, sid in idx.symbols_by_path.get(path, ()):
        node = nodes[sid].node
        changed_syms.append(sid)
        if status in (ADDED, REMOVED) and node.metadata.get("public", node.metadata.get("exported", True)) \
                and node.component_type in ("class", "function", "method"):
            sev = "high" if status == REMOVED else "low"
            items.append({"kind": f"public-symbol-{status}", "detail": node.qualified_name, "severity": sev})
            bump(sev)
    if changed_syms:
        bump("low")
    return items, level, changed_syms


def _tests_affected(module_id: str | None, idx: _ImpactIndex, limit: int = 50) -> list[str]:
    if module_id is None:
        return []
    seen = {module_id}
    queue = deque([module_id])
    tests: list[str] = []
    while queue and len(tests) < limit:
        cur = queue.popleft()
        for importer in sorted(idx.reverse_imports.get(cur, ())):
            if importer in seen:
                continue
            seen.add(importer)
            node = idx.target_index.get(importer)
            if node is not None and "test" in node.tags and node.path:
                tests.append(node.path)
            queue.append(importer)
    return tests


def observe(repo: "Repository", *, use_session: bool = True, record: bool = True) -> dict[str, Any]:
    """Compute the current activity report (and record observation times)."""
    now = utcnow()
    session = repo.current_session() if use_session else None
    status_by_path: dict[str, Any] = {}
    if repo.git is not None:
        for entry in repo.git.status():
            status_by_path[entry.path] = entry
            if entry.orig_path:
                status_by_path.setdefault(entry.orig_path, entry)
    if session is not None:
        base_spec, baseline = "SESSION", {"kind": "session", "label": f"session started {session.started_at}",
                                          "session": session.to_dict()}
    elif repo.git is not None and repo.git.head() is not None:
        base_spec, baseline = "HEAD", {"kind": "head", "label": "HEAD", "revision": repo.git.head()}
    elif repo.git is not None:
        base_spec, baseline = "EMPTY", {"kind": "empty", "label": "empty tree (no commits yet)"}
    else:
        return {"generated_at": now, "baseline": {"kind": "none", "label": "not a Git repository"}, "events": [],
                "summary": {"files": 0}, "notes": ["Activity tracking needs a Git repository or a session."]}

    base_source = repo.open_source(base_spec)
    target_source = repo.open_source("WORKTREE")
    base_snap = repo.snapshot_of(base_source, baseline["label"])
    target_snap = repo.snapshot_of(target_source, "working tree")
    diff = diff_snapshots(base_snap, target_snap)

    candidates = set(status_by_path)
    if session is not None:
        candidates |= set(session.overrides)
        if session.baseline_head and repo.git is not None and repo.git.head() and \
                repo.git.head() != session.baseline_head:
            candidates |= set(repo.git.changed_paths(session.baseline_head, "HEAD"))
    changed_paths = sorted(p for p in candidates
                           if base_source.content_hash(p) != target_source.content_hash(p))

    observations = repo.state.load_observations(session)
    b_index = base_snap.node_index()
    t_index = target_snap.node_index()
    t_by_path = {n.path: n for n in target_snap.nodes() if n.path and n.category != CATEGORY_SYMBOL
                 and n.component_type not in ("directory", "repository", "package", "namespace-package", "project",
                                              "workspace-member")}
    b_by_path = {n.path: n for n in base_snap.nodes() if n.path and n.category != CATEGORY_SYMBOL
                 and n.component_type not in ("directory", "repository", "package", "namespace-package", "project",
                                              "workspace-member")}
    events: list[ActivityEvent] = []
    impact_index = _ImpactIndex(diff, target_snap)
    for path in changed_paths:
        entry = status_by_path.get(path)
        current_hash = target_source.content_hash(path)
        obs = observations.get(path)
        if obs is None:
            obs = {"first_observed": now, "last_observed": now, "hash": current_hash}
            observations[path] = obs
        elif obs.get("hash") != current_hash:
            obs["last_observed"] = now
            obs["hash"] = current_hash
        node = t_by_path.get(path) or b_by_path.get(path)
        index = t_index if path in t_by_path else b_index
        comp_id = node.metadata.get("component_id") if node else None
        comp = index.get(comp_id) if comp_id else None
        before = base_source.read_bytes(path)
        after = target_source.read_bytes(path)
        added, removed = _count_lines(before, after)
        if entry is not None:
            git_status = entry.label
        elif after is None:
            git_status = "deleted"
        elif before is None:
            git_status = "added"
        else:
            git_status = "committed" if session is not None else "modified"
        impact, level, changed_syms = _impact_for(path, impact_index)
        is_test = bool(node and "test" in node.tags) or classify.is_test_path(path)
        module_id = node.id if node and node.category == CATEGORY_MODULE else None
        tests = [path] if is_test else _tests_affected(module_id, impact_index)
        cfg = classify.config_kind(path)
        mtime = target_source.mtime(path)
        ev = ActivityEvent(
            path=path, first_observed=obs["first_observed"], last_observed=obs["last_observed"],
            git_status=git_status, owning_component=comp_id, owning_component_name=comp.qualified_name if comp else None,
            module_id=node.id if node else None, lines_added=added, lines_removed=removed,
            architecture_impact=impact, impact_level=level, tests_affected=tests, is_test=is_test,
            configuration_affected=cfg is not None, configuration_kind=cfg, in_session=session is not None,
            staged=bool(entry and entry.staged), unstaged=bool(entry and entry.unstaged), changed_symbols=changed_syms,
            last_modified=_dt.datetime.fromtimestamp(mtime, _dt.timezone.utc).isoformat(timespec="seconds") if mtime else None,
            previous_path=entry.orig_path if entry is not None and entry.orig_path else None,
        )
        if cfg is not None and level == "none":
            ev.impact_level = "low"
        events.append(ev)
    if record:
        live = {p: observations[p] for p in changed_paths}
        repo.state.save_observations(session, live)

    flow = affected_flow(diff)
    summary = {
        "files": len(events),
        "lines_added": sum(e.lines_added or 0 for e in events),
        "lines_removed": sum(e.lines_removed or 0 for e in events),
        "by_status": _count(e.git_status for e in events),
        "by_impact": _count(e.impact_level for e in events),
        "components": _count(e.owning_component_name or "(root)" for e in events),
        "tests_changed": sum(1 for e in events if e.is_test),
        "config_changed": sum(1 for e in events if e.configuration_affected),
        "new_dependencies": len(diff.new_dependencies),
        "removed_dependencies": len(diff.removed_dependencies),
        "cycles_introduced": len(diff.introduced_cycles),
        "cycles_resolved": len(diff.resolved_cycles),
        "entry_points_affected": len(flow.entry_points),
        "tests_reaching_changes": len(flow.tests),
    }
    return {
        "generated_at": now,
        "baseline": baseline,
        "events": [e.to_dict() for e in sorted(events, key=lambda e: (e.last_observed, e.path), reverse=True)],
        "summary": summary,
        "diff": diff,
        "flow": flow.to_dict(),
        "git": repo.git_info(),
        "poll_seconds": repo.config.poll_seconds,
    }


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
