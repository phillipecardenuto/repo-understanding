"""Manifest dependency analyzer (all ecosystems).

Turns the manifests found by discovery into:

* project components (one per directory holding a project manifest), marked
  as workspace members when a workspace file lists them, with role
  (application / library) and version;
* ``depends-on`` edges between internal projects (path dependencies,
  ``workspace:`` protocols, Gradle ``project(':x')``, .NET project references,
  names matching another project of the repository) and to external packages,
  with the manifest line as evidence and the declared scope (runtime, dev,
  optional, peer, build, test...);
* container images, CI pipelines and Docker Compose services: one service per
  name across a directory's Compose variants (see ``services.py``), first-party
  or infrastructure, with ``builds`` edges to the code they build, ``runs``
  edges to what their command runs, and ``starts-after``, ``talks-to`` and
  ``shares-volume`` edges between services;
* entry-point nodes (console scripts, ``bin`` entries, Cargo binaries, Procfile
  processes, container commands...) that the call-flow analyzer links to the
  code they invoke.
"""

from __future__ import annotations

import posixpath
from typing import Any

from ..manifests import DeclaredDependency, ManifestData, normalize_python_name
from ..model import CATEGORY_COMPONENT, REL_BUILDS, REL_DEPENDS_ON, REL_RUNS, ComponentNode, SourceEvidence
from ..services import compose_services, service_entry_points
from .base import (
    CAP_COMPONENTS,
    CAP_DEPENDENCIES,
    CAP_DIAGNOSTICS,
    CAP_ENTRY_POINTS,
    CAP_EVIDENCE,
    AnalysisContext,
    Analyzer,
    Detection,
    SnapshotBuilder,
)
from .callflow import EntryTarget, get_index


def _norm(ecosystem: str, name: str) -> str:
    if ecosystem == "python":
        return normalize_python_name(name)
    if ecosystem in ("npm", "cargo", "go", "maven", "gradle"):
        return name.lower() if ecosystem != "go" else name
    return name.lower()


