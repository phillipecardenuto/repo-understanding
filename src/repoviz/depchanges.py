"""Third-party dependency changes: what a wave did to declared and resolved packages.

A reviewer should not have to read a 3,000-line ``package-lock.json`` diff to learn that an agent added
``axios``, downgraded ``lodash`` or now installs a package from a Git URL.  For each changed file:

* **Manifests** (``pyproject.toml``, ``package.json``, ``requirements*.txt``, ``Cargo.toml``, ``go.mod``…): the
  declared dependencies before and after, compared by name and scope — added, removed, upgraded, downgraded,
  source changed (a Git URL, a tarball URL, a path outside the repository, an alias or a non-default registry),
  moved between scopes, or loosened to an unbounded spec.  Package indexes added to ``requirements*.txt`` or
  ``pyproject.toml`` count as a source change of the whole file.
* **Lock files** (``package-lock.json``, ``npm-shrinkwrap.json``, ``yarn.lock``, ``pnpm-lock.yaml``,
  ``poetry.lock``, ``uv.lock``, ``pdm.lock``, ``Pipfile.lock``, ``Cargo.lock``, ``go.sum``, ``composer.lock``):
  the resolved version of every package before and after; direct dependencies are listed, the rest counted.

Everything is parsed as text (``json``, ``tomllib``, line patterns), never executed; lock files larger than
:data:`MAX_LOCK_BYTES` are skipped with a note.  Versions are compared best effort, with the standard library.
"""

from __future__ import annotations

import json
import posixpath
import re
import sys
from typing import Any, Callable

from . import classify
from .manifests import DeclaredDependency, normalize_python_name, parse_project_file
from .redact import redact

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

MAX_LOCK_BYTES = 20_000_000
MAX_SPEC = 120
MAX_LISTED = 200  # packages listed per file

#: The manifest a lock file resolves, by lock kind.
LOCK_MANIFEST = {
    "package-lock": "package.json", "npm-shrinkwrap": "package.json", "yarn.lock": "package.json",
    "pnpm-lock": "package.json", "bun.lock": "package.json", "poetry.lock": "pyproject.toml",
    "uv.lock": "pyproject.toml", "pdm.lock": "pyproject.toml", "pipfile.lock": "Pipfile", "cargo.lock": "Cargo.toml",
    "go.sum": "go.mod", "composer.lock": "composer.json", "gemfile.lock": "Gemfile",
}
#: Lock files that go with a manifest, by manifest kind (looked up in its directory, then its parents).
MANIFEST_LOCKS = {
    "package.json": ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock"),
    "pyproject": ("uv.lock", "poetry.lock", "pdm.lock"), "pipfile": ("Pipfile.lock",), "cargo": ("Cargo.lock",),
    "go.mod": ("go.sum",), "composer": ("composer.lock",), "gemfile": ("Gemfile.lock",),
}
RISKY_SOURCES = {"git": "a Git repository", "url": "a URL", "path-outside": "a path outside the repository",
                 "alias": "another package (an npm alias)", "index": "a non-default registry or index"}


# --------------------------------------------------------------------------- versions


def version_key(version: str) -> tuple[int, ...] | None:
    """A comparable key for ``1.2.3``, ``v1.2``, ``2.0.0rc1``…; ``None`` when there is no version."""
    m = re.match(r"\s*[vV=]?\s*(\d+(?:\.\d+)*)(.*)", version or "")
    if not m:
        return None
    nums = [int(x) for x in m.group(1).split(".")][:6]
    nums += [0] * (4 - len(nums))
    pre = 0 if re.match(r"^[-._+]?(a|b|c|rc|alpha|beta|pre|preview|dev)\d*", m.group(2), re.I) else 1
    return (*nums, pre)


_BOUND = re.compile(r"(===|==|>=|<=|~=|!=|\^|~|>|<|=)?\s*v?(\d+(?:\.\d+)*(?:[-.+]?[A-Za-z]+[\w.]*)?)")


def spec_version(spec: str) -> str | None:
    """The version a spec asks for: its highest lower bound or pin (``^4.17.21`` → ``4.17.21``,
    ``>=1.2,<2`` → ``1.2``); an upper bound alone counts when there is nothing else."""
    lower, upper = [], []
    for op, ver in _BOUND.findall(spec or ""):
        (upper if op in ("<", "<=", "!=") else lower).append(ver)
    pool = lower or upper
    keyed = [(version_key(v), v) for v in pool if version_key(v) is not None]
    return max(keyed)[1] if keyed else None


