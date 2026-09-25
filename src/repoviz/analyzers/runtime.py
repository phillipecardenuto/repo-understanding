"""Runtime coupling analyzer: code that starts containers or calls services, linked to what provides them.

Imports miss the architecture of a system whose parts talk at run time: an API that starts ML containers with
the Docker SDK or ``docker run``, and calls other services over HTTP.  This analyzer finds, in Python and
JavaScript text (parsed, never executed):

* **images** a module starts (``client.containers.run(IMAGE)``, ``["docker", "run", …, IMAGE]``) → an
  ``invokes-container`` edge to the code that builds the image;
* **service URLs and hosts** (``requests.post("http://cbir-service:8000/search")``, ``f"http://{HOST}:8000"``)
  → a ``talks-to`` edge to the service.

Values are resolved from string literals, f-strings, module and class constants, imported constants and
``os.getenv("KEY", default)``; a ``KEY`` that a Compose file points at a service counts as that service.  What
provides an image comes from Compose (``build`` + ``image``), ``docker build -t`` in Makefiles and CI files, and
submodules holding a Dockerfile (by name).  Unknown images and hosts make no edge: they are listed on the module as
``external_runtime_references``.  In the last phase, each edge is also drawn between the services involved (the
service running the code → the service providing the image or host), for the System view.
"""

from __future__ import annotations

import ast
import posixpath
import re
import shlex
from typing import Any

from ..model import CATEGORY_SYMBOL, REL_IMPORTS, REL_INVOKES_CONTAINER, REL_RUNS, REL_TALKS_TO
from ..redact import redact
from ..services import image_name
from .base import CAP_DEPENDENCIES, CAP_EVIDENCE, AnalysisContext, Analyzer, Detection, SnapshotBuilder

MAX_TEXT = 400_000
MAX_EXTERNAL_REFS = 10
MAX_DEPTH = 8

_DOCKER_USE = re.compile(r"containers\.(?:run|create)\s*\(|[\"']docker[\"']\s*,\s*[\"'](?:run|create)[\"']"
                         r"|\bdocker\s+(?:run|create)\b|docker\.from_env|DockerClient\s*\(")
_HTTP_HINT = re.compile(r"[a-z]://|\b(?:requests|httpx|aiohttp|urllib3?|urlopen|grpc)\b|_(?:URL|URI|HOST|ENDPOINT|ADDR)\b")
_URL = re.compile(r"\b([a-z][a-z0-9+.-]*)://(?:[^@\s/\"'`{}]+@)?([A-Za-z0-9_.-]+)(?::(\d+))?")
_HOSTLIKE_NAME = re.compile(r"(?:^|_)(HOST|HOSTNAME|SERVER|ADDR|ADDRESS|ENDPOINT|URL|URI)$", re.I)
_HOST_PORT = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*)(?::(\d+))?$")
_IMAGE_LIKE = re.compile(r"^(?:[a-z0-9.-]+(?::\d+)?/)?[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
                         r"(?::[\w][\w.-]{0,127})?$")
_HTTP_CALL = re.compile(r"(?:^|\.)(?:get|post|put|patch|delete|head|options|request|urlopen|stream|fetch|Client|"
                        r"AsyncClient|Session|ClientSession|ws_connect|websocket|connect)$")
_GETENV = {"os.getenv", "getenv", "os.environ.get", "environ.get", "env", "config", "os.environ.setdefault"}
_LOCALHOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
_JS_URL = re.compile(r"[\"'`]((?:https?|wss?)://[^\"'`\s]+)")
_DOCKER_BUILD = re.compile(r"\bdocker\s+(?:buildx\s+)?build\b(.*)")
_BUILD_VALUE_OPTS = {"-t", "--tag", "-f", "--file", "--build-arg", "--target", "--platform", "--label", "--network",
                     "--secret", "--ssh", "--cache-from", "--cache-to", "-o", "--output", "--progress", "--iidfile",
                     "--add-host", "--shm-size", "-m", "--memory"}


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _identifier(node: ast.AST) -> str:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""


