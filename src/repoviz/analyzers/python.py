"""Python analyzer.

Uses the :mod:`ast` module on the content provided by the tree source, so the
same code path works for commits, the index, the working tree and session
baselines -- and repository code is never imported or executed.

Produces:

* module nodes (one per ``.py`` file) and package nodes (regular and
  namespace packages), with qualified names derived from discovered or
  configured source roots;
* ``imports`` edges with source evidence and flags for imports that only
  happen under ``TYPE_CHECKING``, inside ``try/except ImportError`` or ``if``
  blocks (conditional), inside functions (lazy) or through
  ``importlib.import_module`` (dynamic);
* classes, functions and methods with line ranges and AST fingerprints;
* raw call sites and module scopes for the call-flow analyzer;
* entry points: ``if __name__ == "__main__"`` blocks, ``__main__.py``,
  route/command-style decorated handlers and test functions.

When `grimp <https://github.com/seddonym/grimp>`_ is installed, working-tree
snapshots are cross-checked against grimp's import graph (see
``python.use_grimp`` in the configuration).
"""

from __future__ import annotations

import ast
import builtins
import importlib
import importlib.util
import os
import posixpath
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import classify
from ..ids import stable_hash
from ..manifests import normalize_python_name
from ..model import CATEGORY_MODULE, CATEGORY_SYMBOL, REL_IMPORTS, ComponentNode, SourceEvidence
from .base import (
    CAP_CALLS,
    CAP_CONTAINMENT,
    CAP_DEPENDENCIES,
    CAP_DIAGNOSTICS,
    CAP_ENTRY_POINTS,
    CAP_EVIDENCE,
    CAP_MODULES,
    CAP_SYMBOLS,
    AnalysisContext,
    Analyzer,
    Detection,
    SnapshotBuilder,
)
from .callflow import ModuleScope, RawCall, get_index

STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
PY_BUILTINS = set(dir(builtins))

# Import name -> distribution name, for the common cases where they differ.
IMPORT_TO_DIST = {
    "yaml": "pyyaml", "pil": "pillow", "sklearn": "scikit-learn", "cv2": "opencv-python", "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil", "attr": "attrs", "dotenv": "python-dotenv", "jwt": "pyjwt", "magic": "python-magic",
    "google": "google", "serial": "pyserial", "usb": "pyusb", "crypto": "pycryptodome", "skimage": "scikit-image",
    "multipart": "python-multipart", "jose": "python-jose", "git": "gitpython", "docx": "python-docx",
    "pptx": "python-pptx", "fitz": "pymupdf", "zmq": "pyzmq", "mysqldb": "mysqlclient", "psycopg2": "psycopg2",
    "win32api": "pywin32", "ruamel": "ruamel.yaml", "typing_extensions": "typing-extensions",
}

HANDLER_DECORATORS = {"route", "get", "post", "put", "patch", "delete", "head", "options", "websocket", "command",
                      "group", "task", "shared_task", "api_view", "on_event", "listener", "handler", "callback",
                      "endpoint", "subscribe", "consumer", "job", "cron", "schedule", "hookimpl"}


# --------------------------------------------------------------------------
# Per-file extraction (cached by content hash; independent of module name)
# --------------------------------------------------------------------------


@dataclass
class RawImport:
    kind: str  # import | from | dynamic
    module: str | None
    level: int
    names: list[tuple[str, str | None]]
    line: int
    end_line: int
    type_checking: bool = False
    conditional: bool = False
    lazy: bool = False


@dataclass
class RawSymbol:
    qualname: str
    name: str
    kind: str  # class | function | method | main-block
    line: int
    end_line: int
    fingerprint: str
    parent: str | None
    decorators: list[str] = field(default_factory=list)
    bases: list[tuple[str, ...]] = field(default_factory=list)
    signature: str = ""
    is_async: bool = False
    doc: str = ""
    body_fingerprint: str = ""  # the definition without its own name: survives a rename


@dataclass
class RawCallSite:
    caller: str  # symbol qualname, "" for module level
    class_qual: str | None
    parts: tuple[str, ...]
    line: int


@dataclass
class PyFileInfo:
    error: str | None = None
    error_line: int | None = None
    generated: bool = False
    imports: list[RawImport] = field(default_factory=list)
    symbols: list[RawSymbol] = field(default_factory=list)
    calls: list[RawCallSite] = field(default_factory=list)
    unresolvable_calls: int = 0
    semantic_fingerprint: str | None = None
    loc: int = 0
    all_names: list[str] | None = None
    top_level_names: list[str] = field(default_factory=list)


def _dotted(node: ast.AST) -> tuple[str, ...] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return tuple(reversed(parts))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super" and parts:
        parts.append("super()")
        return tuple(reversed(parts))
    return None


def _is_type_checking(test: ast.AST) -> bool:
    d = _dotted(test)
    return bool(d) and d[-1] == "TYPE_CHECKING"


def _is_main_guard(test: ast.AST) -> bool:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    sides = [test.left, *test.comparators]
    names = [s for s in sides if isinstance(s, ast.Name) and s.id == "__name__"]
    consts = [s for s in sides if isinstance(s, ast.Constant) and s.value == "__main__"]
    return bool(names and consts)


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for t in types:
        d = _dotted(t)
        if d and d[-1] in ("ImportError", "ModuleNotFoundError", "Exception", "BaseException"):
            return True
    return False


