"""Mermaid rendering, the HTML report, the live server, the CLI and the read-only guarantee."""

from __future__ import annotations

import base64
import gzip
import hashlib
import http.client
import json
import os
import re
import threading
from pathlib import Path

import pytest

from repoviz.cli import main
from repoviz.render import mermaid, views
from repoviz.render.html import build_bundle, render_live_html, render_static_html
from repoviz.repo import Repository
from repoviz.server import create_server


def cycle_change(repo) -> None:
    models = Path(repo.path, "src/shop/core/models.py")
    models.write_text(models.read_text().replace("if TYPE_CHECKING:\n    from shop.api.views import View",
                                                 "from shop.api.views import View"))
    repo.write({"src/shop/api/extra.py": "import json\n"})


# --------------------------------------------------------------------------- Mermaid


def test_escape_neutralises_markup_and_syntax() -> None:
    evil = 'x"]:::bad\n<script>alert(1)</script> #1 `code` | {a} [b]'
    out = mermaid.escape(evil)
    for ch in '"<>`|{}[]\n':
        assert ch not in out
    assert "#quot;" in out and "#lt;script#gt;" in out


def test_changes_diagram_conventions(shop_repo) -> None:
    cycle_change(shop_repo)
    shop_repo.delete("tests/test_models.py")
    _comp, diff = Repository(shop_repo.path).compare(mode="all")
    view = views.changes_view(diff, level="module", scope="all", include_external=True)
    text = mermaid.to_mermaid(view)
    assert text.startswith("flowchart LR") and "accTitle:" in text and "accDescr:" in text
    # Every node has a status class; removed nodes use the dashed class.
    assert re.search(r'✚ .*shop\.api\.extra.*:::st_added', text)
    assert re.search(r'✖ .*test_models.*:::st_removed', text)
    assert "classDef st_removed" in text and "stroke-dasharray:6 4" in text.split("classDef st_removed")[1].split("\n")[0]
    assert re.search(r'✎ .*shop\.core\.models.*:::st_modified', text)
    # Not colour alone: status words in labels, markers on edges, dashed purple cycle edges.
    assert "<small>added" in text and "<small>removed" in text
    assert '"+ new"' in text and '− removed' in text and "⟲ new cycle" in text
    edge_lines = [l for l in text.splitlines() if re.match(r"\s+\w+ (-->|==>|-\.->)", l)]
    link_styles = [l for l in text.splitlines() if l.strip().startswith("linkStyle")]
    assert len(edge_lines) == len(link_styles) and edge_lines
    cycle_styles = [l for l in link_styles if "#7c3aed" in l]
    assert cycle_styles and all("stroke-dasharray" in l for l in cycle_styles)


def test_other_views_render(shop_repo) -> None:
    r = Repository(shop_repo.path)
    snap = r.snapshot()
    deps = mermaid.to_mermaid(views.dependency_view(snap, level="module"))
    assert "kind_module" in deps and "-->" in deps
    struct = mermaid.to_mermaid(views.structure_view(snap, depth=2))
    assert " --- " in struct and "🏠" in struct
    focus = snap.find(qualified_name="shop.cli")
    focused = views.dependency_view(snap, level="module", focus=focus.id, depth=1)
    assert {n.label for n in focused.nodes} == {"shop.cli", "shop.api.views"}


def test_neighbour_cap_summarises_hubs(make_repo) -> None:
    files = {"hub/__init__.py": "", "hub/core.py": "X = 1\n"}
    for i in range(40):
        files[f"user{i}/__init__.py"] = ""
        files[f"user{i}/m.py"] = "import hub.core\n"
    repo = make_repo(files)
    repo.append("hub/core.py", "Y = 2\n")
    _comp, diff = Repository(repo.path).compare(mode="all")
    view = views.changes_view(diff, level="component", scope="neighbors", neighbor_limit=10)
    assert len(view.nodes) == 1 + 10 + 1
    assert view.nodes[-1].id == "rv_more_neighbors" and "+30 more" in view.nodes[-1].label


# --------------------------------------------------------------------------- HTML


def test_static_report_is_self_contained(shop_repo) -> None:
    cycle_change(shop_repo)
    shop_repo.write({"src/shop/weird.py": "NAME = '</script><script>alert(1)</script><!--'\n"})
    bundle = build_bundle(Repository(shop_repo.path))
    html = render_static_html(bundle, compress=False)
    assert '<meta charset="utf-8">' in html
    # No external resources: everything is inline.
    assert not re.search(r'<(script|link|img)[^>]+(src|href)="(https?:)?//', html)
    assert "mermaid" in html and "repoviz-data" in html
    # The embedded payload cannot terminate its <script> element early.
    data_block = html.split('id="repoviz-data" data-encoding="json">', 1)[1].split("</script>", 1)[0]
    assert "<" not in data_block
    data = json.loads(data_block)
    assert data["mode"] == "static" and data["comparisons"] and data["activity"]
    assert data["comparisons"][0]["diff"]["compact"] is True
    assert data["activity"].get("diff_ref") == data["comparisons"][0]["id"]


