"""Language analyzers, call-flow resolution, the pipeline and graph algorithms."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoviz import analyzers
from repoviz.analyzers.base import Analyzer, Detection
from repoviz.analyzers.javascript import mask, parse_js
from repoviz.analyzers.python import parse_python
from repoviz.graph import find_cycles, strongly_connected_components
from repoviz.model import DependencyEdge
from repoviz.repo import Repository


def edges_by_name(snap, relationship="imports", direct=True):
    idx = snap.node_index()
    out = {}
    for e in snap.dependency_edges + snap.call_edges:
        if e.relationship == relationship and e.direct == direct:
            out[(idx[e.source_id].qualified_name, idx[e.target_id].qualified_name)] = e
    return out


# --------------------------------------------------------------------------- Python


def test_python_modules_imports_and_flags(shop_repo) -> None:
    snap = Repository(shop_repo.path).snapshot("HEAD")
    names = {m.qualified_name for m in snap.modules}
    assert {"shop", "shop.core", "shop.core.models", "shop.api.views", "shop.cli", "test_models"} <= names
    imports = edges_by_name(snap)
    tc = imports[("shop.core.models", "shop.api.views")]
    assert tc.metadata["type_checking_only"] is True
    assert tc.evidence[0].path == "src/shop/core/models.py" and tc.evidence[0].start_line == 4
    assert "from shop.api.views import View" in tc.evidence[0].excerpt
    rel = imports[("shop.api.views", "shop.core.models")]
    assert rel.evidence[0].construct == "relative-from-import"
    assert imports[("shop.core", "shop.core.models")]  # from .models import Order
    ext = {n.qualified_name: n for n in snap.components if "external" in n.tags}
    assert "stdlib" in ext["dataclasses"].tags and "stdlib" not in ext["requests"].tags
    yaml = ext["PyYAML"]  # declared in the manifest, imported as ``yaml``: one merged node
    assert yaml.metadata["distribution"] == "pyyaml" and yaml.metadata["declared"]
    assert yaml.metadata["import_names"] == ["yaml"]
    assert not [d for d in snap.diagnostics if d.code == "undeclared-dependency"]
    # TYPE_CHECKING imports do not create cycles by default.
    assert not [c for c in snap.cycles if c.level == "module"]
    pkg = snap.find(path="src/shop")
    assert pkg.component_type == "package" and "component" in pkg.tags
    assert snap.find(path="src/shop/core/models.py").metadata["component_id"] == pkg.id


def test_python_symbols_calls_and_entry_points(shop_repo) -> None:
    snap = Repository(shop_repo.path).snapshot("HEAD")
    calls = edges_by_name(snap, "calls")
    assert ("shop.api.views.View.render", "shop.core.models.compute_total") in calls
    # self.fmt() resolves through the base class.
    assert ("shop.api.views.View.render", "shop.api.views.Base.fmt") in calls
    assert ("shop.core.models.Order.total", "shop.core.models.compute_total") in calls
    # views.View() through ``from shop.api import views`` (sub-module attribute) -> class
    assert ("shop.cli.main", "shop.api.views.View") in calls
    assert ("shop.cli:__main__", "shop.cli.main") in calls
    assert ("test_models.test_total", "shop.core.models.compute_total") in calls
    invokes = edges_by_name(snap, "invokes")
    assert ("shop [console-script]", "shop.cli.main") in invokes
    main_block = snap.find(qualified_name="shop.cli:__main__")
    assert "entry-point" in main_block.tags
    test_fn = snap.find(qualified_name="test_models.test_total")
    assert {"test", "entry-point"} <= set(test_fn.tags)
    render = snap.find(qualified_name="shop.api.views.View.render")
    assert render.component_type == "method" and render.metadata["signature"].startswith("(self, order")
    assert render.start_line and render.end_line and render.fingerprint


def test_python_import_forms() -> None:
    info = parse_python('''
import a.b as ab
from . import sibling
from ..pkg import thing
try:
    import fast
except ImportError:
    fast = None
def f():
    import lazy_mod
    importlib.import_module("dyn.mod")
if TYPE_CHECKING:
    from typing_only import T
from star import *
''')
    kinds = {(i.module, i.level): i for i in info.imports}
    assert kinds[("a.b", 0)].kind == "import"
    assert kinds[(None, 1)].names == [("sibling", None)]
    assert kinds[("pkg", 2)].kind == "from"
    assert kinds[("fast", 0)].conditional and kinds[("lazy_mod", 0)].lazy
    assert kinds[("dyn.mod", 0)].kind == "dynamic"
    assert kinds[("typing_only", 0)].type_checking
    assert kinds[("star", 0)].names == [("*", None)]


def test_python_parse_errors_are_diagnostics(make_repo) -> None:
    repo = make_repo({"ok.py": "import broken\n", "broken.py": "def (:\n"})
    snap = Repository(repo.path).snapshot("HEAD")
    diag = [d for d in snap.diagnostics if d.code == "parse-error"]
    assert diag and diag[0].path == "broken.py"
    assert snap.find(path="broken.py") is not None  # still a structural node
    assert ("ok", "broken") in edges_by_name(snap)


def test_analysis_never_executes_code(make_repo, tmp_path: Path) -> None:
    marker = tmp_path / "PWNED"
    evil = f"open({str(marker)!r}, 'w').write('x')\n"
    repo = make_repo({"evil.py": evil, "conftest.py": evil, "setup.py": evil + "from setuptools import setup\nsetup()\n",
                      "pkg/__init__.py": evil, "pkg/__main__.py": evil, "noxfile.py": evil})
    r = Repository(repo.path)
    r.snapshot("HEAD")
    r.snapshot("WORKTREE")
    r.compare(mode="all")
    assert not marker.exists()


def test_relative_import_beyond_top_level(make_repo) -> None:
    repo = make_repo({"pkg/__init__.py": "", "pkg/m.py": "from ... import nothing\n"})
    snap = Repository(repo.path).snapshot("HEAD")
    assert any(d.code == "unresolved-relative-import" for d in snap.diagnostics)


def test_python_reexports_nested_and_lazy_calls(make_repo) -> None:
    repo = make_repo({
        "lib/__init__.py": "from .impl import helper as public_helper\n",
        "lib/impl.py": "def helper():\n    return 1\n",
        "app.py": """
            from lib import public_helper
            import lib as L

            def outer():
                def inner():
                    return public_helper()
                return inner()

            def lazy():
                from lib.impl import helper
                return helper() + L.public_helper()
        """,
    })
    calls = edges_by_name(Repository(repo.path).snapshot("HEAD"), "calls")
    assert ("app.outer", "app.outer.inner") in calls
    assert ("app.outer.inner", "lib.impl.helper") in calls
    assert ("app.lazy", "lib.impl.helper") in calls


def test_cycles_detected_at_module_and_component_level(make_repo) -> None:
    repo = make_repo({
        "a/__init__.py": "", "a/x.py": "from b import y\n",
        "b/__init__.py": "", "b/y.py": "import a.x\n",
        "c/__init__.py": "", "c/lazy.py": "def f():\n    import c.other\n", "c/other.py": "import c.lazy\n",
    })
    snap = Repository(repo.path).snapshot("HEAD")
    idx = snap.node_index()
    levels = {c.level: sorted(idx[m].qualified_name for m in c.members) for c in snap.cycles}
    assert levels["module"] in (["a.x", "b.y"], ["c.lazy", "c.other"])
    module_cycles = [sorted(idx[m].qualified_name for m in c.members) for c in snap.cycles if c.level == "module"]
    assert ["a.x", "b.y"] in module_cycles and ["c.lazy", "c.other"] in module_cycles  # lazy imports count by default
    assert levels["component"] == ["a", "b"]
    cyc_edge = edges_by_name(snap)[("a.x", "b.y")]
    assert cyc_edge.cycle_ids
    # Lazy imports can be excluded from cycle detection.
    (Path(repo.path) / ".repoviz.toml").write_text("[cycles]\ninclude_lazy = false\n")
    snap2 = Repository(repo.path).snapshot("WORKTREE")
    idx2 = snap2.node_index()
    assert ["c.lazy", "c.other"] not in [sorted(idx2[m].qualified_name for m in c.members) for c in snap2.cycles]


def test_graph_algorithms() -> None:
    adj = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"d"}, "e": {"a"}}
    comps = strongly_connected_components(adj.keys() | {"c"}, adj)
    assert ["a", "b", "c"] in comps and ["d"] in comps
    edges = [DependencyEdge(f"e{i}", s, t, "imports") for i, (s, t) in enumerate([("a", "b"), ("b", "a"), ("x", "x")])]
    cycles = find_cycles(edges, "module", "imports")
    assert {tuple(c.members) for c in cycles} == {("a", "b"), ("x",)}
    assert all(e.cycle_ids for e in edges)
    assert cycles[0].example_path[0] == cycles[0].example_path[-1]


# --------------------------------------------------------------------------- JavaScript / TypeScript


def test_js_lexer_masks_comments_strings_and_regex() -> None:
    src = 'const a = "import x from \'nope\'"; // import y from "nope2"\nconst r = /import z from "q"/g;\n' \
          '/* require("c") */ const t = `${require("real")} import w from "tpl"`;\n'
    code, noc = mask(src)
    assert len(code) == len(src) and code.count("\n") == src.count("\n")
    info = parse_js(src)
    assert [i.specifier for i in info.imports] == ["real"]


def test_js_imports_symbols_and_calls() -> None:
    info = parse_js('''
import React, { useState as useS } from "react";
import * as utils from './utils';
import type { T } from "./types";
export { helper } from "./helper";
export * from "./all";
const fs = require("node:fs");
const lazy = () => import("./lazy");

export default function App(props) {
  return utils.format(useS(0)) + local();
}
function local() { return 1 }
export class Store extends Base {
  save(x) { return this.load(x) }
  load(x) { return x }
}
export const arrow = async (a, b) => { return local() };
''')
    specs = {(i.specifier, i.kind) for i in info.imports}
    assert {("react", "import"), ("./utils", "import"), ("./types", "import"), ("./helper", "export-from"),
            ("./all", "export-from"), ("node:fs", "require"), ("./lazy", "dynamic")} <= specs
    types_import = next(i for i in info.imports if i.specifier == "./types")
    assert types_import.type_only
    react = next(i for i in info.imports if i.specifier == "react")
    assert react.bindings == {"useS": ("useState",), "React": ("default",)}
    syms = {s.qualname: s for s in info.symbols}
    assert {"App", "local", "Store", "Store.save", "Store.load", "arrow"} <= set(syms)
    assert syms["App"].default and syms["Store.save"].parent == "Store"
    calls = {(c[0], c[2]) for c in info.calls}
    assert ("App", ("utils", "format")) in calls and ("App", ("local",)) in calls
    assert ("Store.save", ("self", "load")) in calls and ("arrow", ("local",)) in calls


def test_js_resolution_and_call_flow(make_repo) -> None:
    repo = make_repo({
        "package.json": '{"name": "root", "workspaces": ["packages/*"]}',
        "tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["packages/lib/src/*"]}}}',
        "packages/lib/package.json": '{"name": "@acme/lib", "main": "dist/index.js"}',
        "packages/lib/src/index.ts": "export function greet(n: string) { return format(n) }\nfunction format(n) { return n }\n",
        "packages/lib/src/extra.ts": "export const extra = () => 1\n",
        "packages/web/package.json": '{"name": "@acme/web", "dependencies": {"@acme/lib": "workspace:*", "lodash": "^4"}}',
        "packages/web/src/main.ts": 'import { greet } from "@acme/lib";\nimport { extra } from "@lib/extra";\n'
                                    'import { util } from "./util.js";\nimport _ from "lodash";\n'
                                    "export function run() { return greet(util()) + extra() }\n",
        "packages/web/src/util.ts": "export function util() { return 'x' }\n",
        "packages/web/src/App.vue": '<template><div/></template>\n<script setup lang="ts">\nimport { run } from "./main";\nrun();\n</script>\n',
    })
    snap = Repository(repo.path).snapshot("HEAD")
    imports = edges_by_name(snap)
    web = "packages/web/src"
    assert (f"{web}/main", "packages/lib/src/index") in imports  # workspace package -> source, not dist
    assert (f"{web}/main", "packages/lib/src/extra") in imports  # tsconfig paths
    assert (f"{web}/main", f"{web}/util") in imports  # "./util.js" -> util.ts
    assert (f"{web}/App", f"{web}/main") in imports  # Vue <script>
    assert (f"{web}/main", "lodash") in imports
    calls = edges_by_name(snap, "calls")
    assert (f"{web}/main:run", "packages/lib/src/index:greet") in calls
    assert ("packages/lib/src/index:greet", "packages/lib/src/index:format") in calls
    assert (f"{web}/main:run", f"{web}/util:util") in calls
    deps = edges_by_name(snap, "depends-on")
    assert ("@acme/web", "@acme/lib") in deps


def test_go_imports(make_repo) -> None:
    repo = make_repo({
        "go.mod": "module example.com/app\n",
        "cmd/app/main.go": 'package main\n\nimport (\n\t"fmt"\n\tdb "example.com/app/internal/store"\n\t"github.com/x/y/z"\n)\n'
                           "func main() { fmt.Println(db.Open()) }\n",
        "internal/store/store.go": "package store\n\nfunc Open() int { return 1 }\n",
    })
    snap = Repository(repo.path).snapshot("HEAD")
    imports = edges_by_name(snap)
    assert ("example.com/app/cmd/app/main.go", "example.com/app/internal/store") in imports
    assert ("example.com/app/cmd/app/main.go", "fmt") in imports
    assert ("example.com/app/cmd/app/main.go", "github.com/x/y") in imports
    main = snap.find(path="cmd/app/main.go")
    assert "entry-point" in main.tags
    assert snap.find(qualified_name="example.com/app/internal/store.Open") is not None


# --------------------------------------------------------------------------- pipeline


class ExplodingAnalyzer(Analyzer):
    name = "exploding"
    languages = ("brainfuck",)

    def detect(self, ctx):
        return Detection(True, "always")

    def discover_dependencies(self, ctx, b):
        raise RuntimeError("boom")


def test_failing_analyzer_is_isolated(shop_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analyzers, "_REGISTRY", list(analyzers._REGISTRY))
    analyzers.register(ExplodingAnalyzer)
    snap = Repository(shop_repo.path).snapshot("HEAD")
    errors = [d for d in snap.diagnostics if d.code == "analyzer-failed"]
    assert errors and "boom" in errors[0].message
    assert snap.modules and snap.dependency_edges  # everything else still analyzed


def test_analyzers_can_be_disabled(shop_repo) -> None:
    (Path(shop_repo.path) / ".repoviz.toml").write_text('[analyzers]\ndisabled = ["python", "git"]\n')
    snap = Repository(shop_repo.path).snapshot("WORKTREE")
    runs = {a.name for a in snap.analyzers}
    assert "python" not in runs and "git" in runs  # mandatory analyzers cannot be disabled
    langs = {l["language"]: l for l in snap.profile["languages"]}
    assert not langs["python"]["supported"]
    assert any(d.code == "unsupported-language" for d in snap.diagnostics)


def test_snapshot_roundtrip(shop_repo) -> None:
    from repoviz.model import RepositorySnapshot

    snap = Repository(shop_repo.path).snapshot("HEAD")
    again = RepositorySnapshot.from_dict(snap.to_dict())
    assert again.to_dict() == snap.to_dict()
    ids = [n.id for n in snap.nodes()]
    assert len(ids) == len(set(ids))
    assert snap.repository_id.startswith("repo_") and len(snap.revision_id) == 40


def test_grimp_cross_check(make_repo) -> None:
    pytest.importorskip("grimp")
    repo = make_repo({"gpkg/__init__.py": "", "gpkg/a.py": "from gpkg import b\n", "gpkg/b.py": "import os\n"})
    snap = Repository(repo.path).snapshot("WORKTREE")
    diag = [d for d in snap.diagnostics if d.code.startswith("grimp")]
    assert diag and diag[0].code == "grimp-cross-check", diag
    assert snap.metadata["python"]["grimp"]["confirmed_edges"] >= 1
    edge = edges_by_name(snap)[("gpkg.a", "gpkg.b")]
    assert edge.metadata.get("confirmed_by") == "grimp"
    # Revisions other than the working tree are analyzed from Git objects with the AST analyzer only.
    head = Repository(repo.path).snapshot("HEAD")
    assert not [d for d in head.diagnostics if d.code.startswith("grimp")]
