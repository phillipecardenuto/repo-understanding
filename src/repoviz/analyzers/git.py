"""Generic Git analyzer (mandatory when the target is a Git repository).

Contributes repository-level metadata (branch, HEAD, default branch, remote
*names* -- never assuming a hosting provider) and per-path change frequency
("churn") over recent history, which the UI uses to highlight hotspots.
"""

from __future__ import annotations

import datetime as _dt

from .base import CAP_DIAGNOSTICS, AnalysisContext, Analyzer, Detection, SnapshotBuilder


class GitAnalyzer(Analyzer):
    name = "git"
    version = "1"
    capabilities = (CAP_DIAGNOSTICS,)
    mandatory = True

    def detect(self, ctx: AnalysisContext) -> Detection:
        if ctx.git is None:
            return Detection(False, "not a Git repository (or git is not installed)")
        return Detection(True, "Git repository")

    def discover_components(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        prof = ctx.profile
        b.metadata["git"] = {
            "branch": prof.branch,
            "head": prof.head,
            "default_branch": prof.default_branch,
            "remotes": prof.remotes,
            "shallow": prof.shallow,
        }
        if prof.head is None:
            b.diagnostic("info", "unborn-head", "The repository has no commits yet; comparisons against HEAD use an "
                         "empty tree.", self.name)
        elif prof.branch is None:
            b.diagnostic("info", "detached-head", "HEAD is detached.", self.name)
        if prof.shallow:
            b.diagnostic("info", "shallow-clone", "This is a shallow clone: history-based metrics (churn) and old "
                         "revisions may be incomplete.", self.name)
        if getattr(ctx.source, "conflicted", None):
            b.diagnostic("warning", "merge-conflicts", "The index contains unresolved conflicts; the 'ours' side is "
                         "analyzed.", self.name, paths=list(ctx.source.conflicted)[:20])

    def finalize(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        if ctx.config.churn_commits <= 0:
            return
        rev = getattr(ctx.source, "sha", None) or ctx.profile.head
        if not rev:
            return
        key = ("git-churn", rev, ctx.config.churn_commits)
        churn = ctx.cached(key, lambda: ctx.git.churn(rev, ctx.config.churn_commits))
        if not churn:
            return
        dir_totals: dict[str, int] = {}
        for node in b.nodes.values():
            if node.path is None or node.component_type == "directory" or node.category == "symbol":
                continue
            stats = churn.get(node.path)
            if not stats:
                continue
            node.metadata["churn"] = {
                "commits": stats["commits"],
                "last_commit": _dt.datetime.fromtimestamp(int(stats["last_commit_ts"]), _dt.timezone.utc).isoformat(),
            }
            parts = node.path.split("/")[:-1]
            for i in range(0, len(parts) + 1):
                d = "/".join(parts[:i])
                dir_totals[d] = dir_totals.get(d, 0) + int(stats["commits"])
        for node in b.nodes.values():
            if node.component_type in ("directory", "repository", "package", "project", "workspace-member") \
                    and node.path is not None and node.path in dir_totals:
                node.metadata["churn"] = {"commits": dir_totals[node.path]}
        b.metadata["churn_window_commits"] = ctx.config.churn_commits
