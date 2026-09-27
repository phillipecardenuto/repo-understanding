"""Cross-service contracts, read from source text: HTTP routes, background tasks and environment variables.

In multi-service systems the breaking changes cross process boundaries, where imports do not reach: a route a
frontend still calls, a task whose ``.delay()`` callers pass the old arguments, an environment variable that
the deployment no longer sets.  This module extracts, from one file's text (never running it):

* **providers**: HTTP routes (FastAPI / Starlette, Flask, Express), Celery tasks, and environment variables
  declared by Compose, ``.env.example``-style files, Dockerfiles (``ENV`` / ``ARG``) and Kubernetes-style
  ``env: - name:`` lists;
* **consumers**: HTTP calls (``requests``, ``httpx``, ``fetch``, ``axios``) whose URL is a literal or a simple
  template, task calls (``.delay``, ``.apply_async``, ``send_task``), and environment reads (``os.environ``,
  ``os.getenv``, pydantic ``BaseSettings`` fields, ``process.env``, ``import.meta.env``).

URLs are normalised to path templates (``/api/images/{}``); a URL whose path cannot be read (no literal
segment) is ignored rather than guessed.  :func:`match` compares a consumer's template with a route's.
"""

from __future__ import annotations

import ast
import posixpath
import re
from typing import Any

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_ROUTE_ATTRS = {"get", "post", "put", "patch", "delete", "head", "options", "websocket", "route", "api_route"}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Set by the platform, the shell or the tooling, not by the application's deployment.
PLATFORM_ENV = {"PATH", "HOME", "USER", "PWD", "SHELL", "HOSTNAME", "LANG", "LC_ALL", "TZ", "TERM", "TMPDIR", "CI",
                "NODE_ENV", "PYTHONPATH", "VIRTUAL_ENV", "GITHUB_ACTIONS", "DEV", "PROD", "MODE", "BASE_URL", "SSR"}
MAX_ITEMS = 200  # of each kind, per file

# Cheap filters: a file is parsed only when it may hold one of these.  Plain substring tests come first and each
# regular expression starts with a literal, so a large repository without any of them is scanned quickly.
_PY_ROUTE_DEC = re.compile(r"@\s*[\w.]+\.(?:get|post|put|patch|delete|head|options|websocket|route|api_route)\s*\("
                           r"\s*[rbuf]*['\"](?:/|['\"])")  # the first argument is a path: @mock.patch("a.b") is not
_PY_TASK_DEC = re.compile(r"@\s*[\w.]+\.task\b")
_PY_HTTP = re.compile(r"(?:requests|httpx)\.(?:get|post|put|patch|delete|head|options|request|Client|AsyncClient|"
                      r"Session|session)\s*\(")
_PY_LITERALS = ("APIRouter(", "Blueprint(", "include_router(", "register_blueprint(", "shared_task", ".delay(",
                "apply_async(", "send_task(", "BaseSettings")
PY_ENV_HINT = re.compile(r"environ\s*(?:\[|\.(?:get|setdefault)\s*\()|getenv\s*\(")
_JS_LITERALS = ("fetch", "axios", "process.env", "import.meta.env")
_JS_ROUTE_CALL = re.compile(r"\.(?:get|post|put|patch|delete|all|use)\s*\(\s*[`'\"]")


def python_hint(text: str) -> str | None:
    """``"ast"`` when a Python file may hold routes, tasks or HTTP calls; ``"env"`` when it may only read the
    environment; ``None`` otherwise."""
    if any(k in text for k in _PY_LITERALS) or "@" in text and (_PY_ROUTE_DEC.search(text) or _PY_TASK_DEC.search(text)) \
            or ("requests." in text or "httpx." in text) and _PY_HTTP.search(text):
        return "ast"
    if ("environ" in text or "getenv" in text) and PY_ENV_HINT.search(text):
        return "env"
    return None


def js_hint(text: str) -> bool:
    return any(k in text for k in _JS_LITERALS) or _JS_ROUTE_CALL.search(text) is not None


_PY_ENV_SUB = re.compile(r"\benviron\s*\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\](?!\s*=[^=])")
_PY_ENV_CALL = re.compile(r"\b(?:environ\.get|environ\.setdefault|getenv)\s*\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*([,)])")