class ManifestAnalyzer(Analyzer):
    name = "manifest"
    version = "2"
    capabilities = (CAP_COMPONENTS, CAP_DEPENDENCIES, CAP_ENTRY_POINTS, CAP_EVIDENCE, CAP_DIAGNOSTICS)

    def detect(self, ctx: AnalysisContext) -> Detection:
        n = len(ctx.profile.manifest_data)
        return Detection(bool(n), f"{n} manifest/container/CI file(s)" if n else "no manifests found")

    def _project_node(self, b: SnapshotBuilder, path: str) -> str:
        return b.ensure_dir(path, self.name) if path else b.root_id  # type: ignore[return-value]

    def discover_components(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        prof = ctx.profile
        for proj in prof.projects:
            ident = self._project_node(b, proj["path"])
            node = b.nodes[ident]
            tags = ["project", "component"]
            if proj.get("role"):
                tags.append(proj["role"])
            ctype = "workspace-member" if proj.get("workspace") else "project"
            qualified = proj["name"] or node.qualified_name
            if "submodule" in node.tags:  # a submodule with a manifest at its root: still a submodule (tagged
                ctype, qualified = "submodule", node.qualified_name  # project), named by its path like the others
            b.add_node(ComponentNode(
                id=ident, name=node.name, qualified_name=qualified, component_type=ctype,
                path=proj["path"], analyzer=self.name, key=node.key, tags=tags,
                metadata={"ecosystem": proj["ecosystem"], "project_name": proj["name"], "version": proj.get("version"),
                          "manifest": proj["manifest"], "manifests": proj["manifests"], "role": proj.get("role"),
                          "workspace": proj.get("workspace"),
                          "qualified_name_authoritative": bool(proj["path"]) and ctype != "submodule"}))
        for ws in prof.workspaces:
            d = posixpath.dirname(ws["path"])
            ident = self._project_node(b, d)
            node = b.nodes[ident]
            if "workspace" not in node.tags:
                node.tags.append("workspace")
            node.metadata.setdefault("workspace_files", []).append(ws["path"])
            node.metadata["workspace_members"] = sorted(set(node.metadata.get("workspace_members", []) + ws["members"]))

        for path, md in sorted(prof.manifest_data.items()):
            if md.kind == "dockerfile":
                self._dockerfile(ctx, b, md)
            elif md.kind == "compose":
                node = b.ensure_file(md.path, self.name, component_type="compose")
                node.metadata["compose_variant"] = md.metadata.get("variant")
            elif md.kind == "ci":
                node = b.ensure_file(path, self.name, component_type="ci-pipeline")
                node.metadata["ci_provider"] = md.metadata.get("provider")
                jobs = md.metadata.get("jobs") or {}
                node.metadata["jobs"] = sorted(jobs) if isinstance(jobs, dict) else jobs
                if isinstance(jobs, dict):
                    node.metadata["job_needs"] = {k: v for k, v in jobs.items() if v}
            elif md.lockfile:
                node = b.ensure_file(path, self.name)
                if "lockfile" not in node.tags:
                    node.tags.append("lockfile")
            else:
                node = b.ensure_file(path, self.name)
                if "manifest" not in node.tags:
                    node.tags.append("manifest")
                node.metadata["manifest_kind"] = md.kind
                if md.metadata.get("parsed") is False:
                    node.metadata["dependency_details"] = "manifest recognised but not parsed"
        self._services(ctx, b)

    def _dockerfile(self, ctx: AnalysisContext, b: SnapshotBuilder, md: ManifestData) -> None:
        node = b.ensure_file(md.path, self.name, component_type="container")
        node.component_type = "container"
        stages = md.metadata.get("stages", [])
        node.metadata["base_images"] = [s["image"] for s in stages]
        if md.metadata.get("expose"):
            node.metadata["expose"] = md.metadata["expose"]
        aliases = {s["alias"] for s in stages if s.get("alias")}
        for stage in stages:
            image = stage["image"]
            if not image or image in aliases or image.lower() == "scratch" or "$" in image:
                continue
            ext = self._external(b, "container-image", image.split("@")[0], image)
            b.add_edge(node.id, ext, REL_DEPENDS_ON, analyzer=self.name,
                       evidence=[self.evidence(ctx, md.path, stage["line"], stage["line"], "FROM")],
                       metadata={"scope": "base-image"})
        context = md.dir
        for src in md.metadata.get("copies", []):
            src = src.strip("\"'")
            if src in (".", "./") or "*" in src or "$" in src:
                continue
            for base in (context, ""):
                cand = posixpath.normpath(posixpath.join(base, src)) if base else posixpath.normpath(src)
                target = b.path_node_id(cand)
                if target and target != node.id:
                    b.add_edge(node.id, target, REL_BUILDS, analyzer=self.name, confidence=0.8,
                               evidence=[SourceEvidence(md.path, None, None, "COPY", self.name, f"COPY {src}")])
                    break

    def _services(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        """One node per Compose service (all its variants), its code, and how services relate."""
        analyzed = [i["path"] for i in ctx.profile.submodule_info if i.get("analyzed")]
        services, edges = compose_services(ctx.profile.manifest_data, analyzed)
        self._service_ids: dict[str, str] = {}
        for svc in services:
            sid = b.id_for("svc", svc.key)
            self._service_ids[svc.key] = sid
            file_node = b.ensure_file(svc.path, self.name, component_type="compose")
            b.add_node(ComponentNode(
                id=sid, name=svc.name, qualified_name=svc.display_name, component_type="service",
                category=CATEGORY_COMPONENT, path=svc.path, parent_id=file_node.id, analyzer=self.name, key=svc.key,
                tags=["service", "component", "deployment", "first-party" if svc.first_party else "infrastructure"],
                start_line=svc.line, metadata=svc.metadata()))
            ev = [self.evidence(ctx, svc.path, svc.line, svc.line, "service")]
            if svc.build_context is not None:  # the code it builds, and its Dockerfile
                code = b.path_node_id(svc.build_context) if svc.build_context else b.root_id
                dockerfile = b.path_node_id(svc.dockerfile or posixpath.join(svc.build_context, "Dockerfile").lstrip("/"))
                for target in dict.fromkeys(t for t in (code, dockerfile) if t):
                    b.add_edge(sid, target, REL_BUILDS, analyzer=self.name, evidence=ev)
            elif svc.image:
                ext = self._external(b, "container-image", svc.image.split("@")[0], svc.image)
                b.add_edge(sid, ext, REL_DEPENDS_ON, analyzer=self.name, evidence=ev, metadata={"scope": "image"})
        for e in edges:
            meta: dict[str, Any] = {"label": ", ".join(e.labels[:3]) + (" …" if len(e.labels) > 3 else "")}
            if e.keys:
                meta["env_keys"] = e.keys
            b.add_edge(self._service_ids[e.source.key], self._service_ids[e.target.key], e.relationship,
                       analyzer=self.name, evidence=[self.evidence(ctx, e.path, e.line, e.line, e.relationship)],
                       metadata=meta)
        self._services_found = services
        ctx.shared["runtime.providers"] = self._runtime_providers(b, services)

    def _runtime_providers(self, b: SnapshotBuilder, services: list[Any]) -> dict[str, Any]:
        """What code can reach at run time (for the runtime analyzer): images this repository builds, service
        host names, and environment variables the Compose files point at a service."""
        from ..services import image_name

        def nearest(path: str) -> str:
            while path:
                nid = b.path_node_id(path)
                if nid:
                    return nid
                path = posixpath.dirname(path)
            return b.root_id  # type: ignore[return-value]

        images: dict[str, dict[str, Any]] = {}
        hosts: dict[str, str] = {}
        for svc in services:
            sid = self._service_ids[svc.key]
            for alias in svc.aliases:
                hosts.setdefault(alias, sid)
            if svc.first_party and svc.image and svc.build_context is not None:
                target = nearest(svc.build_context) if svc.build_context else sid  # the code it is built from
                images.setdefault(image_name(svc.image) or "", {"target": target, "service": sid, "confidence": 0.9,
                                                               "via": f"{svc.path} ({svc.name})"})
        env_hosts: dict[str, Any] = {}
        for svc in services:
            for link in svc.links if svc.first_party else []:
                if link["host"] in hosts:
                    known = env_hosts.get(link["key"])
                    env_hosts[link["key"]] = link if known is None or known == link else False  # conflicting: none
        images.pop("", None)
        return {"images": images, "hosts": hosts, "env_hosts": {k: v for k, v in env_hosts.items() if v}}

    def _external(self, b: SnapshotBuilder, ecosystem: str, name: str, spec: str = "") -> str:
        key_name = _norm(ecosystem, name)
        ident = b.external_id(ecosystem, key_name)
        if ident not in b.nodes:
            b.add_node(ComponentNode(
                id=ident, name=name, qualified_name=name, component_type="container-image" if ecosystem ==
                "container-image" else "external-package", analyzer=self.name, key=f"external:{ecosystem}:{key_name}",
                tags=["external"], metadata={"ecosystem": ecosystem}))
        node = b.nodes[ident]
        if spec:
            specs = node.metadata.setdefault("specs", [])
            if spec not in specs:
                specs.append(spec)
        return ident

    def discover_entry_points(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        index = get_index(ctx)
        self._service_entry_points(ctx, b, index)
        for path, md in sorted(ctx.profile.manifest_data.items()):
            if not md.entry_points:
                continue
            parent = b.path_node_id(path) or self._project_node(b, md.dir)
            for ep in md.entry_points:
                if ep.kind in ("main", "module", "browser") and ep.target_kind == "file":
                    kind_label = f"package {ep.kind}"
                else:
                    kind_label = ep.kind
                key = f"entry:{path}:{ep.kind}:{ep.name}"
                eid = b.id_for("entry", key)
                b.add_node(ComponentNode(
                    id=eid, name=ep.name, qualified_name=f"{ep.name} [{kind_label}]", component_type="entry-point",
                    category=CATEGORY_COMPONENT, path=path, parent_id=parent, analyzer=self.name, key=key,
                    start_line=ep.line, tags=["entry-point"],
                    metadata={"entry_kind": kind_label, "target": ep.target, "target_kind": ep.target_kind,
                              "declared_in": path}))
                ev = self.evidence(ctx, path, ep.line, ep.line, ep.kind) if ep.line else None
                index.entry_targets.append(EntryTarget(eid, ep.target, ep.target_kind, ev))

    def _service_entry_points(self, ctx: AnalysisContext, b: SnapshotBuilder, index: Any) -> None:
        """Compose: one entry point per first-party service with a command, and a ``runs`` edge from each
        first-party service to what it runs (its command, or its Dockerfile's CMD / ENTRYPOINT)."""
        services = getattr(self, "_services_found", [])
        ids = getattr(self, "_service_ids", {})
        by_name = {(s.dir, s.name): s for s in services}
        for ep in service_entry_points(services):
            svc = by_name[(posixpath.dirname(ep["declared_in"]), ep["service"])]
            key = f"entry:{svc.key}"
            eid = b.id_for("entry", key)
            b.add_node(ComponentNode(
                id=eid, name=ep["name"], qualified_name=f"{ep['name']} [compose-command]", component_type="entry-point",
                category=CATEGORY_COMPONENT, path=svc.path, parent_id=ids[svc.key], analyzer=self.name, key=key,
                start_line=svc.line, tags=["entry-point"],
                metadata={"entry_kind": "compose-command", "target": ep["target"], "target_kind": ep["target_kind"],
                          "declared_in": svc.path, "service": svc.name, "variants": ep["variants"]}))
            ev = self.evidence(ctx, svc.path, svc.line, svc.line, "compose-command") if svc.line else None
            index.entry_targets.append(EntryTarget(eid, ep["target"], ep["target_kind"], ev))
        for svc in services:
            if svc.runs is not None and svc.key in ids:
                path = svc.runs_from if svc.runs_from not in ("command", "entrypoint") else svc.path
                line = svc.line if path == svc.path else None
                ev = self.evidence(ctx, path, line, line, "runs") if line else \
                    SourceEvidence(path, None, None, "runs", self.name, svc.runs[0])
                index.entry_targets.append(EntryTarget(ids[svc.key], svc.runs[0], svc.runs[1], ev, REL_RUNS,
                                                       near=svc.build_context))

    def discover_dependencies(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        prof = ctx.profile
        subs = sorted((i["path"] for i in prof.submodule_info if i.get("analyzed")), key=len, reverse=True)

        def repo_of(d: str) -> str:
            return next((s for s in subs if d == s or d.startswith(s + "/")), "")

        projects_by_dir = {p["path"]: p for p in prof.projects}
        self._jvm_groups: dict[str, str] = {}
        names: dict[tuple[str, str], str] = {}  # (ecosystem family, normalized name) -> project dir
        for path, md in prof.manifest_data.items():
            if md.dir in projects_by_dir and md.name:
                names[(self._family(md.ecosystem), _norm(md.ecosystem, md.name))] = md.dir
                if md.ecosystem == "maven" and md.metadata.get("artifactId"):
                    names[("jvm", md.metadata["artifactId"].lower())] = md.dir
                    self._jvm_groups[md.dir] = md.metadata.get("groupId") or ""
                if md.ecosystem == "gradle":
                    names[("jvm", posixpath.basename(md.dir).lower())] = md.dir
        gradle_projects: dict[str, str] = {}
        go_modules: list[tuple[str, str]] = []
        for md in prof.manifest_data.values():
            if md.kind == "gradle-settings":
                gradle_projects.update(md.metadata.get("projects", {}))
            if md.kind == "go.mod" and md.name:
                go_modules.append((md.name, md.dir))
        for path, md in sorted(prof.manifest_data.items()):
            if md.lockfile or md.dir not in projects_by_dir or md.kind in ("dockerfile", "compose", "ci"):
                continue
            source = self._project_node(b, md.dir)
            for dep in md.dependencies:
                target_dir = self._internal_target(md, dep, names, gradle_projects, go_modules, projects_by_dir)
                ev = [self.evidence(ctx, path, dep.line, dep.line, "manifest-dependency")] if dep.line else \
                    [SourceEvidence(path, None, None, "manifest-dependency", self.name, dep.raw or dep.name)]
                meta: dict[str, Any] = {"scope": dep.scope, "declared_name": dep.name}
                if dep.spec:
                    meta["spec"] = dep.spec
                if target_dir is not None:
                    target = self._project_node(b, target_dir)
                    if target == source:
                        continue
                    meta["internal"] = True
                    if repo_of(md.dir) != repo_of(target_dir):  # into (or out of) an analyzed submodule
                        meta["cross_repository"] = True
                    b.add_edge(source, target, REL_DEPENDS_ON, analyzer=self.name, evidence=ev, metadata=meta)
                    b.stat(self.name, "internal_dependencies")
                elif dep.local_path is not None:
                    b.diagnostic("info", "unresolved-path-dependency", f"Path dependency '{dep.name}' points to "
                                 f"'{dep.local_path}', which is not a project in this snapshot.", self.name, path,
                                 dep.line)
                else:
                    eco = md.ecosystem if md.ecosystem != "gradle" else "maven"
                    ext = self._external(b, eco, dep.name, dep.spec)
                    meta["external"] = True
                    b.add_edge(source, ext, REL_DEPENDS_ON, analyzer=self.name, evidence=ev, metadata=meta,
                               confidence=1.0)
                    b.stat(self.name, "external_dependencies")

    @staticmethod
    def _family(ecosystem: str) -> str:
        return "jvm" if ecosystem in ("maven", "gradle") else ecosystem

    def _internal_target(self, md: ManifestData, dep: DeclaredDependency, names: dict[tuple[str, str], str],
                         gradle_projects: dict[str, str], go_modules: list[tuple[str, str]],
                         projects_by_dir: dict[str, dict[str, Any]]) -> str | None:
        if dep.local_path is not None:
            lp = dep.local_path.rstrip("/")
            if lp in projects_by_dir:
                return lp
        if md.kind == "gradle" and dep.raw.startswith("project("):
            proj = ":" + dep.name.lstrip(":")
            if proj in gradle_projects:
                return gradle_projects[proj]
            cand = dep.name.strip(":").replace(":", "/")
            if cand in projects_by_dir:
                return cand
        if md.ecosystem == "go":
            for mod_name, mod_dir in go_modules:
                if dep.name == mod_name and mod_dir != md.dir:
                    return mod_dir
        fam = self._family(md.ecosystem)
        key = (fam, _norm(md.ecosystem, dep.name))
        if key in names and names[key] != md.dir:
            return names[key]
        if fam == "jvm" and ":" in dep.name:
            group, artifact = dep.name.split(":")[0], dep.name.split(":")[1].lower()
            target = names.get((fam, artifact))
            if target is not None and target != md.dir:
                target_group = self._jvm_groups.get(target, "")
                # Same artifact name is only internal when the groupId matches (or is unknown/inherited).
                if not group or not target_group or group == target_group or group.startswith("${"):
                    return target
        return None
