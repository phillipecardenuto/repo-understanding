"""Expected changes: compare what the plan said the agent would change with what it changed.

Scope has *allowed* and *protected* globs; neither catches the opposite failure, work the agent did **not** do
("update the client", "add a migration").  A session (or one review) can list **expectations**:

* a path glob: ``app/routes/reports.py``, ``src/billing/**``, ``docs/`` (a directory means everything under it);
* a symbol: ``symbol:app.services.images.list_images`` (satisfied only when it is in the wave's key changes,
  added or modified);
* a kind: ``test`` (some test file added or modified), ``migration``, ``docs``, ``changelog``.

:func:`check` marks each one done (with what matched) or missing, and, when the plan names files or symbols,
lists large changes the plan did not mention.  :func:`parse_plan` extracts expectations from a Markdown plan:
backticked paths and dotted names it can resolve, and list items about tests, migrations, docs or the changelog;
what it cannot resolve is returned as *unresolved*, never dropped silently.  Text only: nothing is executed.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any, Iterable

from . import globs

KINDS = {"test": "test", "tests": "test", "migration": "migration", "migrations": "migration", "docs": "docs",
         "doc": "docs", "documentation": "docs", "changelog": "changelog"}
KIND_LABEL = {"test": "a test file", "migration": "a database migration", "docs": "the documentation",
              "changelog": "the changelog"}
_MIGRATION = re.compile(r"(?i)(^|/)(migrations?|migrate|alembic/versions|db/migrate|schema_migrations)(/|$)")
_DOCS = re.compile(r"(?i)((^|/)docs?/|\.(md|rst|adoc|txt)$)")
_CHANGELOG = re.compile(r"(?i)(^|/)(changelog|changes|history|news|release[-_]?notes)(\.[a-z]+)?$|(^|/)changelog\.d/")
UNPLANNED_LINES = 50
MAX_EXPECTATIONS = 200


def parse_expectation(text: str) -> dict[str, str] | None:
    """``{"raw", "kind", "value"}`` with kind ``path`` | ``symbol`` | a kind shortcut; ``None`` for an empty entry."""
    raw = " ".join(str(text or "").split())
    if not raw:
        return None
    low = raw.lower()
    if low in KINDS:
        return {"raw": raw, "kind": KINDS[low], "value": KINDS[low]}
    if low.startswith("kind:") and low[5:] in KINDS:
        return {"raw": raw, "kind": KINDS[low[5:]], "value": KINDS[low[5:]]}
    if low.startswith("symbol:"):
        return {"raw": raw, "kind": "symbol", "value": raw[7:].strip().removesuffix("()")}
    value = raw.removeprefix("./")
    if value.endswith("/"):
        value += "**"
    return {"raw": raw, "kind": "path", "value": value}


def normalize(entries: Iterable[str]) -> list[str]:
    """Split comma / newline separated entries, drop blanks and duplicates, keep the order."""
    out: list[str] = []
    for entry in entries:
        for part in re.split(r"[\n,]", str(entry or "")):
            part = " ".join(part.split())
            if part and part not in out:
                out.append(part)
    return out[:MAX_EXPECTATIONS]


def _kind_of(path: str, is_test: bool) -> set[str]:
    kinds = set()
    if is_test:
        kinds.add("test")
    if _MIGRATION.search(path):
        kinds.add("migration")
    if _CHANGELOG.search(path):
        kinds.add("changelog")
    elif _DOCS.search(path):
        kinds.add("docs")
    return kinds


def check(expected: list[str], files: list[dict[str, Any]], *, planned: bool | None = None) -> dict[str, Any]:
    """Each expectation as done (with the files or symbols that match) or missing, and the large unplanned files.

    ``files`` are review file entries (``path``, ``previous_path``, ``status``, ``is_test``, ``symbols``,
    ``lines_added`` / ``lines_removed``).  ``planned`` (default: the plan names a file or a symbol) turns on the
    list of files over :data:`UNPLANNED_LINES` changed lines that no expectation covers.
    """
    items = [e for e in (parse_expectation(x) for x in expected) if e]
    covered: set[str] = set()
    out = []
    for e in items:
        matches: list[str] = []
        if e["kind"] == "path":
            for f in files:
                paths = [f["path"]] + ([f["previous_path"]] if f.get("previous_path") else [])
                if any(globs.match(p, e["value"]) or p == e["value"] for p in paths):
                    matches.append(f["path"])
        elif e["kind"] == "symbol":
            want = e["value"]
            for f in files:
                for s in f.get("symbols") or []:
                    qn = s.get("qualified_name") or ""
                    if s.get("status") in ("added", "modified", "renamed") and (qn == want or qn.endswith("." + want)):
                        matches.append(f"{qn} ({f['path']})")
                        covered.add(f["path"])
        else:
            for f in files:
                # a test is code in a test location (not tests/README.md or tests/test.env)
                is_test = bool(f.get("is_test") and f.get("code", bool(f.get("language"))))
                if f.get("status") != "removed" and e["kind"] in _kind_of(f["path"], is_test):
                    matches.append(f["path"])
        if e["kind"] != "symbol":
            covered.update(m for m in matches)
        out.append({**e, "status": "done" if matches else "missing", "matches": matches[:20],
                    "more": max(0, len(matches) - 20)})
    if planned is None:
        planned = any(e["kind"] in ("path", "symbol") for e in items)
    unplanned = []
    if planned:
        for f in files:
            lines = (f.get("lines_added") or 0) + (f.get("lines_removed") or 0)
            if f["path"] not in covered and lines > UNPLANNED_LINES:
                unplanned.append({"path": f["path"], "lines": lines, "status": f.get("status")})
    unplanned.sort(key=lambda u: -u["lines"])
    return {"expected": out, "done": sum(1 for e in out if e["status"] == "done"),
            "missing": sum(1 for e in out if e["status"] == "missing"), "unplanned": unplanned[:50],
            "unplanned_lines": UNPLANNED_LINES}


# --------------------------------------------------------------------------- Markdown plans

_TICK = re.compile(r"`([^`\n]{1,200})`")
_EXT = (r"py|pyi|js|jsx|mjs|cjs|ts|tsx|vue|svelte|go|rs|java|kt|kts|scala|rb|php|cs|swift|c|h|cc|cpp|hpp|m|"
        r"md|rst|adoc|txt|toml|json|ya?ml|cfg|ini|env|sql|html|css|scss|sh|lock|proto|graphql|tf|dockerfile")
_PATHLIKE = re.compile(r"^(?:\./)?[\w.@+-]+(?:/[\w.@*+-]+)*/?$")
_HAS_EXT = re.compile(rf"(?i)\.({_EXT})$")
_DOTTED = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?:\(\))?$")
_NEW = re.compile(r"(?i)\b(create|creates|new|add|adds|introduce|scaffold)\b")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*)$")
_KIND_WORDS = [("test", re.compile(r"(?i)\b(tests?|unit[- ]tests?|pytest|spec)\b")),
               ("migration", re.compile(r"(?i)\bmigrations?\b")),
               ("changelog", re.compile(r"(?i)\bchange ?log\b")),
               ("docs", re.compile(r"(?i)\b(docs|documentation|readme)\b"))]


def parse_plan(text: str, files: Iterable[str], symbols: Iterable[str], modules: dict[str, str] | None = None,
               ) -> dict[str, Any]:
    """Expectations from a Markdown plan.

    ``files`` are the repository's paths (to confirm a path exists), ``symbols`` the qualified names of its
    functions and classes, ``modules`` qualified module name → path.  Returns ``{"expected": [...],
    "unresolved": [{"line", "text", "reason"}]}``."""
    file_set = set(files)
    dirs = {posixpath.dirname(f) for f in file_set}
    dirs |= {d for f in list(dirs) for d in _parents(f)}
    symbol_set = set(symbols)
    by_suffix: dict[str, list[str]] = {}
    for s in symbol_set:
        parts = s.split(".")
        for i in range(1, len(parts)):
            by_suffix.setdefault(".".join(parts[i:]), []).append(s)
    modules = modules or {}
    expected: list[str] = []
    unresolved: list[dict[str, Any]] = []

    def add(entry: str) -> None:
        if entry not in expected:
            expected.append(entry)

    in_code = False
    for lineno, line in enumerate(str(text or "").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        for token in _TICK.findall(line):
            token = token.strip()
            if " " in token or not token:
                continue
            path = token.removeprefix("./")
            if "/" in path and _PATHLIKE.match(path) or _HAS_EXT.search(path) and _PATHLIKE.match(path):
                if path in file_set or path.rstrip("/") in dirs:
                    add(path.rstrip("/") + "/" if path.rstrip("/") in dirs and path not in file_set else path)
                elif _NEW.search(line):
                    add(path)  # a file the plan says to create
                else:
                    unresolved.append({"line": lineno, "text": token, "reason": "no such file (and not marked new)"})
                continue
            if _DOTTED.match(token):
                name = token.removesuffix("()")
                if name in symbol_set:
                    add(f"symbol:{name}")
                elif name in modules:
                    add(modules[name])
                elif len(by_suffix.get(name, [])) == 1:
                    add(f"symbol:{by_suffix[name][0]}")
                else:
                    reason = "ambiguous name" if by_suffix.get(name) else "no such function, class or module"
                    unresolved.append({"line": lineno, "text": token, "reason": reason})
        item = _LIST_ITEM.match(line)
        if item:
            for kind, pattern in _KIND_WORDS:
                if pattern.search(item.group(1)):
                    add(kind)
    return {"expected": expected[:MAX_EXPECTATIONS], "unresolved": unresolved[:100]}


def _parents(d: str) -> list[str]:
    out = []
    while d:
        out.append(d)
        d = posixpath.dirname(d)
    return out


def parse_plan_in(repo: Any, text: str) -> dict[str, Any]:
    """:func:`parse_plan` against the repository's working tree (its files, symbols and modules)."""
    snap = repo.snapshot("WORKTREE")
    files = repo.open_source("WORKTREE").files() if getattr(repo, "git", None) is not None else \
        [n.path for n in snap.nodes() if n.path]
    symbols = [n.qualified_name for n in snap.symbols if n.qualified_name]
    modules = {n.qualified_name: n.path for n in snap.modules if n.qualified_name and n.path}
    return parse_plan(text, files, symbols, modules)
