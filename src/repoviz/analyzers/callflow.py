"""Static call-flow analyzer (language neutral).

Language analyzers describe each module as a :class:`ModuleScope` (its symbols,
the names it binds through imports, its sub-modules and star imports) and
record raw call sites such as ``helper()``, ``util.parse()`` or
``self.save()``.  This analyzer resolves those call sites into ``calls``
edges between symbols, following import aliases and re-export chains across
modules and walking class hierarchies for method calls.

It also resolves entry points declared in manifests (``pkg.cli:main``,
``python -m pkg``, ``bin/cli.js``) into ``invokes`` edges.

Resolution is purely static and deliberately conservative: calls through
arbitrary objects, dynamic dispatch or external libraries are counted as
unresolved rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import REL_CALLS, REL_INVOKES, SourceEvidence
from .base import CAP_CALLS, CAP_ENTRY_POINTS, AnalysisContext, Analyzer, Detection, SnapshotBuilder

MAX_DEPTH = 12


@dataclass
class ModuleScope:
    key: str  # language-qualified module key, e.g. "py:pkg.mod" or "js:src/app.ts"
    node_id: str
    language: str
    path: str
    symbols: dict[str, str] = field(default_factory=dict)  # local qualname ("Cls.meth") -> symbol id
    kinds: dict[str, str] = field(default_factory=dict)  # local qualname -> class | function | method ...
    bindings: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)  # name -> (module key, attrs)
    submodules: dict[str, str] = field(default_factory=dict)  # attribute -> module key
    star_imports: list[str] = field(default_factory=list)
    class_bases: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)
    main_module: str | None = None  # for packages: key of the ``__main__`` sub-module


@dataclass
class RawCall:
    caller_id: str
    module_key: str
    class_qual: str | None
    parts: tuple[str, ...]
    path: str
    line: int
    excerpt: str | None = None
    caller_qual: str | None = None  # local qualname of the calling symbol (for nested definitions)


@dataclass
class EntryTarget:
    entry_id: str
    target: str
    target_kind: str  # python-callable | python-module | file | command
    evidence: SourceEvidence | None = None


class CallIndex:
    """Cross-module symbol table used for static call resolution."""

    def __init__(self) -> None:
        self.modules: dict[str, ModuleScope] = {}
        self.calls: list[RawCall] = []
        self.entry_targets: list[EntryTarget] = []
        self.file_nodes: dict[str, str] = {}  # path -> module node id

    def add_module(self, scope: ModuleScope) -> None:
        self.modules[scope.key] = scope
        self.file_nodes[scope.path] = scope.node_id

    # -- resolution ------------------------------------------------------------

    def resolve_call(self, call: RawCall) -> tuple[str, float] | None:
        scope = self.modules.get(call.module_key)
        if scope is None or not call.parts:
            return None
        head, rest = call.parts[0], call.parts[1:]
        if head in ("self", "cls") and call.class_qual and len(rest) == 1:
            found = self._method(scope, call.class_qual, rest[0], 0)
            return (found, 0.85) if found else None
        if head == "super()" and call.class_qual and len(rest) == 1:
            for base in scope.class_bases.get(call.class_qual, []):
                target = self._resolve_parts(scope, base, 0)
                if target and target[0].kinds.get(target[1]) == "class":
                    found = self._method(target[0], target[1], rest[0], 0)
                    if found:
                        return found, 0.75
            return None
        target = None
        if call.caller_qual and head not in scope.bindings:
            # Nested definitions: look in enclosing scopes first (outer.inner, then outer's parents).
            enclosing = call.caller_qual.split("#")[0]
            while enclosing:
                # Class bodies are not enclosing scopes for the functions defined in them.
                candidate = f"{enclosing}.{head}"
                if scope.kinds.get(enclosing) != "class" and candidate in scope.symbols:
                    target = self._descend(scope, candidate, rest, 0)
                    break
                enclosing = enclosing.rpartition(".")[0]
        if target is None:
            target = self._resolve_parts(scope, call.parts, 0)
        if target is None:
            return None
        tscope, qual = target
        sid = self._callable_symbol(tscope, qual)
        if sid is None:
            return None
        confidence = 0.95 if head in scope.symbols else 0.9
        return sid, confidence

    def _callable_symbol(self, scope: ModuleScope, qual: str) -> str | None:
        if qual not in scope.symbols:
            return None
        if scope.kinds.get(qual) == "class":
            init = self._method(scope, qual, "__init__", 0) or self._method(scope, qual, "constructor", 0)
            return init or scope.symbols[qual]
        return scope.symbols[qual]

    def _resolve_parts(self, scope: ModuleScope, parts: tuple[str, ...], depth: int) -> tuple[ModuleScope, str] | None:
        """Resolve a dotted reference used inside ``scope`` to (scope, local qualname)."""
        if depth > MAX_DEPTH or not parts:
            return None
        head, rest = parts[0], parts[1:]
        if head in scope.symbols and "." not in head:
            return self._descend(scope, head, rest, depth)
        if head in scope.bindings:
            mkey, attrs = scope.bindings[head]
            return self._resolve_in_module(mkey, attrs + rest, depth + 1)
        for star in scope.star_imports:
            found = self._resolve_in_module(star, parts, depth + 1)
            if found:
                return found
        return None

    def _resolve_in_module(self, mkey: str, attrs: tuple[str, ...], depth: int) -> tuple[ModuleScope, str] | None:
        if depth > MAX_DEPTH:
            return None
        scope = self.modules.get(mkey)
        if scope is None or not attrs:
            return None
        head, rest = attrs[0], attrs[1:]
        if head in scope.symbols and "." not in head:
            return self._descend(scope, head, rest, depth)
        if head in scope.submodules:
            return self._resolve_in_module(scope.submodules[head], rest, depth + 1)
        if head in scope.bindings:
            m2, a2 = scope.bindings[head]
            return self._resolve_in_module(m2, a2 + rest, depth + 1)
        for star in scope.star_imports:
            found = self._resolve_in_module(star, attrs, depth + 1)
            if found:
                return found
        return None

    def _descend(self, scope: ModuleScope, qual: str, rest: tuple[str, ...], depth: int) -> tuple[ModuleScope, str] | None:
        if not rest:
            return scope, qual
        nested = f"{qual}.{rest[0]}"
        if nested in scope.symbols:
            return self._descend(scope, nested, rest[1:], depth)
        if scope.kinds.get(qual) == "class" and len(rest) == 1:
            owner = self._method_owner(scope, qual, rest[0], depth)
            if owner:
                return owner
        return None

    def _method_owner(self, scope: ModuleScope, class_qual: str, name: str, depth: int) -> tuple[ModuleScope, str] | None:
        if depth > MAX_DEPTH:
            return None
        q = f"{class_qual}.{name}"
        if q in scope.symbols:
            return scope, q
        for base in scope.class_bases.get(class_qual, []):
            target = self._resolve_parts(scope, base, depth + 1)
            if target and target[0].kinds.get(target[1]) == "class":
                found = self._method_owner(target[0], target[1], name, depth + 1)
                if found:
                    return found
        return None

    def _method(self, scope: ModuleScope, class_qual: str, name: str, depth: int) -> str | None:
        owner = self._method_owner(scope, class_qual, name, depth)
        return owner[0].symbols[owner[1]] if owner else None

    def resolve_python_target(self, target: str) -> str | None:
        """Resolve ``pkg.mod:attr.path`` or ``pkg.mod`` to a node ID."""
        module, _, attr = target.partition(":")
        module = module.strip()
        attr = attr.strip().split()[0] if attr.strip() else ""
        attr = attr.split("[", 1)[0]
        scope = self.modules.get(f"py:{module}")
        if scope is None:
            return None
        if not attr:
            if scope.main_module and scope.main_module in self.modules:
                return self.modules[scope.main_module].node_id
            return scope.node_id
        found = self._resolve_in_module(scope.key, tuple(attr.split(".")), 0)
        if found:
            return found[0].symbols.get(found[1])
        return scope.node_id


class CallFlowAnalyzer(Analyzer):
    name = "callflow"
    version = "1"
    capabilities = (CAP_CALLS, CAP_ENTRY_POINTS)

    def detect(self, ctx: AnalysisContext) -> Detection:
        return Detection(True, "resolves call sites and entry points reported by language analyzers")

    def discover_calls(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        index: CallIndex | None = ctx.shared.get("call_index")
        if index is None:
            return
        resolved = unresolved = 0
        for call in index.calls:
            result = index.resolve_call(call)
            if result is None:
                unresolved += 1
                continue
            target_id, confidence = result
            if target_id == call.caller_id:
                b.stat(self.name, "recursive_calls")
            resolved += 1
            ev = SourceEvidence(call.path, call.line, call.line, "call", self.name, call.excerpt)
            b.add_edge(call.caller_id, target_id, REL_CALLS, analyzer=self.name, evidence=[ev],
                       confidence=confidence, metadata={"callees": [".".join(call.parts)]})
        b.stat(self.name, "resolved_calls", resolved)
        b.stat(self.name, "unresolved_calls", unresolved)
        if unresolved:
            b.diagnostic("info", "unresolved-calls",
                         f"{unresolved} of {resolved + unresolved} call sites could not be resolved statically "
                         "(calls through objects, dynamic dispatch, builtins or external libraries).", self.name)

        for et in index.entry_targets:
            target_id = None
            if et.target_kind in ("python-callable", "python-module"):
                target_id = index.resolve_python_target(et.target)
            elif et.target_kind == "file":
                path = et.target.removeprefix("./")
                target_id = index.file_nodes.get(path) or b.path_node_id(path)
                if target_id is None:
                    for ext in (".ts", ".tsx", ".js", ".mjs", ".cjs"):
                        stem = path.rsplit(".", 1)[0]
                        target_id = index.file_nodes.get(stem + ext) or b.path_node_id(stem + ext)
                        if target_id:
                            break
            if target_id and target_id in b.nodes:
                b.add_edge(et.entry_id, target_id, REL_INVOKES, analyzer=self.name,
                           evidence=[et.evidence] if et.evidence else [], confidence=0.9)
                b.stat(self.name, "entry_points_resolved")
            elif et.target_kind != "command":
                b.stat(self.name, "entry_points_unresolved")


def get_index(ctx: AnalysisContext) -> CallIndex:
    index = ctx.shared.get("call_index")
    if index is None:
        index = CallIndex()
        ctx.shared["call_index"] = index
    return index
