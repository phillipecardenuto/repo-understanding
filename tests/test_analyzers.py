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


# --------------------------------------------------------------------------- Java / Kotlin

JAVA_APP = {
    "app/pom.xml": """<project><groupId>com.acme</groupId><artifactId>shop</artifactId><version>1</version><dependencies>
<dependency><groupId>com.google.guava</groupId><artifactId>guava</artifactId></dependency>
<dependency><groupId>com.fasterxml.jackson.core</groupId><artifactId>jackson-databind</artifactId></dependency>
<dependency><groupId>com.fasterxml.jackson.core</groupId><artifactId>jackson-annotations</artifactId></dependency>
<dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId><scope>test</scope></dependency>
</dependencies></project>
""",
    "app/src/main/java/com/acme/shop/web/OrderController.java": """package com.acme.shop.web;

import com.acme.shop.core.*;
import com.acme.shop.util.Strings;
import static com.acme.shop.util.Money.format;
import com.google.common.collect.ImmutableList;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.List;
// import com.acme.shop.core.Ghost;

/** Serves orders. import com.acme.Fake; */
public class OrderController {
    private final OrderService service = new OrderService();
    private static final String S = "import com.acme.shop.util.Nope; Unused";
    private static final String BLOCK = \"\"\"
        Unused text block
        \"\"\";

    public OrderController() { }

    @SuppressWarnings({"unchecked", "rawtypes"})
    public List<Order> list(int page) throws java.io.IOException {
        return ImmutableList.of();
    }

    public List<Order> list(String filter, int page) {
        return Strings.isBlank(filter) ? list(page) : List.of();
    }

    public static void main(String[] args) {
        new ObjectMapper();
        Runnable r = new Runnable() { public void run() { } };
        com.acme.shop.util.Money.format(1);
    }

    private static <T extends Comparable<T>> T max(T a, T b) { return a; }
}
""",
    "app/src/main/java/com/acme/shop/core/Order.java":
        "package com.acme.shop.core;\n\npublic record Order(String id, long cents) {\n    public Order {\n    }\n}\n",
    "app/src/main/java/com/acme/shop/core/OrderService.java": """package com.acme.shop.core;

import com.acme.shop.util.Ghost;
import com.acme.shop.util.R;

public class OrderService {
    Order find(String id) { return new Order(id, 0); }
    interface Listener { void onOrder(Order o); }
    enum State { NEW { @Override String label() { return "n"; } }, PAID; String label() { return name(); } }
}
""",
    "app/src/main/java/com/acme/shop/core/Unused.java": "package com.acme.shop.core;\nclass Unused {}\n",
    "app/src/main/java/com/acme/shop/util/Strings.java":
        "package com.acme.shop.util;\npublic final class Strings { public static boolean isBlank(String s) { return s == null; } }\n",
    "app/src/main/java/com/acme/shop/util/Money.java":
        "package com.acme.shop.util;\npublic final class Money { public static String format(long c) { return \"\" + c; } }\n",
    "app/src/test/java/com/acme/shop/core/OrderServiceTest.java":
        "package com.acme.shop.core;\nimport org.junit.jupiter.api.Test;\nclass OrderServiceTest { @Test void finds() { new OrderService(); } }\n",
    "lib/build.gradle.kts": 'plugins { kotlin("jvm") }\ndependencies {\n    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.0")\n}\n',
    "lib/src/main/kotlin/com/acme/lib/Util.kt": """package com.acme.lib

import kotlinx.coroutines.launch
import com.acme.shop.core.Order

/* block /* nested */ comment */
fun formatOrder(o: Order): String = "order ${o.id}"

fun String.shout(): String = uppercase()
fun Int.shout(): Int = this * 2

const val VERSION = "1"

data class Point(val x: Int, val y: Int)

class Registry<T : Any>(private val name: String) : Iterable<T> {
    private val items = mutableListOf<T>()
    fun add(item: T) { items += item }
    override fun iterator(): Iterator<T> = items.iterator()
    companion object {
        fun empty(): Registry<String> = Registry("empty")
    }
    internal fun size() = items.size
}

fun main() { println(formatOrder(Order("1", 2))) }
""",
    "lib/src/main/kotlin/com/acme/lib/Use.kt": "package com.acme.lib\n\nobject Use {\n    fun run() = Registry.empty().add(VERSION)\n}\n",
}


def test_java_imports_resolve_to_files_and_externals_to_declared_artifacts(make_repo) -> None:
    repo = make_repo(JAVA_APP)
    snap = Repository(repo.path).snapshot("HEAD")
    imports = edges_by_name(snap)
    ctl = "com.acme.shop.web.OrderController"
    got = {t: e for (s, t), e in imports.items() if s == ctl}
    # explicit, static and fully qualified imports, and only the types of a wildcard import the file uses
    assert got["com.acme.shop.util.Strings"].evidence[0].construct == "import"
    assert got["com.acme.shop.util.Money"].evidence[0].construct == "static-import"
    assert got["com.acme.shop.core.OrderService"].evidence[0].construct == "import-on-demand"
    assert "com.acme.shop.core.Order" in got and "com.acme.shop.core.Unused" not in got  # not in comments/strings
    # externals: the declared artifact (jackson-databind, not jackson-annotations), else the JDK package
    assert got["com.google.guava:guava"].metadata["external"]
    assert "com.fasterxml.jackson.core:jackson-databind" in got and "com.fasterxml.jackson.core:jackson-annotations" not in got
    assert "stdlib" in snap.find(qualified_name="java.util").tags
    assert not any("Ghost" in n or "Fake" in n or "Nope" in n for e in got.values() for n in e.metadata["imported_names"])
    # types of the same package need no import
    same = imports[("com.acme.shop.core.OrderService", "com.acme.shop.core.Order")]
    assert same.metadata["same_package"] and same.evidence[0].construct == "same-package"
    test_edge = imports[("com.acme.shop.core.OrderServiceTest", "org.junit.jupiter:junit-jupiter")]
    assert test_edge.metadata["test_only"]
    # a folder of one package is that package; entry points
    assert snap.find(path="app/src/main/java/com/acme/shop/core").qualified_name == "com.acme.shop.core"
    assert snap.find(path="app/src/main/java/com/acme/shop/web/OrderController.java").metadata["entry_kind"] == \
        "java main method"
    # a missing type of an existing package is a broken import; a generated one (R) is not
    broken = [d for d in snap.diagnostics if d.code == "unresolved-internal-import"]
    assert [(d.path.rsplit("/", 1)[-1], d.line) for d in broken] == [("OrderService.java", 3)]


