"""Comparisons, diffs, cycles, affected flow, activity and work sessions."""

from __future__ import annotations

import json
from pathlib import Path

from repoviz.activity import observe
from repoviz.diff import diff_snapshots
from repoviz.flow import affected_flow
from repoviz.model import RepositorySnapshot
from repoviz.repo import Repository, parse_comparison


def statuses(diff, category=None):
    return {c.node.qualified_name: c.status for c in diff.nodes.values()
            if c.status != "unchanged" and (category is None or c.node.category == category)}


def edge_statuses(diff, relationship="imports"):
    out = {}
    for c in diff.edges.values():
        if c.edge.relationship == relationship and c.edge.direct and c.status != "unchanged":
            out[(diff.nodes[c.edge.source_id].node.qualified_name,
                 diff.nodes[c.edge.target_id].node.qualified_name)] = c
    return out


def test_parse_comparison_specs() -> None:
    assert parse_comparison("staged") == ("HEAD", "INDEX", "staged")
    assert parse_comparison("v1..v2") == ("v1", "v2", "range")
    assert parse_comparison("main...") == ("merge-base:main:WORKTREE", "WORKTREE", "merge-base")
    assert parse_comparison("abc123") == ("abc123", "WORKTREE", "revision-vs-worktree")


def test_all_comparison_modes(shop_repo) -> None:
    repo = shop_repo
    first = repo.git("rev-parse", "HEAD").strip()
    repo.git("tag", "v1")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write({"src/shop/api/audit.py": "import logging\n"})
    second = repo.commit("add audit")
    repo.git("tag", "v2")
    # Working-tree state: one staged change, one unstaged change, one untracked file.
    repo.write({"src/shop/api/staged.py": "import json\n"}).stage("src/shop/api/staged.py")
    repo.append("src/shop/cli.py", "\ndef extra():\n    return 1\n")
    repo.write({"src/shop/api/untracked.py": "import csv\n"})
    r = Repository(repo.path)

    def added_modules(**kw):
        _comp, d = r.compare(**kw)
        return {n for n, st in statuses(d, "module").items() if st == "added"}, statuses(d, "symbol")

    assert added_modules(base=first, target=second)[0] == {"shop.api.audit"}  # commit vs commit
    assert added_modules(base="main", target="feature")[0] == {"shop.api.audit"}  # branch vs branch
    assert added_modules(spec="v1..v2")[0] == {"shop.api.audit"}  # tag vs tag
    assert added_modules(base="v1")[0] == {"shop.api.audit", "shop.api.staged", "shop.api.untracked"}  # rev vs worktree
    assert added_modules(spec="main...")[0] == {"shop.api.audit", "shop.api.staged", "shop.api.untracked"}  # merge base
    staged_mods, staged_syms = added_modules(mode="staged")
    assert staged_mods == {"shop.api.staged"} and not staged_syms
    unstaged_mods, unstaged_syms = added_modules(mode="unstaged")
    assert unstaged_mods == set() and unstaged_syms == {"shop.cli.extra": "added"}  # untracked excluded
    all_mods, all_syms = added_modules(mode="all")
    assert all_mods == {"shop.api.staged", "shop.api.untracked"} and all_syms == {"shop.cli.extra": "added"}
    comps = r.default_comparisons()
    # the branch's committed work too (#31); its one commit is also the last commit, listed once
    assert [c.mode for c in comps] == ["all", "staged", "unstaged", "merge-base", "branch"]