def _local_names(fn: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    names: set[str] = set()
    args = fn.args
    for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        names.add(a.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    declared_global: set[str] = set()
    stack: list[ast.AST] = list(fn.body) if not isinstance(fn, ast.Lambda) else [fn.body]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # nested definitions are symbols of their own (resolved by the call index)
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared_global.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        stack.extend(ast.iter_child_nodes(node))
    return names - declared_global


def normalized_source(lines: list[str]) -> str:
    """Source text with blank lines, comment lines and (safe) trailing comments removed.

    Used for fingerprints so that formatting- or comment-only edits are recognised
    as such.  Fast (no tokenization); inline comments are only stripped on lines
    without string quotes.
    """
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "#" in line and "'" not in line and '"' not in line:
            line = line.split("#", 1)[0]
        out.append(line.rstrip())
    return "\n".join(out)


class _Extractor:
    def __init__(self, info: PyFileInfo, lines: list[str], dynamic: bool = True) -> None:
        self.info = info
        self.lines = lines
        self.scan_dynamic = dynamic

    def fingerprint(self, node: ast.AST) -> str:
        start = getattr(node, "lineno", 1)
        decorators = getattr(node, "decorator_list", None)
        if decorators:
            start = min(start, *(d.lineno for d in decorators))
        end = getattr(node, "end_lineno", start) or start
        return stable_hash(normalized_source(self.lines[start - 1:end]))

    def body_fingerprint(self, node: Any) -> str:
        """Like :meth:`fingerprint` with the definition's own name blanked, so a renamed but otherwise
        identical function or class keeps it."""
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start) or start
        text = normalized_source(self.lines[start - 1:end])
        return stable_hash(re.sub(r"\b(def|class)\s+" + re.escape(node.name) + r"\b", r"\1 _", text, count=1))

    # Imports ---------------------------------------------------------------

    def imports(self, tree: ast.Module) -> None:
        def visit(nodes: list[ast.stmt] | list[ast.AST], tc: bool, cond: bool, lazy: bool) -> None:
            for node in nodes:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.info.imports.append(RawImport("import", alias.name, 0, [(alias.name, alias.asname)],
                                                           node.lineno, node.end_lineno or node.lineno, tc, cond, lazy))
                elif isinstance(node, ast.ImportFrom):
                    self.info.imports.append(RawImport("from", node.module, node.level or 0,
                                                       [(a.name, a.asname) for a in node.names], node.lineno,
                                                       node.end_lineno or node.lineno, tc, cond, lazy))
                elif isinstance(node, ast.If):
                    if _is_type_checking(node.test):
                        visit(node.body, True, cond, lazy)
                        visit(node.orelse, tc, True, lazy)
                    elif _is_main_guard(node.test):
                        visit(node.body, tc, cond, lazy)
                        visit(node.orelse, tc, True, lazy)
                    else:
                        visit(node.body, tc, True, lazy)
                        visit(node.orelse, tc, True, lazy)
                    self._dynamic(node.test, tc, cond, lazy)
                elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                    guarded = any(_catches_import_error(h) for h in node.handlers)
                    visit(node.body, tc, cond or guarded, lazy)
                    for h in node.handlers:
                        visit(h.body, tc, True, lazy)
                    visit(node.orelse, tc, cond or guarded, lazy)
                    visit(node.finalbody, tc, cond, lazy)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in node.decorator_list:
                        self._dynamic(d, tc, cond, lazy)
                    visit(node.body, tc, cond, True)
                elif isinstance(node, ast.ClassDef):
                    visit(node.body, tc, cond, lazy)
                elif isinstance(node, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
                    visit(node.body, tc, cond, lazy)
                    visit(getattr(node, "orelse", []), tc, cond, lazy)
                    for item in getattr(node, "items", []):
                        self._dynamic(item.context_expr, tc, cond, lazy)
                elif isinstance(node, getattr(ast, "Match", ())):
                    for case in node.cases:  # type: ignore[attr-defined]
                        visit(case.body, tc, True, lazy)
                else:
                    self._dynamic(node, tc, cond, lazy)

        visit(tree.body, False, False, False)

    def _dynamic(self, node: ast.AST, tc: bool, cond: bool, lazy: bool) -> None:
        if not self.scan_dynamic:
            return
        for sub in ast.walk(node):
            if isinstance(sub, ast.Lambda):
                continue
            if not isinstance(sub, ast.Call) or not sub.args:
                continue
            fn = _dotted(sub.func)
            if not fn or fn[-1] not in ("import_module", "__import__"):
                continue
            if fn[-1] == "import_module" and len(fn) > 1 and fn[-2] != "importlib":
                continue
            arg = sub.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value:
                name = arg.value
                level = len(name) - len(name.lstrip("."))
                self.info.imports.append(RawImport("dynamic", name.lstrip(".") or None, level, [(name, None)],
                                                   sub.lineno, sub.end_lineno or sub.lineno, tc, cond, lazy))

    # Symbols and calls -------------------------------------------------------------

    def symbols(self, tree: ast.Module) -> None:
        self.info.top_level_names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.info.top_level_names.append(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                        self.info.all_names = [e.value for e in node.value.elts
                                               if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        seen: dict[str, int] = {}

        def unique(q: str) -> str:
            seen[q] = seen.get(q, 0) + 1
            return q if seen[q] == 1 else f"{q}#{seen[q]}"

        def walk(body: list[ast.stmt], prefix: str, parent: str | None, class_qual: str | None,
                 caller: str, locals_: set[str], in_class_body: bool) -> None:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qual = unique(f"{prefix}{node.name}")
                    kind = "method" if in_class_body else "function"
                    self._decorator_calls(node, caller, class_qual, locals_)
                    self.info.symbols.append(RawSymbol(
                        qual, node.name, kind, node.lineno, node.end_lineno or node.lineno,
                        self.fingerprint(node), parent,
                        decorators=[".".join(d) for d in (_dotted(x.func if isinstance(x, ast.Call) else x)
                                                          for x in node.decorator_list) if d],
                        signature=_signature(node), is_async=isinstance(node, ast.AsyncFunctionDef),
                        doc=(ast.get_docstring(node) or "").strip().split("\n")[0][:160],
                        body_fingerprint=self.body_fingerprint(node)))
                    walk(node.body, f"{qual}.", qual, class_qual, qual, _local_names(node), False)
                    self._defaults_calls(node, caller, class_qual, locals_)
                elif isinstance(node, ast.ClassDef):
                    qual = unique(f"{prefix}{node.name}")
                    self._decorator_calls(node, caller, class_qual, locals_)
                    self.info.symbols.append(RawSymbol(
                        qual, node.name, "class", node.lineno, node.end_lineno or node.lineno,
                        self.fingerprint(node), parent,
                        decorators=[".".join(d) for d in (_dotted(x.func if isinstance(x, ast.Call) else x)
                                                          for x in node.decorator_list) if d],
                        bases=[d for d in (_dotted(b) for b in node.bases) if d],
                        doc=(ast.get_docstring(node) or "").strip().split("\n")[0][:160],
                        body_fingerprint=self.body_fingerprint(node)))
                    walk(node.body, f"{qual}.", qual, qual, qual, set(), True)
                elif isinstance(node, ast.If) and not prefix and _is_main_guard(node.test):
                    qual = unique("__main__")
                    self.info.symbols.append(RawSymbol(
                        qual, "__main__ block", "main-block", node.lineno, node.end_lineno or node.lineno,
                        self.fingerprint(node), None))
                    walk(node.body, "", None, None, qual, set(), False)
                else:
                    self._calls_in(node, caller, class_qual, locals_, body_walker=lambda b: walk(
                        b, prefix, parent, class_qual, caller, locals_, in_class_body))

        walk(tree.body, "", None, None, "", set(), False)

    def _decorator_calls(self, node: Any, caller: str, class_qual: str | None, locals_: set[str]) -> None:
        for d in node.decorator_list:
            self._calls_in(d, caller, class_qual, locals_)

    def _defaults_calls(self, node: Any, caller: str, class_qual: str | None, locals_: set[str]) -> None:
        for d in [*node.args.defaults, *node.args.kw_defaults]:
            if d is not None:
                self._calls_in(d, caller, class_qual, locals_)

    def _calls_in(self, node: ast.AST, caller: str, class_qual: str | None, locals_: set[str],
                  body_walker: Any = None) -> None:
        # Compound statements may contain nested definitions: hand their bodies back to the walker.
        if body_walker is not None and isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With,
                                                         ast.AsyncWith, ast.Try, getattr(ast, "TryStar", ast.Try))):
            for fname in ("test", "iter", "target"):
                sub = getattr(node, fname, None)
                if sub is not None:
                    self._calls_in(sub, caller, class_qual, locals_)
            for item in getattr(node, "items", []):
                self._calls_in(item.context_expr, caller, class_qual, locals_)
            for fname in ("body", "orelse", "finalbody"):
                body_walker(getattr(node, fname, []) or [])
            for h in getattr(node, "handlers", []):
                body_walker(h.body)
            return
        stack = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(cur, ast.Call):
                parts = _dotted(cur.func)
                if parts is None:
                    self.info.unresolvable_calls += 1
                elif parts[0] in locals_ and parts[0] not in ("self", "cls"):
                    self.info.unresolvable_calls += 1
                else:
                    self.info.calls.append(RawCallSite(caller, class_qual, parts, cur.lineno))
            stack.extend(ast.iter_child_nodes(cur))


def _nearest_package_ancestor(directory: str, packages: set[str]) -> str | None:
    """The closest proper ancestor of ``directory`` that is a regular package, if any."""
    d = posixpath.dirname(directory)
    while d:
        if d in packages:
            return d
        d = posixpath.dirname(d)
    return None


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(fn.args)
    except Exception:  # pragma: no cover - defensive
        args = "..."
    ret = ""
    if fn.returns is not None:
        try:
            ret = " -> " + ast.unparse(fn.returns)
        except Exception:  # pragma: no cover
            ret = ""
    return f"({args}){ret}"


MAX_SIGNATURE_DISPLAY = 200


def parse_python(text: str, path: str = "<file>") -> PyFileInfo:
    info = PyFileInfo(loc=text.count("\n") + (0 if text.endswith("\n") or not text else 1))
    info.generated = classify.has_generated_marker(text)
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        info.error = f"{type(exc).__name__}: {getattr(exc, 'msg', exc)}"
        info.error_line = getattr(exc, "lineno", None)
        return info
    lines = text.splitlines()
    info.semantic_fingerprint = stable_hash(normalized_source(lines), length=32)
    ex = _Extractor(info, lines, dynamic="import_module" in text or "__import__" in text)
    try:
        ex.imports(tree)
        ex.symbols(tree)
    except RecursionError:  # pragma: no cover - pathological nesting
        info.error = "RecursionError: file is too deeply nested to analyze"
    return info


PARALLEL_THRESHOLD = 300


def _parse_item(item: tuple[str, str]) -> tuple[str, PyFileInfo]:
    path, text = item
    return path, parse_python(text, path)


def parse_many(texts: dict[str, str]) -> dict[str, PyFileInfo]:
    """Parse files, using worker processes for large batches (pure function, so safe to parallelize)."""
    items = list(texts.items())
    if len(items) >= PARALLEL_THRESHOLD and os.environ.get("REPOVIZ_NO_PARALLEL") != "1":
        try:
            import concurrent.futures as cf
            import multiprocessing

            workers = min(8, os.cpu_count() or 1)
            if workers > 1:
                method = "fork" if "fork" in multiprocessing.get_all_start_methods() and sys.platform != "darwin" else "spawn"
                with cf.ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context(method)) as pool:
                    return dict(pool.map(_parse_item, items, chunksize=32))
        except Exception:  # fall back to serial parsing (restricted environments, pickling issues...)
            pass
    return dict(_parse_item(item) for item in items)


# --------------------------------------------------------------------------
# Analyzer
# --------------------------------------------------------------------------


@dataclass
class _Module:
    path: str
    qualname: str
    is_package: bool
    root: str
    info: PyFileInfo
    node_id: str = ""


_GRIMP_LOCK = threading.Lock()


class PythonAnalyzer(Analyzer):
    name = "python"
    version = "3"
    languages = ("python",)
    capabilities = (CAP_MODULES, CAP_CONTAINMENT, CAP_SYMBOLS, CAP_ENTRY_POINTS, CAP_DEPENDENCIES, CAP_CALLS,
                    CAP_EVIDENCE, CAP_DIAGNOSTICS)

    def detect(self, ctx: AnalysisContext) -> Detection:
        n = len(self._files(ctx))
        if not n:
            return Detection(False, "no Python files")
        return Detection(True, f"{n} Python file(s)")

    @staticmethod
    def _files(ctx: AnalysisContext) -> list[str]:
        files = [f for f in ctx.files("python") if f.endswith((".py", ".pyi", ".pyw"))]
        stems = {f[:-3] for f in files if f.endswith(".py")}
        return [f for f in files if not (f.endswith(".pyi") and f[:-4] in stems)]

    # -- module naming ---------------------------------------------------------------

    def _module_names(self, ctx: AnalysisContext, files: list[str]) -> dict[str, tuple[str, str, bool]]:
        """Map path -> (qualified name, source root, is_package)."""
        declared = sorted((r["path"] for r in ctx.profile.source_roots
                           if r.get("language") in (None, "python") and r.get("origin", "").split(":")[0] in
                           ("configured", "manifest")), key=lambda p: -len(p))
        dirs_with_init = {posixpath.dirname(f) for f in files if posixpath.basename(f) in ("__init__.py", "__init__.pyi")}
        out: dict[str, tuple[str, str, bool]] = {}
        for f in files:
            directory = posixpath.dirname(f)
            root = None
            for r in declared:
                if r == "" or f.startswith(r + "/"):
                    root = r
                    break
            if root is None:
                # Walk up while the directory is a regular package; the first directory
                # without ``__init__.py`` is the import root (flat, src and script layouts).
                # A directory without ``__init__.py`` *inside* a regular package is an implicit
                # namespace subpackage (``app/config/settings.py`` imports as ``app.config.settings``).
                d = directory
                while d:
                    if d in dirs_with_init:
                        d = posixpath.dirname(d)
                        continue
                    ancestor = _nearest_package_ancestor(d, dirs_with_init)
                    if ancestor is None:
                        break
                    d = ancestor
                root = d
            rel = f[len(root) + 1:] if root else f
            stem = rel.rsplit(".", 1)[0]
            parts = stem.split("/")
            is_package = parts[-1] == "__init__"
            if is_package:
                parts = parts[:-1]
            qual = ".".join(parts) if parts else (posixpath.basename(root) or ctx.profile.name)
            out[f] = (qual, root, is_package)
        return out

    # -- phases ---------------------------------------------------------------------------

    def discover_modules(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        files = self._files(ctx)
        names = self._module_names(ctx, files)
        modules: dict[str, _Module] = {}
        by_qual: dict[str, list[str]] = {}
        texts: dict[str, tuple[str, str]] = {}
        for f in files:
            text = ctx.text(f)
            if text is None:
                b.diagnostic("info", "file-skipped", "Python file skipped (binary, unreadable or larger than "
                             f"max_file_bytes={ctx.config.max_file_bytes}).", self.name, f)
                continue
            texts[f] = (ctx.source.content_hash(f) or stable_hash(text), text)
        misses = {f: t for f, (d, t) in texts.items() if ("python", self.version, d) not in ctx.file_cache}
        for f, info in parse_many(misses).items():
            ctx.file_cache[("python", self.version, texts[f][0])] = info
        for f, (digest, text) in texts.items():
            info = ctx.cached(("python", self.version, digest), lambda t=text, p=f: parse_python(t, p))
            qual, root, is_pkg = names[f]
            modules[f] = _Module(f, qual, is_pkg, root, info)
            by_qual.setdefault(qual, []).append(f)
        ctx.shared["python.modules"] = modules
        ctx.shared["python.by_qual"] = by_qual

        for f, mod in modules.items():
            info = mod.info
            tags = ["python"]
            meta: dict[str, Any] = {"loc": info.loc, "is_package": mod.is_package, "source_root": mod.root,
                                    "qualified_name_authoritative": True}
            if info.semantic_fingerprint:
                meta["semantic_fingerprint"] = info.semantic_fingerprint
            if info.generated:
                tags.append("generated")
            if f.endswith(".pyi"):
                meta["stub"] = True
            if ctx.profile.is_test(f):
                tags.append("test")
            if info.all_names is not None:
                meta["exports"] = info.all_names[:200]
            if len(by_qual[mod.qualname]) > 1:
                meta["ambiguous_qualified_name"] = True
            node = b.ensure_file(f, self.name)
            b.add_node(ComponentNode(
                id=node.id, name=posixpath.basename(f), qualified_name=mod.qualname, component_type="module",
                category=CATEGORY_MODULE, language="python", path=f, analyzer=self.name, key=node.key,
                tags=tags, metadata=meta))
            node.category = CATEGORY_MODULE
            mod.node_id = node.id
            if info.error:
                b.diagnostic("warning", "parse-error", f"Cannot parse Python file ({info.error}); it is shown "
                             "without dependencies or symbols.", self.name, f, info.error_line)
        dup = {q: ps for q, ps in by_qual.items() if len(ps) > 1}
        if dup:
            sample = ", ".join(f"{q} ({len(ps)} files)" for q, ps in list(dup.items())[:5])
            b.diagnostic("info", "ambiguous-module-names", f"{len(dup)} Python module name(s) map to several files "
                         f"(e.g. {sample}); imports of these names are resolved to the file in the importer's source "
                         "root when possible. Configure 'source_roots' to disambiguate.", self.name)
        b.stat(self.name, "modules", len(modules))

    def discover_containment(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        modules: dict[str, _Module] = ctx.shared.get("python.modules", {})
        package_dirs: dict[str, tuple[str, bool]] = {}
        for f, mod in modules.items():
            if mod.is_package:
                package_dirs[posixpath.dirname(f)] = (mod.qualname, True)
        # Namespace packages: directories between a source root and a module.
        for f, mod in modules.items():
            d = posixpath.dirname(f)
            parts = mod.qualname.split(".") if mod.is_package else mod.qualname.split(".")[:-1]
            cur = d
            while cur and cur != mod.root and parts and (mod.root == "" or cur.startswith(mod.root + "/")):
                package_dirs.setdefault(cur, (".".join(parts), False))
                cur = posixpath.dirname(cur)
                parts = parts[:-1]
        for d, (qual, regular) in package_dirs.items():
            ident = b.dir_id(d)
            if ident not in b.nodes:
                b.ensure_dir(d, self.name)
            parent_path = posixpath.dirname(d)
            is_top = parent_path not in package_dirs
            tags = ["python"] + (["component"] if is_top else [])
            b.add_node(ComponentNode(
                id=ident, name=posixpath.basename(d), qualified_name=qual,
                component_type="package" if regular else "namespace-package", language="python", path=d,
                analyzer=self.name, key=f"path:dir:{d}", tags=tags,
                metadata={"qualified_name_authoritative": True,
                          **({"component_role": "top-level Python package"} if is_top else {})}))

    def discover_symbols(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        modules: dict[str, _Module] = ctx.shared.get("python.modules", {})
        by_qual: dict[str, list[str]] = ctx.shared.get("python.by_qual", {})
        index = get_index(ctx)
        n_symbols = 0
        for f, mod in modules.items():
            info = mod.info
            scope = ModuleScope(key=f"py:{mod.qualname}", node_id=mod.node_id, language="python", path=f)
            if len(by_qual.get(mod.qualname, [])) > 1:
                scope.key = f"py:{mod.qualname}@{f}"
            is_test_file = ctx.profile.is_test(f)
            for sym in info.symbols:
                sid = b.symbol_id(f, sym.qualname)
                parent_id = b.symbol_id(f, sym.parent) if sym.parent else mod.node_id
                tags: list[str] = []
                meta: dict[str, Any] = {"kind": sym.kind}
                if sym.decorators:
                    meta["decorators"] = sym.decorators
                if sym.signature:
                    sig = sym.signature
                    meta["signature"] = sig if len(sig) <= MAX_SIGNATURE_DISPLAY else sig[:MAX_SIGNATURE_DISPLAY - 3] + "..."
                    # The displayed text may be truncated; changes are detected on the full signature.
                    meta["signature_id"] = stable_hash("sig", sig, length=12)
                if sym.doc:
                    meta["doc"] = sym.doc
                if sym.body_fingerprint:
                    meta["body_fingerprint"] = sym.body_fingerprint
                if sym.is_async:
                    meta["async"] = True
                if sym.kind == "main-block":
                    tags.append("entry-point")
                    meta["entry_kind"] = "__main__ guard"
                if any(d.split(".")[-1] in HANDLER_DECORATORS for d in sym.decorators) and sym.kind != "class":
                    tags.append("entry-point")
                    meta["entry_kind"] = "decorated handler (@" + sym.decorators[0] + ")"
                if is_test_file and ((sym.kind in ("function", "method") and sym.name.startswith("test"))
                                     or (sym.kind == "class" and sym.name.startswith("Test"))):
                    tags.append("test")
                    if sym.kind != "class":
                        tags.append("entry-point")
                        meta["entry_kind"] = "test"
                public = not sym.name.startswith("_") or sym.name.startswith("__") and sym.name.endswith("__")
                meta["public"] = public
                display = sym.qualname.split("#")[0]
                b.add_node(ComponentNode(
                    id=sid, name=sym.name, qualified_name=f"{mod.qualname}.{display}" if sym.kind != "main-block"
                    else f"{mod.qualname}:__main__", component_type=sym.kind, category=CATEGORY_SYMBOL,
                    language="python", path=f, parent_id=parent_id, analyzer=self.name,
                    key=f"symbol:{f}:{sym.qualname}", fingerprint=sym.fingerprint, start_line=sym.line,
                    end_line=sym.end_line, tags=tags, metadata=meta))
                scope.symbols[sym.qualname] = sid
                scope.kinds[sym.qualname] = "class" if sym.kind == "class" else sym.kind
                if sym.kind == "class" and sym.bases:
                    scope.class_bases[sym.qualname] = list(sym.bases)
                n_symbols += 1
            index.add_module(scope)
        b.stat(self.name, "symbols", n_symbols)

        # Bindings need every scope to exist first.
        for f, mod in modules.items():
            scope = index.modules[f"py:{mod.qualname}@{f}" if len(by_qual.get(mod.qualname, [])) > 1
                                  else f"py:{mod.qualname}"]
            package = mod.qualname if mod.is_package else mod.qualname.rpartition(".")[0]
            for imp in sorted(mod.info.imports, key=lambda i: i.lazy):
                if imp.kind == "dynamic":
                    continue
                base = self._absolute(imp, package, mod)
                if base is None:
                    continue
                bind = scope.bindings.setdefault if imp.lazy else scope.bindings.__setitem__
                if imp.kind == "import":
                    for name, asname in imp.names:
                        if asname:
                            bind(asname, self._binding(ctx, name))
                        else:
                            top = name.split(".")[0]
                            bind(top, (f"py:{top}", ()))
                else:
                    for name, asname in imp.names:
                        if name == "*":
                            scope.star_imports.append(f"py:{base}")
                            continue
                        bind(asname or name, self._binding(ctx, f"{base}.{name}" if base else name))
            if mod.is_package:
                prefix = mod.qualname + "."
                for q in by_qual:
                    if q.startswith(prefix) and "." not in q[len(prefix):]:
                        scope.submodules[q[len(prefix):]] = f"py:{q}"
                if f"py:{mod.qualname}.__main__" in index.modules or f"{mod.qualname}.__main__" in by_qual:
                    scope.main_module = f"py:{mod.qualname}.__main__"
            for call in mod.info.calls:
                head = call.parts[0]
                if head in PY_BUILTINS and head not in scope.bindings and head not in scope.symbols:
                    b.stat(self.name, "builtin_calls")
                    continue
                caller_id = b.symbol_id(f, call.caller) if call.caller else mod.node_id
                index.calls.append(RawCall(caller_id, scope.key, call.class_qual, call.parts, f, call.line,
                                           ctx.excerpt(f, call.line), caller_qual=call.caller or None))

    def _binding(self, ctx: AnalysisContext, dotted: str) -> tuple[str, tuple[str, ...]]:
        """Split ``a.b.c`` into the longest known module prefix and remaining attributes."""
        by_qual = ctx.shared.get("python.by_qual", {})
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in by_qual:
                return f"py:{prefix}", tuple(parts[i:])
        return f"py:{parts[0]}", tuple(parts[1:])

    @staticmethod
    def _absolute(imp: RawImport, package: str, mod: _Module) -> str | None:
        if imp.level == 0:
            return imp.module or ""
        pkg_parts = package.split(".") if package else []
        up = imp.level - 1
        if up > len(pkg_parts):
            return None
        base_parts = pkg_parts[: len(pkg_parts) - up] if up else pkg_parts
        if imp.module:
            base_parts = base_parts + imp.module.split(".")
        return ".".join(base_parts)

    def discover_entry_points(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        modules: dict[str, _Module] = ctx.shared.get("python.modules", {})
        for f, mod in modules.items():
            if posixpath.basename(f) == "__main__.py":
                node = b.get(mod.node_id)
                if node is not None:
                    if "entry-point" not in node.tags:
                        node.tags.append("entry-point")
                    node.metadata["entry_kind"] = "python -m package"

    def discover_dependencies(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        modules: dict[str, _Module] = ctx.shared.get("python.modules", {})
        by_qual: dict[str, list[str]] = ctx.shared.get("python.by_qual", {})
        declared = self._declared_distributions(ctx)
        internal_tops = {q.split(".")[0] for q in by_qual}
        undeclared: dict[str, list[SourceEvidence]] = {}
        n_edges = 0

        def pick(qual: str, importer: _Module) -> str | None:
            paths = by_qual.get(qual)
            if not paths:
                return None
            if len(paths) == 1:
                return paths[0]
            same_root = [p for p in paths if modules[p].root == importer.root]
            return (same_root or paths)[0]

        def longest(dotted: str, importer: _Module) -> tuple[str | None, bool]:
            parts = dotted.split(".")
            for i in range(len(parts), 0, -1):
                target = pick(".".join(parts[:i]), importer)
                if target:
                    return target, i == len(parts)
            return None, False

        for f, mod in modules.items():
            package = mod.qualname if mod.is_package else mod.qualname.rpartition(".")[0]
            for imp in mod.info.imports:
                base = self._absolute(imp, package, mod)
                if base is None:
                    b.diagnostic("warning", "unresolved-relative-import",
                                 f"Relative import (level {imp.level}) goes beyond the top-level package "
                                 f"'{mod.qualname.split('.')[0]}'.", self.name, f, imp.line)
                    continue
                construct = {"import": "import", "from": "from-import", "dynamic": "dynamic-import"}[imp.kind]
                if imp.level:
                    construct = "relative-" + construct
                targets: list[tuple[str, str]] = []  # (dotted target, imported name)
                if imp.kind == "from":
                    for name, _as in imp.names:
                        if name == "*":
                            targets.append((base, "*"))
                        else:
                            sub = f"{base}.{name}" if base else name
                            targets.append((sub if pick(sub, mod) else base, name))
                else:
                    targets = [(base, base)]
                for dotted, imported in targets:
                    if not dotted:
                        continue
                    ev = self.evidence(ctx, f, imp.line, imp.end_line, construct)
                    meta: dict[str, Any] = {"imported_names": [imported]}
                    if imp.type_checking:
                        meta["type_checking_only"] = True
                    if imp.conditional:
                        meta["conditional_only"] = True
                    if imp.lazy:
                        meta["lazy_only"] = True
                    if imp.kind == "dynamic":
                        meta["dynamic_only"] = True
                    if ctx.profile.is_test(f):
                        meta["test_only"] = True
                    target_path, exact = longest(dotted, mod)
                    top = dotted.split(".")[0]
                    if target_path is not None:
                        target_id = modules[target_path].node_id
                        if target_id == mod.node_id:
                            continue
                        confidence = 1.0 if exact else 0.6
                        if imp.kind == "dynamic":
                            confidence = min(confidence, 0.7)
                        if not exact:
                            # Only a prefix exists (``import pkg.missing``): the import would fail at runtime.
                            meta["unresolved_submodule"] = dotted
                            if not imp.conditional:
                                b.diagnostic("warning", "unresolved-internal-import",
                                             f"'{dotted}' does not exist in the repository (closest: "
                                             f"'{modules[target_path].qualname}').", self.name, f, imp.line)
                        b.add_edge(mod.node_id, target_id, REL_IMPORTS, analyzer=self.name, evidence=[ev],
                                   confidence=confidence, metadata=meta)
                        n_edges += 1
                    elif top in internal_tops:
                        b.diagnostic("info", "unresolved-internal-import",
                                     f"'{dotted}' looks internal but no matching module was found.", self.name, f,
                                     imp.line)
                    else:
                        ext_id = self._external(ctx, b, top, declared)
                        meta["external"] = True
                        b.add_edge(mod.node_id, ext_id, REL_IMPORTS, analyzer=self.name, evidence=[ev],
                                   confidence=0.9 if imp.kind != "dynamic" else 0.6, metadata=meta)
                        ext = b.nodes[ext_id]
                        if declared and "stdlib" not in ext.tags and not ext.metadata.get("declared") \
                                and not imp.conditional and not imp.type_checking and not ctx.profile.is_test(f):
                            undeclared.setdefault(ext.name, []).append(ev)
        for name, evs in sorted(undeclared.items()):
            b.diagnostic("info", "undeclared-dependency",
                         f"'{name}' is imported ({len(evs)}x) but not declared in any Python manifest.", self.name,
                         evs[0].path, evs[0].start_line)
        b.stat(self.name, "import_edges", n_edges)
        self._grimp_cross_check(ctx, b, modules)

    def _declared_distributions(self, ctx: AnalysisContext) -> set[str]:
        names: set[str] = set()
        for md in ctx.profile.manifest_data.values():
            if md.ecosystem == "python" and not md.lockfile:
                names.update(normalize_python_name(d.name) for d in md.dependencies)
        return names

    def _external(self, ctx: AnalysisContext, b: SnapshotBuilder, top: str, declared: set[str]) -> str:
        is_std = top in STDLIB or top == "__future__"
        dist = IMPORT_TO_DIST.get(top.lower(), normalize_python_name(top))
        key_name = top if is_std else dist
        eco = "python-stdlib" if is_std else "python"
        ext_id = b.external_id(eco, key_name)
        tags = ["external"] + (["stdlib"] if is_std else [])
        meta: dict[str, Any] = {"ecosystem": "python", "import_names": [top]}
        if not is_std:
            meta["distribution"] = dist
            meta["declared"] = dist in declared or normalize_python_name(top) in declared
        # The manifest analyzer may already have created this node from a declared dependency: merge.
        b.add_node(ComponentNode(
            id=ext_id, name=top if is_std else dist, qualified_name=top, component_type="external-package",
            language="python", analyzer=self.name, key=f"external:{eco}:{key_name}", tags=tags, metadata=meta,
            parent_id=None))
        return ext_id

    # -- optional grimp cross-check ------------------------------------------------------

    def _grimp_cross_check(self, ctx: AnalysisContext, b: SnapshotBuilder, modules: dict[str, _Module]) -> None:
        mode = ctx.config.python_use_grimp
        if mode == "never" or ctx.source.kind not in ("worktree", "filesystem"):
            return
        try:
            import grimp  # type: ignore[import-not-found]
        except ImportError:
            if mode == "always":
                b.diagnostic("warning", "grimp-unavailable", "python.use_grimp = 'always' but grimp is not installed.",
                             self.name)
            return
        root_dir = Path(getattr(ctx.source, "root", "") or "")
        tops: dict[str, str] = {}
        for mod in modules.values():
            top = mod.qualname.split(".")[0]
            if mod.is_package and "." not in mod.qualname and top.isidentifier():
                tops[top] = mod.root
        if not tops:
            return
        confirmed = only_grimp = 0
        by_qual = {m.qualname: m for m in modules.values()}
        ast_edges = {(e.source_id, e.target_id): e for e in b.edges.values() if e.relationship == REL_IMPORTS}
        with _GRIMP_LOCK:
            for top, root in sorted(tops.items()):
                abs_root = str((root_dir / root).resolve()) if root else str(root_dir.resolve())
                saved_path = list(sys.path)
                loaded = sys.modules.get(top)
                if loaded is not None:
                    origin = getattr(getattr(loaded, "__spec__", None), "origin", "") or ""
                    if not origin.startswith(abs_root):
                        b.diagnostic("info", "grimp-skipped", f"grimp cross-check skipped for '{top}': a different "
                                     "module with that name is already imported by this process.", self.name)
                        continue
                try:
                    sys.path.insert(0, abs_root)
                    importlib.invalidate_caches()
                    spec = importlib.util.find_spec(top)
                    origin = (spec.origin or "") if spec else ""
                    locations = list(spec.submodule_search_locations or []) if spec else []
                    if not (origin.startswith(abs_root) or any(str(l).startswith(abs_root) for l in locations)):
                        b.diagnostic("info", "grimp-skipped", f"grimp cross-check skipped for '{top}': the package "
                                     "resolves outside the repository.", self.name)
                        continue
                    graph = grimp.build_graph(top, cache_dir=None)
                except Exception as exc:  # grimp failures must never break the analysis
                    b.diagnostic("info", "grimp-failed", f"grimp could not build a graph for '{top}': {exc}",
                                 self.name)
                    continue
                finally:
                    sys.path[:] = saved_path
                for importer in graph.modules:
                    src = by_qual.get(importer)
                    if src is None:
                        continue
                    for imported in graph.find_modules_directly_imported_by(importer):
                        dst = by_qual.get(imported)
                        if dst is None or dst.node_id == src.node_id:
                            continue
                        edge = ast_edges.get((src.node_id, dst.node_id))
                        if edge is not None:
                            confirmed += 1
                            edge.metadata["confirmed_by"] = "grimp"
                            continue
                        only_grimp += 1
                        if mode == "always":
                            details = graph.get_import_details(importer=importer, imported=imported)
                            evs = [SourceEvidence(src.path, d.get("line_number"), d.get("line_number"), "import",
                                                  "grimp", d.get("line_contents")) for d in details]
                            b.add_edge(src.node_id, dst.node_id, REL_IMPORTS, analyzer="grimp", evidence=evs,
                                       confidence=0.9, metadata={"detected_by": "grimp"})
        b.metadata.setdefault("python", {})["grimp"] = {"confirmed_edges": confirmed, "only_in_grimp": only_grimp}
        b.diagnostic("info", "grimp-cross-check",
                     f"grimp cross-check: {confirmed} import edge(s) confirmed, {only_grimp} found only by grimp"
                     + (" (added)." if mode == "always" and only_grimp else "."), self.name)

