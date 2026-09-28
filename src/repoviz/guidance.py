"""Standing guidance: notes on a component, a path or a qualified name that outlive one review (#20).

Reviewers repeat the same guidance wave after wave ("``app/storage`` must stay synchronous", "``legacy/`` is
frozen").  A review note belongs to one wave; guidance belongs to an area of the code:

* **Store.** ``guidance.json`` in the repository's state directory (shared by its worktrees, like the parse cache;
  never in the repository), owner-only permissions.  Each entry: ``id``, ``selector``, ``text``, ``kind`` (``rule``,
  ``context`` or ``frozen``), ``author``, ``created_at``, ``valid_from_sha`` and, once retired, ``valid_until_sha``
  and ``retired_at``.  Retiring keeps the entry, so a past wave's review shows what was valid then.
* **Selectors.** ``component:<name>`` (the review's component), ``path:<glob>`` (a directory matches everything in
  it), ``symbol:<qualified name>`` (a module, or a class or function in it, and what is inside).  Without a
  prefix, a text with ``/`` or a wildcard is a path, a dotted name a symbol, and a bare word matches a component, a
  top-level directory or a top-level module of that name.
* **Where it shows.** The file cards of matching files, the ``guidance-frozen-touched`` signal (a ``frozen``
  entry and a changed file), and the feedback prompt ("Reminder for ``app/storage``: must stay synchronous.")
  whenever a matching file has notes or signals.
* **Export and import.** A Markdown section to paste into ``AGENTS.md`` / ``CLAUDE.md`` (grouped by selector), or
  JSON; either loads back with :func:`import_entries` (entries already there are not duplicated).

Nothing here reads or runs repository code: matching uses paths, component names and qualified names.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from . import globs
from .pipeline import utcnow
from .redact import redact
from .session import _atomic_write

if TYPE_CHECKING:  # pragma: no cover
    from .repo import Repository

#: A bare name with one of these extensions is a file (``setup.py``), not a qualified name.
_FILE = re.compile(r"\.(py|pyi|js|jsx|mjs|cjs|ts|tsx|go|rs|java|kt|rb|php|c|h|cc|cpp|hpp|cs|swift|md|rst|txt|toml|"
                   r"json|ya?ml|cfg|ini|sh|sql|html|css|lock)$", re.IGNORECASE)
KINDS = ("rule", "context", "frozen")
KIND_LABEL = {"rule": "Rule", "context": "Context", "frozen": "Frozen"}
FILE_NAME = "guidance.json"
MAX_ENTRIES = 500
MAX_TEXT = 2000
MAX_SELECTOR = 300
MARKER = "<!-- repoviz guidance v1 -->"


class GuidanceError(ValueError):
    """An invalid entry or file."""


# --------------------------------------------------------------------------- selectors


def parse_selector(text: str) -> tuple[str, str]:
    """``(kind, value)``: kind ``component``, ``path``, ``symbol`` or ``name`` (a bare word)."""
    raw = (text or "").strip()
    for kind in ("component", "path", "symbol"):
        if raw.startswith(kind + ":"):
            return kind, raw[len(kind) + 1:].strip()
    if "/" in raw or any(c in raw for c in "*?[") or _FILE.search(raw):
        return "path", raw
    if "." in raw:
        return "symbol", raw
    return "name", raw


def normalize_selector(text: str) -> str:
    kind, value = parse_selector(text)
    if not value:
        raise GuidanceError("the selector is empty: a component, a path glob or a qualified name")
    return value if kind == "name" else f"{kind}:{value}"


def label_of(selector: str) -> str:
    """What the selector names, for people: ``app/storage/**``, ``app.storage.save``, ``component storage``."""
    kind, value = parse_selector(selector)
    return f"component {value}" if kind == "component" else value


def _under(name: str, prefix: str) -> bool:
    return bool(name) and (name == prefix or name.startswith(prefix + "."))


def matches(selector: str, facts: dict[str, Any]) -> bool:
    """Whether a changed file (``facts``: ``path``, ``component``, ``component_id``, ``module`` and the qualified
    ``symbols`` it changed) falls under ``selector``.  ``paths``, when given, replaces ``path`` and
    ``previous_path`` for path selectors (a folder passes its path and one inside it, as the web app does)."""
    kind, value = parse_selector(selector)
    if not value:
        return False
    path = str(facts.get("path") or "")
    previous = str(facts.get("previous_path") or "")
    module = str(facts.get("module") or "")
    symbols = [str(s) for s in facts.get("symbols") or []]
    if kind == "component":
        return value in (facts.get("component"), facts.get("component_id"))
    if kind == "path":
        return any(p and globs.match(p, value) for p in (facts.get("paths") or (path, previous)))
    if kind == "symbol":  # the module (or package) itself, or a changed class or function under the name
        return _under(module, value) or any(_under(s, value) for s in symbols)
    # a bare word: a component, a top-level directory or a top-level module of that name
    return value == facts.get("component") or path.split("/", 1)[0] == value and "/" in path or \
        module.split(".", 1)[0] == value


# --------------------------------------------------------------------------- store


def _file(repo: "Repository") -> Path:
    return Path(repo.parse_cache_dir) / FILE_NAME  # the repository's state directory, shared by its worktrees


def load(repo: "Repository") -> list[dict[str, Any]]:
    try:
        data = json.loads(_file(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return [e for e in entries or [] if isinstance(e, dict) and e.get("id") and e.get("selector")][:MAX_ENTRIES]


def save(repo: "Repository", entries: list[dict[str, Any]]) -> None:
    _atomic_write(_file(repo), json.dumps({"version": 1, "updated_at": utcnow(), "entries": entries}, indent=1))


def stamp(repo: "Repository") -> tuple[int, int]:
    """Size and modification time of the store: part of a cached review's key."""
    try:
        st = _file(repo).stat()
    except OSError:
        return (0, 0)
    return (st.st_size, st.st_mtime_ns)


def _clean(selector: Any, text: Any, kind: Any) -> tuple[str, str, str]:
    if kind not in KINDS:
        raise GuidanceError(f"kind must be one of {', '.join(KINDS)}")
    if not isinstance(selector, str) or not isinstance(text, str):
        raise GuidanceError("selector and text must be strings")
    text = redact(" ".join(text.split()))[:MAX_TEXT]
    if not text:
        raise GuidanceError("the guidance text is empty")
    return normalize_selector(selector[:MAX_SELECTOR]), text, kind


def _head(repo: "Repository") -> str | None:
    return repo.git.head() if repo.git is not None else None


def add(repo: "Repository", selector: str, text: str, kind: str = "rule", author: str = "") -> dict[str, Any]:
    """A new active entry, valid from the current commit."""
    selector, text, kind = _clean(selector, text, kind)
    entries = load(repo)
    if sum(1 for e in entries if not e.get("valid_until_sha") and not e.get("retired_at")) >= MAX_ENTRIES:
        raise GuidanceError(f"at most {MAX_ENTRIES} active entries")
    from .verdict import default_reviewer

    entry = {"id": secrets.token_hex(6), "selector": selector, "text": text, "kind": kind,
             "author": redact(str(author or "").strip() or default_reviewer(repo))[:100], "created_at": utcnow(),
             "valid_from_sha": _head(repo)}
    entries.append(entry)
    save(repo, entries[-MAX_ENTRIES:])
    return entry


def edit(repo: "Repository", entry_id: str, *, selector: str | None = None, text: str | None = None,
         kind: str | None = None) -> dict[str, Any]:
    entries = load(repo)
    entry = next((e for e in entries if e["id"] == entry_id), None)
    if entry is None:
        raise GuidanceError(f"no guidance {entry_id!r}")
    if entry.get("retired_at"):
        raise GuidanceError("retired guidance cannot be edited; add a new entry")
    entry["selector"], entry["text"], entry["kind"] = _clean(
        entry["selector"] if selector is None else selector, entry["text"] if text is None else text,
        entry["kind"] if kind is None else kind)
    entry["edited_at"] = utcnow()
    save(repo, entries)
    return entry


def retire(repo: "Repository", entry_id: str) -> dict[str, Any]:
    """Retired, not deleted: valid until the current commit, so past waves keep showing it."""
    entries = load(repo)
    entry = next((e for e in entries if e["id"] == entry_id), None)
    if entry is None:
        raise GuidanceError(f"no guidance {entry_id!r}")
    if not entry.get("retired_at"):
        entry["retired_at"] = utcnow()
        entry["valid_until_sha"] = _head(repo)
        save(repo, entries)
    return entry


# --------------------------------------------------------------------------- validity


def valid_at(repo: "Repository", entries: Iterable[dict[str, Any]], sha: str | None) -> list[dict[str, Any]]:
    """The entries valid at commit ``sha`` (``None``: now, the working tree).  An entry is valid from the commit it
    was added at to the commit it was retired at, both included (a wave that ended there was reviewed under it);
    when Git cannot tell (another history, missing objects), an active entry counts and a retired one does not."""
    out = []
    git = repo.git
    memo: dict[tuple[str, str], bool | None] = {}  # entries added at the same commit share one Git call

    def ancestor(a: str, b: str) -> bool | None:
        if (a, b) not in memo:
            memo[(a, b)] = _ancestor(git, a, b)
        return memo[(a, b)]

    for e in entries:
        retired = bool(e.get("retired_at"))
        if sha is None or git is None:
            if not retired:
                out.append(e)
            continue
        start, until = e.get("valid_from_sha"), e.get("valid_until_sha")
        if start and ancestor(start, sha) is False:
            continue  # added after this state
        if retired and sha != until and (not until or ancestor(until, sha) is not False):
            continue  # retired before this state (or unknown)
        out.append(e)
    return out


def _ancestor(git: Any, a: str, b: str) -> bool | None:
    """Whether commit ``a`` is ``b`` or one of its ancestors; ``None`` when Git cannot tell."""
    return True if a == b else git.is_ancestor(a, b)


def shown(repo: "Repository", entries: list[dict[str, Any]], where: str) -> list[dict[str, Any]]:
    """Entries as ``where`` may show them: author names only where ``[privacy] show_authors`` allows."""
    from .metrics import show_authors

    if show_authors(repo.config, where):
        return entries
    return [{k: v for k, v in e.items() if k != "author"} for e in entries]


# --------------------------------------------------------------------------- export / import


def export_markdown(entries: list[dict[str, Any]]) -> str:
    """The active entries as a Markdown section for ``AGENTS.md`` / ``CLAUDE.md``, grouped by selector."""
    active = [e for e in entries if not e.get("retired_at")]
    lines = [MARKER, "## Architecture guidance", "",
             "Standing guidance from code review (repoviz). Follow it when you change these areas.", ""]
    if not active:
        lines.append("_No guidance yet._")
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in active:
        groups.setdefault(e["selector"], []).append(e)
    for selector in sorted(groups, key=lambda s: (parse_selector(s)[0] != "component", s)):
        lines += [f"### `{selector}`", ""]
        for e in sorted(groups[selector], key=lambda x: (KINDS.index(x["kind"]), x["created_at"])):
            lines.append(f"- **{KIND_LABEL[e['kind']]}:** {e['text']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_HEADING = re.compile(r"^###\s+`([^`]+)`\s*$")
_ITEM = re.compile(r"^[-*]\s+\*\*(Rule|Context|Frozen):\*\*\s+(.+?)\s*$", re.IGNORECASE)


def parse_export(text: str) -> list[dict[str, str]]:
    """Entries (``selector``, ``kind``, ``text``) from :func:`export_markdown` or its JSON form."""
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise GuidanceError(f"not valid JSON: {exc}") from None
        items = data.get("entries") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise GuidanceError("expected a list of entries")
        return [{"selector": str(e.get("selector") or ""), "kind": str(e.get("kind") or "rule"),
                 "text": str(e.get("text") or ""), **({"retired": True} if e.get("retired_at") else {})}
                for e in items if isinstance(e, dict)]
    out: list[dict[str, str]] = []
    selector = None
    for line in text.splitlines():
        m = _HEADING.match(line.strip())
        if m:
            selector = m.group(1)
            continue
        m = _ITEM.match(line.strip())
        if m and selector:
            out.append({"selector": selector, "kind": m.group(1).lower(), "text": m.group(2)})
    if not out and MARKER not in text:
        raise GuidanceError("no guidance found (expected the Markdown of `repoviz guidance export` or its JSON)")
    return out


def import_entries(repo: "Repository", text: str, author: str = "") -> dict[str, int]:
    """Load exported guidance back: new entries are added, ones already active (same selector, kind and text)
    are kept as they are.  Retired entries of a JSON export are skipped."""
    items = parse_export(text)
    have = {(e["selector"], e["kind"], e["text"]) for e in load(repo) if not e.get("retired_at")}
    added = skipped = 0
    for item in items:
        if item.get("retired"):
            skipped += 1
            continue
        selector, body, kind = _clean(item["selector"], item["text"], item["kind"])
        if (selector, kind, body) in have:
            skipped += 1
            continue
        add(repo, selector, body, kind, author=author)
        have.add((selector, kind, body))
        added += 1
    return {"added": added, "skipped": skipped}


def for_review(entries: list[dict[str, Any]], files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entries that apply to a review's files, each with the files it matched (review.py adds them to the
    file cards, the frozen signal and the prompt)."""
    out = []
    for e in entries:
        hit = [f["path"] for f in files if matches(e["selector"], f.get("_facts") or f)]
        if hit:
            out.append({k: e.get(k) for k in ("id", "selector", "text", "kind", "created_at", "retired_at")}
                       | {"label": label_of(e["selector"]), "files": hit})  # no author: reviews go to reports and CI
    return out