def test_java_symbols_overloads_and_what_is_not_a_declaration(make_repo) -> None:
    repo = make_repo(JAVA_APP)
    snap = Repository(repo.path).snapshot("HEAD")
    syms = {n.qualified_name: n for n in snap.symbols if n.language == "java"}
    ctl = "com.acme.shop.web.OrderController"
    assert syms[f"{ctl}.list(int)"].metadata["signature"] == "(int page) -> List<Order>"
    assert syms[f"{ctl}.list(String, int)"].start_line == 26  # overloads carry their parameter types
    assert syms[f"{ctl}.list(int)"].start_line == 21  # the annotation line belongs to the method
    assert syms[f"{ctl}.OrderController"].metadata["kind"] == "constructor"
    assert syms[f"{ctl}.max"].metadata["public"] is False and syms[f"{ctl}.main"].metadata["public"]
    assert f"{ctl}.run" not in syms  # a method of an anonymous class
    assert syms["com.acme.shop.core.Order"].metadata["kind"] == "record"
    assert syms["com.acme.shop.util.Strings.isBlank"].metadata["public"]
    # an interface method is public, but not in a package-private interface
    assert syms["com.acme.shop.core.OrderService.Listener.onOrder"].metadata["public"] is False
    assert "com.acme.shop.core.OrderService.State.label" in syms
    assert not any(q.endswith(".NEW") or q.endswith("State.NEW.label") for q in syms)  # enum constant bodies
    assert syms["com.acme.shop.core.OrderService"].end_line == 10


def test_kotlin_functions_objects_and_multiplatform_imports(make_repo) -> None:
    repo = make_repo(JAVA_APP)
    snap = Repository(repo.path).snapshot("HEAD")
    imports = edges_by_name(snap)
    assert ("com.acme.lib.Util", "com.acme.shop.core.Order") in imports  # Kotlin importing Java
    assert ("com.acme.lib.Util", "org.jetbrains.kotlinx:kotlinx-coroutines-core") in imports  # by artifact words
    use = imports[("com.acme.lib.Use", "com.acme.lib.Util")]  # Registry and VERSION: same package
    assert use.metadata["same_package"] and set(use.metadata["imported_names"]) >= {"com.acme.lib.Registry"}
    assert snap.find(path="lib/build.gradle.kts").category != "module"  # a build script is configuration
    syms = {n.qualified_name: n for n in snap.symbols if n.language == "kotlin"}
    assert (syms["com.acme.lib.shout(Int.)"].start_line, syms["com.acme.lib.shout(Int.)"].end_line) == (10, 10)
    assert syms["com.acme.lib.formatOrder"].metadata["signature"] == "(o: Order): String"
    assert syms["com.acme.lib.Registry.Companion.empty"].component_type == "method"
    assert syms["com.acme.lib.Registry.size"].metadata["public"] is False  # internal
    assert syms["com.acme.lib.Point"].end_line == 14 and syms["com.acme.lib.Use"].metadata["kind"] == "object"
    assert snap.find(path="lib/src/main/kotlin/com/acme/lib/Util.kt").metadata["entry_kind"] == "kotlin main function"


def test_jvm_parse_cache_round_trip(make_repo) -> None:
    repo = make_repo(JAVA_APP)
    first = Repository(repo.path).snapshot("HEAD")
    again = Repository(repo.path).snapshot("HEAD")  # a new process reads the parse results from disk
    assert sorted(e.id for e in first.dependency_edges) == sorted(e.id for e in again.dependency_edges)
    assert sorted(n.id for n in first.symbols) == sorted(n.id for n in again.symbols)


# --------------------------------------------------------------------------- C#

CS_APP = {
    "src/Shop.Web/Shop.Web.csproj": """<Project Sdk="Microsoft.NET.Sdk.Web"><ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
    <PackageReference Include="Serilog" Version="3.1.1" />
    <PackageReference Include="Serilog.Sinks.Console" Version="5.0.1" />
    <PackageReference Include="System.Text.Json" Version="8.0.0" />
  </ItemGroup><ItemGroup><ProjectReference Include="..\\Shop.Core\\Shop.Core.csproj" /></ItemGroup></Project>
""",
    "src/Shop.Core/Shop.Core.csproj": '<Project Sdk="Microsoft.NET.Sdk"></Project>\n',
    "tests/Shop.Tests/Shop.Tests.csproj":
        '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="xunit" Version="2.6.0" /></ItemGroup></Project>\n',
    "src/Shop.Web/GlobalUsings.cs": "global using Shop.Core.Orders;\nglobal using System.Text.Json;\n",
    "src/Shop.Web/Program.cs": """\ufeffusing Shop.Core.Extensions;
using Shop.Web.Controllers;
using Serilog;

var builder = WebApplication.CreateBuilder(args);
Log.Logger = new LoggerConfiguration().CreateLogger();
builder.Services.AddShopCore();
builder.Build().Run();
""",
    "src/Shop.Web/Controllers/OrdersController.cs": """using Microsoft.AspNetCore.Mvc;
using Newtonsoft.Json.Linq;
using static Shop.Core.Orders.OrderMath;
using Svc = Shop.Core.Orders.OrderService;
// using Shop.Core.Unused;

namespace Shop.Web.Controllers;

/// <summary>Orders: Unused. "using Shop.Fake;"</summary>
[ApiController]
[Route("api/[controller]")]
public class OrdersController : ControllerBase
{
    private readonly Svc _service = new Svc();
    private const string Note = @"C:\\path\\ with ""quotes"" and Unused";

    public OrdersController() : base() { }

    [HttpGet("{id}")]
    [Audited]
    public ActionResult<Order> Get(int id) => _service.Find(id);

    [HttpGet]
    public async Task<IEnumerable<Order>> List(int page, string? filter = null)
    {
        await Task.Yield();
        return new List<Order> { new Order(Total(1, 2), "x") };
    }

    public (int Count, string Name) Stats() { return (1, "a"); }

    public IEnumerable<Order> List(int page) { return List(page, null).Result; }

    private static T Max<T>(T a, T b) where T : IComparable<T> => a.CompareTo(b) > 0 ? a : b;
}
""",
    "src/Shop.Web/Controllers/AuditedAttribute.cs":
        "namespace Shop.Web.Controllers\n{\n    public sealed class AuditedAttribute : System.Attribute { }\n}\n",
    "src/Shop.Core/Orders/Order.cs":
        "namespace Shop.Core.Orders;\n\npublic record Order(int Total, string Id);\n\npublic delegate void OrderPlaced(Order order);\n",
    "src/Shop.Core/Orders/OrderService.cs": """namespace Shop.Core.Orders
{
    using Shop.Core.Generated;

    public partial class OrderService
    {
        public Order Find(int id) => new Order(id, "a");
        public int Count { get; set; }
        public string Name { get; set; }
        internal interface IListener { void OnOrder(Order o); }
        public enum State { New, Paid }
    }
}
""",
    "src/Shop.Core/Orders/OrderMath.cs":
        "namespace Shop.Core.Orders;\npublic static class OrderMath { public static int Total(int a, int b) => a + b; }\n",
    "src/Shop.Core/Name.cs": "namespace Shop.Core;\npublic class Name { }\n",
    "src/Shop.Core/Extensions/ServiceCollectionExtensions.cs": """using Microsoft.Extensions.DependencyInjection;
namespace Shop.Core.Extensions;

public static class ServiceCollectionExtensions
{
    public static IServiceCollection AddShopCore(this IServiceCollection services) => services;
}
""",
    "src/Shop.Core/Unused.cs": "namespace Shop.Core;\ninternal class Unused { }\n",
    "tests/Shop.Tests/OrderServiceTests.cs": """using Xunit;
using Shop.Core.Orders;
namespace Shop.Tests;
public class OrderServiceTests { [Fact] public void Finds() { Assert.NotNull(new OrderService().Find(1)); } }
""",
}


