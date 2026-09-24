"""Parsers for package manifests, workspace files, containers and CI configs.

All parsers are pure functions of ``(path, text)``; they never execute code
(``setup.py`` is inspected with :mod:`ast`) and never raise on malformed input
-- problems are reported in :attr:`ManifestData.errors`.
"""

from __future__ import annotations

import ast
import configparser
import json
import posixpath
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable

from . import yamlish
from .classify import ci_provider, container_kind, deployment_kind, manifest_kind

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


@dataclass
class DeclaredDependency:
    name: str
    spec: str = ""
    scope: str = "runtime"
    line: int | None = None
    local_path: str | None = None  # repository-relative directory for path/workspace dependencies
    workspace: bool = False
    raw: str = ""


@dataclass
class EntryPointDecl:
    name: str
    kind: str
    target: str
    target_kind: str = "command"  # python-callable | python-module | file | command
    line: int | None = None


@dataclass
class ManifestData:
    path: str
    kind: str
    ecosystem: str
    name: str | None = None
    version: str | None = None
    lockfile: bool = False
    dependencies: list[DeclaredDependency] = field(default_factory=list)
    workspace_members: list[str] = field(default_factory=list)  # globs relative to the repository root
    workspace_exclude: list[str] = field(default_factory=list)
    entry_points: list[EntryPointDecl] = field(default_factory=list)
    source_roots: list[str] = field(default_factory=list)  # repository-relative
    role: str | None = None  # application | library | service | None
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def dir(self) -> str:
        return posixpath.dirname(self.path)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _join(base_dir: str, rel: str) -> str:
    rel = rel.replace("\\", "/").strip()
    if rel.startswith("/"):
        rel = rel.lstrip("/")
        return posixpath.normpath(rel)
    joined = posixpath.normpath(posixpath.join(base_dir or ".", rel))
    return "" if joined == "." else joined


def find_line(text: str, needle: str, start: int = 0) -> int | None:
    """1-based line of the first occurrence of ``needle`` (whole-token-ish) after ``start``."""
    if not needle:
        return None
    pattern = re.compile(r"(?<![\w@/.-])" + re.escape(needle) + r"(?![\w-])")
    m = pattern.search(text, start)
    if not m:
        idx = text.find(needle, start)
        if idx < 0:
            return None
        return text.count("\n", 0, idx) + 1
    return text.count("\n", 0, m.start()) + 1


def _section_offset(text: str, header_regex: str) -> int:
    m = re.search(header_regex, text, re.M)
    return m.start() if m else 0


_PEP508 = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(\[[^\]]*\])?\s*(.*)$")


def normalize_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pep508(req: str, scope: str, text: str = "", base_dir: str = "", start: int = 0) -> DeclaredDependency | None:
    req = req.strip()
    if not req or req.startswith("#"):
        return None
    m = _PEP508.match(req)
    if not m:
        return None
    name, rest = m.group(1), m.group(3)
    spec = rest.split(";", 1)[0].strip()
    local = None
    if spec.startswith("@"):
        url = spec[1:].strip()
        if url.startswith("file:"):
            path = re.sub(r"^file:(//)?", "", url)
            local = _join(base_dir, path) if not path.startswith("/") else None
    return DeclaredDependency(name=name, spec=spec, scope=scope, line=find_line(text, name, start) if text else None,
                              local_path=local, raw=req)