def unbounded(spec: str, ecosystem: str) -> bool:
    """True for a spec that accepts any future version: ``*``, ``latest``, no version, ``>=x`` with no upper bound."""
    s = (spec or "").strip().lower()
    if ecosystem == "go":
        return False
    if s in ("", "*", "latest", "x", "any", "next", "@latest"):
        return True
    if "<" in s or "~=" in s or s.startswith(("==", "===", "^", "~", "=")) or re.match(r"^v?\d", s):
        return False
    return ">" in s


def _compare(a: str | None, b: str | None) -> str:
    ka, kb = version_key(a or ""), version_key(b or "")
    if ka is None or kb is None or ka == kb:
        return "changed"
    return "upgraded" if kb > ka else "downgraded"


# --------------------------------------------------------------------------- declared dependencies


def _norm(name: str, ecosystem: str) -> str:
    return normalize_python_name(name) if ecosystem == "python" else name.lower()


def _source(dep: DeclaredDependency) -> str:
    if dep.workspace:
        return "workspace"
    if dep.source == "path":
        outside = dep.local_path is None or dep.local_path.startswith("..") or \
            re.match(r"^(file:)?(/|~|[A-Za-z]:)", dep.spec.lstrip("@ ").removeprefix("file://"))
        return "path-outside" if outside else "path"
    return dep.source


def _show(spec: str) -> str:
    text = redact(" ".join((spec or "").split()))
    return text if len(text) <= MAX_SPEC else text[:MAX_SPEC - 1] + "…"


def indexes(path: str, text: str | None) -> set[str]:
    """Package indexes a Python manifest adds: ``--index-url`` / ``--extra-index-url`` / ``--find-links`` lines,
    and ``[[tool.uv.index]]`` / ``[[tool.poetry.source]]`` / ``[[tool.pdm.source]]`` URLs."""
    if not text:
        return set()
    kind = (classify.manifest_kind(path) or classify.ManifestKind("", "")).kind
    if kind == "requirements":
        return {m.group(2) for m in re.finditer(r"(?m)^\s*(--index-url|-i|--extra-index-url|--find-links|-f)[\s=]+(\S+)",
                                                text)}
    if kind == "pyproject":
        try:
            tool = tomllib.loads(text).get("tool") or {}
        except tomllib.TOMLDecodeError:
            return set()
        entries = ((tool.get("uv") or {}).get("index") or []) + ((tool.get("poetry") or {}).get("source") or []) + \
            ((tool.get("pdm") or {}).get("source") or [])
        return {str(e["url"]) for e in entries if isinstance(e, dict) and e.get("url")}
    return set()


def declared(path: str, text: str | None) -> tuple[str | None, list[DeclaredDependency]]:
    """(ecosystem, dependencies) of a manifest version; ``(None, [])`` when it is not a parsed manifest."""
    mk = classify.manifest_kind(path)
    if not text or mk is None or mk.lockfile:
        return (mk.ecosystem if mk else None), []
    md = parse_project_file(path, text, lambda _p: False)
    if md is None or md.metadata.get("parsed") is False or md.errors:
        return md.ecosystem if md else None, []
    return md.ecosystem, md.dependencies