class _PythonScopes:
    """Constants and imports of Python modules, read from their syntax tree (never executed)."""

    def __init__(self, ctx: AnalysisContext, env_hosts: dict[str, Any]) -> None:
        self.ctx = ctx
        self.modules = ctx.shared.get("python.modules", {})
        self.by_qual: dict[str, list[str]] = ctx.shared.get("python.by_qual", {})
        self.env_hosts = env_hosts
        self._trees: dict[str, ast.Module | None] = {}
        self._consts: dict[str, dict[str, ast.AST]] = {}
        self._imports: dict[str, dict[str, tuple[str | None, str | None]]] = {}

    def tree(self, path: str) -> ast.Module | None:
        if path not in self._trees:
            text = self.ctx.text(path)
            try:
                self._trees[path] = ast.parse(text) if text is not None and len(text) <= MAX_TEXT else None
            except (SyntaxError, ValueError):
                self._trees[path] = None
        return self._trees[path]

    def module_path(self, qual: str, near: str) -> str | None:
        paths = self.by_qual.get(qual) or []
        if len(paths) <= 1:
            return paths[0] if paths else None
        root = self.modules[near].root if near in self.modules else ""
        same = [p for p in paths if self.modules.get(p) is not None and self.modules[p].root == root]
        return same[0] if len(same) == 1 else None

    def consts(self, path: str) -> dict[str, ast.AST]:
        """Names bound at module level and in top-level classes (settings classes), first binding wins."""
        if path in self._consts:
            return self._consts[path]
        out: dict[str, ast.AST] = {}

        def visit(body: list[ast.stmt], in_class: bool) -> None:
            for st in body:
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    out.setdefault(st.targets[0].id, st.value)
                elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
                    out.setdefault(st.target.id, st.value)
                elif isinstance(st, ast.ClassDef) and not in_class:
                    visit(st.body, True)
                elif isinstance(st, (ast.If, ast.Try)) and not in_class:
                    visit(st.body, False)
                    for extra in (getattr(st, "orelse", []), *[h.body for h in getattr(st, "handlers", [])]):
                        visit(extra, False)

        tree = self.tree(path)
        if tree is not None:
            visit(tree.body, False)
        self._consts[path] = out
        return out

    def imports(self, path: str) -> dict[str, tuple[str | None, str | None]]:
        """alias -> (module path, attribute): ``(M, None)`` binds a module, ``(M, "X")`` a name defined in M."""
        if path in self._imports:
            return self._imports[path]
        out: dict[str, tuple[str | None, str | None]] = {}
        mod = self.modules.get(path)
        tree = self.tree(path)
        for node in ast.walk(tree) if tree is not None else ():
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        out.setdefault(a.asname, (self.module_path(a.name, path), None))
                    else:
                        top = a.name.split(".")[0]
                        out.setdefault(top, (self.module_path(top, path), None))
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level and mod is not None:
                    pkg = mod.qualname.split(".") if mod.is_package else mod.qualname.split(".")[:-1]
                    pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                    base = ".".join([*pkg, *([base] if base else [])])
                for a in node.names:
                    if a.name == "*":
                        continue
                    sub = self.module_path(f"{base}.{a.name}", path) if base else self.module_path(a.name, path)
                    out.setdefault(a.asname or a.name, (sub, None) if sub else (self.module_path(base, path), a.name))
        self._imports[path] = out
        return out

    # -- evaluation of constant expressions (strings only) ------------------------------------------------

    def value(self, node: ast.AST, path: str, depth: int = 0, seen: frozenset[tuple[str, str]] = frozenset()
              ) -> str | None:
        """The string an expression holds when nothing is configured (or what Compose points it at), or ``None``."""
        if depth > MAX_DEPTH:
            return None
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else str(node.value) if isinstance(node.value, int) \
                and not isinstance(node.value, bool) else None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                elif isinstance(v, ast.FormattedValue):
                    inner = self.value(v.value, path, depth + 1, seen)
                    parts.append(inner if inner is not None else "{?}")
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.value(node.left, path, depth + 1, seen), self.value(node.right, path, depth + 1, seen)
            return left + right if left is not None and right is not None else None
        if isinstance(node, ast.Call):
            func = _dotted(node.func)
            if func in _GETENV and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                link = self.env_hosts.get(node.args[0].value)
                if link:  # a Compose file points this variable at a service
                    if link.get("scheme"):  # a URL variable: the whole address
                        return f"{link['scheme']}://{link['host']}" + (f":{link['port']}" if link.get("port") else "")
                    return link["host"]  # a host variable: the port comes from elsewhere (a *_PORT variable)
                default = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "default"), None)
                return self.value(default, path, depth + 1, seen) if default is not None else None
            if func in ("int", "str", "float") and node.args:
                return self.value(node.args[0], path, depth + 1, seen)
            if func.endswith("Field"):
                default = next((k.value for k in node.keywords if k.arg == "default"), node.args[0] if node.args else None)
                return self.value(default, path, depth + 1, seen) if default is not None else None
            return None
        if isinstance(node, ast.Name):
            return self.lookup(path, node.id, depth, seen)
        if isinstance(node, ast.Attribute):
            base = self.base_module(node.value, path)
            return self.lookup(base, node.attr, depth, seen) if base else None
        return None

    def lookup(self, path: str, name: str, depth: int, seen: frozenset[tuple[str, str]]) -> str | None:
        if (path, name) in seen:
            return None
        seen = seen | {(path, name)}
        expr = self.consts(path).get(name)
        if expr is not None:
            return self.value(expr, path, depth + 1, seen)
        mod_path, attr = self.imports(path).get(name, (None, None))
        if mod_path and attr:
            return self.lookup(mod_path, attr, depth + 1, seen)
        return None

    def base_module(self, node: ast.AST, path: str) -> str | None:
        """The module an attribute's base stands for: an imported module, or the module an imported object (a
        settings instance) comes from."""
        if isinstance(node, ast.Name):
            mod_path, attr = self.imports(path).get(node.id, (None, None))
            if mod_path:
                return mod_path
            return path if node.id in self.consts(path) else None
        if isinstance(node, ast.Attribute):
            base = self.base_module(node.value, path)
            if base is None:
                return None
            mod = self.modules.get(base)
            sub = self.module_path(f"{mod.qualname}.{node.attr}", base) if mod is not None else None
            return sub or base
        return None


