"""Diff readability (#33): moved blocks across a wave's files, and the words that changed inside a line.

Both annotate hunks in place (``review.file_hunks`` format: ``lines`` of ``"+text"``, ``"-text"``, ``" text"``) with
extra keys that renderers may ignore:

* ``moved``: ``[{"side": "-" | "+", "start", "end", "path", "line", "lines", "changed", "residual"}]``.  A run of
  3 or more removed lines whose text (whitespace collapsed) comes back as added lines, in the same file or another
  file of the wave, is a moved block on both sides, each pointing at the other (``start`` / ``end`` are indexes
  into ``lines``, inclusive).  The added side carries the ``residual``: the lines that differ inside the block (an
  edit made while moving), each ``{"t", "text", "no", "marks"}``, the removed ones from the other side.  A removed
  run right before an added run in the same hunk is an edit in place, not a move.
* ``marks``: ``[[index, [[start, end], …]], …]``: character ranges (in the text after the sign) of the tokens that
  changed, for similar removed and added lines paired within a hunk (and inside moved blocks).  Tokens are words,
  numbers, single punctuation marks and whitespace runs; whitespace is never marked.  A line mostly rewritten
  gets no marks (they would only add noise).
* ``ws``: indexes of lines whose change is whitespace only (re-indented, or blank).

Matching is hash-based, then ``difflib`` on the candidate runs only; a wave with more than :data:`MAX_LINES`
changed lines is not searched for moves (the marks inside hunks still are).
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

MAX_LINES = 5000  # changed lines per wave searched for moved blocks
MIN_BLOCK = 3  # lines
MIN_SHARE = 0.75  # of a block's lines that must match (the rest is the residual edit)
MAX_RUN = MAX_LINES  # lines of one run compared with difflib (a whole moved file, up to the cap)
MAX_MARK_LINE = 400  # characters; longer lines get no marks
MAX_PAIR_RUN = 60  # removed / added lines paired for marks in one replace
TOKEN = re.compile(r"\w+|\s+|[^\w\s]")


def norm(text: str) -> str:
    return " ".join(text.split())


def _trivial(n: str) -> bool:
    """Lines that match everywhere (``}``, ``end``, blank) do not vote for a move."""
    return len(n) < 3 or not re.search(r"\w", n)


def token_marks(a: str, b: str, sm: difflib.SequenceMatcher | None = None
                ) -> tuple[list[list[int]], list[list[int]]] | None:
    """Character ranges of the tokens that differ in ``a`` (old) and ``b`` (new); None when the lines differ too
    much for marks to help (or are too long).  ``sm`` is a matcher already run on their tokens (``_pair``)."""
    if len(a) > MAX_MARK_LINE or len(b) > MAX_MARK_LINE:
        return None
    if sm is None:
        sm = difflib.SequenceMatcher(None, TOKEN.findall(a), TOKEN.findall(b), autojunk=False)
    ta, tb = sm.a, sm.b
    oa, ob = [0], [0]
    for t in ta:
        oa.append(oa[-1] + len(t))
    for t in tb:
        ob.append(ob[-1] + len(t))
    ma: list[list[int]] = []
    mb: list[list[int]] = []

    def add(out: list[list[int]], toks: Any, offs: list[int], i: int, j: int) -> None:
        while i < j and toks[i].isspace():
            i += 1
        while j > i and toks[j - 1].isspace():
            j -= 1
        if i >= j:
            return
        s, e = offs[i], offs[j]
        if out and out[-1][1] >= s - 1:
            out[-1][1] = e
        else:
            out.append([s, e])

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        add(ma, ta, oa, i1, i2)
        add(mb, tb, ob, j1, j2)
    changed = sum(e - s for s, e in ma) + sum(e - s for s, e in mb)
    size = len(a.strip()) + len(b.strip())
    if size and changed / size > 0.6:
        return None  # mostly rewritten
    return ma, mb


# --------------------------------------------------------------------------- runs of changed lines


@dataclass
class _Run:
    path: str
    hunk: dict[str, Any]
    sign: str
    start: int  # index into hunk["lines"]
    first_no: int  # line number of the first line (old for "-", new for "+")
    texts: list[str] = field(default_factory=list)
    norms: list[str] = field(default_factory=list)


def _runs(path: str, hunks: list[dict[str, Any]]) -> list[_Run]:
    out: list[_Run] = []
    for hk in hunks:
        o, n = hk["old_start"], hk["new_start"]
        cur: _Run | None = None
        for i, raw in enumerate(hk["lines"]):
            t = raw[:1]
            if t in ("-", "+"):
                if cur is None or cur.sign != t:
                    cur = _Run(path, hk, t, i, o if t == "-" else n)
                    out.append(cur)
                cur.texts.append(raw[1:])
                cur.norms.append(norm(raw[1:]))
            else:
                cur = None
            if t != "+":
                o += 1
            if t != "-":
                n += 1
    return out


def _add_marks(hk: dict[str, Any], index: int, ranges: list[list[int]]) -> None:
    if ranges:
        hk.setdefault("_marks", {})[index] = ranges


def _pair(old: list[str], new: list[str]) -> list[tuple[int, int, difflib.SequenceMatcher]]:
    """Similar old / new lines, in order: the next unpaired line first (edits in place mostly keep their order),
    else the most similar of the next few.  Compared token by token; each pair keeps its matcher for the marks."""
    toks_new: dict[int, list[str]] = {}

    def tokens(j: int) -> list[str]:
        if j not in toks_new:
            toks_new[j] = TOKEN.findall(new[j]) if len(new[j]) <= MAX_MARK_LINE else []
        return toks_new[j]

    pairs: list[tuple[int, int, difflib.SequenceMatcher]] = []
    j0 = 0
    for i, a in enumerate(old[:MAX_PAIR_RUN]):
        if j0 >= min(len(new), MAX_PAIR_RUN) or len(a) > MAX_MARK_LINE:
            continue
        ta = TOKEN.findall(a)
        best, best_j, best_sm = 0.5, -1, None
        for j in range(j0, min(len(new), j0 + 6, MAX_PAIR_RUN)):
            sm = difflib.SequenceMatcher(None, ta, tokens(j), autojunk=False)
            if sm.real_quick_ratio() <= best or sm.quick_ratio() <= best:
                continue
            r = sm.ratio()
            if r > best:
                best, best_j, best_sm = r, j, sm
                if j == j0 and r >= 0.6:
                    break  # the line in place is similar enough: no need to look further
        if best_sm is not None:
            pairs.append((i, best_j, best_sm))
            j0 = best_j + 1
    return pairs


# --------------------------------------------------------------------------- moved blocks


def _moves(runs: list[_Run], stats: dict[str, Any]) -> None:
    removed = [r for r in runs if r.sign == "-" and len(r.texts) >= MIN_BLOCK]
    added = [r for r in runs if r.sign == "+" and len(r.texts) >= MIN_BLOCK]
    where: dict[str, list[int]] = {}
    for k, a in enumerate(added):
        for n in set(a.norms[:MAX_RUN]):
            if not _trivial(n):
                where.setdefault(n, []).append(k)
    used_r: set[tuple[int, int]] = set()  # (removed run, line)
    used_a: set[tuple[int, int]] = set()  # (added run, line)
    for ri in sorted(range(len(removed)), key=lambda i: -len(removed[i].texts)):
        r = removed[ri]
        votes = Counter(k for n in set(r.norms[:MAX_RUN]) if not _trivial(n) for k in where.get(n, ()))
        for k, v in votes.most_common(4):
            if v < MIN_BLOCK:
                break
            a = added[k]
            if a.path == r.path and a.hunk is r.hunk and a.start == r.start + len(r.texts):
                continue  # replaced where it stood: an edit, not a move
            sm = difflib.SequenceMatcher(None, r.norms[:MAX_RUN], a.norms[:MAX_RUN], autojunk=False)
            blocks = [b for b in sm.get_matching_blocks() if b.size]
            # the longest stretch of this run that the added run repeats, with small edits inside
            best: tuple[int, int, int, int, int] | None = None
            group: list[Any] = []
            for b in blocks + [None]:  # type: ignore[list-item]
                if b is not None and group:
                    last = group[-1]
                    gap = max(b.a - (last.a + last.size), b.b - (last.b + last.size))
                    if gap <= max(2, (last.a + last.size - group[0].a) // 4):
                        group.append(b)
                        continue
                if group:
                    matched = sum(g.size for g in group)
                    r0, r1 = group[0].a, group[-1].a + group[-1].size
                    a0, a1 = group[0].b, group[-1].b + group[-1].size
                    if matched >= MIN_BLOCK and matched / max(r1 - r0, a1 - a0) >= MIN_SHARE and \
                            (best is None or matched > best[0]):
                        best = (matched, r0, r1, a0, a1)
                group = [b] if b is not None else []
            if best is None:
                continue
            matched, r0, r1, a0, a1 = best
            while r0 < r1 - 1 and a0 < a1 - 1 and not r.norms[r0] and not a.norms[a0]:
                r0, a0, matched = r0 + 1, a0 + 1, matched - 1  # a block starts and ends on code, not blank lines
            while r1 - 1 > r0 and a1 - 1 > a0 and not r.norms[r1 - 1] and not a.norms[a1 - 1]:
                r1, a1, matched = r1 - 1, a1 - 1, matched - 1
            if matched < MIN_BLOCK:
                continue
            if any((ri, x) in used_r for x in range(r0, r1)) or any((k, y) in used_a for y in range(a0, a1)):
                continue
            used_r.update((ri, x) for x in range(r0, r1))
            used_a.update((k, y) for y in range(a0, a1))
            _record(r, a, r0, r1, a0, a1)
            stats["moved_blocks"] += 1
            stats["moved_lines"] += matched


def _record(r: _Run, a: _Run, r0: int, r1: int, a0: int, a1: int) -> None:
    sm = difflib.SequenceMatcher(None, r.norms[r0:r1], a.norms[a0:a1], autojunk=False)
    residual: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old = [(r0 + i, r.texts[r0 + i]) for i in range(i1, i2)]
        new = [(a0 + j, a.texts[a0 + j]) for j in range(j1, j2)]
        marks_old: dict[int, list[list[int]]] = {}
        marks_new: dict[int, list[list[int]]] = {}
        for i, j, sm in _pair([t for _x, t in old], [t for _y, t in new]):
            m = token_marks(old[i][1], new[j][1], sm)
            if m:
                marks_old[i], marks_new[j] = m
                _add_marks(a.hunk, a.start + new[j][0], m[1])
                _add_marks(r.hunk, r.start + old[i][0], m[0])
        residual += [{"t": "-", "text": t, "no": r.first_no + x, "marks": marks_old.get(i, [])}
                     for i, (x, t) in enumerate(old)]
        residual += [{"t": "+", "text": t, "no": a.first_no + y, "marks": marks_new.get(j, [])}
                     for j, (y, t) in enumerate(new)]
    changed = sum(1 for x in residual if x["t"] == "+")
    r.hunk.setdefault("moved", []).append({
        "side": "-", "start": r.start + r0, "end": r.start + r1 - 1, "path": a.path, "line": a.first_no + a0,
        "lines": r1 - r0, "changed": changed})
    a.hunk.setdefault("moved", []).append({
        "side": "+", "start": a.start + a0, "end": a.start + a1 - 1, "path": r.path, "line": r.first_no + r0,
        "lines": a1 - a0, "changed": changed, "residual": residual[:60]})


# --------------------------------------------------------------------------- word-level marks in place


def _in_place(runs: list[_Run]) -> None:
    """Marks for removed lines replaced by similar added lines right below them; whitespace-only changes.  Lines
    of a moved block are left to it."""
    for i, r in enumerate(runs):
        if r.sign != "-" or i + 1 >= len(runs):
            continue
        a = runs[i + 1]
        if a.sign != "+" or a.hunk is not r.hunk or a.start != r.start + len(r.texts):
            continue
        moved = {k for m in r.hunk.get("moved", ()) for k in range(m["start"], m["end"] + 1)}
        for x, y, sm in _pair(r.texts, a.texts):
            if r.start + x in moved or a.start + y in moved:
                continue
            if r.norms[x] == a.norms[y]:
                r.hunk.setdefault("_ws", set()).update((r.start + x, a.start + y))
                continue
            m = token_marks(r.texts[x], a.texts[y], sm)
            if m:
                _add_marks(r.hunk, r.start + x, m[0])
                _add_marks(a.hunk, a.start + y, m[1])


def annotate(files: list[tuple[str, list[dict[str, Any]]]], max_lines: int = MAX_LINES) -> dict[str, Any]:
    """Annotate the hunks of every file of a wave in place; returns counts (and whether moves were searched)."""
    stats: dict[str, Any] = {"moved_blocks": 0, "moved_lines": 0, "moves_searched": True}
    runs_by_file = [_runs(path, hunks) for path, hunks in files if hunks]
    runs = [r for rs in runs_by_file for r in rs]
    changed = sum(len(r.texts) for r in runs)
    if changed > max_lines:
        stats["moves_searched"] = False
        stats["changed_lines"] = changed
    else:
        _moves(runs, stats)
    for rs in runs_by_file:
        _in_place(rs)
    for r in runs:  # blank lines added or removed are whitespace-only too
        for k, n in enumerate(r.norms):
            if not n:
                r.hunk.setdefault("_ws", set()).add(r.start + k)
    for _path, hunks in files:
        for hk in hunks or []:
            marks = hk.pop("_marks", None)
            if marks:
                hk["marks"] = [[i, marks[i]] for i in sorted(marks)]
            ws = hk.pop("_ws", None)
            if ws:
                hk["ws"] = sorted(ws)
            if hk.get("moved"):
                hk["moved"].sort(key=lambda m: m["start"])
    return stats
