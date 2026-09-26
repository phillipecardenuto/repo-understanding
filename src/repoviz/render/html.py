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
import dataclasses
import gzip
import html
import json
import logging
import os
import re
from importlib import resources
from typing import Any

from .. import __version__
from ..activity import observe
from ..model import RepositoryDiff
from ..pipeline import utcnow
from ..repo import Comparison, Repository
from .mermaid import theme
from .views import breakdown

log = logging.getLogger("repoviz.report")

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


#: Node fields the web UI never reads (identity keys and change fingerprints are for diffing only).
_NODE_INTERNAL = ("key", "fingerprint")
_META_INTERNAL = ("semantic_fingerprint", "qualified_name_authoritative", "signature_id", "body_fingerprint")
_UNCHANGED_SYMBOL_FIELDS = ("id", "name", "qualified_name", "component_type", "category", "parent_id", "path", "status",
                            "tags", "start_line", "language")


def _trim_node(n: dict[str, Any]) -> dict[str, Any]:
    for k in _NODE_INTERNAL:
        n.pop(k, None)
    if n.get("analyzers") == [n.get("analyzer")]:
        n.pop("analyzers")
    md = n.get("metadata")
    if md:
        for k in _META_INTERNAL:
            md.pop(k, None)
    return n


def compact_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    """Shrink a serialized snapshot for the web UI (live responses and embedded reports).

    Containment edges duplicate ``parent_id`` and are replaced by a count;
    internal identity fields are dropped; call evidence keeps its first
    location (no excerpt) and dependency evidence at most 20 sites.
    """
    data["containment_edge_count"] = len(data.pop("containment_edges", []) or [])
    for key in ("components", "modules", "symbols"):
        for n in data.get(key, []):
            _trim_node(n)
    for e in data.get("call_edges", []):
        evs = e.get("evidence") or []
        e["evidence"] = [{k: v for k, v in ev.items() if k != "excerpt"} for ev in evs[:1]]
    for e in data.get("dependency_edges", []):
        if len(e.get("evidence") or []) > 20:
            e["evidence"] = e["evidence"][:20]
    return data


def flow_node_ids(flow: dict[str, Any] | None) -> set[str]:
    """Node IDs an affected-flow result refers to (they must survive diff compaction)."""
    if not flow:
        return set()
    ids = {n.get("id") for n in flow.get("nodes", [])}
    for group in ("entry_points", "tests"):
        for item in flow.get(group, []) or []:
            ids.update(item.get("path") or [])
            ids.add(item.get("id"))
    ids.discard(None)
    return ids  # type: ignore[return-value]


def compact_diff(data: dict[str, Any], keep: set[str] | None = None) -> dict[str, Any]:
    """Shrink a serialized diff for the web UI.

    Unchanged call edges are dropped, and unchanged edges lose their evidence
    (the UI falls back to the working-tree snapshot, which carries the same edge
    IDs). Unchanged symbols are kept only when something still refers to them:
    a remaining edge, a changed or kept descendant, or ``keep`` (e.g. the
    affected-flow nodes). The long lists of unchanged IDs become counts.
    """
    edges = []
    for e in data.get("edges", []):
        if e.get("status") == "unchanged":
            if e.get("relationship") == "calls":
                continue
            e = {k: v for k, v in e.items() if k not in ("evidence", "base_evidence")}
        edges.append(e)
    data["edges"] = edges
    by_id = {n["id"]: n for n in data.get("nodes", [])}
    needed = set(keep or ())
    for e in edges:
        needed.add(e.get("source_id"))
        needed.add(e.get("target_id"))
    needed.update(nid for nid, n in by_id.items() if n.get("status") != "unchanged")
    stack = list(needed)
    while stack:  # ancestors of everything kept (methods need their class and module)
        n = by_id.get(stack.pop())
        parent = n.get("parent_id") if n else None
        if parent and parent not in needed:
            needed.add(parent)
            stack.append(parent)
    nodes, omitted = [], 0
    for n in data.get("nodes", []):
        if n.get("status") == "unchanged" and n.get("category") == "symbol":
            if n["id"] not in needed:
                omitted += 1
                continue
            n = {k: v for k, v in n.items() if k in _UNCHANGED_SYMBOL_FIELDS}
        nodes.append(_trim_node(n))
    data["nodes"] = nodes
    data["omitted_unchanged_symbols"] = omitted
    data["unchanged_nodes"] = len(data.get("unchanged_nodes", []))
    data["unchanged_edges"] = len(data.get("unchanged_edges", []))
    data["compact"] = True
    return data


