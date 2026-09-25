"""Analyzer registry.

Analyzers are registered as classes; the pipeline instantiates fresh
analyzers for every snapshot, so analyzers may keep per-snapshot state on
``self``.  Third-party analyzers can be added with :func:`register` (or via
the ``repoviz.analyzers`` entry-point group).
"""

from __future__ import annotations

from importlib import metadata as _metadata

from .base import PHASES, AnalysisContext, Analyzer, Detection, SnapshotBuilder
from .callflow import CallFlowAnalyzer
from .filesystem import FilesystemAnalyzer
from .git import GitAnalyzer
from .golang import GoAnalyzer
from .javascript import JavaScriptAnalyzer
from .manifest import ManifestAnalyzer
from .python import PythonAnalyzer
from .runtime import RuntimeAnalyzer

# Order matters within a phase: generic structure first, call-flow resolution last.
_REGISTRY: list[type[Analyzer]] = [
    FilesystemAnalyzer,
    GitAnalyzer,
    ManifestAnalyzer,
    PythonAnalyzer,
    JavaScriptAnalyzer,
    GoAnalyzer,
    RuntimeAnalyzer,
    CallFlowAnalyzer,
]
_PLUGINS_LOADED = False


def register(cls: type[Analyzer], before: str | None = "callflow") -> None:
    """Register an analyzer class (inserted before the call-flow resolver by default)."""
    if cls in _REGISTRY:
        return
    names = [c.name for c in _REGISTRY]
    if before in names:
        _REGISTRY.insert(names.index(before), cls)
    else:
        _REGISTRY.append(cls)


def _load_plugins() -> None:
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return
    _PLUGINS_LOADED = True
    try:
        eps = _metadata.entry_points(group="repoviz.analyzers")
    except Exception:  # pragma: no cover - importlib.metadata edge cases
        return
    for ep in eps:
        try:
            cls = ep.load()
            if isinstance(cls, type) and issubclass(cls, Analyzer):
                register(cls)
        except Exception:  # pragma: no cover - a broken plugin must not break analysis
            continue


def analyzer_classes() -> list[type[Analyzer]]:
    _load_plugins()
    return list(_REGISTRY)


def supported_languages() -> dict[str, list[str]]:
    """language -> names of analyzers that extract dependencies for it."""
    out: dict[str, list[str]] = {}
    for cls in analyzer_classes():
        for lang in cls.languages:
            out.setdefault(lang, []).append(cls.name)
    return out


__all__ = ["PHASES", "AnalysisContext", "Analyzer", "Detection", "SnapshotBuilder", "analyzer_classes", "register",
           "supported_languages"]