def test_csharp_usings_resolve_like_the_compiler(make_repo) -> None:
    repo = make_repo(CS_APP)
    snap = Repository(repo.path).snapshot("HEAD")
    imports = edges_by_name(snap)
    ctl = "Shop.Web.Controllers.OrdersController"
    got = {t: e for (s, t), e in imports.items() if s == ctl}
    assert got["Shop.Core.Orders.OrderMath"].evidence[0].construct == "using-static"
    assert got["Shop.Core.Orders.OrderService"].evidence[0].construct == "using-alias"
    # a global using of the project, with the evidence in the file that declares it
    order = got["Shop.Core.Orders.Order"]
    assert order.evidence[0].construct == "global-using" and order.evidence[0].path.endswith("GlobalUsings.cs")
    assert got["Shop.Web.Controllers.AuditedAttribute"].metadata["same_package"]  # [Audited]
    assert "Shop.Core.Unused" not in got  # only in comments and strings
    # externals: the NuGet package whose id is the namespace's prefix; framework namespaces are standard
    assert "Newtonsoft.Json" in got and "stdlib" in snap.find(qualified_name="Microsoft.AspNetCore").tags
    program = {t: e for (s, t), e in imports.items() if s == "Program"}
    assert "Serilog" in program and "Serilog.Sinks.Console" not in program  # a BOM before the first using
    ext = program["Shop.Core.Extensions.ServiceCollectionExtensions"]  # an extension method it calls
    assert ext.evidence[0].construct == "extension-method"
    assert "Shop.Web.Controllers.OrdersController" not in program  # a using whose types it does not use
    assert ("Shop.Tests.OrderServiceTests", "xunit") in imports
    assert ("GlobalUsings", "System.Text.Json") in imports  # the declared package, not the framework's System.Text
    # a property called like a type is not a use of it; the generated-looking namespace is not an error
    assert ("Shop.Core.Orders.OrderService", "Shop.Core.Name") not in imports
    assert ("Shop.Core.Orders.OrderService", "Shop.Core.Orders.Order") in imports  # same namespace
    assert not [d for d in snap.diagnostics if d.code == "unresolved-internal-import"]
    assert snap.find(path="src/Shop.Core/Orders").qualified_name == "Shop.Core.Orders"
    assert snap.find(path="src/Shop.Web/Program.cs").metadata["entry_kind"] == "top-level statements"


def test_csharp_symbols_and_signatures(make_repo) -> None:
    repo = make_repo(CS_APP)
    snap = Repository(repo.path).snapshot("HEAD")
    syms = {n.qualified_name: n for n in snap.symbols if n.language == "csharp"}
    ctl = "Shop.Web.Controllers.OrdersController"
    assert syms[f"{ctl}.Get"].metadata["signature"] == "(int id) -> ActionResult<Order>"
    assert (syms[f"{ctl}.Get"].start_line, syms[f"{ctl}.Get"].end_line) == (19, 21)  # attributes included
    assert syms[f"{ctl}.List(int, string?)"].end_line == 28 and f"{ctl}.List(int)" in syms  # overloads
    assert syms[f"{ctl}.Stats"].metadata["signature"] == "() -> (int Count, string Name)"
    assert syms[f"{ctl}.OrdersController"].metadata["kind"] == "constructor"
    assert syms[f"{ctl}.Max"].metadata["public"] is False
    assert syms["Shop.Core.Orders.Order"].metadata["kind"] == "record"
    assert syms["Shop.Core.Orders.OrderPlaced"].metadata["kind"] == "delegate"
    assert syms["Shop.Core.Orders.OrderService.IListener"].metadata["public"] is False  # internal
    assert syms["Shop.Core.Extensions.ServiceCollectionExtensions.AddShopCore"].metadata["extension_method"]
    assert not {"Shop.Core.Orders.OrderService.Count", "Shop.Core.Orders.OrderService.Name"} & syms.keys()  # properties
    assert "Shop.Core.Orders.OrderService.State" in syms


# --------------------------------------------------------------------------- Rust

RUST_APP = {
    "Cargo.toml": '[workspace]\nmembers = ["shop", "util"]\n',
    "shop/Cargo.toml": """[package]
name = "shop-core"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = { version = "1", features = ["derive"] }
serde_json = "1"
shop-util = { path = "../util", package = "util" }

[[bin]]
name = "shopd"
path = "daemon/main.rs"
""",
    "util/Cargo.toml": '[package]\nname = "util"\nversion = "0.1.0"\n',
    "util/src/lib.rs": "pub fn slug(s: &str) -> String { s.to_lowercase() }\n",
    "shop/src/lib.rs": """//! Shop. `use crate::fake::Thing;` in a doc comment
pub mod orders;
mod db;
#[path = "config_impl.rs"]
pub mod config;
pub(crate) mod money {
    pub fn cents(x: u64) -> u64 { x * 100 }
    pub mod fmt;
}
cfg_feature! {
    pub mod extra;
}
#[cfg(windows)]
mod windows_only;

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

pub use orders::Order;

#[derive(Serialize, Deserialize)]
pub struct Shop { pub orders: HashMap<u64, Order> }

impl Shop {
    pub fn new() -> Self { Shop { orders: HashMap::new() } }
    fn secret(&self) -> &'static str { "use crate::db::Ghost;" }
    pub fn total(&self) -> u64 { money::cents(self.orders.len() as u64) }
}

impl std::fmt::Display for Shop {
    fn fmt<'a>(&'a self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { write!(f, "{}", 'x') }
}
""",
    "shop/src/orders/mod.rs": """mod store;
use super::db;
use crate::config::Settings;
pub use self::store::Store;

#[derive(Debug, Clone)]
pub struct Order { pub id: u64, pub buf: [u8; 4] }

pub trait Priced {
    fn price(&self) -> u64;
    fn label(&self) -> String { String::from("x") }
}

pub fn load(id: u64) -> Option<Order> { db::get(id).map(|b| Order { id, buf: b }) }

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn loads() { assert!(load(1).is_none()); }
}
""",
    "shop/src/orders/store.rs": """use crate::{db, money::fmt::render};
pub struct Store;
impl Store { pub fn save(&self) -> String { let _ = db::get(1); render(1) } }
""",
    "shop/src/db.rs": "pub(crate) fn get(_id: u64) -> Option<[u8; 4]> { None }\n",
    "shop/src/config_impl.rs":
        "pub struct Settings { pub name: String }\npub fn dump(s: &Settings) -> String { serde_json::to_string(&s.name).unwrap() }\n",
    "shop/src/money/fmt.rs": "pub fn render(c: u64) -> String { shop_util::slug(&c.to_string()) }\n",
    "shop/src/extra.rs": "pub fn more() -> u8 { 1 }\n",
    "shop/daemon/main.rs": "use shop_core::orders::load;\nfn main() { let _ = load(1); let _ = shop_core::Shop::new(); }\n",
    "shop/tests/it.rs": "use shop_core::Shop;\n#[test]\nfn works() { assert_eq!(Shop::new().total(), 0); }\n",
}


