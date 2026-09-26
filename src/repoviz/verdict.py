"""The reviewer's verdict on a wave, and the gate an agent checks before pushing.

A review ends with a **verdict**: approve, request changes or reject, with a summary.  It is tied to the exact
state reviewed by a *fingerprint* of the reviewed changes (every changed path with its content on both sides),
so it goes **stale** when the files change afterwards, while committing the same changes inside a session, or
ending the session, keeps it.

* ``repoviz review --wait`` blocks until the reviewer submits a verdict in the live app (it polls the state
  file), then prints it with the notes and the feedback prompt; exit 0 approve, 2 request changes, 3 reject,
  4 timeout.
* ``repoviz gate`` exits non-zero unless a fresh verdict meets the requirement and, optionally, every file is
  marked reviewed and no untriaged signal at or above a severity is left.

Verdicts live in the state directory next to the review's notes; nothing is written to the repository.  The gate
is a workflow guard, not a security boundary: whatever runs as the user can write the state directory.
"""

from __future__ import annotations

import getpass
import time
from typing import TYPE_CHECKING, Any, Callable

from .ids import content_hash, stable_hash
from .redact import redact
from .session import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from .repo import Repository
    from .review import ReviewTarget
    from .session import StateStore
    from .sources import TreeSource

VERDICTS = ("approve", "request-changes", "reject")
LABELS = {"approve": "Approved", "request-changes": "Changes requested", "reject": "Rejected"}
EXIT_CODES = {"approve": 0, "request-changes": 2, "reject": 3}
EXIT_TIMEOUT = 4
REQUIREMENTS = ("approve", "any")
STALE = "verdict is stale: files changed since review"
MAX_SUMMARY = 20_000
MAX_PROMPT = 200_000
MAX_NOTES = 500
MAX_HISTORY = 20
MAX_LISTED = 50
MAX_SUBMODULE_FILES = 1000
_NOTE_FIELDS = ("path", "line", "side", "symbol", "verdict", "finding_id")


# --------------------------------------------------------------------------- the state reviewed


def fingerprint(base: "TreeSource", target: "TreeSource", changed: list[str] | None = None) -> str:
    """Identity of the changes between two states: each changed path with its content on both sides, moved
    submodule pointers and uncommitted work inside submodules.  The same changes give the same fingerprint
    whatever the states are called.  ``changed`` (sorted) saves recomputing the changed paths."""
    if changed is None:
        changed = sorted(p for p in set(base.files()) | set(target.files())
                         if base.content_hash(p) != target.content_hash(p))
    parts = [f"{p}\0{base.content_hash(p) or '-'}\0{target.content_hash(p) or '-'}" for p in changed]
    sa, sb = base.submodule_commits(), target.submodule_commits()
    parts += [f"{p}\0{sa.get(p) or '-'}\0{sb.get(p) or '-'}" for p in sorted(set(sa) | set(sb)) if sa.get(p) != sb.get(p)]
    for side, src in (("base", base), ("target", target)):
        for sub in src.submodules:
            for rel in src.submodule_dirty(sub)[:MAX_SUBMODULE_FILES]:
                data = src.submodule_file(sub, rel)
                parts.append(f"{side}\0{sub}/{rel}\0{content_hash(data) if data is not None else '-'}")
    return stable_hash("reviewed-changes", *parts, length=20)


def current(repo: "Repository", target: "ReviewTarget", sources: tuple[Any, Any] | None = None) -> dict[str, str]:
    """The state of ``target`` now: its fingerprint and the revision ids of both ends."""
    base, head = sources or (repo.open_source(target.base), repo.open_source(target.target))
    return {"fingerprint": fingerprint(base, head), "base_revision_id": base.revision_id,
            "head_revision_id": head.revision_id}


# --------------------------------------------------------------------------- storage


def default_reviewer(repo: "Repository") -> str:
    name = (repo.git.try_run("config", "--get", "user.name") or "").strip() if repo.git else ""
    if not name:
        try:
            name = getpass.getuser()
        except (OSError, KeyError, ImportError):
            name = ""
    return name


def compact_notes(notes: list[Any]) -> list[dict[str, Any]]:
    """The reviewer's notes as the agent needs them: where (path, line, symbol), the kind of note and its text."""
    out = []
    for n in notes[:MAX_NOTES]:
        if not isinstance(n, dict):
            continue
        item: dict[str, Any] = {k: n[k] for k in _NOTE_FIELDS if n.get(k) not in (None, "")}
        for k in ("path", "side", "symbol", "verdict", "finding_id"):
            if k in item:
                item[k] = str(item[k])[:500]
        if "line" in item and not isinstance(item["line"], int):
            item.pop("line")
        item["text"] = redact(str(n.get("comment") or ""))[:4000]
        out.append(item)
    return out