def test_diff_detects_dependency_changes_and_cycles(shop_repo) -> None:
    repo = shop_repo
    # Make the TYPE_CHECKING import a runtime import -> introduces a cycle.
    models = Path(repo.path, "src/shop/core/models.py")
    models.write_text(models.read_text().replace("if TYPE_CHECKING:\n    from shop.api.views import View",
                                                 "from shop.api.views import View"))
    repo.write({"src/shop/api/extra.py": "import json\nimport shop.cli\n"})
    repo.delete("tests/test_models.py")
    r = Repository(repo.path)
    _comp, d = r.compare(mode="all")
    st = statuses(d, "module")
    assert st["shop.api.extra"] == "added" and st["test_models"] == "removed" and st["shop.core.models"] == "modified"
    edges = edge_statuses(d)
    assert edges[("shop.api.extra", "shop.cli")].status == "added"
    became = edges[("shop.core.models", "shop.api.views")]
    assert became.status == "modified" and became.in_target_cycle and not became.in_base_cycle
    assert any(r.startswith("type_checking_only") for r in became.reasons)
    assert edges[("test_models", "shop.core.models")].status == "removed"
    assert d.introduced_cycles and d.introduced_cycles[0].level == "module"
    new = {(x["source"], x["target"]): x for x in d.new_dependencies}
    assert new[("shop.core.models", "shop.api.views")]["note"].startswith("type-checking-only")
    assert new[("shop.api.extra", "json")]["stdlib"]
    # Containers of changed nodes are "modified" with a reason.
    pkg = next(c for c in d.nodes.values() if c.node.qualified_name == "shop.api" and c.node.category == "component")
    assert pkg.status == "modified" and "contents changed" in pkg.reasons
    # Reverting resolves the cycle.
    repo.commit("introduce cycle")
    models.write_text(models.read_text().replace("from shop.api.views import View\n", ""))
    _comp, d2 = Repository(repo.path).compare(mode="all")
    assert d2.resolved_cycles and not d2.introduced_cycles


def test_cosmetic_and_evidence_changes(shop_repo) -> None:
    repo = shop_repo
    repo.append("src/shop/cli.py", "\n# just a comment\n\n")
    views = Path(repo.path, "src/shop/api/views.py")
    views.write_text("# header comment moves every line\n" + views.read_text())
    cli = Path(repo.path, "src/shop/cli.py")
    cli.write_text(cli.read_text().replace("from shop.api import views", "from shop.api import views\nfrom shop.api import views as v2"))
    _comp, d = Repository(repo.path).compare(mode="all")
    views_node = next(c for c in d.nodes.values() if c.node.qualified_name == "shop.api.views" and c.node.category == "module")
    assert views_node.reasons == ["formatting or comments only"]
    # Line shifts alone do not change evidence ...
    assert ("shop.api.views", "shop.core.models") not in edge_statuses(d)
    # ... but a second import site does.
    ch = edge_statuses(d)[("shop.cli", "shop.api.views")]
    assert ch.status == "modified" and "occurrences 1 → 2" in ch.reasons


def test_diff_from_snapshot_files(shop_repo, tmp_path: Path) -> None:
    r = Repository(shop_repo.path)
    base = r.snapshot("HEAD")
    shop_repo.write({"src/shop/new.py": "import shop.cli\n"})
    target = Repository(shop_repo.path).snapshot("WORKTREE")
    (tmp_path / "a.json").write_text(json.dumps(base.to_dict()))
    (tmp_path / "b.json").write_text(json.dumps(target.to_dict()))
    d = diff_snapshots(RepositorySnapshot.from_dict(json.loads((tmp_path / "a.json").read_text())),
                       RepositorySnapshot.from_dict(json.loads((tmp_path / "b.json").read_text())))
    assert statuses(d, "module") == {"shop.new": "added"}


def test_affected_flow_reaches_entry_points_and_tests(shop_repo) -> None:
    models = Path(shop_repo.path, "src/shop/core/models.py")
    models.write_text(models.read_text().replace("return 42", "return 43"))
    _comp, d = Repository(shop_repo.path).compare(mode="all")
    flow = affected_flow(d)
    assert flow.mode == "symbols"
    roles = {n["qualified_name"]: n["role"] for n in flow.nodes}
    assert roles["shop.core.models.compute_total"] == "changed"
    assert roles["shop.api.views.View.render"] == "caller"
    entries = {e["name"] for e in flow.entry_points}
    assert "shop.cli:__main__" not in entries  # main() does not reach compute_total statically
    tests = {t["name"]: t for t in flow.tests}
    assert "test_models.test_total" in tests and tests["test_models.test_total"]["distance"] == 1
    assert tests["test_models.test_total"]["path"][-1] in flow.changed


