"""Repository discovery.

Inspects one :class:`~repoviz.sources.TreeSource` and produces a
:class:`RepositoryProfile` describing what the repository contains: languages,
manifests, workspaces and projects, source/test/docs roots, generated and
vendored code, entry points, containers, deployment and CI definitions, and
existing architecture tooling.  Nothing is assumed about layout, language or
build system; configuration can override every inference.
"""

from __future__ import annotations

import posixpath
import time
from dataclasses import dataclass, field
from typing import Any

from . import classify, globs
from .config import Config
from .manifests import ManifestData, parse_project_file
from .model import Diagnostic
from .sources import TreeSource

PROJECT_MANIFEST_KINDS = {
    "pyproject", "setup.py", "setup.cfg", "pipfile", "package.json", "cargo", "go.mod", "maven", "gradle",
    "msbuild-project", "composer", "gemspec", "gemfile", "pubspec", "mix", "swiftpm", "cmake", "deno", "sbt",
}
# Preference when several manifests share a directory.
_PROJECT_PRIORITY = ["pyproject", "package.json", "cargo", "go.mod", "maven", "gradle", "msbuild-project",
                     "setup.py", "setup.cfg", "composer", "gemspec", "pubspec", "mix", "swiftpm", "deno", "cmake",
                     "pipfile", "gemfile", "sbt"]

DEPENDENCY_TOOL_PACKAGES = {
    "import-linter": "import-linter", "grimp": "grimp", "pydeps": "pydeps", "deptry": "deptry", "tach": "tach",
    "snakefood": "snakefood", "pyan3": "pyan", "dependency-cruiser": "dependency-cruiser", "madge": "madge",
    "eslint-plugin-import": "eslint-plugin-import", "eslint-plugin-boundaries": "eslint-plugin-boundaries",
    "nx": "nx", "@nx/workspace": "nx", "skott": "skott", "com.tngtech.archunit:archunit": "archunit",
    "com.tngtech.archunit:archunit-junit5": "archunit", "cargo-deny": "cargo-deny", "jdepend:jdepend": "jdepend",
}


