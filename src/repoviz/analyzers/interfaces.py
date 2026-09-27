"""Cross-service contracts in the graph: HTTP routes, background tasks, and who calls them (interfaces.py).

* ``http-route`` and ``task`` nodes (category ``symbol``, under the module that defines them).  A FastAPI / Flask
  route's path combines its decorator with its router's prefix and the prefix it is mounted with
  (``include_router`` / ``register_blueprint``, resolved through the module's imports); Express routes combine
  with ``app.use('/prefix', router)``.
* ``calls-http`` (module → route), ``enqueues`` (module → task) and ``reads-env`` (module → the Compose service
  or file that declares the variable) edges, each with a ``confidence``.
* The consumers themselves, matched or not, stay on the module node (``metadata.http_calls``, ``enqueues``,
  ``env_reads``), and the declared environment variables on the repository node (``metadata.env_declared``),
  so a review can tell what a change broke.

Everything is read from text; nothing is run.
"""

from __future__ import annotations

import posixpath
from typing import Any

from .. import interfaces as itf
from ..ids import stable_hash
from ..model import CATEGORY_SYMBOL, ComponentNode, SourceEvidence
from .base import CAP_DEPENDENCIES, CAP_EVIDENCE, CAP_SYMBOLS, AnalysisContext, Analyzer, Detection, SnapshotBuilder

REL_CALLS_HTTP = "calls-http"
REL_ENQUEUES = "enqueues"
REL_READS_ENV = "reads-env"
MAX_TEXT = 1_000_000
MAX_PER_MODULE = 50  # consumers kept on a module node, per kind
MAX_DECLARED = 1000
MAX_MATCHES = 3  # routes one call may be linked to
MAX_DEPTH = 6  # nested router mounts


def _absolute(module: str | None, level: int, qual: str, is_package: bool) -> str:
    if not level:
        return module or ""
    parts = qual.split(".") if is_package else qual.split(".")[:-1]
    parts = parts[:len(parts) - (level - 1)] if level > 1 else parts
    return ".".join(parts + ([module] if module else []))