# --------------------------------------------------------------------------- URL templates


def normalize_path(path: str) -> str | None:
    """``/api/images/{id}``, ``/api/images/<int:id>``, ``/api/images/:id`` → ``/api/images/{}``; a full URL keeps its
    path; ``None`` when there is no path with a literal segment."""
    p = str(path).strip()
    p = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*", "", p)  # scheme://host[:port]
    p = p.split("?", 1)[0].split("#", 1)[0]
    p = re.sub(r"\{[^{}]*\}", "{}", p)  # {id}, {id:int}, and the {} of dynamic parts
    p = re.sub(r"<(?:[^:<>]+:)?[^<>]+>", "{}", p)  # Flask <int:id>
    p = re.sub(r"(?<=/):[A-Za-z_]\w*\??", "{}", p)  # Express :id
    p = re.sub(r"^(?:\{\})+(?=/)", "", p)  # `${API_BASE}/api/x`
    if not p.startswith("/"):
        return None
    p = re.sub(r"/{2,}", "/", p)
    if len(p) > 1:
        p = p.rstrip("/")
    return p


def has_literal(template: str) -> bool:
    return bool(re.search(r"/[^/{}]+", template))


def params_of(template: str) -> int:
    return template.count("{}")


def match(consumer: str, route: str) -> bool:
    """A consumer path template reaches a route's: a route parameter takes any segment, and a dynamic part of
    the consumer's URL only matches a route parameter (conservative)."""
    a, b = consumer.split("/"), route.split("/")
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x == y or y == "{}":
            continue
        if "{}" in y and "{}" not in x and re.fullmatch(re.escape(y).replace(r"\{\}", "[^/]+"), x):
            continue
        return False
    return True


def method_match(consumer: str | None, route: str) -> bool:
    return consumer is None or route in ("*", consumer) or (route == "GET" and consumer == "HEAD")


def join_paths(*parts: str) -> str:
    out = "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    return "/" + out if out else "/"


# --------------------------------------------------------------------------- Python


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _const(node: ast.AST | None) -> Any:
    return node.value if isinstance(node, ast.Constant) else None


