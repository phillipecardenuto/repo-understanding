"""Go analyzer (lightweight).

Go packages are directories; imports name packages by path.  Imports whose
path starts with a module declared in a ``go.mod`` of the repository are
resolved to the package directory, everything else becomes an external
package (the standard library is recognised by the absence of a dot in the
first path element).  Top-level ``func`` / method declarations become symbols;
call-flow extraction is not implemented for Go.
"""

from __future__ import annotations

import posixpath
import re

from ..ids import stable_hash
from ..model import CATEGORY_MODULE, CATEGORY_SYMBOL, REL_IMPORTS, ComponentNode
from .base import CAP_DEPENDENCIES, CAP_MODULES, CAP_SYMBOLS, AnalysisContext, Analyzer, Detection, SnapshotBuilder
from .javascript import mask, match_brace

_IMPORT_BLOCK = re.compile(r"^import\s*\(([^)]*)\)", re.M | re.S)
_IMPORT_ONE = re.compile(r"^import\s+(?:[\w.]+\s+)?\"([^\"]+)\"", re.M)
_IMPORT_LINE = re.compile(r"^\s*(?:[\w.]+\s+)?\"([^\"]+)\"", re.M)
_FUNC = re.compile(r"^func\s+(?:\(\s*\w*\s*\*?\s*([\w]+)(?:\[[^\]]*\])?\s*\)\s*)?([\w]+)\s*(?:\[[^\]]*\])?\s*\(", re.M)
_PACKAGE = re.compile(r"^package\s+(\w+)", re.M)


