"""Pair removed and added nodes that are the same thing under a new name or in a new place.

Node IDs contain paths and qualified names, so renaming a function or moving a file makes
the old node "removed" and the new one "added".  :func:`detect_renames` pairs them again,
using only what the snapshots already hold (no file is read):

* **files and modules**: identical content (blob hash), then, for code, the share of
  top-level symbol names they have in common (a ``git mv`` plus a small edit keeps most
  names); files left over in a folder that moved with most of its files follow it;
* **folders**: when most of their files moved to the same new folder;
* **submodules**: same recorded URL or commit, or a similar name in the same folder;
* **symbols**, parents first (classes before methods), within the same (or the renamed)
  parent: same name (moved along with its file), same body, same body up to formatting,
  or a similar name (``elis_handler`` → ``elies_handler``); across parents, an identical
  body with the same name (a function moved to another module).

Pairs are one-to-one and chosen best-first.  Work is bounded: only removed × added
candidates that share a parent, a content hash or a symbol name are compared.
"""

from __future__ import annotations

import difflib
import posixpath
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .model import CATEGORY_MODULE, CATEGORY_SYMBOL, RepositorySnapshot

#: Blob hash of an empty file: empty files are not paired by content (every empty __init__.py would match).
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
MIN_FILE_SIMILARITY = 0.5
MIN_NAME_SIMILARITY = 0.66
MAX_CANDIDATES = 2000
MIN_SHARED_NAMES = 2  # top-level names two code files must share to be one file moved (unless same file name)  # removed or added nodes of one kind considered at most


@dataclass
class Rename:
    old_id: str
    new_id: str
    kind: str  # file | folder | submodule | symbol
    old_name: str  # qualified names
    new_name: str
    old_short: str  # plain names (function name, file name)
    new_short: str
    old_path: str | None
    new_path: str | None
    similarity: float
    how: str  # same content | similar content | same name | similar name | moved with its folder | ...

    @property
    def renamed(self) -> bool:
        return self.old_short != self.new_short

    @property
    def moved(self) -> bool:
        if self.kind == "symbol":
            return (self.old_path or "") != (self.new_path or "")
        return posixpath.dirname(self.old_path or "") != posixpath.dirname(self.new_path or "")

    def to_dict(self) -> dict[str, Any]:
        return {"old_id": self.old_id, "new_id": self.new_id, "kind": self.kind, "old_name": self.old_name,
                "new_name": self.new_name, "old_path": self.old_path, "new_path": self.new_path,
                "similarity": round(self.similarity, 2), "how": self.how, "renamed": self.renamed,
                "moved": self.moved}


def _is_file(n: Any) -> bool:
    return bool(n.path) and (n.key or "").startswith("path:file:")


def _is_dir(n: Any) -> bool:
    return (n.key or "").startswith("path:dir:") and bool(n.path)


def _pick(scored: list[tuple[float, str, str, str]], used_old: set[str], used_new: set[str]) -> list[tuple[str, str, float, str]]:
    """Best-first one-to-one matching of (score, old, new, how) candidates."""
    out = []
    for score, old, new, how in sorted(scored, key=lambda t: (-t[0], t[1], t[2])):
        if old in used_old or new in used_new:
            continue
        used_old.add(old)
        used_new.add(new)
        out.append((old, new, score, how))
    return out


def _meaningful_signature(n: Any) -> bool:
    """A signature with at least one parameter besides ``self`` / ``cls`` (``()`` and ``(self)`` are everywhere)."""
    if not n.metadata.get("signature_id"):
        return False
    sig = str(n.metadata.get("signature") or "")
    inner = sig[sig.find("(") + 1:sig.rfind(")")] if "(" in sig and ")" in sig else ""
    params = [p.strip().split(":")[0].split("=")[0].strip(" *") for p in inner.split(",")]
    return any(p and p not in ("self", "cls", "/") for p in params)


def _similar_size(a: Any, b: Any) -> bool:
    la = (a.end_line or a.start_line or 0) - (a.start_line or 0) + 1
    lb = (b.end_line or b.start_line or 0) - (b.start_line or 0) + 1
    return max(la, lb) <= 1.5 * min(la, lb) + 1


