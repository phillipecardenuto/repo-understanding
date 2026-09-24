from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Callable

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOVIZ_STATE_DIR", str(tmp_path_factory.mktemp("state")))
    monkeypatch.setenv("REPOVIZ_NO_PARALLEL", "1")
    for key, value in {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "Test",
                       "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path_factory.mktemp("gitcfg") / "config"))


class RepoFactory:
    """Creates throw-away repositories: ``repo = make_repo({"a.py": "..."})``."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def git(self, *args: str) -> str:
        out = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=True)
        return out.stdout

    def write(self, files: dict[str, str]) -> "RepoFactory":
        for rel, content in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return self

    def append(self, rel: str, content: str) -> "RepoFactory":
        with (self.root / rel).open("a", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(content))
        return self

    def delete(self, *rels: str) -> "RepoFactory":
        for rel in rels:
            (self.root / rel).unlink()
        return self

    def commit(self, message: str = "commit") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def stage(self, *rels: str) -> "RepoFactory":
        self.git("add", *rels)
        return self

    @property
    def path(self) -> str:
        return str(self.root)


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[..., RepoFactory]:
    counter = [0]

    def factory(files: dict[str, str] | None = None, commit: bool = True, git: bool = True) -> RepoFactory:
        counter[0] += 1
        root = tmp_path / f"repo{counter[0]}"
        root.mkdir()
        repo = RepoFactory(root)
        if git:
            repo.git("init", "-q", "-b", "main")
        if files:
            repo.write(files)
        if git and commit and files is not None:
            repo.commit("initial")
        return repo

    return factory


PY_SHOP = {
    "pyproject.toml": """
        [project]
        name = "shop"
        version = "1.0"
        dependencies = ["requests>=2", "PyYAML"]
        [project.scripts]
        shop = "shop.cli:main"
        [tool.setuptools.packages.find]
        where = ["src"]
    """,
    "src/shop/__init__.py": "",
    "src/shop/core/__init__.py": "from .models import Order\n",
    "src/shop/core/models.py": """
        from dataclasses import dataclass
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from shop.api.views import View


        @dataclass
        class Order:
            id: int

            def total(self):
                return compute_total(self)


        def compute_total(order):
            return 42
    """,
    "src/shop/api/__init__.py": "",
    "src/shop/api/views.py": """
        import requests
        from ..core.models import Order, compute_total


        class Base:
            def fmt(self, x):
                return str(x)


        class View(Base):
            def render(self, order: Order):
                return self.fmt(compute_total(order))
    """,
    "src/shop/cli.py": """
        import yaml
        from shop.api import views


        def main():
            v = views.View()
            return run(v)


        def run(view):
            return view


        if __name__ == "__main__":
            main()
    """,
    "tests/test_models.py": """
        from shop.core.models import compute_total


        def test_total():
            assert compute_total(None) == 42
    """,
}


@pytest.fixture
def shop_repo(make_repo: Callable[..., RepoFactory]) -> RepoFactory:
    return make_repo(PY_SHOP)
