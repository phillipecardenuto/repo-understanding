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
    assert kinds["forbidden-dependency"][0]["detail"].startswith("util must stay generic")
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