def test_rust_module_tree_and_paths(make_repo) -> None:
    repo = make_repo(RUST_APP)
    snap = Repository(repo.path).snapshot("HEAD")
    imports = edges_by_name(snap)
    mods = {n.path: n.qualified_name for n in snap.modules if n.language == "rust"}
    # the module tree: mod x; files, #[path], inline modules, a module declared inside a macro, a [[bin]] path
    assert mods["shop/src/orders/store.rs"] == "shop_core::orders::store"
    assert mods["shop/src/config_impl.rs"] == "shop_core::config"
    assert mods["shop/src/money/fmt.rs"] == "shop_core::money::fmt"
    assert mods["shop/src/extra.rs"] == "shop_core::extra"
    assert snap.find(path="shop/daemon/main.rs").metadata["entry_kind"] == "rust binary"
    # use trees and paths, resolved down the tree: crate::, super::, self::, groups, a renamed workspace crate
    assert imports[("shop_core::orders", "shop_core::db")].evidence[0].construct == "use"  # use super::db
    assert ("shop_core::orders", "shop_core::config") in imports  # use crate::config::Settings (#[path])
    assert ("shop_core::orders", "shop_core::orders::store") in imports  # pub use self::store::Store
    assert ("shop_core::orders::store", "shop_core::money::fmt") in imports  # use crate::{db, money::fmt::render}
    assert ("shop_core::orders::store", "shop_core::db") in imports
    assert imports[("shop_core::money::fmt", "util")].evidence[0].construct == "path"  # shop_util:: → package util
    assert ("shopd", "shop_core::orders") in imports and ("shopd", "shop_core") in imports  # a binary uses the lib
    assert ("it", "shop_core") in imports and "test" in snap.find(path="shop/tests/it.rs").tags
    # externals: declared crates, std; nothing from comments or strings
    assert ("shop_core", "serde") in imports and "stdlib" in snap.find(qualified_name="std").tags
    assert ("shop_core::config", "serde_json") in imports  # a path in code, no use
    assert not any("fake" in t or "Ghost" in t for (_s, t) in imports)
    # `mod x;` without a file is a broken import, unless a cfg attribute builds it on other platforms only
    assert not [d for d in snap.diagnostics if d.code == "unresolved-internal-import"]
    repo.write({"shop/src/lib.rs": RUST_APP["shop/src/lib.rs"].replace("mod db;", "mod db;\nmod ledger;")})
    broken = [d for d in Repository(repo.path).snapshot("WORKTREE").diagnostics if d.code == "unresolved-internal-import"]
    assert [(d.path, d.line) for d in broken] == [("shop/src/lib.rs", 4)] and "ledger.rs" in broken[0].message


def test_rust_symbols_lifetimes_and_impls(make_repo) -> None:
    repo = make_repo(RUST_APP)
    snap = Repository(repo.path).snapshot("HEAD")
    syms = {n.qualified_name: n for n in snap.symbols if n.language == "rust"}
    assert syms["shop_core::Shop::new"].metadata["signature"] == "() -> Self"
    assert syms["shop_core::Shop::secret"].metadata["public"] is False
    fmt = syms["shop_core::<Shop as Display>::fmt"]  # lifetimes ('a, '_) and a char literal stay intact
    assert fmt.metadata["signature"] == "(&'a self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result"
    assert syms["shop_core::db::get"].metadata["signature"] == "(_id: u64) -> Option<[u8; 4]>"
    assert syms["shop_core::db::get"].metadata["public"] is False  # pub(crate)
    assert syms["shop_core::orders::Priced::price"].component_type == "method"
    assert syms["shop_core::orders::Order"].metadata["kind"] == "struct"
    assert syms["shop_core::money::cents"].start_line == 7
    assert "shop_core::orders::tests::loads" in syms


# --------------------------------------------------------------------------- PHP

PHP_APP = {
    'app/Http/Controllers/Controller.php': '<?php\nnamespace App\\Http\\Controllers;\nabstract class Controller { }\n',
    'app/Http/Controllers/OrderController.php': '<?php\n\ndeclare(strict_types=1);\n\nnamespace App\\Http\\Controllers;\n\nuse App\\Models\\{Order, Customer as Client};\nuse App\\Services\\PriceService;\nuse function App\\Support\\money;\nuse Illuminate\\Http\\Request;\nuse GuzzleHttp\\Client as Http;\nuse App\\Models\\Ghost;\nuse Exception;\n// use App\\Models\\Invoice;  (a comment)\n# use App\\Models\\Refund;\n\n/** Orders. "use App\\Models\\Fake;" */\n#[\\Attribute]\nclass OrderController extends Controller implements \\JsonSerializable\n{\n    use \\App\\Support\\Loggable;\n\n    public function __construct(private readonly PriceService $prices) {}\n\n    public function show(Request $request, int $id): ?Order\n    {\n        $order = Order::find($id);\n        $client = new Client();\n        $msg = <<<EOT\n        new Invoice() in a heredoc\n        EOT;\n        try { money(1); } catch (Exception | \\RuntimeException $e) { }\n        return $order instanceof Order ? $order : null;\n    }\n\n    protected function secret(): string { return \'new App\\Models\\Nope()\'; }\n\n    public function jsonSerialize(): mixed { return []; }\n}\n',
    'app/Models/Customer.php': '<?php\nnamespace App\\Models;\nclass Customer { }\n',
    'app/Models/Order.php': '<?php\nnamespace App\\Models;\n\nuse Illuminate\\Database\\Eloquent\\Model;\n\nclass Order extends Model\n{\n    public function customer(): Customer { return new Customer(); }\n    public static function find(int $id): ?self { return null; }\n}\n',
    'app/Services/PriceService.php': '<?php\nnamespace App\\Services;\n\nuse Monolog\\Logger;\n\ninterface Priced { public function price(): int; }\n\nfinal class PriceService implements Priced\n{\n    public function __construct(private ?Logger $log = null) {}\n    public function price(): int { return \\App\\Support\\money(2); }\n}\n',
    'app/Support/Loggable.php': '<?php\nnamespace App\\Support;\ntrait LoggableAlias { }\n',
    'app/Support/helpers.php': '<?php\nnamespace App\\Support;\n\nfunction money(int $cents): string { return (string) $cents; }\n\ntrait Loggable { public function log(string $m): void {} }\n',
    'composer.json': '{\n  "name": "acme/shop", "type": "project",\n  "require": {"php": "^8.2", "laravel/framework": "^11.0", "monolog/monolog": "^3.0", "guzzlehttp/guzzle": "^7.8"},\n  "require-dev": {"phpunit/phpunit": "^10.5"},\n  "autoload": {"psr-4": {"App\\\\": "app/"}, "files": ["app/Support/helpers.php"]},\n  "autoload-dev": {"psr-4": {"Tests\\\\": "tests/"}}\n}\n',
    'composer.lock': '{"packages": [\n  {"name": "laravel/framework", "autoload": {"psr-4": {"Illuminate\\\\": "src/Illuminate/"}}},\n  {"name": "monolog/monolog", "autoload": {"psr-4": {"Monolog\\\\": "src/Monolog"}}},\n  {"name": "guzzlehttp/guzzle", "autoload": {"psr-4": {"GuzzleHttp\\\\": "src/"}}}\n ], "packages-dev": [{"name": "phpunit/phpunit", "autoload": {"classmap": ["src/"]}}]}\n',
    'legacy/boot.php': "<?php\ninclude 'config.php';\nfunction boot() { return true; }\n",
    'legacy/config.php': "<?php\n$config = ['debug' => false];\n",
    'public/index.php': '<?php\nrequire __DIR__.\'/../vendor/autoload.php\';\nrequire_once __DIR__ . \'/../legacy/boot.php\';\n$app = new \\App\\Http\\Controllers\\OrderController(new \\App\\Services\\PriceService());\n?>\n<html><body><?= "use App\\Models\\Html;" ?> new Invisible() </body></html>\n',
    'tests/Feature/OrderTest.php': '<?php\nnamespace Tests\\Feature;\nuse PHPUnit\\Framework\\TestCase;\nuse App\\Models\\Order;\nclass OrderTest extends TestCase { public function test_find(): void { $this->assertNull(Order::find(1)); } }\n',
}


