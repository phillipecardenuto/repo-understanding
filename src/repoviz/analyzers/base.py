"""The analyzer plugin interface.

An analyzer is a class with a ``name`` and a set of ``discover_*`` hooks.  The
pipeline calls :meth:`Analyzer.detect` once and then runs the hooks phase by
phase *across all applicable analyzers*, so later phases can rely on what
earlier phases produced by any analyzer (for example the call-flow resolver
uses symbols discovered by the Python and JavaScript analyzers).

Phases, in order::

    components -> modules -> containment -> symbols -> entry_points
               -> dependencies -> calls -> finalize

Analyzers never execute repository code and only read content through the
:class:`AnalysisContext` (which wraps a :class:`~repoviz.sources.TreeSource`).
"""

from __future__ import annotations

import posixpath
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from ..config import Config
from ..ids import IdRegistry, edge_id
from ..model import (
    CATEGORY_COMPONENT,
    CATEGORY_MODULE,
    REL_CONTAINS,
    ComponentNode,
    DependencyEdge,
    Diagnostic,
    SourceEvidence,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..discovery import RepositoryProfile
    from ..sources import TreeSource

PHASES = ("components", "modules", "containment", "symbols", "entry_points", "dependencies", "calls", "finalize")

CAP_COMPONENTS = "components"
CAP_MODULES = "modules"
CAP_CONTAINMENT = "containment"
CAP_DEPENDENCIES = "dependencies"
CAP_SYMBOLS = "symbols"
CAP_ENTRY_POINTS = "entry_points"
CAP_CALLS = "calls"
CAP_EVIDENCE = "evidence"
CAP_DIAGNOSTICS = "diagnostics"


@dataclass
class Detection:
    applicable: bool
    reason: str = ""
    confidence: float = 1.0


class Analyzer:
    """Base class for analyzers.  Override the hooks you support."""

    name: str = "analyzer"
    version: str = "1"
    languages: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    #: Mandatory analyzers cannot be disabled through configuration.
    mandatory: bool = False

    def detect(self, ctx: "AnalysisContext") -> Detection:
        """Decide whether this analyzer applies to the repository."""
        return Detection(True, "always applicable")

    # -- discovery hooks (default: no-op) ------------------------------------

    def discover_components(self, ctx: "AnalysisContext", b: "SnapshotBuilder") -> None:
        """Projects, services, containers, top-level areas."""

    def discover_modules(self, ctx: "AnalysisContext", b: "SnapshotBuilder") -> None:
        """Importable/compilable units (files, packages)."""

    def discover_containment(self, ctx: "AnalysisContext", b: "SnapshotBuilder") -> None:
        """Parent/child structure (``parent_id``); edges are derived automatically."""

    def discover_symbols(self, ctx: "AnalysisContext", b: "SnapshotBuilder") -> None:
        """Classes, functions and methods."""

    def discover_entry_points(self, ctx: "AnalysisContext", b: "SnapshotBuilder") -> None:
        """Executable entry points (scripts, mains, binaries, containers)."""

    def discover_dependencies(self, ctx: "AnalysisContext", b: "SnapshotBuilder") -> None:
        """Import / manifest dependencies between nodes."""

    def discover_calls(self, ctx: "AnalysisContext", b: "SnapshotBuilder") -> None:
        """Direct call relationships between symbols."""

    def finalize(self, ctx: "AnalysisContext", b: "SnapshotBuilder") -> None:
        """Post-processing once every analyzer ran the other phases."""

    # -- helpers -------------------------------------------------------------------

    def evidence(self, ctx: "AnalysisContext", path: str, start: int | None, end: int | None = None,
                 construct: str = "") -> SourceEvidence:
        """Build source evidence with an excerpt of the referenced lines."""
        return SourceEvidence(path=path, start_line=start, end_line=end or start, construct=construct,
                              analyzer=self.name, excerpt=ctx.excerpt(path, start, end))

    def run_phase(self, phase: str, ctx: "AnalysisContext", b: "SnapshotBuilder") -> None:
        hook = getattr(self, "finalize" if phase == "finalize" else f"discover_{phase}")
        hook(ctx, b)


@dataclass
class AnalysisContext:
    """Everything an analyzer may read."""

    source: "TreeSource"
    profile: "RepositoryProfile"
    config: Config
    git: Any = None
    #: Cross-snapshot cache for per-file results, keyed by (analyzer, content hash, ...).
    file_cache: dict[Any, Any] = field(default_factory=dict)
    #: Data shared between analyzers within one snapshot (e.g. the call index).
    shared: dict[str, Any] = field(default_factory=dict)
    _lines: dict[str, list[str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def files(self, language: str | None = None) -> list[str]:
        if language is None:
            return list(self.profile.included_files)
        return [f for f in self.profile.included_files if self.profile.file_languages.get(f, (None, None))[0] == language]

    def text(self, path: str) -> str | None:
        size = self.source.size(path)
        if size is not None and size > self.config.max_file_bytes:
            return None
        return self.source.read_text(path, self.config.max_file_bytes)

    def lines(self, path: str) -> list[str]:
        with self._lock:
            cached = self._lines.get(path)
        if cached is None:
            text = self.text(path)
            cached = text.splitlines() if text is not None else []
            with self._lock:
                self._lines[path] = cached
        return cached

    def excerpt(self, path: str, start: int | None, end: int | None = None, max_lines: int = 3) -> str | None:
        if start is None:
            return None
        lines = self.lines(path)
        if not lines or start < 1 or start > len(lines):
            return None
        end = min(end or start, start + max_lines - 1, len(lines))
        text = "\n".join(l.rstrip() for l in lines[start - 1:end])
        return text[:400]

    def cached(self, key: tuple[Any, ...], compute: Any) -> Any:
        """Per-file cache that survives across snapshots (keys must include a content hash)."""
        with self._lock:
            if key in self.file_cache:
                return self.file_cache[key]
        value = compute()
        with self._lock:
            self.file_cache[key] = value
        return value


# Merge precedence of component types: more specific types win.
_TYPE_RANK = {
    "file": 0, "directory": 0, "module": 10, "package": 10, "namespace-package": 9, "tests": 5, "docs": 5,
    "container": 12, "config": 3, "ci-pipeline": 12, "project": 20, "workspace-member": 25, "workspace": 26,
    "repository": 100,
}
# Metadata flags that stay true only if *every* occurrence has them.
_AND_FLAGS = ("type_checking_only", "conditional_only", "lazy_only", "dynamic_only", "test_only")


class SnapshotBuilder:
    """Accumulates nodes, edges and diagnostics, merging contributions by ID."""

    def __init__(self) -> None:
        self.ids = IdRegistry()
        self.nodes: dict[str, ComponentNode] = {}
        self.edges: dict[str, DependencyEdge] = {}
        self.diagnostics: list[Diagnostic] = []
        self._diag_keys: set[tuple[Any, ...]] = set()
        self.root_id: str | None = None
        #: Snapshot-level metadata contributed by analyzers (branch, HEAD...).
        self.metadata: dict[str, Any] = {}
        #: Per-analyzer statistics reported in ``AnalyzerRun.stats``.
        self.stats: dict[str, dict[str, Any]] = {}

    def stat(self, analyzer: str, key: str, increment: int = 1) -> None:
        bucket = self.stats.setdefault(analyzer, {})
        bucket[key] = bucket.get(key, 0) + increment

    # -- identifiers -------------------------------------------------------------

    def id_for(self, prefix: str, key: str) -> str:
        return self.ids.get(prefix, key)

    def dir_id(self, path: str) -> str:
        return self.id_for("dir", f"path:dir:{path}")

    def file_id(self, path: str) -> str:
        return self.id_for("file", f"path:file:{path}")

    def symbol_id(self, path: str, qualname: str) -> str:
        return self.id_for("sym", f"symbol:{path}:{qualname}")

    def external_id(self, ecosystem: str, name: str) -> str:
        return self.id_for("ext", f"external:{ecosystem}:{name}")

    def path_node_id(self, path: str) -> str | None:
        """ID of the directory or file node at ``path`` if it exists."""
        for ident in (self.file_id(path), self.dir_id(path)):
            if ident in self.nodes:
                return ident
        return None

    # -- nodes -----------------------------------------------------------------------

    def add_node(self, node: ComponentNode) -> ComponentNode:
        existing = self.nodes.get(node.id)
        if existing is None:
            if node.analyzer and node.analyzer not in node.analyzers:
                node.analyzers.append(node.analyzer)
            self.nodes[node.id] = node
            return node
        self._merge_node(existing, node)
        return existing

    def _merge_node(self, into: ComponentNode, new: ComponentNode) -> None:
        if _TYPE_RANK.get(new.component_type, 10) > _TYPE_RANK.get(into.component_type, 10):
            into.component_type = new.component_type
        if new.category == CATEGORY_MODULE and into.category == CATEGORY_COMPONENT and into.component_type in (
                "file", "module"):
            into.category = CATEGORY_MODULE
        if new.language and (not into.language or into.analyzer in ("filesystem", "")):
            into.language = new.language
        if new.qualified_name and (into.qualified_name == into.path or not into.qualified_name or
                                   new.metadata.get("qualified_name_authoritative")):
            into.qualified_name = new.qualified_name
        if new.name and not into.name:
            into.name = new.name
        if new.fingerprint and not into.fingerprint:
            into.fingerprint = new.fingerprint
        if new.parent_id and not into.parent_id:
            into.parent_id = new.parent_id
        into.start_line = into.start_line or new.start_line
        into.end_line = into.end_line or new.end_line
        for tag in new.tags:
            if tag not in into.tags:
                into.tags.append(tag)
        for a in [new.analyzer, *new.analyzers]:
            if a and a not in into.analyzers:
                into.analyzers.append(a)
        for k, v in new.metadata.items():
            if k == "qualified_name_authoritative":
                continue
            if isinstance(v, list) and isinstance(into.metadata.get(k), list):
                into.metadata[k] = list(dict.fromkeys([*into.metadata[k], *v]))
            elif isinstance(v, dict) and isinstance(into.metadata.get(k), dict):
                into.metadata[k] = {**into.metadata[k], **v}
            else:
                into.metadata[k] = v

    def get(self, node_id: str | None) -> ComponentNode | None:
        return self.nodes.get(node_id) if node_id else None

    def ensure_dir(self, path: str, analyzer: str) -> str:
        """Create directory nodes for ``path`` and all its ancestors; return the ID."""
        if path in ("", "."):
            assert self.root_id is not None, "repository root must be created first"
            return self.root_id
        ident = self.dir_id(path)
        if ident not in self.nodes:
            parent = self.ensure_dir(posixpath.dirname(path), analyzer)
            self.add_node(ComponentNode(
                id=ident, name=posixpath.basename(path), qualified_name=path, component_type="directory",
                category=CATEGORY_COMPONENT, path=path, parent_id=parent, analyzer=analyzer, key=f"path:dir:{path}",
            ))
        return ident

    def ensure_file(self, path: str, analyzer: str, **kwargs: Any) -> ComponentNode:
        """Create (or enrich) the node of a file; attributes merge into an existing node."""
        ident = self.file_id(path)
        existing = self.nodes.get(ident)
        return self.add_node(ComponentNode(
            id=ident, name=posixpath.basename(path), qualified_name=path,
            component_type=kwargs.pop("component_type", "file"),
            category=kwargs.pop("category", existing.category if existing else CATEGORY_COMPONENT), path=path,
            parent_id=existing.parent_id if existing else self.ensure_dir(posixpath.dirname(path), analyzer),
            analyzer=analyzer, key=f"path:file:{path}", **kwargs,
        ))

    # -- edges ---------------------------------------------------------------------------

    def add_edge(self, source_id: str, target_id: str, relationship: str, *, analyzer: str,
                 evidence: Iterable[SourceEvidence] = (), confidence: float = 1.0, direct: bool = True,
                 metadata: dict[str, Any] | None = None, occurrences: int | None = None) -> DependencyEdge:
        ident = edge_id(relationship, source_id, target_id) if direct else \
            edge_id(f"{relationship}@aggregated", source_id, target_id)
        evidence = list(evidence)
        metadata = dict(metadata or {})
        count = occurrences if occurrences is not None else max(1, len(evidence))
        existing = self.edges.get(ident)
        if existing is None:
            edge = DependencyEdge(id=ident, source_id=source_id, target_id=target_id, relationship=relationship,
                                  evidence=evidence, occurrences=count, direct=direct, confidence=confidence,
                                  analyzer=analyzer, metadata=metadata)
            self.edges[ident] = edge
            return edge
        seen = {(e.path, e.start_line, e.construct) for e in existing.evidence}
        new_sites = 0
        for ev in evidence:
            if (ev.path, ev.start_line, ev.construct) not in seen:
                existing.evidence.append(ev)
                seen.add((ev.path, ev.start_line, ev.construct))
                new_sites += 1
        # Occurrences count distinct source sites (``from x import a, b`` is one site).
        existing.occurrences += new_sites if evidence and occurrences is None else count
        existing.confidence = max(existing.confidence, confidence)
        for k, v in metadata.items():
            if k in _AND_FLAGS:
                existing.metadata[k] = bool(existing.metadata.get(k, False)) and bool(v)
            elif isinstance(v, list):
                existing.metadata[k] = list(dict.fromkeys([*existing.metadata.get(k, []), *v]))
            else:
                existing.metadata.setdefault(k, v)
        for flag in _AND_FLAGS:
            if flag in existing.metadata and flag not in metadata:
                existing.metadata[flag] = False
        if analyzer and analyzer != existing.analyzer:
            existing.metadata.setdefault("also_detected_by", [])
            if analyzer not in existing.metadata["also_detected_by"]:
                existing.metadata["also_detected_by"].append(analyzer)
        return existing

    # -- diagnostics -------------------------------------------------------------------

    def diagnostic(self, severity: str, code: str, message: str, analyzer: str, path: str | None = None,
                   line: int | None = None, **details: Any) -> None:
        key = (severity, code, message, path, line)
        if key in self._diag_keys:
            return
        self._diag_keys.add(key)
        self.diagnostics.append(Diagnostic(severity, code, message, analyzer, path, line, details))

    # -- structure helpers --------------------------------------------------------------

    def containment_edges(self) -> list[DependencyEdge]:
        out = []
        for node in self.nodes.values():
            if node.parent_id and node.parent_id in self.nodes:
                out.append(DependencyEdge(id=edge_id(REL_CONTAINS, node.parent_id, node.id),
                                          source_id=node.parent_id, target_id=node.id, relationship=REL_CONTAINS,
                                          analyzer="pipeline", occurrences=1))
        return out