def test_static_report_compression_roundtrip(shop_repo) -> None:
    bundle = build_bundle(Repository(shop_repo.path), include_activity=False)
    html = render_static_html(bundle, compress=True)
    payload = html.split('data-encoding="gzip+base64">', 1)[1].split("</script>", 1)[0]
    data = json.loads(gzip.decompress(base64.b64decode(payload)))
    assert data["snapshot"]["repository_name"] == Path(shop_repo.path).name


def test_live_page_links_assets() -> None:
    page = render_live_html("demo")
    assert '<script src="/assets/mermaid.min.js"></script>' in page and "repoviz-data" not in page


# --------------------------------------------------------------------------- server


@pytest.fixture
def server(shop_repo):
    srv = create_server(Repository(shop_repo.path), port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, shop_repo
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, body=None, headers=None, full=False):
    """Call the server like the web app does (with X-Repoviz) unless ``headers`` is given."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers={"X-Repoviz": "1"} if headers is None else headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    if full:
        return resp.status, dict(resp.getheaders()), data
    return resp.status, resp.getheader("Content-Type"), data


def test_server_endpoints(server) -> None:
    srv, repo = server
    status, ctype, body = request(srv, "GET", "/")
    assert status == 200 and "text/html" in ctype and b"/assets/app.js" in body
    status, ctype, body = request(srv, "GET", "/assets/mermaid.min.js")
    assert status == 200 and "javascript" in ctype and len(body) > 1_000_000
    status, _, body = request(srv, "GET", "/api/bundle")
    bundle = json.loads(body)
    assert status == 200 and bundle["mode"] == "live" and bundle["snapshot"]["modules"]
    cycle_change(repo)
    status, _, body = request(srv, "GET", "/api/diff?mode=all")
    diff = json.loads(body)
    assert status == 200 and diff["diff"]["summary"]["cycles"]["introduced"] == 1
    status, headers, body = request(srv, "GET", "/api/activity", full=True)
    assert status == 200 and json.loads(body)["summary"]["files"] == 2
    # Polling an unchanged repository is answered with 304 Not Modified.
    status, _, body = request(srv, "GET", "/api/activity", headers={"X-Repoviz": "1", "If-None-Match": headers["ETag"]})
    assert status == 304 and body == b""
    Path(repo.path, "src/shop/api/brand_new.py").write_text("import os\n")
    status, _, body = request(srv, "GET", "/api/activity", headers={"X-Repoviz": "1", "If-None-Match": headers["ETag"]})
    assert status == 200 and json.loads(body)["summary"]["files"] == 3
    status, _, body = request(srv, "GET", "/api/diff?base=--output=/tmp/x&target=HEAD")
    assert status == 400 and b"invalid revision" in body
    status, _, _ = request(srv, "GET", "/api/diff?mode=bogus")
    assert status == 400


def test_server_security_checks(server) -> None:
    srv, _repo = server
    status, _, _ = request(srv, "GET", "/api/health", headers={"Host": "evil.example:80", "X-Repoviz": "1"})
    assert status == 403  # DNS rebinding protection
    status, _, _ = request(srv, "POST", "/api/session/start", body={}, headers={})
    assert status == 403  # cross-site request without the custom header
    for path in ("/api/activity", "/api/bundle", "/api/review?id=all"):
        assert request(srv, "GET", path, headers={})[0] == 403  # cross-site pages cannot trigger or embed API calls
    status, headers, _ = request(srv, "GET", "/", headers={}, full=True)
    assert status == 200 and "script-src 'self';" in headers["Content-Security-Policy"]
    assert "unsafe-eval" not in headers["Content-Security-Policy"]
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin" and headers["X-Content-Type-Options"] == "nosniff"
    status, _, _ = request(srv, "POST", "/api/session/start", headers={"X-Repoviz": "1", "Content-Length": "-5"})
    assert status == 400
    status, _, body = request(srv, "POST", "/api/session/start", body={"label": "t"},
                              headers={"X-Repoviz": "1", "Content-Type": "application/json"})
    assert status == 200 and json.loads(body)["label"] == "t"
    status, _, body = request(srv, "POST", "/api/session/end", body={}, headers={"X-Repoviz": "1"})
    assert status == 200


def test_server_ignores_clients_that_disconnect(server, capfd, caplog) -> None:
    """A reload or closed tab drops connections mid-response; that is routine, not an error."""
    import socket
    import struct
    import time

    srv, _repo = server
    for path in ("/api/bundle", "/assets/mermaid.min.js", "/api/activity"):
        s = socket.create_connection(("127.0.0.1", srv.server_address[1]))
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Repoviz: 1\r\n\r\n".encode())
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # reset, like a cancelled fetch
        s.close()
    time.sleep(1.5)
    assert request(srv, "GET", "/api/health")[0] == 200
    err = capfd.readouterr().err
    assert "Traceback" not in err and "Broken pipe" not in err
    assert not [r for r in caplog.records if r.levelno >= 30 and r.name == "repoviz.server"]


# --------------------------------------------------------------------------- CLI


def test_cli_commands(shop_repo, tmp_path: Path, capsys) -> None:
    repo = shop_repo.path
    assert main(["discover", "-C", repo]) == 0
    assert "Source roots" in capsys.readouterr().out
    cycle_change(shop_repo)
    assert main(["diff", "-C", repo]) == 0
    out = capsys.readouterr().out
    assert "Cycles introduced:" in out and "shop.core.models → shop.api.views" in out
    assert main(["diff", "-C", repo, "--fail-on", "new-cycle"]) == 3
    assert main(["diff", "-C", repo, "--mode", "staged", "--fail-on", "new-cycle"]) == 0
    capsys.readouterr()
    assert main(["diff", "-C", repo, "--format", "markdown", "--level", "module"]) == 0
    assert "```mermaid" in capsys.readouterr().out
    assert main(["mermaid", "-C", repo, "--view", "structure", "--files"]) == 0
    assert capsys.readouterr().out.startswith("flowchart")
    snap = tmp_path / "snap.json"
    assert main(["snapshot", "-C", repo, "--rev", "HEAD", "-o", str(snap)]) == 0
    assert json.loads(snap.read_text())["kind"] == "commit"
    report = tmp_path / "r.html"
    assert main(["report", "-C", repo, "-o", str(report), "--compare", "HEAD..WORKTREE"]) == 0
    assert report.stat().st_size > 1_000_000
    assert main(["session", "-C", repo, "start", "--label", "x"]) == 0
    assert main(["activity", "-C", repo, "--json"]) == 0
    assert main(["session", "-C", repo, "end"]) == 0
    assert main(["diff", "-C", repo, "--base", "no-such-rev"]) == 1


# --------------------------------------------------------------------------- read-only guarantee


def fingerprint_tree(root: Path) -> dict[str, tuple[int, str]]:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_mtime_ns, hashlib.sha1(p.read_bytes()).hexdigest())
    return out


def test_analysis_is_read_only(shop_repo, tmp_path: Path) -> None:
    repo = shop_repo
    cycle_change(repo)
    repo.write({"src/shop/api/staged.py": "x = 1\n"}).stage("src/shop/api/staged.py")
    before = fingerprint_tree(Path(repo.path))
    r = Repository(repo.path)
    for spec in ("HEAD", "INDEX", "WORKTREE", "WORKTREE-TRACKED"):
        r.snapshot(spec)
    for mode in ("all", "staged", "unstaged"):
        r.compare(mode=mode)
    from repoviz.activity import observe

    observe(r)
    render_static_html(build_bundle(r))
    main(["report", "-C", repo.path, "-o", str(tmp_path / "out.html")])
    after = fingerprint_tree(Path(repo.path))
    assert before == after, sorted(set(before.items()) ^ set(after.items()))[:5]


def test_coupling_cli(make_repo, capsys) -> None:
    repo = make_repo({"app.py": "x = 0\n", "schema.sql": "-- 0\n", ".repoviz.toml": "[history]\nmin_commits = 3\n"})
    for i in range(1, 6):
        repo.write({"app.py": f"x = {i}\n", "schema.sql": f"-- {i}\n"}).commit(f"c{i}")
    assert main(["coupling", "--repo", repo.path, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["history"]["usable"] and {data["pairs"][0]["a"], data["pairs"][0]["b"]} == {"app.py", "schema.sql"}
    assert main(["coupling", "--repo", repo.path, "--path", "app.py"]) == 0
    out = capsys.readouterr().out
    assert "6 commits together  app.py  (100% of its 6 commits)" in out and "schema.sql" in out


def test_review_any_two_branches_over_the_api_and_in_reports(make_repo, tmp_path) -> None:
    from test_review import diverged_repo

    repo = diverged_repo(make_repo)
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, _, body = request(srv, "GET", "/api/review?base=main&target=feature&mode=merge-base")
        data = json.loads(body)
        assert status == 200 and [f["path"] for f in data["files"]] == ["app/b.py"]
        assert data["target"]["key"] == "range:main...feature" and "notes" in data
        status, _, body = request(srv, "GET", "/api/review?base=main&target=feature&mode=exact")
        assert status == 200 and len(json.loads(body)["files"]) == 3
        status, _, body = request(srv, "GET", "/api/review?base=main&target=no-such-branch&mode=merge-base")
        assert status == 400 and "unknown revision" in json.loads(body)["error"]
        status, _, body = request(srv, "GET", "/api/review/targets")
        assert {"range:main...feature", "range:main...other"} <= {t["id"] for t in json.loads(body)}
    finally:
        srv.shutdown()
        srv.server_close()
    bundle = build_bundle(Repository(repo.path), extra_reviews=["feature...other", "main...nope"])
    first = bundle["reviews"][0]
    assert first["target"]["key"] == "range:feature...other" and [f["path"] for f in first["files"]] == ["app/o.py"]
    assert any("main...nope" in e["message"] for e in bundle["errors"])  # reported, the report still builds
    out = tmp_path / "branches.html"
    assert main(["report", "-C", repo.path, "-o", str(out), "--no-compress", "--no-activity",
                 "--review", "main...feature"]) == 0
    assert "feature since it left main" in out.read_text(encoding="utf-8")


def test_file_changes_over_the_api_and_in_reports(make_repo, monkeypatch) -> None:
    from test_changes_activity import hotspot_repo

    import repoviz.filechanges as fc

    repo = hotspot_repo(make_repo)
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, _, body = request(srv, "GET", "/api/file/changes?path=app/hot.py")
        data = json.loads(body)
        assert status == 200 and len(data["commits"]) == 5 and data["hunks"]
        sha = data["commits"][2]["sha"]
        status, _, body = request(srv, "GET", f"/api/file/changes?path=app/hot.py&commit={sha[:10]}")
        assert status == 200 and json.loads(body)["shown"] == sha
        for bad in ("path=../x", "path=app/hot.py&commit=nope", "path=app/none.py"):
            status, _, body = request(srv, "GET", f"/api/file/changes?{bad}")
            assert status == 400 and json.loads(body)["error"]
        status, _, _ = request(srv, "GET", "/api/file/changes?path=app/hot.py", headers={})  # no X-Repoviz header
        assert status == 403
    finally:
        srv.shutdown()
        srv.server_close()
    monkeypatch.setattr(fc, "REPORT_DIFF_LINES", 3)  # a tiny budget: the report says the diff was truncated
    bundle = build_bundle(Repository(repo.path))
    assert list(bundle["file_changes"]) == ["app/hot.py"]  # only the churn hotspots
    hot = bundle["file_changes"]["app/hot.py"]
    assert hot["truncated"] and sum(len(hk["lines"]) for hk in hot["hunks"]) == 3
    assert build_bundle(Repository(repo.path), mode="live")["file_changes"] == {}  # the live app asks on demand


def test_contracts_command_exit_codes_json_and_sarif(make_repo, capsys) -> None:
    from test_review import LAYERED, LAYERS_TOML

    repo = make_repo(dict(LAYERED, **{".repoviz.toml": LAYERS_TOML}))
    assert main(["contracts", "-C", repo.path]) == 0
    assert "Contracts (1): all pass" in capsys.readouterr().out
    repo.write({"app/models/user.py": "from app.routes import api\n"})
    assert main(["contracts", "-C", repo.path, "--format", "json"]) == 3  # a violation not in the baseline
    data = json.loads(capsys.readouterr().out)
    assert data["new"] == 1 and data["contracts"][0]["status"] == "fail"
    [v] = data["violations"]
    assert (v["path"], v["line"], v["key"]) == ("app/models/user.py", 1, "Layered backend::app.models.user::app.routes.api")
    assert main(["contracts", "-C", repo.path, "--format", "sarif"]) == 3
    sarif = json.loads(capsys.readouterr().out)
    [result] = sarif["runs"][0]["results"]
    assert sarif["version"] == "2.1.0" and result["ruleId"] == "Layered backend" and result["level"] == "error"
    assert result["locations"][0]["physicalLocation"] == {"artifactLocation": {"uri": "app/models/user.py"},
                                                          "region": {"startLine": 1}}
    assert main(["contracts", "-C", repo.path, "--baseline"]) == 0
    baseline = capsys.readouterr().out
    assert json.loads(baseline)["violations"][0]["key"] == v["key"]
    assert not Path(repo.path, ".repoviz-known-violations.json").exists()  # printed, never written by repoviz
    repo.write({".repoviz-known-violations.json": baseline})
    assert main(["contracts", "-C", repo.path]) == 0
    assert "1 known violation(s) not reported" in capsys.readouterr().out
    assert main(["contracts", "-C", repo.path, "--no-baseline"]) == 3
    capsys.readouterr()
    assert main(["contracts", "-C", repo.path, "--suggest"]) == 0
    assert "import each other in a cycle" in capsys.readouterr().out  # models now imports routes
    assert main(["review", "-C", repo.path, "--fail-on", "contract-broken"]) == 0  # known: not reported


def test_contracts_command_without_contracts(make_repo, capsys) -> None:
    repo = make_repo({"a.py": "A = 1\n"})
    assert main(["contracts", "-C", repo.path]) == 0
    assert "no contracts configured" in capsys.readouterr().err


# --------------------------------------------------------------------------- CI outputs (#17)


def _ci_report(make_repo):
    from test_review import diverged_repo

    repo = diverged_repo(make_repo)
    r = Repository(repo.path)
    from repoviz.review import build_review, resolve_target

    return repo, build_review(r, resolve_target(r, "main...feature"))


def test_review_sarif_has_the_required_keys_and_stable_fingerprints(make_repo, capsys) -> None:
    from repoviz.ci import review_sarif

    repo, report = _ci_report(make_repo)
    sarif = review_sarif(report, "9.9")
    assert sarif["version"] == "2.1.0" and sarif["$schema"].endswith("sarif-2.1.0.json")
    [run] = sarif["runs"]
    rules = {r["id"]: r for r in run["tool"]["driver"]["rules"]}
    assert run["tool"]["driver"]["name"] == "repoviz" and len(rules) == len(run["tool"]["driver"]["rules"])
    assert run["results"] and all(set(r) >= {"ruleId", "level", "message", "partialFingerprints"} for r in run["results"])
    for res in run["results"]:
        assert res["ruleId"] in rules and res["level"] in ("error", "warning", "note")
        assert res["message"]["text"] and res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "app/b.py"
    for rule in rules.values():
        assert rule["shortDescription"]["text"] and rule["defaultConfiguration"]["level"] in ("error", "warning", "note")
    assert main(["review", "-C", repo.path, "main...feature", "--format", "sarif"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert [r["partialFingerprints"] for r in again["runs"][0]["results"]] == \
        [r["partialFingerprints"] for r in run["results"]]  # stable across runs: tracked across pushes


def test_github_workflow_commands_are_escaped() -> None:
    from repoviz.ci import github_commands

    findings = [{"kind": "k", "severity": "high", "title": "Bad: 100%, really", "path": "a,b:c%.py", "line": 7,
                 "detail": "50% done\r\nnext: line, more", "suggestion": "fix"},
                {"kind": "k", "severity": "low", "title": "Minor", "detail": "no path"},
                {"kind": "k", "severity": "info", "title": "Hidden"}]
    out = github_commands(findings, "low").splitlines()
    assert out[0] == ("::error file=a%2Cb%3Ac%25.py,line=7,title=repoviz%3A Bad%3A 100%25%2C really::"
                      "50%25 done%0D%0Anext: line, more%0ASuggestion: fix")
    assert out[1] == "::notice title=repoviz%3A Minor::no path" and len(out) == 2  # info is below "low"


def test_pr_comment_map_links_and_size(make_repo) -> None:
    from repoviz.ci import COMMENT_MARKER, MAX_MAP_NODES, pr_comment

    _repo, report = _ci_report(make_repo)
    text = pr_comment(report, link_base="https://github.com/o/r/blob/abc")
    assert text.startswith(COMMENT_MARKER) and "```mermaid\nflowchart LR" in text
    assert "[`app/b.py`](https://github.com/o/r/blob/abc/app/b.py)" in text and "<details>" in text
    # A wave of 120 files in one component: the map caps at 40 nodes, the comment at the size limit.
    big = dict(report, files=[dict(report["files"][0], path=f"app/m{i:03}.py", findings=[]) for i in range(120)],
               components=[dict(report["components"][0], files=120)])
    text = pr_comment(big, link_base="https://github.com/o/r/blob/abc")
    diagram = text.split("```mermaid\n", 1)[1].split("```", 1)[0]
    nodes = [ln for ln in diagram.splitlines() if ln.strip().startswith(("n", "more")) and "[" in ln]
    assert len(nodes) == MAX_MAP_NODES and '"… 81 more"' in diagram and "81 more not drawn" in text
    small = pr_comment(big, max_chars=4000)
    assert len(small) <= 4000 and "The file list was shortened" in small and small.startswith(COMMENT_MARKER)


def test_review_from_report_formats_and_gates_without_analysing(make_repo, tmp_path, capsys) -> None:
    repo, _report = _ci_report(make_repo)
    saved = tmp_path / "review.json"
    assert main(["review", "-C", repo.path, "main...feature", "--format", "json", "-o", str(saved)]) == 0
    assert main(["review", "--from-report", str(saved), "--format", "pr-comment"]) == 0
    assert capsys.readouterr().out.startswith("<!-- repoviz-review -->")
    assert main(["review", "--from-report", str(saved), "--fail-on", "medium"]) == 3
    (tmp_path / "bad.json").write_text('{"x": 1}')
    assert main(["review", "--from-report", str(tmp_path / "bad.json")]) == 1
    assert "not a repoviz review report" in capsys.readouterr().err


def test_contracts_github_annotations(make_repo, capsys) -> None:
    from test_review import LAYERED, LAYERS_TOML

    repo = make_repo(dict(LAYERED, **{".repoviz.toml": LAYERS_TOML}))
    repo.write({"app/models/user.py": "from app.routes import api\n"})
    assert main(["contracts", "-C", repo.path, "--format", "github"]) == 3
    assert capsys.readouterr().out.startswith("::error file=app/models/user.py,line=1,title=repoviz%3A Contract broken")


def test_review_action_is_sticky_and_never_runs_the_repository() -> None:
    import pytest

    from repoviz.ci import COMMENT_MARKER

    yaml = pytest.importorskip("yaml")
    root = Path(__file__).resolve().parents[1]
    action = yaml.safe_load((root / ".github/actions/review/action.yml").read_text())
    steps = action["runs"]["steps"]
    runs = "\n".join(s.get("run", "") for s in steps)
    assert COMMENT_MARKER in runs and "PATCH" in runs  # updates its own comment in place
    assert 'pip install --quiet --disable-pip-version-check "$GITHUB_ACTION_PATH/../../.."' in runs
    assert "$GITHUB_WORKSPACE" not in runs and "pip install ." not in runs  # installs repoviz, never the analyzed repo
    assert set(action["inputs"]) >= {"base", "head", "fail-on", "min-severity", "comment", "sarif"}
    workflow = yaml.safe_load((root / ".github/workflows/repoviz-review.yml").read_text())
    assert workflow["jobs"]["review"]["steps"][0]["with"]["fetch-depth"] == 0


def test_mermaid_system_view(make_repo, capsys) -> None:
    from test_discovery_manifests import SYSTEM

    repo = make_repo(SYSTEM)
    assert main(["mermaid", "-C", repo.path, "--view", "system"]) == 0
    out = capsys.readouterr().out
    assert 'subgraph sg_infra["Infrastructure"]' in out and "🗄 mongo" in out and "⚡ redis" in out
    assert '==>|"talks to · mongodb:27017, starts after"|' in out  # one line per pair
    assert '---|"shares volume · uploads"|' in out and "📄 app.main" in out
    assert "stroke-dasharray:1 3" in out and "classDef kind_infra" in out
    empty = make_repo({"a.py": "x = 1\n"})
    assert main(["mermaid", "-C", empty.path, "--view", "system"]) == 0
    assert "no services found" in capsys.readouterr().err


TWO_PACKAGES = {  # 2 code packages and 5 Compose services (#24)
    "app/__init__.py": "", "app/api/__init__.py": "", "app/core/__init__.py": "",
    "app/api/routes.py": "from app.core import models\n\napp = models\n",
    "app/core/models.py": "import requests\n\nUser = object\n",
    "worker/__init__.py": "", "worker/tasks.py": "from app.core import models\n\ncelery = models\n",
    "Dockerfile": "FROM python:3.12\n",
    "docker-compose.yml": "services:\n  api:\n    build: .\n    command: uvicorn app.api.routes:app\n"
                          "  worker:\n    build: .\n    command: celery -A worker.tasks worker\n"
                          "  redis:\n    image: redis:7\n  mongo:\n    image: mongo:7\n  proxy:\n    image: nginx:1.25\n",
    "requirements.txt": "requests==2.32.0\n",
}


def test_discover_breaks_the_counts_down(make_repo, capsys) -> None:
    repo = make_repo(TWO_PACKAGES)
    assert main(["discover", "-C", repo.path, "--json"]) == 0
    bd = json.loads(capsys.readouterr().out)["breakdown"]
    assert bd["code_components"] == 2 and bd["services"] == 5 and bd["first_party_services"] == 2
    assert bd["external_packages"] == 1 and bd["submodules"] == 0 and bd["modules"] == 7
    assert main(["discover", "-C", repo.path]) == 0
    text = capsys.readouterr().out
    assert "contents: 2 code components · 5 services (2 first-party) · 1 external package" in text
    from repoviz.render.html import build_bundle

    assert build_bundle(Repository(repo.path), include_activity=False)["breakdown"] == bd  # the header's numbers


# --------------------------------------------------------------------------- Readability at scale (#27)


def branching_tree(branching: tuple[int, ...] = (2, 2, 2, 2, 1)) -> dict[str, str]:
    """A package tree one level deeper than ``branching``: the default is 48 packages, 6 levels deep."""
    files: dict[str, str] = {}
    paths = ["pkg"]
    for b in branching:
        nxt: list[str] = []
        for p in paths:
            files[f"{p}/__init__.py"] = ""
            nxt += [f"{p}/d{len(nxt) + i}" for i in range(b)]
        paths = nxt
    files.update({f"{p}/__init__.py": "" for p in paths})
    return files


def test_orientation_follows_the_shape(make_repo, capsys) -> None:
    repo = make_repo(branching_tree())
    deep = views.structure_view(Repository(repo.path).snapshot(), depth=8)
    assert len(deep.nodes) == 48 and deep.orientable and views.choose_direction(deep) == "LR"  # 6 levels deep
    chain = views.ViewGraph("chain", nodes=[views.VNode(f"n{i}", "x") for i in range(12)],
                            edges=[views.VEdge(f"n{i}", f"n{i + 1}") for i in range(11)])
    assert views.choose_direction(chain) == "TB"  # 12 × 250 px wide, or 12 × 116 px tall: TB fits at a larger zoom
    small = views.ViewGraph("small", direction="LR", nodes=chain.nodes[:3], edges=chain.edges[:2])
    assert views.choose_direction(small) == "LR" and views.choose_direction(views.ViewGraph("empty")) == "LR"
    assert main(["mermaid", "-C", repo.path, "--view", "structure", "--depth", "8"]) == 0
    assert capsys.readouterr().out.startswith("flowchart LR")
    assert main(["mermaid", "-C", repo.path, "--view", "structure", "--depth", "8", "--direction", "TB"]) == 0
    assert capsys.readouterr().out.startswith("flowchart TB")
    # A small system keeps its services side by side; it can still be turned.
    system = make_repo(TWO_PACKAGES)
    assert main(["mermaid", "-C", system.path, "--view", "system"]) == 0
    assert capsys.readouterr().out.startswith("flowchart TB")
    assert main(["mermaid", "-C", system.path, "--view", "system", "--direction", "LR"]) == 0
    assert capsys.readouterr().out.startswith("flowchart LR")
    # A layout whose direction is part of it (a layers contract) keeps it.
    from repoviz.cli import _orient

    layers = views.ViewGraph("layers", direction="TB", orientable=False, nodes=chain.nodes, edges=chain.edges)
    _orient(layers, "LR")
    _orient(layers, "auto")
    assert layers.direction == "TB"


TESTS_27 = {"pkg/__init__.py": "", "pkg/core.py": "X = 1\n",
            **{f"pkg/tests/test_{i:02}.py": "def test_x():\n    pass\n" for i in range(27)}}


def test_long_leaf_lists_fold(make_repo, capsys) -> None:
    repo = make_repo(TESTS_27)
    snap = Repository(repo.path).snapshot()
    tests_dir = next(n for n in snap.components if n.path == "pkg/tests")
    view = views.structure_view(snap, depth=4, include_files=True)
    fold_id = f"fold_{tests_dir.id}_test"
    assert view.folds[fold_id] == {"parent": tests_dir.id, "kind": "test", "count": 27}
    fold = next(n for n in view.nodes if n.id == fold_id)
    assert fold.label == "+ 27 test files" and fold.shape == "stadium" and fold.icon == "🧪"
    assert not any(n.label.startswith("test_") for n in view.nodes)
    assert any(e.source == tests_dir.id and e.target == fold_id for e in view.edges)
    # What must stay visible (a changed file) is pulled out of the fold.
    changed = next(n for n in snap.nodes() if n.path == "pkg/tests/test_05.py")
    kept = views.structure_view(snap, depth=4, include_files=True, keep={changed.id})
    assert kept.folds[fold_id]["count"] == 26 and any(n.id == changed.id for n in kept.nodes)
    everything = views.structure_view(snap, depth=4, include_files=True, fold=0)
    assert not everything.folds and sum(n.label.startswith("test_") for n in everything.nodes) == 27
    # Folding needs more than N members: 8 test files are drawn one by one.
    small = views.structure_view(snap, depth=4, include_files=True, fold=30)
    assert not small.folds
    assert main(["mermaid", "-C", repo.path, "--view", "structure", "--files", "--depth", "4"]) == 0
    out = capsys.readouterr()
    assert f'{fold_id}(["🧪 + 27 test files' in out.out and "1 group(s) of files folded" in out.err
    assert main(["mermaid", "-C", repo.path, "--view", "structure", "--files", "--depth", "4", "--fold", "0"]) == 0
    assert "fold_" not in capsys.readouterr().out


def test_long_labels_are_shortened_in_the_middle() -> None:
    name = "repoviz.analyzers.javascript_resolver.more"
    short = mermaid.mid_trunc(name)
    assert len(short) == mermaid.MAX_LABEL and short == "repoviz.analyzers.…ascript_resolver.more"
    assert mermaid.mid_trunc("short.name") == "short.name"
    view = views.ViewGraph("t", mode="kind", nodes=[views.VNode("a", name)])
    assert short in mermaid.to_mermaid(view) and name not in mermaid.to_mermaid(view)


CYCLE_27 = {"app/__init__.py": "", "app/routes.py": "from app import tasks\n", "app/tasks.py": "from app import routes\n",
            "app/other.py": "X = 1\n", "app/util.py": "Y = 1\n"}


def test_old_cycles_the_change_does_not_touch_are_faint(make_repo) -> None:
    repo = make_repo(CYCLE_27)
    repo.write({"app/other.py": "from app import util\n"})  # a change away from the cycle
    _comp, diff = Repository(repo.path).compare(mode="all")
    view = views.changes_view(diff, level="module", scope="all")
    cyc = [e for e in view.edges if e.cycle]
    assert len(cyc) == 2 and all(e.cycle_existing and not e.cycle_introduced for e in cyc)
    text = mermaid.to_mermaid(view)
    assert text.count('"existing cycle"') == 2 and "⟲ cycle" not in text
    faint = [l for l in text.splitlines() if "stroke-opacity:0.5" in l]
    assert len(faint) == 2 and all("stroke-width:1px" in l and "stroke-dasharray:2 4" in l for l in faint)
    # Touching a member of the cycle brings back the strong style.
    repo.write({"app/tasks.py": "from app import routes\n\nZ = 2\n"})
    _comp, diff = Repository(repo.path).compare(mode="all")
    view = views.changes_view(diff, level="module", scope="all")
    assert [e.cycle_existing for e in view.edges if e.cycle] == [False, False]
    assert "⟲ cycle" in mermaid.to_mermaid(view)
    # A new cycle is never faint.
    fresh = make_repo({k: v for k, v in CYCLE_27.items() if k != "app/tasks.py"} | {"app/tasks.py": "X = 1\n"})
    fresh.write({"app/tasks.py": "from app import routes\n"})
    _comp, diff = Repository(fresh.path).compare(mode="all")
    new = [e for e in views.changes_view(diff, level="module", scope="all").edges if e.cycle]
    assert new and all(e.cycle_introduced and not e.cycle_existing for e in new)


def test_checkpoint_endpoint_and_automatic_checkpoints(make_repo, monkeypatch) -> None:
    import repoviz.server as server_module

    repo = make_repo({"app/__init__.py": "", "app/a.py": "A = 1\n"})
    r = Repository(repo.path)
    srv = create_server(r, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, _, body = request(srv, "POST", "/api/session/checkpoint", {})
        assert status == 400 and b"no active session" in body
        r.state.start_session(r.git, r.root, "wave")
        status, _, _ = request(srv, "POST", "/api/session/checkpoint", {}, headers={"Content-Type": "application/json"})
        assert status == 403  # the X-Repoviz header is required
        Path(repo.path, "app/a.py").write_text("A = 2\n")
        clock = [1000.0]
        monkeypatch.setattr(server_module.time, "monotonic", lambda: clock[0])
        # The page's poll records an automatic checkpoint, travels with the timeline and changes the ETag.
        status, headers, body = request(srv, "GET", "/api/activity", full=True)
        tl = json.loads(body)["timeline"]
        assert [(c["n"], c["origin"]) for c in tl["checkpoints"]] == [(1, "auto")]
        Path(repo.path, "app/a.py").write_text("A = 3\n")
        clock[0] += 5  # too soon for another automatic one
        status, _, body = request(srv, "GET", "/api/activity")
        assert len(json.loads(body)["timeline"]["checkpoints"]) == 1
        status, _, body = request(srv, "POST", "/api/session/checkpoint", {"label": "by hand"})
        out = json.loads(body)
        assert status == 200 and out["created"] and out["checkpoint"]["n"] == 2 and "state" not in out["checkpoint"]
        status, _, body = request(srv, "POST", "/api/session/checkpoint", {"label": "again"})
        assert json.loads(body)["created"] is False and "nothing changed since checkpoint 2" in json.loads(body)["message"]
        status, _, _ = request(srv, "GET", "/api/activity", headers={"X-Repoviz": "1", "If-None-Match": headers["ETag"]})
        assert status == 200  # the timeline changed
        clock[0] += 60
        Path(repo.path, "app/a.py").write_text("A = 4\n")
        _, _, body = request(srv, "GET", "/api/activity")
        assert [c["n"] for c in json.loads(body)["timeline"]["checkpoints"]] == [1, 2, 3]
        status, _, body = request(srv, "GET", "/api/review?id=checkpoint:2-3")
        assert status == 200 and [f["path"] for f in json.loads(body)["files"]] == ["app/a.py"]
    finally:
        srv.shutdown()
        srv.server_close()
