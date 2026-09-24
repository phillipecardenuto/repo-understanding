"""Reviewing AI-agent work: scope, waves, findings, key changes and feedback."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

from repoviz.cli import main
from repoviz.render.html import build_bundle, render_static_html
from repoviz.repo import Repository
from repoviz.review import ScopePolicy, build_review, feedback_markdown, resolve_target, review_targets
from repoviz.server import create_server

SECRET = "sk-live-abcdefghijklmnopqrstuvwxyz123456"

APP = {
    "pyproject.toml": '[project]\nname = "app"\ndependencies = []\n[tool.setuptools.packages.find]\nwhere = ["src"]\n',
    "src/app/__init__.py": "",
    "src/app/billing/__init__.py": "",
    "src/app/billing/invoice.py": """
        from app.util.money import round_money


        def total(items):
            return round_money(sum(items))


        def tax(amount, rate):
            return amount * rate
    """,
    "src/app/util/__init__.py": "",
    "src/app/util/money.py": "def round_money(x):\n    return round(x, 2)\n\n\ndef legacy(x):\n    return x\n",
    "src/app/reports/__init__.py": "",
    "src/app/reports/summary.py": """
        from app.billing.invoice import tax
        from app.util.money import legacy


        def summary(amount):
            return legacy(tax(amount, 0.2))
    """,
    "src/app/auth/__init__.py": "def check(token):\n    return token == 'ok'\n",
    "tests/test_invoice.py": """
        from app.billing.invoice import total


        def test_total():
            assert total([1, 2]) == 3
            assert total([]) == 0
    """,
}


def agent_wave(repo) -> None:
    """What a (sloppy) agent does during the wave."""
    inv = Path(repo.path, "src/app/billing/invoice.py")
    inv.write_text(inv.read_text()
                   .replace("def tax(amount, rate):", "def tax(amount, rate, region):")
                   .replace("    return round_money(sum(items))",
                            "    breakpoint()\n    # TODO: discounts\n    return round_money(sum(items))")
                   + "\n\ndef refund(x):\n    try:\n        return -x\n    except Exception:\n        pass\n")
    money = Path(repo.path, "src/app/util/money.py")
    money.write_text(money.read_text().replace("\n\ndef legacy(x):\n    return x\n", "\n"))
    Path(repo.path, "src/app/auth/__init__.py").write_text(f'API_KEY = "{SECRET}"\n\ndef check(token):\n    return True\n')
    Path(repo.path, "tests/test_invoice.py").write_text(
        "import pytest\nfrom app.billing.invoice import total\n\n\n@pytest.mark.skip\ndef test_total():\n"
        "    assert total([1, 2]) == 3\n")


def by_kind(report):
    out: dict[str, list] = {}
    for f in report["findings"]:
        out.setdefault(f["kind"], []).append(f)
    return out


def test_scope_policy() -> None:
    policy = ScopePolicy(allowed=["src/app/billing/**", "tests/**"], protected=["src/app/auth/**"])
    assert policy.classify("src/app/billing/invoice.py") == "allowed"
    assert policy.classify("tests/test_x.py") == "allowed"
    assert policy.classify("src/app/util/money.py") == "out-of-scope"
    assert policy.classify("src/app/auth/__init__.py") == "protected"
    assert ScopePolicy().classify("anything.py") == "unscoped"


def test_session_review_end_to_end(make_repo) -> None:
    repo = make_repo(APP)
    r = Repository(repo.path)
    r.state.start_session(r.git, r.root, "wave 1", allowed=["src/app/billing/**", "tests/**"],
                          protected=["src/app/auth/**"])
    agent_wave(repo)
    targets = review_targets(r)
    assert targets[0].id == "session" and targets[0].kind == "session"
    target = resolve_target(r)
    report = build_review(r, target)
    kinds = by_kind(report)
    files = {f["path"]: f for f in report["files"]}
    # Where did the agent go?
    assert files["src/app/auth/__init__.py"]["scope"] == "protected"
    assert files["src/app/util/money.py"]["scope"] == "out-of-scope"
    assert files["src/app/billing/invoice.py"]["scope"] == "allowed"
    assert report["summary"]["protected"] == 1 and report["summary"]["out_of_scope"] == 1
    assert {"protected-touched", "out-of-scope"} <= set(kinds)
    # Key changes: symbols with before/after signatures and line counts.
    inv = {s["name"]: s for s in files["src/app/billing/invoice.py"]["symbols"]}
    assert inv["tax"]["signature_before"] == "(amount, rate)" and inv["tax"]["signature"] == "(amount, rate, region)"
    assert inv["refund"]["status"] == "added" and inv["total"]["lines_added"] == 2
    assert files["src/app/billing/invoice.py"]["hunks"]
    # Correctness signals.
    assert kinds["dangling-call"][0]["path"] == "src/app/reports/summary.py"  # legacy() was removed but is still called
    assert "legacy" in kinds["dangling-call"][0]["detail"]
    assert "app.reports.summary.summary" in kinds["stale-callers"][0]["detail"]  # tax() callers not updated
    assert kinds["debugger"][0]["line"] == 5
    assert kinds["swallowed-exception"] and kinds["todo"]
    # Test weakening.
    assert kinds["test-disabled"][0]["path"] == "tests/test_invoice.py"
    assert kinds["assertions-removed"]
    # Secrets are flagged but never copied into the report.
    assert kinds["secret"][0]["severity"] == "high"
    assert SECRET not in json.dumps(report)
    # Components summary.
    comps = {c["name"]: c for c in report["components"]}
    assert comps["app"]["scope"].get("protected") == 1


def test_past_wave_is_frozen_at_session_end(make_repo) -> None:
    repo = make_repo(APP)
    r = Repository(repo.path)
    r.state.start_session(r.git, r.root, "wave 1", protected=["src/app/auth/**"])
    agent_wave(repo)
    session = r.state.end_session(r.git, r.root)
    # Work continues after the wave: it must not leak into the wave's review.
    repo.write({"src/app/later.py": "x = 1\n"})
    repo.commit("later")
    target = resolve_target(Repository(repo.path), f"session:{session.id}")
    assert target.kind == "past-session" and target.base == f"SESSION@{session.id}"
    report = build_review(Repository(repo.path), target)
    paths = {f["path"] for f in report["files"]}
    assert "src/app/auth/__init__.py" in paths and "src/app/later.py" not in paths
    assert report["summary"]["protected"] == 1  # the session's scope is remembered


def test_architecture_signals_and_rules(make_repo) -> None:
    repo = make_repo(APP)
    Path(repo.path, ".repoviz.toml").write_text(
        '[review]\nprotected = ["src/app/auth/**"]\n'
        '[[review.rules]]\nfrom = ["src/app/util/**"]\nto = ["src/app/billing/**"]\nmessage = "util must stay generic"\n')
    repo.commit("config")
    money = Path(repo.path, "src/app/util/money.py")
    money.write_text("from app.billing.invoice import total\nimport app.missing_module\n\n" + money.read_text())
    report = build_review(Repository(repo.path), resolve_target(Repository(repo.path), "all"))
    kinds = by_kind(report)
    assert kinds["contract-broken"][0]["detail"].startswith("util must stay generic")  # [[review.rules]] unchanged
    assert kinds["new-cycle"]  # billing -> util -> billing
    assert kinds["unresolved-internal-import"][0]["title"] == "Broken import"
    assert report["scope"]["protected"] == ["src/app/auth/**"]


def test_untested_change_signal(make_repo) -> None:
    repo = make_repo(APP)
    Path(repo.path, "src/app/reports/summary.py").write_text(
        Path(repo.path, "src/app/reports/summary.py").read_text() + "\n\ndef extra():\n    return 1\n")
    kinds = by_kind(build_review(Repository(repo.path), resolve_target(Repository(repo.path), "all")))
    assert kinds["untested-change"][0]["path"] == "src/app/reports/summary.py"


def test_feedback_prompt() -> None:
    report = {
        "target": {"label": "wave 2"}, "base": {"label": "SESSION"}, "head": {"label": "WORKTREE"},
        "scope": {"allowed": ["src/a/**"], "protected": ["src/auth/**"]},
        "findings": [
            {"id": "f1", "kind": "protected-touched", "category": "scope", "severity": "high", "title": "Protected area modified",
             "detail": "x", "path": "src/auth/x.py"},
            {"id": "f2", "kind": "debugger", "category": "hygiene", "severity": "medium", "title": "Debugger", "detail": "bp",
             "path": "src/a/y.py", "line": 3, "excerpt": "breakpoint()"},
            {"id": "f3", "kind": "todo", "category": "hygiene", "severity": "low", "title": "TODO", "detail": "t", "path": "src/a/y.py"},
            {"id": "f4", "kind": "stub", "category": "correctness", "severity": "medium", "title": "Stub", "detail": "s", "path": "src/a/z.py"},
        ],
    }
    notes = [
        {"verdict": "should-not-touch", "path": "src/auth/x.py", "comment": "Auth is out of scope; revert."},
        {"verdict": "logic-error", "path": "src/a/y.py", "line": 12, "symbol": "a.y.f", "comment": "Rounding happens twice.",
         "excerpt": "return round(round(x))"},
        {"verdict": "ok", "finding_id": "f4"},
    ]
    text = feedback_markdown(report, notes)
    assert "Do not modify: `src/auth/**`" in text
    assert "## Revert: changes that should not have been made" in text and "Auth is out of scope; revert." in text
    assert "`src/a/y.py:12` (`a.y.f`) — Rounding happens twice." in text
    assert "Debugger" in text  # untriaged medium signal included
    assert "Protected area modified" not in text  # covered by the revert note
    assert "Stub" not in text  # dismissed
    assert "TODO" not in text  # below the default minimum severity
    assert "TODO" in feedback_markdown(report, notes, min_severity="low")


def test_review_cli(make_repo, capsys) -> None:
    repo = make_repo(APP)
    assert main(["session", "-C", repo.path, "start", "--label", "w", "--allow", "src/app/billing/**",
                 "--protect", "src/app/auth/**"]) == 0
    agent_wave(repo)
    capsys.readouterr()
    assert main(["review", "-C", repo.path, "--list"]) == 0
    assert capsys.readouterr().out.startswith("session")
    assert main(["review", "-C", repo.path]) == 0
    out = capsys.readouterr().out
    assert "Touched components:" in out and "Protected area modified" in out
    assert main(["review", "-C", repo.path, "--format", "prompt"]) == 0
    assert "## Automated review signals" in capsys.readouterr().out
    assert main(["review", "-C", repo.path, "--fail-on", "protected"]) == 3
    assert main(["review", "-C", repo.path, "--fail-on", "dangling-call"]) == 3
    assert main(["review", "-C", repo.path, "--format", "markdown"]) == 0
    assert "| Component |" in capsys.readouterr().out
    assert main(["session", "-C", repo.path, "end"]) == 0
    assert "repoviz review session:" in capsys.readouterr().out


def test_review_server_endpoints(make_repo) -> None:
    repo = make_repo(APP)
    r = Repository(repo.path)
    session = r.state.start_session(r.git, r.root, "w")
    agent_wave(repo)
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=60)
        headers = {"X-Repoviz": "1", "Content-Type": "application/json"} if method == "POST" else {"X-Repoviz": "1"}
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())

    try:
        status, targets = call("GET", "/api/review/targets")
        assert status == 200 and targets[0]["id"] == "session"
        status, report = call("GET", "/api/review?id=session")
        assert status == 200 and report["summary"]["files"] == 4 and report["notes"] == []
        status, _ = call("POST", "/api/session/scope", {"session_id": session.id, "protected": ["src/app/auth/**"]})
        assert status == 200
        status, report = call("GET", "/api/review?id=session")
        assert report["summary"]["protected"] == 1
        key = report["target"]["key"]
        note = {"id": "n1", "verdict": "logic-error", "path": "src/app/billing/invoice.py", "line": 5, "comment": "why?"}
        assert call("POST", "/api/review/notes", {"key": key, "notes": [note]})[0] == 200
        status, saved = call("GET", "/api/review/notes?key=" + key)
        assert saved["notes"][0]["comment"] == "why?"
        assert call("GET", "/api/review?base=HEAD&target=WORKTREE")[0] == 200
        assert call("GET", "/api/review?base=--bad")[0] == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_static_report_embeds_reviews_without_secrets(make_repo) -> None:
    repo = make_repo(APP)
    r = Repository(repo.path)
    r.state.start_session(r.git, r.root, "w", protected=["src/app/auth/**"])
    agent_wave(repo)
    bundle = build_bundle(Repository(repo.path))
    assert bundle["review_targets"][0]["id"] == "session"
    assert bundle["reviews"][0]["summary"]["protected"] == 1
    assert SECRET not in render_static_html(bundle, compress=False)


def test_new_package_dependency_signal(make_repo) -> None:
    repo = make_repo(APP)
    auth = Path(repo.path, "src/app/auth/__init__.py")
    auth.write_text("from app.util.money import round_money\n\n" + auth.read_text())
    reports = Path(repo.path, "src/app/reports/summary.py")
    reports.write_text(reports.read_text().replace("from app.util.money import legacy",
                                                   "from app.util.money import legacy, round_money"))
    kinds = by_kind(build_review(Repository(repo.path), resolve_target(Repository(repo.path), "all")))
    details = [f["detail"] for f in kinds["new-package-dependency"]]
    assert details == ["src/app/auth now depends on src/app/util (app.auth → app.util.money)"]  # reports→util existed


# --------------------------------------------------------------------------- new code that is not wired in

FASTAPI_APP = {
    "requirements.txt": "fastapi\n",
    "Dockerfile": "FROM python:3.12\nCMD uvicorn app.main:app --host 0.0.0.0\n",
    "app/__init__.py": "",
    "app/main.py": """
        from fastapi import FastAPI
        from app.routes import images

        app = FastAPI()
        app.include_router(images.router)
    """,
    "app/routes/__init__.py": "",
    "app/routes/images.py": """
        from fastapi import APIRouter
        from app.services.images import list_images

        router = APIRouter(prefix="/images")


        @router.get("/")
        def index():
            return list_images()
    """,
    "app/services/__init__.py": "",
    "app/services/images.py": "def list_images():\n    return []\n",
    "tests/test_images.py": "from app.services.images import list_images\n\n\ndef test_list():\n"
                            "    assert list_images() == []\n",
}

REPORTS_ROUTE = """
    from fastapi import APIRouter
    from app.services.reports import build_report

    router = APIRouter(prefix="/reports")


    @router.get("/")
    def report():
        return build_report()