BS = "\\"  # PHP names are backslash-separated


def php(name: str) -> str:
    return name.replace("/", BS)


def test_php_names_resolve_through_imports_namespaces_and_psr4(make_repo) -> None:
    repo = make_repo(PHP_APP)
    snap = Repository(repo.path).snapshot("HEAD")
    imports = edges_by_name(snap)
    ctl = php("App/Http/Controllers/OrderController")
    got = {t: e for (s, t), e in imports.items() if s == ctl}
    # use (grouped, aliased), use function, the same namespace (extends Controller); a trait in the class body
    assert got[php("App/Models/Order")].evidence[0].construct == "use"
    assert php("App/Models/Customer") in got and php("App/Services/PriceService") in got
    assert got[php("App/Support/helpers")].metadata["imported_names"] == [php("App/Support/money")]
    assert got[php("App/Http/Controllers/Controller")].metadata["same_package"]
    # externals: the package composer.lock says provides the namespace; one-segment names are PHP's own
    assert "laravel/framework" in got and "guzzlehttp/guzzle" in got and "stdlib" in snap.find(qualified_name="php").tags
    assert (php("Tests/Feature/OrderTest"), "phpunit/phpunit") in imports  # by name: no PSR-4 in the lock
    assert (php("App/Services/PriceService"), "monolog/monolog") in imports
    # nothing from comments, strings, heredocs or the HTML around <?php ?>
    assert not any(w in t for t in got for w in ("Invoice", "Refund", "Fake", "Nope", "Html", "Invisible"))
    # fully qualified names and require / include of literal paths
    index = {t: e.evidence[0].construct for (s, t), e in imports.items() if s == "index"}
    assert index == {ctl: "qualified-name", php("App/Services/PriceService"): "qualified-name", "boot": "require"}
    assert ("boot", "config") in imports
    assert snap.find(path="public/index.php").metadata["entry_kind"] == "php front controller"
    # a class under the project's PSR-4 prefix whose file is missing is a broken import
    broken = [(d.path, d.line) for d in snap.diagnostics if d.code == "unresolved-internal-import"]
    assert broken == [("app/Http/Controllers/OrderController.php", 12)]
    assert snap.find(path="app/Models").qualified_name == php("App/Models")


def test_php_symbols_and_signatures(make_repo) -> None:
    repo = make_repo(PHP_APP)
    snap = Repository(repo.path).snapshot("HEAD")
    syms = {n.qualified_name: n for n in snap.symbols if n.language == "php"}
    ctl = php("App/Http/Controllers/OrderController")
    assert syms[f"{ctl}::show"].metadata["signature"] == "(Request $request, int $id): ?Order"
    assert (syms[f"{ctl}::show"].start_line, syms[f"{ctl}::show"].end_line) == (25, 34)
    assert syms[f"{ctl}::secret"].metadata["public"] is False
    assert syms[php("App/Services/Priced")].metadata["kind"] == "interface"
    assert syms[php("App/Support/Loggable")].metadata["kind"] == "trait"
    assert syms[php("App/Support/money")].component_type == "function"
    assert "boot" in syms  # a function outside any namespace


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


# --------------------------------------------------------------------------- runtime coupling (#23)

RUNTIME = {
    "app/__init__.py": "",
    "app/settings.py": "import os\n\nENGINE_IMAGE = \"engine:latest\"\nSCANNER_IMAGE = \"tools/scanner:2\"\n"
                       "CBIR_HOST = os.getenv(\"CBIR_HOST\", \"localhost\")\n"
                       "CBIR_URL = os.getenv(\"CBIR_URL\", f\"http://{CBIR_HOST}:8000\")\n",
    "app/worker.py": "import docker\n\nfrom app import settings\n\n\ndef run():\n    client = docker.from_env()\n"
                     "    return client.containers.run(settings.ENGINE_IMAGE)\n",
    "app/search.py": "import requests\n\n\ndef search(q):\n"
                     "    return requests.post(\"http://cbir-service:8000/search\", json={\"q\": q})\n",
    "app/via_env.py": "import requests\n\nfrom app.settings import CBIR_URL\n\n\ndef health():\n"
                      "    return requests.get(f\"{CBIR_URL}/health\")\n",
    "app/scan.py": "import subprocess\n\nfrom app.settings import SCANNER_IMAGE\n\n\ndef scan(p):\n"
                   "    subprocess.run(\"docker run --rm -v /data:/data tools/scanner:2 --fast\", shell=True)\n"
                   "    return [\"docker\", \"run\", SCANNER_IMAGE]\n",
    "app/other.py": "import requests\n\nIMAGE = \"someone/else:1\"\n\n\ndef ping():\n"
                    "    return requests.get(\"https://api.example.com/v1/ping\")\n",
    "modules/engine/Dockerfile": "FROM python:3.12\n",
    "modules/engine/run.py": "print('engine')\n",
    "cbir/Dockerfile": "FROM python:3.12\nCMD [\"python\", \"main.py\"]\n",
    "cbir/main.py": "print('cbir')\n",
    "tools/Dockerfile": "FROM alpine\n",
    "Makefile": "images:\n\tdocker build -t tools/scanner:2 \\\n\t    ./tools\n",
    "docker-compose.yml": "services:\n  engine:\n    build: ./modules/engine\n    image: engine:latest\n"
                          "  cbir-service:\n    build: ./cbir\n  worker:\n    build: .\n"
                          "    command: celery -A app.worker worker\n    environment:\n      CBIR_HOST: cbir-service\n",
}


