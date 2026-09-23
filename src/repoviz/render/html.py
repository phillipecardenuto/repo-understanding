"""Build the application bundle and the HTML pages.

The same page and script serve both output modes:

* the **live** page links ``/assets/*`` and fetches data from the local
  server's ``/api/*`` endpoints;
* the **static** report inlines the stylesheet, the vendored Mermaid library,
  the application script and all data (optionally gzip-compressed), so the
  single HTML file works offline from ``file://``.
"""

from __future__ import annotations

import base64
import gzip
import html
import json
import re
from importlib import resources
from typing import Any

from .. import __version__
from ..activity import observe
from ..model import RepositoryDiff
from ..pipeline import utcnow
from ..repo import Comparison, Repository
from .mermaid import theme

COMPRESS_THRESHOLD = 1_500_000


def asset(name: str) -> str:
    return resources.files("repoviz").joinpath(f"web/{name}").read_text(encoding="utf-8")


def asset_bytes(name: str) -> bytes:
    return resources.files("repoviz").joinpath(f"web/{name}").read_bytes()


def to_jsonable(value: Any) -> Any:
    if isinstance(value, RepositoryDiff):
        return value.to_dict()
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def dumps(data: Any) -> str:
    return json.dumps(to_jsonable(data), separators=(",", ":"), ensure_ascii=False, default=str)


KEEP_SYMBOL_META = ("kind", "public", "exported", "component_id", "project_id")


def compact_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    """Shrink a serialized snapshot for embedding: call evidence keeps locations, not excerpts."""
    for e in data.get("call_edges", []):
        evs = e.get("evidence") or []
        e["evidence"] = [{k: v for k, v in ev.items() if k != "excerpt"} for ev in evs[:3]]
    for e in data.get("dependency_edges", []):
        if len(e.get("evidence") or []) > 20:
            e["evidence"] = e["evidence"][:20]
    return data


def compact_diff(data: dict[str, Any]) -> dict[str, Any]:
    """Shrink a serialized diff for embedding.

    Unchanged call edges are dropped, unchanged edges lose their evidence (the UI
    falls back to the working-tree snapshot, which carries the same edge IDs),
    unchanged symbols keep only structural fields, and the long lists of
    unchanged IDs are replaced by counts.
    """
    edges = []
    for e in data.get("edges", []):
        if e.get("status") == "unchanged":
            if e.get("relationship") == "calls":
                continue
            e = {k: v for k, v in e.items() if k not in ("evidence", "base_evidence")}
        edges.append(e)
    data["edges"] = edges
    nodes = []
    for n in data.get("nodes", []):
        if n.get("status") == "unchanged" and n.get("category") == "symbol":
            n = {k: v for k, v in n.items() if k in ("id", "name", "qualified_name", "component_type", "category",
                                                     "parent_id", "path", "status", "tags", "start_line", "language")}
        nodes.append(n)
    data["nodes"] = nodes
    data["unchanged_nodes"] = len(data.get("unchanged_nodes", []))
    data["unchanged_edges"] = len(data.get("unchanged_edges", []))
    data["compact"] = True
    return data


def comparison_payload(repo: Repository, comp: Comparison, index: int = 0, compact: bool = False) -> dict[str, Any]:
    base = repo.snapshot(comp.base, comp.base_label)
    target = repo.snapshot(comp.target, comp.target_label)
    from ..diff import diff_snapshots

    diff = diff_snapshots(base, target)
    data = diff.to_dict()
    return {"id": f"c{index}", "label": comp.label, "mode": comp.mode, "base": comp.base, "target": comp.target,
            "base_label": comp.base_label, "target_label": comp.target_label,
            "diff": compact_diff(data) if compact else data, "_revisions": (base.revision_id, target.revision_id)}


