"""Regressions found while evaluating repoviz on a multi-service repository with Git submodules."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repoviz.render.views import submodule_state
from repoviz.repo import Repository
from repoviz.review import build_review, resolve_target


def by_kind(report):
    out: dict[str, list] = {}
    for f in report["findings"]:
        out.setdefault(f["kind"], []).append(f)
    return out


# --------------------------------------------------------------------------- Python naming


def test_namespace_subpackage_inside_a_package(make_repo) -> None:
    """``app/config/`` without ``__init__.py`` inside package ``app`` imports as ``app.config``."""
    repo = make_repo({
        "app/__init__.py": "",
        "app/config/settings.py": "LIMIT = 20\n",
        "app/main.py": "from app.config.settings import LIMIT\n\n\ndef run():\n    return LIMIT\n",
        "tools/script.py": "print('standalone')\n",
    })
    r = Repository(repo.path)
    snap = r.snapshot("WORKTREE")
    names = {m.path: m.qualified_name for m in snap.modules}
    assert names["app/config/settings.py"] == "app.config.settings"
    assert names["tools/script.py"] == "script"  # a directory outside any package stays an import root
    assert not [d for d in snap.diagnostics if d.code == "unresolved-internal-import"]
    idx = snap.node_index()
    assert ("app.main", "app.config.settings") in {(idx[e.source_id].qualified_name, idx[e.target_id].qualified_name)
                                                   for e in snap.dependency_edges if e.relationship == "imports"}
    # "app" is a package, so it is not an import root itself.
    assert [root["path"] for root in r.discover().source_roots] == [""]


# --------------------------------------------------------------------------- review signals


LONG = ", ".join(f"option_{i}: int = {i}" for i in range(12))


def test_signature_change_beyond_display_length_is_detected(make_repo) -> None:
    repo = make_repo({
        "pkg/__init__.py": "",
        "pkg/service.py": f"def list_items(user_id: str, {LONG}, limit: int = 50):\n    return []\n",
        "pkg/api.py": "from pkg.service import list_items\n\n\ndef handler():\n    return list_items('u', limit=5)\n",
    })
    service = Path(repo.path, "pkg/service.py")
    service.write_text(service.read_text().replace("limit: int = 50", "page_size: int = 50"))
    report = build_review(Repository(repo.path), resolve_target(Repository(repo.path), "all"))
    stale = by_kind(report)["stale-callers"]
    assert "parameters removed: limit; added: page_size" in stale[0]["detail"]
    assert "pkg.api.handler" in stale[0]["detail"]


def test_renamed_base_class_is_not_a_dangling_call(make_repo) -> None:
    repo = make_repo({
        "errs.py": """
            class OldBase(Exception):
                def __init__(self, message):
                    super().__init__(message)


            class NotFound(OldBase):
                def __init__(self, what):
                    super().__init__(f"{what} not found")
        """,
    })
    errs = Path(repo.path, "errs.py")
    errs.write_text(errs.read_text().replace("OldBase", "NewBase"))
    kinds = by_kind(build_review(Repository(repo.path), resolve_target(Repository(repo.path), "all")))
    assert "dangling-call" not in kinds


def test_external_dependency_already_used_by_the_component_is_not_news(make_repo) -> None:
    repo = make_repo({
        "requirements.txt": "requests\n",
        "app/__init__.py": "",
        "app/client.py": "import requests\n",
    })
    repo.write({"app/other.py": "import requests\n", "app/fresh.py": "import yaml\n"})
    kinds = by_kind(build_review(Repository(repo.path), resolve_target(Repository(repo.path), "all")))
    details = [f["detail"] for f in kinds.get("new-external-dependency", [])]
    assert len(details) == 1 and details[0].startswith("app.fresh now uses")


# --------------------------------------------------------------------------- submodules


def _git(cwd, *args: str) -> str:
    return subprocess.run(["git", "-c", "protocol.file.allow=always", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


@pytest.fixture
def with_submodule(make_repo, tmp_path: Path):
    lib = make_repo({"src/engine.py": "def run():\n    return 1\n", "README.md": "engine\n"})
    main = make_repo({"app/__init__.py": "", "app/main.py": "print('hi')\n"})
    _git(main.path, "submodule", "add", "-q", lib.path, "modules/engine")
    _git(main.path, "commit", "-qm", "add engine submodule")
    return main, lib


def test_clean_checkout_with_submodules_has_no_phantom_changes(with_submodule) -> None:
    main, _lib = with_submodule
    r = Repository(main.path)
    _comp, diff = r.compare(mode="all")
    assert diff.summary()["nodes"]["removed"] == 0 and diff.summary()["nodes"]["added"] == 0
    sub = next(c for c in r.snapshot("WORKTREE").components if c.component_type == "submodule")
    assert "component" in sub.tags and len(sub.metadata["commit"]) == 40
    info = r.discover().submodule_info
    assert info[0]["path"] == "modules/engine" and info[0]["checked_out"] is True


def test_review_sees_uncommitted_work_inside_a_submodule(with_submodule) -> None:
    main, _lib = with_submodule
    r = Repository(main.path)
    r.state.start_session(r.git, r.root, "wave", protected=["modules/**"])
    engine = Path(main.path, "modules/engine/src/engine.py")
    engine.write_text(engine.read_text() + "\nprint('debug')\n")
    report = build_review(Repository(main.path), resolve_target(Repository(main.path), "session"))
    paths = {f["path"]: f for f in report["files"]}
    assert paths["modules/engine"]["kind"] == "submodule" and paths["modules/engine"]["submodule"]["dirty"] == ["src/engine.py"]
    inner = paths["modules/engine/src/engine.py"]
    assert inner["scope"] == "protected" and inner["lines_added"] == 2 and inner["component"] == "modules/engine"
    kinds = by_kind(report)
    assert kinds["submodule-uncommitted"] and kinds["debug-output"][0]["path"] == "modules/engine/src/engine.py"
    assert [f["path"] for f in kinds["protected-touched"]] == ["modules/engine/src/engine.py"]


def test_review_lists_commits_and_files_of_a_submodule_update(with_submodule) -> None:
    main, lib = with_submodule
    sub = Path(main.path, "modules/engine")
    Path(sub, "src/engine.py").write_text("def run():\n    return 2\n")
    _git(sub, "commit", "-qam", "engine returns 2")
    _git(main.path, "commit", "-qam", "bump engine")
    report = build_review(Repository(main.path), resolve_target(Repository(main.path), "last-commit"))
    entry = next(f for f in report["files"] if f["path"] == "modules/engine")
    assert entry["submodule"]["status"] == "updated" and entry["submodule"]["commit_count"] == 1
    assert entry["submodule"]["commits"][0]["subject"] == "engine returns 2"
    assert "modules/engine/src/engine.py" in {f["path"] for f in report["files"]}
    assert by_kind(report)["submodule-updated"]


def test_dirty_submodule_file_at_session_start_is_not_blamed_on_the_agent(with_submodule) -> None:
    main, _lib = with_submodule
    engine = Path(main.path, "modules/engine/src/engine.py")
    engine.write_text(engine.read_text() + "# already here\n")
    r = Repository(main.path)
    r.state.start_session(r.git, r.root, "wave")
    Path(main.path, "app/main.py").write_text("print('changed')\n")
    report = build_review(Repository(main.path), resolve_target(Repository(main.path), "session"))
    assert [f["path"] for f in report["files"]] == ["app/main.py"]


# --------------------------------------------------------------------------- submodules as sub-projects (#21)


def test_submodule_code_is_analyzed_as_a_nested_sub_project(with_submodule) -> None:
    main, _lib = with_submodule
    snap = Repository(main.path).snapshot("WORKTREE")
    idx = snap.node_index()
    sub = next(n for n in snap.components if n.component_type == "submodule")
    engine = snap.find(path="modules/engine/src/engine.py")
    assert engine is not None and engine.category == "module"
    run = next(s for s in snap.symbols if s.path == engine.path and s.name == "run")
    assert idx[run.parent_id].id == engine.id
    chain, cur = [], engine
    while cur.parent_id:
        cur = idx[cur.parent_id]
        chain.append(cur.id)
    assert sub.id in chain and engine.metadata["component_id"] == sub.id  # nested under the submodule component
    assert sum(1 for n in snap.nodes() if n.path == "modules/engine") == 1  # one node for the submodule's path
    assert sub.metadata["analyzed"] is True and sub.metadata["languages"] == ["Python"]
    assert "dependency_details" not in sub.metadata  # its code is in the graph now
    [info] = snap.profile["submodule_info"]
    assert info["analyzed"] is True and info["files"] == 2 and info["behind"] == 0


def test_changes_inside_a_submodule_reach_the_changes_diagram(with_submodule) -> None:
    main, _lib = with_submodule
    engine = Path(main.path, "modules/engine/src/engine.py")
    engine.write_text("def run():\n    return 2\n\n\ndef stop():\n    return 0\n")
    r = Repository(main.path)
    _comp, diff = r.compare(mode="all")
    changed = {c.node.path: c.status for c in diff.nodes.values() if c.status != "unchanged" and c.node.path}
    assert changed.get("modules/engine/src/engine.py") == "modified"
    assert any(c.node.name == "stop" and c.status == "added" for c in diff.nodes.values())
    # the same work stays one entry per file in reviews (no double counting)
    report = build_review(r, resolve_target(r, "all"))
    paths = [f["path"] for f in report["files"]]
    assert paths.count("modules/engine/src/engine.py") == 1


def test_superproject_depending_on_a_submodule_package_gets_a_cross_repository_edge(make_repo) -> None:
    lib = make_repo({"engine/__init__.py": "def run():\n    return 1\n",
                     "pyproject.toml": '[project]\nname = "engine"\nversion = "1.0"\n'})
    main = make_repo({"app/__init__.py": "", "app/main.py": "import engine\n",
                      "pyproject.toml": '[project]\nname = "app"\nversion = "1.0"\ndependencies = ["engine>=1"]\n'})
    _git(main.path, "submodule", "add", "-q", lib.path, "vendor/engine")
    _git(main.path, "commit", "-qm", "engine")
    snap = Repository(main.path).snapshot("WORKTREE")
    idx = snap.node_index()
    [edge] = [e for e in snap.dependency_edges if e.metadata.get("cross_repository")]
    assert idx[edge.source_id].component_type == "repository" and idx[edge.target_id].component_type == "submodule"
    assert edge.relationship == "depends-on" and idx[edge.target_id].qualified_name == "vendor/engine"
    assert "project" in idx[edge.target_id].tags and idx[edge.target_id].metadata["project_name"] == "engine"


def test_submodule_exclusion_and_size_caps(with_submodule) -> None:
    main, _lib = with_submodule
    toml = Path(main.path, ".repoviz.toml")
    for config, reason, short in (('[submodules]\nexclude = ["modules/*"]\n', "excluded", "not analyzed (excluded)"),
                                  ("[submodules]\nmax_files = 1\n", "too large: 2 files", "not analyzed (too large)"),
                                  ("[submodules]\nanalyze = false\n", "turned off", "not analyzed (turned off)")):
        toml.write_text(config)
        snap = Repository(main.path).snapshot("WORKTREE")
        sub = next(n for n in snap.components if n.component_type == "submodule")
        assert sub.metadata["analyzed"] is False and sub.metadata["not_analyzed"].startswith(reason), config
        assert snap.find(path="modules/engine/src/engine.py") is None
        assert short in submodule_state(sub) and sub.metadata["dependency_details"].startswith("unavailable")


def test_services_run_the_code_of_their_own_submodule(make_repo) -> None:
    svc = make_repo({"app/__init__.py": "", "app/main.py": "app = object()\n", "Dockerfile": "FROM python:3.12\n"})
    main = make_repo({"app/__init__.py": "", "app/main.py": "app = object()\n", "Dockerfile": "FROM python:3.12\n",
                      "docker-compose.yml": "services:\n  api:\n    build: .\n    command: uvicorn app.main:app\n"
                                            "  scorer:\n    build: ./services/scorer\n"
                                            "    command: uvicorn app.main:app --port 9000\n"})
    _git(main.path, "submodule", "add", "-q", svc.path, "services/scorer")
    _git(main.path, "commit", "-qm", "scorer")
    snap = Repository(main.path).snapshot("WORKTREE")
    idx = snap.node_index()
    runs = {idx[e.source_id].name: idx[e.target_id].path for e in snap.dependency_edges if e.relationship == "runs"}
    assert runs == {"api": "app/main.py", "scorer": "services/scorer/app/main.py"}  # the same module name, twice