class GoAnalyzer(Analyzer):
    name = "go"
    version = "1"
    languages = ("go",)
    capabilities = (CAP_MODULES, CAP_SYMBOLS, CAP_DEPENDENCIES)

    def detect(self, ctx: AnalysisContext) -> Detection:
        n = len(ctx.files("go"))
        return Detection(bool(n), f"{n} Go file(s)" if n else "no Go files")

    def _modules(self, ctx: AnalysisContext) -> list[tuple[str, str]]:
        mods = [(md.dir, md.name) for md in ctx.profile.manifest_data.values() if md.kind == "go.mod" and md.name]
        return sorted(mods, key=lambda m: -len(m[1]))

    def discover_modules(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        mods = self._modules(ctx)
        ctx.shared["go.infos"] = infos = {}
        for f in ctx.files("go"):
            text = ctx.text(f)
            if text is None:
                continue
            code, _ = mask(text)
            pkg = _PACKAGE.search(code)
            imports: list[tuple[str, int]] = []
            for m in _IMPORT_BLOCK.finditer(text):
                if code[m.start()] != "i":
                    continue
                for im in _IMPORT_LINE.finditer(m.group(1)):
                    line = text.count("\n", 0, m.start(1) + im.start(1)) + 1
                    imports.append((im.group(1), line))
            for m in _IMPORT_ONE.finditer(text):
                if code[m.start()] == "i":
                    imports.append((m.group(1), text.count("\n", 0, m.start()) + 1))
            funcs = []
            for m in _FUNC.finditer(code):
                brace = code.find("{", m.end())
                end = match_brace(code, brace) if brace >= 0 else m.end()
                funcs.append((m.group(1), m.group(2), m.start(), end))
            infos[f] = (pkg.group(1) if pkg else None, imports, funcs, text)
            directory = posixpath.dirname(f)
            mod_dir, mod_path = next(((d, p) for d, p in mods if d == "" or directory == d
                                      or directory.startswith(d + "/")), ("", None))
            rel = directory[len(mod_dir):].strip("/") if mod_dir else directory
            import_path = f"{mod_path}/{rel}".rstrip("/") if mod_path else directory
            node = b.ensure_file(f, self.name)
            b.add_node(ComponentNode(
                id=node.id, name=posixpath.basename(f), qualified_name=f"{import_path}/{posixpath.basename(f)}",
                component_type="module", category=CATEGORY_MODULE, language="go", path=f, analyzer=self.name,
                key=node.key, tags=["go"] + (["test"] if f.endswith("_test.go") else []),
                metadata={"go_package": pkg.group(1) if pkg else None, "import_path": import_path}))
            node.category = CATEGORY_MODULE
            if directory:
                dnode = b.nodes[b.ensure_dir(directory, self.name)]
                b.add_node(ComponentNode(
                    id=dnode.id, name=dnode.name, qualified_name=import_path, component_type="package",
                    language="go", path=directory, analyzer=self.name, key=dnode.key, tags=["go"],
                    metadata={"qualified_name_authoritative": True, "go_package": pkg.group(1) if pkg else None}))
            if pkg and pkg.group(1) == "main" and any(fn[1] == "main" and fn[0] is None for fn in funcs):
                node.tags.append("entry-point")
                node.metadata["entry_kind"] = "go main package"
        for d, _p in mods:
            if d:
                dn = b.get(b.dir_id(d))
                if dn is not None and "component" not in dn.tags:
                    dn.tags.append("component")

    def discover_symbols(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        for f, (_pkg, _imports, funcs, text) in ctx.shared.get("go.infos", {}).items():
            for receiver, name, start, end in funcs:
                qual = f"{receiver}.{name}" if receiver else name
                line = text.count("\n", 0, start) + 1
                b.add_node(ComponentNode(
                    id=b.symbol_id(f, qual), name=name, qualified_name=f"{b.nodes[b.file_id(f)].metadata.get('import_path')}.{qual}",
                    component_type="method" if receiver else "function", category=CATEGORY_SYMBOL, language="go",
                    path=f, parent_id=b.file_id(f), analyzer=self.name, key=f"symbol:{f}:{qual}",
                    fingerprint=stable_hash(" ".join(text[start:end + 1].split())), start_line=line,
                    end_line=text.count("\n", 0, end) + 1, metadata={"kind": "method" if receiver else "function"}))
        if ctx.shared.get("go.infos"):
            b.diagnostic("info", "callflow-unsupported", "Call-flow extraction is not implemented for Go; the Activity "
                         "tab falls back to package-level import impact.", self.name)

    def discover_dependencies(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        mods = self._modules(ctx)
        dirs = {posixpath.dirname(f) for f in ctx.files("go")}
        for f, (_pkg, imports, _funcs, _text) in ctx.shared.get("go.infos", {}).items():
            src = b.file_id(f)
            for path, line in imports:
                ev = self.evidence(ctx, f, line, line, "import")
                target_dir = None
                for mod_dir, mod_path in mods:
                    if path == mod_path or path.startswith(mod_path + "/"):
                        rel = path[len(mod_path):].strip("/")
                        cand = posixpath.normpath(posixpath.join(mod_dir, rel)) if rel else mod_dir
                        cand = "" if cand == "." else cand
                        if cand in dirs:
                            target_dir = cand
                        break
                if target_dir is not None:
                    tid = b.dir_id(target_dir) if target_dir else b.root_id
                    if tid in b.nodes and tid != b.nodes[src].parent_id:
                        b.add_edge(src, tid, REL_IMPORTS, analyzer=self.name, evidence=[ev], confidence=1.0,
                                   metadata={"imported_names": [path]})
                    continue
                std = "." not in path.split("/")[0]
                eco = "go-stdlib" if std else "go"
                name = path if std else "/".join(path.split("/")[:3])
                ext_id = b.external_id(eco, name)
                if ext_id not in b.nodes:
                    b.add_node(ComponentNode(id=ext_id, name=name, qualified_name=name,
                                             component_type="external-package", language="go", analyzer=self.name,
                                             key=f"external:{eco}:{name}",
                                             tags=["external"] + (["stdlib"] if std else []),
                                             metadata={"ecosystem": "go"}))
                b.add_edge(src, ext_id, REL_IMPORTS, analyzer=self.name, evidence=[ev], confidence=0.95,
                           metadata={"external": True, "imported_names": [path]})