def test_affected_flow_falls_back_to_modules(make_repo) -> None:
    repo = make_repo({"a.go": "package a\n", "go.mod": "module m\n", "b/b.go": 'package b\nimport "m"\n'})
    repo.write({"a.go": "package a\n\nvar X = 1\n"})
    _comp, d = Repository(repo.path).compare(mode="all")
    flow = affected_flow(d)
    assert flow.mode in ("modules", "none")


def test_activity_against_head(shop_repo) -> None:
    repo = shop_repo
    repo.append("src/shop/core/models.py", "\ndef added_fn():\n    return 1\n")
    repo.write({"src/shop/api/new.py": "import shop.cli\n", "config/settings.toml": "x = 1\n"})
    repo.delete("src/shop/cli.py")
    r = Repository(repo.path)
    report = observe(r)
    assert report["baseline"]["kind"] == "head"
    events = {e["path"]: e for e in report["events"]}
    models = events["src/shop/core/models.py"]
    assert models["git_status"] == "modified" and models["lines_added"] == 3 and models["lines_removed"] == 0
    assert models["owning_component_name"] == "shop"
    assert "tests/test_models.py" in models["tests_affected"]
    assert any(i["kind"] == "public-symbol-added" for i in models["architecture_impact"])
    assert events["src/shop/cli.py"]["git_status"] == "deleted"
    assert events["src/shop/api/new.py"]["git_status"] == "untracked"
    assert events["config/settings.toml"]["configuration_affected"]
    first = models["first_observed"]
    # A later observation keeps first_observed and moves last_observed only when content changes.
    repo.append("src/shop/core/models.py", "\n# more\n")
    report2 = observe(r)
    models2 = {e["path"]: e for e in report2["events"]}["src/shop/core/models.py"]
    assert models2["first_observed"] == first and models2["last_observed"] >= first


def test_work_session_tracks_only_session_changes(shop_repo) -> None:
    repo = shop_repo
    repo.append("src/shop/cli.py", "\n# dirty before the session\n")
    r = Repository(repo.path)
    session = r.state.start_session(r.git, r.root, label="agent")
    assert "src/shop/cli.py" in session.overrides
    # Nothing changed since the session started.
    assert observe(r)["events"] == []
    # The agent edits a file, adds one and commits another change.
    repo.write({"src/shop/api/agent.py": "from shop.core.models import compute_total\n"})
    repo.append("src/shop/api/views.py", "\ndef helper():\n    return 2\n")
    repo.git("add", "src/shop/api/views.py")
    repo.git("commit", "-q", "-m", "agent commit")
    report = observe(r)
    assert report["baseline"]["kind"] == "session"
    events = {e["path"]: e for e in report["events"]}
    assert set(events) == {"src/shop/api/agent.py", "src/shop/api/views.py"}  # cli.py was already dirty
    assert events["src/shop/api/views.py"]["git_status"] == "committed" and events["src/shop/api/views.py"]["in_session"]
    _comp, d = r.compare(mode="session")
    assert statuses(d, "module").get("shop.api.agent") == "added"
    assert "shop.cli" not in statuses(d, "module")
    ended = r.state.end_session()
    assert ended is not None and r.current_session() is None
    assert observe(r)["baseline"]["kind"] == "head"


# --------------------------------------------------------------------------- change coupling from history


def history_repo(make_repo):
    """app.py and schema.sql always change together; other.py changes alone."""
    repo = make_repo({"app.py": "x = 0\n", "schema.sql": "-- 0\n", "other.py": "y = 0\n",
                      ".repoviz.toml": "[history]\nmin_commits = 5\n"})
    for i in range(1, 7):
        repo.write({"app.py": f"x = {i}\n", "schema.sql": f"-- {i}\n"}).commit(f"feature {i}")
    for i in range(1, 5):
        repo.write({"other.py": f"y = {i}\n"}).commit(f"other {i}")
    return repo