def _template(node: ast.AST | None) -> str | None:
    """A URL with its dynamic parts as ``{}``: a literal, an f-string, ``'a' + x + 'b'``, ``'…{}'.format(x)``;
    ``None`` when no part of it is literal."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}" for v in node.values]
        return "".join(parts) if any(p != "{}" for p in parts) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _template(node.left), _template(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else "{}") + (right if right is not None else "{}")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        fmt = _const(node.func.value)
        return re.sub(r"\{[^{}]*\}", "{}", fmt) if isinstance(fmt, str) else None
    return None


def _kw(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _str_list(node: ast.AST | None) -> list[str] | None:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        vals = [_const(e) for e in node.elts]
        return [v for v in vals if isinstance(v, str)] if all(isinstance(v, str) for v in vals) else None
    return None


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef, drop_first: bool) -> dict[str, Any]:
    a = fn.args
    positional = [x.arg for x in a.posonlyargs + a.args]
    n_defaults = len(a.defaults)
    required = positional[:len(positional) - n_defaults]
    if drop_first and positional:
        positional, required = positional[1:], required[1:] if required else []
    kwonly = [x.arg for x in a.kwonlyargs]
    kw_required = [x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
    return {"positional": positional, "required": required + kw_required, "kwonly": kwonly,
            "var_positional": a.vararg is not None, "var_keyword": a.kwarg is not None,
            "signature": "(" + ", ".join(positional + [f"*{a.vararg.arg}" if a.vararg else "*"] * bool(kwonly or a.vararg)
                                         + kwonly + ([f"**{a.kwarg.arg}"] if a.kwarg else [])) + ")"}


def _call_args(call: ast.Call, positional: ast.AST | None = None, keywords: ast.AST | None = None
               ) -> tuple[int | None, list[str] | None]:
    """How many positional arguments and which keyword names a call passes (``None``: not known statically)."""
    if positional is not None or keywords is not None:  # apply_async(args=[…], kwargs={…}) / send_task
        n = len(positional.elts) if isinstance(positional, (ast.List, ast.Tuple)) and not any(
            isinstance(e, ast.Starred) for e in positional.elts) else (0 if positional is None else None)
        names: list[str] | None = []
        if isinstance(keywords, ast.Dict):
            keys = [_const(k) for k in keywords.keys]
            names = [k for k in keys if isinstance(k, str)] if all(isinstance(k, str) for k in keys) else None
        elif keywords is not None:
            names = None
        return n, names
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        return None, None
    return len(call.args), [k.arg for k in call.keywords if k.arg]


def python_env_reads(text: str) -> list[dict[str, Any]]:
    """``os.environ["X"]``, ``os.getenv("X", default)``, ``os.environ.get(…)`` without parsing (comments skipped)."""
    out = []
    for pattern in (_PY_ENV_SUB, _PY_ENV_CALL):
        for m in pattern.finditer(text):
            start = text.rfind("\n", 0, m.start()) + 1
            if "#" in text[start:m.start()]:
                continue
            out.append({"name": m.group(1), "line": text.count("\n", 0, m.start()) + 1,
                        "default": pattern is _PY_ENV_CALL and m.group(2) == ","})
    return sorted(out, key=lambda x: x["line"])[:MAX_ITEMS]


def python_interfaces(text: str) -> dict[str, Any] | None:
    """Routes, routers, tasks and their consumers in one Python file (``None``: it does not parse)."""
    out: dict[str, Any] = {"routes": [], "routers": {}, "includes": [], "tasks": [], "enqueues": [], "http_calls": [],
                           "env_reads": []}
    if python_hint(text) != "ast":  # only environment reads: no need to parse
        out["env_reads"] = python_env_reads(text)
        return out
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    celery = "celery" in text.lower() or "shared_task" in text
    clients: dict[str, str] = {}  # httpx.Client(base_url=…) / requests.Session() variables → base path
    parents: dict[int, ast.AST] = {}  # definitions only: enough for qualified names
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    parents[id(child)] = node

    def qualname(fn: ast.AST) -> str:
        names = [fn.name]  # type: ignore[attr-defined]
        p = parents.get(id(fn))
        while p is not None and not isinstance(p, ast.Module):
            names.append(p.name)  # type: ignore[attr-defined]
            p = parents.get(id(p))
        return ".".join(reversed(names))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Call):
            var, call = node.targets[0].id, node.value
            ctor = _dotted(call.func).split(".")[-1]
            if ctor in ("APIRouter", "Blueprint"):
                prefix = _const(_kw(call, "prefix" if ctor == "APIRouter" else "url_prefix"))
                out["routers"][var] = {"kind": ctor, "prefix": prefix if isinstance(prefix, str) else "",
                                       "line": node.lineno}
            elif ctor in ("FastAPI", "Flask", "Starlette", "Quart", "Sanic"):
                out["routers"][var] = {"kind": ctor, "prefix": "", "line": node.lineno, "app": True}
            elif _dotted(call.func) in ("httpx.Client", "httpx.AsyncClient", "requests.Session", "requests.session"):
                base = _template(_kw(call, "base_url"))
                clients[var] = normalize_path(base) or "" if base else ""
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                call = dec if isinstance(dec, ast.Call) else None
                func = call.func if call else dec
                if isinstance(func, ast.Attribute) and func.attr in _ROUTE_ATTRS and call is not None:
                    raw = _template(call.args[0] if call.args else _kw(call, "path") or _kw(call, "rule"))
                    if raw is None or raw and normalize_path(raw) is None:
                        continue  # "" is the router's own prefix (FastAPI)
                    if func.attr in ("route", "api_route"):
                        methods = [m.upper() for m in _str_list(_kw(call, "methods")) or ["GET"]]
                    else:
                        methods = ["WS" if func.attr == "websocket" else func.attr.upper()]
                    out["routes"].append({"owner": _dotted(func.value), "methods": methods, "path": raw,
                                          "line": dec.lineno, "handler": qualname(node)})
                dotted = _dotted(func)
                if celery and (dotted == "shared_task" or dotted.endswith(".task") and "." in dotted):
                    name = _const(_kw(call, "name")) if call else None
                    bind = bool(call is not None and _const(_kw(call, "bind")) is True)
                    out["tasks"].append({"func": qualname(node), "name": name if isinstance(name, str) else None,
                                         "line": node.lineno, "bind": bind, **_params(node, drop_first=bind)})
        elif isinstance(node, ast.Call):
            func = node.func
            dotted = _dotted(func)
            attr = func.attr if isinstance(func, ast.Attribute) else ""
            if attr in ("include_router", "register_blueprint") and node.args:
                prefix = _const(_kw(node, "prefix" if attr == "include_router" else "url_prefix"))
                out["includes"].append({"owner": _dotted(func.value), "router": _dotted(node.args[0]),
                                        "prefix": prefix if isinstance(prefix, str) else "", "line": node.lineno})
            elif attr in ("delay", "apply_async") and isinstance(func, ast.Attribute):
                target = _dotted(func.value)
                if not target:
                    continue
                if attr == "delay":
                    n, names = _call_args(node)
                else:
                    args = _kw(node, "args") or (node.args[0] if node.args else None)
                    kwargs = _kw(node, "kwargs") or (node.args[1] if len(node.args) > 1 else None)
                    n, names = _call_args(node, args if args is not None else ast.Tuple(elts=[]), kwargs)
                out["enqueues"].append({"target": target, "kind": attr, "positional": n, "keywords": names,
                                        "line": node.lineno})
            elif attr == "send_task" and node.args and isinstance(_const(node.args[0]), str):
                n, names = _call_args(node, _kw(node, "args") or ast.Tuple(elts=[]), _kw(node, "kwargs"))
                out["enqueues"].append({"name": _const(node.args[0]), "kind": "send_task", "positional": n,
                                        "keywords": names, "line": node.lineno})
            elif dotted in ("os.getenv", "getenv", "os.environ.get", "environ.get", "os.environ.setdefault",
                            "environ.setdefault") and node.args:
                name = _const(node.args[0])
                if isinstance(name, str) and _ENV_NAME.match(name):
                    default = len(node.args) > 1 or _kw(node, "default") is not None
                    out["env_reads"].append({"name": name, "line": node.lineno, "default": default})
            else:
                method, url = None, None
                owner = _dotted(func.value) if isinstance(func, ast.Attribute) else ""
                if owner in ("requests", "httpx") or owner in clients:
                    if attr.upper() in HTTP_METHODS:
                        method, url = attr.upper(), node.args[0] if node.args else _kw(node, "url")
                    elif attr == "request" and len(node.args) >= 2 and isinstance(_const(node.args[0]), str):
                        method, url = str(_const(node.args[0])).upper(), node.args[1]
                raw = _template(url)
                template = normalize_path((clients.get(owner, "") + raw) if raw and owner in clients and raw.startswith("/")
                                          else raw) if raw is not None else None
                if template and has_literal(template):
                    out["http_calls"].append({"method": method, "template": template, "line": node.lineno,
                                              "url": raw[:200], "confidence": 0.6 if "{}" in template else 0.7})
        elif isinstance(node, ast.Subscript) and _dotted(node.value) in ("os.environ", "environ") \
                and not isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del)):
            name = _const(node.slice)
            if isinstance(name, str) and _ENV_NAME.match(name):
                out["env_reads"].append({"name": name, "line": node.lineno, "default": False})
        elif isinstance(node, ast.ClassDef) and any(_dotted(b).endswith("BaseSettings") for b in node.bases):
            prefix = ""
            for st in ast.walk(node):
                if isinstance(st, ast.keyword) and st.arg == "env_prefix" and isinstance(_const(st.value), str):
                    prefix = _const(st.value)
                elif isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "env_prefix"
                                                        for t in st.targets) and isinstance(_const(st.value), str):
                    prefix = _const(st.value)
            for st in node.body:
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) \
                        and not st.target.id.startswith("_") and st.target.id != "model_config" \
                        and "ClassVar" not in ast.unparse(st.annotation):
                    out["env_reads"].append({"name": (prefix + st.target.id).upper(), "line": st.lineno,
                                             "default": st.value is not None, "via": f"{node.name} (BaseSettings)"})
    for key in ("routes", "includes", "tasks", "enqueues", "http_calls", "env_reads"):
        out[key] = sorted(out[key], key=lambda x: x["line"])[:MAX_ITEMS]  # ast.walk is breadth first
    return out


# --------------------------------------------------------------------------- JavaScript / TypeScript


_JS_ROUTE = re.compile(r"\b([A-Za-z_$][\w$]*)\.(get|post|put|patch|delete|all|options|head)\s*\(")
_JS_USE = re.compile(r"\b([A-Za-z_$][\w$]*)\.use\s*\(")
_JS_ROUTER = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:express\.)?Router\s*\(")
_JS_APP = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*express\s*\(\s*\)")
_JS_FETCH = re.compile(r"(?<![\w$.])fetch\s*\(")
_JS_AXIOS = re.compile(r"\b(axios|[A-Za-z_$][\w$]*)\.(get|post|put|patch|delete|head|options)\s*\(")
_JS_AXIOS_CREATE = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*axios\.create\s*\(")
_JS_ENV = re.compile(r"\b(process\.env|import\.meta\.env)(?:\.([A-Za-z_][\w]*)|\[\s*['\"]([A-Za-z_][\w]*)['\"]\s*\])")
_JS_IMPORT = re.compile(r"import\s+([A-Za-z_$][\w$]*)\s+from\s+['\"]([^'\"]+)['\"]|"
                        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
_JS_STRING = re.compile(r"\s*(`(?:[^`\\]|\\.)*`|'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\")\s*(\+\s*[\w$.()[\]'\"`]+)?")


_JS_LEAD = re.compile(r"\s*[A-Za-z_$][\w$.]*\s*\+(?=\s*[`'\"])")


def _js_literal(noc: str, pos: int) -> str | None:
    """The string argument starting at ``pos``, with ``${…}``, a leading ``base +`` and a trailing ``+ expr`` as
    ``{}``."""
    lead = _JS_LEAD.match(noc, pos)
    m = _JS_STRING.match(noc, lead.end() if lead else pos)
    if not m:
        return None
    lit = ("{}" if lead else "") + m.group(1)[1:-1]
    lit = re.sub(r"\$\{[^{}]*\}", "{}", lit)
    return lit + ("{}" if m.group(2) else "")


def js_interfaces(text: str) -> dict[str, Any]:
    """Express routes and their mounts, HTTP calls and environment reads in one JavaScript / TypeScript file."""
    from .analyzers.javascript import mask

    code, noc = mask(text)
    line_of = lambda pos: text.count("\n", 0, pos) + 1  # noqa: E731
    routers = {m.group(1) for m in _JS_ROUTER.finditer(code)}
    apps = {m.group(1) for m in _JS_APP.finditer(code)}
    express = bool(routers or apps) or "express" in text
    out: dict[str, Any] = {"routes": [], "mounts": [], "imports": {}, "http_calls": [], "env_reads": [],
                           "routers": sorted(routers), "apps": sorted(apps)}
    for m in _JS_IMPORT.finditer(noc):
        name, spec = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        out["imports"][name] = spec
    bases = {}
    for m in _JS_AXIOS_CREATE.finditer(code):
        seg = noc[m.end():m.end() + 400]
        b = re.search(r"baseURL\s*:\s*(['\"`])([^'\"`]*)\1", seg)
        bases[m.group(1)] = normalize_path(re.sub(r"\$\{[^{}]*\}", "{}", b.group(2))) or "" if b else ""
    if express:
        for m in _JS_ROUTE.finditer(code):
            owner, verb = m.group(1), m.group(2)
            if owner in bases or owner not in routers and owner not in apps and owner not in ("app", "router", "api", "server"):
                continue  # an axios instance calls routes; it does not define them
            raw = _js_literal(noc, m.end())
            template = normalize_path(raw) if raw else None
            if template:
                out["routes"].append({"owner": owner, "methods": ["*" if verb == "all" else verb.upper()],
                                      "path": raw, "line": line_of(m.start()), "handler": f"{owner}.{verb}"})
        for m in _JS_USE.finditer(code):
            raw = _js_literal(noc, m.end())
            rest = re.match(r"\s*(?:`[^`]*`|'[^']*'|\"[^\"]*\")\s*,\s*([A-Za-z_$][\w$]*)", noc[m.end():m.end() + 300])
            if raw and rest and normalize_path(raw):
                out["mounts"].append({"owner": m.group(1), "prefix": raw, "router": rest.group(1),
                                      "line": line_of(m.start())})
    for m in list(_JS_FETCH.finditer(code)) + list(_JS_AXIOS.finditer(code)):
        is_fetch = m.re is _JS_FETCH
        if not is_fetch and m.group(1) != "axios" and m.group(1) not in bases:
            continue
        raw = _js_literal(noc, m.end())
        if raw is None:
            continue
        if is_fetch:
            opts = noc[m.end():m.end() + 300].split("fetch(")[0]
            mm = re.search(r"method\s*:\s*['\"`](\w+)['\"`]", opts)
            method = mm.group(1).upper() if mm else "GET"
        else:
            method = m.group(2).upper()
        base = bases.get(m.group(1), "") if not is_fetch else ""
        template = normalize_path(base + raw if base and raw.startswith("/") else raw)
        if template and has_literal(template):
            out["http_calls"].append({"method": method, "template": template, "line": line_of(m.start()),
                                      "url": raw[:200], "confidence": 0.6 if "{}" in template else 0.7})
    for m in _JS_ENV.finditer(code):
        name = m.group(2) or m.group(3)
        if not name:
            continue
        after = code[m.end():m.end() + 8]
        out["env_reads"].append({"name": name, "line": line_of(m.start()),
                                 "default": bool(re.match(r"\s*(\|\||\?\?)", after))})
    for key in ("routes", "mounts", "http_calls", "env_reads"):
        out[key] = out[key][:MAX_ITEMS]
    return out


def resolve_js(spec: str, importer: str, files: set[str]) -> str | None:
    """A relative import of a JavaScript file, as a repository path."""
    if not spec.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    for cand in (base, *(base + e for e in (".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx")),
                 *(f"{base}/index{e}" for e in (".js", ".ts"))):
        if cand in files:
            return cand
    return None


# --------------------------------------------------------------------------- environment declarations


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", re.M)
_DOCKER_ENV = re.compile(r"^\s*(ENV|ARG)\s+(.+)$", re.I | re.M)
_K8S_ENV = re.compile(r"^\s*-\s*name\s*:\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*$", re.M)


def is_env_file(path: str) -> bool:
    name = posixpath.basename(path)
    return name.startswith(".env") or name.endswith(".env") or name in ("env.example", "env.sample", "env.template")


def env_declarations(path: str, text: str) -> list[tuple[str, int]]:
    """Variable names a file declares (never their values), with lines."""
    name = posixpath.basename(path)
    line_of = lambda pos: text.count("\n", 0, pos) + 1  # noqa: E731
    if is_env_file(path):
        return [(m.group(1), line_of(m.start())) for m in _ENV_LINE.finditer(text)]
    if name.startswith("Dockerfile") or name.endswith(".dockerfile") or name.endswith(".Dockerfile"):
        out = []
        for m in _DOCKER_ENV.finditer(text):
            body = m.group(2).strip()
            names = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", body) or ([body.split()[0]] if body.split() else [])
            out += [(n, line_of(m.start())) for n in names if _ENV_NAME.match(n)]
        return out
    if name.endswith((".yaml", ".yml")) and re.search(r"^\s*env\s*:\s*$", text, re.M) and "- name:" in text:
        return [(m.group(1), line_of(m.start())) for m in _K8S_ENV.finditer(text)]
    return []


# --------------------------------------------------------------------------- task calls


def call_fits(call: dict[str, Any], task: dict[str, Any]) -> bool | None:
    """Whether a task call's arguments fit the task's parameters (``None``: not known statically)."""
    n, names = call.get("positional"), call.get("keywords")
    if n is None or names is None:
        return None
    positional = task.get("positional") or []
    if n > len(positional) and not task.get("var_positional"):
        return False
    given = set(positional[:n]) | set(names)
    if any(k not in positional and k not in (task.get("kwonly") or []) for k in names) and not task.get("var_keyword"):
        return False
    return all(r in given for r in task.get("required") or [])