def load_jsonc(text: str) -> Any:
    """JSON with comments and trailing commas (tsconfig.json, rush.json...)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(ch)
            i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(cleaned)


def command_entry_target(command: str) -> tuple[str, str] | None:
    """Best-effort extraction of what a shell command runs: ``(target, target_kind)``."""
    cmd = command.strip()
    m = re.search(r"\bpython[\d.]*\s+(?:-[a-zA-Z]+\s+)*-m\s+([\w.]+)", cmd)
    if m:
        return m.group(1), "python-module"
    m = re.search(r"\b(?:gunicorn|uvicorn|hypercorn|daphne|waitress-serve|granian)\b.*?\s([\w.]+):([\w.]+)", cmd)
    if m:
        return f"{m.group(1)}:{m.group(2)}", "python-callable"
    m = re.search(r"\bpython[\d.]*\s+(?:-[a-zA-Z]+\s+)*([\w./-]+\.py)\b", cmd)
    if m:
        return m.group(1).removeprefix("./"), "file"
    m = re.search(r"\b(?:node|bun|deno run|ts-node|tsx)\s+(?:--?[\w-]+(?:=\S+)?\s+)*([\w./@-]+\.(?:[cm]?[jt]sx?))\b", cmd)
    if m:
        return m.group(1).removeprefix("./"), "file"
    m = re.search(r"\bgo\s+run\s+([\w./-]+)", cmd)
    if m:
        return m.group(1), "file"
    return None


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------


def parse_pyproject(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="pyproject", ecosystem="python")
    base = md.dir
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        md.errors.append(f"invalid TOML: {exc}")
        return md
    project = data.get("project") or {}
    tool = data.get("tool") or {}
    poetry = tool.get("poetry") or {}
    md.name = project.get("name") or poetry.get("name")
    md.version = str(project.get("version") or poetry.get("version") or "") or None
    deps_off = _section_offset(text, r"^\s*dependencies\s*=")
    for req in project.get("dependencies") or []:
        if isinstance(req, str) and (d := parse_pep508(req, "runtime", text, base, deps_off)):
            md.dependencies.append(d)
    for extra, reqs in (project.get("optional-dependencies") or {}).items():
        off = _section_offset(text, r"^\[project\.optional-dependencies\]")
        for req in reqs or []:
            if isinstance(req, str) and (d := parse_pep508(req, f"optional:{extra}", text, base, off)):
                md.dependencies.append(d)
    for group, reqs in (data.get("dependency-groups") or {}).items():
        off = _section_offset(text, r"^\[dependency-groups\]")
        for req in reqs or []:
            if isinstance(req, str) and (d := parse_pep508(req, "dev" if group == "dev" else f"group:{group}", text, base, off)):
                md.dependencies.append(d)
    for req in (data.get("build-system") or {}).get("requires") or []:
        if isinstance(req, str) and (d := parse_pep508(req, "build", text, base,
                                                        _section_offset(text, r"^\[build-system\]"))):
            md.dependencies.append(d)
    for kind, key in (("console-script", "scripts"), ("gui-script", "gui-scripts")):
        for name, target in (project.get(key) or {}).items():
            md.entry_points.append(EntryPointDecl(name, kind, str(target), "python-callable",
                                                  find_line(text, name, _section_offset(text, rf"^\[project\.{key}\]"))))
    for group, eps in (project.get("entry-points") or {}).items():
        for name, target in (eps or {}).items():
            md.entry_points.append(EntryPointDecl(name, f"entry-point:{group}", str(target), "python-callable",
                                                  find_line(text, name)))
    # Poetry
    for section, scope in (("dependencies", "runtime"), ("dev-dependencies", "dev")):
        off = _section_offset(text, rf"^\[tool\.poetry\.{section}\]")
        for name, spec in (poetry.get(section) or {}).items():
            if name.lower() == "python":
                continue
            md.dependencies.append(_poetry_dep(name, spec, scope, text, base, off))
    for group, gdata in (poetry.get("group") or {}).items():
        off = _section_offset(text, rf"^\[tool\.poetry\.group\.{re.escape(group)}\.dependencies\]")
        for name, spec in ((gdata or {}).get("dependencies") or {}).items():
            md.dependencies.append(_poetry_dep(name, spec, "dev" if group == "dev" else f"group:{group}", text, base, off))
    for name, target in (poetry.get("scripts") or {}).items():
        tgt = target if isinstance(target, str) else (target or {}).get("callable", "")
        md.entry_points.append(EntryPointDecl(name, "console-script", str(tgt), "python-callable", find_line(text, name)))
    for pkg in poetry.get("packages") or []:
        if isinstance(pkg, dict) and pkg.get("from"):
            md.source_roots.append(_join(base, pkg["from"]))
    # PDM dev dependencies
    for group, reqs in ((tool.get("pdm") or {}).get("dev-dependencies") or {}).items():
        for req in reqs or []:
            if isinstance(req, str) and (d := parse_pep508(req, "dev", text, base)):
                md.dependencies.append(d)
    # setuptools / hatch source roots
    setuptools = tool.get("setuptools") or {}
    pkg_dir = setuptools.get("package-dir") or {}
    if isinstance(pkg_dir, dict) and "" in pkg_dir:
        md.source_roots.append(_join(base, pkg_dir[""]))
    packages = setuptools.get("packages")
    if isinstance(packages, dict):
        for where in (packages.get("find") or {}).get("where") or []:
            md.source_roots.append(_join(base, where))
    wheel = (((tool.get("hatch") or {}).get("build") or {}).get("targets") or {}).get("wheel") or {}
    for p in wheel.get("packages") or []:
        parent = posixpath.dirname(p.rstrip("/"))
        md.source_roots.append(_join(base, parent) if parent else base)
    # uv workspaces and sources
    uv = tool.get("uv") or {}
    ws = uv.get("workspace") or {}
    md.workspace_members += [_join(base, m) for m in ws.get("members") or []]
    md.workspace_exclude += [_join(base, m) for m in ws.get("exclude") or []]
    sources = uv.get("sources") or {}
    for dep in md.dependencies:
        src = sources.get(dep.name) or sources.get(normalize_python_name(dep.name))
        if isinstance(src, dict):
            if src.get("workspace"):
                dep.workspace = True
            if src.get("path"):
                dep.local_path = _join(base, src["path"])
    md.source_roots = list(dict.fromkeys(md.source_roots))
    if md.entry_points:
        md.role = "application"
    elif md.name:
        md.role = "library"
    md.metadata["build_backend"] = (data.get("build-system") or {}).get("build-backend")
    for tool_name in ("importlinter", "tach", "deptry", "pydeps", "repoviz"):
        if tool_name in tool:
            md.metadata.setdefault("tool_config", []).append(tool_name)
    return md


def _poetry_dep(name: str, spec: Any, scope: str, text: str, base: str, off: int) -> DeclaredDependency:
    dep = DeclaredDependency(name=name, scope=scope, line=find_line(text, name, off))
    if isinstance(spec, str):
        dep.spec = spec
    elif isinstance(spec, dict):
        dep.spec = str(spec.get("version", ""))
        if spec.get("path"):
            dep.local_path = _join(base, spec["path"])
        if spec.get("optional"):
            dep.scope = "optional"
    return dep


def parse_setup_cfg(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="setup.cfg", ecosystem="python")
    base = md.dir
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read_string(text)
    except configparser.Error as exc:
        md.errors.append(f"invalid setup.cfg: {exc}")
        return md
    if cp.has_section("metadata"):
        md.name = cp.get("metadata", "name", fallback=None)
        md.version = cp.get("metadata", "version", fallback=None)
    if cp.has_section("options"):
        for req in cp.get("options", "install_requires", fallback="").splitlines():
            if d := parse_pep508(req, "runtime", text, base):
                md.dependencies.append(d)
        for req in cp.get("options", "tests_require", fallback="").splitlines():
            if d := parse_pep508(req, "test", text, base):
                md.dependencies.append(d)
        pkg_dir = cp.get("options", "package_dir", fallback="")
        for line in pkg_dir.splitlines() or [pkg_dir]:
            key, _, value = line.partition("=")
            if _ and not key.strip() and value.strip():
                md.source_roots.append(_join(base, value.strip()))
    if cp.has_section("options.packages.find"):
        where = cp.get("options.packages.find", "where", fallback="")
        if where.strip():
            md.source_roots.append(_join(base, where.strip()))
    if cp.has_section("options.extras_require"):
        for extra, reqs in cp.items("options.extras_require"):
            for req in reqs.splitlines():
                if d := parse_pep508(req, f"optional:{extra}", text, base):
                    md.dependencies.append(d)
    if cp.has_section("options.entry_points"):
        for group, body in cp.items("options.entry_points"):
            for line in body.splitlines():
                if "=" in line:
                    name, _, target = line.partition("=")
                    kind = "console-script" if group == "console_scripts" else f"entry-point:{group}"
                    md.entry_points.append(EntryPointDecl(name.strip(), kind, target.strip(), "python-callable",
                                                          find_line(text, name.strip())))
    for section in ("importlinter",):
        if cp.has_section(section):
            md.metadata.setdefault("tool_config", []).append("importlinter")
    md.role = "application" if any(e.kind == "console-script" for e in md.entry_points) else ("library" if md.name else None)
    return md


def parse_setup_py(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    """Inspect ``setup(...)`` keyword literals with :mod:`ast` -- the file is never executed."""
    md = ManifestData(path=path, kind="setup.py", ecosystem="python")
    base = md.dir
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        md.errors.append(f"cannot parse setup.py: {exc}")
        return md
    assigned: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assigned[node.targets[0].id] = node.value
    call = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if fname == "setup":
                call = node
                break
    if call is None:
        md.errors.append("no setup() call found")
        return md
    dynamic: list[str] = []

    def lit(value: ast.AST, key: str) -> Any:
        if isinstance(value, ast.Name) and value.id in assigned:
            value = assigned[value.id]
        try:
            return ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            dynamic.append(key)
            return None

    for kw in call.keywords:
        if kw.arg is None:
            continue
        if kw.arg in ("name", "version"):
            val = lit(kw.value, kw.arg)
            if isinstance(val, str):
                setattr(md, kw.arg, val)
        elif kw.arg in ("install_requires", "tests_require", "setup_requires"):
            val = lit(kw.value, kw.arg)
            scope = {"install_requires": "runtime", "tests_require": "test", "setup_requires": "build"}[kw.arg]
            for req in val or []:
                if isinstance(req, str) and (d := parse_pep508(req, scope, text, base)):
                    md.dependencies.append(d)
        elif kw.arg == "extras_require":
            val = lit(kw.value, kw.arg) or {}
            if isinstance(val, dict):
                for extra, reqs in val.items():
                    for req in reqs or []:
                        if isinstance(req, str) and (d := parse_pep508(req, f"optional:{extra}", text, base)):
                            md.dependencies.append(d)
        elif kw.arg == "entry_points":
            val = lit(kw.value, kw.arg) or {}
            if isinstance(val, dict):
                for group, items in val.items():
                    if isinstance(items, str):
                        items = items.splitlines()
                    for item in items or []:
                        if isinstance(item, str) and "=" in item:
                            name, _, target = item.partition("=")
                            kind = "console-script" if group == "console_scripts" else f"entry-point:{group}"
                            md.entry_points.append(EntryPointDecl(name.strip(), kind, target.strip(),
                                                                  "python-callable", find_line(text, name.strip())))
        elif kw.arg == "package_dir":
            val = lit(kw.value, kw.arg)
            if isinstance(val, dict) and "" in val:
                md.source_roots.append(_join(base, val[""]))
        elif kw.arg == "scripts":
            val = lit(kw.value, kw.arg)
            for script in val or []:
                if isinstance(script, str):
                    md.entry_points.append(EntryPointDecl(posixpath.basename(script), "script",
                                                          _join(base, script), "file", find_line(text, script)))
    if dynamic:
        md.metadata["dynamic_keywords"] = sorted(set(dynamic))
    md.role = "application" if md.entry_points else ("library" if md.name else None)
    return md


def parse_requirements(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="requirements", ecosystem="python")
    base = md.dir
    name = posixpath.basename(path).lower()
    scope = "dev" if any(t in name for t in ("dev", "test", "lint", "doc", "ci")) else "runtime"
    if "constraint" in name:
        scope = "constraint"
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement", "-c ", "--constraint")):
            ref = line.split(None, 1)[1] if " " in line else line.split("=", 1)[-1]
            md.metadata.setdefault("includes", []).append(_join(base, ref.strip()))
            continue
        if line.startswith(("-e ", "--editable")):
            target = line.split(None, 1)[1].strip() if " " in line else ""
            if target.startswith((".", "/")) or target.startswith("file:"):
                target = re.sub(r"^file:(//)?", "", target).split("#", 1)[0]
                md.dependencies.append(DeclaredDependency(posixpath.basename(target.rstrip("/")) or target, "", scope,
                                                          lineno, _join(base, target), raw=raw))
            continue
        if line.startswith("-"):
            continue
        if line.startswith((".", "/")):
            md.dependencies.append(DeclaredDependency(posixpath.basename(line.rstrip("/")), "", scope, lineno,
                                                      _join(base, line), raw=raw))
            continue
        dep = parse_pep508(line, scope, base_dir=base)
        if dep:
            dep.line = lineno
            md.dependencies.append(dep)
    return md


def parse_pipfile(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="pipfile", ecosystem="python")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        md.errors.append(f"invalid TOML: {exc}")
        return md
    for section, scope in (("packages", "runtime"), ("dev-packages", "dev")):
        off = _section_offset(text, rf"^\[{section}\]")
        for name, spec in (data.get(section) or {}).items():
            dep = DeclaredDependency(name, spec if isinstance(spec, str) else str((spec or {}).get("version", "")),
                                     scope, find_line(text, name, off))
            if isinstance(spec, dict) and spec.get("path"):
                dep.local_path = _join(md.dir, spec["path"])
            md.dependencies.append(dep)
    return md


def parse_conda(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="conda", ecosystem="python")
    try:
        data = yamlish.safe_load(text) or {}
    except yamlish.YamlError as exc:
        md.errors.append(f"invalid YAML: {exc}")
        return md
    md.name = data.get("name") if isinstance(data, dict) else None
    for item in (data.get("dependencies") or []) if isinstance(data, dict) else []:
        if isinstance(item, str):
            name = re.split(r"[=<>!\s]", item, 1)[0]
            if name and name != "python":
                md.dependencies.append(DeclaredDependency(name, item[len(name):], "runtime", find_line(text, name)))
        elif isinstance(item, dict):
            for req in item.get("pip") or []:
                if isinstance(req, str) and (d := parse_pep508(req, "runtime", text, md.dir)):
                    md.dependencies.append(d)
    return md


# --------------------------------------------------------------------------
# JavaScript / TypeScript
# --------------------------------------------------------------------------


def parse_package_json(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="package.json", ecosystem="npm")
    base = md.dir
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        md.errors.append(f"invalid JSON: {exc}")
        return md
    if not isinstance(data, dict):
        md.errors.append("package.json is not an object")
        return md
    md.name = data.get("name")
    md.version = data.get("version")
    for section, scope in (("dependencies", "runtime"), ("devDependencies", "dev"), ("peerDependencies", "peer"),
                           ("optionalDependencies", "optional")):
        off = text.find(f'"{section}"')
        for name, spec in (data.get(section) or {}).items():
            spec = str(spec)
            dep = DeclaredDependency(name, spec, scope, find_line(text, f'"{name}"', max(off, 0)))
            if spec.startswith("workspace:"):
                dep.workspace = True
            elif spec.startswith(("file:", "link:", "portal:")):
                dep.local_path = _join(base, spec.split(":", 1)[1])
            md.dependencies.append(dep)
    ws = data.get("workspaces")
    if isinstance(ws, dict):
        ws = ws.get("packages")
    for pattern in ws or []:
        if isinstance(pattern, str):
            (md.workspace_exclude if pattern.startswith("!") else md.workspace_members).append(
                _join(base, pattern.lstrip("!")))
    bin_field = data.get("bin")
    if isinstance(bin_field, str):
        md.entry_points.append(EntryPointDecl(md.name or posixpath.basename(base) or "bin", "bin",
                                              _join(base, bin_field), "file", find_line(text, '"bin"')))
    elif isinstance(bin_field, dict):
        for name, target in bin_field.items():
            md.entry_points.append(EntryPointDecl(name, "bin", _join(base, str(target)), "file",
                                                  find_line(text, f'"{name}"')))
    for key in ("main", "module", "browser"):
        if isinstance(data.get(key), str):
            md.metadata[key] = _join(base, data[key])
            md.entry_points.append(EntryPointDecl(f"{md.name or 'package'} ({key})", key, _join(base, data[key]),
                                                  "file", find_line(text, f'"{key}"')))
    if isinstance(data.get("types") or data.get("typings"), str):
        md.metadata["types"] = _join(base, data.get("types") or data.get("typings"))
    if "exports" in data:
        md.metadata["exports"] = data["exports"]
    scripts = data.get("scripts") or {}
    if isinstance(scripts, dict):
        md.metadata["scripts"] = sorted(scripts)
        for name in ("start", "serve", "dev"):
            if name in scripts:
                tgt = command_entry_target(str(scripts[name]))
                md.entry_points.append(EntryPointDecl(f"npm run {name}", "script",
                                                      _join(base, tgt[0]) if tgt and tgt[1] == "file" else str(scripts[name]),
                                                      tgt[1] if tgt else "command", find_line(text, f'"{name}"')))
    md.metadata["private"] = bool(data.get("private"))
    if data.get("bin") or ("start" in scripts and not data.get("main") and not data.get("exports")):
        md.role = "application"
    elif data.get("main") or data.get("exports") or data.get("module") or data.get("types"):
        md.role = "library"
    for key in ("eslintConfig", "jest", "prettier"):
        if key in data:
            md.metadata.setdefault("tool_config", []).append(key)
    return md


def parse_pnpm_workspace(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="pnpm-workspace", ecosystem="npm")
    try:
        data = yamlish.safe_load(text) or {}
    except yamlish.YamlError as exc:
        md.errors.append(f"invalid YAML: {exc}")
        return md
    for pattern in (data.get("packages") or []) if isinstance(data, dict) else []:
        if isinstance(pattern, str):
            (md.workspace_exclude if pattern.startswith("!") else md.workspace_members).append(
                _join(md.dir, pattern.lstrip("!")))
    return md


def parse_lerna(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="lerna", ecosystem="npm")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        md.errors.append(f"invalid JSON: {exc}")
        return md
    md.workspace_members = [_join(md.dir, p) for p in data.get("packages") or [] if isinstance(p, str)]
    return md


def parse_rush(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="rush", ecosystem="npm")
    try:
        data = load_jsonc(text)
    except (json.JSONDecodeError, ValueError) as exc:
        md.errors.append(f"invalid JSON: {exc}")
        return md
    for proj in data.get("projects") or []:
        if isinstance(proj, dict) and proj.get("projectFolder"):
            md.workspace_members.append(_join(md.dir, proj["projectFolder"]))
    return md


def parse_tsconfig(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="tsconfig", ecosystem="npm")
    try:
        data = load_jsonc(text)
    except (json.JSONDecodeError, ValueError) as exc:
        md.errors.append(f"invalid tsconfig: {exc}")
        return md
    opts = data.get("compilerOptions") or {}
    if opts.get("baseUrl"):
        md.metadata["baseUrl"] = _join(md.dir, opts["baseUrl"])
    if isinstance(opts.get("paths"), dict):
        md.metadata["paths"] = opts["paths"]
    if opts.get("rootDir"):
        md.source_roots.append(_join(md.dir, opts["rootDir"]))
    if isinstance(data.get("extends"), str):
        md.metadata["extends"] = data["extends"]
    refs = [r.get("path") for r in data.get("references") or [] if isinstance(r, dict) and r.get("path")]
    if refs:
        md.metadata["references"] = [_join(md.dir, r) for r in refs]
    return md


def parse_deno(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="deno", ecosystem="deno")
    try:
        data = load_jsonc(text)
    except (json.JSONDecodeError, ValueError) as exc:
        md.errors.append(f"invalid JSON: {exc}")
        return md
    md.name = data.get("name")
    md.version = data.get("version")
    for name, spec in (data.get("imports") or {}).items():
        md.dependencies.append(DeclaredDependency(name, str(spec), "runtime", find_line(text, f'"{name}"')))
    md.workspace_members = [_join(md.dir, p) for p in data.get("workspace") or [] if isinstance(p, str)]
    return md


# --------------------------------------------------------------------------
# Rust, Go
# --------------------------------------------------------------------------


def parse_cargo(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="cargo", ecosystem="cargo")
    base = md.dir
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        md.errors.append(f"invalid TOML: {exc}")
        return md
    pkg = data.get("package") or {}
    md.name = pkg.get("name")
    v = pkg.get("version")
    md.version = v if isinstance(v, str) else None

    def add_table(table: dict[str, Any], scope: str, header: str) -> None:
        off = _section_offset(text, header)
        for name, spec in (table or {}).items():
            dep = DeclaredDependency(name, scope=scope, line=find_line(text, name, off))
            if isinstance(spec, str):
                dep.spec = spec
            elif isinstance(spec, dict):
                dep.spec = str(spec.get("version", ""))
                if spec.get("path"):
                    dep.local_path = _join(base, spec["path"])
                if spec.get("workspace"):
                    dep.workspace = True
                if spec.get("package"):
                    dep.raw = f"package = {spec['package']}"
            md.dependencies.append(dep)

    add_table(data.get("dependencies"), "runtime", r"^\[dependencies\]")
    add_table(data.get("dev-dependencies"), "dev", r"^\[dev-dependencies\]")
    add_table(data.get("build-dependencies"), "build", r"^\[build-dependencies\]")
    for target, tdata in (data.get("target") or {}).items():
        for key, scope in (("dependencies", "runtime"), ("dev-dependencies", "dev")):
            add_table((tdata or {}).get(key), scope, r"^\[target\.")
    ws = data.get("workspace") or {}
    md.workspace_members = [_join(base, m) for m in ws.get("members") or []]
    md.workspace_exclude = [_join(base, m) for m in ws.get("exclude") or []]
    ws_deps = ws.get("dependencies") or {}
    if ws_deps:
        md.metadata["workspace_dependencies"] = {
            k: (_join(base, v["path"]) if isinstance(v, dict) and v.get("path") else None) for k, v in ws_deps.items()}
    for b in data.get("bin") or []:
        if isinstance(b, dict) and b.get("name"):
            md.entry_points.append(EntryPointDecl(b["name"], "cargo-bin", _join(base, b.get("path") or "src/main.rs"),
                                                  "file", find_line(text, b["name"])))
    if md.name and not md.entry_points and exists(_join(base, "src/main.rs")):
        md.entry_points.append(EntryPointDecl(md.name, "cargo-bin", _join(base, "src/main.rs"), "file"))
    if md.name:
        md.source_roots.append(_join(base, "src"))
    md.role = "application" if md.entry_points else ("library" if md.name else None)
    return md


def parse_go_mod(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="go.mod", ecosystem="go")
    base = md.dir
    m = re.search(r"^module\s+(\S+)", text, re.M)
    md.name = m.group(1).strip('"') if m else None
    gv = re.search(r"^go\s+(\S+)", text, re.M)
    if gv:
        md.metadata["go_version"] = gv.group(1)
    in_block = None
    replaces: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("//", 1)[0].strip()
        indirect = "// indirect" in raw
        if not line:
            continue
        if line.endswith("(") and line.split()[0] in ("require", "replace", "exclude", "retract", "tool"):
            in_block = line.split()[0]
            continue
        if line == ")":
            in_block = None
            continue
        directive = in_block
        body = line
        if directive is None:
            first, _, body = line.partition(" ")
            directive = first
        body = body.strip()
        if directive == "require":
            parts = body.split()
            if parts:
                md.dependencies.append(DeclaredDependency(parts[0], parts[1] if len(parts) > 1 else "",
                                                          "indirect" if indirect else "runtime", lineno))
        elif directive == "replace" and "=>" in body:
            old, new = (s.strip() for s in body.split("=>", 1))
            old_mod = old.split()[0]
            new_target = new.split()[0]
            replaces[old_mod] = new_target
    for dep in md.dependencies:
        tgt = replaces.get(dep.name)
        if tgt and tgt.startswith((".", "/")):
            dep.local_path = _join(base, tgt)
    if replaces:
        md.metadata["replace"] = replaces
    md.source_roots.append(base)
    return md


def parse_go_work(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="go.work", ecosystem="go")
    uses: list[str] = []
    for block in re.findall(r"^use\s*\(([^)]*)\)", text, re.M | re.S):
        uses += [l.split("//")[0].strip() for l in block.splitlines() if l.split("//")[0].strip()]
    uses += [m.strip() for m in re.findall(r"^use\s+([^\s(]+)", text, re.M)]
    md.workspace_members = [_join(md.dir, u) for u in uses]
    return md


# --------------------------------------------------------------------------
# JVM
# --------------------------------------------------------------------------


def _parse_xml(text: str) -> ET.Element:
    """Parse a build file; entity declarations (entity-expansion bombs, external entities) are refused."""
    if "<!ENTITY" in text.upper():
        raise ET.ParseError("XML entity declarations are not supported")
    return _strip_ns(ET.fromstring(text.encode("utf-8")))


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parse_pom(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="maven", ecosystem="maven")
    try:
        root = _parse_xml(text)
    except ET.ParseError as exc:
        md.errors.append(f"invalid XML: {exc}")
        return md
    group = root.findtext("groupId") or root.findtext("parent/groupId") or ""
    artifact = root.findtext("artifactId") or ""
    md.name = f"{group}:{artifact}" if group else artifact
    md.version = root.findtext("version") or root.findtext("parent/version")
    md.metadata["artifactId"] = artifact
    md.metadata["groupId"] = group
    packaging = root.findtext("packaging") or "jar"
    md.metadata["packaging"] = packaging
    for mod in root.findall("modules/module"):
        if mod.text:
            md.workspace_members.append(_join(md.dir, mod.text.strip()))
    for dep in root.findall("dependencies/dependency"):
        g, a = dep.findtext("groupId") or "", dep.findtext("artifactId") or ""
        scope = dep.findtext("scope") or "compile"
        md.dependencies.append(DeclaredDependency(f"{g}:{a}", dep.findtext("version") or "",
                                                  "test" if scope == "test" else ("runtime" if scope in ("compile", "runtime") else scope),
                                                  find_line(text, f"<artifactId>{a}</artifactId>")))
    plugins = " ".join(p.findtext("artifactId") or "" for p in root.findall("build/plugins/plugin"))
    if packaging in ("war", "ear") or "spring-boot-maven-plugin" in plugins or "exec-maven-plugin" in plugins:
        md.role = "application"
    elif packaging == "pom":
        md.role = None
    else:
        md.role = "library"
    for rel in ("src/main/java", "src/main/kotlin", "src/main/scala"):
        if exists(_join(md.dir, rel) + "/"):
            md.source_roots.append(_join(md.dir, rel))
    return md


_GRADLE_CONF = (r"implementation|api|compile|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|"
                r"testCompileOnly|annotationProcessor|kapt|ksp|developmentOnly|androidTestImplementation|"
                r"debugImplementation|releaseImplementation|compileOnlyApi|testFixturesImplementation")


def parse_gradle(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="gradle", ecosystem="gradle")
    for m in re.finditer(rf"^\s*({_GRADLE_CONF})\s*\(?\s*(['\"])([^'\"]+)\2", text, re.M):
        conf, coord = m.group(1), m.group(3)
        scope = "test" if conf.lower().startswith(("test", "androidtest")) else "runtime"
        parts = coord.split(":")
        name = ":".join(parts[:2]) if len(parts) >= 2 else coord
        md.dependencies.append(DeclaredDependency(name, parts[2] if len(parts) > 2 else "", scope,
                                                  text.count("\n", 0, m.start()) + 1, raw=coord))
    for m in re.finditer(rf"^\s*({_GRADLE_CONF})\s*\(?\s*(?:platform\s*\()?\s*project\s*\(\s*(?:path\s*[:=]\s*)?['\"]([^'\"]+)['\"]",
                         text, re.M):
        conf, proj = m.group(1), m.group(2)
        md.dependencies.append(DeclaredDependency(proj, "", "test" if conf.startswith("test") else "runtime",
                                                  text.count("\n", 0, m.start()) + 1, workspace=True,
                                                  raw=f"project({proj})"))
    if re.search(r"(id\s*\(?\s*['\"](application|org\.springframework\.boot)['\"]|apply\s+plugin:\s*['\"]application|"
                 r"\bapplication\s*\{|mainClass)", text):
        md.role = "application"
    elif re.search(r"java-library|com\.android\.library|kotlin\(\"jvm\"\)|`java-library`", text):
        md.role = "library"
    for rel in ("src/main/java", "src/main/kotlin"):
        if exists(_join(md.dir, rel) + "/"):
            md.source_roots.append(_join(md.dir, rel))
    return md


def parse_gradle_settings(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="gradle-settings", ecosystem="gradle")
    m = re.search(r"rootProject\.name\s*=\s*['\"]([^'\"]+)['\"]", text)
    md.name = m.group(1) if m else None
    overrides: dict[str, str] = {}
    for m in re.finditer(r"project\(\s*['\"]:?([^'\"]+)['\"]\s*\)\.projectDir\s*=\s*(?:file|new File)\(\s*(?:rootDir\s*,\s*)?['\"]([^'\"]+)['\"]",
                         text):
        overrides[m.group(1)] = m.group(2)
    for m in re.finditer(r"^\s*include\s*\(?([^\n)]*)\)?", text, re.M):
        for proj in re.findall(r"['\"]:?([^'\"]+)['\"]", m.group(1)):
            md.workspace_members.append(_join(md.dir, overrides.get(proj, proj.replace(":", "/"))))
            md.metadata.setdefault("projects", {})[":" + proj.lstrip(":")] = _join(md.dir, overrides.get(proj, proj.replace(":", "/")))
    return md


# --------------------------------------------------------------------------
# .NET
# --------------------------------------------------------------------------


def parse_sln(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="sln", ecosystem="dotnet")
    md.name = posixpath.splitext(posixpath.basename(path))[0]
    projects = {}
    for m in re.finditer(r'^Project\("\{[^}]+\}"\)\s*=\s*"([^"]+)",\s*"([^"]+)"', text, re.M):
        name, rel = m.group(1), m.group(2).replace("\\", "/")
        if rel.lower().endswith(("proj", ".proj")):
            full = _join(md.dir, rel)
            projects[name] = full
            md.workspace_members.append(posixpath.dirname(full))
    md.metadata["projects"] = projects
    return md


def parse_msbuild_project(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="msbuild-project", ecosystem="dotnet")
    try:
        root = _parse_xml(text)
    except ET.ParseError as exc:
        md.errors.append(f"invalid XML: {exc}")
        return md
    md.name = root.findtext(".//AssemblyName") or posixpath.splitext(posixpath.basename(path))[0]
    md.version = root.findtext(".//Version")
    sdk = root.get("Sdk") or ""
    output = (root.findtext(".//OutputType") or "").lower()
    md.metadata["sdk"] = sdk
    md.metadata["target_framework"] = root.findtext(".//TargetFramework") or root.findtext(".//TargetFrameworks")
    for ref in root.iter("PackageReference"):
        name = ref.get("Include") or ref.get("Update")
        if name:
            md.dependencies.append(DeclaredDependency(name, ref.get("Version") or ref.findtext("Version") or "",
                                                      "runtime", find_line(text, f'"{name}"')))
    for ref in root.iter("ProjectReference"):
        inc = ref.get("Include")
        if inc:
            target = _join(md.dir, inc.replace("\\", "/"))
            md.dependencies.append(DeclaredDependency(posixpath.splitext(posixpath.basename(target))[0], "", "runtime",
                                                      find_line(text, inc), local_path=posixpath.dirname(target),
                                                      raw=inc))
    md.role = "application" if output in ("exe", "winexe") or sdk.endswith(".Web") or sdk.endswith(".Worker") else "library"
    if output in ("exe", "winexe"):
        md.entry_points.append(EntryPointDecl(md.name, "dotnet-exe", path, "file"))
    md.source_roots.append(md.dir)
    return md


# --------------------------------------------------------------------------
# Other ecosystems (dependencies only)
# --------------------------------------------------------------------------


def parse_composer(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="composer", ecosystem="php")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        md.errors.append(f"invalid JSON: {exc}")
        return md
    md.name, md.version = data.get("name"), data.get("version")
    for section, scope in (("require", "runtime"), ("require-dev", "dev")):
        for name, spec in (data.get(section) or {}).items():
            if name == "php" or name.startswith("ext-"):
                continue
            md.dependencies.append(DeclaredDependency(name, str(spec), scope, find_line(text, f'"{name}"')))
    for prefix, dirs in ((data.get("autoload") or {}).get("psr-4") or {}).items():
        for d in dirs if isinstance(dirs, list) else [dirs]:
            md.source_roots.append(_join(md.dir, d))
    md.role = "application" if data.get("type") == "project" else "library"
    return md


def parse_gemfile(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="gemfile", ecosystem="ruby")
    group = "runtime"
    for lineno, line in enumerate(text.splitlines(), 1):
        g = re.match(r"\s*group\s+(.+?)\s+do", line)
        if g:
            group = "dev" if re.search(r":(development|test)", g.group(1)) else "group"
        if re.match(r"\s*end\b", line):
            group = "runtime"
        m = re.match(r"""\s*gem\s+['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?(.*)""", line)
        if m:
            dep = DeclaredDependency(m.group(1), m.group(2) or "", group, lineno)
            pm = re.search(r"path:\s*['\"]([^'\"]+)['\"]", m.group(3) or "")
            if pm:
                dep.local_path = _join(md.dir, pm.group(1))
            md.dependencies.append(dep)
    return md


def parse_gemspec(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="gemspec", ecosystem="ruby")
    m = re.search(r"\.name\s*=\s*['\"]([^'\"]+)", text)
    md.name = m.group(1) if m else None
    for m in re.finditer(r"add_(runtime_|development_)?dependency\s*\(?\s*['\"]([^'\"]+)['\"]", text):
        md.dependencies.append(DeclaredDependency(m.group(2), "", "dev" if m.group(1) == "development_" else "runtime",
                                                  text.count("\n", 0, m.start()) + 1))
    md.role = "library"
    return md


def parse_pubspec(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="pubspec", ecosystem="dart")
    try:
        data = yamlish.safe_load(text) or {}
    except yamlish.YamlError as exc:
        md.errors.append(f"invalid YAML: {exc}")
        return md
    if not isinstance(data, dict):
        return md
    md.name, md.version = data.get("name"), data.get("version")
    for section, scope in (("dependencies", "runtime"), ("dev_dependencies", "dev")):
        for name, spec in (data.get(section) or {}).items():
            dep = DeclaredDependency(name, spec if isinstance(spec, str) else "", scope, find_line(text, name))
            if isinstance(spec, dict) and spec.get("path"):
                dep.local_path = _join(md.dir, spec["path"])
            md.dependencies.append(dep)
    return md


def parse_mix(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="mix", ecosystem="elixir")
    m = re.search(r"app:\s*:(\w+)", text)
    md.name = m.group(1) if m else None
    for m in re.finditer(r"\{:(\w+),\s*(?:\"([^\"]*)\"|(in_umbrella:\s*true)|path:\s*\"([^\"]+)\")?", text):
        dep = DeclaredDependency(m.group(1), m.group(2) or "", "runtime", text.count("\n", 0, m.start()) + 1)
        if m.group(3):
            dep.workspace = True
        if m.group(4):
            dep.local_path = _join(md.dir, m.group(4))
        md.dependencies.append(dep)
    return md


def parse_swiftpm(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="swiftpm", ecosystem="swift")
    m = re.search(r"name:\s*\"([^\"]+)\"", text)
    md.name = m.group(1) if m else None
    for m in re.finditer(r"\.package\(\s*(?:name:\s*\"[^\"]*\",\s*)?(url|path):\s*\"([^\"]+)\"", text):
        name = posixpath.basename(m.group(2)).removesuffix(".git")
        dep = DeclaredDependency(name, "", "runtime", text.count("\n", 0, m.start()) + 1)
        if m.group(1) == "path":
            dep.local_path = _join(md.dir, m.group(2))
        md.dependencies.append(dep)
    return md


def parse_cmake(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="cmake", ecosystem="cmake")
    m = re.search(r"project\s*\(\s*([\w.-]+)", text, re.I)
    md.name = m.group(1) if m else None
    for m in re.finditer(r"add_subdirectory\s*\(\s*([^\s)]+)", text, re.I):
        md.workspace_members.append(_join(md.dir, m.group(1).strip('"')))
    targets = {}
    for m in re.finditer(r"add_(executable|library)\s*\(\s*([\w.-]+)", text, re.I):
        targets[m.group(2)] = m.group(1).lower()
        if m.group(1).lower() == "executable":
            md.entry_points.append(EntryPointDecl(m.group(2), "cmake-executable", path, "file",
                                                  text.count("\n", 0, m.start()) + 1))
    if targets:
        md.metadata["targets"] = targets
    for m in re.finditer(r"find_package\s*\(\s*([\w.-]+)", text, re.I):
        md.dependencies.append(DeclaredDependency(m.group(1), "", "runtime", text.count("\n", 0, m.start()) + 1))
    md.role = "application" if "executable" in targets.values() else ("library" if targets else None)
    return md


# --------------------------------------------------------------------------
# Containers, deployment, CI
# --------------------------------------------------------------------------


def _docker_logical_lines(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not buf and (not line.strip() or line.strip().startswith("#")):
            continue
        if not buf:
            start = lineno
        if line.endswith("\\"):
            buf.append(line[:-1])
            continue
        buf.append(line)
        out.append((start, " ".join(s.strip() for s in buf)))
        buf = []
    if buf:
        out.append((start, " ".join(buf)))
    return out


def parse_dockerfile(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="dockerfile", ecosystem="container")
    stages: list[dict[str, Any]] = []
    copies: list[str] = []
    for lineno, line in _docker_logical_lines(text):
        instr, _, args = line.partition(" ")
        instr = instr.upper()
        args = args.strip()
        if instr == "FROM":
            parts = [p for p in args.split() if not p.startswith("--")]
            image = parts[0] if parts else ""
            alias = parts[2] if len(parts) >= 3 and parts[1].lower() == "as" else None
            stages.append({"image": image, "alias": alias, "line": lineno})
            md.dependencies.append(DeclaredDependency(image, "", "base-image", lineno))
        elif instr in ("COPY", "ADD"):
            parts = [p for p in args.split() if not p.startswith("--")]
            if "--from=" in args:
                continue
            if args.startswith("["):
                try:
                    parts = json.loads(args)
                except json.JSONDecodeError:
                    pass
            copies += [p for p in parts[:-1] if not p.startswith(("http://", "https://"))]
        elif instr in ("ENTRYPOINT", "CMD"):
            cmd = args
            if args.startswith("["):
                try:
                    cmd = " ".join(json.loads(args))
                except json.JSONDecodeError:
                    pass
            tgt = command_entry_target(cmd)
            md.entry_points.append(EntryPointDecl(f"{posixpath.basename(path)} {instr.lower()}", f"container-{instr.lower()}",
                                                  tgt[0] if tgt else cmd, tgt[1] if tgt else "command", lineno))
        elif instr == "EXPOSE":
            md.metadata.setdefault("expose", []).extend(args.split())
    md.metadata["stages"] = stages
    md.metadata["copies"] = copies
    md.role = "application"
    return md


def parse_compose(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="compose", ecosystem="container")
    try:
        data = yamlish.safe_load(text) or {}
    except yamlish.YamlError as exc:
        md.errors.append(f"invalid YAML: {exc}")
        return md
    services: dict[str, Any] = {}
    for name, svc in ((data.get("services") or {}) if isinstance(data, dict) else {}).items():
        svc = svc or {}
        build = svc.get("build")
        context = dockerfile = None
        if isinstance(build, str):
            context = _join(md.dir, build)
        elif isinstance(build, dict):
            context = _join(md.dir, build.get("context", "."))
            if build.get("dockerfile"):
                dockerfile = _join(context, build["dockerfile"])
        deps = svc.get("depends_on") or []
        if isinstance(deps, dict):
            deps = list(deps)
        services[name] = {
            "image": svc.get("image"), "build_context": context, "dockerfile": dockerfile,
            "depends_on": [d for d in deps if isinstance(d, str)], "line": find_line(text, f"{name}:"),
            "ports": [str(p) for p in svc.get("ports") or []],
        }
        cmd = svc.get("command")
        if cmd:
            cmd_s = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            tgt = command_entry_target(cmd_s)
            md.entry_points.append(EntryPointDecl(f"{name} (compose)", "compose-command", tgt[0] if tgt else cmd_s,
                                                  tgt[1] if tgt else "command", services[name]["line"]))
    md.metadata["services"] = services
    return md


def parse_procfile(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    md = ManifestData(path=path, kind="procfile", ecosystem="deployment")
    for lineno, line in enumerate(text.splitlines(), 1):
        if ":" in line and not line.strip().startswith("#"):
            name, _, cmd = line.partition(":")
            tgt = command_entry_target(cmd)
            md.entry_points.append(EntryPointDecl(name.strip(), "procfile", tgt[0] if tgt else cmd.strip(),
                                                  tgt[1] if tgt else "command", lineno))
    md.role = "application"
    return md


def parse_ci(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData:
    provider = ci_provider(path) or "ci"
    md = ManifestData(path=path, kind="ci", ecosystem="ci")
    md.metadata["provider"] = provider
    if not path.endswith((".yml", ".yaml")):
        if provider == "jenkins":
            md.metadata["jobs"] = re.findall(r"stage\s*\(\s*['\"]([^'\"]+)", text)
        return md
    try:
        data = yamlish.safe_load(text) or {}
    except yamlish.YamlError as exc:
        md.errors.append(f"invalid YAML: {exc}")
        return md
    if not isinstance(data, dict):
        return md
    jobs: dict[str, list[str]] = {}
    if provider == "github-actions":
        md.name = data.get("name")
        for name, job in (data.get("jobs") or {}).items():
            needs = (job or {}).get("needs") if isinstance(job, dict) else None
            jobs[name] = [needs] if isinstance(needs, str) else list(needs or [])
    elif provider == "gitlab-ci":
        reserved = {"stages", "variables", "include", "default", "workflow", "image", "services", "cache",
                    "before_script", "after_script", "types", "pages"}
        for name, job in data.items():
            if name in reserved or name.startswith(".") or not isinstance(job, dict):
                continue
            needs = job.get("needs") or []
            jobs[name] = [n if isinstance(n, str) else n.get("job", "") for n in needs if n]
        md.metadata["stages"] = data.get("stages") or []
    elif provider == "circleci":
        for name in (data.get("jobs") or {}):
            jobs[name] = []
    md.metadata["jobs"] = jobs
    return md


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

PARSERS: dict[str, Callable[[str, str, Callable[[str], bool]], ManifestData]] = {
    "pyproject": parse_pyproject, "setup.cfg": parse_setup_cfg, "setup.py": parse_setup_py,
    "requirements": parse_requirements, "pipfile": parse_pipfile, "conda": parse_conda,
    "package.json": parse_package_json, "pnpm-workspace": parse_pnpm_workspace, "lerna": parse_lerna,
    "rush": parse_rush, "tsconfig": parse_tsconfig, "deno": parse_deno,
    "cargo": parse_cargo, "go.mod": parse_go_mod, "go.work": parse_go_work,
    "maven": parse_pom, "gradle": parse_gradle, "gradle-settings": parse_gradle_settings,
    "sln": parse_sln, "msbuild-project": parse_msbuild_project,
    "composer": parse_composer, "gemfile": parse_gemfile, "gemspec": parse_gemspec, "pubspec": parse_pubspec,
    "mix": parse_mix, "swiftpm": parse_swiftpm, "cmake": parse_cmake,
}


def parse_project_file(path: str, text: str, exists: Callable[[str], bool]) -> ManifestData | None:
    """Parse any recognised manifest, lock file, container, deployment or CI file."""
    try:
        mk = manifest_kind(path)
        if mk is not None:
            parser = PARSERS.get(mk.kind)
            if parser is None:
                md = ManifestData(path=path, kind=mk.kind, ecosystem=mk.ecosystem, lockfile=mk.lockfile)
                if not mk.lockfile:
                    md.metadata["parsed"] = False
                return md
            md = parser(path, text, exists)
            md.lockfile = mk.lockfile
            return md
        ck = container_kind(path)
        if ck == "dockerfile":
            return parse_dockerfile(path, text, exists)
        if ck == "compose":
            return parse_compose(path, text, exists)
        if ci_provider(path):
            return parse_ci(path, text, exists)
        dk = deployment_kind(path)
        if dk == "procfile":
            return parse_procfile(path, text, exists)
    except Exception as exc:  # defensive: a parser bug must never abort the analysis
        return ManifestData(path=path, kind="unknown", ecosystem="unknown", errors=[f"parser failed: {exc!r}"])
    return None
