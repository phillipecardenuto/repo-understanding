"""Is new code wired in?  Finds modules and functions added in a change that nothing uses.

A common mistake of coding agents is to write the new piece and forget to connect it: a
router that is never included in the application, a module nothing imports, a helper
nobody calls.  :func:`unwired_code` looks only at code *added* between two snapshots, so
the rest of the repository causes no noise, and reports three kinds of problems:

* ``unwired-module``: a new module that no other module imports, that is not an entry
  point and that nothing refers to by name or path (configuration, Dockerfiles, HTML...).
  A router module (``APIRouter()``, ``Blueprint()``, ``express.Router()``) also counts
  when its importers never use it;
* ``unwired-symbol``: a new top-level function or class whose name appears nowhere
  outside its definition, neither in its module nor in the modules that import it;
* ``unreachable-from-entry``: a new module that is imported, but only by tests or by
  other new modules that are themselves unused.

Everything is static: import edges from the snapshot plus a plain-text search, never
executing anything.  Files that frameworks load by convention (Django apps, migrations,
pytest ``conftest.py``, Next.js pages...) count as wired; see :data:`WIRED_BY_CONVENTION`.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from . import classify, globs
from .activity import nodes_by_path
from .model import ADDED, CATEGORY_MODULE, CATEGORY_SYMBOL, REL_IMPORTS, REL_INVOKES, RepositoryDiff, RepositorySnapshot

#: Languages whose imports point at files, so "nothing imports this file" is meaningful.
#: (Go imports whole packages, so a new file in an existing package has no importer of its own.)
LANGUAGES = ("python", "javascript", "typescript")

#: Files that frameworks and tools load by convention, without an import.
WIRED_BY_CONVENTION: dict[str, tuple[str, ...]] = {
    "python": (
        "__init__.py", "__main__.py", "conftest.py", "setup.py", "manage.py", "noxfile.py", "fabfile.py",
        "tasks.py", "wsgi.py", "asgi.py", "settings.py", "settings/", "urls.py", "admin.py", "apps.py",
        "models.py", "migrations/", "**/management/commands/**", "templatetags/", "**/alembic/versions/**",
        "conf.py", "gunicorn.conf.py", "dags/", "scripts/", "bin/", "examples/", "benchmarks/", "docs/",
    ),
    "js": (
        "*.config.*", "*.d.ts", "*.stories.*", "setupTests.*", "__mocks__/", "middleware.*",
        "service-worker.*", "sw.*", "public/", "scripts/", "bin/", "examples/",
        "**/pages/**", "**/app/**/page.*", "**/app/**/layout.*", "**/app/**/route.*", "**/app/**/loading.*",
        "**/app/**/error.*", "**/app/**/not-found.*", "**/app/**/template.*", "**/app/**/default.*",
        "**/app/routes/**", "**/server/api/**", "**/server/routes/**",
        "+page*", "+layout*", "+server*", "+error*",
    ),
}

#: Names that configuration or frameworks refer to, rather than code.
_CONVENTIONAL_NAMES = {"main", "app", "application", "create_app", "handler", "lambda_handler", "setup", "teardown",
                       "setUp", "tearDown", "setUpModule", "tearDownModule", "default"}

#: Files where a mention of a module's dotted name or path wires it in (code, configuration, build files).
_REFERENCE_EXTENSIONS = (".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte",
                         ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".html", ".htm", ".sh", ".mk", ".conf")
_REFERENCE_NAMES = ("dockerfile", "makefile", "procfile", "justfile", "containerfile")
MAX_REFERENCE_FILE_CHARS = 512_000
MAX_REFERENCE_CHARS = 64_000_000
MAX_CANDIDATES = 300

# Module-level router objects: they serve nothing until the application registers them.
_PY_ROUTER = re.compile(r"^(\w+)\s*(?::[^=\n]+)?=\s*(?:[\w.]+\.)?(APIRouter|Blueprint|Router)\s*\(", re.M)
_JS_ROUTER = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:\w+\.)?Router\s*\(", re.M)
_PY_REGISTER = re.compile(r"\b(include_router|register_blueprint)\s*\(")
_JS_REGISTER = re.compile(r"\.use\s*\(")
# Import statements, including parenthesized / braced ones spanning several lines.
_PY_IMPORT = re.compile(r"^[ \t]*(?:from[ \t]+([\w.]+)[ \t]+import[ \t]*(\([^)]*\)|[^\n]*)|import[ \t]+([^\n]*))", re.M)
_JS_IMPORT = re.compile(r"^[ \t]*(?:import\s+(?P<imp>[^;'\"]*?)\s*from\s*|import\s*|export\s+(?P<exp>[^;'\"]*?)\s*from\s*|"
                        r"(?:const|let|var)\s+(?P<req>[^=;]+?)\s*=\s*require\(\s*)['\"](?P<spec>[^'\"]+)['\"]\)?;?", re.M)
_JS_INLINE_LOAD = re.compile(r"\b(?:require|import)\(\s*['\"]([^'\"]+)['\"]\s*\)")
_IMPORT_LINE = re.compile(r"^\s*(from\s+\S+\s+import\b|import\b|export\b.*\bfrom\b|(const|let|var)\s+.*=\s*require\()")
_WORD = re.compile(r"[A-Za-z_$][\w$]*")
#: File names too common to be matched by name alone (``"main": "index.js"`` is not every index.js).
_GENERIC_NAMES = {"index", "main", "mod", "lib", "__init__", "__main__", "setup", "utils", "helpers", "config"}
#: Entry points that make a repository an application: services and containers.
_SERVICE_ENTRY_KINDS = ("compose-command", "procfile", "container-")


@dataclass
class Unwired:
    kind: str  # unwired-module | unwired-symbol | unreachable-from-entry
    path: str
    name: str
    line: int | None = None
    router: str | None = None  # the router variable, for router modules
    router_kind: str | None = None  # APIRouter | Blueprint | Router
    importers: list[str] = field(default_factory=list)
    register_in: str | None = None  # where routers are registered today (a suggestion)
    application: bool = True  # False in a library, where unused public modules can be API


def _family(language: str | None) -> str | None:
    if language == "python":
        return "python"
    if language in ("javascript", "typescript"):
        return "js"
    return None


def wired_by_convention(path: str, language: str | None, extra: list[str] | tuple[str, ...] = ()) -> bool:
    fam = _family(language)
    return bool(fam and globs.match_any(path, WIRED_BY_CONVENTION[fam])) or bool(extra and globs.match_any(path, extra))


def _is_test(node: Any) -> bool:
    return "test" in node.tags or classify.is_test_path(node.path or "")


def _skippable(node: Any) -> bool:
    return "generated" in node.tags or "vendored" in node.tags


def _is_init(node: Any) -> bool:
    return posixpath.basename(node.path or "") == "__init__.py"


def _reference_tokens(node: Any) -> list[str]:
    """Strings whose presence elsewhere means the module is referenced by name or path."""
    path = node.path
    stem = path.rsplit(".", 1)[0]
    if _is_init(node):  # a package is referenced by its directory or dotted name
        path = stem = posixpath.dirname(path)
    tokens = [path] if path else []
    if "/" in stem and stem != path:
        tokens.append(stem)
    qn = (node.qualified_name or "").removesuffix(".__init__")
    if node.language == "python" and "." in qn:
        tokens.append(qn)
    return [t for t in tokens if t]


def unwired_code(diff: RepositoryDiff, target: RepositorySnapshot, source: Any, *,
                 ignore: list[str] | tuple[str, ...] = (), is_test: Callable[[Any], bool] = _is_test) -> list[Unwired]:
    """New modules and top-level symbols of ``target`` (added in ``diff``) that nothing uses."""
    idx = target.node_index()
    added = {nid for nid, ch in diff.nodes.items() if ch.status == ADDED and nid in idx}
    new_modules = [idx[nid] for nid in sorted(added) if idx[nid].category == CATEGORY_MODULE and idx[nid].path
                   and idx[nid].language in LANGUAGES and not is_test(idx[nid]) and not _skippable(idx[nid])]
    new_symbols = [idx[nid] for nid in sorted(added) if idx[nid].category == CATEGORY_SYMBOL and idx[nid].path
                   and idx[nid].language in LANGUAGES]
    if not new_modules and not new_symbols:
        return []

    importers: dict[str, set[str]] = {}
    for e in target.dependency_edges:
        if e.relationship == REL_IMPORTS and e.source_id != e.target_id and e.source_id in idx:
            importers.setdefault(e.target_id, set()).add(e.source_id)
    invoked = {e.target_id for e in target.edges() if e.relationship == REL_INVOKES}
    invoked_paths = {idx[i].path for i in invoked if i in idx and idx[i].path}
    main_blocks = {s.parent_id for s in target.symbols
                   if str(s.metadata.get("entry_kind", "")).startswith("__main__")}
    module_of_path = {n.path: n for n in target.modules if n.path}
    entries = [n for n in target.nodes()
               if "entry-point" in n.tags and "test" not in n.tags and n.metadata.get("entry_kind") != "test"]
    has_entry_points = bool(entries)
    # An application serves requests or runs as a service; in a library, new public modules used only by tests
    # (or by nobody yet) are normal API.  Containers make the whole repository an application; route and task
    # handlers make their own component one (a library's examples/ folder does not make the library an app).
    repo_app = any(str(n.metadata.get("entry_kind", "")).startswith(_SERVICE_ENTRY_KINDS) for n in entries)
    app_components = {n.metadata.get("component_id")
                      or (module_of_path[n.path].metadata.get("component_id") if n.path in module_of_path else None)
                      for n in entries if str(n.metadata.get("entry_kind", "")).startswith("decorated handler")}
    app_components.discard(None)

    def application(module: Any) -> bool:
        return repo_app or module.metadata.get("component_id") in app_components

    def own_entry(module: Any) -> bool:
        """Runs by itself: ``__main__.py``, an ``if __name__ == "__main__"`` block, or a declared entry point.

        Decorated handlers (``@router.get``) do not count: the module must still be imported to register them.
        """
        return ("entry-point" in module.tags or module.id in invoked or module.path in invoked_paths
                or module.id in main_blocks)

    texts: dict[str, str] = {}

    def text(path: str) -> str:
        if path not in texts:
            texts[path] = source.read_text(path) or ""
        return texts[path]

    loaded_like_neighbours = _dynamic_families(target, added, importers, idx, is_test)
    candidates = [m for m in new_modules if not own_entry(m) and not wired_by_convention(m.path, m.language, ignore)
                  and not loaded_like_neighbours(m)]
    candidates = candidates[:MAX_CANDIDATES]
    new_inits = [m for m in new_modules if _is_init(m)]
    # Plain-text references by name or path (INSTALLED_APPS, Celery include lists, Dockerfile CMD, index.html...).
    referenced = _referenced_modules(candidates + new_inits, target, text, is_test)

    # A new package's __init__ counts as a user only if something outside the package uses the package.
    module_edges = [(idx[s].path, idx[d].path) for d, srcs in importers.items() for s in srcs
                    if d in idx and s in idx and idx[s].path and idx[d].path]

    used_from_outside: set[str] = set()  # packages (directories) imported from outside themselves
    for src_path, dst_path in module_edges:
        folder = posixpath.dirname(dst_path)
        while folder and not (src_path + "/").startswith(folder + "/"):
            used_from_outside.add(folder)
            folder = posixpath.dirname(folder)

    def package_used(init: Any) -> bool:
        return init.id in referenced or posixpath.dirname(init.path) in used_from_outside

    unused_inits = {m.id for m in new_inits if not package_used(m)}

    out: list[Unwired] = []
    unwired_ids: set[str] = set()
    register_in: dict[str | None, str | None] = {}  # per language family, only when a router needs it

    def registration_file(language: str | None) -> str | None:
        fam = _family(language)
        if fam not in register_in:
            register_in[fam] = _registration_file(target, text, language, importers, idx)
        return register_in[fam]

    for m in candidates:
        if m.id in referenced:
            continue
        router, router_kind = _router(text(m.path), m.language)
        users = {u for u in importers.get(m.id, set()) if u not in unused_inits}
        if not users:
            out.append(Unwired("unwired-module", m.path, m.qualified_name, router=router, router_kind=router_kind,
                               register_in=registration_file(m.language) if router else None,
                               application=application(m) or bool(router)))
            unwired_ids.add(m.id)
        elif router and not _router_wired(m, users, importers, idx, text):
            out.append(Unwired("unwired-module", m.path, m.qualified_name, router=router, router_kind=router_kind,
                               importers=sorted(idx[u].path for u in users if idx[u].path),
                               register_in=registration_file(m.language)))
            unwired_ids.add(m.id)

    # Reachability along imports, from pre-existing code, entry points and files loaded by convention.
    if has_entry_points:
        forward: dict[str, set[str]] = {}
        for dst, srcs in importers.items():
            for s in srcs:
                forward.setdefault(s, set()).add(dst)
        init_of = {posixpath.dirname(n.path): n.id for n in target.modules if n.path and _is_init(n)}
        roots = [n.id for n in target.modules if n.path and not is_test(n) and n.id not in unwired_ids and (
            n.id not in added or own_entry(n) or n.id in referenced
            or (wired_by_convention(n.path, n.language, ignore) and not _is_init(n)))]
        seen = set(roots)
        stack = list(roots)
        while stack:
            cur = idx.get(stack.pop())
            if cur is None:
                continue
            nxt = set(forward.get(cur.id, ()))
            if cur.path:  # importing pkg.mod runs pkg/__init__ first
                parent = init_of.get(posixpath.dirname(cur.path))
                if parent:
                    nxt.add(parent)
            for n in nxt:
                if n not in seen and n not in unwired_ids:
                    seen.add(n)
                    stack.append(n)
        for m in candidates:
            if m.id in seen or m.id in unwired_ids or m.id in referenced:
                continue
            users = sorted(idx[u].path or idx[u].qualified_name for u in importers.get(m.id, ()))
            if not application(m) and all(is_test(idx[u]) for u in importers.get(m.id, ()) if u in idx):
                continue  # a library module exercised by its tests: normal for new API
            out.append(Unwired("unreachable-from-entry", m.path, m.qualified_name, importers=users,
                               application=application(m)))

    # New top-level functions and classes nobody refers to.
    callers = {e.target_id for e in target.call_edges}
    checked = 0
    for s in new_symbols:
        module = module_of_path.get(s.path)
        if module is None or module.id in unwired_ids or s.parent_id != module.id or is_test(s) or is_test(module):
            continue
        if module.id in referenced:  # loaded by name (plugins, task lists): its functions are looked up dynamically
            continue
        if s.component_type not in ("function", "class") or s.metadata.get("decorators") or "entry-point" in s.tags:
            continue
        if not s.metadata.get("public", s.metadata.get("exported", True)) or s.name.startswith("_"):
            continue
        if s.name in _CONVENTIONAL_NAMES or s.name in (module.metadata.get("exports") or ()):
            continue
        if s.id in callers or s.id in invoked or wired_by_convention(s.path, s.language, ignore):
            continue
        if s.metadata.get("default_export") and importers.get(module.id):
            continue  # imported under whatever name the importer chose
        checked += 1
        if checked > MAX_CANDIDATES or _name_used(s, module, importers, idx, text):
            continue
        out.append(Unwired("unwired-symbol", s.path, s.qualified_name, s.start_line))
    return out


def _dynamic_families(target: RepositorySnapshot, added: set[str], importers: dict[str, set[str]],
                      idx: dict[str, Any], is_test: Callable[[Any], bool]) -> Callable[[Any], bool]:
    """Whether a new module follows a pattern its existing neighbours use to get loaded without imports.

    Plugins, locales and backends are often loaded by a computed name (``import_module(f"locale.{code}.formats")``).
    A new ``locale/xx/formats.py`` is wired like the existing ``locale/*/formats.py`` files if none of those is
    imported either; the same goes for a new file in a folder whose existing modules nothing imports.
    """
    def imported(n: Any) -> bool:
        return any(u in idx and not is_test(idx[u]) for u in importers.get(n.id, ()))

    by_dir: dict[str, list[Any]] = {}
    by_parallel: dict[tuple[str, str], list[Any]] = {}
    for n in target.modules:
        if not n.path or n.id in added or n.language not in LANGUAGES or is_test(n) or _is_init(n):
            continue
        folder = posixpath.dirname(n.path)
        by_dir.setdefault(folder, []).append(n)
        by_parallel.setdefault((posixpath.dirname(folder), posixpath.basename(n.path)), []).append(n)

    def check(m: Any) -> bool:
        folder = posixpath.dirname(m.path)
        parallel = by_parallel.get((posixpath.dirname(folder), posixpath.basename(m.path)), [])
        if parallel and folder and not any(imported(n) for n in parallel):
            return True
        same_dir = by_dir.get(folder, [])
        return len(same_dir) >= 2 and not any(imported(n) for n in same_dir)

    return check


def _referenced_modules(candidates: list[Any], target: RepositorySnapshot, text: Callable[[str], str],
                        is_test: Callable[[Any], bool]) -> set[str]:
    tokens = {c.id: _reference_tokens(c) for c in candidates}
    tokens = {k: v for k, v in tokens.items() if v}
    if not tokens:
        return set()
    # Files in the same directory may use the bare file name (<script src="app.js">, CMD python worker.py), and
    # files in a parent directory may load it by a quoted name (asset('app.js')) when no file of that name sits
    # next to them.  Generic entry names (index.js, main.py) are too ambiguous for that.
    local = {c.id: (posixpath.dirname(c.path), posixpath.basename(c.path)) for c in candidates
             if c.id in tokens and not _is_init(c)}
    known = set(nodes_by_path(target))
    own = {c.path: c.id for c in candidates}
    # Every token contains the file's stem (or the package's name) as a word: a cheap first filter.
    by_word: dict[str, list[str]] = {}
    for c in candidates:
        if c.id in tokens:
            name = posixpath.basename(posixpath.dirname(c.path)) if _is_init(c) else \
                posixpath.basename(c.path).rsplit(".", 1)[0]
            by_word.setdefault(name, []).append(c.id)
    found: set[str] = set()
    budget = MAX_REFERENCE_CHARS
    for path, node in sorted(nodes_by_path(target).items()):
        if len(found) == len(tokens) or budget <= 0:
            break
        base = posixpath.basename(path).lower()
        if not (base.endswith(_REFERENCE_EXTENSIONS) or base.startswith(_REFERENCE_NAMES)):
            continue
        if is_test(node) or _skippable(node):
            continue
        mk = classify.manifest_kind(path)
        if mk is not None and mk.lockfile:
            continue
        data = text(path)
        if not data or len(data) > MAX_REFERENCE_FILE_CHARS:
            continue
        budget -= len(data)
        folder = posixpath.dirname(path)
        words = set(_WORD.findall(data))
        for cid in (c for w in words & by_word.keys() for c in by_word[w]):
            toks = tokens[cid]
            if cid in found or own.get(path) == cid:
                continue
            if cid in local:
                cand_dir, base = local[cid]
                if cand_dir == folder:
                    toks = toks + [base]
                elif (not folder or cand_dir.startswith(folder + "/")) \
                        and base.split(".", 1)[0] not in _GENERIC_NAMES and posixpath.join(folder, base) not in known:
                    toks = toks + [f'"{base}"', f"'{base}'", f'/{base}"', f"/{base}'"]
            if any(t in data for t in toks) and _reference_outside_imports(data, toks):
                found.add(cid)
    return found


def _reference_outside_imports(data: str, tokens: list[str]) -> bool:
    """A mention counts unless every mention is an import statement (imports are graph edges already)."""
    for t in tokens:
        pos = data.find(t)
        while pos != -1:
            start = data.rfind("\n", 0, pos) + 1
            end = data.find("\n", pos)
            if not _IMPORT_LINE.match(data[start:end if end != -1 else len(data)]):
                return True
            pos = data.find(t, end) if end != -1 else -1
    return False


def _router(data: str, language: str | None) -> tuple[str | None, str | None]:
    """The module-level router variable and its kind (``APIRouter``, ``Blueprint``, ``Router``), if any."""
    m = (_PY_ROUTER if language == "python" else _JS_ROUTER).search(data)
    if m is None:
        return None, None
    return m.group(1), (m.group(2) if language == "python" else "Router")


def _bound_names(clause: str, language: str | None) -> set[str]:
    """Names an import clause binds: ``a as b, c`` → {b, c}; ``X, { a as b }`` → {X, b}; ``* as ns`` → {ns}."""
    names: set[str] = set()
    for part in re.split(r"[,{}()]", clause):
        words = part.replace(":", " as ").split() if language != "python" else part.split()
        if not words or words[0] in ("*",) and len(words) < 3:
            continue
        if "as" in words:
            names.add(words[words.index("as") + 1] if words.index("as") + 1 < len(words) else "")
        elif language == "python":
            names.add(words[0].split(".")[0])
        elif words[0] not in ("type", "typeof"):
            names.add(words[0])
    return {n for n in names if n and _WORD.fullmatch(n)}


def _router_wired(module: Any, users: set[str], importers: dict[str, set[str]], idx: dict[str, Any],
                  text: Callable[[str], str]) -> bool:
    """Whether some importer uses the router, directly or through a package ``__init__`` / index barrel that
    re-exports it."""
    for uid in sorted(users):
        user = idx.get(uid)
        if user is None or not user.path:
            continue
        used, names = _router_used(module, user, text)
        if used:
            return True
        base = posixpath.basename(user.path)
        if names and (base == "__init__.py" or base.startswith("index.")):
            for wid in importers.get(uid, ()):
                w = idx.get(wid)
                if w is not None and w.path and w.id != module.id and _uses_names(text(w.path), names, w.language):
                    return True
    return False


def _uses_names(data: str, names: set[str], language: str | None) -> bool:
    body = (_PY_IMPORT if language == "python" else _JS_IMPORT).sub("", data)
    return any(re.search(r"(?<![\w$])" + re.escape(n) + r"(?![\w$])", body) for n in names)


def _router_used(module: Any, user: Any, text: Callable[[str], str]) -> tuple[bool, set[str]]:
    """Whether ``user`` does something with the router module besides importing it (registers it, lists it...),
    and the names its imports bind to the module."""
    stem = posixpath.basename(module.path).rsplit(".", 1)[0]
    qn = module.qualified_name or ""
    data = text(user.path)
    names: set[str] = set()
    dotted: set[str] = set()  # ``import a.b.c`` is used as ``a.b.c.router``
    if user.language == "python":
        pattern = _PY_IMPORT
        for m in pattern.finditer(data):
            base, clause, plain = m.group(1), m.group(2), m.group(3)
            if base is not None:  # from base import clause
                rel = base.lstrip(".")
                if base == qn or (rel and qn.endswith("." + rel)):
                    names |= _bound_names(clause, "python")
                elif re.search(r"(?<!\w)" + re.escape(stem) + r"(?!\w)", clause):
                    names |= {n for n in _bound_names(clause, "python")
                              if n == stem or re.search(r"\b" + re.escape(stem) + r"\s+as\s+" + re.escape(n), clause)}
            elif plain:
                for part in plain.split(","):
                    words = part.split()
                    if words and words[0] == qn:
                        if "as" in words and words[-1] != "as":
                            names.add(words[-1])
                        else:
                            dotted.add(qn)
    else:
        pattern = _JS_IMPORT
        for m in pattern.finditer(data):
            spec = m.group("spec") or ""
            if posixpath.basename(spec).rsplit(".", 1)[0] == stem or spec.endswith("/" + stem):
                names |= _bound_names(",".join(m.group(g) or "" for g in ("imp", "exp", "req")), "js")
    body = pattern.sub("", data)
    names -= {"import", "from", "as", "require", "const", "let", "var", "default", "type"}
    if user.language != "python" and any(posixpath.basename(sp).rsplit(".", 1)[0] == stem
                                         for sp in _JS_INLINE_LOAD.findall(body)):
        return True, names  # app.use('/orders', require('./routes/orders'))
    used = any(d in body for d in dotted) or any(re.search(r"(?<![\w$])" + re.escape(n) + r"(?![\w$])", body)
                                                 for n in names)
    return used, names


def _registration_file(target: RepositorySnapshot, text: Callable[[str], str], language: str | None,
                       importers: dict[str, set[str]], idx: dict[str, Any]) -> str | None:
    """Where routers are registered today (``app.include_router(...)``, ``app.use(...)``), to suggest where to
    register a new one."""
    modules = sorted((n for n in target.modules if n.path and "test" not in n.tags and n.language in LANGUAGES
                      and _family(n.language) == _family(language)), key=lambda n: n.path)
    if language == "python":
        return next((n.path for n in modules if _PY_REGISTER.search(text(n.path))), None)
    for n in modules:  # JavaScript: a module that mounts an existing router
        if _JS_ROUTER.search(text(n.path)):
            for uid in sorted(importers.get(n.id, ())):
                u = idx.get(uid)
                if u is not None and u.path and _JS_REGISTER.search(text(u.path)):
                    return u.path
    return None


def _name_used(symbol: Any, module: Any, importers: dict[str, set[str]], idx: dict[str, Any],
               text: Callable[[str], str]) -> bool:
    """Whether ``symbol``'s name appears outside its own definition: in its module or in the modules that import
    it (directly, or through a package that re-exports it)."""
    word = re.compile(r"(?<![\w$])" + re.escape(symbol.name) + r"(?![\w$])")
    start, end = symbol.start_line or 0, symbol.end_line or 0
    for i, line in enumerate(text(module.path).splitlines(), 1):
        if not start <= i <= end and word.search(line):
            return True
    frontier = set(importers.get(module.id, ()))
    seen: set[str] = set()
    for _depth in range(2):
        nxt: set[str] = set()
        for uid in frontier - seen:
            seen.add(uid)
            u = idx.get(uid)
            if u is None or not u.path:
                continue
            if word.search(text(u.path)):
                return True
            nxt |= importers.get(uid, set())
        frontier = nxt
    return False
