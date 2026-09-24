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

from .risk import DEFAULT_THRESHOLDS as RISK_THRESHOLDS
from .risk import FACTORS as RISK_FACTORS

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


CONTRACT_TYPES = ("layers", "independence", "forbidden", "public-interface", "acyclic", "required")
_CONTRACT_KEYS = {
    "layers": {"layers", "containers"},
    "independence": {"modules"},
    "forbidden": {"from", "to"},
    "public-interface": {"module", "public"},
    "acyclic": {"modules"},
    "required": {"from", "to"},
}
_CONTRACT_COMMON = {"name", "type", "severity", "ignore", "allow_indirect", "message"}


@dataclass
class Contract:
    """An architecture contract over the module import graph (see ``contracts.py``)."""

    name: str
    type: str
    severity: str = "high"
    layers: list[str] = field(default_factory=list)  # layers: high → low
    containers: list[str] = field(default_factory=list)  # layers: the same layering inside each container
    modules: list[str] = field(default_factory=list)  # independence / acyclic
    source: list[str] = field(default_factory=list)  # forbidden / required: "from"
    target: list[str] = field(default_factory=list)  # forbidden / required: "to"
    module: str = ""  # public-interface: the package
    public: list[str] = field(default_factory=list)  # public-interface: what others may import
    ignore: list[str] = field(default_factory=list)  # "a -> b" imports that are allowed anyway
    allow_indirect: bool = True  # False: also follow chains of imports
    message: str = ""
    origin: str = "contracts"  # or "review.rules"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v not in ([], "")}


@dataclass
class DependencyRule:
    """A forbidden dependency: code matching ``source`` must not depend on code matching ``target``."""

    source: list[str]
    target: list[str]
    message: str = ""
    severity: str = "high"


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
    # Review of AI-agent work
    review_allowed: list[str] = field(default_factory=list)
    review_protected: list[str] = field(default_factory=list)
    review_rules: list[DependencyRule] = field(default_factory=list)
    review_sensitive: bool = True
    review_disabled_checks: list[str] = field(default_factory=list)
    review_wiring_ignore: list[str] = field(default_factory=list)  # new files that need no importer
    review_risk_weights: dict[str, float] = field(default_factory=dict)  # [review.risk] factor weights
    review_risk_thresholds: dict[str, float] = field(default_factory=dict)  # [review.risk] high / medium
    # Architecture contracts ([[contracts]]; [[review.rules]] are "forbidden" contracts) and their baseline.
    contracts: list[Contract] = field(default_factory=list)
    contracts_baseline: str = ".repoviz-known-violations.json"
    # Change coupling from Git history ("these files usually change together").
    history_commits: int = 300  # how many recent commits to learn from (0 disables)
    history_min_revs: int = 5  # a file needs this many commits before its habits count
    history_min_shared: int = 3  # commits two files must share
    history_min_degree: float = 0.5  # share of the file's commits that also changed the partner
    history_max_files_per_commit: int = 30  # larger commits (bulk renames, formatting) are ignored
    history_min_commits: int = 20  # fewer usable commits (e.g. a shallow clone): no coupling signals
    # Where the values came from (for display/debugging).
    sources: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        data = dataclasses.asdict(self)
        data.pop("sources", None)
        data.pop("poll_seconds", None)
        data.pop("max_diagram_nodes", None)
        for key in [k for k in data if k.startswith(("review_", "history_", "contracts"))]:
            data.pop(key)
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
    unknown_nested: list[str] = []

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
    history = take("history")
    if history is not None:
        if not isinstance(history, dict):
            raise ConfigError("'history' must be a table")
        for key, conv in (("commits", int), ("min_revs", int), ("min_shared", int), ("min_degree", float),
                          ("max_files_per_commit", int), ("min_commits", int)):
            if key in history:
                try:
                    value = conv(history[key])
                except (TypeError, ValueError):
                    raise ConfigError(f"history.{key} must be a number") from None
                if value < 0 or (key == "min_degree" and value > 1):
                    raise ConfigError(f"history.{key} is out of range")
                setattr(cfg, f"history_{key}", value)
    review = take("review")
    if review is not None:
        if not isinstance(review, dict):
            raise ConfigError("'review' must be a table")
        if "allowed" in review:
            cfg.review_allowed = _as_list(review["allowed"], "review.allowed")
        if "protected" in review:
            cfg.review_protected = _as_list(review["protected"], "review.protected")
        if "sensitive" in review:
            cfg.review_sensitive = bool(review["sensitive"])
        if "disabled_checks" in review:
            cfg.review_disabled_checks = _as_list(review["disabled_checks"], "review.disabled_checks")
        if "wiring_ignore" in review:
            cfg.review_wiring_ignore = _as_list(review["wiring_ignore"], "review.wiring_ignore")
        risk = review.get("risk")
        if risk is not None:
            if not isinstance(risk, dict):
                raise ConfigError("'review.risk' must be a table")
            weights, thresholds = dict(cfg.review_risk_weights), dict(cfg.review_risk_thresholds)
            for key, value in risk.items():
                if key not in RISK_FACTORS and key not in ("high", "medium"):
                    unknown_nested.append(f"review.risk.{key}")
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 \
                        or (key in ("high", "medium") and value > 100):
                    raise ConfigError(f"review.risk.{key} must be a number" + (" from 0 to 100" if key in
                                                                               ("high", "medium") else " ≥ 0"))
                (thresholds if key in ("high", "medium") else weights)[key] = float(value)
            if weights and not any(weights.get(k, 1) for k in RISK_FACTORS):
                raise ConfigError("review.risk: at least one weight must be above 0")
            if thresholds.get("medium", RISK_THRESHOLDS["medium"]) > thresholds.get("high", RISK_THRESHOLDS["high"]):
                raise ConfigError("review.risk.medium must not be above review.risk.high")
            cfg.review_risk_weights, cfg.review_risk_thresholds = weights, thresholds
        rules = review.get("rules")
        if rules is not None:
            if not isinstance(rules, list):
                raise ConfigError("'review.rules' must be an array of tables")
            cfg.review_rules = []
            for r in rules:
                if not isinstance(r, dict) or not r.get("from") or not r.get("to"):
                    raise ConfigError("each review rule needs 'from' and 'to' globs")
                sev = str(r.get("severity", "high"))
                if sev not in ("high", "medium", "low"):
                    raise ConfigError("review rule severity must be high, medium or low")
                cfg.review_rules.append(DependencyRule(_as_list(r["from"], "review.rules.from"),
                                                       _as_list(r["to"], "review.rules.to"),
                                                       str(r.get("message", "")), sev))
    contracts = take("contracts")
    if contracts is not None:
        if not isinstance(contracts, list):
            raise ConfigError("'contracts' must be an array of tables ([[contracts]])")
        cfg.contracts = [_contract(c, i, unknown_nested) for i, c in enumerate(contracts)]
        names = [c.name for c in cfg.contracts]
        if len(set(names)) != len(names):
            raise ConfigError("contract names must be unique")
    if take("contracts_baseline") is not None:
        cfg.contracts_baseline = str(data["contracts_baseline"])
    unknown = sorted(set(data) - known) + unknown_nested
    if unknown:
        cfg.sources.append(f"{origin} (ignored unknown keys: {', '.join(unknown)})")
    else:
        cfg.sources.append(origin)
    return cfg