def _brief(v: dict[str, Any]) -> dict[str, Any]:
    return {k: v.get(k) for k in ("verdict", "reviewer", "at", "fingerprint")} | {"summary": (v.get("summary") or "")[:300]}


def record(repo: "Repository", target: "ReviewTarget", verdict: str, *, summary: str = "", reviewer: str = "",
           state: dict[str, str] | None = None, notes: list[Any] | None = None, prompt: str = "",
           origin: str = "page") -> dict[str, Any]:
    """Store a verdict on ``target`` for the state the reviewer saw (``state``: its fingerprint and revision ids;
    default: the state now).  The previous verdicts are kept, briefly, as its history."""
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r} (use {', '.join(VERDICTS)})")
    store = repo.state
    state = state or current(repo, target)
    previous = store.load_verdict(target.key)
    history = ((previous.get("history") or [])[-(MAX_HISTORY - 1):] + [_brief(previous)]) if previous else []
    v = {"key": target.key, "verdict": verdict, "label": LABELS[verdict],
         "target": {"id": target.id, "label": target.label, "kind": target.kind, "base": target.base,
                    "target": target.target, "session_id": target.session_id},
         "summary": redact(str(summary or "").strip())[:MAX_SUMMARY],
         "reviewer": redact(str(reviewer or "").strip() or default_reviewer(repo))[:100],
         "at": utcnow(), "ts": time.time(), "origin": origin,
         "fingerprint": str(state.get("fingerprint") or ""), "base_revision_id": str(state.get("base_revision_id") or ""),
         "head_revision_id": str(state.get("head_revision_id") or ""),
         "head_commit": repo.git.head() if repo.git else None,
         "notes": compact_notes(store.load_notes(target.key) if notes is None else notes),
         "prompt": redact(str(prompt or ""))[:MAX_PROMPT], "history": history}
    store.save_verdict(target.key, v)
    return v


def view(v: dict[str, Any] | None, fingerprint_now: str | None) -> dict[str, Any] | None:
    """What the page shows: the verdict without its stored prompt, and whether it is stale."""
    if not v:
        return None
    out = {k: val for k, val in v.items() if k != "prompt"}
    out["stale"] = fingerprint_now is not None and v.get("fingerprint") != fingerprint_now
    return out


# --------------------------------------------------------------------------- waiting and gating


def wait(store: "StateStore", key: str, since: float, timeout: float | None, *, poll: float = 0.5,
         sleep: Callable[[float], None] = time.sleep) -> dict[str, Any] | None:
    """Poll the verdict file until a verdict newer than ``since`` (a ``time.time()``) arrives; ``None`` after
    ``timeout`` seconds (``None`` or 0: no limit)."""
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        v = store.load_verdict(key)
        try:
            newer = v is not None and float(v.get("ts") or 0) > since
        except (TypeError, ValueError):
            newer = False
        if newer:
            return v
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            sleep(min(poll, left))
        else:
            sleep(poll)


def parse_duration(text: str) -> float:
    """``90``, ``90s``, ``30m``, ``2h`` → seconds (0: no limit)."""
    t = str(text).strip().lower()
    scale = {"s": 1, "m": 60, "h": 3600}.get(t[-1:], None)
    try:
        value = float(t[:-1] if scale else t) * (scale or 1)
    except ValueError:
        raise ValueError(f"invalid duration {text!r} (e.g. 90s, 30m, 2h)") from None
    if value < 0:
        raise ValueError(f"invalid duration {text!r}")
    return value


def triaged_ids(notes: list[dict[str, Any]]) -> set[str]:
    """Findings the reviewer handled: a note refers to them (sent to the agent, commented or "not an issue")."""
    return {str(n.get("finding_id")) for n in notes if isinstance(n, dict) and n.get("finding_id")}


