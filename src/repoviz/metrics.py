"""Code health metrics: size, complexity, coupling, hotspots and ownership (#30).

Cheap, standard measures that say *what kind of file* an agent touched, so a reviewer knows how careful to be:

* ``sloc``: non-blank lines that are not only a comment.
* ``complexity``:

  - Python: McCabe cyclomatic complexity per function or method, from the AST (``1`` plus one for each ``if`` /
    ``elif`` / conditional expression, loop, ``except`` handler, ``match`` case, extra operand of ``and`` / ``or``,
    and ``for`` / ``if`` of a comprehension; ``else``, ``try``, ``finally`` and ``with`` add nothing).  Nested
    functions and classes are measured on their own.  A module's complexity is the sum over its functions plus
    the decision points of its module-level code; ``max_nesting`` is the deepest block nesting (``if`` / ``for`` /
    ``while`` / ``try`` / ``with``; an ``elif`` stays at its ``if``'s depth).  The Python analyzer counts them in
    the walk that already collects calls (``analyzers/python.py``), so they cost no second pass.
  - Other languages: Adam Tornhill's *whitespace complexity*, the sum of the indentation levels of the code lines
    (the unit is the file's smallest indentation; a tab is one level).  It tracks nesting and length.

* ``fan_in`` / ``fan_out``: modules that import this one (tests not counted) / modules it imports, and
  ``instability = fan_out / (fan_in + fan_out)``.
* ``hotspot``: complexity × change frequency (Tornhill).  Both are ranked, complexity within its kind
  (cyclomatic or whitespace), and the product is ranked again: ``hotspot_top`` is the share of modules at or
  above this one (``3`` = in the top 3 %).  Only modules changed at least twice in the churn window take part,
  and tests are not ranked.
* ``authors`` / ``owner_share``: distinct authors in the churn window and the share of the file's commits made by
  the most active one.  Names never enter a snapshot; :func:`owner_names` reads them for the live app, and only
  when ``[privacy] show_authors`` allows it.

Everything is read from text or Git history; nothing is run.
"""

from __future__ import annotations

import bisect
import math
from typing import Any, Iterable

#: Line-comment prefixes for SLOC, by language.
_COMMENTS = {"python": ("#",), "shell": ("#",), "ruby": ("#",), "yaml": ("#",), "toml": ("#",),
             "javascript": ("//", "/*", "*", "*/"), "typescript": ("//", "/*", "*", "*/"),
             "go": ("//", "/*", "*", "*/"), "rust": ("//", "/*", "*", "*/"), "java": ("//", "/*", "*", "*/"),
             "c": ("//", "/*", "*", "*/"), "cpp": ("//", "/*", "*", "*/"), "csharp": ("//", "/*", "*", "*/"),
             "kotlin": ("//", "/*", "*", "*/"), "swift": ("//", "/*", "*", "*/"), "php": ("//", "#", "/*", "*")}
MIN_HOT_COMMITS = 2  # as risk.MIN_HOT_COMMITS: a file committed once is not churn
SINGLE_OWNER_COMMITS = 3  # "single owner" needs this many commits by one author, and a team of at least two
BARS = 4


# --------------------------------------------------------------------------- text metrics


def sloc(lines: Iterable[str], language: str | None) -> int:
    prefixes = _COMMENTS.get(language or "", ("#", "//"))
    n = 0
    for line in lines:
        s = line.strip()
        if s and not s.startswith(prefixes):
            n += 1
    return n


