"""The services of a system, from its Compose files.

A directory's Compose files are variants of one system (``docker-compose.yml``, ``docker-compose.prod.yml``,
``compose.override.yml``…): a service declared in several of them is **one** service with several variants.
A service is *first-party* when this repository builds it (a ``build`` context, or the image of a service that
is built here); otherwise it is *infrastructure*, classified by its image (database, cache, queue…).  What a
first-party service runs comes from its command (``uvicorn app.main:app``, ``celery -A app.worker``…), or from
its Dockerfile's ``CMD`` / ``ENTRYPOINT``.

Services relate through ``depends_on`` (``starts-after``), a shared named volume (``shares-volume``) and an
environment value naming another service (``talks-to``: ``redis://cache:6379``, ``API_HOST=api``).  Only the
names of environment variables are kept, never their values.

Used by discovery (one entry point per service, none for infrastructure) and by the manifest analyzer (service
nodes and their edges), so both see the same services.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass, field
from typing import Any

from .classify import image_kind
from .model import REL_SHARES_VOLUME, REL_STARTS_AFTER, REL_TALKS_TO

#: A volume shared by more services than this is plumbing (logs, sockets…), not a relationship worth drawing.
MAX_VOLUME_PEERS = 8
DIFFERENCE_FIELDS = ("image", "command", "ports", "build_context")


@dataclass
class Service:
    name: str
    dir: str  # the directory of its Compose files (one system per directory)
    path: str  # the Compose file that declares it first (the base variant when there is one)
    line: int | None
    variants: list[dict[str, Any]]  # {"variant", "file", "line"}, the base variant first
    first_party: bool
    kind: str  # "first-party", or the infrastructure kind (database, cache… or "other")
    image: str | None = None
    build_context: str | None = None
    dockerfile: str | None = None
    command: str | None = None
    entrypoint: str | None = None
    ports: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)
    env_keys: list[str] = field(default_factory=list)
    env_files: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)  # name, container_name, hostname
    differences: dict[str, dict[str, Any]] = field(default_factory=dict)  # field -> variant -> value
    runs: tuple[str, str] | None = None  # (target, target_kind) of what it runs
    runs_from: str | None = None  # "command", "entrypoint" or the Dockerfile's path
    links: list[dict[str, Any]] = field(default_factory=list)  # hosts named in the environment

    @property
    def key(self) -> str:
        return f"service:{self.dir}:{self.name}"

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.dir})" if self.dir else self.name

    def metadata(self) -> dict[str, Any]:
        out = {"service_kind": self.kind, "first_party": self.first_party, "compose_dir": self.dir,
               "variants": [v["variant"] for v in self.variants],
               "variant_files": {v["variant"]: v["file"] for v in self.variants},
               "image": self.image, "build_context": self.build_context, "dockerfile": self.dockerfile,
               "command": self.command, "entrypoint": self.entrypoint, "ports": self.ports, "volumes": self.volumes,
               "networks": self.networks, "env_keys": self.env_keys, "env_files": self.env_files,
               "profiles": self.profiles, "differences": self.differences,
               "runs": self.runs[0] if self.runs else None, "runs_from": self.runs_from}
        return {k: v for k, v in out.items() if v not in (None, [], {}, "") or (k == "build_context" and v == "")}


@dataclass
class ServiceEdge:
    source: Service
    target: Service
    relationship: str
    labels: list[str]
    path: str
    line: int | None
    keys: list[str] = field(default_factory=list)  # environment variable names (talks-to)


def _variant_order(md: Any) -> tuple[int, str, str]:
    variant = md.metadata.get("variant", "base")
    return (variant != "base", variant, md.path)


def image_name(image: str | None) -> str | None:
    """``registry/org/app:1.2@sha`` → ``org/app`` (what a build and a pull of the same image share)."""
    if not image:
        return None
    name = image.split("@")[0]
    last = name.rsplit("/", 1)
    if ":" in last[-1]:
        name = name[: len(name) - len(last[-1])] + last[-1].split(":")[0]
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]  # a registry host
    return "/".join(parts).lower()


def compose_services(manifest_data: dict[str, Any], submodules: list[str] | tuple[str, ...] = ()
                     ) -> tuple[list[Service], list[ServiceEdge]]:
    """Services and the edges between them, for every directory holding Compose files (deterministic).

    Compose files inside ``submodules`` (analyzed submodules) describe how that submodule runs on its own; they
    are ignored when the superproject has Compose files of its own, which describe the system being looked at."""
    from .manifests import command_entry_target

    def inside(path: str) -> bool:
        return any(path.startswith(s + "/") for s in submodules)

    composes = [md for md in manifest_data.values() if md.kind == "compose"]
    if any(not inside(md.path) for md in composes):
        composes = [md for md in composes if not inside(md.path)]
    groups: dict[str, list[Any]] = {}
    for md in composes:
        groups.setdefault(md.dir, []).append(md)
    services: list[Service] = []
    edges: list[ServiceEdge] = []
    for d in sorted(groups):
        files = sorted(groups[d], key=_variant_order)
        declared: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
        for md in files:
            for name, svc in (md.metadata.get("services") or {}).items():
                declared.setdefault(name, []).append((md, svc))
        built_images = {image_name(svc.get("image")) for pairs in declared.values() for _md, svc in pairs
                        if svc.get("build_context") is not None and svc.get("image")}
        by_name: dict[str, Service] = {}
        for name in sorted(declared):
            pairs = declared[name]

            def first(key: str) -> Any:
                return next((svc[key] for _md, svc in pairs if svc.get(key)), None)

            def union(key: str) -> list[str]:
                return list(dict.fromkeys(x for _md, svc in pairs for x in svc.get(key) or []))

            image, context = first("image"), next((svc["build_context"] for _md, svc in pairs
                                                   if svc.get("build_context") is not None), None)
            first_party = context is not None or any(image_name(svc.get("image")) in built_images
                                                     for _md, svc in pairs if svc.get("image"))
            s = Service(name=name, dir=d, path=pairs[0][0].path, line=pairs[0][1].get("line"),
                        variants=[{"variant": md.metadata.get("variant", "base"), "file": md.path,
                                   "line": svc.get("line")} for md, svc in pairs],
                        first_party=first_party,
                        kind="first-party" if first_party else (image_kind(image) or "other"),
                        image=image, build_context=context, dockerfile=first("dockerfile"),
                        command=first("command"), entrypoint=first("entrypoint"), ports=union("ports"),
                        volumes=union("volumes"), networks=union("networks"), env_keys=sorted(set(union("env_keys"))),
                        env_files=union("env_files"), profiles=union("profiles"), depends_on=union("depends_on"),
                        aliases=list(dict.fromkeys(a for a in [name, first("container_name"), first("hostname")] if a)),
                        links=[link for _md, svc in pairs for link in svc.get("env_links") or []])
            if len(pairs) > 1:
                for f in DIFFERENCE_FIELDS:
                    values = {md.metadata.get("variant", "base"): svc.get(f) for md, svc in pairs}
                    if len({json.dumps(v, sort_keys=True) for v in values.values()}) > 1:
                        s.differences[f] = values
            if first_party:
                for source, text in (("command", s.command), ("entrypoint", s.entrypoint)):
                    target = command_entry_target(text) if text else None
                    if target:
                        s.runs, s.runs_from = target, source
                        break
                if s.runs is None and s.command is None and context is not None:
                    dockerfile = s.dockerfile or posixpath.join(context, "Dockerfile").lstrip("/")
                    df = manifest_data.get(dockerfile)
                    for ep in reversed(df.entry_points if df is not None else []):
                        if ep.target_kind != "command":
                            s.runs, s.runs_from = (ep.target, ep.target_kind), dockerfile
                            break
            if s.runs and s.runs[1] == "file" and context:  # a script path is relative to the image's code
                s.runs = (posixpath.normpath(posixpath.join(context, s.runs[0])), "file")
            by_name[name] = s
            services.append(s)
        alias_of = {a: s for s in by_name.values() for a in s.aliases}
        for s in by_name.values():
            for dep in s.depends_on:
                if dep in by_name and dep != s.name:
                    edges.append(ServiceEdge(s, by_name[dep], REL_STARTS_AFTER, ["starts after"], s.path, s.line))
            talks: dict[str, ServiceEdge] = {}
            for link in s.links:
                other = alias_of.get(link["host"])
                if other is None or other is s:
                    continue
                scheme, port = link["scheme"], link["port"]
                label = f"{scheme}:{port}" if scheme and port else scheme or (f"port {port}" if port else "")
                e = talks.setdefault(other.name, ServiceEdge(s, other, REL_TALKS_TO, [], s.path, s.line))
                if label and label not in e.labels:
                    e.labels.append(label)
                if link["key"] not in e.keys:
                    e.keys.append(link["key"])
            edges.extend(talks[k] for k in sorted(talks))
        sharing: dict[tuple[str, str], list[str]] = {}
        for volume in sorted({v for s in by_name.values() for v in s.volumes}):
            users = sorted(n for n, s in by_name.items() if volume in s.volumes)
            if 2 <= len(users) <= MAX_VOLUME_PEERS:
                for i, a in enumerate(users):
                    for b in users[i + 1:]:
                        sharing.setdefault((a, b), []).append(volume)
        for (a, b), volumes in sorted(sharing.items()):
            edges.append(ServiceEdge(by_name[a], by_name[b], REL_SHARES_VOLUME, volumes,
                                     by_name[a].path, by_name[a].line))
    return services, edges


def service_entry_points(services: list[Service]) -> list[dict[str, Any]]:
    """One entry point per first-party service with a command (with its variants); infrastructure commands
    (``etcd``, ``minio server``…) are not entry points of this repository."""
    out = []
    for s in services:
        if not s.first_party or s.runs_from not in ("command", "entrypoint"):
            if s.first_party and (s.command or s.entrypoint) and s.runs is None:
                out.append({"name": f"{s.name} (compose)", "kind": "compose-command",
                            "target": s.command or s.entrypoint, "target_kind": "command", "declared_in": s.path,
                            "line": s.line, "service": s.name, "variants": [v["variant"] for v in s.variants]})
            continue
        out.append({"name": f"{s.name} (compose)", "kind": "compose-command", "target": s.runs[0],
                    "target_kind": s.runs[1], "declared_in": s.path, "line": s.line, "service": s.name,
                    "variants": [v["variant"] for v in s.variants]})
    return out