def gate(repo: "Repository", target: "ReviewTarget", *, require: str = "approve", all_reviewed: bool = False,
         max_open: str | None = None, report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Whether work on ``target`` may go further (a push): ``{"ok", "reasons", ...}``.

    Closed unless a verdict exists, is fresh (the files did not change since) and meets ``require``
    (``approve``, or ``any`` verdict); with ``all_reviewed``, every changed file must be marked reviewed at its
    current version; with ``max_open`` (a severity), no signal at or above it may be left without a note."""
    from .review import SEVERITY_ORDER, build_review, finding_aliases

    if require not in REQUIREMENTS:
        raise ValueError(f"unknown requirement {require!r} (use {', '.join(REQUIREMENTS)})")
    store = repo.state
    v = store.load_verdict(target.key)
    now = current(repo, target)
    reasons: list[str] = []
    stale = bool(v) and v.get("fingerprint") != now["fingerprint"]
    if v is None:
        reasons.append(f"no verdict on “{target.label}”: ask the human to review it (repoviz review --wait)")
    else:
        if stale:
            reasons.append(STALE)
        if require == "approve" and v["verdict"] != "approve":
            reasons.append(f"the verdict is “{LABELS.get(v['verdict'], v['verdict'])}”"
                           + (f" ({v['summary'][:200]})" if v.get("summary") else "") + "; approval is required")
    unreviewed: list[str] = []
    open_findings: list[dict[str, Any]] = []
    if all_reviewed or max_open:
        report = report or build_review(repo, target)
        if all_reviewed:
            marks = store.load_reviewed(target.key)
            unreviewed = [f["path"] for f in report["files"] if marks.get(f["path"]) != f.get("version")]
            if unreviewed:
                reasons.append(f"{len(unreviewed)} of {len(report['files'])} file(s) not marked reviewed: "
                               + ", ".join(unreviewed[:5]) + (" …" if len(unreviewed) > 5 else ""))
        if max_open:
            limit = SEVERITY_ORDER[max_open]
            done = triaged_ids(store.load_notes(target.key))
            open_findings = [f for f in report["findings"] if SEVERITY_ORDER.get(f["severity"], 9) <= limit
                             and not finding_aliases(f) & done]
            if open_findings:
                reasons.append(f"{len(open_findings)} untriaged signal(s) at or above {max_open}: "
                               + "; ".join(f"{f['title']}" + (f" ({f['path']})" if f.get("path") else "")
                                           for f in open_findings[:3]) + (" …" if len(open_findings) > 3 else ""))
    return {"ok": not reasons, "reasons": reasons,
            "target": {"id": target.id, "label": target.label, "key": target.key},
            "require": require, "verdict": view(v, now["fingerprint"]), "stale": stale, **now,
            "unreviewed": unreviewed[:MAX_LISTED], "unreviewed_count": len(unreviewed),
            "open": [{k: f.get(k) for k in ("id", "kind", "severity", "title", "path", "line")}
                     for f in open_findings[:MAX_LISTED]], "open_count": len(open_findings)}


def result(v: dict[str, Any] | None, target: "ReviewTarget", *, fingerprint_now: str | None, waited: bool,
           prompt: str = "", url: str = "") -> dict[str, Any]:
    """What ``review --wait`` prints: the verdict, its summary, the reviewer's notes and the feedback prompt."""
    base = {"target": {"id": target.id, "label": target.label, "key": target.key}, "waited": waited}
    if url:
        base["url"] = url
    if v is None:
        return {**base, "verdict": None, "timeout": True, "exit_code": EXIT_TIMEOUT}
    return {**base, "verdict": v["verdict"], "label": LABELS.get(v["verdict"], v["verdict"]),
            "exit_code": EXIT_CODES[v["verdict"]], "summary": v.get("summary") or "", "reviewer": v.get("reviewer") or "",
            "at": v.get("at"), "stale": fingerprint_now is not None and v.get("fingerprint") != fingerprint_now,
            "notes": v.get("notes") or [], "prompt": prompt or v.get("prompt") or ""}


def format_result(res: dict[str, Any]) -> str:
    """``review --wait`` as text."""
    if res["verdict"] is None:
        return f"No verdict on “{res['target']['label']}” before the timeout.\n"
    lines = [f"Verdict: {res['label']}" + (f" by {res['reviewer']}" if res.get("reviewer") else "") + f" ({res['at']})",
             f"Review: {res['target']['label']}"]
    if res.get("stale"):
        lines.append("Note: files changed after the reviewer loaded this review, so the verdict is stale.")
    if res.get("summary"):
        lines += ["", res["summary"]]
    if res.get("prompt") and res["verdict"] != "approve":
        lines += ["", res["prompt"].rstrip()]
    elif res.get("notes"):
        lines += ["", "Notes:"] + [f"- {n.get('path') or 'general'}{':' + str(n['line']) if n.get('line') else ''}: "
                                   f"{n.get('text') or n.get('verdict')}" for n in res["notes"]]
    return "\n".join(lines) + "\n"


def format_gate(res: dict[str, Any]) -> str:
    label = res["target"]["label"]
    if res["ok"]:
        v = res["verdict"] or {}
        who = f" by {v['reviewer']}" if v.get("reviewer") else ""
        return f"repoviz gate: open: “{label}” {LABELS.get(v.get('verdict', ''), 'reviewed').lower()}{who} ({v.get('at')})\n"
    lines = [f"repoviz gate: closed for “{label}”:"] + [f"  - {r}" for r in res["reasons"]]
    lines.append("Ask the human to review first: run `repoviz review --wait` and wait for the verdict.")
    return "\n".join(lines) + "\n"