def test_coupling_learns_files_that_change_together(make_repo) -> None:
    repo = history_repo(make_repo)
    # A bulk commit (more than 30 files) and a merge commit must not count.
    repo.write({**{f"bulk/f{i}.txt": "x\n" for i in range(31)}, "app.py": "x = 'bulk'\n", "other.py": "y = 'b'\n"})
    repo.commit("bulk rename")
    repo.git("checkout", "-q", "-b", "side")
    repo.write({"other.py": "y = 'side'\n"}).commit("side")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
    index = Repository(repo.path).coupling()
    assert index.usable and index.bulk_skipped == 1
    assert index.commits == 12  # initial + 6 features + 4 others + side (merge and bulk skipped)
    [partner] = index.of("app.py")
    assert (partner.path, partner.shared, partner.revs, partner.degree) == ("schema.sql", 7, 7, 1.0)
    assert index.of("other.py") == []  # changed with app.py only once
    assert Repository(repo.path).coupling() is not index  # a new Repository has its own cache
    r = Repository(repo.path)
    assert r.coupling() is r.coupling()  # cached per commit and settings


def test_coupling_thresholds_and_short_history(make_repo) -> None:
    repo = make_repo({"a.py": "1\n", "b.py": "1\n"})
    for i in range(2, 4):
        repo.write({"a.py": f"{i}\n", "b.py": f"{i}\n"}).commit(f"c{i}")
    index = Repository(repo.path).coupling()
    assert not index.usable and index.of("a.py") == []
    assert "needs at least 20" in index.note
    repo.write({".repoviz.toml": "[history]\nmin_commits = 1\nmin_shared = 4\n"}).commit("config")
    assert Repository(repo.path).coupling().of("a.py") == []  # 3 shared commits < min_shared


def test_activity_shows_companions_not_touched_yet(make_repo) -> None:
    repo = history_repo(make_repo)
    Path(repo.path, "app.py").write_text("x = 'agent'\n")
    events = {e["path"]: e for e in observe(Repository(repo.path))["events"]}
    assert events["app.py"]["companions"][0]["path"] == "schema.sql"
    Path(repo.path, "schema.sql").write_text("-- agent\n")
    events = {e["path"]: e for e in observe(Repository(repo.path))["events"]}
    assert not events["app.py"].get("companions")


def test_diff_folds_renames_and_redirects_their_edges(make_repo) -> None:
    repo = make_repo({"app/__init__.py": "", "app/billing/__init__.py": "",
                      "app/billing/invoice.py": "from app.billing import tax\n\n\ndef total(x):\n    return tax.tax(x)\n",
                      "app/billing/tax.py": "from app.billing import invoice\n\n\ndef tax(x):\n    return x * 2\n",
                      "app/main.py": "from app.billing.invoice import total\n\nprint(total(1))\n"})
    repo.git("mv", "app/billing", "app/payments")
    for name in ("app/payments/invoice.py", "app/payments/tax.py", "app/main.py"):
        p = Path(repo.path, name)
        p.write_text(p.read_text().replace("app.billing", "app.payments"))
    _comp, diff = Repository(repo.path).compare(mode="all")
    kinds = {(r["kind"], r["old_name"], r["new_name"]) for r in diff.renames}
    assert ("file", "app.billing.invoice", "app.payments.invoice") in kinds
    assert ("symbol", "app.billing.tax.tax", "app.payments.tax.tax") in kinds
    assert not diff.added_nodes or all(diff.nodes[n].node.path in ("app/payments",) for n in diff.added_nodes)
    moved = next(c for c in diff.nodes.values() if c.node.qualified_name == "app.payments.invoice")
    assert moved.status == "modified" and moved.before["previous_id"] and "moved from app/billing/invoice.py" in moved.reasons
    # The import edges followed the move: no dependency appears or disappears, and the cycle is the same one.
    assert diff.new_dependencies == [] and diff.removed_dependencies == []
    assert diff.introduced_cycles == [] and diff.resolved_cycles == []
    assert any(c.previous_id for c in diff.edges.values())


# --------------------------------------------------------------------------- code changes of one file (#36)


def hotspot_repo(make_repo):
    """``app/hot.py`` changed in five commits (one subject leaks a token); the other files once."""
    repo = make_repo({"app/__init__.py": "", "app/cold.py": "def cold():\n    return 1\n",
                      "app/hot.py": "def rate():\n    return 1\n\n\ndef other():\n    return 0\n"})
    for i in range(2, 6):
        repo.write({"app/hot.py": f"def rate():\n    return {i}\n\n\ndef other():\n    return 0\n"})
        repo.commit(f"tune rate {i}" + (" token=ghp_abcdefghijklmnopqrstuvwxyz0123456789" if i == 5 else ""))
    return repo