@dataclass
class RepositoryProfile:
    root: str
    name: str
    source_kind: str
    revision: str
    is_git: bool = False
    branch: str | None = None
    head: str | None = None
    default_branch: str | None = None
    remotes: list[str] = field(default_factory=list)
    shallow: bool = False
    file_count: int = 0
    analyzed_file_count: int = 0
    languages: list[dict[str, Any]] = field(default_factory=list)
    manifests: list[dict[str, Any]] = field(default_factory=list)
    lockfiles: list[dict[str, Any]] = field(default_factory=list)
    workspaces: list[dict[str, Any]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    source_roots: list[dict[str, Any]] = field(default_factory=list)
    test_roots: list[dict[str, Any]] = field(default_factory=list)
    generated: list[dict[str, Any]] = field(default_factory=list)
    vendored: list[dict[str, Any]] = field(default_factory=list)
    docs: list[dict[str, Any]] = field(default_factory=list)
    entry_points: list[dict[str, Any]] = field(default_factory=list)
    containers: list[dict[str, Any]] = field(default_factory=list)
    deployment: list[dict[str, Any]] = field(default_factory=list)
    ci: list[dict[str, Any]] = field(default_factory=list)
    architecture_config: list[dict[str, Any]] = field(default_factory=list)
    dependency_tools: list[dict[str, Any]] = field(default_factory=list)
    submodules: list[str] = field(default_factory=list)
    excluded_count: int = 0
    config_sources: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    duration_ms: float = 0.0
    # Internal data used by analyzers (not serialized).
    manifest_data: dict[str, ManifestData] = field(default_factory=dict, repr=False)
    included_files: list[str] = field(default_factory=list, repr=False)
    generated_paths: set[str] = field(default_factory=set, repr=False)
    vendored_paths: set[str] = field(default_factory=set, repr=False)
    test_paths: set[str] = field(default_factory=set, repr=False)
    file_languages: dict[str, tuple[str | None, str | None]] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        skip = {"manifest_data", "included_files", "generated_paths", "vendored_paths", "test_paths",
                "file_languages", "diagnostics"}
        out = {k: v for k, v in self.__dict__.items() if k not in skip and not k.startswith("_")}
        out["diagnostics"] = [d.to_dict() for d in self.diagnostics]
        out["duration_ms"] = round(self.duration_ms, 1)
        return out

    # -- queries used by analyzers ----------------------------------------------

    def source_root_paths(self, language: str | None = None) -> list[str]:
        return [r["path"] for r in self.source_roots if language is None or r.get("language") in (None, language)]

    def project_for(self, path: str) -> dict[str, Any] | None:
        """Innermost project whose directory contains ``path``."""
        best = None
        for proj in self.projects:
            d = proj["path"]
            if d == "" or path == d or path.startswith(d + "/"):
                if best is None or len(d) > len(best["path"]):
                    best = proj
        return best

    def is_test(self, path: str) -> bool:
        return path in self.test_paths


def _top_dirs(paths: set[str]) -> list[str]:
    """Remove paths nested inside other paths of the set."""
    out: list[str] = []
    for p in sorted(paths, key=lambda s: (s.count("/"), s)):
        if not any(p == q or p.startswith(q + "/") for q in out if q):
            out.append(p)
    return sorted(out)


def discover(source: TreeSource, config: Config, *, root: str = "", name: str = "",
             supported_languages: dict[str, list[str]] | None = None, git_info: dict[str, Any] | None = None,
             ) -> RepositoryProfile:
    started = time.perf_counter()
    supported_languages = supported_languages or {}
    prof = RepositoryProfile(root=root, name=name or posixpath.basename(root.rstrip("/")) or "repository",
                             source_kind=source.kind, revision=source.label,
                             config_sources=list(config.sources))
    if git_info:
        for key in ("is_git", "branch", "head", "default_branch", "remotes", "shallow"):
            if key in git_info:
                setattr(prof, key, git_info[key])
    prof.submodules = list(getattr(source, "submodules", []))

    all_files = source.files()
    prof.file_count = len(all_files)
    dirs = sorted({posixpath.dirname(f) for f in all_files} - {""})
    all_dirs: set[str] = set()
    for d in dirs:
        parts = d.split("/")
        for i in range(1, len(parts) + 1):
            all_dirs.add("/".join(parts[:i]))

    # 1. exclusions ------------------------------------------------------------
    excludes = config.all_excludes
    excluded_dirs = {d for d in all_dirs if globs.match_dir(d, excludes)}

    def under(path: str, dirset: set[str]) -> bool:
        parts = path.split("/")
        return any("/".join(parts[:i]) in dirset for i in range(1, len(parts)))

    candidates = []
    for f in all_files:
        if under(f, excluded_dirs) or globs.match_any(f, excludes):
            continue
        if config.include and not globs.match_any(f, config.include):
            continue
        candidates.append(f)
    prof.excluded_count = len(all_files) - len(candidates)
    file_set = set(candidates)
    cand_dirs: set[str] = set()
    for f in candidates:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            cand_dirs.add("/".join(parts[:i]))

    def exists(path: str) -> bool:
        if path.endswith("/"):
            return path.rstrip("/") in cand_dirs
        return path in file_set

    # 2. manifests (needed to protect declared source roots from "generated" rules)
    for f in candidates:
        if classify.manifest_kind(f) or classify.container_kind(f) in ("dockerfile", "compose") \
                or classify.ci_provider(f) or classify.deployment_kind(f) == "procfile":
            size = source.size(f)
            if size is not None and size > config.max_file_bytes:
                continue
            text = source.read_text(f)
            if text is None:
                continue
            md = parse_project_file(f, text, exists)
            if md is None:
                continue
            prof.manifest_data[f] = md
            for err in md.errors:
                prof.diagnostics.append(Diagnostic("warning", "manifest-parse-error", err, "discovery", f))

    declared_roots: dict[str, str] = {}
    for md in prof.manifest_data.values():
        for r in md.source_roots:
            if r == "" or r in cand_dirs:
                declared_roots.setdefault(r, md.ecosystem)
    if config.source_roots is not None:
        declared_roots = {_norm_dir(r): "configured" for r in config.source_roots}

    # 3. generated / vendored ------------------------------------------------------
    python_packages = {posixpath.dirname(f) for f in candidates if posixpath.basename(f) == "__init__.py"}
    gen_dirs: dict[str, str] = {}
    vend_dirs: dict[str, str] = {}
    for d in sorted(cand_dirs):
        if d in python_packages or d in declared_roots:
            continue
        if config.generated and globs.match_dir(d, config.generated):
            gen_dirs[d] = "configured"
        elif globs.match_dir(d, classify.GENERATED_DIR_PATTERNS):
            gen_dirs[d] = "conventional build/output directory"
        elif globs.match_dir(d, classify.VENDOR_DIR_PATTERNS):
            vend_dirs[d] = "conventional vendored-code directory"
    gen_top = _top_dirs(set(gen_dirs))
    vend_top = _top_dirs(set(vend_dirs))
    for f in candidates:
        if under(f, set(gen_top)):
            prof.generated_paths.add(f)
        elif under(f, set(vend_top)):
            prof.vendored_paths.add(f)
        else:
            reason = classify.generated_reason(f, config.generated)
            if reason and not reason.startswith("directory"):
                prof.generated_paths.add(f)
    prof.generated = [{"path": d, "reason": gen_dirs[d], "kind": "directory",
                       "files": sum(1 for f in prof.generated_paths if f.startswith(d + "/"))} for d in gen_top]
    loose_generated = sorted(f for f in prof.generated_paths if not under(f, set(gen_top)))
    prof.generated += [{"path": f, "reason": classify.generated_reason(f, config.generated) or "", "kind": "file"}
                       for f in loose_generated[:200]]
    prof.vendored = [{"path": d, "reason": vend_dirs[d],
                      "files": sum(1 for f in prof.vendored_paths if f.startswith(d + "/"))} for d in vend_top]

    included = [f for f in candidates
                if (config.include_generated or f not in prof.generated_paths)
                and (config.include_vendored or f not in prof.vendored_paths)]
    prof.included_files = included
    prof.analyzed_file_count = len(included)

    # 4. languages -----------------------------------------------------------------
    lang_stats: dict[str, dict[str, Any]] = {}
    for f in included:
        lang, kind = classify.language_of(f, config.languages)
        prof.file_languages[f] = (lang, kind)
        if not lang:
            continue
        st = lang_stats.setdefault(lang, {"language": lang, "display": classify.display_language(lang),
                                          "kind": kind, "files": 0, "bytes": 0})
        st["files"] += 1
        st["bytes"] += source.size(f) or 0
    for st in lang_stats.values():
        st["analyzers"] = supported_languages.get(st["language"], [])
        st["supported"] = bool(st["analyzers"])
    prof.languages = sorted(lang_stats.values(), key=lambda s: (-s["files"], s["language"]))

    # 5. manifests, projects, workspaces -------------------------------------------------
    for path, md in sorted(prof.manifest_data.items()):
        mk = classify.manifest_kind(path)
        if mk is None:
            continue
        entry = {"path": path, "kind": md.kind, "ecosystem": md.ecosystem, "name": md.name, "version": md.version,
                 "role": md.role, "dependencies": len(md.dependencies), "errors": md.errors}
        if md.metadata.get("parsed") is False:
            entry["parsed"] = False
        (prof.lockfiles if mk.lockfile else prof.manifests).append(entry)

    workspace_members: dict[str, str] = {}
    manifest_dirs = {posixpath.dirname(p) for p in prof.manifest_data}
    for path, md in sorted(prof.manifest_data.items()):
        kind = classify.manifest_kind(path)
        if not md.workspace_members and not (kind and kind.workspace):
            continue
        members: list[str] = []
        for pattern in md.workspace_members:
            if any(c in pattern for c in "*?["):
                matched = [d for d in sorted(cand_dirs) if globs.match_exact(d, "/" + pattern)]
            else:
                matched = [pattern] if pattern in cand_dirs or pattern == "" else []
            for d in matched:
                if any(globs.match_exact(d, "/" + ex) for ex in md.workspace_exclude):
                    continue
                if d in manifest_dirs:
                    members.append(d)
        members = sorted(set(members))
        for m in members:
            workspace_members.setdefault(m, path)
        prof.workspaces.append({"path": path, "kind": md.kind, "ecosystem": md.ecosystem,
                                "patterns": md.workspace_members, "members": members})

    by_dir: dict[str, list[ManifestData]] = {}
    for path, md in prof.manifest_data.items():
        if md.kind in PROJECT_MANIFEST_KINDS:
            by_dir.setdefault(md.dir, []).append(md)
    for d, mds in sorted(by_dir.items()):
        mds.sort(key=lambda m: _PROJECT_PRIORITY.index(m.kind) if m.kind in _PROJECT_PRIORITY else 99)
        primary = mds[0]
        name_ = next((m.name for m in mds if m.name), None) or (posixpath.basename(d) if d else prof.name)
        role = next((m.role for m in mds if m.role), None)
        prof.projects.append({
            "path": d, "name": name_, "ecosystem": primary.ecosystem, "manifest": primary.path,
            "manifests": [m.path for m in mds], "version": next((m.version for m in mds if m.version), None),
            "role": role, "workspace": workspace_members.get(d),
            "entry_points": sum(len(m.entry_points) for m in mds),
        })

    # 6. source roots --------------------------------------------------------------------
    roots: dict[str, dict[str, Any]] = {}
    if config.source_roots is not None:
        for r in config.source_roots:
            r = _norm_dir(r)
            roots[r] = {"path": r, "language": None, "origin": "configured"}
    else:
        for r, eco in declared_roots.items():
            roots[r] = {"path": r, "language": _eco_language(eco), "origin": "manifest"}
        for pkg in sorted(python_packages):
            parent = posixpath.dirname(pkg)
            if parent not in python_packages and not any(pkg == r or pkg.startswith(r + "/") for r in declared_roots
                                                          if r and _eco_language(declared_roots[r]) == "python"):
                if parent not in roots and pkg not in prof.generated_paths:
                    roots[parent] = {"path": parent, "language": "python", "origin": "heuristic: top-level package"}
        for proj in prof.projects:
            if proj["ecosystem"] == "npm":
                src = (proj["path"] + "/src").lstrip("/")
                r = src if src in cand_dirs else proj["path"]
                roots.setdefault(r, {"path": r, "language": "javascript", "origin": "heuristic: package.json"})
            elif proj["ecosystem"] == "go":
                roots.setdefault(proj["path"], {"path": proj["path"], "language": "go", "origin": "go.mod"})
        for conventional in ("src", "lib", "app", "source", "sources"):
            if conventional in cand_dirs and not any(r == conventional or conventional.startswith(r + "/") and r
                                                     for r in roots):
                roots.setdefault(conventional, {"path": conventional, "language": None,
                                                "origin": "heuristic: conventional directory"})
    prof.source_roots = sorted(roots.values(), key=lambda r: r["path"])

    # 7. tests ----------------------------------------------------------------------------
    for f in included:
        if classify.is_test_path(f, config.test_patterns):
            prof.test_paths.add(f)
    if config.test_roots is not None:
        test_dirs = {_norm_dir(r) for r in config.test_roots}
        for f in included:
            if any(f.startswith(t + "/") for t in test_dirs):
                prof.test_paths.add(f)
        test_roots = [{"path": t, "origin": "configured"} for t in sorted(test_dirs)]
    else:
        names = classify.TEST_DIR_NAMES
        tdirs = {d for d in cand_dirs if posixpath.basename(d).lower() in names or d.endswith("src/test")}
        test_roots = [{"path": t, "origin": "heuristic: directory name"} for t in _top_dirs(tdirs)]
    for tr in test_roots:
        tr["files"] = sum(1 for f in prof.test_paths if f.startswith(tr["path"] + "/"))
    loose = sum(1 for f in prof.test_paths if not any(f.startswith(t["path"] + "/") for t in test_roots))
    prof.test_roots = test_roots
    if loose:
        prof.test_roots.append({"path": "(colocated)", "origin": "heuristic: file name", "files": loose})

    # 8. docs -------------------------------------------------------------------------------
    docs: dict[str, str] = {}
    if config.docs_roots is not None:
        docs = {_norm_dir(d): "configured" for d in config.docs_roots}
    else:
        for d in cand_dirs:
            if posixpath.basename(d).lower() in classify.DOC_DIR_NAMES:
                docs[d] = "directory name"
        for f in included:
            base = posixpath.basename(f)
            tool = classify.DOC_TOOL_FILES.get(base)
            if tool and (tool != "sphinx?" or any(p in classify.DOC_DIR_NAMES for p in f.lower().split("/")[:-1])):
                docs.setdefault(posixpath.dirname(f), f"{tool.rstrip('?')} configuration")
        doc_counts: dict[str, list[int]] = {}
        for f in included:
            lang_kind = prof.file_languages.get(f, (None, None))[1]
            parent = posixpath.dirname(f)
            if parent:
                c = doc_counts.setdefault(parent, [0, 0])
                c[0] += 1
                c[1] += lang_kind == "docs"
        for d, (total, n_docs) in doc_counts.items():
            if total >= 4 and n_docs / total >= 0.8 and d not in docs:
                docs[d] = "mostly documentation files"
    prof.docs = [{"path": d, "reason": docs[d], "files": sum(1 for f in included if f.startswith(d + "/") or (d == "" and "/" not in f))}
                 for d in _top_dirs({d for d in docs if d})]
    readmes = [f for f in included if posixpath.basename(f).lower().startswith("readme")]
    if readmes:
        prof.docs.append({"path": "(readme files)", "reason": "README files", "files": len(readmes)})

    # 9. entry points, containers, deployment, CI --------------------------------------------------
    for path, md in sorted(prof.manifest_data.items()):
        proj = prof.project_for(path)
        for ep in md.entry_points:
            prof.entry_points.append({"name": ep.name, "kind": ep.kind, "target": ep.target,
                                      "target_kind": ep.target_kind, "declared_in": path, "line": ep.line,
                                      "project": proj["path"] if proj else None})
    for f in included:
        base = posixpath.basename(f)
        if base == "__main__.py":
            prof.entry_points.append({"name": posixpath.dirname(f) or "(root)", "kind": "python-main-package",
                                      "target": f, "target_kind": "file", "declared_in": f, "line": 1})
        elif base == "main.go":
            prof.entry_points.append({"name": posixpath.dirname(f) or "(root)", "kind": "go-main", "target": f,
                                      "target_kind": "file", "declared_in": f, "line": 1})
        ck = classify.container_kind(f)
        if ck and ck != "dockerignore":
            entry = {"path": f, "kind": ck}
            md = prof.manifest_data.get(f)
            if md is not None:
                if ck == "compose":
                    entry["services"] = sorted((md.metadata.get("services") or {}).keys())
                else:
                    entry["base_images"] = [s["image"] for s in md.metadata.get("stages", [])]
            prof.containers.append(entry)
        dk = classify.deployment_kind(f)
        if dk:
            prof.deployment.append({"path": f, "kind": dk})
        provider = classify.ci_provider(f)
        if provider:
            md = prof.manifest_data.get(f)
            jobs = (md.metadata.get("jobs") if md else None) or {}
            prof.ci.append({"path": f, "provider": provider, "jobs": sorted(jobs) if isinstance(jobs, dict) else jobs})
        tool = classify.architecture_config_tool(f)
        if tool:
            prof.architecture_config.append({"path": f, "tool": tool})
    for path, md in prof.manifest_data.items():
        for tool in md.metadata.get("tool_config", []):
            prof.architecture_config.append({"path": path, "tool": tool, "embedded": True})

    tools: dict[str, set[str]] = {}
    for path, md in prof.manifest_data.items():
        for dep in md.dependencies:
            key = dep.name.lower()
            tool = DEPENDENCY_TOOL_PACKAGES.get(key) or DEPENDENCY_TOOL_PACKAGES.get(key.replace("_", "-"))
            if tool:
                tools.setdefault(tool, set()).add(path)
    for item in prof.architecture_config:
        if item["tool"] in ("import-linter", "dependency-cruiser", "madge", "tach", "pydeps", "deptry", "nx",
                            "packwerk", "cargo-deny", "archunit"):
            tools.setdefault(item["tool"], set()).add(item["path"])
    for f in included:
        base = posixpath.basename(f)
        if base in ("renovate.json", "renovate.json5", ".renovaterc", ".renovaterc.json"):
            tools.setdefault("renovate", set()).add(f)
        if f in (".github/dependabot.yml", ".github/dependabot.yaml"):
            tools.setdefault("dependabot", set()).add(f)
    prof.dependency_tools = [{"tool": t, "evidence": sorted(p)} for t, p in sorted(tools.items())]

    # 10. diagnostics ------------------------------------------------------------------------------
    for lang in prof.languages:
        if lang["kind"] == "programming" and not lang["supported"]:
            prof.diagnostics.append(Diagnostic(
                "info", "unsupported-language",
                f"{lang['files']} {lang['display']} file(s) found; no dependency analyzer supports "
                f"{lang['display']}, so these files are shown as structural nodes only.",
                "discovery", details={"language": lang["language"], "files": lang["files"]}))
    if prof.submodules:
        prof.diagnostics.append(Diagnostic("info", "submodules", f"{len(prof.submodules)} Git submodule(s) are shown "
                                           "as structural nodes; their content is not analyzed.", "discovery",
                                           details={"submodules": prof.submodules}))
    prof.duration_ms = (time.perf_counter() - started) * 1000
    return prof


def _norm_dir(path: str) -> str:
    path = posixpath.normpath(path.strip().replace("\\", "/")).strip("/")
    return "" if path in (".", "") else path


def _eco_language(ecosystem: str) -> str | None:
    return {"python": "python", "npm": "javascript", "deno": "javascript", "go": "go", "cargo": "rust",
            "maven": "java", "gradle": "java", "dotnet": "csharp", "php": "php", "ruby": "ruby"}.get(ecosystem)