def detect_renames(base: RepositorySnapshot, target: RepositorySnapshot, removed: set[str],
                   added: set[str]) -> list[Rename]:
    b_idx, t_idx = base.node_index(), target.node_index()
    removed_nodes = [b_idx[i] for i in sorted(removed) if i in b_idx]
    added_nodes = [t_idx[i] for i in sorted(added) if i in t_idx]
    if not removed_nodes or not added_nodes:
        return []
    used_old: set[str] = set()
    used_new: set[str] = set()
    pairs: list[tuple[str, str, float, str, str]] = []  # (old, new, score, how, kind)

    # -- files and modules --------------------------------------------------------------------------------
    r_files = [n for n in removed_nodes if _is_file(n)][:MAX_CANDIDATES]
    a_files = [n for n in added_nodes if _is_file(n)][:MAX_CANDIDATES]
    by_fp: dict[str, list[Any]] = {}
    for n in a_files:
        if n.fingerprint and n.fingerprint != EMPTY_BLOB:
            by_fp.setdefault(n.fingerprint, []).append(n)
    scored = []
    for r in r_files:
        for a in by_fp.get(r.fingerprint or "", []) if r.fingerprint != EMPTY_BLOB else []:
            same_base = posixpath.basename(r.path) == posixpath.basename(a.path)
            scored.append((1.0 + (0.01 if same_base else 0), r.id, a.id, "same content"))
    pairs += [(o, n, min(s, 1.0), h, "file") for o, n, s, h in _pick(scored, used_old, used_new)]

    # Code files with most of their top-level names in common (git mv plus a few edits).
    def top_names(snap_symbols: list[Any], ids: set[str]) -> dict[str, set[str]]:
        names: dict[str, set[str]] = {}
        for s in snap_symbols:
            if s.parent_id in ids and s.component_type in ("function", "class", "method") and "#" not in s.key:
                names.setdefault(s.parent_id, set()).add(s.name)
        return names

    r_left = {n.id: n for n in r_files if n.id not in used_old and n.category == CATEGORY_MODULE}
    a_left = {n.id: n for n in a_files if n.id not in used_new and n.category == CATEGORY_MODULE}
    if r_left and a_left:
        r_names = top_names(base.symbols, set(r_left))
        a_names = top_names(target.symbols, set(a_left))
        index: dict[str, list[str]] = {}
        for aid, names in a_names.items():
            for name in names:
                index.setdefault(name, []).append(aid)
        scored = []
        for rid, names in r_names.items():
            shared: Counter[str] = Counter()
            for name in names:
                for aid in index.get(name, ())[:50]:
                    shared[aid] += 1
            for aid, n_shared in shared.items():
                r, a = r_left[rid], a_left[aid]
                same_base = posixpath.basename(r.path) == posixpath.basename(a.path)
                # One shared generic name (``class Migration``, ``def main``) says nothing: need two, or the same
                # file name.
                if r.language != a.language or (n_shared < MIN_SHARED_NAMES and not same_base):
                    continue
                sim = n_shared / max(len(names), len(a_names.get(aid, ())))
                if same_base:
                    sim = min(1.0, sim + 0.1)
                if sim >= MIN_FILE_SIMILARITY:
                    scored.append((sim, rid, aid, "similar content"))
        pairs += [(o, n, s, h, "file") for o, n, s, h in _pick(scored, used_old, used_new)]

    # Folders that moved with most of their files, then the files left behind in them (empty __init__.py...).
    file_pairs = [(b_idx[o], t_idx[n]) for o, n, _s, _h, k in pairs if k == "file"]
    moves: Counter[tuple[str, str]] = Counter((posixpath.dirname(o.path), posixpath.dirname(n.path))
                                              for o, n in file_pairs if posixpath.dirname(o.path) != posixpath.dirname(n.path))
    removed_dirs = {n.path: n for n in removed_nodes if _is_dir(n)}
    added_dirs = {n.path: n for n in added_nodes if _is_dir(n)}
    dir_map: dict[str, str] = {}
    files_per_dir = Counter(posixpath.dirname(n.path) for n in b_idx.values() if _is_file(n)) if moves else Counter()
    for (old_dir, new_dir), count in moves.most_common():
        if old_dir in dir_map:
            continue
        if count * 2 >= max(1, files_per_dir[old_dir]):
            dir_map[old_dir] = new_dir
    scored = []
    for old_dir, new_dir in dir_map.items():
        if old_dir in removed_dirs and new_dir in added_dirs:
            scored.append((0.9, removed_dirs[old_dir].id, added_dirs[new_dir].id, "most of its files moved"))
    pairs += [(o, n, s, h, "folder") for o, n, s, h in _pick(scored, used_old, used_new)]
    a_by_path = {n.path: n for n in a_files if n.id not in used_new}
    scored = []
    for r in r_files:
        if r.id in used_old:
            continue
        new_dir = dir_map.get(posixpath.dirname(r.path))
        a = a_by_path.get(posixpath.join(new_dir, posixpath.basename(r.path))) if new_dir is not None else None
        if a is not None and a.language == r.language:
            scored.append((0.8, r.id, a.id, "moved with its folder"))
    pairs += [(o, n, s, h, "file") for o, n, s, h in _pick(scored, used_old, used_new)]

    # -- submodules ------------------------------------------------------------------------------------------
    scored = []
    r_subs = [n for n in removed_nodes if "submodule" in n.tags]
    for a in (n for n in added_nodes if "submodule" in n.tags):
        for r in r_subs:
            url_a, url_r = a.metadata.get("url"), r.metadata.get("url")
            if url_a and url_a == url_r:
                scored.append((1.0, r.id, a.id, "same repository URL"))
            elif a.metadata.get("commit") and a.metadata.get("commit") == r.metadata.get("commit"):
                scored.append((0.9, r.id, a.id, "same commit"))
            elif a.path and r.path and posixpath.dirname(a.path) == posixpath.dirname(r.path):
                # The upstream repository was renamed too (``elis-frontend`` → ``elies-frontend``).
                ratio = difflib.SequenceMatcher(None, posixpath.basename(r.path).lower(),
                                                posixpath.basename(a.path).lower()).ratio()
                if ratio >= MIN_NAME_SIMILARITY:
                    scored.append((0.5 + ratio / 5, r.id, a.id, "similar name in the same folder"))
    pairs += [(o, n, s, h, "submodule") for o, n, s, h in _pick(scored, used_old, used_new)]

    # -- symbols, parents first -----------------------------------------------------------------------------
    id_map = {o: n for o, n, _s, _h, _k in pairs}

    depths: dict[str, int] = {}

    def depth(n: Any, idx: dict[str, Any]) -> int:
        if n.id not in depths:
            d, cur, seen = 0, n.parent_id, set()
            while cur in idx and cur not in seen and idx[cur].category == CATEGORY_SYMBOL:
                seen.add(cur)
                d += 1
                cur = idx[cur].parent_id
            depths[n.id] = d
        return depths[n.id]

    r_syms = [n for n in removed_nodes if n.category == CATEGORY_SYMBOL][:MAX_CANDIDATES]
    a_syms = [n for n in added_nodes if n.category == CATEGORY_SYMBOL][:MAX_CANDIDATES]
    for level in sorted({depth(n, b_idx) for n in r_syms}):
        by_parent: dict[str, list[Any]] = {}
        for a in a_syms:
            if a.id not in used_new and depth(a, t_idx) == level:
                by_parent.setdefault(a.parent_id or "", []).append(a)
        level_r = [n for n in r_syms if n.id not in used_old and depth(n, b_idx) == level]
        # "Same signature and size" is a last resort: only when the signature is the only one of its kind on both
        # sides of the parent, so two unrelated ``(self)`` methods are never taken for one renamed.
        sig_removed = Counter((id_map.get(r.parent_id or "", r.parent_id or ""), r.component_type,
                               r.metadata.get("signature_id")) for r in level_r)
        scored = []
        for r in level_r:
            parent = id_map.get(r.parent_id or "", r.parent_id or "")
            group = [a for a in by_parent.get(parent, ()) if a.component_type == r.component_type
                     and a.language == r.language]
            same = [a for a in group if a.name == r.name]
            for a in same or group:  # an exact name match makes the other candidates irrelevant
                if a.name == r.name:
                    scored.append((1.0, r.id, a.id, "same name"))
                elif r.fingerprint and r.fingerprint == a.fingerprint:
                    scored.append((0.95, r.id, a.id, "same body"))
                elif r.metadata.get("body_fingerprint") and \
                        r.metadata.get("body_fingerprint") == a.metadata.get("body_fingerprint"):
                    scored.append((0.93, r.id, a.id, "same body, new name"))
                else:
                    matcher = difflib.SequenceMatcher(None, r.name.lower(), a.name.lower())
                    ratio = matcher.ratio() if matcher.real_quick_ratio() >= MIN_NAME_SIMILARITY and \
                        matcher.quick_ratio() >= MIN_NAME_SIMILARITY else 0.0
                    if ratio >= MIN_NAME_SIMILARITY:
                        scored.append((ratio * 0.85, r.id, a.id, "similar name"))
                    elif _meaningful_signature(r) and r.metadata.get("signature_id") == a.metadata.get("signature_id") \
                            and _similar_size(r, a) \
                            and sig_removed[(parent, r.component_type, r.metadata.get("signature_id"))] == 1 \
                            and sum(1 for x in group if x.metadata.get("signature_id") == a.metadata.get("signature_id")) == 1:
                        scored.append((0.55, r.id, a.id, "same signature and size"))
        picked = _pick(scored, used_old, used_new)
        pairs += [(o, n, s, h, "symbol") for o, n, s, h in picked]
        id_map.update({o: n for o, n, _s, _h in picked})
    # Moved to another module with the same name and body.
    by_body: dict[tuple[str, str, str | None], list[Any]] = {}
    for a in a_syms:
        if a.id not in used_new and a.fingerprint:
            by_body.setdefault((a.component_type, a.name, a.fingerprint), []).append(a)
    scored = []
    for r in r_syms:
        if r.id in used_old or not r.fingerprint:
            continue
        for a in by_body.get((r.component_type, r.name, r.fingerprint), ()):
            scored.append((0.8, r.id, a.id, "same name and body, other module"))
    pairs += [(o, n, s, h, "symbol") for o, n, s, h in _pick(scored, used_old, used_new)]

    out = []
    for old, new, score, how, kind in pairs:
        o, n = b_idx[old], t_idx[new]
        out.append(Rename(old, new, kind, o.qualified_name, n.qualified_name, o.name, n.name, o.path, n.path,
                          score, how))
    return out