def compact_diff_of(diff: RepositoryDiff, keep: set[str] | None = None) -> dict[str, Any]:
    """:func:`compact_diff` of a diff object, without serializing what compaction would drop anyway."""
    edges = {eid: c for eid, c in diff.edges.items() if not (c.status == "unchanged" and c.edge.relationship == "calls")}
    needed = set(keep or ())
    for c in edges.values():
        needed.add(c.edge.source_id)
        needed.add(c.edge.target_id)
    needed.update(nid for nid, c in diff.nodes.items() if c.status != "unchanged")
    stack = list(needed)
    while stack:
        c = diff.nodes.get(stack.pop())
        parent = c.node.parent_id if c else None
        if parent and parent not in needed:
            needed.add(parent)
            stack.append(parent)
    nodes = {nid: c for nid, c in diff.nodes.items()
             if c.status != "unchanged" or c.node.category != "symbol" or nid in needed}
    slim = dataclasses.replace(diff, nodes=nodes, edges=edges)
    data = compact_diff(slim.to_dict(), needed)
    data["omitted_unchanged_symbols"] = len(diff.nodes) - len(nodes)
    return data


def comparison_payload(repo: Repository, comp: Comparison, index: int = 0, compact: bool = False,
                       keep: set[str] | None = None) -> dict[str, Any]:
    base = repo.snapshot(comp.base, comp.base_label)
    target = repo.snapshot(comp.target, comp.target_label)
    diff = repo.diff(base, target)
    return {"id": f"c{index}", "label": comp.label, "mode": comp.mode, "base": comp.base, "target": comp.target,
            "base_label": comp.base_label, "target_label": comp.target_label, "files": repo.changed_file_count(comp),
            "diff": compact_diff_of(diff, keep) if compact else diff.to_dict(),
            "_revisions": (base.revision_id, target.revision_id)}


def _timeline(repo: Repository) -> dict[str, Any]:
    """Checkpoints and notes of the active session, else of the latest one (metadata only)."""
    from ..checkpoints import timeline

    session = repo.current_session() or max(repo.state.list_sessions(), key=lambda s: s.started_at, default=None)
    return timeline(repo.state, session)


def build_bundle(repo: Repository, *, comparisons: list[Comparison] | None = None, include_activity: bool = True,
                 mode: str = "static", include_reviews: bool | None = None, max_reviews: int = 6,
                 embed_snapshot: bool = True, extra_reviews: list[str] | None = None) -> dict[str, Any]:
    """Everything the web UI needs.  With ``embed_snapshot=False`` the (large) snapshot is left out, for callers
    that serialize and cache it separately.  ``extra_reviews`` are review specs (``main...feature``) to include
    first, on top of the listed targets."""
    snapshot = repo.snapshot("WORKTREE", "working tree")
    if include_reviews is None:
        include_reviews = mode == "static"  # the live app fetches reviews on demand
    comps = comparisons if comparisons is not None else repo.default_comparisons()
    errors = []
    activity = None
    if include_activity:
        try:
            activity = observe(repo, record=False)
            activity["timeline"] = _timeline(repo)
        except Exception as exc:
            errors.append({"severity": "error", "code": "activity-failed", "message": str(exc), "analyzer": "report"})
    keep = flow_node_ids(activity.get("flow")) if activity else set()
    payloads = []
    for i, comp in enumerate(comps):
        try:
            payloads.append(comparison_payload(repo, comp, i, compact=mode == "static", keep=keep))
        except Exception as exc:  # a bad revision must not prevent the report
            errors.append({"severity": "error", "code": "comparison-failed", "message": f"{comp.label}: {exc}",
                           "analyzer": "report"})
    if activity is not None:
        diff = activity.get("diff")
        if isinstance(diff, RepositoryDiff):
            for p in payloads:
                if p["_revisions"] == (diff.base.revision_id, diff.target.revision_id):
                    activity["diff_ref"] = p["id"]
                    activity.pop("diff")
                    break
            else:
                activity["diff"] = compact_diff_of(diff, keep) if mode == "static" else diff.to_dict()
    for p in payloads:
        p.pop("_revisions", None)
    reviews = []
    targets = []
    if include_reviews:
        from ..review import build_review, resolve_target, review_targets
        from ..verdict import view as verdict_view

        for spec in extra_reviews or []:
            try:
                targets.append(resolve_target(repo, spec))
            except Exception as exc:
                errors.append({"severity": "error", "code": "review-failed", "message": f"{spec}: {exc}",
                               "analyzer": "report"})
        try:
            # checkpoint steps need `repoviz serve` (the report embeds the timeline, not each step's review)
            listed = [t for t in review_targets(repo) if t.key not in {x.key for x in targets} and t.kind != "checkpoint"]
            targets += listed[:max_reviews]
        except Exception as exc:
            errors.append({"severity": "error", "code": "review-failed", "message": str(exc), "analyzer": "report"})
        for t in targets:
            try:
                report = build_review(repo, t, max_total_diff_lines=20000 if mode == "static" else 40000)
                report["notes"] = repo.state.load_notes(t.key)
                report["reviewed"] = repo.state.load_reviewed(t.key)
                # the verdict given in the live app (read-only here), stale when the files changed since
                report["verdict"] = verdict_view(repo.state.load_verdict(t.key), report.get("fingerprint"))
                reviews.append(report)
            except Exception as exc:  # a broken target must not prevent the report
                errors.append({"severity": "error", "code": "review-failed", "message": f"{t.label}: {exc}",
                               "analyzer": "report"})
    bundle: dict[str, Any] = {
        "schema_version": snapshot.schema_version,
        "mode": mode,
        "tool_version": __version__,
        "generated_at": utcnow(),
        "profile": snapshot.profile,
        "breakdown": breakdown(snapshot),
        "revisions": _revisions(repo),
        "comparisons": payloads,
        "activity": to_jsonable(activity) if activity is not None else None,
        "reviews": reviews,
        "file_changes": _hotspot_changes(repo, snapshot) if mode == "static" else {},
        "contracts": _contracts_payload(repo, snapshot),
        "review_targets": [t.to_dict() for t in targets],
        "theme": theme(),
        "session": _current_session(repo),
        "config": {"sources": repo.config.sources, "max_diagram_nodes": repo.config.max_diagram_nodes,
                   "external_dependencies": repo.config.external_dependencies},
        "errors": errors,
    }
    bundle.update(_worktrees(repo, mode))
    if embed_snapshot:
        snap = compact_snapshot(snapshot.to_dict())
        snap["diagnostics"] = snap["diagnostics"] + errors
        bundle["snapshot"] = snap
    return bundle


