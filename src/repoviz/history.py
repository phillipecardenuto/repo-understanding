"""Change coupling learned from Git history: which files usually change together.

Static imports miss a whole class of coupling: a route and the frontend call that uses
it, a model and its migration, a settings key and ``.env.example``, a module and its
test.  History knows these pairs.  :func:`co_change` reads the file lists of recent
commits (one ``git log``, never running repository code) and, for every file, keeps
the partners that changed in most of its commits.

The degree is directional: ``degree = shared / revs(file)``, the share of the file's
commits that also changed the partner.  It answers "when *this* file changes, does
*that* one usually change too?", which is what matters when an agent edited one side.

Merge commits and bulk commits (more than ``max_files_per_commit`` files: renames,
reformatting, vendoring) are ignored, as in code-maat's coupling analysis.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from . import classify
from .config import Config


@dataclass(frozen=True)
class HistorySettings:
    commits: int = 300
    min_revs: int = 5
    min_shared: int = 3
    min_degree: float = 0.5
    max_files_per_commit: int = 30
    min_commits: int = 20
    top: int = 5

    @classmethod
    def from_config(cls, config: Config) -> "HistorySettings":
        return cls(config.history_commits, config.history_min_revs, config.history_min_shared,
                   config.history_min_degree, config.history_max_files_per_commit, config.history_min_commits)


@dataclass
class Partner:
    path: str
    shared: int  # commits that changed both files
    revs: int  # commits that changed the file the partner belongs to
    degree: float  # shared / revs

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "shared": self.shared, "revs": self.revs, "degree": round(self.degree, 2)}


@dataclass
class CouplingIndex:
    rev: str | None
    commits: int = 0  # commits used (after skipping merges, bulk and empty commits)
    bulk_skipped: int = 0
    shallow: bool = False
    revs: dict[str, int] = field(default_factory=dict)
    partners: dict[str, list[Partner]] = field(default_factory=dict)
    note: str | None = None
    settings: HistorySettings = field(default_factory=HistorySettings)

    @property
    def usable(self) -> bool:
        """Enough history to trust the pairs (a shallow clone or a young repository may not have it)."""
        return self.commits >= max(1, self.settings.min_commits)

    def of(self, path: str) -> list[Partner]:
        return self.partners.get(path, []) if self.usable else []

    def top_pairs(self, limit: int = 50, path: str | None = None) -> list[dict[str, Any]]:
        """Strongest pairs, each once: ``a`` ⇄ ``b`` with both directions' degrees."""
        seen: set[tuple[str, str]] = set()
        rows: list[dict[str, Any]] = []
        for a, plist in self.partners.items():
            if path is not None and a != path:
                continue
            for p in plist:
                key = (a, p.path) if path is not None or a < p.path else (p.path, a)
                if key in seen:
                    continue
                seen.add(key)
                back = next((q for q in self.partners.get(p.path, []) if q.path == a), None)
                rows.append({"a": a, "b": p.path, "shared": p.shared, "a_revs": p.revs,
                             "b_revs": self.revs.get(p.path, 0), "a_to_b": round(p.degree, 2),
                             "b_to_a": round(back.degree if back else p.shared / max(1, self.revs.get(p.path, 1)), 2)})
        rows.sort(key=lambda r: (-r["shared"], -max(r["a_to_b"], r["b_to_a"]), r["a"], r["b"]))
        return rows[:limit]

    def summary(self) -> dict[str, Any]:
        return {"rev": self.rev, "commits": self.commits, "bulk_skipped": self.bulk_skipped, "shallow": self.shallow,
                "usable": self.usable, "window": self.settings.commits, "note": self.note}


def skip_companion(partner: str, path: str) -> bool:
    """Partners not worth reporting: generated or vendored files, and lock files (unless both are manifests)."""
    if classify.generated_reason(partner) or classify.vendored_reason(partner):
        return True
    mk = classify.manifest_kind(partner)
    return bool(mk is not None and mk.lockfile and classify.manifest_kind(path) is None)


def co_change(git: Any, rev: str | None, settings: HistorySettings = HistorySettings()) -> CouplingIndex:
    """Learn which files change together from the last ``settings.commits`` commits reachable from ``rev``."""
    index = CouplingIndex(rev, settings=settings)
    if git is None or not rev or settings.commits <= 0:
        index.note = "history analysis is disabled" if settings.commits <= 0 else "no Git history"
        return index
    index.shallow = bool(git.is_shallow())
    revs: dict[str, int] = {}
    shared: dict[tuple[str, str], int] = {}
    for files in git.commit_file_sets(rev, settings.commits):
        unique = sorted(set(files))
        if not unique:
            continue
        if len(unique) > settings.max_files_per_commit:
            index.bulk_skipped += 1
            continue
        index.commits += 1
        for f in unique:
            revs[f] = revs.get(f, 0) + 1
        for a, b in itertools.combinations(unique, 2):
            shared[(a, b)] = shared.get((a, b), 0) + 1
    index.revs = revs
    candidates: dict[str, list[Partner]] = {}
    for (a, b), n in shared.items():
        if n < settings.min_shared:
            continue
        for x, y in ((a, b), (b, a)):
            rx = revs[x]
            if rx >= settings.min_revs and n / rx >= settings.min_degree:
                candidates.setdefault(x, []).append(Partner(y, n, rx, n / rx))
    for path, plist in candidates.items():
        plist.sort(key=lambda p: (-p.degree, -p.shared, p.path))
        index.partners[path] = plist[:settings.top]
    if not index.usable:
        index.note = (f"only {index.commits} usable commit(s) in the history"
                      + (" (shallow clone)" if index.shallow else "")
                      + f"; change coupling needs at least {settings.min_commits}")
    elif index.shallow:
        index.note = f"shallow clone: coupling learned from the {index.commits} commits available"
    return index
