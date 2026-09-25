"""Graph queries: why A depends on B, and blast radius (#28)."""

from __future__ import annotations

import json

import pytest

from repoviz.cli import main
from repoviz.flow import affected_flow
from repoviz.query import GraphIndex, QueryError, blast_radius, why
from repoviz.repo import Repository

CHAIN = {  # ui → forms → services → db, a main block and a test
    "ui/__init__.py": "",
    "ui/views.py": "from ui import forms\n\n\ndef page():\n    return forms.render()\n",
    "ui/forms.py": "from services import orders\n\n\ndef render():\n    return orders.list_orders()\n",
    "services/__init__.py": "",
    "services/orders.py": "from db import models\n\n\ndef list_orders():\n    return models.query()\n",
    "db/__init__.py": "",
    "db/models.py": "def query():\n    return []\n",
    "tests/test_orders.py": "from services.orders import list_orders\n\n\ndef test_list():\n    assert list_orders() == []\n",
    "main.py": "from ui.views import page\n\nif __name__ == '__main__':\n    page()\n",
}


def index(repo) -> GraphIndex:
    return Repository(repo.path).graph_index()


def names(chain: list[dict]) -> list[str]:
    return [s["name"] for s in chain]


def test_why_follows_imports_with_evidence(make_repo, capsys) -> None:
    repo = make_repo(CHAIN)
    idx = index(repo)
    res = why(idx, idx.resolve("ui"), idx.resolve("db"))
    assert res["level"] == "imports" and [names(p) for p in res["paths"]] == [["ui.forms", "services.orders", "db.models"]]
    hop = res["paths"][0][1]
    assert hop["how"] == "imports" and hop["evidence"] == "ui/forms.py:1" and hop["code"] == "from services import orders"
    # the CLI: one line per chain, then file:line per hop; --json carries the evidence
    assert main(["why", "-C", repo.path, "ui", "db"]) == 0
    text = capsys.readouterr().out
    assert "ui depends on db: 1 shortest chain(s) of 2 imports." in text
    assert "chain 1: ui.forms → services.orders → db.models" in text
    assert "ui.forms imports services.orders  (ui/forms.py:1)  from services import orders" in text
    assert "services.orders imports db.models  (services/orders.py:1)" in text
    assert main(["why", "-C", repo.path, "ui/views.py", "db.models", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert names(data["paths"][0]) == ["ui.views", "ui.forms", "services.orders", "db.models"]
    assert [s.get("evidence") for s in data["paths"][0]] == [None, "ui/views.py:1", "ui/forms.py:1", "services/orders.py:1"]


def test_why_between_symbols_uses_calls(make_repo) -> None:
    idx = index(make_repo(CHAIN))
    res = why(idx, idx.resolve("ui.views.page"), idx.resolve("db.models.query"))
    assert res["level"] == "calls"
    [chain] = res["paths"]
    assert names(chain) == ["ui.views.page", "ui.forms.render", "services.orders.list_orders", "db.models.query"]
    assert chain[1]["how"] == "calls" and chain[1]["evidence"] == "ui/views.py:5"


def test_why_several_paths_no_path_and_cycles(make_repo, capsys) -> None:
    files = dict(CHAIN)
    files["ui/admin.py"] = "from services import orders\n"  # a second chain of the same length
    files["db/cache.py"] = "import db.models\n"
    files["db/models.py"] = "import db.cache\n\n\ndef query():\n    return []\n"  # a cycle inside db
    repo = make_repo(files)
    idx = index(repo)
    res = why(idx, idx.resolve("ui"), idx.resolve("db"))
    assert sorted(names(p)[0] for p in res["paths"]) == ["ui.admin", "ui.forms"]
    assert len(why(idx, idx.resolve("ui"), idx.resolve("db"), max_paths=1)["paths"]) == 1
    loop = why(idx, idx.resolve("db.cache"), idx.resolve("db.models"))
    assert [names(p) for p in loop["paths"]] == [["db.cache", "db.models"]]
    none = why(idx, idx.resolve("db"), idx.resolve("ui"))
    assert none["paths"] == [] and none["reverse_paths"] and "but ui depends on db" in none["summary"]
    assert main(["why", "-C", repo.path, "db", "ui"]) == 0
    assert "reverse chain 1: ui." in capsys.readouterr().out
    capped = why(idx, idx.resolve("ui.views"), idx.resolve("db.models"), max_len=2)
    assert capped["paths"] == []  # 3 imports needed
    with pytest.raises(QueryError, match="nothing named"):
        idx.resolve("nosuch")
    assert main(["why", "-C", repo.path, "nosuch", "db"]) == 2
    assert "nothing named 'nosuch'" in capsys.readouterr().err


def test_blast_radius_ranks_callers_and_matches_affected_flow(make_repo, capsys) -> None:
    repo = make_repo(CHAIN)
    idx = index(repo)
    res = blast_radius(idx, idx.resolve("db.models.query"))
    ranked = [(d["name"], d["distance"]) for d in res["dependents"]]
    assert ranked[0] == ("services.orders.list_orders", 1)
    assert ("ui.forms.render", 2) in ranked and ("ui.views.page", 3) in ranked
    assert [d["distance"] for d in res["dependents"]] == sorted(d["distance"] for d in res["dependents"])
    assert res["dependents"][0]["how"] == "calls" and res["dependents"][0]["evidence"] == "services/orders.py:5"
    assert [t["name"] for t in res["tests"]] == ["test_orders.test_list"] and res["test_files"] == ["tests/test_orders.py"]
    [entry] = res["entry_points"]
    assert entry["path"] == "main.py" and entry["chain"][-1] == idx.resolve("db.models.query").id
    assert res["totals"]["components"] >= 2 and res["summary"].startswith("Changing db.models.query can affect")
    # the same seed, changed: affected_flow reaches the same entry points and tests
    repo.write({"db/models.py": "def query():\n    return [1]\n"})
    _comp, diff = Repository(repo.path).compare(mode="all")
    flow = affected_flow(diff)
    assert {e["id"] for e in flow.entry_points} == {e["id"] for e in res["entry_points"]}
    assert {t["id"] for t in flow.tests} == {t["id"] for t in res["tests"]}
    # a module (or directory) is also followed through importers; depth bounds the walk
    mod = blast_radius(idx, idx.resolve("db/models.py"))
    assert {"services.orders", "services.orders.list_orders"} <= {d["name"] for d in mod["dependents"]}
    near = blast_radius(idx, idx.resolve("db.models.query"), depth=1)
    assert [d["name"] for d in near["dependents"]] == ["services.orders.list_orders"] and near["entry_points"] == []
    assert main(["impact", "-C", repo.path, "db.models.query"]) == 0
    text = capsys.readouterr().out
    assert "Changing db.models.query can affect" in text and " 1  services.orders.list_orders calls it  (services/orders.py:5)" in text
    assert "Tests reached (1):" in text and "Entry points reached (1):" in text
    assert main(["impact", "-C", repo.path, "db.models.query", "--json", "--depth", "2"]) == 0
    assert {d["distance"] for d in json.loads(capsys.readouterr().out)["dependents"]} == {1, 2}


def test_path_and_impact_over_the_api(make_repo) -> None:
    import threading

    from test_outputs import request

    from repoviz.server import create_server

    repo = make_repo(CHAIN)
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, _, body = request(srv, "GET", "/api/path?from=ui&to=db")
        data = json.loads(body)
        assert status == 200 and names(data["paths"][0]) == ["ui.forms", "services.orders", "db.models"]
        status, _, body = request(srv, "GET", "/api/impact?node=db.models.query&depth=2")
        data = json.loads(body)
        assert status == 200 and {d["distance"] for d in data["dependents"]} == {1, 2}
        status, _, body = request(srv, "GET", "/api/impact?node=nosuch")
        assert status == 400 and "nothing named" in json.loads(body)["error"]
        status, _, _ = request(srv, "GET", "/api/path?from=ui&to=db", headers={})  # no X-Repoviz header
        assert status == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_nothing_uses_it(make_repo) -> None:
    idx = index(make_repo(CHAIN))
    res = blast_radius(idx, idx.resolve("test_orders.test_list"))
    assert res["dependents"] == [] and "Nothing in the analyzed code uses it" in res["summary"]