"""


def _review_all(repo) -> dict:
    return build_review(Repository(repo.path), resolve_target(Repository(repo.path), "all"))


def test_new_router_that_is_never_registered(make_repo) -> None:
    repo = make_repo(FASTAPI_APP)
    repo.write({"app/routes/reports.py": REPORTS_ROUTE,
                "app/services/reports.py": "def build_report():\n    return {}\n"})
    kinds = by_kind(_review_all(repo))
    [route] = kinds["unwired-module"]
    assert route["path"] == "app/routes/reports.py" and route["title"] == "New router is never registered"
    assert "`app.include_router(reports.router)` in `app/main.py`" in route["suggestion"]
    # The new service is used, but only by the unregistered route.
    [service] = kinds["unreachable-from-entry"]
    assert service["path"] == "app/services/reports.py" and "app/routes/reports.py" in service["detail"]
    # Wiring the route in clears every signal.
    main = Path(repo.path, "app/main.py")
    main.write_text(main.read_text().replace("import images", "import images, reports")
                    + "app.include_router(reports.router)\n")
    kinds = by_kind(_review_all(repo))
    assert not {"unwired-module", "unreachable-from-entry", "unwired-symbol"} & set(kinds)


def test_router_imported_but_not_registered(make_repo) -> None:
    repo = make_repo(FASTAPI_APP)
    repo.write({"app/routes/reports.py": REPORTS_ROUTE,
                "app/services/reports.py": "def build_report():\n    return {}\n"})
    main = Path(repo.path, "app/main.py")
    main.write_text(main.read_text().replace("import images", "import images, reports"))
    [route] = by_kind(_review_all(repo))["unwired-module"]
    assert "app/main.py imports it but never registers it" in route["detail"]


def test_unwired_modules_and_symbols(make_repo) -> None:
    repo = make_repo(FASTAPI_APP)
    services = Path(repo.path, "app/services/images.py")
    services.write_text(services.read_text()
                        + "\n\ndef export_csv(rows):\n    return rows\n"          # never used
                        + "\n\ndef get_db():\n    return None\n"                  # used as a value, not called
                        + "\n\nDEPENDENCIES = [get_db]\n")
    repo.write({"app/utils/formatting.py": "def pretty(x):\n    return str(x)\n",  # nothing imports it
                "app/reports/__init__.py": "from .service import build\n",       # a new package nothing uses
                "app/reports/service.py": "def build():\n    return 1\n"})
    kinds = by_kind(_review_all(repo))
    assert sorted(f["path"] for f in kinds["unwired-module"]) == ["app/reports/service.py", "app/utils/formatting.py"]
    assert all(f["severity"] == "medium" and f["title"] == "New module is not wired in" for f in kinds["unwired-module"])
    [symbol] = kinds["unwired-symbol"]
    assert symbol["symbol"] == "app.services.images.export_csv" and symbol["severity"] == "low"
    assert symbol["line"] == 5


def test_code_wired_by_convention_or_reference_is_not_flagged(make_repo) -> None:
    repo = make_repo({**FASTAPI_APP,
                      "pyproject.toml": '[project]\nname = "app"\n[project.scripts]\napp-admin = "app.cli:main"\n'})
    repo.write({
        "tests/test_reports.py": "def test_nothing():\n    assert 1 + 1 == 2\n",
        "tests/conftest.py": "import pytest\n",
        "app/migrations/0002_add_reports.py": "def upgrade():\n    pass\n",
        "app/cli.py": "def main():\n    print('admin')\n",                         # declared console script
        "app/worker.py": 'from celery import Celery\n\ncelery = Celery(include=["app.jobs.cleanup"])\n',
        "app/jobs/__init__.py": "",
        "app/jobs/cleanup.py": "def run():\n    return 0\n",                      # referenced by a string
        "scripts/seed.py": "print('seeding')\n",
        "app/extras.py": "X = 1\n",
    })
    Path(repo.path, ".repoviz.toml").write_text('[review]\nwiring_ignore = ["app/extras.py", "app/worker.py"]\n')
    report = _review_all(repo)
    flagged = {f["path"] for f in report["findings"]
               if f["kind"] in ("unwired-module", "unwired-symbol", "unreachable-from-entry")}
    assert flagged == set(), flagged


def test_unwired_signals_can_be_disabled(make_repo) -> None:
    repo = make_repo({**FASTAPI_APP, ".repoviz.toml": '[review]\ndisabled_checks = ["unwired-module"]\n'})
    repo.write({"app/utils/formatting.py": "def pretty(x):\n    return str(x)\n"})
    assert "unwired-module" not in by_kind(_review_all(repo))


def test_new_express_router_that_is_never_mounted(make_repo) -> None:
    repo = make_repo({
        "package.json": '{"name": "api", "main": "src/app.js", "dependencies": {"express": "^4"}}',
        "src/app.js": "const express = require('express');\nconst users = require('./routes/users');\n\n"
                      "const app = express();\napp.use('/users', users);\nmodule.exports = app;\n",
        "src/routes/users.js": "const express = require('express');\nconst router = express.Router();\n\n"
                               "router.get('/', (req, res) => res.json([]));\nmodule.exports = router;\n",
    })
    repo.write({"src/routes/orders.js": "const express = require('express');\nconst router = express.Router();\n\n"
                                        "router.get('/', (req, res) => res.json([]));\nmodule.exports = router;\n"})
    [route] = by_kind(_review_all(repo))["unwired-module"]
    assert route["path"] == "src/routes/orders.js" and "`app.use('/path', orders)` in `src/app.js`" in \
        route["suggestion"]


# --------------------------------------------------------------------------- change coupling (missed companion)


def test_missed_companion_signal(make_repo) -> None:
    repo = make_repo({"app.py": "x = 0\n", "schema.sql": "-- 0\n", "other.py": "y = 0\n",
                      ".repoviz.toml": "[history]\nmin_commits = 5\n"})
    for i in range(1, 9):
        repo.write({"app.py": f"x = {i}\n", "schema.sql": f"-- {i}\n"}).commit(f"feature {i}")
    for i in range(1, 4):
        repo.write({"other.py": f"y = {i}\n"}).commit(f"other {i}")
    Path(repo.path, "app.py").write_text("x = 'agent'\n")
    report = _review_all(repo)
    [finding] = by_kind(report)["missed-companion"]
    assert finding["path"] == "app.py" and finding["severity"] == "medium"  # 9 of 9 commits
    assert "schema.sql in 9 of its last 9 commits" in finding["detail"]
    entry = next(f for f in report["files"] if f["path"] == "app.py")
    assert entry["usually_changes_with"] == [{"path": "schema.sql", "shared": 9, "revs": 9, "degree": 1.0,
                                              "changed": False}]
    assert report["history"]["usable"] and report["history"]["commits"] == 12
    # Changing the companion too clears the signal.
    Path(repo.path, "schema.sql").write_text("-- agent\n")
    report = _review_all(repo)
    assert "missed-companion" not in by_kind(report)
    assert next(f for f in report["files"] if f["path"] == "app.py")["usually_changes_with"][0]["changed"] is True


def test_missed_companion_ignores_lock_files_and_short_history(make_repo) -> None:
    repo = make_repo({"app.py": "x = 0\n", "uv.lock": "0\n", ".repoviz.toml": "[history]\nmin_commits = 3\n"})
    for i in range(1, 7):
        repo.write({"app.py": f"x = {i}\n", "uv.lock": f"{i}\n"}).commit(f"c{i}")
    Path(repo.path, "app.py").write_text("x = 'agent'\n")
    assert "missed-companion" not in by_kind(_review_all(repo))  # a lock file follows its manifest, not code
    short = make_repo({"a.py": "0\n", "b.py": "0\n"})
    for i in range(1, 6):
        short.write({"a.py": f"{i}\n", "b.py": f"{i}\n"}).commit(f"c{i}")
    Path(short.path, "a.py").write_text("'agent'\n")
    report = _review_all(short)
    assert "missed-companion" not in by_kind(report) and not report["history"]["usable"]


# --------------------------------------------------------------------------- commit by commit

import pytest  # noqa: E402

from repoviz import review as review_mod  # noqa: E402


def _commits_repo(make_repo):
    repo = make_repo({"a.py": "A = 0\n", "b.py": "B = 0\n", "c.py": "C = 0\n"})
    shas = [repo.write({"a.py": "A = 1\n"}).commit("change a"),
            repo.write({"b.py": "B = 1\n", "c.py": "C = 1\nC2 = 2\n"}).commit("change b and c"),
            repo.write({"d.py": "D = 1\n"}).commit("add d")]
    return repo, shas


def test_review_lists_the_commits_of_a_range(make_repo) -> None:
    repo, shas = _commits_repo(make_repo)
    r = Repository(repo.path)
    report = build_review(r, resolve_target(r, base="HEAD~3", target="HEAD"))
    items = report["commits"]["items"]
    assert [c["sha"] for c in items] == shas and [c["subject"] for c in items][0] == "change a"
    assert report["commits"]["total"] == 3 and report["commits"]["shown"] == 3
    second = {f["path"]: f for f in items[1]["files"]}
    assert second["c.py"] == {"path": "c.py", "status": "M", "added": 2, "removed": 1, "in_review": True}
    assert items[2]["files"][0]["status"] == "A"
    files = {f["path"]: f for f in report["files"]}
    assert files["b.py"]["commits"] == [shas[1]] and files["d.py"]["commits"] == [shas[2]]


def test_review_of_one_commit_keeps_the_wave(make_repo) -> None:
    repo, shas = _commits_repo(make_repo)
    r = Repository(repo.path)
    target = resolve_target(r, base="HEAD~3", target="HEAD")
    one = build_review(r, target, commit=shas[1][:10])
    assert sorted(f["path"] for f in one["files"]) == ["b.py", "c.py"]
    assert one["commit"]["sha"] == shas[1] and one["target"]["key"] == target.key
    assert len(one["commits"]["items"]) == 3  # the wave's list stays available
    with pytest.raises(ValueError):
        build_review(r, target, commit="not-a-sha")
    with pytest.raises(ValueError):  # a real commit, but outside the range
        build_review(r, resolve_target(r, base="HEAD~1", target="HEAD"), commit=shas[0])


def test_session_commits_end_with_uncommitted_work(make_repo) -> None:
    repo = make_repo({"a.py": "A = 0\n", "b.py": "B = 0\n", "notes.txt": "draft\n"})
    Path(repo.path, "notes.txt").write_text("already being edited\n")  # dirty before the agent starts
    r = Repository(repo.path)
    r.state.start_session(r.git, r.root, "wave")
    repo.write({"a.py": "A = 1\n"}).stage("a.py").git("commit", "-qm", "agent: a")
    repo.write({"b.py": "B = 1\n"}).stage("b.py").git("commit", "-qm", "agent: b")
    Path(repo.path, "a.py").write_text("A = 2\n")  # uncommitted
    r = Repository(repo.path)
    target = resolve_target(r, "session")
    report = build_review(r, target)
    items = report["commits"]["items"]
    assert [c["subject"] for c in items] == ["agent: a", "agent: b", "Uncommitted changes"]
    assert items[-1]["sha"] == "WORKTREE" and [f["path"] for f in items[-1]["files"]] == ["a.py"]
    files = {f["path"]: f for f in report["files"]}
    assert files["a.py"]["commits"] == [items[0]["sha"], "WORKTREE"] and "notes.txt" not in files
    # The uncommitted step on its own: the file edited before the session started is not blamed on it.
    step = build_review(r, target, commit="WORKTREE")
    assert [f["path"] for f in step["files"]] == ["a.py"]


def test_last_commit_of_a_merge_lists_the_merged_commits(make_repo) -> None:
    repo = make_repo({"a.py": "A = 0\n", "b.py": "B = 0\n"})
    repo.git("checkout", "-q", "-b", "feature")
    one = repo.write({"a.py": "A = 1\n"}).commit("feature 1")
    two = repo.write({"b.py": "B = 1\n"}).commit("feature 2")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    report = build_review(Repository(repo.path), resolve_target(Repository(repo.path), "last-commit"))
    assert [c["sha"] for c in report["commits"]["items"]] == [one, two]
    assert report["commits"]["merges"] == 1


def test_changed_then_changed_back_and_the_commit_cap(make_repo, monkeypatch) -> None:
    repo = make_repo({"a.py": "A = 0\n", "b.py": "B = 0\n"})
    repo.write({"a.py": "A = 'experiment'\n"}).commit("try something")
    repo.write({"a.py": "A = 0\n", "b.py": "B = 1\n"}).commit("undo it, change b")
    repo.write({"b.py": "B = 2\n"}).commit("b again")
    r = Repository(repo.path)
    kinds = by_kind(build_review(r, resolve_target(r, base="HEAD~3", target="HEAD")))
    [back] = kinds["reverted-within-wave"]
    assert back["path"] == "a.py" and back["severity"] == "info" and "try something" in back["detail"]
    monkeypatch.setattr(review_mod, "MAX_COMMITS", 2)
    commits = build_review(r, resolve_target(r, base="HEAD~3", target="HEAD"))["commits"]
    assert commits["total"] == 3 and commits["shown"] == 2 and commits["items"][0]["subject"] == "undo it, change b"


def test_commit_review_api_and_cli(make_repo, capsys) -> None:
    repo, shas = _commits_repo(make_repo)
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
        for commit, status in ((shas[1], 200), ("zzz", 400)):
            conn.request("GET", f"/api/review?base=HEAD~3&target=HEAD&commit={commit}", headers={"X-Repoviz": "1"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            assert resp.status == status
        conn.request("GET", f"/api/review?base=HEAD~3&target=HEAD&commit={shas[1]}", headers={"X-Repoviz": "1"})
        one = json.loads(conn.getresponse().read())
        assert one["commit"]["subject"] == "change b and c" and "notes" in one and body.get("error")
    finally:
        srv.shutdown()
    assert main(["review", "-C", repo.path, "--base", "HEAD~3", "--head", "HEAD", "--by-commit"]) == 0
    out = capsys.readouterr().out
    assert "Commits (3, oldest first):" in out and "M c.py" in out and out.index("change a") < out.index("add d")
    assert main(["review", "-C", repo.path, "--base", "HEAD~3", "--head", "HEAD", "--commit", shas[2][:8]]) == 0
    assert "commit " + shas[2][:8] + " add d" in capsys.readouterr().out
    assert main(["review", "-C", repo.path, "--base", "HEAD~1", "--head", "HEAD", "--commit", shas[0][:8]]) == 1


# --------------------------------------------------------------------------- regressions from the code review


def test_live_review_sees_new_commits_without_file_changes(make_repo) -> None:
    repo = make_repo({"a.py": "A = 0\n"})
    r = Repository(repo.path)
    r.state.start_session(r.git, r.root, "wave")
    Path(repo.path, "a.py").write_text("A = 1\n")
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def subjects():
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
            conn.request("GET", "/api/review?id=session", headers={"X-Repoviz": "1"})
            return [c["subject"] for c in json.loads(conn.getresponse().read())["commits"]["items"]]
        assert subjects() == ["Uncommitted changes"]
        repo.stage("a.py").git("commit", "-qm", "agent: a")  # same content, now committed
        assert subjects() == ["agent: a"]
    finally:
        srv.shutdown()


def test_reverted_within_wave_ignores_submodule_pointers_and_symlinks(make_repo) -> None:
    from test_large_repo_fixes import _git

    lib = make_repo({"x.py": "X = 1\n"})
    main = make_repo({"a.py": "A = 0\n"})
    _git(main.path, "submodule", "add", "-q", lib.path, "sub")
    _git(main.path, "commit", "-qm", "add sub")
    Path(lib.path, "x.py").write_text("X = 2\n")
    lib.commit("lib 2")
    _git(str(Path(main.path, "sub")), "pull", "-q", "origin", "main")
    _git(main.path, "commit", "-qam", "bump sub")
    Path(main.path, "link.py").symlink_to("a.py")
    main.commit("add a link")
    r = Repository(main.path)
    kinds = by_kind(build_review(r, resolve_target(r, base="HEAD~2", target="HEAD")))
    assert "reverted-within-wave" not in kinds


def test_commit_review_is_limited_to_the_range(make_repo, monkeypatch) -> None:
    repo, shas = _commits_repo(make_repo)
    r = Repository(repo.path)
    with pytest.raises(ValueError):  # the uncommitted review has no commits: an old commit is refused
        build_review(r, resolve_target(r, "all"), commit=shas[0])
    monkeypatch.setattr(review_mod, "MAX_COMMITS", 1)
    one = build_review(r, resolve_target(r, base="HEAD~3", target="HEAD"), commit=shas[0])  # beyond the cap
    assert one["commit"]["subject"] == "change a" and [f["path"] for f in one["files"]] == ["a.py"]


def test_merge_base_range_uses_its_second_revision(make_repo) -> None:
    repo = make_repo({"a.py": "A = 0\n", "b.py": "B = 0\n"})
    repo.git("checkout", "-q", "-b", "feature")
    repo.write({"a.py": "A = 1\n"}).commit("feature work")
    repo.git("checkout", "-q", "main")
    repo.write({"b.py": "B = 1\n"}).commit("main work")
    repo.git("checkout", "-q", "-b", "other")
    r = Repository(repo.path)
    report = build_review(r, resolve_target(r, "main...feature"))
    assert [c["subject"] for c in report["commits"]["items"]] == ["feature work"]
    assert "reverted-within-wave" not in by_kind(report)


def test_commit_of_a_session_does_not_blame_pre_session_edits(make_repo) -> None:
    repo = make_repo({"a.py": "A = 0\n", "notes.txt": "draft\n"})
    Path(repo.path, "notes.txt").write_text("the human's edit\n")
    r = Repository(repo.path)
    r.state.start_session(r.git, r.root, "wave")
    Path(repo.path, "a.py").write_text("A = 1\n")
    repo.git("commit", "-qam", "agent: a (commit -a also records notes.txt)")
    r = Repository(repo.path)
    target = resolve_target(r, "session")
    sha = build_review(r, target)["commits"]["items"][0]["sha"]
    assert [f["path"] for f in build_review(r, target, commit=sha)["files"]] == ["a.py"]


def test_commit_review_has_no_missed_companion_already_in_the_wave(make_repo) -> None:
    repo = make_repo({"app.py": "x = 0\n", "schema.sql": "-- 0\n", ".repoviz.toml": "[history]\nmin_commits = 3\n"})
    for i in range(1, 7):
        repo.write({"app.py": f"x = {i}\n", "schema.sql": f"-- {i}\n"}).commit(f"feature {i}")
    first = repo.write({"app.py": "x = 'agent'\n"}).commit("agent: app")
    repo.write({"schema.sql": "-- agent\n"}).commit("agent: schema")
    r = Repository(repo.path)
    target = resolve_target(r, base="HEAD~2", target="HEAD")
    assert "missed-companion" not in by_kind(build_review(r, target))
    one = build_review(r, target, commit=first)
    assert "missed-companion" not in by_kind(one)
    assert one["files"][0]["usually_changes_with"][0]["changed"] is True


def test_commit_subjects_are_redacted(make_repo) -> None:
    repo = make_repo({"a.py": "A = 0\n"})
    repo.write({"a.py": "A = 1\n"}).commit(f"use token {SECRET} for CI")
    r = Repository(repo.path)
    report = build_review(r, resolve_target(r, "last-commit"))
    assert SECRET not in json.dumps(report) and "for CI" in report["commits"]["items"][0]["subject"]


def test_commit_review_in_a_sha256_repository(make_repo, tmp_path: Path) -> None:
    root = tmp_path / "sha256"
    root.mkdir()
    import subprocess

    if subprocess.run(["git", "init", "-q", "--object-format=sha256", "-b", "main"], cwd=root).returncode:
        pytest.skip("this Git has no SHA-256 support")
    run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True, text=True).stdout  # noqa: E731
    (root / "a.py").write_text("A = 0\n")
    run("add", "-A"), run("commit", "-qm", "init")
    (root / "a.py").write_text("A = 1\n")
    run("commit", "-qam", "change a")
    r = Repository(root)
    target = resolve_target(r, "last-commit")
    sha = build_review(r, target)["commits"]["items"][0]["sha"]
    assert len(sha) == 64 and build_review(r, target, commit=sha)["commit"]["subject"] == "change a"


def test_router_registered_through_a_package_reexport(make_repo) -> None:
    repo = make_repo(FASTAPI_APP)
    repo.write({"app/routes/reports.py": REPORTS_ROUTE, "app/services/reports.py": "def build_report():\n    return {}\n",
                "app/routes/__init__.py": "from .reports import router as reports_router\n"})
    main = Path(repo.path, "app/main.py")
    main.write_text(main.read_text().replace("from app.routes import images",
                                             "from app.routes import images, reports_router")
                    + "app.include_router(reports_router)\n")
    assert "unwired-module" not in by_kind(_review_all(repo))


def test_express_router_mounted_with_an_inline_require(make_repo) -> None:
    repo = make_repo({
        "package.json": '{"name": "api", "dependencies": {"express": "^4"}}',
        "src/app.js": "const express = require('express');\nconst app = express();\n"
                      "app.use('/users', require('./routes/users'));\nmodule.exports = app;\n",
        "src/routes/users.js": "const express = require('express');\nconst router = express.Router();\n"
                               "module.exports = router;\n",
    })
    repo.write({"src/routes/orders.js": "const express = require('express');\nconst router = express.Router();\n"
                                        "module.exports = router;\n"})
    Path(repo.path, "src/app.js").write_text(Path(repo.path, "src/app.js").read_text().replace(
        "module.exports", "app.use('/orders', require('./routes/orders'));\nmodule.exports"))
    assert "unwired-module" not in by_kind(_review_all(repo))


def test_route_handlers_make_an_application(make_repo) -> None:
    app = {k: v for k, v in FASTAPI_APP.items() if k != "Dockerfile"}  # no container, compose file or Procfile
    repo = make_repo(app)
    repo.write({"app/utils/formatting.py": "def pretty(x):\n    return str(x)\n",
                "app/services/stats.py": "def count():\n    return 0\n",
                "tests/test_stats.py": "from app.services.stats import count\n\n\ndef test_count():\n"
                                       "    assert count() == 0\n"})
    kinds = by_kind(_review_all(repo))
    assert kinds["unwired-module"][0]["severity"] == "medium"
    assert [f["path"] for f in kinds["unreachable-from-entry"]] == ["app/services/stats.py"]


def test_default_export_imported_under_another_name(make_repo) -> None:
    repo = make_repo({"package.json": '{"name": "api"}',
                      "src/app.js": "const express = require('express');\nconst app = express();\n"})
    repo.write({"src/handlers.js": "export default function listOrders(req, res) {\n  res.json([]);\n}\n"})
    Path(repo.path, "src/app.js").write_text("import ordersHandler from './handlers.js';\n"
                                             + Path(repo.path, "src/app.js").read_text()
                                             + "app.get('/orders', ordersHandler);\n")
    assert "unwired-symbol" not in by_kind(_review_all(repo))


def test_common_file_names_are_not_matched_from_anywhere(make_repo) -> None:
    repo = make_repo({"package.json": '{"name": "api", "main": "index.js"}',
                      "index.js": "module.exports = {};\n"})
    repo.write({"src/feature/index.js": "export function helper() { return 1; }\n"})
    assert [f["path"] for f in by_kind(_review_all(repo))["unwired-module"]] == ["src/feature/index.js"]


# --------------------------------------------------------------------------- renames (#3)


def test_renamed_symbols_with_every_reference_updated(make_repo) -> None:
    repo = make_repo({
        "errs.py": "class ELISException(Exception):\n    pass\n\n\ndef elis_exception_handler(e):\n    return str(e)\n",
        "app.py": "from errs import ELISException, elis_exception_handler\n\n\ndef run():\n    try:\n        pass\n"
                  "    except ELISException as e:\n        return elis_exception_handler(e)\n",
    })
    for name in ("errs.py", "app.py"):
        p = Path(repo.path, name)
        p.write_text(p.read_text().replace("ELIS", "ELIES").replace("elis_", "elies_"))
    kinds = by_kind(_review_all(repo))
    assert sorted(f["detail"].split(" →")[0] for f in kinds["renamed-symbol"]) == [
        "errs.ELISException", "errs.elis_exception_handler"]
    assert not {"public-api-removed", "dangling-call", "renamed-symbol-stale-references"} & set(kinds)


def test_renamed_symbol_still_used_by_an_untouched_module(make_repo) -> None:
    repo = make_repo({
        "lib.py": "def compute(x):\n    return x * 2\n",
        "a.py": "from lib import compute\n\n\ndef first():\n    return compute(1)\n",
        "b.py": "import lib\n\n\ndef second():\n    return lib.compute(2)\n",
    })
    Path(repo.path, "lib.py").write_text("def calculate(x):\n    return x * 2\n")
    Path(repo.path, "a.py").write_text("from lib import calculate\n\n\ndef first():\n    return calculate(1)\n")
    report = _review_all(repo)
    [stale] = by_kind(report)["renamed-symbol-stale-references"]
    assert stale["severity"] == "high" and "`compute` is still called at b.py:5" in stale["detail"]
    assert stale["path"] == "b.py" and stale["line"] == 5
    key = next(f for f in report["files"] if f["path"] == "lib.py")["symbols"][0]
    assert key["name"] == "calculate" and key["renamed_from"] == "compute" and key["status"] == "modified"


def test_moved_file_is_one_entry_with_its_small_edit(make_repo) -> None:
    repo = make_repo({"pkg/__init__.py": "", "pkg/util.py": "def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\n"
                      "def c():\n    return 3\n", "main.py": "from pkg.util import a\n\nprint(a())\n"})
    Path(repo.path, "lib").mkdir()
    Path(repo.path, "lib/__init__.py").write_text("")
    repo.git("mv", "pkg/util.py", "lib/helpers.py")
    moved = Path(repo.path, "lib/helpers.py")
    moved.write_text(moved.read_text().replace("return 3", "return 33"))
    Path(repo.path, "main.py").write_text("from lib.helpers import a\n\nprint(a())\n")
    report = _review_all(repo)
    files = {f["path"]: f for f in report["files"]}
    entry = files["lib/helpers.py"]
    assert "pkg/util.py" not in files and entry["status"] == "renamed" and entry["previous_path"] == "pkg/util.py"
    assert (entry["lines_added"], entry["lines_removed"]) == (1, 1)
    assert [k["name"] for k in entry["symbols"]] == ["c"]  # a and b only moved with the file
    assert "public-api-removed" not in by_kind(report)


def test_submodule_moved_to_another_path(make_repo) -> None:
    from test_large_repo_fixes import _git

    lib = make_repo({"src/engine.py": "def run():\n    return 1\n"})
    main = make_repo({"app/__init__.py": "", "app/main.py": "print('hi')\n"})
    _git(main.path, "submodule", "add", "-q", lib.path, "modules/engine")
    _git(main.path, "commit", "-qm", "add engine")
    Path(main.path, "vendor").mkdir()
    _git(main.path, "mv", "modules/engine", "vendor/engine")
    kinds = by_kind(_review_all(main))
    assert [f["detail"] for f in kinds["submodule-moved"]] and "modules/engine" in kinds["submodule-moved"][0]["detail"]
    assert not {"submodule-added", "submodule-removed"} & set(kinds)


def test_submodule_renamed_upstream_pairs_by_name(make_repo) -> None:
    from test_large_repo_fixes import _git

    old = make_repo({"src/engine.py": "def run():\n    return 1\n"})
    new = make_repo({"src/engine.py": "def run():\n    return 2\n"})
    other = make_repo({"src/search.py": "def find():\n    return 3\n"})
    main = make_repo({"app/__init__.py": "", "app/main.py": "print('hi')\n"})
    _git(main.path, "submodule", "add", "-q", old.path, "modules/elis-engine")
    _git(main.path, "commit", "-qm", "add engine")
    _git(main.path, "rm", "-q", "modules/elis-engine")
    _git(main.path, "submodule", "add", "-q", new.path, "modules/elies-engine")  # other URL, other commit
    _git(main.path, "submodule", "add", "-q", other.path, "modules/search")
    kinds = by_kind(_review_all(main))
    [moved] = kinds["submodule-moved"]
    assert moved["path"] == "modules/elies-engine" and "modules/elis-engine" in moved["detail"]
    assert [f["path"] for f in kinds["submodule-added"]] == ["modules/search"]
    assert "submodule-removed" not in kinds


def test_renamed_and_edited_function(make_repo) -> None:
    repo = make_repo({"lib.py": "def compute(x):\n    y = x * 2\n    return y\n", "a.py": "from lib import compute\n"})
    Path(repo.path, "lib.py").write_text("def calculate(x):\n    y = x * 3\n    return y\n")
    Path(repo.path, "a.py").write_text("from lib import calculate\n")
    report = _review_all(repo)
    [key] = next(f for f in report["files"] if f["path"] == "lib.py")["symbols"]
    assert key["renamed_from"] == "compute" and key["status"] == "modified"
    assert "content changed" in key["reasons"] and any(r.startswith("renamed from") for r in key["reasons"])
    assert "public-api-removed" not in by_kind(report)


# --------------------------------------------------------------------------- risk (#5)

RISK_APP = {
    ".repoviz.toml": '[review]\nprotected = ["app/auth/**"]\n',
    "pyproject.toml": '[project]\nname = "app"\nversion = "1"\n\n[project.scripts]\napp = "app.cli:main"\n',
    "app/__init__.py": "",
    "app/core.py": "def price(x):\n    return x * 2\n",
    "app/cli.py": "from app.core import price\n\n\ndef main():\n    return price(1)\n",
    "app/api.py": "from app.core import price\n\n\ndef quote():\n    return price(2)\n\n\ndef invoice():\n    return price(3)\n",
    "app/report.py": "from app import core\n\n\ndef summary():\n    return core.price(4)\n",
    "app/auth/__init__.py": "",
    "app/auth/tokens.py": "def check(token):\n    return token == 'ok'\n",
    "tests/test_auth.py": "from app.auth.tokens import check\n\n\ndef test_check():\n    assert check('ok')\n",
    "README.md": "# app\n",
}


def risk_wave(repo) -> None:
    """A widely used function changes signature, a protected file is edited, a test and the docs grow."""
    Path(repo.path, "app/core.py").write_text("def price(x, rate):\n    return x * rate\n")
    Path(repo.path, "app/auth/tokens.py").write_text("def check(token):\n    return token in ('ok', 'yes')\n")
    Path(repo.path, "tests/test_auth.py").write_text(RISK_APP["tests/test_auth.py"]
                                                     + "\n\ndef test_yes():\n    assert check('yes')\n")
    Path(repo.path, "README.md").write_text("# app\n\n" + "".join(f"Line {i}.\n" for i in range(30)))


def test_risk_orders_the_wave_and_explains_each_factor(make_repo) -> None:
    repo = make_repo(RISK_APP)
    risk_wave(repo)
    report = _review_all(repo)
    risk = {f["path"]: f["risk"] for f in report["files"]}
    assert {p: (r["score"], r["level"]) for p, r in risk.items()} == {
        "app/auth/tokens.py": (42, "high"), "app/core.py": (42, "high"),
        "README.md": (6, "low"), "tests/test_auth.py": (3, "low")}
    assert [(x["factor"], x["points"], x["text"]) for x in risk["app/core.py"]["factors"]] == [
        ("signals", 15, "medium signal: Signature changed; callers not updated and 1 more signal"),
        ("tests", 10, "no test imports or calls this code"),
        ("fan_in", 9, "called from 4 places"),
        ("entry_points", 5, "reached from 1 entry point (app [console-script])"),
        ("size", 3, "4 lines changed")]
    assert [x["text"] for x in risk["app/auth/tokens.py"]["factors"]] == [
        "high signal: Protected area modified", "protected area", "2 lines changed"]
    assert [x["factor"] for x in risk["README.md"]["factors"]] == ["size"]
    wave = report["risk"]
    assert (wave["score"], wave["level"], wave["path"]) == (42, "high", "app/auth/tokens.py")
    assert wave["summary"] == ("high risk, because of app/auth/tokens.py "
                               "(high signal: Protected area modified; protected area)")
    assert [t["path"] for t in wave["top"]] == ["app/auth/tokens.py", "app/core.py", "README.md"]
    assert wave["counts"] == {"high": 2, "medium": 0, "low": 2}
    assert _review_all(repo)["risk"] == wave  # deterministic


def test_risk_in_prompt_text_and_gate(make_repo, capsys) -> None:
    repo = make_repo(RISK_APP)
    risk_wave(repo)
    prompt = feedback_markdown(_review_all(repo), [])
    riskiest = prompt.split("## Riskiest files (double-check them)")[1]
    assert "- `app/auth/tokens.py`: high risk (42/100): high signal: Protected area modified" in riskiest
    assert "- `app/core.py`: high risk (42/100)" in riskiest and "README.md" not in riskiest  # low risk: not listed
    assert "Riskiest" not in feedback_markdown(_review_all(repo), [], include_findings=False)
    capsys.readouterr()
    assert main(["review", "-C", repo.path]) == 0
    out = capsys.readouterr().out
    assert "risk: high (42/100), because of app/auth/tokens.py" in out
    assert "Review first (riskiest files):" in out and "+9   called from 4 places" in out
    assert main(["review", "-C", repo.path, "--fail-on", "risk:high"]) == 3
    assert "wave risk is high (42/100, app/auth/tokens.py)" in capsys.readouterr().err
    assert main(["review", "-C", repo.path, "--fail-on", "risk:bogus"]) == 1
    Path(repo.path, "app/auth/tokens.py").write_text(RISK_APP["app/auth/tokens.py"])
    Path(repo.path, "app/core.py").write_text(RISK_APP["app/core.py"])
    assert main(["review", "-C", repo.path, "--fail-on", "risk:high"]) == 0  # docs and tests only: low
    capsys.readouterr()
    assert main(["review", "-C", repo.path, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["risk"]["level"] == "low"


def test_risk_weights_are_configurable_and_validated(make_repo) -> None:
    import pytest

    from repoviz.config import ConfigError, load_config

    repo = make_repo(dict(RISK_APP, **{".repoviz.toml": RISK_APP[".repoviz.toml"]
                                       + "\n[review.risk]\nsignals = 0\nsensitive = 0\nhigh = 30\nbogus = 1\n"}))
    risk_wave(repo)
    cfg = Repository(repo.path).config
    assert cfg.review_risk_weights == {"signals": 0.0, "sensitive": 0.0} and cfg.review_risk_thresholds == {"high": 30.0}
    assert any("review.risk.bogus" in s for s in cfg.sources)  # unknown key: a configuration diagnostic
    risk = {f["path"]: f["risk"] for f in _review_all(repo)["files"]}
    # Signals and sensitive paths no longer count; the rest is rescaled so that the maximum is still 100.
    assert (risk["app/core.py"]["score"], risk["app/core.py"]["level"]) == (44, "high")
    assert [x["factor"] for x in risk["app/auth/tokens.py"]["factors"]] == ["size"]
    for bad in ("signals = -1", "size = 'big'", "high = 120", "medium = 50\nhigh = 40",
                "signals = 0\nfan_in = 0\nentry_points = 0\ntests = 0\nsensitive = 0\nchurn = 0\nsize = 0"):
        Path(repo.path, ".repoviz.toml").write_text(f"[review.risk]\n{bad}\n")
        with pytest.raises(ConfigError):
            load_config(Path(repo.path))


def test_risk_hotspot_security_path_and_stale_tests(make_repo) -> None:
    files = {f"app/m{i}.py": f"def f{i}():\n    return {i}\n" for i in range(10)}
    repo = make_repo(dict(files, **{
        "app/__init__.py": "", "app/hot.py": "def rate():\n    return 1\n",
        "app/auth/__init__.py": "", "app/auth/login.py": "def login(user):\n    return bool(user)\n",
        "tests/test_login.py": "from app.auth.login import login\n\n\ndef test_login():\n    assert login('a')\n"}))
    for i in range(4):  # a hotspot: changed far more often than the other files
        repo.write({"app/hot.py": f"def rate():\n    return {i + 2}\n"})
        repo.commit(f"tune rate {i}")
    repo.write({"app/hot.py": "def rate():\n    return 9\n",
                "app/auth/login.py": "def login(user):\n    return bool(user) and user != 'root'\n"})
    risk = {f["path"]: f["risk"] for f in _review_all(repo)["files"]}
    texts = {p: [x["text"] for x in r["factors"]] for p, r in risk.items()}
    assert ("churn hotspot: changed in 5 of the last 300 commits (hotspots: 2 or more, the busiest 20% of modules)"
            in texts["app/hot.py"])
    from repoviz.filechanges import hotspots
    assert hotspots(Repository(repo.path).snapshot("HEAD")) == ["app/hot.py"]  # one definition: Structure agrees
    assert "security-related path" in texts["app/auth/login.py"]
    assert "1 test file covers it; none was updated" in texts["app/auth/login.py"]
    assert not any(t.startswith("hotspot") for t in texts["app/auth/login.py"])


# --------------------------------------------------------------------------- any branch against any branch (#35)


def diverged_repo(make_repo):
    """``main`` moved on (``g`` in a.py, a new c.py) after ``feature`` (adds b.py) and ``other`` (adds o.py) left
    it; ``merged`` has nothing of its own."""
    repo = make_repo({"app/__init__.py": "", "app/a.py": "def f():\n    return 1\n"})
    repo.git("checkout", "-q", "-b", "feature")
    repo.write({"app/b.py": "from app.a import f\n\n\ndef b():\n    return f()\n"}).commit("feature: add b")
    repo.git("checkout", "-q", "-b", "other", "main")
    repo.write({"app/o.py": "def o():\n    return 3\n"}).commit("other: add o")
    repo.git("branch", "merged", "main")
    repo.git("checkout", "-q", "main")
    repo.write({"app/a.py": "def f():\n    return 1\n\n\ndef g():\n    return 2\n",
                "app/c.py": "def c():\n    return 3\n"}).commit("main: add g and c")
    return repo


def test_branch_since_it_diverged_shows_only_its_own_work(make_repo) -> None:
    repo = diverged_repo(make_repo)
    r = Repository(repo.path)
    target = resolve_target(r, base="main", target="feature", mode="merge-base")
    assert target.key == "range:main...feature" and target.label.startswith("feature since it left main (merge base ")
    report = build_review(r, target)
    assert [f["path"] for f in report["files"]] == ["app/b.py"]  # main's newer g() and c.py are not "undone"
    assert "public-api-removed" not in by_kind(report)
    assert [c["subject"] for c in report["commits"]["items"]] == ["feature: add b"]
    # The exact difference compares the trees: main's newer work shows up as removed by the branch.
    exact = resolve_target(r, base="main", target="feature", mode="exact")
    assert exact.key == "range:main..feature" and exact.label == "main → feature (exact difference)"
    report = build_review(r, exact)
    assert {f["path"]: f["status"] for f in report["files"]} == {
        "app/a.py": "modified", "app/b.py": "added", "app/c.py": "removed"}
    assert "public-api-removed" in by_kind(report)
    # The CLI spec and the listed target share the key, so notes are shared.
    for spec in ("main...feature", "range:main...feature"):
        assert resolve_target(r, spec).key == "range:main...feature"
    assert resolve_target(r, "main..feature").key == "range:main..feature"


def test_any_two_branches_remote_ones_and_bad_input(make_repo) -> None:
    import pytest

    from repoviz.gitutil import GitError

    repo = diverged_repo(make_repo)
    repo.git("update-ref", "refs/remotes/origin/other", "other")  # a branch only known from the remote
    r = Repository(repo.path)
    for base, target in (("feature", "other"), ("origin/other", "feature")):
        report = build_review(r, resolve_target(r, base=base, target=target, mode="merge-base"))
        assert [f["path"] for f in report["files"]] == ["app/o.py" if target == "other" else "app/b.py"]
    with pytest.raises(GitError, match="unknown revision"):
        resolve_target(r, base="main", target="nope", mode="merge-base")
    with pytest.raises(ValueError, match="needs a branch, tag or commit"):
        resolve_target(r, base="WORKTREE", target="feature", mode="merge-base")
    with pytest.raises(ValueError, match="unknown comparison mode"):
        resolve_target(r, base="main", target="feature", mode="sideways")
    with pytest.raises(GitError):
        resolve_target(r, base="--output=/tmp/x", target="feature", mode="merge-base")  # never an option


def test_recent_unmerged_branches_are_review_targets(make_repo) -> None:
    repo = diverged_repo(make_repo)
    repo.git("update-ref", "refs/remotes/origin/feature", "feature")  # the local branch stands for it
    repo.git("update-ref", "refs/remotes/origin/agent-x", "other")  # only known from the remote (a fresh clone)
    repo.git("update-ref", "refs/remotes/origin/HEAD", "main")
    targets = {t.id: t for t in review_targets(Repository(repo.path))}
    branches = {i: t for i, t in targets.items() if t.kind == "branch"}
    # Not main, not the merged branch, not a remote copy of a local branch.
    assert set(branches) == {"range:main...feature", "range:main...other", "range:main...origin/agent-x"}
    assert branches["range:main...feature"].label == "Branch feature vs main (since merge base)"
    assert branches["range:main...feature"].description == "1 commit(s) not in main"
    report = build_review(Repository(repo.path), resolve_target(Repository(repo.path), "range:main...other"))
    assert [f["path"] for f in report["files"]] == ["app/o.py"]
    repo.git("checkout", "-q", "feature")  # the current branch is already offered as "Branch feature vs main"
    ids = [t.id for t in review_targets(Repository(repo.path))]
    assert "branch" in ids and "range:main...feature" not in ids and "range:main...other" in ids


# --------------------------------------------------------------------------- deep review of #3 / #5 (regressions)


def test_class_rename_keeps_its_unchanged_methods_out_of_key_changes(make_repo) -> None:
    repo = make_repo({"app/__init__.py": "", "app/m.py": "class Foo:\n    def a(self):\n        return 1\n\n"
                      "    def b(self):\n        return 2\n", "app/use.py": "from app.m import Foo\n"})
    Path(repo.path, "app/m.py").write_text(Path(repo.path, "app/m.py").read_text().replace("Foo", "Bar"))
    Path(repo.path, "app/use.py").write_text("from app.m import Bar\n")
    report = _review_all(repo)
    [key] = next(f for f in report["files"] if f["path"] == "app/m.py")["symbols"]
    assert (key["name"], key["renamed_from"]) == ("Bar", "Foo")  # the rename, not "a renamed from a"


def test_moved_file_keeps_its_deleted_symbols_and_their_signals(make_repo) -> None:
    repo = make_repo({"app/__init__.py": "", "app/m.py": "def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\n"
                      "def c():\n    return 3\n", "app/use.py": "from app.m import a\n\nprint(a())\n"})
    repo.git("mv", "app/m.py", "app/n.py")
    Path(repo.path, "app/n.py").write_text("def a():\n    return 1\n\n\ndef c():\n    return 3\n")
    Path(repo.path, "app/use.py").write_text("from app.n import a\n\nprint(a())\n")
    report = _review_all(repo)
    entry = next(f for f in report["files"] if f["path"] == "app/n.py")
    assert entry["status"] == "renamed" and [(k["name"], k["status"]) for k in entry["symbols"]] == [("b", "removed")]
    removed = by_kind(report).get("public-api-removed", [])
    assert removed and all(f["path"] == "app/n.py" and "before the move: app/m.py" in f["detail"] for f in removed)
    assert set(entry["findings"]) >= {f["id"] for f in removed}  # on the file's card


def test_one_shared_generic_name_is_not_a_moved_file(make_repo) -> None:
    repo = make_repo({"app/mig/__init__.py": "", "app/mig/0002_b.py": "class Migration:\n    ops = ['b']\n"})
    repo.delete("app/mig/0002_b.py")
    repo.write({"app/mig/0003_c.py": "class Migration:\n    ops = ['c', 'd']\n"})
    files = {f["path"]: f["status"] for f in _review_all(repo)["files"]}
    assert files == {"app/mig/0002_b.py": "removed", "app/mig/0003_c.py": "added"}


def test_common_signature_alone_does_not_pair_methods(make_repo) -> None:
    repo = make_repo({"app/__init__.py": "", "app/s.py": "class S:\n    def close(self):\n        self.x = 0\n"
                      "        self.y = 0\n        return None\n"})
    Path(repo.path, "app/s.py").write_text("class S:\n    def reset(self):\n        self.a = []\n        self.b = {}\n"
                                           "        self.c = 1\n        return self\n")
    report = _review_all(repo)
    assert "renamed-symbol" not in by_kind(report) and "renamed-symbol-stale-references" not in by_kind(report)
    assert sorted((k["name"], k["status"]) for k in report["files"][0]["symbols"]) == [("close", "removed"), ("reset", "added")]


def test_old_name_in_strings_docstrings_and_attributes_is_not_stale(make_repo) -> None:
    repo = make_repo({"app/__init__.py": "", "app/p.py": "def parse(text):\n    return text.split()\n",
                      "app/cli.py": 'import logging\nfrom app.p import parse\n\n\ndef run(parser, args):\n'
                                    '    """Calls parse on the input."""\n    logging.info("parse failed")\n'
                                    '    parser.parse(args)  # parse again\n    return parse(args)\n'})
    Path(repo.path, "app/p.py").write_text("def parse_text(text):\n    return text.split()\n")
    Path(repo.path, "app/cli.py").write_text(Path(repo.path, "app/cli.py").read_text()
                                             .replace("from app.p import parse", "from app.p import parse_text")
                                             .replace("return parse(args)", "return parse_text(args)"))
    kinds = by_kind(_review_all(repo))
    assert "renamed-symbol-stale-references" not in kinds and len(kinds["renamed-symbol"]) == 1


def test_stale_name_without_a_resolved_call_is_medium(make_repo) -> None:
    repo = make_repo({"app/__init__.py": "", "app/p.py": "def parse(text):\n    return text\n",
                      "app/reg.py": "from app import p\n\nHANDLERS = [parse]\n"})
    Path(repo.path, "app/p.py").write_text("def parse_text(text):\n    return text\n")
    [stale] = by_kind(_review_all(repo))["renamed-symbol-stale-references"]
    assert stale["severity"] == "medium" and "may still be used" in stale["title"] and "app/reg.py:3" in stale["detail"]


def test_rename_reference_check_is_capped(make_repo, monkeypatch) -> None:
    import repoviz.review as review_mod

    monkeypatch.setattr(review_mod, "MAX_RENAME_CHECKS", 1)
    repo = make_repo({"app/__init__.py": "", "app/p.py": "def alpha_one(x):\n    return x\n\n\n"
                      "def beta_two(y):\n    return y * 2\n"})
    Path(repo.path, "app/p.py").write_text("def alpha_uno(x):\n    return x\n\n\ndef beta_dos(y):\n    return y * 2\n")
    details = sorted(f["detail"] for f in by_kind(_review_all(repo))["renamed-symbol"])
    assert len(details) == 2 and sum("were not checked (more than 1 renames" in d for d in details) == 1


def test_renamed_class_counts_the_callers_of_its_constructor(make_repo) -> None:
    repo = make_repo({"app/__init__.py": "", "app/errs.py": "class AppError(Exception):\n    def __init__(self, msg):\n"
                      "        super().__init__(msg)\n",
                      "app/a.py": "from app.errs import AppError\n\n\ndef f():\n    raise AppError('a')\n",
                      "app/b.py": "from app.errs import AppError\n\n\ndef g():\n    raise AppError('b')\n"})
    Path(repo.path, "app/errs.py").write_text(Path(repo.path, "app/errs.py").read_text().replace("AppError", "ServiceError"))
    for name in ("app/a.py", "app/b.py"):
        p = Path(repo.path, name)
        p.write_text(p.read_text().replace("AppError", "ServiceError"))
    risk = next(f for f in _review_all(repo)["files"] if f["path"] == "app/errs.py")["risk"]
    assert "called from 2 places" in [x["text"] for x in risk["factors"]]  # calls to the class hit its __init__


# --------------------------------------------------------------------------- architecture contracts (#16)

LAYERED = {
    "app/__init__.py": "", "app/routes/__init__.py": "", "app/services/__init__.py": "", "app/models/__init__.py": "",
    "app/util/__init__.py": "", "app/storage/__init__.py": "",
    "app/routes/api.py": "from app.services import billing\n",
    "app/services/billing.py": "from app.models import user\n",
    "app/models/user.py": "X = 1\n",
    "app/util/helpers.py": "Y = 2\n",
    "app/storage/api.py": "def api():\n    return 1\n",
    "app/storage/_internal.py": "def _raw():\n    return 2\n",
    "tests/test_x.py": "from app.storage import _internal\nfrom app.routes import api\n",
}
LAYERS_TOML = ('[[contracts]]\nname = "Layered backend"\ntype = "layers"\n'
               'layers = ["app.routes", "app.services", "app.models"]\n')


def _contracts(repo, toml: str):
    from repoviz.contracts import check, contracts_of

    Path(repo.path, ".repoviz.toml").write_text(toml)
    r = Repository(repo.path)
    return {res.contract.name: res for res in check(r.snapshot("WORKTREE"), contracts_of(r.config))}


def test_layers_contract_new_upward_import_breaks_it(make_repo) -> None:
    repo = make_repo(dict(LAYERED, **{".repoviz.toml": LAYERS_TOML}))
    repo.write({"app/models/user.py": "from app.routes import api\n\nX = 1\n"})
    report = _review_all(repo)
    [broken] = by_kind(report)["contract-broken"]
    assert broken["severity"] == "high" and broken["title"] == "Contract broken: Layered backend"
    assert (broken["path"], broken["line"]) == ("app/models/user.py", 1)
    assert "app.models.user imports app.routes.api" in broken["detail"] and "'app.models' is below 'app.routes'" in broken["detail"]
    assert "forbidden-dependency" not in by_kind(report)  # the old name is gone


def test_indirect_violation_is_reported_with_its_chain(make_repo) -> None:
    toml = LAYERS_TOML + "allow_indirect = false\n"
    repo = make_repo(dict(LAYERED, **{"app/models/user.py": "from app.util import helpers\n",
                                      "app/util/helpers.py": "from app.routes import api\n"}))
    direct = _contracts(repo, LAYERS_TOML)["Layered backend"]
    assert direct.violations == []  # models → util → routes is indirect
    [v] = _contracts(repo, toml)["Layered backend"].violations
    assert v.chain == ["app.models.user", "app.util.helpers", "app.routes.api"] and v.indirect
    assert "(through app.util.helpers)" in v.detail and (v.path, v.line) == ("app/models/user.py", 1)


def test_independence_and_public_interface_in_python(make_repo) -> None:
    repo = make_repo({"app/__init__.py": "", "app/features/__init__.py": "", "app/features/a/__init__.py": "",
                      "app/features/b/__init__.py": "", "app/features/a/views.py": "from app.features.b import models\n",
                      "app/features/b/models.py": "M = 1\n", "app/storage/__init__.py": "",
                      "app/storage/api.py": "A = 1\n", "app/storage/_internal.py": "I = 1\n",
                      "app/features/b/repo.py": "from app.storage import _internal\nfrom app.storage import api\n"})
    toml = ('[[contracts]]\nname = "Features"\ntype = "independence"\nmodules = ["app.features.*"]\n'
            '[[contracts]]\nname = "Storage API"\ntype = "public-interface"\nmodule = "app.storage"\n'
            'public = ["app.storage.api"]\n')
    res = _contracts(repo, toml)
    [ind] = res["Features"].violations
    assert (ind.source, ind.target) == ("app.features.a.views", "app.features.b.models")
    assert "app.features.a and app.features.b must stay independent" in ind.detail
    [pub] = res["Storage API"].violations
    assert (pub.source, pub.target) == ("app.features.b.repo", "app.storage._internal")


def test_independence_and_public_interface_in_javascript(make_repo) -> None:
    repo = make_repo({"package.json": '{"name": "web"}',
                      "src/features/a/index.js": "import { b } from '../b/index.js';\nexport const a = b;\n",
                      "src/features/b/index.js": "import { raw } from '../../storage/internal.js';\nexport const b = raw;\n",
                      "src/storage/api.js": "export const api = 1;\n", "src/storage/internal.js": "export const raw = 2;\n"})
    toml = ('[[contracts]]\nname = "Features"\ntype = "independence"\nmodules = ["src/features/*"]\n'
            '[[contracts]]\nname = "Storage API"\ntype = "public-interface"\nmodule = "src/storage"\n'
            'public = ["src/storage/api.js"]\n')
    res = _contracts(repo, toml)
    assert [(v.source, v.target) for v in res["Features"].violations] == [("src/features/a/index", "src/features/b/index")]
    assert [(v.source, v.target) for v in res["Storage API"].violations] == [("src/features/b/index", "src/storage/internal")]


def test_forbidden_acyclic_required_and_containers(make_repo) -> None:
    repo = make_repo({"app/__init__.py": "", "app/a/__init__.py": "", "app/b/__init__.py": "",
                      "app/a/x.py": "from app.b import y\n", "app/b/y.py": "from app.a import z\n", "app/a/z.py": "Z = 1\n",
                      "app/handlers/__init__.py": "", "app/handlers/h1.py": "from app.a import z\n",
                      "app/handlers/h2.py": "H = 2\n",
                      "svc/__init__.py": "", "svc/one/__init__.py": "", "svc/one/web.py": "W = 1\n",
                      "svc/one/db.py": "from svc.one import web\n"})
    toml = ('[[contracts]]\nname = "No b from a"\ntype = "forbidden"\nfrom = ["app.a"]\nto = ["app.b"]\n'
            '[[contracts]]\nname = "No cycles"\ntype = "acyclic"\nmodules = ["app.*"]\n'
            '[[contracts]]\nname = "Handlers use a"\ntype = "required"\nfrom = ["app.handlers.h*"]\nto = ["app.a"]\n'
            '[[contracts]]\nname = "Per service"\ntype = "layers"\ncontainers = ["svc.one"]\nlayers = ["web", "db"]\n')
    res = _contracts(repo, toml)
    assert [(v.source, v.target) for v in res["No b from a"].violations] == [("app.a.x", "app.b.y")]
    [cycle] = res["No cycles"].violations
    assert cycle.source == "app.a ⇄ app.b" and "import cycle between app.a → app.b → app.a" in cycle.detail
    assert [v.source for v in res["Handlers use a"].violations] == ["app.handlers.h2"]
    assert [(v.source, v.target) for v in res["Per service"].violations] == [("svc.one.db", "svc.one.web")]


def test_contract_baseline_known_new_fixed_and_changed(make_repo) -> None:
    from repoviz.cli import main as cli

    repo = make_repo(dict(LAYERED, **{".repoviz.toml": LAYERS_TOML,
                                      "app/models/legacy.py": "from app.routes import api\n",
                                      "app/models/old.py": "from app.routes import api\n"}))
    out = Path(repo.path, "..", "baseline.json")
    assert cli(["contracts", "-C", repo.path, "--baseline", "-o", str(out)]) == 0
    repo.write({".repoviz-known-violations.json": out.read_text()}).commit("baseline")
    repo.write({"app/models/new.py": "from app.routes import api\n"})  # a new violation
    repo.delete("app/models/old.py")  # a known one goes away
    kinds = by_kind(_review_all(repo))
    assert [f["detail"].split(":")[0] for f in kinds["contract-broken"]] == ["app.models.new imports app.routes.api"]
    assert [f["severity"] for f in kinds["contract-fixed"]] == ["info"]
    assert "contract-baseline-changed" not in kinds
    # Accepting a violation by editing the baseline is itself a signal.
    data = json.loads(out.read_text())
    data["violations"].append({"key": "Layered backend::app.models.new::app.routes.api"})
    repo.write({".repoviz-known-violations.json": json.dumps(data)})
    kinds = by_kind(_review_all(repo))
    assert "contract-broken" not in kinds and kinds["contract-baseline-changed"][0]["severity"] == "medium"
    repo.write({".repoviz-known-violations.json": "{not json"})
    assert by_kind(_review_all(repo))["contract-baseline-invalid"][0]["severity"] == "low"


def test_contract_ignores_and_configuration_checks(make_repo) -> None:
    import pytest

    from repoviz.config import ConfigError, load_config

    repo = make_repo(dict(LAYERED, **{"app/models/legacy.py": "from app.routes import api\n"}))
    res = _contracts(repo, LAYERS_TOML + 'ignore = ["app.models.legacy -> app.routes", "app.gone -> app.routes"]\n')
    assert res["Layered backend"].violations == [] and res["Layered backend"].stale_ignores == ["app.gone -> app.routes"]
    Path(repo.path, ".repoviz.toml").write_text(LAYERS_TOML + "colour = 'red'\n")
    assert any("contracts.Layered backend.colour" in s for s in load_config(Path(repo.path)).sources)
    for bad in ('[[contracts]]\nname = "x"\ntype = "onion"\n', '[[contracts]]\ntype = "layers"\nlayers = ["a"]\n',
                LAYERS_TOML + LAYERS_TOML, '[[contracts]]\ntype = "forbidden"\nfrom = ["a"]\nto = ["b"]\nignore = ["a b"]\n'):
        Path(repo.path, ".repoviz.toml").write_text(bad)
        with pytest.raises(ConfigError):
            load_config(Path(repo.path))


def test_disabled_forbidden_dependency_disables_contract_signals(make_repo) -> None:
    repo = make_repo(dict(LAYERED, **{".repoviz.toml": LAYERS_TOML + '[review]\ndisabled_checks = ["forbidden-dependency"]\n'}))
    repo.write({"app/models/user.py": "from app.routes import api\n"})
    assert "contract-broken" not in by_kind(_review_all(repo))