def declared_changes(path: str, before: str | None, after: str | None) -> list[dict[str, Any]]:
    """The declared dependencies that were added, removed or changed between two versions of a manifest."""
    eco_b, old = declared(path, before)
    eco_a, new = declared(path, after)
    eco = eco_a or eco_b or ""

    def by_name(deps: list[DeclaredDependency]) -> dict[str, dict[str, DeclaredDependency]]:
        out: dict[str, dict[str, DeclaredDependency]] = {}
        for d in deps:
            out.setdefault(_norm(d.name, eco), {}).setdefault(d.scope, d)
        return out

    b, a = by_name(old), by_name(new)
    registry = [d for d in new if _source(d) in ("", "index")]
    pinned = sum(1 for d in registry if not unbounded(d.spec, eco))
    # "the rest of the file pins": at least 3 other registry dependencies, 80% of them pinned or bounded
    file_pins = len(registry) - 1 >= 3 and pinned / (len(registry) - 1) >= 0.8
    out: list[dict[str, Any]] = []
    for name in sorted(set(b) | set(a)):
        bs, as_ = b.get(name, {}), a.get(name, {})
        pairs: list[tuple[DeclaredDependency | None, DeclaredDependency | None]] = []
        if len(bs) == 1 and len(as_) == 1 and set(bs) != set(as_):  # moved to another scope
            pairs.append((next(iter(bs.values())), next(iter(as_.values()))))
        else:
            pairs += [(bs.get(scope), as_.get(scope)) for scope in sorted(set(bs) | set(as_))]
        for x, y in pairs:
            item = _entry(x, y, eco, file_pins)
            if item:
                out.append(item)
    added_indexes = sorted(indexes(path, after) - indexes(path, before))
    for url in added_indexes:
        out.append({"name": "(package index)", "ecosystem": eco, "scope": "", "status": "source-changed",
                    "before": None, "after": _show(url), "line": _line_of(after or "", url), "source": "index",
                    "source_before": None, "index": True})
    return out[:MAX_LISTED]


def _line_of(text: str, needle: str) -> int | None:
    i = text.find(needle)
    return text.count("\n", 0, i) + 1 if i >= 0 else None


def _entry(x: DeclaredDependency | None, y: DeclaredDependency | None, eco: str,
           file_pins: bool) -> dict[str, Any] | None:
    cur = y or x
    assert cur is not None
    sx, sy = (_source(x) if x else None), (_source(y) if y else None)
    if "workspace" in (sx, sy) or (sx in ("path", None) and sy in ("path", None) and (sx or sy) == "path"):
        return None  # a package of this repository, not a third-party one
    if x is not None and y is not None and x.spec == y.spec and x.scope == y.scope and sx == sy and x.raw == y.raw:
        return None
    item: dict[str, Any] = {"name": cur.name, "ecosystem": eco, "scope": cur.scope, "line": y.line if y else None,
                            "before": _show(x.spec) if x else None, "after": _show(y.spec) if y else None,
                            "source": sy, "source_before": sx}
    if x is None:
        item["status"] = "added"
    elif y is None:
        item["status"] = "removed"
    elif sy != sx and sy in RISKY_SOURCES:
        item["status"] = "source-changed"
    elif x.scope != y.scope:
        item["status"], item["scope_before"] = "scope-changed", x.scope
    elif sx or sy:  # a URL, path or alias that changed: no version to compare
        item["status"] = "source-changed" if sy in RISKY_SOURCES else "changed"
    else:
        item["status"] = _compare(spec_version(x.spec), spec_version(y.spec))
    if y is not None and not sy and unbounded(y.spec, eco) and (
            (x is not None and not sx and not unbounded(x.spec, eco)) or (x is None and file_pins)):
        item["unpinned"] = True
    return item


# --------------------------------------------------------------------------- lock files


def _npm_lock(text: str) -> tuple[dict[str, set[str]], set[str]]:
    data = json.loads(text)
    versions: dict[str, set[str]] = {}
    direct: set[str] = set()
    packages = data.get("packages")
    if isinstance(packages, dict):  # lockfileVersion 2 and 3
        for key, info in packages.items():
            if not isinstance(info, dict):
                continue
            if key == "":
                for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                    direct.update((info.get(section) or {}).keys())
                continue
            if "node_modules/" not in key or info.get("link"):
                continue
            name = info.get("name") or key.rsplit("node_modules/", 1)[1]
            if info.get("version"):
                versions.setdefault(name, set()).add(str(info["version"]))
    else:  # lockfileVersion 1
        def walk(deps: dict[str, Any], top: bool) -> None:
            for name, info in (deps or {}).items():
                if isinstance(info, dict) and info.get("version"):
                    versions.setdefault(name, set()).add(str(info["version"]))
                    if top:
                        direct.add(name)
                    walk(info.get("dependencies") or {}, False)
        walk(data.get("dependencies") or {}, True)
    return versions, direct


