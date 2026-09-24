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
