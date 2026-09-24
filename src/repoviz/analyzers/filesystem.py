"""Generic filesystem analyzer (mandatory).

Works for every repository regardless of language: it creates the repository
root, directory nodes and file nodes for source, configuration, manifest,
container and CI files, and tags them with the roles found by discovery
(tests, docs, generated, vendored...).  Files in languages that no analyzer
supports become structural nodes tagged ``unsupported``.
"""

from __future__ import annotations

from .. import classify
from ..submodules import gitmodules_urls
from ..model import CATEGORY_COMPONENT, ComponentNode
from .base import CAP_COMPONENTS, CAP_CONTAINMENT, CAP_DIAGNOSTICS, AnalysisContext, Analyzer, Detection, SnapshotBuilder

INTERESTING_KINDS = ("programming", "markup")


class FilesystemAnalyzer(Analyzer):
    name = "filesystem"
    version = "1"
    capabilities = (CAP_COMPONENTS, CAP_CONTAINMENT, CAP_DIAGNOSTICS)
    mandatory = True

    def detect(self, ctx: AnalysisContext) -> Detection:
        return Detection(True, "structural analysis applies to every repository")

    def discover_components(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        prof = ctx.profile
        root_id = b.dir_id("")
        b.root_id = root_id
        b.add_node(ComponentNode(
            id=root_id, name=prof.name, qualified_name=prof.name, component_type="repository",
            category=CATEGORY_COMPONENT, path="", analyzer=self.name, key="path:dir:",
            tags=["component"], metadata={"files": prof.file_count, "analyzed_files": prof.analyzed_file_count},
        ))
        # Files and directories are created in the first phase so that every other
        # analyzer enriches existing structural nodes.
        self._structure(ctx, b)

    def _structure(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        prof = ctx.profile
        supported = {l["language"] for l in prof.languages if l.get("supported")}
        dir_counts: dict[str, int] = {}
        for f in prof.included_files:
            parts = f.split("/")[:-1]
            for i in range(0, len(parts) + 1):
                d = "/".join(parts[:i])
                dir_counts[d] = dir_counts.get(d, 0) + 1

        for f in prof.included_files:
            lang, kind = prof.file_languages.get(f, (None, None))
            cfg_kind = classify.config_kind(f)
            if kind not in INTERESTING_KINDS and cfg_kind is None:
                continue
            tags: list[str] = []
            meta: dict[str, object] = {}
            ctype = "file"
            if prof.is_test(f):
                tags.append("test")
            if cfg_kind:
                tags.append(cfg_kind if cfg_kind != "lockfile" else "lockfile")
                if cfg_kind != "lockfile":
                    tags.append("config")
                meta["config_kind"] = cfg_kind
            ci = classify.ci_provider(f)
            if ci:
                ctype = "ci-pipeline"
                meta["ci_provider"] = ci
            ck = classify.container_kind(f)
            if ck in ("dockerfile", "compose"):
                ctype = "container" if ck == "dockerfile" else "compose"
            if kind == "programming" and lang not in supported:
                tags.append("unsupported")
                meta["dependency_details"] = "unavailable"
            size = ctx.source.size(f)
            if size is not None:
                meta["bytes"] = size
            b.ensure_file(f, self.name, component_type=ctype, language=lang,
                          fingerprint=ctx.source.content_hash(f), tags=tags, metadata=meta)
            b.stat(self.name, "file_nodes")

        # Top-level directories are the fallback component boundary.
        for node in list(b.nodes.values()):
            if node.component_type == "directory" and node.path is not None:
                node.metadata["files"] = dir_counts.get(node.path, 0)
                if "/" not in node.path:
                    if "top-level" not in node.tags:
                        node.tags.append("top-level")

        for tr in prof.test_roots:
            self._tag_dir(ctx, b, tr["path"], "test", "test root")
        for d in prof.docs:
            self._tag_dir(ctx, b, d["path"], "docs", d.get("reason", "documentation"), create=True)
        for sr in prof.source_roots:
            self._tag_dir(ctx, b, sr["path"], "source-root", sr.get("origin", ""))
        for g in prof.generated:
            if g.get("kind") == "directory":
                self._tag_dir(ctx, b, g["path"], "generated", g.get("reason", ""), create=True,
                              files=g.get("files"))
        for v in prof.vendored:
            self._tag_dir(ctx, b, v["path"], "vendored", v.get("reason", ""), create=True, files=v.get("files"))
        commits = ctx.source.submodule_commits() if prof.submodules else {}
        urls = gitmodules_urls(ctx.source.read_text(".gitmodules") or "") if prof.submodules else {}
        for sub in prof.submodules:
            # A submodule is a separate repository: it is its own component, pinned to a commit.
            meta: dict[str, object] = {"dependency_details": "unavailable (Git submodule)"}
            if commits.get(sub):
                meta["commit"] = commits[sub]
            if urls.get(sub):
                meta["url"] = urls[sub]
            dirty = ctx.source.submodule_dirty(sub)
            if dirty:
                meta["uncommitted_files"] = len(dirty)
            node = b.ensure_file(sub, self.name, component_type="submodule", tags=["submodule", "component"],
                                 metadata=meta)
            node.category = CATEGORY_COMPONENT

    def _tag_dir(self, ctx: AnalysisContext, b: SnapshotBuilder, path: str, tag: str, reason: str,
                 create: bool = False, files: int | None = None) -> None:
        if path.startswith("(") or path == "":
            return
        ident = b.dir_id(path)
        if ident not in b.nodes:
            if not create:
                return
            b.ensure_dir(path, self.name)
        node = b.nodes[ident]
        if tag not in node.tags:
            node.tags.append(tag)
        node.metadata.setdefault("roles", {})[tag] = reason
        if files is not None:
            node.metadata["files"] = files