class InterfacesAnalyzer(Analyzer):
    name = "interfaces"
    version = "1"
    capabilities = (CAP_SYMBOLS, CAP_DEPENDENCIES, CAP_EVIDENCE)

    def detect(self, ctx: AnalysisContext) -> Detection:
        langs = {lang["language"] for lang in ctx.profile.languages if lang.get("supported")}
        ok = bool(langs & {"python", "javascript", "typescript"})
        return Detection(ok, "HTTP routes, tasks and environment variables across services" if ok
                         else "no Python or JavaScript")

    def _extract(self, ctx: AnalysisContext, path: str, lang: str) -> dict[str, Any] | None:
        text = ctx.text(path)
        if text is None or len(text) > MAX_TEXT:
            return None
        if not (itf.python_hint(text) if lang == "python" else itf.js_hint(text)):
            return None
        digest = ctx.source.content_hash(path) or stable_hash(text)
        parse = itf.python_interfaces if lang == "python" else itf.js_interfaces
        return ctx.cached(("interfaces", self.version, lang, digest), lambda: parse(text))

    def discover_calls(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        py_modules = ctx.shared.get("python.modules") or {}
        by_qual = ctx.shared.get("python.by_qual") or {}
        found: dict[str, tuple[str, dict[str, Any]]] = {}
        for path in ctx.files("python"):
            info = self._extract(ctx, path, "python")
            if info:
                found[path] = ("python", info)
        for lang in ("javascript", "typescript"):
            for path in ctx.files(lang):
                info = self._extract(ctx, path, lang)
                if info:
                    found[path] = ("js", info)
        routes = self._routes(b, found, py_modules, by_qual, set(ctx.profile.included_files))
        tasks = self._tasks(b, found, py_modules)
        declared = self._declarations(ctx, b)
        for path, (lang, info) in found.items():
            node = b.nodes.get(b.file_id(path))
            if node is None:
                continue
            self._consumers(b, node, path, info, routes, tasks, declared, py_modules, by_qual)

    # -- providers ------------------------------------------------------------------------------------------

    def _alias(self, mod: Any, name: str) -> str | None:
        """What a name in a Python module refers to (absolute dotted), from its imports."""
        head, _, rest = name.partition(".")
        for imp in mod.info.imports:
            if imp.kind == "import":
                for full, asname in imp.names:
                    if (asname or full.split(".")[0]) == head:
                        target = full if asname else full.split(".")[0]
                        return ".".join(x for x in (target, rest) if x)
            elif imp.kind == "from":
                base = _absolute(imp.module, imp.level, mod.qualname, mod.is_package)
                for full, asname in imp.names:
                    if (asname or full) == head:
                        return ".".join(x for x in (base, full, rest) if x)
        return None

    def _split(self, dotted: str, by_qual: dict[str, list[str]]) -> tuple[str, str] | None:
        """``pkg.mod.router`` → (the module's path, ``router``)."""
        parts = dotted.split(".")
        for i in range(len(parts) - 1, 0, -1):
            paths = by_qual.get(".".join(parts[:i]))
            if paths and len(parts) - i == 1:
                return paths[0], parts[i]
        return None

    def _routes(self, b: SnapshotBuilder, found: dict[str, tuple[str, dict[str, Any]]], py_modules: dict[str, Any],
                by_qual: dict[str, list[str]], files: set[str]) -> list[dict[str, Any]]:
        routers: dict[tuple[str, str], dict[str, Any]] = {}
        mounts: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
        for path, (lang, info) in found.items():
            if lang == "python":
                for var, r in info["routers"].items():
                    routers[(path, var)] = r
                mod = py_modules.get(path)
                for inc in info["includes"]:
                    ref = inc["router"]
                    target = (path, ref) if "." not in ref and (path, ref) in routers or ref in info["routers"] else None
                    if target is None and mod is not None:
                        full = self._alias(mod, ref)
                        target = self._split(full, by_qual) if full else None
                    if target:
                        mounts.setdefault(target, []).append((path, inc["owner"], inc["prefix"]))
            else:
                for var in info["routers"]:
                    routers[(path, var)] = {"kind": "Router", "prefix": ""}
                for var in info.get("apps") or []:
                    routers[(path, var)] = {"kind": "express", "prefix": "", "app": True}
                for m in info["mounts"]:
                    router = m["router"]
                    if (path, router) in routers:
                        mounts.setdefault((path, router), []).append((path, m["owner"], m["prefix"]))
                        continue
                    spec = info["imports"].get(router)
                    other = itf.resolve_js(spec, path, files) if spec else None
                    if other and other in found:  # mounting a module: every router it defines
                        for var in found[other][1].get("routers") or ["router"]:
                            mounts.setdefault((other, var), []).append((path, m["owner"], m["prefix"]))

        def prefixes(key: tuple[str, str], depth: int = 0, seen: frozenset = frozenset()) -> list[tuple[str, bool]]:
            """(prefix, mounted) for a router or app variable of a file."""
            r = routers.get(key)
            if r is None or r.get("app") or depth > MAX_DEPTH or key in seen:
                return [("", r is not None and bool(r.get("app")))]
            own = r.get("prefix") or ""
            out = []
            for includer, owner, prefix in mounts.get(key, []):
                # Flask: a url_prefix given when registering replaces the blueprint's own
                mine = prefix if r.get("kind") == "Blueprint" and prefix else itf.join_paths(prefix, own)
                for pre, mounted in prefixes((includer, owner), depth + 1, seen | {key}):
                    out.append((itf.join_paths(pre, mine), mounted))
            return out or [(own, False)]

        routes: list[dict[str, Any]] = []
        for path, (lang, info) in found.items():
            module = b.nodes.get(b.file_id(path))
            if module is None:
                continue
            for r in info["routes"]:
                for pre, mounted in prefixes((path, r["owner"]))[:4]:
                    template = itf.normalize_path(itf.join_paths(pre, r["path"])
                                                  if not r["path"].startswith(("http:", "https:")) else r["path"])
                    if not template:
                        continue
                    display = itf.join_paths(pre, r["path"]) if pre else r["path"]
                    for method in r["methods"][:4]:
                        key = f"route:{path}:{method} {template}"
                        confidence = 0.85 if mounted or (path, r["owner"]) not in routers else 0.7
                        node = b.add_node(ComponentNode(
                            id=b.id_for("route", key), name=f"{method} {display}", qualified_name=f"{method} {display}",
                            component_type="http-route", category=CATEGORY_SYMBOL, language=module.language,
                            path=path, parent_id=module.id, analyzer=self.name, key=key,
                            fingerprint=stable_hash("route", method, template, r["handler"]), start_line=r["line"],
                            tags=["api"], metadata={"method": method, "path": display, "template": template,
                                                    "handler": r["handler"], "confidence": confidence,
                                                    "mounted": mounted}))
                        routes.append({"id": node.id, "method": method, "template": template, "path": path,
                                       "confidence": confidence})
        return routes

    def _tasks(self, b: SnapshotBuilder, found: dict[str, tuple[str, dict[str, Any]]],
               py_modules: dict[str, Any]) -> list[dict[str, Any]]:
        tasks = []
        for path, (lang, info) in found.items():
            module = b.nodes.get(b.file_id(path))
            mod = py_modules.get(path)
            if lang != "python" or module is None:
                continue
            for t in info["tasks"]:
                qual = f"{mod.qualname}.{t['func']}" if mod else t["func"]
                name = t["name"] or qual
                key = f"task:{name}"
                meta = {k: t[k] for k in ("positional", "required", "kwonly", "var_positional", "var_keyword",
                                          "signature", "bind")}
                node = b.add_node(ComponentNode(
                    id=b.id_for("task", key), name=name, qualified_name=name, component_type="task", category=CATEGORY_SYMBOL,
                    language="python", path=path, parent_id=module.id, analyzer=self.name, key=key,
                    fingerprint=stable_hash("task", name, t["signature"], ",".join(t["required"])), start_line=t["line"],
                    tags=["api"], metadata={**meta, "func": qual}))
                tasks.append({"id": node.id, "name": name, "func": qual, "short": t["func"].split(".")[-1], **meta})
        return tasks

    def _declarations(self, ctx: AnalysisContext, b: SnapshotBuilder) -> dict[str, list[dict[str, Any]]]:
        """Environment variables the deployment declares: Compose services, env files, Dockerfiles, Kubernetes."""
        declared: dict[str, list[dict[str, Any]]] = {}

        def add(name: str, node_id: str | None, where: str, line: int | None = None) -> None:
            if len(declared) < MAX_DECLARED or name in declared:
                lst = declared.setdefault(name, [])
                if len(lst) < 5 and not any(x["where"] == where for x in lst):
                    lst.append({"where": where, "line": line, "node": node_id})

        for node in list(b.nodes.values()):
            if node.component_type == "service":
                for k in node.metadata.get("env_keys") or []:
                    add(k, node.id, f"{node.path or '.'} (service {node.name})" if node.path else f"service {node.name}")
        for path in ctx.profile.included_files:
            base = posixpath.basename(path)
            if not (itf.is_env_file(path) or base.startswith("Dockerfile") or base.endswith((".dockerfile", ".yaml", ".yml"))):
                continue
            text = ctx.text(path)
            if not text:
                continue
            for name, line in itf.env_declarations(path, text):
                add(name, b.path_node_id(path) or b.file_id(path), path, line)
        root = b.nodes.get(b.root_id) if b.root_id else None
        if root is not None and declared:
            root.metadata["env_declared"] = {k: [x["where"] + (f":{x['line']}" if x["line"] else "") for x in v]
                                             for k, v in sorted(declared.items())}
        return declared

    # -- consumers ------------------------------------------------------------------------------------------

    def _consumers(self, b: SnapshotBuilder, node: ComponentNode, path: str, info: dict[str, Any],
                   routes: list[dict[str, Any]], tasks: list[dict[str, Any]],
                   declared: dict[str, list[dict[str, Any]]], py_modules: dict[str, Any],
                   by_qual: dict[str, list[str]]) -> None:
        calls = info.get("http_calls") or []
        if calls:
            node.metadata["http_calls"] = [{k: c[k] for k in ("method", "template", "line", "url", "confidence")}
                                           for c in calls[:MAX_PER_MODULE]]
        for c in calls:
            hits = [r for r in routes if itf.method_match(c["method"], r["method"]) and itf.match(c["template"], r["template"])]
            for r in hits[:MAX_MATCHES]:
                if r["path"] == path:
                    continue
                conf = round(c["confidence"] * (1 if len(hits) == 1 else 0.8), 2)
                b.add_edge(node.id, r["id"], REL_CALLS_HTTP, analyzer=self.name, confidence=conf,
                           evidence=[SourceEvidence(path, c["line"], c["line"], "http call", self.name, c["url"])],
                           metadata={"label": f"{c['method'] or 'ANY'} {c['template']}"})
        enq = info.get("enqueues") or []
        if enq:
            mod = py_modules.get(path)
            kept = []
            for e in enq[:MAX_PER_MODULE]:
                task = self._task_of(e, tasks, mod, by_qual)
                kept.append({**e, "task": task["name"] if task else None})
                if task:
                    b.add_edge(node.id, task["id"], REL_ENQUEUES, analyzer=self.name, confidence=0.8,
                               evidence=[SourceEvidence(path, e["line"], e["line"], e["kind"], self.name, task["name"])],
                               metadata={"label": task["name"]})
            node.metadata["enqueues"] = kept
        reads = info.get("env_reads") or []
        if reads:
            node.metadata["env_reads"] = reads[:MAX_PER_MODULE * 2]
            by_target: dict[str, list[str]] = {}
            for r in reads:
                for d in declared.get(r["name"], [])[:2]:
                    if d["node"] and d["node"] != node.id:
                        names = by_target.setdefault(d["node"], [])
                        if r["name"] not in names:
                            names.append(r["name"])
            for target, names in by_target.items():
                if target in b.nodes:
                    b.add_edge(node.id, target, REL_READS_ENV, analyzer=self.name, confidence=0.9,
                               evidence=[SourceEvidence(path, None, None, "environment", self.name, ", ".join(names[:5]))],
                               metadata={"label": ", ".join(names[:3]) + (" …" if len(names) > 3 else ""),
                                         "env_keys": names[:20]})

    def _task_of(self, e: dict[str, Any], tasks: list[dict[str, Any]], mod: Any,
                 by_qual: dict[str, list[str]]) -> dict[str, Any] | None:
        if e.get("name"):
            return next((t for t in tasks if t["name"] == e["name"]), None)
        target = e.get("target") or ""
        full = self._alias(mod, target) if mod is not None else None
        if full:
            hit = [t for t in tasks if t["func"] == full]
            if len(hit) == 1:
                return hit[0]
        if mod is not None and "." not in target:  # defined in the same module
            hit = [t for t in tasks if t["func"] == f"{mod.qualname}.{target}"]
            if hit:
                return hit[0]
        short = target.split(".")[-1]
        hit = [t for t in tasks if t["short"] == short]
        return hit[0] if len(hit) == 1 else None
