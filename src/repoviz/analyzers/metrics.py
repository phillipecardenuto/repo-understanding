"""Health metrics on the graph (#30): size, complexity, coupling and hotspots, per module and rolled up.

Runs last (``finalize``), when the modules, their imports and the Git churn exist:

* ``metadata.metrics`` on each module (and each programming file of a language repoviz does not parse):
  ``sloc``, ``complexity`` with its ``complexity_kind`` (``cyclomatic`` for Python, from the parse the Python
  analyzer already made; ``whitespace`` otherwise, cached per file content), ``max_nesting``, the most complex
  function (``max_complexity``, ``max_complexity_symbol``), ``fan_in``, ``fan_out``, ``instability`` and, for
  modules that changed at least twice in the churn window, ``hotspot_top`` / ``hotspot_score``;
* the same, summed, on the directories, packages and projects that contain them (``modules``, ``sloc``,
  ``complexity``; ``fan_in`` / ``fan_out`` count the modules *outside* the container; ``hotspot_top`` is its
  hottest module's; ``owner_share`` is weighted by commits);
* ``metadata.metrics`` on the snapshot: how many modules were measured and how many are hotspot candidates.

See :mod:`repoviz.metrics` for the definitions.  Nothing is run.
"""

from __future__ import annotations

from typing import Any

from .. import metrics
from ..ids import stable_hash
from ..model import CATEGORY_MODULE, CATEGORY_SYMBOL, REL_IMPORTS
from .base import CAP_DIAGNOSTICS, AnalysisContext, Analyzer, Detection, SnapshotBuilder

MAX_TEXT = 2_000_000
ROLLUP_TYPES = ("directory", "package", "namespace-package", "project", "workspace-member", "repository", "submodule")


class MetricsAnalyzer(Analyzer):
    name = "metrics"
    version = "1"
    capabilities = (CAP_DIAGNOSTICS,)

    def detect(self, ctx: AnalysisContext) -> Detection:
        return Detection(True, "size, complexity, coupling and hotspots")

    def finalize(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        py = ctx.shared.get("python.modules") or {}
        measured: dict[str, dict[str, Any]] = {}
        for n in list(b.nodes.values()):
            if not n.path or "external" in n.tags:
                continue
            if n.category != CATEGORY_MODULE and "unsupported" not in n.tags:
                continue
            mod = py.get(n.path) if n.language == "python" else None
            if mod is not None and not mod.info.error:
                info = mod.info
                m: dict[str, Any] = {"sloc": info.sloc, "complexity": info.complexity,
                                     "complexity_kind": "cyclomatic", "max_nesting": info.max_nesting}
                fns = [s for s in info.symbols if s.complexity]
                if fns:
                    worst = max(fns, key=lambda s: (s.complexity, -s.line))
                    m["max_complexity"] = worst.complexity
                    m["max_complexity_symbol"] = worst.qualname.split("#")[0]
            else:
                text = ctx.text(n.path)
                if text is None or len(text) > MAX_TEXT:
                    continue
                digest = ctx.source.content_hash(n.path) or stable_hash(text)
                lang = n.language or ""
                m = dict(ctx.cached(("metrics", self.version, lang, digest),
                                    lambda text=text, lang=lang: metrics.text_metrics(text, lang)))
            measured[n.id] = m
        if not measured:
            return
        self._coupling(b, measured)
        commits = {nid: int((b.nodes[nid].metadata.get("churn") or {}).get("commits") or 0) for nid in measured}
        hot = metrics.hotspots({nid: {"complexity": m["complexity"], "complexity_kind": m["complexity_kind"],
                                      "commits": commits[nid]} for nid, m in measured.items()
                                if "test" not in b.nodes[nid].tags})  # tests are not ranked against the code
        for nid, m in measured.items():
            m.update(hot.get(nid, {}))
            b.nodes[nid].metadata["metrics"] = m
        self._rollup(b, measured, commits)
        b.metadata["metrics"] = {"measured": len(measured), "hotspot_candidates": len(hot)}
        b.stat(self.name, "measured", len(measured))

    def _coupling(self, b: SnapshotBuilder, measured: dict[str, dict[str, Any]]) -> None:
        """Fan-in (importers, tests not counted) and fan-out (modules imported) from direct import edges."""
        by_path = {b.nodes[nid].path: nid for nid in measured}

        def module_of(nid: str) -> str | None:
            if nid in measured:
                return nid
            node = b.nodes.get(nid)
            return by_path.get(node.path) if node is not None and node.category == CATEGORY_SYMBOL else None

        fan_in: dict[str, set[str]] = {}
        fan_out: dict[str, set[str]] = {}
        self.imports: list[tuple[str, str, bool]] = []  # (importer, imported, the importer is a test)
        for e in b.edges.values():
            if e.relationship != REL_IMPORTS or not e.direct:
                continue
            src, tgt = module_of(e.source_id), module_of(e.target_id)
            if src is None or tgt is None or src == tgt:
                continue
            fan_out.setdefault(src, set()).add(tgt)
            test = "test" in b.nodes[src].tags
            if not test:
                fan_in.setdefault(tgt, set()).add(src)
            self.imports.append((src, tgt, test))
        for nid, m in measured.items():
            i, o = len(fan_in.get(nid, ())), len(fan_out.get(nid, ()))
            m["fan_in"], m["fan_out"] = i, o
            m["instability"] = round(o / (i + o), 2) if i + o else None

    def _rollup(self, b: SnapshotBuilder, measured: dict[str, dict[str, Any]], commits: dict[str, int]) -> None:
        """Containers sum their modules; their fan-in / fan-out count modules outside them."""
        chains: dict[str, list[str]] = {}

        def ancestors(nid: str) -> list[str]:
            if nid not in chains:
                out, cur, seen = [], b.nodes.get(b.nodes[nid].parent_id or ""), 0
                while cur is not None and seen < 64:
                    if cur.component_type in ROLLUP_TYPES:
                        out.append(cur.id)
                    cur, seen = b.nodes.get(cur.parent_id or ""), seen + 1
                chains[nid] = out
            return chains[nid]

        acc: dict[str, dict[str, Any]] = {}
        for nid, m in measured.items():
            share = (b.nodes[nid].metadata.get("churn") or {}).get("owner_share")
            for a in ancestors(nid):
                r = acc.setdefault(a, {"modules": 0, "sloc": 0, "complexity": 0, "_in": set(), "_out": set(),
                                       "_owned": 0.0, "_commits": 0})
                r["modules"] += 1
                r["sloc"] += m["sloc"]
                r["complexity"] += m["complexity"]
                if m.get("hotspot_top") and (not r.get("hotspot_top") or m["hotspot_top"] < r["hotspot_top"]):
                    r["hotspot_top"] = m["hotspot_top"]
                    r["hottest"] = b.nodes[nid].path
                if share and commits[nid]:
                    r["_owned"] += share * commits[nid]
                    r["_commits"] += commits[nid]
        for src, tgt, test in self.imports:
            inside_src = set(ancestors(src))
            if not test:
                for a in ancestors(tgt):
                    if a not in inside_src:
                        acc[a]["_in"].add(src)
            inside_tgt = set(ancestors(tgt))
            for a in inside_src - inside_tgt:
                acc[a]["_out"].add(tgt)
        for a, r in acc.items():
            r["fan_in"], r["fan_out"] = len(r.pop("_in")), len(r.pop("_out"))
            owned, total = r.pop("_owned"), r.pop("_commits")
            if total:
                r["owner_share"] = round(owned / total, 2)
            b.nodes[a].metadata["metrics"] = r
