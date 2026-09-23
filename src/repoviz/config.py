"""User configuration.

Discovery is automatic; configuration only *overrides* it.  Settings are read,
in increasing priority, from:

1. built-in defaults,
2. ``[tool.repoviz]`` in the repository's ``pyproject.toml``,
3. ``.repoviz.toml`` (top-level keys) in the repository root,
4. an explicit ``--config`` file,
5. command-line flags.

All keys are optional.  See ``docs/configuration.md`` for the reference.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

#: Directories that are never analyzed (in addition to what .gitignore hides).
BUILTIN_EXCLUDES = [
    ".git/", ".hg/", ".svn/", "node_modules/", "__pycache__/", ".mypy_cache/", ".pytest_cache/",
    ".ruff_cache/", ".tox/", ".nox/", ".venv/", "venv/", ".eggs/", "*.egg-info/", ".idea/",
    ".vscode/", ".gradle/", ".next/", ".nuxt/", ".svelte-kit/", ".turbo/", ".cache/",
    "bower_components/", ".terraform/", ".DS_Store",
]


@dataclass
class ComponentRule:
    """Explicit component declared by the user."""

    name: str
    paths: list[str]
    type: str = "component"
    description: str = ""


@dataclass
class Config:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)
    include_generated: bool = False
    include_vendored: bool = False
    source_roots: list[str] | None = None
    test_roots: list[str] | None = None
    test_patterns: list[str] = field(default_factory=list)
    docs_roots: list[str] | None = None
    default_branch: str | None = None
    languages: dict[str, str] = field(default_factory=dict)
    components: list[ComponentRule] = field(default_factory=list)
    analyzers_enabled: list[str] | None = None
    analyzers_disabled: list[str] = field(default_factory=list)
    max_file_bytes: int = 2_000_000
    # Python analyzer
    python_use_grimp: str = "auto"  # auto | always | never
    # Cycle detection
    cycles_include_type_checking: bool = False
    cycles_include_lazy: bool = True
    # Rendering / UI
    max_diagram_nodes: int = 250
    external_dependencies: bool = False
    # Activity tracking
    poll_seconds: float = 3.0
    churn_commits: int = 300
    state_dir: str | None = None
    # Where the values came from (for display/debugging).
    sources: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        data = dataclasses.asdict(self)
        data.pop("sources", None)
        data.pop("poll_seconds", None)
        data.pop("max_diagram_nodes", None)
        return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @property
    def all_excludes(self) -> list[str]:
        return BUILTIN_EXCLUDES + list(self.exclude)

    def analyzer_enabled(self, name: str) -> bool:
        if name in self.analyzers_disabled:
            return False
        if self.analyzers_enabled is not None:
            return name in self.analyzers_enabled
        return True


class ConfigError(ValueError):
    pass


def _as_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ConfigError(f"'{key}' must be a string or a list of strings")


def apply_mapping(cfg: Config, data: dict[str, Any], origin: str) -> Config:
    """Apply a ``[tool.repoviz]``-shaped mapping onto ``cfg`` (returns a copy)."""
    cfg = copy.deepcopy(cfg)
    known = set()

    def take(key: str) -> Any:
        known.add(key)
        return data.get(key)

    for key in ("include", "exclude", "generated", "test_patterns"):
        if take(key) is not None:
            setattr(cfg, key, _as_list(data[key], key))
    for key in ("source_roots", "test_roots", "docs_roots"):
        if take(key) is not None:
            setattr(cfg, key, _as_list(data[key], key))
    for key in ("include_generated", "include_vendored", "external_dependencies"):
        if take(key) is not None:
            setattr(cfg, key, bool(data[key]))
    if take("default_branch") is not None:
        cfg.default_branch = str(data["default_branch"])
    if take("max_file_bytes") is not None:
        cfg.max_file_bytes = int(data["max_file_bytes"])
    if take("state_dir") is not None:
        cfg.state_dir = str(data["state_dir"])
    if take("languages") is not None:
        langs = data["languages"]
        if not isinstance(langs, dict):
            raise ConfigError("'languages' must map file extensions to language names")
        cfg.languages.update({(k if k.startswith(".") else "." + k).lower(): str(v).lower() for k, v in langs.items()})
    if take("components") is not None:
        comps = data["components"]
        rules: list[ComponentRule] = []
        if isinstance(comps, dict):
            comps = [dict(v, name=k) if isinstance(v, dict) else {"name": k, "paths": v} for k, v in comps.items()]
        if not isinstance(comps, list):
            raise ConfigError("'components' must be a table or an array of tables")
        for c in comps:
            if not isinstance(c, dict) or "name" not in c:
                raise ConfigError("each component needs a 'name'")
            paths = _as_list(c.get("paths", c.get("path")), "components.paths")
            if not paths:
                raise ConfigError(f"component {c['name']!r} needs 'paths'")
            rules.append(ComponentRule(name=str(c["name"]), paths=paths, type=str(c.get("type", "component")),
                                       description=str(c.get("description", ""))))
        cfg.components = rules
    analyzers = take("analyzers")
    if analyzers is not None:
        if not isinstance(analyzers, dict):
            raise ConfigError("'analyzers' must be a table with 'enabled'/'disabled'")
        if "enabled" in analyzers:
            cfg.analyzers_enabled = _as_list(analyzers["enabled"], "analyzers.enabled")
        if "disabled" in analyzers:
            cfg.analyzers_disabled = _as_list(analyzers["disabled"], "analyzers.disabled")
    python = take("python")
    if python is not None:
        use = str(python.get("use_grimp", cfg.python_use_grimp)).lower()
        if use in ("true", "yes"):
            use = "always"
        if use in ("false", "no"):
            use = "never"
        if use not in ("auto", "always", "never"):
            raise ConfigError("python.use_grimp must be auto, always or never")
        cfg.python_use_grimp = use
    cycles = take("cycles")
    if cycles is not None:
        cfg.cycles_include_type_checking = bool(cycles.get("include_type_checking", cfg.cycles_include_type_checking))
        cfg.cycles_include_lazy = bool(cycles.get("include_lazy", cfg.cycles_include_lazy))
    ui = take("ui")
    if ui is not None:
        cfg.max_diagram_nodes = int(ui.get("max_diagram_nodes", cfg.max_diagram_nodes))
        cfg.external_dependencies = bool(ui.get("external_dependencies", cfg.external_dependencies))
    activity = take("activity")
    if activity is not None:
        cfg.poll_seconds = float(activity.get("poll_seconds", cfg.poll_seconds))
        cfg.churn_commits = int(activity.get("churn_commits", cfg.churn_commits))
    unknown = sorted(set(data) - known)
    if unknown:
        cfg.sources.append(f"{origin} (ignored unknown keys: {', '.join(unknown)})")
    else:
        cfg.sources.append(origin)
    return cfg


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def load_config(root: Path | str | None, explicit: Path | str | None = None,
                overrides: dict[str, Any] | None = None) -> Config:
    cfg = Config()
    if root is not None:
        root = Path(root)
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            try:
                tool = _read_toml(pyproject).get("tool", {}).get("repoviz")
            except ConfigError:
                tool = None  # a broken pyproject is reported by the manifest analyzer instead
            if isinstance(tool, dict):
                cfg = apply_mapping(cfg, tool, "pyproject.toml [tool.repoviz]")
        dotfile = root / ".repoviz.toml"
        if dotfile.is_file():
            cfg = apply_mapping(cfg, _read_toml(dotfile), ".repoviz.toml")
    if explicit is not None:
        path = Path(explicit)
        data = _read_toml(path)
        if "tool" in data and isinstance(data["tool"], dict) and "repoviz" in data["tool"]:
            data = data["tool"]["repoviz"]
        cfg = apply_mapping(cfg, data, str(path))
    if overrides:
        cfg = apply_mapping(cfg, overrides, "command line")
    return cfg
