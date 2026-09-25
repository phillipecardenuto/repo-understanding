"""Normalized, language-independent graph model.

Nothing in this module knows about Mermaid, Git, or any programming language.
Every analyzer produces these entities and every renderer consumes them.

Serialization omits ``None`` values and empty collections to keep embedded
reports small; ``from_dict`` restores the defaults.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from . import SCHEMA_VERSION

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Which snapshot list a node belongs to.
CATEGORY_COMPONENT = "component"
CATEGORY_MODULE = "module"
CATEGORY_SYMBOL = "symbol"
CATEGORIES = (CATEGORY_COMPONENT, CATEGORY_MODULE, CATEGORY_SYMBOL)

#: Relationship types used by the bundled analyzers.  Analyzers may add more.
REL_CONTAINS = "contains"
REL_IMPORTS = "imports"
REL_DEPENDS_ON = "depends-on"
REL_CALLS = "calls"
REL_INVOKES = "invokes"  # entry point -> symbol/module/file
REL_BUILDS = "builds"  # container/service -> project/directory
REL_RUNS = "runs"  # service -> the module or callable its command runs
REL_STARTS_AFTER = "starts-after"  # service -> service (compose depends_on)
REL_SHARES_VOLUME = "shares-volume"  # service -> service (the same named volume)
REL_TALKS_TO = "talks-to"  # service -> service (a URL or host in its environment names the other); module -> service
REL_INVOKES_CONTAINER = "invokes-container"  # module -> the code (or service) that builds the image it starts

#: Diff statuses.
ADDED = "added"
REMOVED = "removed"
MODIFIED = "modified"
UNCHANGED = "unchanged"
STATUSES = (ADDED, REMOVED, MODIFIED, UNCHANGED)


def _clean(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return value.to_dict()  # type: ignore[attr-defined]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_clean(v) for v in value]
        return sorted(items) if isinstance(value, (set, frozenset)) else items
    return value


def _compact(obj: Any, keep: tuple[str, ...] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in dataclasses.fields(obj):
        value = getattr(obj, f.name)
        if f.name not in keep and (value is None or value == [] or value == {} or value == ""):
            continue
        out[f.name] = _clean(value)
    return out


def _from(cls: type, data: dict[str, Any]) -> Any:
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


# --------------------------------------------------------------------------
# Core entities
# --------------------------------------------------------------------------


@dataclass
class SourceEvidence:
    """Where in the source a relationship or entity was observed."""

    path: str
    start_line: int | None = None
    end_line: int | None = None
    construct: str = ""
    analyzer: str = ""
    excerpt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceEvidence":
        return _from(cls, data)

    def signature(self) -> tuple[str, str, str]:
        """Line-independent identity used to decide whether evidence *changed*.

        Line numbers shift whenever code above them is edited, so they are
        deliberately excluded.
        """
        excerpt = " ".join((self.excerpt or "").split())
        return (self.path, self.construct, excerpt)

    @property
    def location(self) -> str:
        if self.start_line is None:
            return self.path
        if self.end_line and self.end_line != self.start_line:
            return f"{self.path}:{self.start_line}-{self.end_line}"
        return f"{self.path}:{self.start_line}"


@dataclass
class ComponentNode:
    """Any node of the graph: component, module, or symbol.

    ``category`` says which snapshot list the node lives in; ``component_type``
    is the finer classification (``directory``, ``package``, ``project``,
    ``module``, ``class``, ``function``, ``external-package``...).
    """

    id: str
    name: str
    qualified_name: str
    component_type: str
    category: str = CATEGORY_COMPONENT
    language: str | None = None
    path: str | None = None
    parent_id: str | None = None
    analyzer: str = ""
    key: str = ""
    fingerprint: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    tags: list[str] = field(default_factory=list)
    analyzers: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _compact(self, keep=("id", "name", "qualified_name", "component_type", "category"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComponentNode":
        return _from(cls, data)

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags


@dataclass
class DependencyEdge:
    """A directed relationship between two nodes (imports, calls, contains...)."""

    id: str
    source_id: str
    target_id: str
    relationship: str
    evidence: list[SourceEvidence] = field(default_factory=list)
    occurrences: int = 1
    direct: bool = True
    cycle_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0
    analyzer: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = _compact(self, keep=("id", "source_id", "target_id", "relationship", "occurrences", "direct"))
        data["confidence"] = round(self.confidence, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DependencyEdge":
        edge = _from(cls, data)
        edge.evidence = [SourceEvidence.from_dict(e) for e in data.get("evidence", [])]
        return edge

    @property
    def in_cycle(self) -> bool:
        return bool(self.cycle_ids)

    def evidence_signature(self) -> frozenset[tuple[str, str, str]]:
        return frozenset(e.signature() for e in self.evidence)


@dataclass
class Cycle:
    """A strongly connected component (size > 1, or a self-loop)."""

    id: str
    level: str
    relationship: str
    members: list[str]
    edge_ids: list[str] = field(default_factory=list)
    example_path: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _compact(self, keep=("members",))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Cycle":
        return _from(cls, data)


@dataclass
class Diagnostic:
    severity: str  # info | warning | error
    code: str
    message: str
    analyzer: str = ""
    path: str | None = None
    line: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _compact(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Diagnostic":
        return _from(cls, data)


@dataclass
class AnalyzerRun:
    """Metadata about one analyzer's participation in a snapshot."""

    name: str
    version: str
    applicable: bool
    reason: str = ""
    capabilities: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = _compact(self, keep=("applicable",))
        data["duration_ms"] = round(self.duration_ms, 1)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnalyzerRun":
        return _from(cls, data)