def test_runtime_edges_from_images_and_service_urls(make_repo) -> None:
    repo = make_repo(RUNTIME)
    snap = Repository(repo.path).snapshot("WORKTREE")
    idx = snap.node_index()
    edges = {(idx[e.source_id].path or idx[e.source_id].name, idx[e.target_id].path or "", idx[e.target_id].name,
              e.relationship): e for e in snap.dependency_edges if e.relationship in ("invokes-container", "talks-to")}
    # the acceptance case: a settings constant, imported and run through the Docker SDK
    run = edges[("app/worker.py", "modules/engine", "engine", "invokes-container")]
    assert run.evidence[0].start_line == 8 and "containers.run" in run.evidence[0].excerpt
    assert run.metadata["label"] == "engine:latest" and run.metadata["via"] == "ENGINE_IMAGE"
    # an image built by `docker build -t` in a Makefile, started from a shell string and from a command list
    scan = edges[("app/scan.py", "tools", "tools", "invokes-container")]
    assert scan.metadata["image"] == "tools/scanner" and "Makefile" in scan.metadata["provided_by"]
    # URLs naming a service: a literal, and a constant whose host variable Compose points at the service
    talks = edges[("app/search.py", "docker-compose.yml", "cbir-service", "talks-to")]
    assert talks.metadata["label"] == "http:8000"
    assert ("app/via_env.py", "docker-compose.yml", "cbir-service", "talks-to") in edges
    assert not any(k[0] == "app/settings.py" for k in edges)  # defining a URL is not calling it
    # unknown images and hosts: no edge, but listed on the module
    other = snap.find(path="app/other.py")
    assert not any(k[0] == "app/other.py" for k in edges)
    assert other.metadata["external_runtime_references"] == [
        {"kind": "url", "value": "https://api.example.com", "line": 7, "via": "literal"}]
    # between services, for the System view: the worker (whose code starts the engine) → the engine service
    svc = {n.name: n.id for n in snap.components if n.component_type == "service"}
    derived = {(idx[e.source_id].name, idx[e.target_id].name, e.relationship) for e in snap.dependency_edges
               if e.source_id in svc.values() and e.metadata.get("from_code")}
    assert ("worker", "engine", "invokes-container") in derived and ("worker", "cbir-service", "talks-to") in derived


def test_docker_command_parsing() -> None:
    from repoviz.analyzers.runtime import _docker_build_tags, docker_run_image

    assert docker_run_image(["docker", "run", "--rm", "-v", "a:b", "-e", "X=1", "--gpus", "all", "img:1", "cmd"]) == "img:1"
    assert docker_run_image(["docker", "create", "--name", "x", "reg.io/team/app"]) == "reg.io/team/app"
    assert docker_run_image(["docker", "ps"]) is None
    tags = _docker_build_tags("build:\n\tdocker buildx build --platform linux/amd64 -t org/api:1 -f api/Dockerfile api\n"
                              "\tdocker build -t $(IMAGE) .\n# docker build -t commented/out .\n")
    assert tags == [(2, "org/api:1", "api")]


# --------------------------------------------------------------------------- Python: sys.path edits (#25)


def test_sys_path_patterns_are_read_statically() -> None:
    def edits(code: str) -> list:
        return parse_python(code).sys_paths

    assert edits("import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))\n") \
        == [["dir", "../app", 2, "insert"]]
    assert edits("root = Path(__file__).parent.parent\nif str(root) not in sys.path:\n    sys.path.insert(0, str(root))\n") \
        == [["dir", "..", 3, "insert"]]
    assert edits("sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n") \
        == [["dir", "..", 1, "append"]]
    assert edits("here = Path(__file__).resolve().parent\nsys.path.append(str(here.parent / 'src'))\n") \
        == [["dir", "../src", 2, "append"]]
    assert edits("FILE = Path(__file__).absolute()\nsys.path.append(FILE.parents[0].as_posix())\n") \
        == [["dir", ".", 2, "append"]]
    assert edits("sys.path.insert(0, str(Path(__file__).parents[1].joinpath('libs')))\n") == [["dir", "../libs", 1, "insert"]]
    assert edits("sys.path.append(os.path.dirname(__file__) + '/../lib')\n") == [["dir", "../lib", 1, "append"]]
    assert edits("try:\n    import x\nexcept ImportError:\n    sys.path.extend(['src', 'lib'])\n") \
        == [["cwd", "src", 4, "append"], ["cwd", "lib", 4, "append"]]
    # Unknowable without running the code: environment, working directory, absolute paths, function bodies.
    assert edits("sys.path.insert(0, os.environ['APP_HOME'])\nsys.path.insert(0, os.getcwd())\n"
                 "sys.path.append('/opt/lib')\nsys.path.append(f'{BASE}/x')\n"
                 "def setup():\n    sys.path.insert(0, 'src')\n") == []
    root = "ROOT = Path(__file__).parent\nROOT = get_root()\nsys.path.insert(0, str(ROOT))\n"
    assert edits(root) == []  # the name was rebound to something unknown


SYS_PATH_APP = {
    "app/__init__.py": "",
    "app/schemas.py": "class ImageResponse:\n    pass\n\n\ndef make():\n    return ImageResponse()\n",
    "app/utils/__init__.py": "",
    "app/utils/panels.py": "def parse():\n    return 1\n",
    "tests/test_x.py": (
        "import os\nimport sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))\n\n"
        "import schemas\nfrom utils.panels import parse\n\n\ndef test_it():\n    assert schemas.make() and parse()\n"),
}


def test_imports_through_sys_path_edits_are_internal(make_repo) -> None:
    snap = Repository(make_repo(SYS_PATH_APP).path).snapshot("HEAD")
    imports = edges_by_name(snap)
    edge = imports[("test_x", "app.schemas")]
    assert edge.metadata["via"] == "sys.path" and edge.metadata["sys_path_edit"] == "tests/test_x.py:3"
    assert imports[("test_x", "app.utils.panels")].metadata["via"] == "sys.path"
    assert not [n for n in snap.nodes() if "external" in n.tags and n.name in ("schemas", "utils")]
    calls = edges_by_name(snap, "calls")
    assert ("test_x.test_it", "app.schemas.make") in calls and ("test_x.test_it", "app.utils.panels.parse") in calls
    [diag] = [d for d in snap.diagnostics if d.code == "sys-path-imports"]
    assert diag.path == "tests/test_x.py" and diag.line == 3 and "tests/test_x.py (2)" in diag.message


def test_conftest_sys_path_applies_to_its_directory_tree(make_repo) -> None:
    repo = make_repo({
        **{k: v for k, v in SYS_PATH_APP.items() if not k.startswith("tests/")},
        "tests/conftest.py": "import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).parent.parent / 'app'))\n",
        "tests/test_a.py": "import schemas\n",
        "tests/unit/test_b.py": "from utils import panels\n",
        "scripts/run.py": "import schemas\n",  # outside the conftest's tree: still an unknown package
    })
    imports = edges_by_name(Repository(repo.path).snapshot("HEAD"))
    assert imports[("test_a", "app.schemas")].metadata["sys_path_edit"] == "tests/conftest.py:4"
    assert imports[("test_b", "app.utils.panels")].metadata["via"] == "sys.path"
    assert ("run", "app.schemas") not in imports and ("run", "schemas") in imports