def build_bundle(repo: Repository, *, comparisons: list[Comparison] | None = None, include_activity: bool = True,
                 mode: str = "static") -> dict[str, Any]:
    snapshot = repo.snapshot("WORKTREE", "working tree")
    comps = comparisons if comparisons is not None else repo.default_comparisons()
    payloads = []
    errors = []
    for i, comp in enumerate(comps):
        try:
            payloads.append(comparison_payload(repo, comp, i, compact=mode == "static"))
        except Exception as exc:  # a bad revision must not prevent the report
            errors.append({"severity": "error", "code": "comparison-failed", "message": f"{comp.label}: {exc}",
                           "analyzer": "report"})
    activity = None
    if include_activity:
        try:
            activity = observe(repo, record=False)
            diff = activity.get("diff")
            if isinstance(diff, RepositoryDiff):
                for p in payloads:
                    if p["_revisions"] == (diff.base.revision_id, diff.target.revision_id):
                        activity["diff_ref"] = p["id"]
                        activity.pop("diff")
                        break
        except Exception as exc:
            errors.append({"severity": "error", "code": "activity-failed", "message": str(exc), "analyzer": "report"})
    for p in payloads:
        p.pop("_revisions", None)
    snap = snapshot.to_dict()
    if mode == "static":
        compact_snapshot(snap)
        if activity is not None and isinstance(activity.get("diff"), RepositoryDiff):
            activity["diff"] = compact_diff(activity["diff"].to_dict())
    snap["diagnostics"] = snap["diagnostics"] + errors
    return {
        "schema_version": snapshot.schema_version,
        "mode": mode,
        "tool_version": __version__,
        "generated_at": utcnow(),
        "profile": snapshot.profile,
        "revisions": _revisions(repo),
        "snapshot": snap,
        "comparisons": payloads,
        "activity": to_jsonable(activity) if activity is not None else None,
        "theme": theme(),
        "config": {"sources": repo.config.sources, "max_diagram_nodes": repo.config.max_diagram_nodes},
    }


def _revisions(repo: Repository) -> dict[str, Any]:
    try:
        return repo.revisions()
    except Exception:
        return repo.git_info()


def _inline_script(js: str) -> str:
    # Nothing inside an inline <script> may close it early.
    return re.sub(r"</(script)", r"<\\/\1", js, flags=re.IGNORECASE)


def encode_data(bundle: dict[str, Any], compress: bool | None = None) -> tuple[str, str]:
    text = dumps(bundle)
    if compress is None:
        compress = len(text) > COMPRESS_THRESHOLD
    if compress:
        packed = base64.b64encode(gzip.compress(text.encode("utf-8"), compresslevel=6, mtime=0)).decode("ascii")
        return "gzip+base64", packed
    # "<" keeps "</script>" and "<!--" out of the element while remaining valid JSON.
    return "json", text.replace("<", "\\u003c")


def render_static_html(bundle: dict[str, Any], compress: bool | None = None) -> str:
    encoding, data = encode_data(bundle, compress)
    name = bundle.get("snapshot", {}).get("repository_name", "repository")
    page = asset("index.html").replace("__TITLE__", html.escape(f"repoviz · {name}"))
    page = page.replace("<!--REPOVIZ:CSS-->", f"<style>\n{asset('app.css')}\n</style>")
    page = page.replace("<!--REPOVIZ:DATA-->",
                        f'<script type="application/json" id="repoviz-data" data-encoding="{encoding}">{data}</script>')
    page = page.replace("<!--REPOVIZ:MERMAID-->",
                        f"<script>{_inline_script(asset('vendor/mermaid.min.js'))}</script>")
    page = page.replace("<!--REPOVIZ:APP-->", f"<script>{_inline_script(asset('app.js'))}</script>")
    return page


def render_live_html(name: str) -> str:
    page = asset("index.html").replace("__TITLE__", html.escape(f"repoviz · {name}"))
    page = page.replace("<!--REPOVIZ:CSS-->", '<link rel="stylesheet" href="/assets/app.css">')
    page = page.replace("<!--REPOVIZ:DATA-->", "")
    page = page.replace("<!--REPOVIZ:MERMAID-->", '<script src="/assets/mermaid.min.js"></script>')
    page = page.replace("<!--REPOVIZ:APP-->", '<script src="/assets/app.js"></script>')
    return page
