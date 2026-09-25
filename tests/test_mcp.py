"""The read-only MCP server (`repoviz mcp`, #18): protocol conformance, tool answers, caps and no writes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from repoviz.mcp import MAX_RESULT_CHARS, McpServer, fit
from repoviz.repo import Repository

SRC = Path(__file__).resolve().parents[1] / "src"

APP = {
    "app/__init__.py": "",
    "app/routes/__init__.py": "",
    "app/services/__init__.py": "",
    "app/models/__init__.py": "",
    "app/routes/api.py": "from app.services.billing import charge\n\n\ndef post(order):\n    return charge(order)\n",
    "app/services/billing.py": "from app.models.user import User\n\n\ndef charge(order):\n    return User(order).pay()\n",
    "app/models/user.py": "class User:\n    def __init__(self, o):\n        self.o = o\n\n    def pay(self):\n        return 1\n",
    "tests/test_billing.py": "from app.services.billing import charge\n\n\ndef test_charge():\n    assert charge(1) == 1\n",
    ".repoviz.toml": ('[review]\nprotected = ["app/models/**"]\n\n[[contracts]]\nname = "Layers"\ntype = "layers"\n'
                     'layers = ["app.routes", "app.services", "app.models"]\nmessage = "Keep the API thin."\n'),
}


def _call(server: McpServer, name: str, **arguments):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})
    return resp["result"]


def _data(result):
    assert result["isError"] is False, result["content"][0]["text"]
    return result["structuredContent"]


def _server(repo, **kw) -> McpServer:
    server = McpServer(Repository(repo.path), **kw)
    server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    return server


def test_stdio_conformance(make_repo) -> None:
    repo = make_repo(APP)
    repo.commit("app")
    requests = [
        {"jsonrpc": "2.0", "id": "init", "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "architecture_overview", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "where_does_this_go", "arguments": {"target": "app/services/refunds.py"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "impact", "arguments": {"target": "charge"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "dependency_path", "arguments": {"source": "app.routes.api", "target": "app/models/user.py"}}},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
         "params": {"name": "check_scope", "arguments": {"paths": ["app/models/user.py", "app/routes/api.py"]}}},
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "review_current", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "contracts_check", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
         "params": {"name": "check_scope", "arguments": {"paths": ["../../etc/passwd"]}}},
        {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 13, "method": "resources/subscribe"},
        {"jsonrpc": "2.0", "id": 14, "method": "prompts/list"},
        {"jsonrpc": "2.0", "id": 15, "method": "prompts/get",
         "params": {"name": "plan_check", "arguments": {"files": "app/models/user.py, app/routes/new.py"}}},
        [{"jsonrpc": "2.0", "id": 16, "method": "ping"}],
    ]
    lines = [json.dumps(r) for r in requests] + ["{not json"]
    env = dict(os.environ, PYTHONPATH=str(SRC))
    proc = subprocess.run([sys.executable, "-m", "repoviz", "mcp", "-C", repo.path], input="\n".join(lines) + "\n",
                          capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = [json.loads(line) for line in proc.stdout.splitlines()]
    assert all(m["jsonrpc"] == "2.0" for m in out)
    by_id = {m["id"]: m for m in out if m["id"] is not None}
    # one response per request (the notification gets none), ids echoed
    assert sorted(by_id, key=str) == sorted(["init", *range(2, 16)], key=str)
    assert by_id["init"]["result"]["protocolVersion"] == "2025-06-18"
    assert by_id["init"]["result"]["capabilities"]["tools"] == {"listChanged": False}
    tools = {t["name"]: t for t in by_id[2]["result"]["tools"]}
    assert set(tools) == {"architecture_overview", "where_does_this_go", "impact", "dependency_path", "check_scope",
                          "review_current", "contracts_check"}  # no set_scope without --allow-writes
    assert all(t["inputSchema"]["type"] == "object" and t["annotations"]["readOnlyHint"] for t in tools.values())
    assert by_id[3]["result"] == {}
    for i in range(4, 11):
        res = by_id[i]["result"]
        assert res["isError"] is False, (i, res)
        assert res["content"][0]["type"] == "text" and res["structuredContent"]
    where = by_id[5]["result"]["structuredContent"]
    assert where["exists"] is False and where["module"] == "app.services.refunds"
    assert where["layer"]["layer"] == "app.services" and where["layer"]["must_not_import"] == ["app.routes"]
    assert by_id[8]["result"]["structuredContent"]["counts"] == {"allowed": 0, "protected": 1, "out-of-scope": 0,
                                                                 "unscoped": 1}
    assert by_id[11]["result"]["isError"] is True and "refused" in by_id[11]["result"]["content"][0]["text"]
    assert by_id[12]["error"]["code"] == -32602
    assert by_id[13]["error"]["code"] == -32601
    assert {p["name"] for p in by_id[14]["result"]["prompts"]} == {"self_review", "plan_check"}
    text = by_id[15]["result"]["messages"][0]["content"]["text"]
    assert "1 protected" in text and "Do not edit: app/models/user.py" in text
    errors = [m for m in out if m["id"] is None]
    assert sorted(m["error"]["code"] for m in errors) == [-32700, -32600]  # the batch and the bad line


def test_tool_answers(make_repo) -> None:
    repo = make_repo(APP)
    repo.commit("app")
    server = _server(repo)
    overview = _data(_call(server, "architecture_overview"))
    assert overview["contracts"][0]["name"] == "Layers" and overview["contracts"][0]["status"] == "pass"
    impact = _data(_call(server, "impact", target="app.services.billing.charge"))
    [caller] = impact["callers"]  # tests are listed apart
    assert caller["name"] == "app.routes.api.post" and caller["at"] == "app/routes/api.py:4"
    assert caller["how"] == "calls" and caller["evidence"] == "app/routes/api.py:5" and "charge(order)" in caller["code"]
    assert impact["tests_affected"] == ["tests/test_billing.py"]
    path = _data(_call(server, "dependency_path", source="app/routes", target="app.models.user"))
    [chain] = path["paths"]
    assert [s["module"] for s in chain] == ["app.routes.api", "app.services.billing", "app.models.user"]
    assert chain[1]["evidence"] == "app/routes/api.py:1" and chain[2]["code"].startswith("from app.models.user")
    back = _data(_call(server, "dependency_path", source="app.models.user", target="app.routes.api"))
    assert back["paths"] == [] and back["reverse_paths"]
    where = _data(_call(server, "where_does_this_go", target=str(Path(repo.path, "app/models/user.py"))))
    assert where["exists"] and where["scope"]["verdict"] == "protected" and where["scope"]["rule"] == "app/models/**"
    assert where["contracts"][0]["why"] == "Keep the API thin." and where["layer"]["may_import"] == []
    root_file = _data(_call(server, "where_does_this_go", target="setup.py"))
    assert root_file["exists"] is False and root_file["path"] == "setup.py"
    package = _data(_call(server, "where_does_this_go", target="app/services"))
    assert package["module"] == "app.services" and package["layer"]["layer"] == "app.services"
    # errors the agent can fix are tool results, not protocol errors
    for bad in ({"target": "/etc/passwd"}, {"target": "nothing_like_this"}, {"target": "models.nothing"},
                {"target": "x", "extra": 1}, {}):
        res = _call(server, "where_does_this_go", **bad)
        assert res["isError"] is True and res["content"][0]["text"].startswith("Error: ")
    # an edit that breaks the layers: the agent sees it before handing over
    repo.write({"app/models/user.py": APP["app/models/user.py"] + "\nfrom app.routes import api\n"})
    contracts = _data(_call(server, "contracts_check"))
    [v] = contracts["new_violations"]
    assert v["source"] == "app.models.user" and v["path"] == "app/models/user.py"
    review = _data(_call(server, "review_current", min_severity="high"))
    kinds = {f["kind"] for f in review["findings"]}
    assert kinds == {"protected-touched", "contract-broken", "new-cycle"}
    assert all(f["severity"] == "high" for f in review["findings"])


def test_outputs_are_capped_and_deterministic(make_repo) -> None:
    files = {"core.py": "def base():\n    return 1\n"}
    for i in range(250):
        files[f"mods/m{i:03d}.py"] = f"from core import base\n\n\ndef f{i}():\n    return base()\n"
    repo = make_repo(files)
    repo.commit("many")
    server = _server(repo)
    first = _call(server, "impact", target="core.py", max_items=500)  # clamped to the maximum
    data = _data(first)
    assert data["totals"]["callers"] == 250 and data["totals"]["importers"] == 250
    assert len(first["content"][0]["text"]) <= MAX_RESULT_CHARS
    assert data["truncated"] and len(data["callers"]) < 200
    assert _call(server, "impact", target="core.py", max_items=500) == first
    small = _data(_call(server, "impact", target="core.base", max_items=3))
    assert len(small["callers"]) == 3 and small["totals"]["callers"] == 250
    assert [c["name"].rsplit(".", 2)[-2:] for c in small["callers"]] == [["m000", "f0"], ["m001", "f1"], ["m002", "f2"]]
    big = {"items": [{"text": "x" * 100}] * 1000, "note": "y" * 50_000}
    fitted = fit(json.loads(json.dumps(big)))
    assert len(json.dumps(fitted, indent=1)) <= MAX_RESULT_CHARS and fitted["truncated"]


def _state(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def test_nothing_is_written_without_allow_writes(make_repo) -> None:
    repo = make_repo(APP)
    repo.commit("app")
    Repository(repo.path).state.start_session(None, Path(repo.path), label="wave", allowed=["app/routes/**"])
    repo.write({"app/routes/api.py": APP["app/routes/api.py"] + "\n# edited\n"})
    state_dir = Path(os.environ["REPOVIZ_STATE_DIR"])
    status = repo.git("status", "--porcelain")  # (this test's own git status refreshes .git/index: run it first)
    before_state, before_repo = _state(state_dir), _state(Path(repo.path))
    server = _server(repo)
    for name, args in [("architecture_overview", {}), ("where_does_this_go", {"target": "app/routes/api.py"}),
                       ("impact", {"target": "app.models.user.User"}), ("check_scope", {"paths": ["app/x.py"]}),
                       ("dependency_path", {"source": "app.routes.api", "target": "app.models.user"}),
                       ("review_current", {"min_severity": "info"}), ("contracts_check", {})]:
        _data(_call(server, name, **args))
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "prompts/get",
                   "params": {"name": "plan_check", "arguments": {"files": "app/routes/api.py"}}})
    assert _state(state_dir) == before_state and _state(Path(repo.path)) == before_repo
    assert repo.git("status", "--porcelain") == status
    before_repo = _state(Path(repo.path))
    scope = _data(_call(server, "check_scope", paths=["app/routes/api.py", "app/services/billing.py"]))
    assert scope["session"] == "wave" and [f["verdict"] for f in scope["files"]] == ["allowed", "out-of-scope"]
    denied = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "set_scope", "arguments": {"allowed": ["app/**"]}}})
    assert denied["error"]["code"] == -32602
    # --allow-writes adds set_scope, which changes only the session (state directory), never the repository
    writer = _server(repo, allow_writes=True)
    listed = {t["name"]: t for t in writer.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]}
    assert listed["set_scope"]["annotations"]["readOnlyHint"] is False
    _data(_call(writer, "set_scope", allowed=["app/**"]))
    assert Repository(repo.path).current_session().allowed == ["app/**"]
    assert _state(Path(repo.path)) == before_repo


def test_prompts_and_protocol_edges(make_repo) -> None:
    repo = make_repo(dict(APP, **{"app/services/refunds.py": "def post(order):\n    return order\n"}))
    repo.commit("app")
    server = McpServer(Repository(repo.path))
    init = server.handle({"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert init["id"] == 7 and init["result"]["protocolVersion"] == "2025-11-25"
    old = McpServer(Repository(repo.path))
    old.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
    assert "structuredContent" not in _call(old, "architecture_overview")  # not in that protocol revision
    review = server.handle({"jsonrpc": "2.0", "id": 8, "method": "prompts/get",
                            "params": {"name": "self_review", "arguments": {"target": "last-commit"}}})
    assert "review_current with target 'last-commit'" in review["result"]["messages"][0]["content"]["text"]
    assert server.handle({"jsonrpc": "2.0", "id": 9, "method": "prompts/get",
                          "params": {"name": "plan_check", "arguments": {"files": "../x"}}})["error"]["code"] == -32602
    assert server.handle({"jsonrpc": "2.0", "id": True, "method": "ping"})["error"]["code"] == -32600
    assert server.handle({"jsonrpc": "2.0", "id": 1, "result": {}}) is None  # a response from the client
    assert server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "impact", "arguments": []}})["error"]["code"] == -32602
    ambiguous = _call(server, "impact", target="post")
    assert ambiguous["isError"] and "ambiguous (2 matches): app.routes.api.post, app.services.refunds.post" in \
        ambiguous["content"][0]["text"]
    assert _call(server, "impact", target="refunds.post")["isError"] is False  # a unique suffix is enough


def test_shortest_paths() -> None:
    from repoviz.graph import shortest_paths

    adj = {"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": {"e", "a"}, "x": {"a"}}
    assert shortest_paths(["a"], ["e"], adj) == [["a", "b", "d", "e"], ["a", "c", "d", "e"]]
    assert shortest_paths(["a"], ["e"], adj, max_paths=1) == [["a", "b", "d", "e"]]
    assert shortest_paths(["a"], ["x"], adj) == []  # x imports a, not the other way round
    assert shortest_paths(["a", "b"], ["d"], adj) == [["b", "d"]]  # the shortest from any start
    assert shortest_paths(["a"], ["e"], adj, max_depth=2) == []