_RUN_VALUE_OPTS = {"-v", "--volume", "-e", "--env", "-p", "--publish", "--name", "--network", "--net", "-w", "--workdir",
                   "--gpus", "--entrypoint", "-u", "--user", "--mount", "--shm-size", "--cpus", "-m", "--memory",
                   "--platform", "--runtime", "--ipc", "--label", "-l", "--add-host", "--device", "--env-file",
                   "--hostname", "-h", "--restart", "--pull", "--cidfile", "--log-driver", "--log-opt"}


def docker_run_image(tokens: list[str]) -> str | None:
    """The image of a ``docker run`` / ``docker create`` command given as tokens (``None`` if not one)."""
    for i in range(len(tokens) - 1):
        if tokens[i] == "docker" and tokens[i + 1] in ("run", "create"):
            skip = False
            for tok in tokens[i + 2:]:
                if skip:
                    skip = False
                elif tok in _RUN_VALUE_OPTS:
                    skip = True
                elif tok.startswith("-"):
                    continue
                else:
                    return tok
    return None


def _docker_build_tags(text: str) -> list[tuple[int, str, str]]:
    """``(line, image, context)`` for each ``docker build -t IMAGE CONTEXT`` in a Makefile or CI file."""
    out = []
    lines = text.replace("\\\n", " ").splitlines()
    for i, line in enumerate(lines, 1):
        m = _DOCKER_BUILD.search(line)
        if not m or line.lstrip().startswith("#"):
            continue
        try:
            tokens = shlex.split(m.group(1), comments=True)
        except ValueError:
            tokens = m.group(1).split()
        tags, positional, skip = [], [], False
        for j, tok in enumerate(tokens):
            if skip:
                skip = False
                continue
            if tok in ("-t", "--tag") and j + 1 < len(tokens):
                tags.append(tokens[j + 1])
                skip = True
            elif tok.startswith(("--tag=", "-t=")):
                tags.append(tok.split("=", 1)[1])
            elif tok in _BUILD_VALUE_OPTS:
                skip = True
            elif tok.startswith("-") or tok in ("&&", ";", "|"):
                if tok in ("&&", ";", "|"):
                    break
            else:
                positional.append(tok)
        context = positional[-1] if positional else None
        for tag in tags:
            if context and "$" not in tag and "$" not in context and "{" not in tag:
                out.append((i, tag, context))
    return out