def test_sys_path_order_script_directory_and_bounds(make_repo) -> None:
    repo = make_repo({
        "schemas.py": "X = 1\n",  # what `import schemas` normally reaches from the repository root
        "app/__init__.py": "", "app/schemas.py": "Y = 1\n",
        "first.py": "import sys\nsys.path.insert(0, 'app')\nimport schemas\n",  # inserted: searched first
        "last.py": "import sys\nsys.path.append('app')\nimport schemas\n",  # appended: searched last
        # a script's own directory is searched too (sys.path[0]), even when its edit points elsewhere
        "svc/src/main.py": "import os, sys\nsys.path.append(os.path.dirname(os.path.dirname(__file__)))\nimport config\n",
        "svc/src/config.py": "PORT = 1\n", "svc/src/__init__.py": "", "svc/__init__.py": "",
        "gone.py": "import sys\nsys.path.insert(0, '../outside')\nsys.path.insert(0, 'missing')\nimport nothing\n",
    })
    snap = Repository(repo.path).snapshot("HEAD")
    imports = edges_by_name(snap)
    assert imports[("first", "app.schemas")].metadata["via"] == "sys.path" and ("first", "schemas") not in imports
    assert "via" not in imports[("last", "schemas")].metadata and ("last", "app.schemas") not in imports
    assert imports[("svc.src.main", "svc.src.config")].metadata["via"] == "sys.path"
    assert ("gone", "nothing") in imports and imports[("gone", "nothing")].metadata.get("external")


# --------------------------------------------------------------------------- cross-service contracts (#13)

API_APP = {
    "backend/app/__init__.py": "",
    "backend/app/main.py": ("import os\n\nfrom fastapi import FastAPI\n\nfrom app.routes import images\n\napp = FastAPI()\n"
                            "app.include_router(images.router, prefix=\"/api\")\nDB_URL = os.environ[\"DATABASE_URL\"]\n"
                            "DEBUG = os.getenv(\"APP_DEBUG\", \"0\")\n"),
    "backend/app/routes/__init__.py": "",
    "backend/app/routes/images.py": ("from fastapi import APIRouter\n\nfrom app.tasks import process_image\n\n"
                                     "router = APIRouter(prefix=\"/images\")\n\n\n@router.get(\"/{image_id}\")\n"
                                     "def get_image(image_id: str):\n    process_image.delay(image_id)\n    return {}\n\n\n"
                                     "@router.post(\"\")\ndef upload():\n    return {}\n"),
    "backend/app/tasks.py": "from celery import shared_task\n\n\n@shared_task\ndef process_image(image_id):\n    return image_id\n",
    "frontend/src/api.js": ("export async function getImage(id) {\n  const r = await fetch(`/api/images/${id}`);\n"
                            "  return r.json();\n}\nexport const upload = (body) => fetch(\"/api/images\", { method: \"POST\", body });\n"
                            "const base = process.env.API_BASE || \"\";\n"),
    "docker-compose.yml": ("services:\n  api:\n    build: ./backend\n    environment:\n      DATABASE_URL: postgres://db/app\n"
                           "  db:\n    image: postgres:16\n"),
    ".env.example": "APP_DEBUG=0\n",
}


def test_routes_tasks_and_their_consumers_become_nodes_and_edges(make_repo) -> None:
    snap = Repository(make_repo(API_APP).path).snapshot("WORKTREE")
    idx = snap.node_index()
    routes = {n.name: n for n in snap.symbols if n.component_type == "http-route"}
    assert set(routes) == {"GET /api/images/{image_id}", "POST /api/images"}  # decorator + router + include prefixes
    get = routes["GET /api/images/{image_id}"]
    assert get.metadata["template"] == "/api/images/{}" and get.metadata["handler"] == "get_image"
    assert get.metadata["mounted"] and get.path == "backend/app/routes/images.py" and "api" in get.tags
    [task] = [n for n in snap.symbols if n.component_type == "task"]
    assert task.name == "app.tasks.process_image" and task.metadata["required"] == ["image_id"]
    edges = {(e.relationship, idx[e.source_id].path, idx[e.target_id].name): e for e in snap.dependency_edges
             if e.relationship in ("calls-http", "enqueues", "reads-env")}
    calls = edges[("calls-http", "frontend/src/api.js", "GET /api/images/{image_id}")]
    assert calls.confidence == 0.6 and calls.evidence[0].start_line == 2 and calls.metadata["label"] == "GET /api/images/{}"
    assert edges[("calls-http", "frontend/src/api.js", "POST /api/images")].confidence == 0.7
    assert ("enqueues", "backend/app/routes/images.py", "app.tasks.process_image") in edges
    assert edges[("reads-env", "backend/app/main.py", "api")].metadata["env_keys"] == ["DATABASE_URL"]
    assert ("reads-env", "backend/app/main.py", ".env.example") in edges
    root = next(n for n in snap.components if n.component_type == "repository")
    assert root.metadata["env_declared"] == {"APP_DEBUG": [".env.example:1"],
                                             "DATABASE_URL": ["docker-compose.yml (service api)"]}
    api_js = idx[next(e.source_id for e in snap.dependency_edges if e.relationship == "calls-http")]
    assert api_js.metadata["env_reads"] == [{"name": "API_BASE", "line": 6, "default": True}]