def test_file_changes_lists_recent_commits_and_one_diff(make_repo) -> None:
    import pytest

    from repoviz.filechanges import file_changes, hotspots

    repo = hotspot_repo(make_repo)
    r = Repository(repo.path)
    c = file_changes(r, "app/hot.py")
    assert [x["subject"][:11] for x in c["commits"]] == ["tune rate 5", "tune rate 4", "tune rate 3", "tune rate 2", "initial"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in str(c)  # commit subjects are redacted
    assert c["shown"] == c["commits"][0]["sha"] and not c["uncommitted"] and (c["added"], c["removed"]) == (1, 1)
    assert [line for hk in c["hunks"] for line in hk["lines"] if line[0] in "+-"] == ["-    return 4", "+    return 5"]
    first = file_changes(r, "app/hot.py", c["commits"][-1]["short"])  # the root commit: everything added
    assert first["label"].endswith("initial") and (first["added"], first["removed"]) == (6, 0)
    repo.write({"app/hot.py": "def rate():\n    return 99\n\n\ndef other():\n    return 0\n"})
    live = file_changes(r, "app/hot.py")  # uncommitted edits come first
    assert live["uncommitted"] and live["shown"] == "WORKTREE" and live["label"].startswith("Uncommitted")
    capped = file_changes(r, "app/hot.py", c["commits"][-1]["sha"], max_lines=2)
    assert capped["truncated"] and sum(len(hk["lines"]) for hk in capped["hunks"]) == 2 and capped["total_lines"] == 6
    for bad in ("../outside.py", "/etc/passwd", ".git/config", "-p", "app/missing.py"):
        with pytest.raises(ValueError):
            file_changes(r, bad)
    with pytest.raises(ValueError, match="not one of the last"):
        file_changes(r, "app/hot.py", "0" * 40)
    assert hotspots(r.snapshot("HEAD")) == ["app/hot.py"]


def test_file_changes_never_follows_symlinks(make_repo, tmp_path) -> None:
    import pytest

    from repoviz.filechanges import file_changes

    secret = tmp_path / "secret.txt"
    secret.write_text("outside the repository\n")
    repo = make_repo({"a.py": "A = 1\n"})
    (Path(repo.path) / "link.txt").symlink_to(secret)
    with pytest.raises(ValueError, match="unknown file"):
        file_changes(Repository(repo.path), "link.txt")


def test_removed_dependency_of_a_moved_module_stays_on_the_diagram(make_repo) -> None:
    from repoviz.render.views import changes_view

    repo = make_repo({"app/__init__.py": "", "app/x.py": "X = 1\n",
                      "app/m.py": "from app import x\n\n\ndef a():\n    return x.X\n\n\ndef b():\n    return 2\n"})
    repo.git("mv", "app/m.py", "app/n.py")
    Path(repo.path, "app/n.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n")
    r = Repository(repo.path)
    diff = r.diff(r.snapshot("HEAD"), r.snapshot("WORKTREE"))
    assert [(d["source"], d["target"]) for d in diff.removed_dependencies if d["level"] == "module"] == [("app.n", "app.x")]
    view = changes_view(diff, level="module")
    label = {n.id: n.label for n in view.nodes}
    assert [(label[e.source], label[e.target], e.status) for e in view.edges] == [("app.n", "app.x", "removed")]


def test_large_blobs_are_never_read(make_repo, monkeypatch) -> None:
    import repoviz.filechanges as fc
    from repoviz.gitutil import Git

    repo = make_repo({"data.txt": "x" * 200 + "\n"})
    repo.write({"data.txt": "y" * 200 + "\n"}).commit("bigger")
    monkeypatch.setattr(fc, "MAX_FILE_BYTES", 100)
    reads: list[str] = []
    original = Git.show_file
    monkeypatch.setattr(Git, "show_file", lambda self, rev, path: reads.append(path) or original(self, rev, path))
    c = fc.file_changes(Repository(repo.path), "data.txt")
    assert c["omitted"] == "file too large" and reads == []  # sizes are checked first
    assert not c["uncommitted"]  # two large versions of the same size are not taken for an edit


# --------------------------------------------------------------------------- History comparisons (#31)

def merge_history_repo(make_repo):
    """main: 2 commits; feature: 1 commit merged back with --no-ff; then feature2 with one more commit."""
    repo = make_repo({"app/__init__.py": "", "app/core.py": "X = 1\n"})
    repo.git("tag", "v0.1.0")
    repo.write({"app/api.py": "from app import core\n\n\ndef get():\n    return core.X\n"})
    repo.commit("add the api")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write({"app/jobs.py": "from app import api\n", "app/more.py": "Y = 2\n"})
    repo.commit("jobs")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    return repo


def test_history_presets(make_repo, capsys) -> None:
    from repoviz.cli import main

    repo = merge_history_repo(make_repo)
    r = Repository(repo.path)
    last = r.resolve_comparison(mode="last-commit")
    assert last.mode == "last-commit" and last.label == "Last commit: Merge feature"
    assert r.changed_file_count(last) == 2  # the merge brought jobs.py and more.py
    merge = r.resolve_comparison(spec="last-merge")
    assert merge.label == "Last merge: Merge feature" and merge.base == repo.git("rev-parse", "HEAD^1").strip()
    _comp, diff = r.compare(mode="last-merge")
    assert {c.node.path for c in diff.nodes.values() if c.status == "added" and c.node.category == "module"} == \
        {"app/jobs.py", "app/more.py"}
    since = r.resolve_comparison(spec="since:v0.1.0")
    assert since.mode == "since" and since.base == repo.git("rev-parse", "v0.1.0^{commit}").strip()
    assert r.changed_file_count(since) == 3
    import datetime

    soon = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
    dated = r.resolve_comparison(spec=f"since:{soon}")  # a date: the last commit before it
    assert dated.base == repo.git("rev-parse", "HEAD").strip() and r.changed_file_count(dated) == 0
    try:
        r.resolve_comparison(mode="branch")
        raise AssertionError("main is not a feature branch")
    except Exception as exc:
        assert "not on a feature branch" in str(exc)
    assert main(["diff", "-C", repo.path, "--mode", "last-merge"]) == 0
    assert "[Last merge: Merge feature]" in capsys.readouterr().out
    assert main(["diff", "-C", repo.path, "since:v0.1.0"]) == 0
    assert "Since v0.1.0" in capsys.readouterr().out
    assert main(["diff", "-C", repo.path, "since:no-such-thing"]) != 0


def test_branch_preset_and_first_commit(make_repo) -> None:
    repo = merge_history_repo(make_repo)
    repo.git("checkout", "-q", "-b", "feature2")
    repo.write({"app/extra.py": "Z = 3\n"})
    repo.commit("extra")
    r = Repository(repo.path)
    branch = r.resolve_comparison(mode="branch")
    assert branch.label == "Branch feature2 since it left main" and r.changed_file_count(branch) == 1
    # the branch's one commit is the last commit: listed once, as the branch
    assert [c.mode for c in r.history_comparisons()] == ["branch", "last-merge"]
    first = make_repo({"a.py": "x = 1\n"})
    only = Repository(first.path).resolve_comparison(mode="last-commit")
    assert only.base == "EMPTY" and Repository(first.path).changed_file_count(only) == 1
    assert Repository(first.path).history_comparisons()[0].mode == "last-commit"  # no merge, no branch


def test_reports_carry_history_comparisons(make_repo) -> None:
    from repoviz.render.html import build_bundle

    repo = merge_history_repo(make_repo)
    comps = build_bundle(Repository(repo.path), include_activity=False)["comparisons"]
    by_mode = {c["mode"]: c for c in comps}
    assert by_mode["all"]["files"] == 0  # a clean checkout
    # the last commit is the last merge: listed once, as the last commit
    assert by_mode["last-commit"]["files"] == 2 and by_mode["last-commit"]["label"] == "Last commit: Merge feature"
    assert "last-merge" not in by_mode
    assert by_mode["last-commit"]["diff"]["summary"]["nodes"]["added"] > 0
