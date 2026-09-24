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