def test_flask_express_urls_env_readers_and_declarations() -> None:
    from repoviz import interfaces as itf

    flask = ('from flask import Blueprint, Flask\nbp = Blueprint("docs", __name__, url_prefix="/docs")\n\n'
             '@bp.route("/<int:doc_id>", methods=["GET", "DELETE"])\ndef doc(doc_id): ...\n\n'
             'app = Flask(__name__)\napp.register_blueprint(bp, url_prefix="/api/v1/docs")\n'
             'import requests, httpx, os\nBASE = os.getenv("API", "http://x")\n'
             'requests.get(f"{BASE}/api/v1/docs/{7}")\nrequests.post(BASE + "/api/v1/docs/" + str(7))\n'
             'client = httpx.Client(base_url="http://svc:8000/api")\nclient.get("/items/1")\nrequests.get(url)\n'
             'class S(BaseSettings):\n    model_config = SettingsConfigDict(env_prefix="app_")\n    token: str\n'
             '    level: int = 3\nos.environ["X_SET"] = "1"\n')
    r = itf.python_interfaces(flask)
    assert r["routes"] == [{"owner": "bp", "methods": ["GET", "DELETE"], "path": "/<int:doc_id>", "line": 4, "handler": "doc"}]
    assert r["includes"] == [{"owner": "app", "router": "bp", "prefix": "/api/v1/docs", "line": 8}]
    assert [(c["method"], c["template"]) for c in r["http_calls"]] == [
        ("GET", "/api/v1/docs/{}"), ("POST", "/api/v1/docs/{}"), ("GET", "/api/items/1")]  # requests.get(url): unknown
    assert [(e["name"], e["default"]) for e in r["env_reads"]] == [("API", True), ("APP_TOKEN", False), ("APP_LEVEL", True)]
    js = ('const express = require("express");\nconst app = express();\nconst images = require("./routes/images");\n'
          'app.use("/api/images", images);\napp.get("/health", (req, res) => res.send("ok"));\n'
          'const api = axios.create({ baseURL: "/api" });\napi.get(`/images/${id}/thumb`);\n'
          'fetch(API + "/api/x", { method: "PUT" });\nfetch(url);\nfetch(`${a}${b}`);\n'
          'const k = process.env.SECRET_KEY;\nconst v = import.meta.env.VITE_API ?? "x";\n// fetch("/api/commented")\n')
    j = itf.js_interfaces(js)
    assert [(x["owner"], x["path"]) for x in j["routes"]] == [("app", "/health")]  # api is an axios instance
    assert j["mounts"] == [{"owner": "app", "prefix": "/api/images", "router": "images", "line": 4}] and j["apps"] == ["app"]
    assert [(c["method"], c["template"]) for c in j["http_calls"]] == [("PUT", "/api/x"), ("GET", "/api/images/{}/thumb")]
    assert [(e["name"], e["default"]) for e in j["env_reads"]] == [("SECRET_KEY", False), ("VITE_API", True)]
    assert itf.env_declarations("deploy/Dockerfile", "FROM x\nENV A=1 B=2\nARG C\nENV D 4\n") == \
        [("A", 2), ("B", 2), ("C", 3), ("D", 4)]
    assert itf.env_declarations("k8s/api.yaml", "spec:\n  env:\n    - name: TOKEN\n      value: x\n") == [("TOKEN", 3)]
    assert itf.env_declarations(".env.example", "# c\nexport A=1\nB = 2\n") == [("A", 2), ("B", 3)]
    assert itf.normalize_path("http://h:1/a/:id/<int:x>/{y}/?q=1") == "/a/{}/{}/{}" and itf.normalize_path("{}") is None
    assert itf.match("/api/images/recent", "/api/images/{}") and not itf.match("/api/{}/x", "/api/images/x")
    task = {"positional": ["a", "b"], "required": ["a", "b"], "kwonly": [], "var_positional": False, "var_keyword": False}
    assert itf.call_fits({"positional": 1, "keywords": ["b"]}, task) and itf.call_fits({"positional": 1, "keywords": []}, task) is False
    assert itf.call_fits({"positional": None, "keywords": None}, task) is None


def test_express_router_mounted_from_another_file(make_repo) -> None:
    repo = make_repo({"server/app.js": ('const express = require("express");\nconst app = express();\n'
                                        'const images = require("./routes/images");\napp.use("/api/images", images);\n'),
                      "server/routes/images.js": ('const express = require("express");\nconst router = express.Router();\n'
                                                  'router.get("/:id", (req, res) => res.json({}));\nmodule.exports = router;\n'),
                      "web/client.js": "export const one = (id) => fetch(`/api/images/${id}`);\n"})
    snap = Repository(repo.path).snapshot("WORKTREE")
    routes = [n for n in snap.symbols if n.component_type == "http-route"]
    assert [(n.name, n.metadata["mounted"]) for n in routes] == [("GET /api/images/:id", True)]
    idx = snap.node_index()
    assert [(idx[e.source_id].path, idx[e.target_id].name) for e in snap.dependency_edges
            if e.relationship == "calls-http"] == [("web/client.js", "GET /api/images/:id")]


# ---------------------------------------------------------------- health metrics (#30)

COMPLEX_PY = '''\
"""Module docstring."""
import os

# a comment line: not code
LIMIT = 10 if os.name == "nt" else 20


def classify(n, flags):
    if n < 0 and flags or n == 0:
        return "small"
    elif n < LIMIT:
        for f in flags:
            while f:
                f -= 1
    else:
        pass
    try:
        n = int(n)
    except ValueError:
        return None
    except (TypeError, KeyError):
        return None
    finally:
        pass
    return [x for x in flags if x if n]


class Box:
    SIZES = [s for s in range(3)]

    def get(self, key):
        with open(key) as fh:
            def inner():
                return key or fh
            return inner()
'''


def test_python_cyclomatic_complexity_matches_a_hand_count() -> None:
    from repoviz.analyzers.python import parse_python

    info = parse_python(COMPLEX_PY)
    got = {s.qualname: (s.complexity, s.nesting) for s in info.symbols if s.kind in ("function", "method")}
    # classify: 1 + if + (and, or) 2 + elif + for + while + 2 except + comprehension (for + 2 ifs) 3 = 12;
    # nesting: elif (same depth as its if) > for > while = 3; else, try, finally and with add nothing
    assert got["classify"] == (12, 3)
    assert got["Box.get"] == (1, 1)  # with: nesting only
    assert got["Box.get.inner"] == (2, 0)  # `or` in a nested function counts there, not in get
    # module: its functions (12 + 1 + 2) + the conditional expression + the class-level comprehension
    assert info.complexity == 17 and info.max_nesting == 3
    assert info.sloc == len([ln for ln in COMPLEX_PY.splitlines() if ln.strip() and not ln.strip().startswith("#")])


def test_whitespace_complexity_and_code_lines_for_other_languages() -> None:
    from repoviz import metrics

    js = "// header comment\nfunction a(x) {\n  if (x) {\n    for (const y of x) {\n      use(y);\n    }\n  }\n" \
         "  /* block\n   * comment */\n  return x;\n}\n"
    m = metrics.text_metrics(js, "javascript")
    # 2-space indentation: levels 0, 1, 2, 3, 2, 1, 1, 0 over the code lines (comments and blank lines skipped)
    assert m == {"sloc": 8, "complexity": 10, "complexity_kind": "whitespace", "max_nesting": 3}
    go = "package x\n\nfunc a() {\n\tif b {\n\t\tc()\n\t}\n}\n"
    assert metrics.text_metrics(go, "go")["complexity"] == 1 + 2 + 1  # tabs are whole levels
    assert metrics.sloc(["x = 1", "", "   # note", "y = 2  # trailing"], "python") == 2
    assert metrics.bars(3) == "▮▮▮▯" and metrics.bars(0) == "▯▯▯▯"


def test_jvm_package_roots_keep_sibling_libraries_external() -> None:
    from repoviz.analyzers.jvm import _import_package, _roots

    assert _roots({"com.google.common.base", "com.google.common.collect", "com.google.thirdparty.publicsuffix"}) == \
        {"com.google.common", "com.google.thirdparty.publicsuffix"}  # com.google.errorprone stays external
    assert _roots({"org.apache.commons.lang3", "org.apache.commons.lang3.builder"}) == {"org.apache.commons.lang3"}
    assert _import_package("kotlinx.coroutines.launch", False) == "kotlinx.coroutines"  # a top-level function
    assert _import_package("a.b.Outer.Inner", False) == "a.b" and _import_package("a.b", True) == "a.b"
