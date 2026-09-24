"""Regressions found while evaluating repoviz on a multi-service repository with Git submodules."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