def _toml_packages(text: str) -> tuple[dict[str, set[str]], set[str]]:
    """poetry.lock, uv.lock, pdm.lock, Cargo.lock: ``[[package]]`` tables with ``name`` and ``version``."""
    data = tomllib.loads(text)
    versions: dict[str, set[str]] = {}
    direct: set[str] = set()
    for pkg in data.get("package") or []:
        if not isinstance(pkg, dict) or not pkg.get("name"):
            continue
        source = pkg.get("source") if isinstance(pkg.get("source"), dict) else {}
        if source.get("editable") == "." or source.get("virtual") == ".":  # uv: the project itself
            direct.update(d["name"] for d in pkg.get("dependencies") or [] if isinstance(d, dict) and d.get("name"))
            for group in (pkg.get("dev-dependencies") or {}).values():
                direct.update(d["name"] for d in group or [] if isinstance(d, dict) and d.get("name"))
            continue
        if pkg.get("version"):
            versions.setdefault(str(pkg["name"]), set()).add(str(pkg["version"]))
    return versions, direct


def _pipfile_lock(text: str) -> tuple[dict[str, set[str]], set[str]]:
    data = json.loads(text)
    versions: dict[str, set[str]] = {}
    for section in ("default", "develop"):
        for name, info in (data.get(section) or {}).items():
            if isinstance(info, dict) and info.get("version"):
                versions.setdefault(name, set()).add(str(info["version"]).lstrip("="))
    return versions, set()


def _composer_lock(text: str) -> tuple[dict[str, set[str]], set[str]]:
    data = json.loads(text)
    versions: dict[str, set[str]] = {}
    for section in ("packages", "packages-dev"):
        for pkg in data.get(section) or []:
            if isinstance(pkg, dict) and pkg.get("name") and pkg.get("version"):
                versions.setdefault(pkg["name"], set()).add(str(pkg["version"]))
    return versions, set()