def _contract(c: Any, i: int, unknown: list[str]) -> Contract:
    if not isinstance(c, dict):
        raise ConfigError("each [[contracts]] entry must be a table")
    ctype = str(c.get("type", ""))
    if ctype not in CONTRACT_TYPES:
        raise ConfigError(f"contract #{i + 1}: type must be one of {', '.join(CONTRACT_TYPES)}")
    name = str(c.get("name") or f"{ctype} #{i + 1}")
    where = f"contract {name!r}"
    unknown += [f"contracts.{name}.{k}" for k in sorted(set(c) - _CONTRACT_KEYS[ctype] - _CONTRACT_COMMON)]
    severity = str(c.get("severity", "high"))
    if severity not in ("high", "medium", "low"):
        raise ConfigError(f"{where}: severity must be high, medium or low")
    lst = lambda key: _as_list(c.get(key), f"{where}: {key}")  # noqa: E731
    contract = Contract(name=name, type=ctype, severity=severity, layers=lst("layers"), containers=lst("containers"),
                        modules=lst("modules"), source=lst("from"), target=lst("to"), module=str(c.get("module", "")),
                        public=lst("public"), ignore=lst("ignore"), allow_indirect=bool(c.get("allow_indirect", True)),
                        message=str(c.get("message", "")))
    need = {"layers": contract.layers and len(contract.layers) >= 2, "independence": len(contract.modules) >= 1,
            "forbidden": contract.source and contract.target, "public-interface": contract.module and contract.public,
            "acyclic": len(contract.modules) >= 1, "required": contract.source and contract.target}[ctype]
    if not need:
        raise ConfigError(f"{where}: a {ctype} contract needs " + {
            "layers": "at least two 'layers'", "independence": "'modules'", "forbidden": "'from' and 'to'",
            "public-interface": "'module' and 'public'", "acyclic": "'modules'", "required": "'from' and 'to'"}[ctype])
    for entry in contract.ignore:
        if "->" not in entry:
            raise ConfigError(f"{where}: ignore entries look like 'importer -> imported', not {entry!r}")
    return contract


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