def whitespace_complexity(lines: list[str], language: str | None) -> dict[str, int]:
    """Tornhill's whitespace complexity: the sum (and maximum) of the indentation levels of the code lines."""
    prefixes = _COMMENTS.get(language or "", ("#", "//"))
    widths: list[int] = []
    count = 0
    for line in lines:
        s = line.strip()
        if not s or s.startswith(prefixes):
            continue
        count += 1
        lead = line[:len(line) - len(line.lstrip())]
        widths.append(lead.count("\t") * 1000 + len(lead.replace("\t", "")))  # tabs: whole levels (below)
    unit = _indent_unit([w % 1000 for w in widths if w % 1000])
    levels = [w // 1000 + (w % 1000) // unit for w in widths]
    return {"sloc": count, "complexity": sum(levels), "max_nesting": max(levels, default=0)}


def _indent_unit(spaces: list[int]) -> int:
    """The file's indentation step: the smallest width that at least 5 % of the indented lines (and 2) use, so a
    stray odd line (an aligned continuation) does not shrink it."""
    if not spaces:
        return 4
    counts: dict[int, int] = {}
    for w in spaces:
        counts[w] = counts.get(w, 0) + 1
    need = max(2, len(spaces) // 20)
    common = [w for w, c in counts.items() if c >= need]
    return max(1, min(8, min(common) if common else min(spaces)))


def text_metrics(text: str, language: str | None) -> dict[str, Any]:
    """Metrics of a file the Python analyzer does not parse (JavaScript, TypeScript, Go…)."""
    m = whitespace_complexity(text.splitlines(), language)
    return {"sloc": m["sloc"], "complexity": m["complexity"], "complexity_kind": "whitespace",
            "max_nesting": m["max_nesting"]}


# --------------------------------------------------------------------------- ranking


def rank_shares(values: dict[str, float]) -> dict[str, float]:
    """Each key's share of the values at or below its own (the highest gets 1.0; ties share a rank)."""
    ordered = sorted(values.values())
    n = len(ordered)
    if not n:
        return {}
    return {k: bisect.bisect_right(ordered, v) / n for k, v in values.items()}


def top_shares(values: dict[str, float]) -> dict[str, int]:
    """Each key's place from the top as a percentage (1 = among the top 1 %), rounded up."""
    neg = sorted(-v for v in values.values())
    n = len(neg)
    return {k: max(1, math.ceil(100 * bisect.bisect_right(neg, -v) / n)) for k, v in values.items()} if n else {}


def hotspots(modules: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{path: {"hotspot_top", "hotspot_score"}}`` for modules with churn and complexity.

    ``modules`` maps a path to ``{"complexity", "complexity_kind", "commits"}``."""
    eligible = {p: m for p, m in modules.items()
                if (m.get("commits") or 0) >= MIN_HOT_COMMITS and (m.get("complexity") or 0) > 0}
    if not eligible:
        return {}
    churn = rank_shares({p: float(m["commits"]) for p, m in eligible.items()})
    comp: dict[str, float] = {}
    for kind in {m.get("complexity_kind") for m in eligible.values()}:
        comp.update(rank_shares({p: float(m["complexity"]) for p, m in eligible.items()
                                 if m.get("complexity_kind") == kind}))
    score = {p: round(churn[p] * comp[p], 4) for p in eligible}
    top = top_shares(score)
    return {p: {"hotspot_top": top[p], "hotspot_score": score[p]} for p in eligible}


def hotspot_level(top: int | None) -> int:
    """0-4 bars for a hotspot place: top 5 % = 4, 10 % = 3, 25 % = 2, any other hotspot candidate = 1."""
    if not top:
        return 0
    return 4 if top <= 5 else 3 if top <= 10 else 2 if top <= 25 else 1


def ownership_level(share: float | None) -> int:
    if not share:
        return 0
    return 4 if share >= 0.9 else 3 if share >= 0.75 else 2 if share >= 0.5 else 1


def bars(level: int) -> str:
    """``▮▮▮▯``: the level as a glyph that reads without colour (and in greyscale)."""
    level = max(0, min(BARS, int(level)))
    return "▮" * level + "▯" * (BARS - level)


# --------------------------------------------------------------------------- ownership


def ownership(authors: dict[str, int]) -> dict[str, Any]:
    """Anonymous ownership figures from ``{name: commits}``: no name is kept."""
    total = sum(authors.values())
    if not total:
        return {}
    return {"authors": len(authors), "owner_share": round(max(authors.values()) / total, 2)}


def single_owner(metrics: dict[str, Any], commits: int, team: int) -> bool:
    return (metrics.get("authors") == 1 and commits >= SINGLE_OWNER_COMMITS and team >= 2)


def show_authors(config: Any, where: str) -> bool:
    """Whether author names may be shown ``where`` ("live" app / API, or "report" / CLI / JSON output).

    ``[privacy] show_authors``: ``"live"`` (the default) shows them only in the live app, ``true`` everywhere,
    ``false`` nowhere."""
    mode = getattr(config, "privacy_show_authors", "live")
    return mode == "true" or (mode == "live" and where == "live")


def owner_names(repo: Any, limit: int = 3) -> dict[str, list[list[Any]]]:
    """``{path: [[name, commits], …]}``, the most active authors of each file in the churn window (never emails).

    Reads the same cached ``git log`` the Git analyzer uses for churn."""
    git = getattr(repo, "git", None)
    window = int(getattr(repo.config, "churn_commits", 0) or 0)
    head = git.head() if git is not None else None  # the revision the Git analyzer reads for the working tree
    if window <= 0 or not head:
        return {}
    key = ("git-churn", head, window, "authors")
    cache = getattr(repo, "file_cache", None)
    data = cache.get(key) if cache is not None else None
    if data is None:
        data = git.churn(head, window, authors=True)
        if cache is not None:
            cache[key] = data
    out = {}
    for path, stats in (data or {}).items():
        names = sorted((stats.get("authors") or {}).items(), key=lambda kv: (-kv[1], kv[0]))
        if names:
            out[path] = [[n, c] for n, c in names[:limit]]
    return out


# --------------------------------------------------------------------------- review


HIGH_FAN_IN_SHARE = 0.9  # "high fan-in": at or above the 90th percentile of the modules' fan-in,
MIN_HIGH_FAN_IN = 5  # and used by at least this many modules
HOTSPOT_BADGE_TOP = 10  # "hotspot (top N %)" on a file card: in the top 10 %


def _fan_in_threshold(snapshot: Any) -> int:
    cached = snapshot.__dict__.get("_fan_in_threshold")
    if cached is None:
        values = sorted(int((n.metadata.get("metrics") or {}).get("fan_in") or 0) for n in snapshot.modules
                        if "test" not in n.tags)
        cached = max(MIN_HIGH_FAN_IN, values[int(len(values) * HIGH_FAN_IN_SHARE)] if values else 0)
        snapshot.__dict__["_fan_in_threshold"] = cached
    return cached


def health(node: Any, snapshot: Any) -> dict[str, Any] | None:
    """A changed file's health before the change (``node`` from the base when it existed): its metrics and the
    badges a file card shows: ``hotspot (top 5%)``, ``high fan-in (used by 42 modules)``, ``single owner``."""
    if node is None:
        return None
    m = node.metadata.get("metrics") or {}
    c = node.metadata.get("churn") or {}
    if not m and not c:
        return None
    out: dict[str, Any] = {k: m[k] for k in ("sloc", "complexity", "complexity_kind", "max_complexity",
                                             "max_complexity_symbol", "max_nesting", "fan_in", "fan_out",
                                             "instability", "hotspot_top") if m.get(k) is not None}
    for k in ("commits", "authors", "owner_share"):
        if c.get(k) is not None:
            out[k] = c[k]
    badges = []
    top = m.get("hotspot_top")
    if top and top <= HOTSPOT_BADGE_TOP:
        badges.append({"kind": "hotspot", "text": f"hotspot (top {top}%)",
                       "detail": f"complexity {m.get('complexity')} × {c.get('commits')} recent commits"})
    fan_in = int(m.get("fan_in") or 0)
    if fan_in and fan_in >= _fan_in_threshold(snapshot):
        badges.append({"kind": "fan-in", "text": f"high fan-in (used by {fan_in} modules)",
                       "detail": "a change here reaches many modules"})
    team = int((snapshot.metadata.get("churn_span") or {}).get("authors") or 0)
    if single_owner(c, int(c.get("commits") or 0), team):
        badges.append({"kind": "owner", "text": "single owner",
                       "detail": f"one author made all {c.get('commits')} recent commits: ask them"})
    out["badges"] = badges
    return out


# --------------------------------------------------------------------------- CLI: repoviz metrics


SORTS = ("hotspot", "churn", "complexity", "fan-in", "sloc", "ownership")


def rows(snapshot: Any, sort: str = "hotspot", by: str = "module", tests: bool = False) -> list[dict[str, Any]]:
    """The measured modules (or components) as rows, sorted: ``repoviz metrics``."""
    if by == "component":
        nodes = [n for n in snapshot.components if "component" in n.tags and n.metadata.get("metrics")]
    else:
        nodes = [n for n in (*snapshot.modules, *snapshot.components) if n.metadata.get("metrics")
                 and (n.category == "module" or "unsupported" in n.tags) and (tests or "test" not in n.tags)]
    out = []
    for n in nodes:
        m, c = n.metadata["metrics"], n.metadata.get("churn") or {}
        out.append({"path": n.path if by != "component" else n.qualified_name, "id": n.id,
                    **{k: m.get(k) for k in ("sloc", "complexity", "complexity_kind", "max_complexity",
                                            "max_complexity_symbol", "max_nesting", "fan_in", "fan_out",
                                            "instability", "hotspot_top", "modules")},
                    "commits": c.get("commits"), "authors": c.get("authors"),
                    "owner_share": c.get("owner_share", m.get("owner_share"))})
    key = {"hotspot": lambda r: (r["hotspot_top"] or 101, -(r["commits"] or 0)),
           "churn": lambda r: -(r["commits"] or 0),
           "complexity": lambda r: -(r["complexity"] or 0),
           "fan-in": lambda r: -(r["fan_in"] or 0),
           "sloc": lambda r: -(r["sloc"] or 0),
           "ownership": lambda r: (-(r["owner_share"] or 0), -(r["commits"] or 0))}[sort]
    return sorted(out, key=lambda r: (key(r), r["path"] or ""))


def format_rows(items: list[dict[str, Any]], window: int | None, names: dict[str, list[list[Any]]] | None = None,
                by: str = "module") -> str:
    head = f"{'component' if by == 'component' else 'module':<44} {'sloc':>6} {'complexity':>11} {'fan-in':>6} " \
           f"{'out':>4} {'commits':>7}  {'hotspot':<15} owner"
    lines = [head, "-" * len(head)]
    for r in items:
        cx = f"{r['complexity'] or 0}{'ws' if r.get('complexity_kind') == 'whitespace' else ''}"
        hot = f"{bars(hotspot_level(r['hotspot_top']))} top {r['hotspot_top']}%" if r.get("hotspot_top") else "-"
        owner = "-"
        if r.get("owner_share") is not None:
            owner = f"{round(r['owner_share'] * 100)}%" + (f" of {r['authors']} author(s)" if r.get("authors") else "")
            who = (names or {}).get(r["path"] or "")
            if who:
                owner += " (" + ", ".join(f"{n} {c}" for n, c in who) + ")"
        path = r["path"] or ""
        path = path if len(path) <= 44 else "…" + path[-43:]
        lines.append(f"{path:<44} {r['sloc'] or 0:>6} {cx:>11} {r['fan_in'] or 0:>6} {r['fan_out'] or 0:>4} "
                     f"{r['commits'] or 0:>7}  {hot:<15} {owner}")
    lines.append("")
    lines.append("complexity: cyclomatic (Python) or whitespace (ws: other languages); hotspot: complexity × churn"
                 + (f" over the last {window} commits" if window else "") + ", ranked (tests not ranked)")
    return "\n".join(lines) + "\n"
