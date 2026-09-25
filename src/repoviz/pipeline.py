"""Runs analyzers over a tree source and assembles a :class:`RepositorySnapshot`."""

from __future__ import annotations

import datetime as _dt
import time
import traceback
from typing import Any

from . import __version__, globs
from .analyzers import PHASES, AnalysisContext, SnapshotBuilder, analyzer_classes
from .config import Config
from .discovery import RepositoryProfile
from .graph import aggregate, find_cycles
from .model import (
    CATEGORY_COMPONENT,
    CATEGORY_MODULE,
    CATEGORY_SYMBOL,
    REL_CALLS,
    REL_DEPENDS_ON,
    REL_IMPORTS,
    AnalyzerRun,
    ComponentNode,
    DependencyEdge,
    RepositorySnapshot,
)
from .sources import TreeSource


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def enabled_analyzer_classes(config: Config) -> list[type]:
    return [cls for cls in analyzer_classes() if cls.mandatory or config.analyzer_enabled(cls.name)]


def run_analyzers(source: TreeSource, profile: RepositoryProfile, config: Config, *, git: Any = None,
                  file_cache: dict[Any, Any] | None = None) -> tuple[SnapshotBuilder, list[AnalyzerRun]]:
    ctx = AnalysisContext(source=source, profile=profile, config=config, git=git,
                          file_cache=file_cache if file_cache is not None else {})
    b = SnapshotBuilder()
    runs: list[tuple[Any, AnalyzerRun]] = []
    all_runs: list[AnalyzerRun] = []
    for cls in enabled_analyzer_classes(config):
        analyzer = cls()
        run = AnalyzerRun(name=analyzer.name, version=analyzer.version, applicable=False,
                          capabilities=list(analyzer.capabilities), languages=list(analyzer.languages))
        try:
            det = analyzer.detect(ctx)
            run.applicable, run.reason = det.applicable, det.reason
        except Exception as exc:  # detection must never break the analysis
            run.reason = f"detection failed: {exc}"
            b.diagnostic("error", "analyzer-failed", f"{analyzer.name}: detection failed: {exc}", analyzer.name)
        all_runs.append(run)
        if run.applicable:
            runs.append((analyzer, run))
    for phase in PHASES:
        for analyzer, run in runs:
            started = time.perf_counter()
            try:
                analyzer.run_phase(phase, ctx, b)
            except Exception as exc:
                tb = traceback.extract_tb(exc.__traceback__)[-1]
                b.diagnostic("error", "analyzer-failed",
                             f"{analyzer.name} failed during '{phase}': {type(exc).__name__}: {exc} "
                             f"({tb.filename.rsplit('/', 1)[-1]}:{tb.lineno}). Other analyzers were not affected.",
                             analyzer.name)
            run.duration_ms += (time.perf_counter() - started) * 1000
    for analyzer, run in runs:
        run.stats = dict(b.stats.get(analyzer.name, {}))
    return b, all_runs