def _worktrees(repo: Repository, mode: str) -> dict[str, Any]:
    """The repository's worktrees when there are several (parallel agents): the list for the live app's switcher,
    and in a report the whole fleet (each worktree's state and the overlaps between them)."""
    if repo.git is None:
        return {}
    from ..fleet import fleet, list_worktrees

    try:
        wts, _ = list_worktrees(repo.git)
        if len(wts) < 2:
            return {}
        return {"worktrees": [w.to_dict() for w in wts], **({"fleet": fleet(repo)} if mode == "static" else {})}
    except Exception as exc:  # never break the page over the fleet
        log.warning("worktrees: %s", exc)
        return {}


def _current_session(repo: Repository) -> dict[str, Any] | None:
    try:
        session = repo.current_session()
    except Exception:
        return None
    return session.to_dict() if session else None


def _contracts_payload(repo: Repository, snapshot: Any) -> dict[str, Any] | None:
    """Architecture contracts on the working tree, for the Dependencies overlay and the Structure profile."""
    from ..contracts import check, contracts_of, layer_groups, load_baseline, report
    from ..filechanges import _read_disk, checked_path

    contracts = contracts_of(repo.config)
    if not contracts:
        return None
    path = repo.config.contracts_baseline
    try:
        data = _read_disk(repo.root, checked_path(path)) if path else None
    except ValueError:
        data = None
    known, problem = load_baseline(data.decode("utf-8", "replace") if isinstance(data, bytes) else None)
    try:
        payload = report(check(snapshot, contracts), known, problem, path)
    except Exception as exc:  # contracts are optional for a report
        return {"error": f"{type(exc).__name__}: {exc}", "contracts": [], "violations": []}
    payload["layers"] = layer_groups(snapshot, contracts)
    return payload


def _hotspot_changes(repo: Repository, snapshot: Any) -> dict[str, Any]:
    """The latest change of each churn hotspot, so the Structure tab's drawer works offline (capped)."""
    from ..filechanges import report_changes

    try:
        return report_changes(repo, snapshot)
    except Exception:  # history is optional for a report
        return {}


def _revisions(repo: Repository) -> dict[str, Any]:
    try:
        return repo.revisions()
    except Exception:
        return repo.git_info()


def _inline_script(js: str) -> str:
    # Nothing inside an inline <script> may close it early.
    return re.sub(r"</(script)", r"<\\/\1", js, flags=re.IGNORECASE)


def _home_to_tilde(text: str) -> str:
    """Shareable reports should not disclose the author's home directory (user name, layout)."""
    home = os.path.expanduser("~").rstrip("/\\")
    if len(home) < 2:
        return text
    needle = json.dumps(home, ensure_ascii=False)[1:-1]
    return re.sub(re.escape(needle) + r"(?=[\\/\"])", "~", text)


def encode_data(bundle: dict[str, Any], compress: bool | None = None) -> tuple[str, str]:
    text = _home_to_tilde(dumps(bundle))
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
