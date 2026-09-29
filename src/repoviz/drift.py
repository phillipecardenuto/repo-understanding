"""Architecture drift over time (#32): the same metrics at sampled points of history, and the jumps between them.

A point is a revision the Changes tab can compare: a tag, a commit of the first-parent history, or the start or end
of a recorded wave (``SESSION@id`` / ``SESSION-END@id``).  Each point is snapshotted like any revision (per-file parse
results come from the parse cache, so only files that changed between points are parsed again), measured, and its
metrics are kept in the state directory: a second run only measures new points.

Metrics leave test code out (its imports would dominate the cross-component counts):

* ``modules``: code modules;
* ``components``: components holding code;
* ``internal_edges``: import edges between modules of the repository;
* ``cross_component_edges``: those whose ends lie in different components, and ``component_links`` the component
  pairs they connect (``ui → db``);
* ``cycles`` and ``largest_cycle``: module-level import cycles and the size of the largest;
* ``contract_violations``: violations of today's architecture contracts (#16), so the past is judged by today's rules;
* ``external_packages``: third-party packages imported or declared (standard libraries aside);
* ``avg_instability``: the mean of fan-out / (fan-in + fan-out) over the modules that have either (#30).

A jump between two points scores 1 per component link that appeared or disappeared, plus 5 per component, cycle
or contract violation gained or lost, plus 1 per external package gained or lost.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from . import __version__
from .ids import stable_hash
from .model import REL_IMPORTS, RepositorySnapshot

MAX_POINTS = 12
MAX_LINKS = 400  # component pairs kept per point (for "ui → db appeared")
MAX_CACHE = 300  # measured points kept in the state directory
MODES = ("auto", "tags", "every", "waves")

#: (key, label, what a rise means) in display order.
METRICS = [
    ("modules", "Modules", "size"),
    ("components", "Components", "size"),
    ("internal_edges", "Internal dependencies", "coupling"),
    ("cross_component_edges", "Cross-component dependencies", "coupling"),
    ("cycles", "Cycles", "worse"),
    ("largest_cycle", "Largest cycle (modules)", "worse"),
    ("contract_violations", "Contract violations", "worse"),
    ("external_packages", "External packages", "size"),
    ("avg_instability", "Average instability", "coupling"),
]


class DriftError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


@dataclass
class Point:
    spec: str  # what is measured: a commit SHA, SESSION@id or SESSION-END@id
    label: str
    date: str | None
    kind: str  # tag | commit | head | wave-start | wave-end
    ref: str = ""  # what a comparison names: the tag, a short SHA, or the spec

    def to_dict(self) -> dict[str, Any]:
        return {"spec": self.spec, "ref": self.ref or self.spec, "label": self.label, "date": self.date,
                "kind": self.kind}


# --------------------------------------------------------------------------- sampling


def _spread(items: list[Any], limit: int) -> list[Any]:
    """At most ``limit`` items, evenly spread, the first and the last always kept."""
    if len(items) <= limit:
        return list(items)
    if limit <= 1:
        return items[-1:]
    step = (len(items) - 1) / (limit - 1)
    picked = sorted({round(i * step) for i in range(limit)})
    return [items[i] for i in picked]


def _iso(ts: str | int | None) -> str | None:
    if ts in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return str(ts)[:10]


def tag_points(git: Any, merged: str | None = "HEAD") -> list[Point]:
    """Tags that name commits, oldest first (by the commit's date); with ``merged``, only those in its history
    (one line of releases: a maintenance branch's tags would make the timeline jump back and forth)."""
    out = git.try_run("for-each-ref", "--format=%(refname:short)%09%(objecttype)%09%(objectname)%09%(*objecttype)"
                      "%09%(*objectname)", *([f"--merged={merged}"] if merged else []), "refs/tags") or ""
    commits: dict[str, list[str]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name, otype, oid, ptype, pid = parts
        sha = pid if otype == "tag" and ptype == "commit" else oid if otype == "commit" else None
        if sha:
            commits.setdefault(sha, []).append(name)
    if not commits:
        return []
    dates = {}
    shas = list(commits)
    for i in range(0, len(shas), 200):
        log = git.try_run("show", "-s", "--format=%H %ct", *shas[i:i + 200]) or ""
        for line in log.splitlines():
            sha, _, ts = line.partition(" ")
            if ts.isdigit():
                dates[sha] = int(ts)
    points = [Point(sha, ", ".join(sorted(names)), _iso(dates.get(sha)), "tag", sorted(names)[0])
              for sha, names in commits.items() if sha in dates]
    return sorted(points, key=lambda p: (dates.get(p.spec, 0), p.label))


def every_points(git: Any, days: int, limit: int) -> list[Point]:
    """One commit per ``days`` on the first-parent history, newest first back ``limit`` steps, returned oldest
    first: the most recent commit of each step."""
    out = git.try_run("log", "--first-parent", "--format=%H %ct", "HEAD") or ""
    points: list[Point] = []
    next_before: int | None = None
    for line in out.splitlines():
        sha, _, ts = line.partition(" ")
        if not ts.isdigit():
            continue
        t = int(ts)
        if next_before is None or t <= next_before:
            points.append(Point(sha, _iso(t) or sha[:10], _iso(t), "commit", sha[:12]))
            next_before = t - days * 86400
            if len(points) >= limit:
                break
    if points:
        points[0].kind = "head"
    return list(reversed(points))


def wave_points(repo: Any) -> list[Point]:
    """The start of the first recorded wave, then the end of every finished wave, oldest first."""
    sessions = sorted((s for s in repo.state.list_sessions() if not s.active), key=lambda s: (s.started_at, s.started_us))
    if not sessions:
        return []
    first = sessions[0]
    points = [Point(f"SESSION@{first.id}", f"before {first.label or first.id}", (first.started_at or "")[:10] or None,
                    "wave-start")]
    for s in sessions:
        points.append(Point(f"SESSION-END@{s.id}", s.label or s.id, (s.ended_at or "")[:10] or None, "wave-end"))
    return points


def sample(repo: Any, how: str = "auto", every_days: int = 7, limit: int = MAX_POINTS) -> dict[str, Any]:
    """``{"mode", "points", "available", "notes"}``: the points to measure (at most ``limit``)."""
    if how not in MODES:
        raise DriftError(f"unknown sampling {how!r} (use one of {', '.join(MODES)})")
    limit = max(2, min(int(limit), 50))
    every_days = max(1, min(int(every_days), 3650))
    git = repo.git
    notes: list[str] = []
    if how == "waves":
        points = wave_points(repo)
        if len(points) < 2:
            raise DriftError("no finished wave is recorded yet (see 'repoviz session start' / 'end')")
        mode = "waves"
    else:
        if git is None or git.head() is None:
            raise DriftError("drift needs a Git repository with commits")
        tags = tag_points(git) if how in ("auto", "tags") else []
        if how == "tags" or (how == "auto" and len(tags) >= 3):
            if not tags:
                raise DriftError("no tag in the history of HEAD (use --every 7d or --waves)")
            elsewhere = len(tag_points(git, None)) - len(tags)
            if elsewhere > 0:
                notes.append(f"{_plural(elsewhere, 'tag')} not in the history of HEAD left out (other release lines)")
            head = git.head()
            points = list(tags)
            if head and head not in {p.spec for p in points}:
                info = git.commit_info(head)
                points.append(Point(head, "HEAD", (info.date or "")[:10] if info else None, "head", head[:12]))
            mode = "tags"
        else:
            if how == "auto":
                notes.append(f"fewer than 3 tags: one commit every {every_days} days instead")
            points = every_points(git, every_days, limit)
            mode = "every"
    available = len(points)
    if len(points) > limit:
        points = _spread(points, limit)
        notes.append(f"{limit} of {available} points sampled, evenly spread (the first and the last kept)")
    if len(points) < 2:
        raise DriftError("fewer than two points to compare")
    return {"mode": mode, "every_days": every_days if mode == "every" else None, "points": points,
            "available": available, "notes": notes}


# --------------------------------------------------------------------------- metrics


def measure(snap: RepositorySnapshot, contracts: list[Any]) -> dict[str, Any]:
    """The drift metrics of one snapshot, plus what the jumps need (component links, cycle keys)."""
    idx = snap.node_index()

    def is_test(n: Any) -> bool:
        return "test" in n.tags

    modules = {n.id: n for n in snap.modules if not is_test(n)}
    comp_of = {mid: n.metadata.get("component_id") for mid, n in modules.items()}

    def comp_name(cid: str | None) -> str:
        node = idx.get(cid) if cid else None
        return (node.qualified_name or node.name) if node is not None else "(root)"

    internal = cross = 0
    links: set[tuple[str, str]] = set()
    for e in snap.dependency_edges:
        if e.relationship != REL_IMPORTS or not e.direct or e.metadata.get("test_only"):
            continue
        if e.source_id not in modules or e.target_id not in modules or e.source_id == e.target_id:
            continue
        internal += 1
        a, b = comp_of.get(e.source_id), comp_of.get(e.target_id)
        if a != b:
            cross += 1
            links.add((comp_name(a), comp_name(b)))
    cycles = [c for c in snap.cycles if c.level == "module"
              and not all(idx.get(m) is not None and is_test(idx[m]) for m in c.members)]
    cycle_keys = sorted({stable_hash("cycle", *sorted(idx[m].qualified_name or m if m in idx else m for m in c.members),
                                     length=12) for c in cycles})
    components = {comp_of[m] for m in modules if comp_of.get(m)}
    externals = sum(1 for n in snap.components if n.component_type == "external-package" and "stdlib" not in n.tags)
    inst = [float(m["instability"]) for n in modules.values()
            for m in [n.metadata.get("metrics") or {}] if m.get("instability") is not None]
    violations = 0
    if contracts:
        from .contracts import check

        violations = sum(len(r.violations) for r in check(snap, contracts))
    return {
        "metrics": {
            "modules": len(modules),
            "components": len(components),
            "internal_edges": internal,
            "cross_component_edges": cross,
            "cycles": len(cycles),
            "largest_cycle": max((len(c.members) for c in cycles), default=0),
            "contract_violations": violations,
            "external_packages": externals,
            "avg_instability": round(sum(inst) / len(inst), 2) if inst else None,
        },
        "links": sorted(f"{a} → {b}" for a, b in links)[:MAX_LINKS],
        "links_capped": len(links) > MAX_LINKS,
        "cycle_keys": cycle_keys,
    }


# --------------------------------------------------------------------------- jumps


def _plural(n: int, word: str) -> str:
    if abs(n) == 1:
        return f"{n} {word}"
    return f"{n} {word[:-1]}ies" if word.endswith("y") else f"{n} {word}s"


def _signed(n: int, word: str) -> str:
    return f"{'+' if n > 0 else '−'}{_plural(abs(n), word)}"


def segment(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """What changed from point ``a`` to point ``b``, a score and one sentence."""
    ma, mb = a["metrics"], b["metrics"]
    delta = {}
    for key, _label, _kind in METRICS:
        x, y = ma.get(key), mb.get(key)
        delta[key] = None if x is None or y is None else round(y - x, 2)
    la, lb = set(a.get("links") or []), set(b.get("links") or [])
    new_links, gone_links = sorted(lb - la), sorted(la - lb)
    ca, cb = set(a.get("cycle_keys") or []), set(b.get("cycle_keys") or [])
    new_cycles, resolved_cycles = len(cb - ca), len(ca - cb)

    def d(key: str) -> int:
        return int(delta.get(key) or 0)

    score = len(new_links) + len(gone_links) + 5 * (abs(d("components")) + new_cycles + resolved_cycles
                                                     + abs(d("contract_violations"))) + abs(d("external_packages"))
    parts = []
    if d("components"):
        parts.append(_signed(d("components"), "component"))
    if d("cross_component_edges") or new_links or gone_links:
        text = _signed(d("cross_component_edges"), "cross-component dependency") if d("cross_component_edges") \
            else "cross-component dependencies rewired"
        said = []
        if new_links:
            said.append(", ".join(new_links[:2]) + (f" and {len(new_links) - 2} more" if len(new_links) > 2 else "")
                        + " appeared")
        if gone_links:
            said.append(f"{_plural(len(gone_links), 'link')} gone")
        text += f" ({'; '.join(said)})" if said else ""
        parts.append(text)
    if new_cycles:
        parts.append(f"{_plural(new_cycles, 'new cycle')}")
    if resolved_cycles:
        parts.append(f"{_plural(resolved_cycles, 'cycle')} resolved")
    if d("contract_violations"):
        parts.append(_signed(d("contract_violations"), "contract violation"))
    if d("external_packages"):
        parts.append(_signed(d("external_packages"), "external package"))
    if d("modules") and not parts:
        parts.append(_signed(d("modules"), "module"))
    return {"delta": delta, "new_links": new_links[:20], "removed_links": gone_links[:20],
            "new_links_count": len(new_links), "removed_links_count": len(gone_links),
            "new_cycles": new_cycles, "resolved_cycles": resolved_cycles, "score": score,
            "summary": ", ".join(parts) if parts else "no architectural change"}


# --------------------------------------------------------------------------- the whole timeline


class _Cache:
    """Measured points in ``drift.json`` of the state directory (owner-only), keyed by revision and settings."""

    def __init__(self, repo: Any) -> None:
        self.path = repo.state.dir / "drift.json"
        self.lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text())
            self.items: dict[str, Any] = data.get("points", {}) if data.get("version") == __version__ else {}
        except (OSError, ValueError, AttributeError):
            self.items = {}
        self.dirty = False

    def get(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            return self.items.get(key)

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self.lock:
            self.items.pop(key, None)
            self.items[key] = value
            while len(self.items) > MAX_CACHE:
                self.items.pop(next(iter(self.items)))
            self.dirty = True

    def save(self) -> None:
        from .session import _atomic_write

        with self.lock:
            if not self.dirty:
                return
            try:
                _atomic_write(self.path, json.dumps({"version": __version__, "points": self.items}))
                self.dirty = False
            except OSError:
                pass  # a cache only


def settings_key(repo: Any) -> str:
    from .analyzers import analyzer_classes
    from .contracts import contracts_of

    return stable_hash("drift", __version__, repo.config.fingerprint(),
                       repr([(c.name, getattr(c, "version", "")) for c in analyzer_classes()]),
                       repr(contracts_of(repo.config)), length=16)


def compute(repo: Any, how: str = "auto", every_days: int = 7, limit: int = MAX_POINTS, *,
            progress: Callable[[int, int, str], None] | None = None,
            cancelled: Callable[[], bool] | None = None, sampled: dict[str, Any] | None = None,
            save_lock: Any = None) -> dict[str, Any]:
    """The drift document: sampled points with their metrics, the segments between them and the largest jump.
    ``progress(done, total, label)`` is called before each point; ``cancelled()`` stops between points;
    ``save_lock`` serializes the cache write with the server's other writes to the state directory."""
    from .contracts import contracts_of

    sampled = sampled or sample(repo, how, every_days, limit)
    points: list[Point] = sampled["points"]
    contracts = contracts_of(repo.config)
    cache = _Cache(repo)
    settings = settings_key(repo)
    measured: list[dict[str, Any]] = []
    fresh = 0
    try:
        for i, p in enumerate(points):
            if cancelled is not None and cancelled():
                raise Cancelled()
            if progress is not None:
                progress(i, len(points), p.label)
            source = repo.open_source(p.spec)
            key = stable_hash(settings, source.kind, source.revision_id, length=20)
            hit = cache.get(key)
            if hit is None:
                snap = repo.snapshot_of(source, p.label, keep=False)
                hit = measure(snap, contracts)
                cache.put(key, hit)
                fresh += 1
            measured.append({**p.to_dict(), **hit})
    finally:
        if save_lock is not None:
            with save_lock:
                cache.save()
        else:
            cache.save()
    if progress is not None:
        progress(len(points), len(points), "")
    segments = []
    for i in range(1, len(measured)):
        seg = segment(measured[i - 1], measured[i])
        seg.update({"from": i - 1, "to": i, "base": measured[i - 1]["ref"], "target": measured[i]["ref"],
                    "label": f"{measured[i - 1]['label']} → {measured[i]['label']}"})
        segments.append(seg)
    largest = max(range(len(segments)), key=lambda k: (segments[k]["score"], k)) if segments else None
    if largest is not None and segments[largest]["score"] == 0:
        largest = None
    for m in measured:
        m.pop("cycle_keys", None)
        m["links_count"] = len(m.pop("links", []) or [])
    return {"mode": sampled["mode"], "every_days": sampled.get("every_days"), "available": sampled["available"],
            "notes": sampled["notes"], "metrics": [{"key": k, "label": lab, "kind": kind} for k, lab, kind in METRICS],
            "points": measured, "segments": segments, "largest": largest, "measured_now": fresh,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


# --------------------------------------------------------------------------- output


def _fmt(v: Any) -> str:
    return "–" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))


def to_text(doc: dict[str, Any], markdown: bool = False) -> str:
    pts = doc["points"]
    how = {"tags": "tags", "every": f"one commit every {doc.get('every_days')} days", "waves": "waves"}[doc["mode"]]
    out = [f"{'## ' if markdown else ''}Architecture drift: {len(pts)} points ({how})"]
    out += [f"{'- ' if markdown else '  '}{n}" for n in doc.get("notes") or []]
    out.append("")
    labels = [p["label"] if len(p["label"]) <= 18 else p["label"][:17] + "…" for p in pts]
    if markdown:
        out.append("| Metric | " + " | ".join(labels) + " |")
        out.append("|---|" + "---|" * len(labels))
        for m in doc["metrics"]:
            out.append(f"| {m['label']} | " + " | ".join(_fmt(p["metrics"].get(m["key"])) for p in pts) + " |")
    else:
        width = max(len(m["label"]) for m in doc["metrics"])
        cols = [max(len(lab), 5) for lab in labels]
        out.append(" " * (width + 2) + "  ".join(lab.rjust(c) for lab, c in zip(labels, cols)))
        for m in doc["metrics"]:
            out.append(m["label"].ljust(width + 2) + "  ".join(_fmt(p["metrics"].get(m["key"])).rjust(c)
                                                            for p, c in zip(pts, cols)))
    out.append("")
    segs = doc["segments"]
    if doc.get("largest") is not None:
        s = segs[doc["largest"]]
        out.append(f"{'**' if markdown else ''}Largest jump: {s['label']}: {s['summary']}{'**' if markdown else ''}")
    else:
        out.append("No architectural jump between the sampled points.")
    ranked = sorted((s for s in segs if s["score"]), key=lambda s: -s["score"])
    if ranked:
        out.append("")
        out.append(f"{'### ' if markdown else ''}Jumps, largest first")
        for s in ranked[:10]:
            out.append(f"{'- ' if markdown else '  '}{s['label']}: {s['summary']} (score {s['score']})")
    return "\n".join(out) + "\n"


_DAYS = re.compile(r"(\d+)\s*([dwm]?)")


def parse_every(text: str) -> int:
    """``7d``, ``2w``, ``1m`` or ``10`` → days."""
    m = _DAYS.fullmatch((text or "").strip().lower())
    if not m:
        raise DriftError(f"cannot read {text!r} as a period (use 7d, 2w or 1m)")
    n = int(m.group(1))
    return max(1, n * {"": 1, "d": 1, "w": 7, "m": 30}[m.group(2)])