@dataclass
class SnapshotRef:
    repository_id: str
    revision: str
    revision_id: str
    kind: str
    generated_at: str
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _compact(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SnapshotRef":
        return _from(cls, data)


@dataclass
class RepositorySnapshot:
    repository_id: str
    repository_name: str
    revision: str
    revision_id: str
    kind: str  # commit | worktree | index | session-baseline | filesystem | empty
    generated_at: str
    root: str = ""
    label: str = ""
    components: list[ComponentNode] = field(default_factory=list)
    modules: list[ComponentNode] = field(default_factory=list)
    symbols: list[ComponentNode] = field(default_factory=list)
    containment_edges: list[DependencyEdge] = field(default_factory=list)
    dependency_edges: list[DependencyEdge] = field(default_factory=list)
    call_edges: list[DependencyEdge] = field(default_factory=list)
    cycles: list[Cycle] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    analyzers: list[AnalyzerRun] = field(default_factory=list)
    profile: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    # -- navigation helpers (not serialized) --------------------------------

    def nodes(self) -> Iterator[ComponentNode]:
        yield from self.components
        yield from self.modules
        yield from self.symbols

    def edges(self) -> Iterator[DependencyEdge]:
        yield from self.containment_edges
        yield from self.dependency_edges
        yield from self.call_edges

    def node_index(self) -> dict[str, ComponentNode]:
        cache = self.__dict__.get("_node_index")
        if cache is None or len(cache) != len(self.components) + len(self.modules) + len(self.symbols):
            cache = {n.id: n for n in self.nodes()}
            self.__dict__["_node_index"] = cache
        return cache

    def edge_index(self) -> dict[str, DependencyEdge]:
        return {e.id: e for e in self.edges()}

    def node(self, node_id: str) -> ComponentNode | None:
        return self.node_index().get(node_id)

    def ref(self) -> SnapshotRef:
        return SnapshotRef(
            repository_id=self.repository_id,
            revision=self.revision,
            revision_id=self.revision_id,
            kind=self.kind,
            generated_at=self.generated_at,
            label=self.label or self.revision,
        )

    def find(self, *, path: str | None = None, qualified_name: str | None = None) -> ComponentNode | None:
        for node in self.nodes():
            if path is not None and node.path != path:
                continue
            if qualified_name is not None and node.qualified_name != qualified_name:
                continue
            return node
        return None

    def children(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for node in self.nodes():
            if node.parent_id:
                out.setdefault(node.parent_id, []).append(node.id)
        return out

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository_id": self.repository_id,
            "repository_name": self.repository_name,
            "root": self.root,
            "revision": self.revision,
            "revision_id": self.revision_id,
            "kind": self.kind,
            "label": self.label or self.revision,
            "generated_at": self.generated_at,
            "components": [n.to_dict() for n in self.components],
            "modules": [n.to_dict() for n in self.modules],
            "symbols": [n.to_dict() for n in self.symbols],
            "containment_edges": [e.to_dict() for e in self.containment_edges],
            "dependency_edges": [e.to_dict() for e in self.dependency_edges],
            "call_edges": [e.to_dict() for e in self.call_edges],
            "cycles": [c.to_dict() for c in self.cycles],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "analyzers": [a.to_dict() for a in self.analyzers],
            "profile": self.profile,
            "metadata": _clean(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepositorySnapshot":
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            repository_id=data["repository_id"],
            repository_name=data.get("repository_name", ""),
            root=data.get("root", ""),
            revision=data["revision"],
            revision_id=data.get("revision_id", data["revision"]),
            kind=data.get("kind", "commit"),
            label=data.get("label", ""),
            generated_at=data.get("generated_at", ""),
            components=[ComponentNode.from_dict(n) for n in data.get("components", [])],
            modules=[ComponentNode.from_dict(n) for n in data.get("modules", [])],
            symbols=[ComponentNode.from_dict(n) for n in data.get("symbols", [])],
            containment_edges=[DependencyEdge.from_dict(e) for e in data.get("containment_edges", [])],
            dependency_edges=[DependencyEdge.from_dict(e) for e in data.get("dependency_edges", [])],
            call_edges=[DependencyEdge.from_dict(e) for e in data.get("call_edges", [])],
            cycles=[Cycle.from_dict(c) for c in data.get("cycles", [])],
            diagnostics=[Diagnostic.from_dict(d) for d in data.get("diagnostics", [])],
            analyzers=[AnalyzerRun.from_dict(a) for a in data.get("analyzers", [])],
            profile=data.get("profile"),
            metadata=data.get("metadata", {}),
        )


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------


@dataclass
class NodeChange:
    """A node of the union graph annotated with its diff status."""

    node: ComponentNode
    status: str
    reasons: list[str] = field(default_factory=list)
    before: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = self.node.to_dict()
        data["status"] = self.status
        if self.reasons:
            data["change_reasons"] = list(self.reasons)
        if self.before:
            data["before"] = _clean(self.before)
        return data


@dataclass
class EdgeChange:
    edge: DependencyEdge
    status: str
    reasons: list[str] = field(default_factory=list)
    in_base_cycle: bool = False
    in_target_cycle: bool = False
    base_evidence: list[SourceEvidence] = field(default_factory=list)
    #: Flags (type_checking_only, lazy_only, scope...) of the base version of a modified edge.
    base_flags: dict[str, Any] = field(default_factory=dict)
    #: The base edge this one continues when an endpoint was renamed or moved (same relationship).
    previous_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = self.edge.to_dict()
        data["status"] = self.status
        if self.reasons:
            data["change_reasons"] = list(self.reasons)
        data["in_base_cycle"] = self.in_base_cycle
        data["in_target_cycle"] = self.in_target_cycle
        if self.base_evidence:
            data["base_evidence"] = [e.to_dict() for e in self.base_evidence]
        if self.base_flags:
            data["base_flags"] = dict(self.base_flags)
        if self.previous_id:
            data["previous_id"] = self.previous_id
        return data


@dataclass
class RepositoryDiff:
    base: SnapshotRef
    target: SnapshotRef
    generated_at: str
    added_nodes: list[str] = field(default_factory=list)
    removed_nodes: list[str] = field(default_factory=list)
    modified_nodes: list[str] = field(default_factory=list)
    unchanged_nodes: list[str] = field(default_factory=list)
    added_edges: list[str] = field(default_factory=list)
    removed_edges: list[str] = field(default_factory=list)
    modified_edges: list[str] = field(default_factory=list)
    unchanged_edges: list[str] = field(default_factory=list)
    introduced_cycles: list[Cycle] = field(default_factory=list)
    resolved_cycles: list[Cycle] = field(default_factory=list)
    changed_cycles: list[dict[str, Any]] = field(default_factory=list)
    new_dependencies: list[dict[str, Any]] = field(default_factory=list)
    removed_dependencies: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    nodes: dict[str, NodeChange] = field(default_factory=dict)
    edges: dict[str, EdgeChange] = field(default_factory=dict)
    #: Removed + added node pairs that are the same thing renamed or moved (see ``renames.py``); the old node
    #: is folded into the new one, which is "modified" with ``before.previous_id``.
    renames: list[dict[str, Any]] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def summary(self) -> dict[str, Any]:
        def by_category(ids: Iterable[str]) -> dict[str, int]:
            counts: dict[str, int] = {}
            for i in ids:
                cat = self.nodes[i].node.category
                counts[cat] = counts.get(cat, 0) + 1
            return counts

        def by_rel(ids: Iterable[str]) -> dict[str, int]:
            counts: dict[str, int] = {}
            for i in ids:
                e = self.edges[i].edge
                rel = e.relationship if e.direct else f"{e.relationship} (aggregated)"
                counts[rel] = counts.get(rel, 0) + 1
            return counts

        return {
            "nodes": {
                ADDED: len(self.added_nodes),
                REMOVED: len(self.removed_nodes),
                MODIFIED: len(self.modified_nodes),
                UNCHANGED: len(self.unchanged_nodes),
            },
            "nodes_by_category": {
                ADDED: by_category(self.added_nodes),
                REMOVED: by_category(self.removed_nodes),
                MODIFIED: by_category(self.modified_nodes),
            },
            "edges": {
                ADDED: len(self.added_edges),
                REMOVED: len(self.removed_edges),
                MODIFIED: len(self.modified_edges),
                UNCHANGED: len(self.unchanged_edges),
            },
            "edges_by_relationship": {
                ADDED: by_rel(self.added_edges),
                REMOVED: by_rel(self.removed_edges),
                MODIFIED: by_rel(self.modified_edges),
            },
            "cycles": {
                "introduced": len(self.introduced_cycles),
                "resolved": len(self.resolved_cycles),
                "changed": len(self.changed_cycles),
            },
            "new_dependencies": len(self.new_dependencies),
            "removed_dependencies": len(self.removed_dependencies),
            "renamed": len(self.renames),
        }

    @property
    def has_changes(self) -> bool:
        return bool(self.added_nodes or self.removed_nodes or self.modified_nodes or self.added_edges
                    or self.removed_edges or self.modified_edges)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "base": self.base.to_dict(),
            "target": self.target.to_dict(),
            "generated_at": self.generated_at,
            "summary": self.summary(),
            "added_nodes": self.added_nodes,
            "removed_nodes": self.removed_nodes,
            "modified_nodes": self.modified_nodes,
            "unchanged_nodes": self.unchanged_nodes,
            "added_edges": self.added_edges,
            "removed_edges": self.removed_edges,
            "modified_edges": self.modified_edges,
            "unchanged_edges": self.unchanged_edges,
            "introduced_cycles": [c.to_dict() for c in self.introduced_cycles],
            "resolved_cycles": [c.to_dict() for c in self.resolved_cycles],
            "changed_cycles": _clean(self.changed_cycles),
            "new_dependencies": _clean(self.new_dependencies),
            "removed_dependencies": _clean(self.removed_dependencies),
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "renames": _clean(self.renames),
            "nodes": [c.to_dict() for c in self.nodes.values()],
            "edges": [c.to_dict() for c in self.edges.values()],
        }


# --------------------------------------------------------------------------
# Activity
# --------------------------------------------------------------------------


@dataclass
class ActivityEvent:
    """One file that is currently being modified."""

    path: str
    first_observed: str
    last_observed: str
    git_status: str
    owning_component: str | None = None
    owning_component_name: str | None = None
    module_id: str | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    architecture_impact: list[dict[str, Any]] = field(default_factory=list)
    impact_level: str = "none"  # none | low | medium | high
    tests_affected: list[str] = field(default_factory=list)
    is_test: bool = False
    configuration_affected: bool = False
    configuration_kind: str | None = None
    in_session: bool = False
    staged: bool = False
    unstaged: bool = False
    changed_symbols: list[str] = field(default_factory=list)
    last_modified: str | None = None
    previous_path: str | None = None
    submodule: str | None = None  # set for a file inside a Git submodule
    # Files that usually change together with this one (from Git history) and are not being edited yet.
    companions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _compact(
            self,
            keep=("git_status", "lines_added", "lines_removed", "impact_level", "is_test",
                  "configuration_affected", "in_session", "staged", "unstaged", "tests_affected",
                  "architecture_impact"),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActivityEvent":
        return _from(cls, data)