def finalize_graph(b: SnapshotBuilder, config: Config) -> list:
    """Assign components, aggregate edges and detect cycles.  Returns the cycles."""
    nodes = b.nodes
    root = b.root_id
    # 1. Orphans (a parent that no analyzer created) are attached to the root.
    for node in nodes.values():
        if node.parent_id and node.parent_id not in nodes:
            b.diagnostic("warning", "orphan-node", f"Parent of {node.qualified_name} is missing; attached to the "
                         "repository root.", "pipeline", node.path)
            node.parent_id = root

    # Inside an analyzed submodule the submodule is the component: its top-level packages are drill-down detail
    # (five submodules would otherwise each add a "src" or "app" component).  Nested projects stay components.
    subs = {n.id for n in nodes.values() if "submodule" in n.tags}
    if subs:
        for node in nodes.values():
            if "component" in node.tags and not {"project", "submodule", "configured"} & set(node.tags):
                cur, seen = nodes.get(node.parent_id or ""), 0
                while cur is not None and seen < 64 and cur.id not in subs:
                    cur, seen = nodes.get(cur.parent_id or ""), seen + 1
                if cur is not None:
                    node.tags.remove("component")

    # 2. Explicitly configured components.
    configured: list[tuple[str, list[str]]] = []
    for rule in config.components:
        cid = b.id_for("cmp", f"configured:{rule.name}")
        b.add_node(ComponentNode(id=cid, name=rule.name, qualified_name=rule.name, component_type=rule.type,
                                 category=CATEGORY_COMPONENT, parent_id=root, analyzer="configuration",
                                 key=f"configured:{rule.name}", tags=["component", "configured"],
                                 metadata={"paths": rule.paths, "description": rule.description}))
        configured.append((cid, rule.paths))

    memo: dict[str, str | None] = {}
    project_memo: dict[str, str | None] = {}

    def ancestors(nid: str) -> list[str]:
        chain = []
        cur = nodes.get(nid)
        seen = set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            chain.append(cur.id)
            cur = nodes.get(cur.parent_id) if cur.parent_id else None
        return chain

    def component_of(nid: str) -> str | None:
        if nid in memo:
            return memo[nid]
        node = nodes.get(nid)
        result: str | None = None
        if node is None:
            return None
        if "external" in node.tags:
            result = nid
        else:
            if node.path is not None and configured:
                for cid, patterns in configured:
                    if globs.match_any(node.path, patterns):
                        result = cid
                        break
            if result is None:
                chain = ancestors(nid)
                # Everything below a test root belongs to that test root (test packages are not components).
                test_roots = [a for a in chain if a != root and "test" in nodes[a].tags
                              and nodes[a].metadata.get("roles", {}).get("test")]
                result = test_roots[-1] if test_roots else None
            if result is None:
                # Inside an analyzed submodule, the submodule is the component (its packages are drill-down
                # detail); only a project of its own (a nested manifest) is a finer component.
                sub_at = next((i for i, a in enumerate(chain) if "submodule" in nodes[a].tags), -1)
                result = next((a for i, a in enumerate(chain) if a != root and "component" in nodes[a].tags
                               and (sub_at < 0 or i >= sub_at or "project" in nodes[a].tags)), None)
                if result is None:
                    result = next((a for a in chain if "top-level" in nodes[a].tags), None)
                if result is None:
                    result = root
        memo[nid] = result
        return result

    def project_of(nid: str) -> str | None:
        if nid not in project_memo:
            chain = ancestors(nid)
            project_memo[nid] = next((a for a in chain if "project" in nodes[a].tags), root)
        return project_memo[nid]

    for node in nodes.values():
        if node.category == CATEGORY_SYMBOL:
            continue
        comp = component_of(node.id)
        if comp and comp != node.id:
            node.metadata["component_id"] = comp
        proj = project_of(node.id)
        if proj and proj != node.id and "external" not in node.tags:
            node.metadata["project_id"] = proj

    def module_of(nid: str) -> str:
        node = nodes.get(nid)
        while node is not None and node.category == CATEGORY_SYMBOL and node.parent_id:
            node = nodes.get(node.parent_id)
        return node.id if node is not None else nid

    def include_in_cycles(e: DependencyEdge) -> bool:
        if e.metadata.get("type_checking_only") and not config.cycles_include_type_checking:
            return False
        if e.metadata.get("lazy_only") and not config.cycles_include_lazy:
            return False
        return True

    direct_imports = [e for e in b.edges.values() if e.relationship == REL_IMPORTS and e.direct
                      and e.source_id in nodes and e.target_id in nodes
                      and "external" not in nodes[e.target_id].tags]
    cycles = find_cycles([e for e in direct_imports if nodes[e.source_id].category == CATEGORY_MODULE
                          and nodes[e.target_id].category == CATEGORY_MODULE],
                         "module", REL_IMPORTS, include_in_cycles)

    # 3. Component-level aggregation of import edges.
    agg = aggregate(direct_imports, lambda nid: component_of(module_of(nid)))
    aggregated: list[DependencyEdge] = []
    for (s, t), data in agg.items():
        flags = {k: True for k, v in data["flags"].items() if v}  # type: ignore[union-attr]
        edge = b.add_edge(s, t, REL_IMPORTS, analyzer="pipeline", direct=False, evidence=data["evidence"],
                          occurrences=int(data["occurrences"]), confidence=float(data["confidence"]),  # type: ignore[arg-type]
                          metadata={"level": "component", "underlying_edges": list(data["edges"])[:200],  # type: ignore[arg-type]
                                    "underlying_count": len(data["edges"]), **flags})  # type: ignore[arg-type]
        aggregated.append(edge)
    cycles += find_cycles(aggregated, "component", REL_IMPORTS, include_in_cycles)

    # 4. Project-level cycles in declared (manifest) dependencies.
    project_edges = [e for e in b.edges.values() if e.relationship == REL_DEPENDS_ON and e.metadata.get("internal")
                     and e.metadata.get("scope") not in ("dev", "test")]
    cycles += find_cycles(project_edges, "project", REL_DEPENDS_ON)
    return cycles


def assemble(b: SnapshotBuilder, runs: list[AnalyzerRun], cycles: list, *, repository_id: str,
             repository_name: str, root: str, source: TreeSource, label: str, profile: RepositoryProfile,
             config: Config) -> RepositorySnapshot:
    components: list[ComponentNode] = []
    modules: list[ComponentNode] = []
    symbols: list[ComponentNode] = []
    for node in sorted(b.nodes.values(), key=lambda n: n.id):
        node.tags = sorted(set(node.tags))
        node.analyzers = sorted(set(node.analyzers))
        {CATEGORY_MODULE: modules, CATEGORY_SYMBOL: symbols}.get(node.category, components).append(node)
    deps, calls = [], []
    for edge in sorted(b.edges.values(), key=lambda e: e.id):
        edge.cycle_ids = sorted(set(edge.cycle_ids))
        (calls if edge.relationship == REL_CALLS else deps).append(edge)
    diagnostics = list(profile.diagnostics) + b.diagnostics
    metadata: dict[str, Any] = dict(b.metadata)
    metadata.update({"tool": "repoviz", "tool_version": __version__, "config_fingerprint": config.fingerprint(),
                     "source_kind": source.kind, "id_collisions": len(b.ids.collisions)})
    return RepositorySnapshot(
        repository_id=repository_id, repository_name=repository_name, root=root, revision=label,
        revision_id=source.revision_id, kind=source.kind if source.kind != "overlay" else "session-baseline",
        label=label, generated_at=utcnow(), components=components, modules=modules, symbols=symbols,
        containment_edges=sorted(b.containment_edges(), key=lambda e: e.id), dependency_edges=deps, call_edges=calls,
        cycles=cycles, diagnostics=diagnostics, analyzers=runs, profile=profile.to_dict(), metadata=metadata,
    )


def build_snapshot(source: TreeSource, profile: RepositoryProfile, config: Config, *, repository_id: str,
                   repository_name: str, root: str, label: str, git: Any = None,
                   file_cache: dict[Any, Any] | None = None) -> RepositorySnapshot:
    started = time.perf_counter()
    b, runs = run_analyzers(source, profile, config, git=git, file_cache=file_cache)
    cycles = finalize_graph(b, config)
    snap = assemble(b, runs, cycles, repository_id=repository_id, repository_name=repository_name, root=root,
                    source=source, label=label, profile=profile, config=config)
    snap.metadata["duration_ms"] = round((time.perf_counter() - started) * 1000 + profile.duration_ms, 1)
    return snap