def _definitions(tree: ast.Module) -> list[ast.AST]:
    """Right-hand sides of module- and class-level assignments (where constants are defined)."""
    out: list[ast.AST] = []

    def visit(body: list[ast.stmt], in_class: bool) -> None:
        for st in body:
            if isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None:
                out.append(st.value)
            elif isinstance(st, ast.ClassDef) and not in_class:
                visit(st.body, True)
            elif isinstance(st, (ast.If, ast.Try)) and not in_class:
                visit(st.body, False)
                for extra in (getattr(st, "orelse", []), *[h.body for h in getattr(st, "handlers", [])]):
                    visit(extra, False)

    visit(tree.body, False)
    return out


class RuntimeAnalyzer(Analyzer):
    name = "runtime"
    version = "1"
    capabilities = (CAP_DEPENDENCIES, CAP_EVIDENCE)

    def detect(self, ctx: AnalysisContext) -> Detection:
        langs = {lang["language"] for lang in ctx.profile.languages if lang.get("supported")}
        ok = bool(langs & {"python", "javascript", "typescript"})
        return Detection(ok, "looks for containers and services used at run time" if ok else "no Python or JavaScript")

    # -- providers ------------------------------------------------------------------------------------------

    def _providers(self, ctx: AnalysisContext, b: SnapshotBuilder) -> dict[str, Any]:
        prov = ctx.shared.get("runtime.providers") or {"images": {}, "hosts": {}, "env_hosts": {}}
        images: dict[str, dict[str, Any]] = prov["images"]

        def nearest(path: str) -> str:
            path = posixpath.normpath(path) if path else ""
            path = "" if path in (".", "..") or path.startswith("../") else path
            while path:
                nid = b.path_node_id(path)
                if nid:
                    return nid
                path = posixpath.dirname(path)
            return b.root_id  # type: ignore[return-value]

        ci = {c["path"] for c in ctx.profile.ci}
        for f in ctx.profile.included_files:
            base = posixpath.basename(f)
            if not (base in ("Makefile", "makefile", "GNUmakefile") or base.endswith(".mk") or f in ci):
                continue
            text = ctx.text(f) or ""
            if "docker" not in text:
                continue
            here = posixpath.dirname(f) if f not in ci else ""
            for line, tag, context in _docker_build_tags(text):
                norm = image_name(tag)
                if norm and norm not in images:
                    images[norm] = {"target": nearest(posixpath.join(here, context)), "service": None,
                                    "confidence": 0.8, "via": f"{f}:{line}"}
        for sub in ctx.profile.submodules:  # a submodule with a Dockerfile is the likely source of its namesake image
            if any(p.startswith(sub + "/") and posixpath.basename(p).startswith("Dockerfile")
                   for p in ctx.profile.included_files):
                images.setdefault(posixpath.basename(sub).lower(), {"target": nearest(sub), "service": None,
                                                                    "confidence": 0.6, "via": f"{sub} (Dockerfile)"})
        return prov

    # -- consumers ------------------------------------------------------------------------------------------

    def discover_calls(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        prov = self._providers(ctx, b)
        images, hosts = prov["images"], prov["hosts"]
        if not images and not hosts:
            return  # nothing in this repository to start or call: no scan (large libraries stay fast)
        scopes = _PythonScopes(ctx, prov["env_hosts"])
        edges = 0
        for path in sorted(scopes.modules):
            node = b.nodes.get(b.file_id(path))
            text = ctx.text(path)
            if node is None or text is None or len(text) > MAX_TEXT:
                continue
            docker = bool(images) and _DOCKER_USE.search(text) is not None
            http = bool(hosts) and _HTTP_HINT.search(text) is not None
            if not docker and not http:
                continue
            tree = scopes.tree(path)
            if tree is None:
                continue
            found: dict[tuple[str, str], tuple[int, str, dict[str, Any], float]] = {}
            external: list[dict[str, Any]] = []
            # Constants are resolved where they are used: the module that only defines a URL or an image name
            # (a settings module) is not the one talking to the service.
            defined = {id(x) for st in _definitions(tree) for x in ast.walk(st)}
            http_args = {id(a) for c in ast.walk(tree) if isinstance(c, ast.Call) and _HTTP_CALL.search(_dotted(c.func))
                         for a in c.args[:1]}
            for n in ast.walk(tree):
                if not isinstance(n, (ast.Constant, ast.JoinedStr, ast.Name, ast.Attribute, ast.BinOp)):
                    continue
                if isinstance(n, (ast.Name, ast.Attribute)) and not isinstance(getattr(n, "ctx", None), ast.Load):
                    continue
                if id(n) in defined:
                    continue
                ident = _identifier(n)
                line = getattr(n, "lineno", None) or 1
                if docker and isinstance(n, ast.Constant) and isinstance(n.value, str) and "docker " in n.value:
                    try:  # a shell command: subprocess.run("docker run --rm engine:1", shell=True), os.system(…)
                        img = docker_run_image(shlex.split(n.value))
                    except ValueError:
                        img = None
                    p = images.get(image_name(img) or "") if img else None
                    if p:
                        found.setdefault((REL_INVOKES_CONTAINER, p["target"]), (line, "docker run", {
                            "label": img, "image": image_name(img), "provider_service": p["service"],
                            "provided_by": p["via"]}, 0.8))
                        continue
                if docker and (isinstance(n, ast.Constant) or "image" in ident.lower()):
                    v = scopes.value(n, path)
                    if v and _IMAGE_LIKE.match(v) and (":" in v or "/" in v or isinstance(n, (ast.Name, ast.Attribute))):
                        norm = image_name(v) or ""
                        p = images.get(norm)
                        if p:
                            found.setdefault((REL_INVOKES_CONTAINER, p["target"]), (line, ident or v, {
                                "label": v.split("@")[0], "image": norm, "provider_service": p["service"],
                                "provided_by": p["via"]}, 0.8 if isinstance(n, ast.Constant) else 0.7))
                        elif "image" in ident.lower() and (":" in v or "/" in v) and len(external) < MAX_EXTERNAL_REFS:
                            external.append({"kind": "image", "value": redact(v), "line": line, "via": ident})
                if http and (isinstance(n, (ast.Constant, ast.JoinedStr, ast.BinOp)) or _HOSTLIKE_NAME.search(ident)):
                    if isinstance(n, ast.BinOp) and not isinstance(n.op, ast.Add):
                        continue
                    v = scopes.value(n, path)
                    if not v:
                        continue
                    targets = [(m.group(1), m.group(2), m.group(3)) for m in _URL.finditer(v)]
                    if not targets and _HOSTLIKE_NAME.search(ident) and (hp := _HOST_PORT.match(v)):
                        targets = [(None, hp.group(1), hp.group(2))]
                    for scheme, host, port in targets:
                        sid = hosts.get(host)
                        label = f"{scheme}:{port}" if scheme and port else scheme or (f"port {port}" if port else "")
                        if sid:
                            found.setdefault((REL_TALKS_TO, sid), (line, ident or host, {"label": label, "host": host},
                                                                   0.8 if isinstance(n, ast.Constant) else 0.7))
                        elif id(n) in http_args and len(external) < MAX_EXTERNAL_REFS and host:
                            external.append({"kind": "url", "value": redact(f"{scheme}://{host}" + (
                                f":{port}" if port else "") if scheme else host), "line": line, "via": ident or "literal"})
            for (rel, target), (line, via, meta, conf) in sorted(found.items(), key=lambda kv: kv[1][0]):
                if target == node.id:
                    continue
                b.add_edge(node.id, target, rel, analyzer=self.name, confidence=conf,
                           evidence=[self.evidence(ctx, path, line, line, "container image" if rel ==
                                                   REL_INVOKES_CONTAINER else "service address")],
                           metadata={**{k: v for k, v in meta.items() if v}, "via": via})
                edges += 1
            if external:
                seen = set()
                node.metadata["external_runtime_references"] = [
                    e for e in external if (e["kind"], e["value"]) not in seen and not seen.add((e["kind"], e["value"]))]
        edges += self._javascript(ctx, b, hosts)
        b.stat(self.name, "runtime_edges", edges)

    def _javascript(self, ctx: AnalysisContext, b: SnapshotBuilder, hosts: dict[str, str]) -> int:
        """JS/TS: URL literals naming a service (``fetch("http://api:8000/…")``) → ``talks-to``."""
        count = 0
        for path in ctx.shared.get("javascript.files", {}):
            node = b.nodes.get(b.file_id(path))
            text = ctx.text(path)
            if node is None or not text or len(text) > MAX_TEXT or "://" not in text:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith(("//", "*", "/*")):
                    continue
                for m in _JS_URL.finditer(line):
                    u = _URL.match(m.group(1))
                    if u and u.group(2) in hosts:
                        label = f"{u.group(1)}:{u.group(3)}" if u.group(3) else u.group(1)
                        b.add_edge(node.id, hosts[u.group(2)], REL_TALKS_TO, analyzer=self.name, confidence=0.8,
                                   evidence=[self.evidence(ctx, path, i, i, "service address")],
                                   metadata={"label": label, "host": u.group(2), "via": "literal"})
                        count += 1
        return count

    # -- between services (System view) ----------------------------------------------------------------------

    def finalize(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        runtime = [e for e in list(b.edges.values()) if e.analyzer == self.name and e.direct
                   and e.relationship in (REL_INVOKES_CONTAINER, REL_TALKS_TO)]
        if not runtime:
            return
        services = sorted((n for n in b.nodes.values() if n.component_type == "service" and "first-party" in n.tags),
                          key=lambda n: n.id)
        if not services:
            return
        imports: dict[str, set[str]] = {}
        for e in b.edges.values():
            if e.relationship == REL_IMPORTS and e.direct:
                imports.setdefault(e.source_id, set()).add(e.target_id)
        runs = {e.source_id: e.target_id for e in b.edges.values() if e.relationship == REL_RUNS}
        reach_memo: dict[str, set[str]] = {}

        def reach(sid: str) -> set[str]:
            if sid not in reach_memo:
                start = runs.get(sid)
                if start is not None and b.nodes.get(start) is not None and b.nodes[start].category == CATEGORY_SYMBOL:
                    start = b.nodes[start].parent_id
                seen = {start} if start else set()
                frontier = list(seen)
                for _ in range(MAX_DEPTH):
                    frontier = [t for f in frontier for t in imports.get(f, ()) if t not in seen]
                    seen.update(frontier)
                reach_memo[sid] = seen
            return reach_memo[sid]

        def contains(context: Any, path: str) -> bool:
            return context is not None and (context == "" or path == context or path.startswith(str(context) + "/"))

        for e in runtime:
            module = b.nodes.get(e.source_id)
            if module is None or not module.path:
                continue
            target = e.target_id if e.relationship == REL_TALKS_TO else e.metadata.get("provider_service")
            if not target:
                continue
            owners = [s for s in services if contains(s.metadata.get("build_context"), module.path)]
            running = [s for s in owners if module.id in reach(s.id)]
            for s in running or owners:
                if s.id != target:
                    b.add_edge(s.id, target, e.relationship, analyzer=self.name, confidence=e.confidence,
                               evidence=e.evidence[:1], metadata={"label": e.metadata.get("label", ""),
                                                                   "via": [module.path], "from_code": True})