def _go_sum(text: str) -> tuple[dict[str, set[str]], set[str]]:
    versions: dict[str, set[str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not parts[1].endswith("/go.mod"):
            versions.setdefault(parts[0], set()).add(parts[1])
    return versions, set()


def _yarn_lock(text: str) -> tuple[dict[str, set[str]], set[str]]:
    """Yarn classic (``version "1.2.3"``) and Berry (``version: 1.2.3``) entries."""
    versions: dict[str, set[str]] = {}
    names: list[str] = []
    for line in text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":") and not line.startswith("#"):
            names = []
            for spec in line.rstrip()[:-1].split(","):
                spec = spec.strip().strip('"')
                at = spec.rfind("@")
                if at > 0:
                    names.append(spec[:at])
        elif names:
            m = re.match(r'^\s+version:?\s+"?([^"\s]+)"?\s*$', line)
            if m:
                for name in names:
                    versions.setdefault(name, set()).add(m.group(1))
                names = []
    return versions, set()


def _pnpm_lock(text: str) -> tuple[dict[str, set[str]], set[str]]:
    """``pnpm-lock.yaml`` (v5 to v9) by line patterns: package keys under ``packages:`` and the root importer."""
    versions: dict[str, set[str]] = {}
    direct: set[str] = set()
    section = ""
    in_root = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            section = line.rstrip().rstrip(":")
            in_root = False
            continue
        if section == "packages":
            m = re.match(r"^  '?/?((?:@[^/@'\s]+/)?[^@'\s/(]+)[@/]([0-9][^:('\s]*)", line)
            if m:
                versions.setdefault(m.group(1), set()).add(m.group(2))
        elif section == "importers":
            if re.match(r"^  \S", line):
                in_root = line.strip().rstrip(":") == "."
            elif in_root:
                m = re.match(r"^      '?((?:@[^/'\s]+/)?[^:'\s]+)'?:", line)
                if m:
                    direct.add(m.group(1))
        elif section in ("dependencies", "devDependencies", "optionalDependencies"):  # v5 root importer
            m = re.match(r"^  '?((?:@[^/'\s]+/)?[^:'\s]+)'?:", line)
            if m:
                direct.add(m.group(1))
    return versions, direct


LOCK_PARSERS: dict[str, Callable[[str], tuple[dict[str, set[str]], set[str]]]] = {
    "package-lock": _npm_lock, "npm-shrinkwrap": _npm_lock, "yarn.lock": _yarn_lock, "pnpm-lock": _pnpm_lock,
    "poetry.lock": _toml_packages, "uv.lock": _toml_packages, "pdm.lock": _toml_packages,
    "cargo.lock": _toml_packages, "pipfile.lock": _pipfile_lock, "composer.lock": _composer_lock, "go.sum": _go_sum,
}


def lock_kind(path: str) -> str | None:
    mk = classify.manifest_kind(path)
    return mk.kind if mk is not None and mk.lockfile and mk.kind in LOCK_PARSERS else None


def resolved(path: str, text: str | None) -> tuple[dict[str, set[str]], set[str]]:
    """(name → resolved versions, direct dependency names the lock file records itself) of one lock file version."""
    kind = lock_kind(path)
    if not text or kind is None:
        return {}, set()
    return LOCK_PARSERS[kind](text)


def _show_versions(vs: set[str] | None) -> str | None:
    if not vs:
        return None
    ordered = sorted(vs, key=lambda v: version_key(v) or (0,))
    return ", ".join(ordered[-3:]) + (f" (+{len(ordered) - 3})" if len(ordered) > 3 else "")


def lock_changes(path: str, before: str | None, after: str | None, direct: set[str], ecosystem: str) -> dict[str, Any]:
    """Resolved-version changes in a lock file: the direct dependencies one by one, the others counted.

    ``direct`` holds the (normalized) names the matching manifest declares; the lock file's own record of its
    direct dependencies (npm, pnpm, uv) is added to it.
    """
    for text in (before, after):
        if text is not None and len(text.encode("utf-8", "ignore")) > MAX_LOCK_BYTES:
            return {"packages": [], "transitive": 0,
                    "note": f"lock file larger than {MAX_LOCK_BYTES // 1_000_000} MB: resolved versions not compared"}
    try:
        old, old_direct = resolved(path, before)
        new, new_direct = resolved(path, after)
    except (ValueError, tomllib.TOMLDecodeError, AttributeError, TypeError) as exc:
        return {"packages": [], "transitive": 0, "note": f"lock file could not be parsed ({type(exc).__name__})"}
    wanted = {_norm(n, ecosystem) for n in direct | old_direct | new_direct}
    packages: list[dict[str, Any]] = []
    transitive = 0
    for name in sorted(set(old) | set(new)):
        a, b = old.get(name), new.get(name)
        if a == b:
            continue
        if wanted and _norm(name, ecosystem) not in wanted:
            transitive += 1
            continue
        status = "added" if not a else "removed" if not b else _compare(max(a, key=lambda v: version_key(v) or (0,)),
                                                                          max(b, key=lambda v: version_key(v) or (0,)))
        packages.append({"name": name, "ecosystem": ecosystem, "status": status, "before": _show_versions(a),
                         "after": _show_versions(b), "locked": True})
    return {"packages": packages[:MAX_LISTED], "transitive": transitive}


def manifest_for_lock(path: str) -> str | None:
    kind = lock_kind(path)
    return posixpath.join(posixpath.dirname(path), LOCK_MANIFEST[kind]) if kind in LOCK_MANIFEST else None


def locks_for_manifest(path: str, exists: Callable[[str], bool]) -> str | None:
    """The lock file that resolves a manifest: in its directory, else the nearest parent (workspaces)."""
    mk = classify.manifest_kind(path)
    names = MANIFEST_LOCKS.get(mk.kind if mk else "", ())
    d = posixpath.dirname(path)
    while True:
        for name in names:
            cand = posixpath.join(d, name) if d else name
            if exists(cand):
                return cand
        if not d:
            return None
        d = posixpath.dirname(d)


def summary(files: list[dict[str, Any]]) -> dict[str, int]:
    """Counts for the review header: one per package, manifests first, then lock-only direct changes."""
    counts = {"added": 0, "removed": 0, "upgraded": 0, "downgraded": 0, "other": 0}
    seen: set[tuple[str, str, str]] = set()
    ordered = sorted(files, key=lambda f: bool(f.get("lock")))  # manifests before lock files
    for f in ordered:
        for p in f.get("packages") or []:
            if p.get("index"):
                counts["other"] += 1
                continue
            key = (p.get("ecosystem", ""), _norm(p["name"], p.get("ecosystem", "")), "")
            if key in seen:
                continue
            seen.add(key)
            counts[p["status"] if p["status"] in counts else "other"] += 1
    return counts
