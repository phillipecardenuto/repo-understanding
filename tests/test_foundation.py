"""IDs, globs, configuration, the YAML reader and tree sources."""

from __future__ import annotations

import pytest

from repoviz import globs, ids, yamlish
from repoviz.config import Config, ConfigError, apply_mapping, load_config
from repoviz.gitutil import Git, RevisionError
from repoviz.ids import IdRegistry, content_hash, make_id
from repoviz.sources import GitIndexSource, GitRevisionSource, RevSpec, WorkingTreeSource


# --------------------------------------------------------------------------- ids


def test_ids_are_stable_and_mermaid_safe() -> None:
    a = make_id("file", "path:file:src/a.py")
    assert a == make_id("file", "path:file:src/a.py")
    assert a.startswith("file_") and a.replace("_", "").isalnum()


def test_ids_do_not_collide_on_punctuation() -> None:
    # A naive "replace punctuation with underscores" scheme maps all of these to the same ID.
    keys = ["a/b_c", "a_b/c", "a.b.c", "a-b-c", "a b c", "a/b/c"]
    assert len({make_id("n", k) for k in keys}) == len(keys)
    # Length-prefixed hashing: part boundaries matter.
    assert ids.stable_hash("ab", "c") != ids.stable_hash("a", "bc")


def test_id_registry_resolves_collisions(monkeypatch: pytest.MonkeyPatch) -> None:
    real = ids.make_id

    def colliding(prefix: str, key: str, length: int = ids.DEFAULT_HEX_LENGTH) -> str:
        return f"{prefix}_same" if length == ids.DEFAULT_HEX_LENGTH else real(prefix, key, length)

    monkeypatch.setattr(ids, "make_id", colliding)
    reg = IdRegistry()
    first, second = reg.get("n", "one"), reg.get("n", "two")
    assert first == "n_same" and second != first and len(reg.collisions) == 1
    assert reg.get("n", "two") == second  # stable afterwards


def test_content_hash_is_git_blob_hash(make_repo) -> None:
    repo = make_repo({"x.txt": "hello\n"})
    assert content_hash(b"hello\n") == repo.git("hash-object", "x.txt").strip()


# --------------------------------------------------------------------------- globs


@pytest.mark.parametrize("path,pattern,expected", [
    ("node_modules/a/b.js", "node_modules/", True),
    ("pkg/node_modules/a.js", "node_modules/", True),
    ("src/a.egg-info/PKG-INFO", "*.egg-info/", True),
    ("a/b.py", "*.py", True),
    ("src/pkg/mod.py", "src/**/*.py", True),
    ("src/mod.py", "src/**/*.py", True),
    ("lib/src/mod.py", "src/**/*.py", False),
    ("build", "build/", False),
    ("build/out.txt", "build/", True),
    ("docs/index.md", "/docs", True),
    ("x/docs/index.md", "/docs", False),
])
def test_glob_semantics(path: str, pattern: str, expected: bool) -> None:
    assert globs.match(path, pattern) is expected


def test_glob_exact_and_dir() -> None:
    assert globs.match_exact("packages/a", "/packages/*")
    assert not globs.match_exact("packages/a/src", "/packages/*")
    assert globs.match_dir("proj/build", ["build/"])


# --------------------------------------------------------------------------- config


def test_config_precedence(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.repoviz]\nexclude = ["a/**"]\nsource_roots = ["lib"]\n')
    (tmp_path / ".repoviz.toml").write_text('exclude = ["b/**"]\n[python]\nuse_grimp = false\n')
    explicit = tmp_path / "custom.toml"
    explicit.write_text('[tool.repoviz]\ndefault_branch = "trunk"\nbogus = 1\n')
    cfg = load_config(tmp_path, explicit, {"max_file_bytes": 10})
    assert cfg.exclude == ["b/**"]  # .repoviz.toml overrides pyproject
    assert cfg.source_roots == ["lib"]
    assert cfg.python_use_grimp == "never"
    assert cfg.default_branch == "trunk"
    assert cfg.max_file_bytes == 10
    assert any("bogus" in s for s in cfg.sources)


def test_config_components_and_errors() -> None:
    cfg = apply_mapping(Config(), {"components": {"api": {"paths": ["services/api/**"], "type": "service"}}}, "t")
    assert cfg.components[0].name == "api" and cfg.components[0].type == "service"
    with pytest.raises(ConfigError):
        apply_mapping(Config(), {"components": [{"name": "x"}]}, "t")
    with pytest.raises(ConfigError):
        apply_mapping(Config(), {"python": {"use_grimp": "sometimes"}}, "t")
    assert Config().fingerprint() == Config().fingerprint()
    assert Config(exclude=["x"]).fingerprint() != Config().fingerprint()


# --------------------------------------------------------------------------- yaml


def test_builtin_yaml_subset() -> None:
    doc = yamlish.safe_load("""
services:
  web:
    build: {context: ./web, dockerfile: Dockerfile.dev}
    depends_on:
      - db   # comment
    command: |
      python -m app
      --reload
  db:
    image: "postgres:16"
list: [a, 'b', 3, true, null]
""", force_builtin=True)
    assert doc["services"]["web"]["build"] == {"context": "./web", "dockerfile": "Dockerfile.dev"}
    assert doc["services"]["web"]["depends_on"] == ["db"]
    assert doc["services"]["web"]["command"].startswith("python -m app")
    assert doc["list"] == ["a", "b", 3, True, None]
    docs = yamlish.safe_load_all("kind: A\n---\nkind: B\n", force_builtin=True)
    assert [d["kind"] for d in docs] == ["A", "B"]


# --------------------------------------------------------------------------- sources


def test_sources_see_each_state(make_repo) -> None:
    repo = make_repo({"a.py": "a = 1\n", "b.py": "b = 1\n"})
    repo.write({"a.py": "a = 2\n", "c.py": "c = 1\n"})  # unstaged edit + untracked file
    repo.write({"b.py": "b = 2\n"}).stage("b.py")  # staged edit
    git = Git(repo.path)
    head = GitRevisionSource(git, git.resolve("HEAD"))
    index = GitIndexSource(git)
    work = WorkingTreeSource(git)
    tracked = WorkingTreeSource(git, include_untracked=False)
    assert head.read_bytes("a.py") == b"a = 1\n" and head.read_bytes("b.py") == b"b = 1\n"
    assert index.read_bytes("b.py") == b"b = 2\n" and index.read_bytes("a.py") == b"a = 1\n"
    assert work.read_bytes("a.py") == b"a = 2\n"
    assert "c.py" in work.files() and "c.py" not in tracked.files() and "c.py" not in index.files()
    # Hashes are comparable across sources.
    assert head.content_hash("a.py") == index.content_hash("a.py") != work.content_hash("a.py")
    assert index.content_hash("b.py") == work.content_hash("b.py")
    repo.delete("a.py")
    assert "a.py" not in WorkingTreeSource(git).files()
    git.close()


def test_revspec_and_injection_guard(make_repo) -> None:
    assert RevSpec.parse("worktree").kind == "worktree"
    assert RevSpec.parse("STAGED").kind == "index"
    assert RevSpec.parse("main").kind == "git"
    repo = make_repo({"a.txt": "x"})
    git = Git(repo.path)
    for bad in ("--output=/tmp/x", "-n", "", "HEAD\n--all"):
        with pytest.raises(RevisionError):
            git.resolve(bad)
    with pytest.raises(RevisionError):
        git.resolve("does-not-exist")
    assert len(git.resolve("HEAD")) == 40
    git.close()
