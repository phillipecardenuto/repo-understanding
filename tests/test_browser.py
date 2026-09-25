"""End-to-end tests of the web UI in a headless browser.

Skipped unless Playwright and a Chromium build are available.  The browser is
taken from ``REPOVIZ_CHROMIUM`` or Playwright's default installation.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from repoviz.render.html import build_bundle, render_static_html
from repoviz.repo import Repository
from repoviz.server import create_server

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.browser

TABS = ("review", "changes", "structure", "dependencies", "activity")
ALL_RENDERED = """(tab) => { const vs = document.querySelectorAll('#tab-' + tab + ' .viewport');
  return vs.length > 0 && Array.from(vs).every(v => v.querySelector('svg') ||
    (!v.querySelector('.overlay').hidden && !v.querySelector('.overlay').textContent.includes('Rendering'))); }"""


def _executable() -> str | None:
    env = os.environ.get("REPOVIZ_CHROMIUM")
    if env:
        return env
    for candidate in sorted(Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/nonexistent")).glob("chromium-*/chrome-linux/chrome")):
        return str(candidate)
    return None


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(executable_path=_executable())
        except Exception as exc:  # pragma: no cover - depends on the environment
            pytest.skip(f"Chromium is not available: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page(viewport={"width": 1400, "height": 900})
    pg.errors = []  # type: ignore[attr-defined]
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))  # type: ignore[attr-defined]
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)  # type: ignore[attr-defined]
    yield pg
    pg.close()


def prepare(repo) -> None:
    models = Path(repo.path, "src/shop/core/models.py")
    models.write_text(models.read_text().replace("if TYPE_CHECKING:\n    from shop.api.views import View",
                                                 "from shop.api.views import View"))
    # A file name full of markup must not break diagrams or execute anything.
    repo.write({"src/shop/api/extra.py": "import json\n", "src/shop/x<img src=q onerror=alert(1)>.py": "import json\n"})


def visit_all_tabs(page) -> dict[str, list[int]]:
    rendered = {}
    for tab in TABS:
        page.click(f"#tabbtn-{tab}")
        page.wait_for_function(ALL_RENDERED, arg=tab, timeout=60_000)
        rendered[tab] = page.evaluate("(t) => Array.from(document.querySelectorAll('#tab-' + t + ' .viewport')).map(v => v.querySelectorAll('g.node').length)", tab)
    return rendered


def test_static_report_renders_every_tab(page, shop_repo, tmp_path: Path) -> None:
    prepare(shop_repo)
    report = tmp_path / "report.html"
    report.write_text(render_static_html(build_bundle(Repository(shop_repo.path))), encoding="utf-8")
    page.goto(report.as_uri())
    rendered = visit_all_tabs(page)
    assert all(counts and all(c > 0 for c in counts) for counts in rendered.values()), rendered
    assert page.errors == []  # type: ignore[attr-defined]
    # Module level shows the introduced cycle in purple with a "new cycle" label, computed in JavaScript.
    page.click("#tabbtn-changes")
    edges = page.evaluate("""() => { const t = repoviz.app.tabs.changes;
        const view = repoviz.changesView(t.di, Object.assign({}, t.opts, {level: 'module', scope: 'all'}));
        return view.edges.filter(e => e.cycle).map(e => [t.di.nodes.get(e.source).qualified_name, t.di.nodes.get(e.target).qualified_name, e.cycleIntroduced]); }""")
    assert ["shop.core.models", "shop.api.views", True] in edges
    text = page.evaluate("() => repoviz.toMermaid(repoviz.changesView(repoviz.app.tabs.changes.di, Object.assign({}, repoviz.app.tabs.changes.opts, {level: 'module', scope: 'all'})))")
    assert "⟲ new cycle" in text and "#lt;img" in text and "<img" not in text.replace("<small>", "")
    # Clicking a node shows details with source evidence.
    page.click("#tab-changes g.node >> nth=0")
    assert page.inner_text("#tab-changes .split > .card").strip() != ""
    # Kind icons are drawn with CSS masks (no emoji): a house for the repository, folders for packages.
    page.click("#tabbtn-structure")
    assert page.locator("#tab-structure g.node i.rvi-house").count() == 1
    assert page.locator("#tab-structure g.node i.rvi-folder").count() >= 1
    mask = page.evaluate("getComputedStyle(document.querySelector('#tab-structure g.node i.rvi-house')).webkitMaskImage")
    width = page.evaluate("document.querySelector('#tab-structure g.node i.rvi-house').getBoundingClientRect().width")
    assert mask.startswith('url("data:image/svg+xml') and width > 8
    assert not any(e in page.inner_text("body") for e in ("🏠", "📁", "📄", "🧪", "🚀"))


def test_live_app_and_session_controls(page, shop_repo) -> None:
    prepare(shop_repo)
    srv = create_server(Repository(shop_repo.path), port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
        rendered = visit_all_tabs(page)
        assert all(counts for counts in rendered.values())
        assert "live" in page.inner_text("#mode-badge")
        # Switch comparison mode through the live API.
        page.click("#tabbtn-changes")
        page.select_option("#tab-changes select >> nth=0", "staged")
        page.wait_for_function("() => repoviz.app.tabs.changes.comp && repoviz.app.tabs.changes.comp.mode === 'staged'", timeout=30_000)
        # Start a work session from the Activity tab.
        page.click("#tabbtn-activity")
        page.click("text=Start session")
        page.wait_for_function("() => document.querySelector('#tab-activity').innerText.includes('work session')", timeout=30_000)
        assert Repository(shop_repo.path).current_session() is not None
        assert page.errors == []  # type: ignore[attr-defined]
    finally:
        srv.shutdown()
        srv.server_close()


def test_review_tab_triage_and_feedback(page, make_repo, tmp_path: Path) -> None:
    from test_review import APP, agent_wave

    repo = make_repo(APP)
    r = Repository(repo.path)
    r.state.start_session(r.git, r.root, "wave 1", allowed=["src/app/billing/**", "tests/**"],
                          protected=["src/app/auth/**"])
    agent_wave(repo)
    report = tmp_path / "review.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.goto(report.as_uri())
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    assert page.evaluate("document.querySelectorAll('#tab-review .viewport g.node').length") > 0
    text = page.inner_text("#tab-review")
    assert "Protected area modified" in text and "Call to a removed function" in text
    # The map marks the protected area with its own style.
    assert "scope_protected" in page.evaluate("repoviz.app.tabs.review.map.text")
    # Scope can be edited in the page (client-side re-evaluation).
    page.fill("#tab-review textarea >> nth=1", "src/app/auth/**, src/app/util/**")
    page.click("#tab-review >> text=Apply scope")
    assert page.evaluate("repoviz.app.tabs.review.report.files.filter(f => f.scope === 'protected').length") == 2
    # Open a file, annotate a diff line, send a signal to the agent.
    page.click("#tab-review .files-split tbody tr >> text=src/app/billing/invoice.py")
    page.click("#tab-review table.diff tr.add >> nth=0")
    page.fill("#tab-review .note-form textarea", "Remove the breakpoint before merging")
    page.click("#tab-review .note-form >> text=Add to feedback")
    page.click("#tab-review .findings-card li.finding:has-text('Call to a removed function') >> text=→ Send to agent")
    prompt = page.inner_text("#tab-review pre.prompt")
    assert "Remove the breakpoint before merging" in prompt and "`src/app/billing/invoice.py:" in prompt
    assert "Call to a removed function" in prompt and "Do not modify: `src/app/auth/**`" in prompt
    # Notes survive a reload (static reports keep them in the browser).
    page.reload()
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    assert "Remove the breakpoint before merging" in page.inner_text("#tab-review pre.prompt")
    assert page.errors == []  # type: ignore[attr-defined]


def test_review_navigation_progress_and_scope_reset(page, make_repo, tmp_path: Path) -> None:
    from test_review import APP, agent_wave

    repo = make_repo(APP)
    r = Repository(repo.path)
    r.state.start_session(r.git, r.root, "wave 1", protected=["src/app/auth/**"])
    agent_wave(repo)
    report = tmp_path / "review.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.goto(report.as_uri())
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    total = page.evaluate("repoviz.app.tabs.review.report.files.length")
    selected = "repoviz.app.tabs.review.selectedFile"
    # j / k walk through the files in table order.
    page.keyboard.press("j")
    first = page.evaluate(selected)
    page.keyboard.press("j")
    second = page.evaluate(selected)
    page.keyboard.press("k")
    assert first and second and first != second and page.evaluate(selected) == first
    assert page.locator("#tab-review .files-split tbody tr.selected").count() == 1
    # "Reviewed & next" records progress and moves on; progress survives a reload.
    page.click("#tab-review .file-nav >> text=Reviewed & next")
    assert page.evaluate(selected) != first
    assert f"1 / {total} reviewed" in page.inner_text("#tab-review .progress")
    page.reload()
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    assert f"1 / {total} reviewed" in page.inner_text("#tab-review .progress")
    # Scope edits can be reset to the session's scope.
    protected = "repoviz.app.tabs.review.report.files.filter(f => f.scope === 'protected').length"
    assert page.evaluate(protected) == 1
    page.fill("#tab-review textarea >> nth=1", "src/**")
    page.click("#tab-review >> text=Apply scope")
    assert page.evaluate(protected) > 1
    page.click("#tab-review .toolbar >> text=Reset")
    assert page.evaluate(protected) == 1 and page.input_value("#tab-review textarea >> nth=1") == "src/app/auth/**"
    assert page.errors == []  # type: ignore[attr-defined]


def test_review_tab_explains_how_to_start(page, make_repo, tmp_path: Path) -> None:
    repo = make_repo({"src/app.py": "import os\n"})
    report = tmp_path / "clean.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.goto(report.as_uri())
    page.wait_for_selector("#tab-review .empty-state:not([hidden])", timeout=60_000)
    text = page.inner_text("#tab-review .empty-state")
    assert "To review what a coding agent does" in text and "repoviz session start" in text
    assert page.errors == []  # type: ignore[attr-defined]


def test_live_review_refreshes_and_keeps_selection(page, make_repo) -> None:
    from test_review import APP, agent_wave

    repo = make_repo(APP)
    r = Repository(repo.path)
    r.state.start_session(r.git, r.root, "wave 1")
    agent_wave(repo)
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/#tab=review")
        page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
        files = "repoviz.app.tabs.review.report.files.length"
        before = page.evaluate(files)
        page.click("#tab-review .files-split tbody tr >> text=src/app/billing/invoice.py")
        # The agent keeps working; coming back to the tab picks the change up and keeps the open file.
        repo.write({"src/app/billing/late.py": "def late():\n    return 1\n"})
        page.click("#tabbtn-structure")
        page.wait_for_timeout(2100)
        page.click("#tabbtn-review")
        page.wait_for_function(f"() => {files} === {before + 1}", timeout=60_000)
        assert page.evaluate("repoviz.app.tabs.review.selectedFile") == "src/app/billing/invoice.py"
        assert page.errors == []  # type: ignore[attr-defined]
    finally:
        srv.shutdown()
        srv.server_close()


def test_review_shows_work_inside_submodules(page, make_repo, tmp_path: Path) -> None:
    from test_large_repo_fixes import _git

    lib = make_repo({"src/engine.py": "def run():\n    return 1\n"})
    main = make_repo({"app/__init__.py": "", "app/main.py": "print('hi')\n"})
    _git(main.path, "submodule", "add", "-q", lib.path, "modules/engine")
    _git(main.path, "commit", "-qm", "add engine")
    r = Repository(main.path)
    r.state.start_session(r.git, r.root, "wave", protected=["modules/**"])
    Path(main.path, "modules/engine/src/engine.py").write_text("def run():\n    return 2\n")
    report = tmp_path / "sub.html"
    report.write_text(render_static_html(build_bundle(Repository(main.path))), encoding="utf-8")
    page.goto(report.as_uri())
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    # One protected signal for the file inside (not a second one for the submodule itself).
    protected = page.evaluate("repoviz.app.tabs.review.report.findings.filter(f => f.kind === 'protected-touched').map(f => f.path)")
    assert protected == ["modules/engine/src/engine.py"]
    page.evaluate("repoviz.app.tabs.review.selectFile('modules/engine')")
    card = page.inner_text("#tab-review .file-card")
    assert "Submodule has uncommitted changes inside" in card and "src/engine.py" in card
    page.click("#tab-review .file-card tbody tr >> text=src/engine.py")
    assert page.evaluate("repoviz.app.tabs.review.selectedFile") == "modules/engine/src/engine.py"
    assert page.locator("#tab-review table.diff tr.add").count() >= 1
    # Grouped by component, the submodule's entry heads the group of the files inside it.
    page.check("#tab-review .list-tools label.check:has-text('group by component') input")
    head = page.locator("#tab-review tr.group-head")
    assert head.count() == 1 and "modules/engine" in head.inner_text() and "1 file inside" in head.inner_text()
    assert head.locator("button.group-toggle").get_attribute("aria-expanded") == "true"
    inner = page.locator("#tab-review tr.in-group:has-text('…/engine/src/engine.py')")
    assert inner.count() == 1 and inner.locator("span[title='modules/engine/src/engine.py']").count() == 1
    head.locator("button.group-toggle").click()
    assert page.locator("#tab-review tr.in-group:has-text('engine.py')").count() == 0 and head.count() == 1
    assert page.errors == []  # type: ignore[attr-defined]


def test_in_app_guide(page, shop_repo, tmp_path: Path) -> None:
    report = tmp_path / "guide.html"
    report.write_text(render_static_html(build_bundle(Repository(shop_repo.path))), encoding="utf-8")
    page.goto(report.as_uri())
    page.wait_for_function("window.repoviz && window.repoviz.app && window.repoviz.app.ready", timeout=60_000)
    assert page.locator("#help-toggle.is-new").count() == 1  # nudges first-time users
    # Every tab starts with a one-line hint that opens its section of the guide.
    assert "AI Review:" in page.inner_text("#tab-review .tab-intro")
    page.click("#tab-review .tab-intro >> text=How to use it")
    assert page.is_visible(".help-dialog") and "Set the scope first" in page.inner_text(".help-body")
    assert page.locator("#help-toggle.is-new").count() == 0
    # Sections, search and closing.
    page.click(".help-nav button >> text=Keyboard shortcuts")
    assert "mark the open file reviewed" in page.inner_text(".help-body")
    page.fill(".help-head input", "cycle")
    nav = page.inner_text(".help-nav")
    assert "Dependencies" in nav and "Privacy and safety" not in nav
    page.keyboard.press("Escape")
    assert not page.is_visible(".help-dialog")
    # "?" opens the guide at the current tab's section.
    page.click("#tabbtn-dependencies")
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=60_000)
    page.keyboard.press("?")
    assert page.is_visible(".help-dialog") and page.inner_text(".help-body h3").strip() == "Dependencies"
    page.click(".help-head >> text=Close")
    # A hidden hint stays hidden after a reload.
    page.click("#tab-dependencies .tab-intro button[aria-label='Hide this hint']")
    page.reload()
    page.wait_for_function("window.repoviz && window.repoviz.app && window.repoviz.app.ready", timeout=60_000)
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=60_000)
    assert page.locator("#tab-dependencies .tab-intro").count() == 0
    assert page.errors == []  # type: ignore[attr-defined]


def test_usual_companions_in_review_and_activity(page, make_repo, tmp_path: Path) -> None:
    repo = make_repo({"app.py": "x = 0\n", "schema.sql": "-- 0\n", ".repoviz.toml": "[history]\nmin_commits = 3\n"})
    for i in range(1, 7):
        repo.write({"app.py": f"x = {i}\n", "schema.sql": f"-- {i}\n"}).commit(f"feature {i}")
    Path(repo.path, "app.py").write_text("x = 'agent'\n")
    report = tmp_path / "coupling.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.goto(report.as_uri())
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    assert "Usual companion change missing" in page.inner_text("#tab-review")
    page.evaluate("repoviz.app.tabs.review.selectFile('app.py')")
    card = page.inner_text("#tab-review .file-card")
    assert "Usually changes with" in card and "schema.sql" in card and "not changed" in card
    page.click("#tabbtn-activity")
    page.wait_for_function(ALL_RENDERED, arg="activity", timeout=60_000)
    row = page.inner_text("#tab-activity tbody tr:has-text('app.py')")
    assert "schema.sql" in row
    page.click("#tab-activity tbody tr:has-text('app.py')")
    assert "Often changes with" in page.inner_text("#tab-activity")
    assert page.errors == []  # type: ignore[attr-defined]


def _wave_with_commits(make_repo):
    repo = make_repo({"app/__init__.py": "", "app/a.py": "A = 0\n", "app/b.py": "B = 0\n", "app/c.py": "C = 0\n"})
    r = Repository(repo.path)
    r.state.start_session(r.git, r.root, "wave")
    repo.write({"app/a.py": "A = 1\n"}).commit("agent: a")
    repo.write({"app/b.py": "B = 1\nprint('debug')\n"}).commit("agent: b")
    Path(repo.path, "app/c.py").write_text("C = 1\n")  # uncommitted
    return repo


def test_review_commit_by_commit_in_a_static_report(page, make_repo, tmp_path: Path) -> None:
    repo = _wave_with_commits(make_repo)
    report = tmp_path / "commits.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.goto(report.as_uri())
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    rows = page.locator("#tab-review .commits-card tbody tr")
    assert rows.count() == 3 and "Uncommitted changes" in rows.nth(2).inner_text()
    files = lambda: page.evaluate("repoviz.app.tabs.review.report.files.map(f => f.path).sort()")  # noqa: E731
    assert files() == ["app/a.py", "app/b.py", "app/c.py"]
    rows.nth(1).click()
    assert files() == ["app/b.py"]
    banner = page.inner_text("#tab-review .commit-banner")
    assert "Showing commit 2 of 3" in banner and "agent: b" in banner and "repoviz serve" in banner
    page.keyboard.press("]")  # next: the uncommitted work
    assert files() == ["app/c.py"]
    page.click("#tab-review .commit-banner >> text=Show all")
    assert files() == ["app/a.py", "app/b.py", "app/c.py"] and page.locator("#tab-review .commit-banner").is_hidden()
    assert page.errors == []  # type: ignore[attr-defined]


def test_review_commit_by_commit_in_the_live_app(page, make_repo) -> None:
    repo = _wave_with_commits(make_repo)
    Path(repo.path, "app/b.py").write_text("B = 1\n")  # the debug print is gone by the end of the wave
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
        page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
        kinds = lambda: page.evaluate("repoviz.app.tabs.review.report.findings.map(f => f.kind)")  # noqa: E731
        assert "debug-output" not in kinds()
        page.click("#tab-review .commits-card tbody tr >> nth=1")
        page.wait_for_function("() => repoviz.app.tabs.review.report.commit && repoviz.app.tabs.review.report.commit.subject === 'agent: b'", timeout=30_000)
        assert page.evaluate("repoviz.app.tabs.review.report.files.map(f => f.path)") == ["app/b.py"]
        assert "debug-output" in kinds()  # this step added a print that a later edit removed
        assert "alone" in page.inner_text("#tab-review .commit-banner")
        # A note taken on the commit belongs to the wave's feedback.
        page.click("#tab-review .findings-card li.finding:has-text('Debug output added') >> text=→ Send to agent")
        page.click("#tab-review .commit-banner >> text=Show all")
        page.wait_for_function("() => !repoviz.app.tabs.review.commit", timeout=30_000)
        assert "Debug output added" in page.inner_text("#tab-review pre.prompt")
        assert page.errors == []  # type: ignore[attr-defined]
    finally:
        srv.shutdown()


def test_commit_view_failures_and_reviewed_marks(page, make_repo) -> None:
    repo = _wave_with_commits(make_repo)
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    showing = "(s) => { const t = repoviz.app.tabs.review; return s ? !!(t.report.commit && t.report.commit.subject === s) : (!t.commit && t.report === t.waveReport); }"
    try:
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
        page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
        wave = page.evaluate("repoviz.app.tabs.review.report.files.map(f => f.path).sort()")
        # A reviewed mark set while looking at one commit still counts for the whole wave.
        page.evaluate("repoviz.app.tabs.review.selectCommit(repoviz.app.tabs.review.commitItems()[1].sha)")
        page.wait_for_function(showing, arg="agent: b", timeout=30_000)
        page.evaluate("repoviz.app.tabs.review.setReviewed('app/b.py', true)")
        page.evaluate("repoviz.app.tabs.review.selectCommit(null)")
        page.wait_for_function(showing, arg=None, timeout=30_000)
        assert page.evaluate("(() => { const t = repoviz.app.tabs.review; return t.isReviewed(t.report.files.find(f => f.path === 'app/b.py')); })()")
        # If a commit cannot be reviewed, the page goes back to the whole wave instead of showing a stale step.
        page.evaluate("repoviz.app.tabs.review.selectCommit(repoviz.app.tabs.review.commitItems()[1].sha)")
        page.wait_for_function(showing, arg="agent: b", timeout=30_000)
        page.route("**/api/review?*commit=*", lambda route: route.abort())
        page.evaluate("repoviz.app.tabs.review.stepCommit(1)")
        page.wait_for_function(showing, arg=None, timeout=30_000)
        assert page.evaluate("repoviz.app.tabs.review.report.files.map(f => f.path).sort()") == wave
        page.wait_for_function("() => document.querySelector('#tab-review').innerText.includes('Could not review this commit')", timeout=30_000)
    finally:
        srv.shutdown()


def test_review_orders_files_by_risk(page, make_repo, tmp_path: Path) -> None:
    from test_review import RISK_APP, risk_wave

    repo = make_repo(RISK_APP)
    risk_wave(repo)
    report = tmp_path / "risk.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.goto(report.as_uri())
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    badge = page.locator("#tab-review .stat.risk")
    assert "high · 42" in badge.inner_text() and "wave risk · tokens.py" in badge.inner_text()
    assert badge.locator("i.rvi-alert-circle").count() == 1  # an icon and a word, not colour alone
    rows = "Array.from(document.querySelectorAll('#tab-review .files-split > .card:not(.file-card) tbody tr')).map(r => r.cells[3].innerText.trim())"
    assert page.evaluate(rows) == ["app/auth/tokens.py", "app/core.py", "README.md", "tests/test_auth.py"]
    cell = page.locator("#tab-review .files-split > .card:not(.file-card) tbody tr >> nth=1").locator(".risk-cell")
    assert cell.inner_text().strip() == "42 high" and "+9 called from 4 places" in cell.get_attribute("title")
    page.keyboard.press("j")  # the riskiest file first
    assert page.evaluate("repoviz.app.tabs.review.selectedFile") == "app/auth/tokens.py"
    assert "high signal: Protected area modified" in page.inner_text("#tab-review .risk-factors")
    page.click("#tab-review .files-split th >> text=File")  # sort by path; the choice survives redraws
    page.keyboard.press("m")
    assert page.evaluate(rows) == ["README.md", "app/auth/tokens.py", "app/core.py", "tests/test_auth.py"]
    badge.click()
    assert page.evaluate("repoviz.app.tabs.review.selectedFile") == "app/auth/tokens.py"
    assert page.errors == []  # type: ignore[attr-defined]


def test_compare_any_two_branches_in_the_live_app(page, make_repo, tmp_path: Path) -> None:
    from test_review import diverged_repo

    repo = diverged_repo(make_repo)
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base, target = "#tab-review input[aria-label='Base branch or revision']", "#tab-review input[aria-label='Target branch or revision']"
    key_is = ("(k) => { const t = window.repoviz && repoviz.app && repoviz.app.tabs && repoviz.app.tabs.review;"
              " return !!(t && t.report && t.report.target.key === k && !t.loading); }")
    files = lambda: page.evaluate("repoviz.app.tabs.review.report.files.map(f => f.path).sort()")  # noqa: E731
    shown = lambda: page.evaluate("document.querySelector('#tab-review select[aria-label=\"Review target\"]').selectedOptions[0].textContent")  # noqa: E731
    try:
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
        page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
        suggestions = page.evaluate("Array.from(document.querySelectorAll('#rv-revs-review option')).map(o => o.value)")
        assert {"main", "feature", "other", "WORKTREE"} <= set(suggestions)
        page.fill(base, "feature")
        page.fill(target, "main")
        page.click("#tab-review button[aria-label='Swap base and target']")
        assert (page.input_value(base), page.input_value(target)) == ("main", "feature")
        page.click("#tab-review .compare-field >> text=Review")  # "since they diverged" by default
        page.wait_for_function(key_is, arg="range:main...feature", timeout=30_000)
        assert files() == ["app/b.py"] and shown().startswith("⇄ feature since it left main (merge base ")
        page.select_option("#tab-review select[aria-label='How to compare']", "exact")
        page.click("#tab-review .compare-field >> text=Review")
        page.wait_for_function(key_is, arg="range:main..feature", timeout=30_000)
        assert files() == ["app/a.py", "app/b.py", "app/c.py"] and shown() == "⇄ main → feature (exact difference)"
        page.reload()  # the comparison, its mode and the boxes are kept
        page.wait_for_function(key_is, arg="range:main..feature", timeout=60_000)
        assert (page.input_value(base), page.input_value(target)) == ("main", "feature")
        assert page.input_value("#tab-review select[aria-label='How to compare']") == "exact"
        page.fill(target, "no-such-branch")  # an unknown branch keeps the current review on screen
        page.click("#tab-review .compare-field >> text=Review")
        page.wait_for_function("() => document.querySelector('#tab-review [role=status]').textContent.includes('Could not compare')", timeout=30_000)
        assert "unknown revision" in page.inner_text("#tab-review [role=status]")
        assert page.evaluate("repoviz.app.tabs.review.report.target.key") == "range:main..feature"
        page.select_option("#tab-review select[aria-label='Review target']", "range:main...other")  # a listed branch
        page.wait_for_function(key_is, arg="range:main...other", timeout=30_000)
        assert files() == ["app/o.py"] and shown() == "Branch other vs main (since merge base)"
        assert page.locator("#tab-review select[aria-label='Review target'] option[value='__compare__']").count() == 0
        assert [e for e in page.errors if "status of 400" not in e] == []  # type: ignore[attr-defined]
    finally:
        srv.shutdown()


def test_static_report_says_how_to_compare_branches(page, make_repo, tmp_path: Path) -> None:
    from test_review import diverged_repo

    repo = diverged_repo(make_repo)
    report = tmp_path / "branches.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path), extra_reviews=["main...feature"])), encoding="utf-8")
    page.goto(report.as_uri())
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    note = page.inner_text("#tab-review .compare-note")
    assert "repoviz serve" in note and "repoviz report --review main...feature" in note
    assert page.evaluate("repoviz.app.tabs.review.report.target.key") == "range:main...feature"  # precomputed first
    assert page.evaluate("repoviz.app.tabs.review.report.files.map(f => f.path)") == ["app/b.py"]
    assert page.errors == []  # type: ignore[attr-defined]


def test_dependencies_spotlight_on_click(page, make_repo, tmp_path: Path) -> None:
    repo = make_repo({"pkg/__init__.py": "", "pkg/a.py": "from pkg import b\n", "pkg/d.py": "from pkg import b\n",
                      "pkg/b.py": "from pkg import c\n", "pkg/c.py": "X = 1\n", "pkg/e.py": "from pkg import c\n"})
    report = tmp_path / "deps.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.goto(report.as_uri() + "#tab=dependencies")
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=60_000)
    page.select_option("#tab-dependencies .toolbar select >> nth=0", "module")
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=60_000)
    page.wait_for_function("() => document.querySelectorAll('#tab-dependencies g.node[data-node-id]').length >= 5", timeout=30_000)
    node = lambda name: f"#tab-dependencies g.node[data-node-id]:has-text('{name}')"  # noqa: E731
    classes = """() => Object.fromEntries(Array.from(document.querySelectorAll('#tab-dependencies g.node[data-node-id]'))
        .map(g => [g.textContent.trim().split(/\\s+/)[0].replace(/module$/, ''), ['is-focused', 'is-linked-inbound', 'is-linked-outbound', 'is-dimmed']
        .filter(c => g.classList.contains(c)).join(' ')]))"""
    page.click(node("pkg.b"))
    state = page.evaluate(classes)
    assert state == {"pkg.a": "is-linked-inbound", "pkg.d": "is-linked-inbound", "pkg.b": "is-focused",
                     "pkg.c": "is-linked-outbound", "pkg.e": "is-dimmed"}
    edges = """() => Array.from(document.querySelectorAll('#tab-dependencies path.flowchart-link')).map(p => {
        const s = getComputedStyle(p); return [['is-linked-inbound', 'is-linked-outbound', 'is-dimmed'].find(c => p.classList.contains(c)) || '',
        s.strokeDasharray, parseFloat(s.strokeWidth), parseFloat(s.opacity)]; })"""
    by_kind: dict = {}
    for kind, dash, width, opacity in page.evaluate(edges):
        by_kind.setdefault(kind, []).append((dash, width, opacity))
    assert len(by_kind["is-linked-inbound"]) == 2 and len(by_kind["is-linked-outbound"]) == 1 and by_kind["is-dimmed"]
    assert all(dash == "none" and width >= 3 for dash, width, _ in by_kind["is-linked-inbound"])  # solid, thick
    assert all(dash not in ("none", "") and width >= 3 for dash, width, _ in by_kind["is-linked-outbound"])  # dashed, thick
    assert all(opacity < 0.3 for _, _, opacity in by_kind["is-dimmed"])
    note = page.inner_text("#tab-dependencies .spot-note")
    assert "pkg.b" in note and "used by 2" in note and "depends on 1" in note  # said in words, not only drawn
    transform = page.evaluate("document.querySelector('#tab-dependencies .stage').style.transform")
    page.keyboard.press("Escape")
    assert page.locator("#tab-dependencies svg.rv-spotlight").count() == 0 and page.locator("#tab-dependencies .spot-note").is_hidden()
    assert page.evaluate("document.querySelector('#tab-dependencies .stage').style.transform") == transform  # layout kept
    page.click(node("pkg.c"))  # c is used by b and e
    assert "used by 2" in page.inner_text("#tab-dependencies .spot-note")
    page.locator("#tab-dependencies .viewport").click(position={"x": 4, "y": 4})  # the empty background
    assert page.locator("#tab-dependencies .is-dimmed").count() == 0
    assert page.errors == []  # type: ignore[attr-defined]


NODE_OF = "(p) => [...repoviz.app.snapshotIndex.nodes.values()].find(n => n.path === p && n.category === 'module').id"


def _structure_with_hotspots(page) -> None:
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)
    page.check("#tab-structure label.check:has-text('modules / files') input")
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)
    page.check("#tab-structure label.check:has-text('churn hotspots') input")
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)


def test_structure_hotspot_opens_its_code_changes(page, make_repo, tmp_path: Path) -> None:
    from test_changes_activity import hotspot_repo

    repo = hotspot_repo(make_repo)
    report = tmp_path / "hot.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.goto(report.as_uri() + "#tab=structure")
    _structure_with_hotspots(page)
    hot, cold = page.evaluate(NODE_OF, "app/hot.py"), page.evaluate(NODE_OF, "app/cold.py")
    drawer = page.locator("#tab-structure .changes-drawer")
    assert drawer.is_hidden()
    page.click(f"#tab-structure g.node[data-node-id='{hot}']")
    page.wait_for_function("() => document.querySelector('#tab-structure .changes-drawer table.diff')", timeout=10_000)
    assert "app/hot.py" in drawer.inner_text() and "+1 −1" in drawer.inner_text() and "tune rate 5" in drawer.inner_text()
    marks = page.evaluate("Array.from(document.querySelectorAll('#tab-structure .changes-drawer tr.add td.mk, #tab-structure .changes-drawer tr.del td.mk')).map(td => td.textContent)")
    assert sorted(marks) == ["+", "−"]  # explicit markers, not only colour
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in drawer.inner_text()
    assert "Other changes need the live app" in drawer.inner_text()  # a report has the latest change only
    transform = page.evaluate("document.querySelector('#tab-structure .stage').style.transform")
    page.keyboard.press("Escape")
    assert drawer.is_hidden()
    assert page.evaluate("document.querySelector('#tab-structure .stage').style.transform") == transform  # zoom kept
    assert page.evaluate(f"document.querySelector(\"#tab-structure g.node[data-node-id='{hot}']\").classList.contains('rv-selected')")
    assert page.evaluate("document.activeElement.dataset.nodeId") == hot  # focus back on the graph
    page.click(f"#tab-structure g.node[data-node-id='{cold}']")  # not a hotspot: its metadata, no drawer
    assert drawer.is_hidden() and "cold" in page.inner_text("#tab-structure .split > .card")
    assert page.locator("#tab-structure .split > .card >> text=Show code changes").count() == 0  # not in the report
    assert page.errors == []  # type: ignore[attr-defined]


def test_structure_code_changes_in_the_live_app(page, make_repo) -> None:
    from test_changes_activity import hotspot_repo

    repo = hotspot_repo(make_repo)
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/#tab=structure")
        _structure_with_hotspots(page)
        cold = page.evaluate(NODE_OF, "app/cold.py")
        page.click(f"#tab-structure g.node[data-node-id='{cold}']")
        page.click("#tab-structure .split > .card >> text=Show code changes")  # any file, on request
        page.wait_for_function("() => document.querySelector('#tab-structure .changes-drawer .drawer-commits')", timeout=10_000)
        assert "app/cold.py" in page.inner_text("#tab-structure .changes-drawer")
        hot = page.evaluate(NODE_OF, "app/hot.py")
        page.click(f"#tab-structure g.node[data-node-id='{hot}']")
        page.wait_for_function("() => document.querySelector('#tab-structure .changes-drawer').textContent.includes('tune rate 5')", timeout=10_000)
        page.click("#tab-structure .changes-drawer .drawer-commits button >> nth=3")  # an older commit
        page.wait_for_function("() => document.querySelector('#tab-structure .changes-drawer').textContent.includes('tune rate 2')", timeout=10_000)
        assert page.evaluate("Array.from(document.querySelectorAll('#tab-structure .changes-drawer tr.add td.code')).map(td => td.textContent)") == ["    return 2"]
        page.click("#tab-structure .changes-drawer button[aria-label='Close code changes (Esc)']")
        assert page.locator("#tab-structure .changes-drawer").is_hidden()
        assert page.errors == []  # type: ignore[attr-defined]
    finally:
        srv.shutdown()


def test_contracts_overlay_in_dependencies_and_structure(page, make_repo, tmp_path: Path) -> None:
    from test_review import LAYERED, LAYERS_TOML

    repo = make_repo(dict(LAYERED, **{".repoviz.toml": LAYERS_TOML}))
    repo.write({"app/models/user.py": "from app.routes import api\n"})
    report = tmp_path / "contracts.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.goto(report.as_uri() + "#tab=dependencies")
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=60_000)
    card = page.inner_text("#tab-dependencies .contracts-card")
    assert "1 failing" in card and "Layered backend" in card and "app.models.user → app.routes.api" in card
    assert "app/models/user.py:1" in card
    page.select_option("#tab-dependencies .toolbar select >> nth=0", "module")
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=60_000)
    page.check("#tab-dependencies label.check:has-text('contracts') input")
    page.wait_for_function("() => document.querySelector('#tab-dependencies .viewport svg') && "
                           "document.querySelector('#tab-dependencies .viewport').textContent.includes('⚠ Layered backend')", timeout=30_000)
    svg = page.inner_text("#tab-dependencies .viewport")
    assert "Layer 1: app.routes" in svg and "Layer 3: app.models" in svg  # the layers, as ordered groups
    styles = page.evaluate("Array.from(document.querySelectorAll('#tab-dependencies path.flowchart-link')).map(p => [getComputedStyle(p).strokeDasharray, parseFloat(getComputedStyle(p).strokeWidth)])")
    assert any(dash not in ("none", "") and width >= 3 for dash, width in styles)  # thick and dashed, not only red
    page.click("#tabbtn-structure")
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)
    assert "Architecture contracts (1)" in page.inner_text("#tab-structure") and "1 new" in page.inner_text("#tab-structure")
    assert page.errors == []  # type: ignore[attr-defined]


def test_pr_comment_diagram_renders_with_mermaid(page, make_repo, tmp_path: Path) -> None:
    """The Mermaid block of the CI pull-request comment is valid for the vendored Mermaid (GitHub renders it)."""
    import re

    from test_review import diverged_repo

    from repoviz.ci import pr_comment
    from repoviz.review import build_review, resolve_target

    repo = diverged_repo(make_repo)
    repo.git("checkout", "-q", "feature")
    repo.write({"app/x<b>\"q\"|y.py": "import json\n", "lib/tool.py": "from app import b\n"})
    repo.commit("markup in names")
    r = Repository(repo.path)
    report = build_review(r, resolve_target(r, "main...feature"))
    diagrams = [re.search(r"```mermaid\n(.*?)\n```", pr_comment(report, max_nodes=n), re.S).group(1) for n in (40, 1)]
    out = tmp_path / "r.html"
    out.write_text(render_static_html(build_bundle(r)), encoding="utf-8")
    page.goto(out.as_uri())
    page.wait_for_function("() => window.mermaid && repoviz.app", timeout=30_000)
    svgs = page.evaluate("""async (texts) => { const out = [];
        for (const [i, t] of texts.entries()) out.push((await window.mermaid.render('ci' + i, t)).svg);
        return out; }""", diagrams)
    assert all("<svg" in s and "Syntax error" not in s for s in svgs)
    assert "more" in diagrams[1] and "more" in svgs[1]
    assert page.errors == []  # type: ignore[attr-defined]


def test_structure_system_view_from_compose(page, make_repo, tmp_path: Path) -> None:
    from test_discovery_manifests import SYSTEM

    repo = make_repo(SYSTEM)
    repo.commit("system")
    report = tmp_path / "system.html"
    html = render_static_html(build_bundle(Repository(repo.path)))
    assert "hunter2secret" not in html and "s3cr3t-value-never-shown" not in html  # environment values never shipped
    report.write_text(html, encoding="utf-8")
    page.goto(report.as_uri() + "#tab=structure")
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)
    tab = page.locator("#tab-structure")
    assert tab.locator(".toolbar select >> nth=0").input_value() == "system"  # the first view when services exist
    assert "System: 5 services" in tab.locator(".diagram-head .title").inner_text()
    svg = tab.locator(".viewport svg")
    text = page.evaluate("document.querySelector('#tab-structure .viewport svg').textContent")
    assert "Infrastructure" in text and "app.main" in text and "app.worker" in text
    assert "talks to · mongodb:27017" in text and "starts after" in text and "shares volume · uploads" in text
    legend = tab.locator(".legend").inner_text()
    assert "talks to" in legend and "starts after" in legend and "database" in legend and "cache" in legend
    assert tab.locator(".legend i.rvi-database").count() == 1  # an icon per kind, never colour alone
    assert svg.locator("i.rvi-database").count() == 1 and svg.locator("i.rvi-zap").count() == 1
    # the browser and the CLI (render/views.py) build the same graph
    from repoviz.render import views

    py = views.system_view(Repository(repo.path).snapshot("WORKTREE"))
    js = page.evaluate("(() => { const v = repoviz.systemView(repoviz.app.snapshotIndex, {}); return {nodes: v.nodes.map(n => [n.id, n.label, n.sublabel, n.kind, n.parent]), edges: v.edges.map(e => [e.source, e.target, e.relationship, e.label || ''])}; })()")
    assert js["nodes"] == [[n.id, n.label, n.sublabel, n.kind, n.parent] for n in py.nodes]
    assert js["edges"] == [[e.source, e.target, e.relationship, e.label] for e in py.edges]
    # a service shows its variants and what differs between them
    api = page.evaluate("[...repoviz.app.snapshotIndex.nodes.values()].find(n => n.component_type === 'service' && n.name === 'api').id")
    page.click(f"#tab-structure g.node[data-node-id='{api}']")
    card = tab.locator(".service-card")
    assert "variants" in card.inner_text() and "base" in card.inner_text() and "prod" in card.inner_text()
    assert "80:8000" in card.inner_text() and "names only" in card.inner_text()
    # the code inside a service box leads to the files view
    copy = page.evaluate("[...document.querySelectorAll('#tab-structure g.node')].map(g => g.dataset.nodeId).find(id => id.startsWith('c_'))")
    page.dblclick(f"#tab-structure g.node[data-node-id='{copy}']")
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=30_000)
    assert tab.locator(".toolbar select >> nth=0").input_value() == "files"
    assert tab.locator(".crumbs").is_visible() and "Structure of" in tab.locator(".diagram-head .title").inner_text()
    entry = tab.locator(".card", has=page.locator("h3", has_text="Entry points")).inner_text()
    assert "api (compose)" in entry and "redis-server" not in entry
    assert page.errors == []  # type: ignore[attr-defined]


def test_submodules_as_groups_in_structure(page, make_repo, tmp_path: Path) -> None:
    from test_large_repo_fixes import _git

    lib = make_repo({"src/engine.py": "def run():\n    return 1\n", "src/util.py": "from src.engine import run\n"})
    main = make_repo({"app/__init__.py": "", "app/main.py": "print('hi')\n"})
    _git(main.path, "submodule", "add", "-q", lib.path, "modules/engine")
    _git(main.path, "commit", "-qm", "add engine")
    Path(main.path, "modules/engine/src/util.py").write_text("from src.engine import run\nrun()\n")  # uncommitted
    report = tmp_path / "subs.html"
    report.write_text(render_static_html(build_bundle(Repository(main.path))), encoding="utf-8")
    page.goto(report.as_uri() + "#tab=structure")
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)
    chip = page.locator("#repo-info [data-chip=submodules]")
    assert chip.inner_text().strip() == "1 submodule (1 modified)"
    sub = page.evaluate("[...repoviz.app.snapshotIndex.nodes.values()].find(n => n.component_type === 'submodule').id")
    label = page.evaluate(f"document.querySelector(\"#tab-structure g.node[data-node-id='{sub}']\").textContent")
    assert "submodule · @" in label and "2 files" in label and "Python" in label and "✎ 1 uncommitted" in label
    page.dblclick(f"#tab-structure g.node[data-node-id='{sub}']")  # a group: drill into its code
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=30_000)
    assert page.evaluate("repoviz.app.tabs.structure.opts.root") == sub
    assert "src" in page.evaluate("document.querySelector('#tab-structure .viewport svg').textContent")
    page.click("#tabbtn-review")
    chip.click()
    card = page.locator("#submodules-card")
    page.wait_for_function("() => document.querySelector('#tab-structure').offsetParent !== null", timeout=10_000)
    assert card.is_visible() and "modules/engine" in card.inner_text() and "analyzed" in card.inner_text()
    assert "✎ 1 uncommitted" in card.inner_text()
    assert page.errors == []  # type: ignore[attr-defined]


def test_dependencies_default_and_header_counts(page, make_repo, tmp_path: Path) -> None:
    from test_outputs import TWO_PACKAGES

    repo = make_repo(TWO_PACKAGES)
    bundle = build_bundle(Repository(repo.path))
    report = tmp_path / "counts.html"
    report.write_text(render_static_html(bundle), encoding="utf-8")
    page.goto(report.as_uri() + "#tab=dependencies")
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=60_000)
    tab = page.locator("#tab-dependencies")
    assert "package level (auto)" in tab.locator(".diagram-head .title").inner_text()
    note = tab.locator(".notice.level-note")
    assert note.is_visible() and "Showing packages because the code has only 2 components" in note.inner_text()
    names = page.evaluate("Array.from(document.querySelectorAll('#tab-dependencies g.node')).map(g => g.textContent)")
    assert any("app.core" in n for n in names) and not any("redis" in n or "service" in n for n in names)  # services hidden
    page.check("#tab-dependencies label.check:has-text('services') input")
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=30_000)
    assert "api" in page.evaluate("document.querySelector('#tab-dependencies .viewport svg').textContent")
    note.locator("a").click()  # "Switch to components"
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=30_000)
    assert tab.locator(".toolbar select >> nth=0").input_value() == "component" and note.is_hidden()
    # the header: separate counts, each with an icon and words, matching `repoviz discover`
    chips = {c.get_attribute("data-chip"): c for c in page.locator("#repo-info [data-chip]").all()}
    assert set(chips) == {"code-components", "services", "external-packages", "entry-points"}  # zero counts hidden
    assert chips["code-components"].inner_text().strip() == "2 code components"
    assert chips["services"].inner_text().strip() == "5 services (2 first-party)"
    assert chips["entry-points"].inner_text().strip() == f"{bundle['breakdown']['entry_points']} entry points"
    assert all(c.locator("i.rvi").count() == 1 for c in chips.values())
    chips["services"].click()
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=30_000)
    assert page.locator("#tab-structure .toolbar select >> nth=0").input_value() == "system"
    assert page.errors == []  # type: ignore[attr-defined]


def test_a_stored_dependencies_level_wins(page, make_repo, tmp_path: Path) -> None:
    from test_outputs import TWO_PACKAGES

    repo = make_repo(TWO_PACKAGES)
    report = tmp_path / "stored.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.add_init_script("localStorage.setItem('rv.deps', JSON.stringify({level: 'component'}))")
    page.goto(report.as_uri() + "#tab=dependencies")
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=60_000)
    assert "component level" in page.locator("#tab-dependencies .diagram-head .title").inner_text()
    assert page.locator("#tab-dependencies .notice.level-note").is_hidden()
    assert page.errors == []  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- Readability at scale (#27)

SCALE = "(tab) => parseFloat((document.querySelector('#tab-' + tab + ' .stage').style.transform.match(/scale\\(([\\d.]+)\\)/) || [0, 0])[1])"
LABEL_PX = "(tab) => parseFloat(getComputedStyle(document.querySelector('#tab-' + tab + ' g.node .nodeLabel')).fontSize)"


def _report(make_repo, tmp_path: Path, files: dict[str, str], name: str, change: dict[str, str] | None = None) -> str:
    repo = make_repo(files)
    if change:
        repo.write(change)
    report = tmp_path / f"{name}.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    return report.as_uri()


def test_orientation_from_shape_and_readable_fit(page, make_repo, tmp_path: Path) -> None:
    from test_outputs import branching_tree

    uri = _report(make_repo, tmp_path, branching_tree(), "deep")  # 48 packages, 6 levels deep
    page.add_init_script("localStorage.setItem('rv.structure', JSON.stringify({depth: 8}))")
    page.goto(uri + "#tab=structure")
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)
    assert page.locator("#tab-structure g.node").count() == 48
    assert page.evaluate("repoviz.app.tabs.structure.diagram.view.direction") == "LR"
    # The same rule as views.choose_direction: a long thin chain turns TB, a wide system is stacked LR.
    from repoviz.render import views

    for n, fan, direction in ((12, False, "LR"), (30, True, "TB"), (3, False, "LR")):
        nodes = [views.VNode(f"n{i}", "x") for i in range(n)]
        edges = [views.VEdge("n0" if fan else f"n{i - 1}", f"n{i}") for i in range(1, n)]
        view = views.ViewGraph("t", direction=direction, nodes=nodes, edges=edges)
        js = page.evaluate("(v) => repoviz.chooseDirection(v, 1600, 1000)",
                           {"direction": direction, "nodes": [{"id": x.id} for x in nodes],
                            "edges": [{"source": e.source, "target": e.target} for e in edges]})
        assert js == views.choose_direction(view)
    assert views.choose_direction(views.ViewGraph("chain", nodes=[views.VNode(f"n{i}", "x") for i in range(12)],
                                                  edges=[views.VEdge(f"n{i}", f"n{i + 1}") for i in range(11)])) == "TB"
    button = page.locator("#tab-structure .diagram-head button:has-text('auto')")
    assert button.inner_text() == "⇄ auto" and "automatic (left to right" in button.get_attribute("title")
    # Fit never goes below the zoom at which labels read at 11px.
    assert page.evaluate(LABEL_PX, "structure") * page.evaluate(SCALE, "structure") >= 11 - 0.01
    page.locator("#tab-structure button[title='Fit to view']").click()
    assert page.evaluate(LABEL_PX, "structure") * page.evaluate(SCALE, "structure") >= 11 - 0.01
    # The toggle overrides the choice (auto → LR → TB → auto), and the choice is remembered per view.
    button.click()
    toggle = page.locator("#tab-structure .diagram-head button[title^='Layout']")
    page.wait_for_function("() => document.querySelector(\"#tab-structure .diagram-head button[title^='Layout']\").textContent === '⇄'", timeout=30_000)
    assert page.evaluate("localStorage.getItem('rv.orient.structure')") == '"LR"'
    toggle.click()
    page.wait_for_function("() => repoviz.app.tabs.structure.diagram.view.direction === 'TB'", timeout=30_000)
    assert toggle.inner_text() == "⇅" and page.evaluate("localStorage.getItem('rv.orient.structure')") == '"TB"'
    assert page.evaluate("document.querySelector('#tab-structure .diagram-card').textContent").count("flowchart TB") == 1
    page.reload()
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)
    assert page.evaluate("repoviz.app.tabs.structure.diagram.view.direction") == "TB"
    assert page.errors == []  # type: ignore[attr-defined]


def test_long_leaf_lists_fold_and_expand(page, make_repo, tmp_path: Path) -> None:
    from test_outputs import TESTS_27

    uri = _report(make_repo, tmp_path, TESTS_27, "fold", change={"pkg/tests/test_05.py": "def test_y():\n    pass\n"})
    page.add_init_script("localStorage.setItem('rv.structure', JSON.stringify({depth: 4, files: true}))")
    page.goto(uri + "#tab=structure")
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)
    texts = page.evaluate("Array.from(document.querySelectorAll('#tab-structure g.node')).map(g => g.textContent)")
    folds = [t for t in texts if "test files" in t]
    assert len(folds) == 1 and "+ 26 test files" in folds[0] and "click to expand" in folds[0]
    assert [t for t in texts if "test_" in t] == [t for t in texts if "test_05" in t]  # the changed one stays out
    fold = page.locator("#tab-structure g.node:has-text('+ 26 test files')")
    assert fold.locator("title").count() == 1  # full text in the tooltip
    fold.click()
    page.wait_for_function("() => Array.from(document.querySelectorAll('#tab-structure g.node')).filter(g => g.textContent.includes('test_')).length === 27", timeout=30_000)
    assert page.locator("#tab-structure g.node:has-text('test files')").count() == 0
    # A search match comes out of a fold.
    page.reload()
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)
    page.fill("#tab-structure input[type=search]", "test_17")
    page.wait_for_function("() => Array.from(document.querySelectorAll('#tab-structure g.node')).some(g => g.textContent.includes('test_17'))", timeout=30_000)
    assert page.locator("#tab-structure g.node:has-text('+ 25 test files')").count() == 1
    assert page.errors == []  # type: ignore[attr-defined]


def test_old_cycles_are_faint_and_new_ones_strong(page, make_repo, tmp_path: Path) -> None:
    from test_outputs import CYCLE_27

    page.add_init_script("localStorage.setItem('rv.changes', JSON.stringify({level: 'module', scope: 'all'}))")
    page.goto(_report(make_repo, tmp_path, CYCLE_27, "old", change={"app/other.py": "from app import util\n"}) + "#tab=changes")
    page.wait_for_function(ALL_RENDERED, arg="changes", timeout=60_000)
    assert page.locator("#tab-changes path.cycle-existing").count() == 2  # and their labels
    assert page.locator("#tab-changes .edgeLabel.cycle-existing").count() == 2
    assert page.locator("#tab-changes .viewport .cycle-new, #tab-changes .viewport .cycle-kept").count() == 0
    assert "existing cycle" in page.evaluate("document.querySelector('#tab-changes .viewport svg').textContent")
    legend = page.locator("#tab-changes .legend").inner_text()
    assert "existing cycle" in legend and "new cycle" in legend
    fresh = {k: v for k, v in CYCLE_27.items() if k != "app/tasks.py"} | {"app/tasks.py": "X = 1\n"}
    page.goto(_report(make_repo, tmp_path, fresh, "new", change={"app/tasks.py": "from app import routes\n"}) + "#tab=changes")
    page.wait_for_function(ALL_RENDERED, arg="changes", timeout=60_000)
    assert page.locator("#tab-changes path.cycle-new").count() == 2
    assert page.locator("#tab-changes .viewport .cycle-existing").count() == 0
    assert page.errors == []  # type: ignore[attr-defined]


def test_minimap_for_large_diagrams_only(page, make_repo, tmp_path: Path) -> None:
    from test_outputs import branching_tree

    page.add_init_script("localStorage.setItem('rv.structure', JSON.stringify({depth: 8}))")
    page.goto(_report(make_repo, tmp_path, branching_tree((1, 2)), "small") + "#tab=structure")
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)
    assert page.locator("#tab-structure .minimap").count() == 0
    page.goto(_report(make_repo, tmp_path, branching_tree((3, 3, 3, 3)), "big") + "#tab=structure")
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)
    mini = page.locator("#tab-structure .minimap")
    assert mini.count() == 1 and mini.locator("rect.mini-node").count() > 100
    before = page.evaluate("document.querySelector('#tab-structure .stage').style.transform")
    view_before = mini.locator("rect.mini-view").get_attribute("y")
    box = mini.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] - 3)  # the bottom of the diagram
    after = page.evaluate("document.querySelector('#tab-structure .stage').style.transform")
    assert after != before and mini.locator("rect.mini-view").get_attribute("y") != view_before
    assert page.errors == []  # type: ignore[attr-defined]


def test_system_view_orientation_is_its_own(page, make_repo, tmp_path: Path) -> None:
    from test_discovery_manifests import SYSTEM

    page.goto(_report(make_repo, tmp_path, SYSTEM, "system") + "#tab=structure")
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=60_000)
    diagram = "repoviz.app.tabs.structure.diagram"
    assert page.evaluate(f"{diagram}.view.title") == "System"
    assert page.evaluate(f"{diagram}.view.direction") == "TB"  # a few services: side by side
    toggle = page.locator("#tab-structure .diagram-head button[title^='Layout']")
    assert toggle.inner_text() == "⇅ auto"
    toggle.click()
    page.wait_for_function(f"() => {diagram}.view.direction === 'LR'", timeout=30_000)
    assert page.evaluate("localStorage.getItem('rv.orient.system')") == '"LR"'
    page.select_option("#tab-structure .toolbar select >> nth=0", "files")
    page.wait_for_function(f"() => {diagram}.view.title === 'Structure'", timeout=30_000)
    assert toggle.inner_text().endswith("auto") and page.evaluate("localStorage.getItem('rv.orient.structure')") is None
    assert page.errors == []  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- Lists that scale (#29)

FILES_LIST = ("#tab-review .files-split > .card:not(.file-card)")
VISIBLE_FILES = "() => repoviz.app.tabs.review.fileOrder"


def fifty_files(make_repo, tmp_path: Path, name: str = "fifty") -> str:
    """50 changed files in 5 components (c0…c4), uncommitted; returns the report's URI."""
    files = {f"c{c}/__init__.py": "" for c in range(5)}
    files.update({f"c{c}/mod{m}.py": f"X = {m}\n" for c in range(5) for m in range(10)})
    repo = make_repo(files)
    repo.write({f"c{c}/mod{m}.py": f"X = {m}\n\ndef f{m}():\n    return {m}\n" for c in range(5) for m in range(10)})
    report = tmp_path / f"{name}.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    return report.as_uri()


def test_review_files_group_collapse_and_keys(page, make_repo, tmp_path: Path) -> None:
    page.goto(fifty_files(make_repo, tmp_path) + "#tab=review")
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    card = page.locator(FILES_LIST)
    groups = card.locator("tr.group-row")
    assert groups.count() == 5 and card.locator("tr.in-group").count() == 50  # grouped by default at this size
    first = groups.nth(0).inner_text()
    assert "10 files · +30 −0" in first and "medium" in first
    headers = [h.strip() for h in card.locator("thead th").all_inner_texts()]
    assert headers[:5] == ["✓", "SIGNALS", "RISK", "FILE", "CHANGE"]  # signals and risk first
    assert card.locator("tr.in-group td:nth-child(4) span[title='c0/mod0.py']").inner_text() == "…/c0/mod0.py"
    order = page.evaluate(VISIBLE_FILES)
    assert len(order) == 50 and [p.split("/")[0] for p in order[::10]] == ["c0", "c1", "c2", "c3", "c4"]
    # Collapse the second group: its rows go, and j jumps from the first group to the third.
    groups.nth(1).locator("button.group-toggle").click()
    assert card.locator("tr.in-group").count() == 40 and len(page.evaluate(VISIBLE_FILES)) == 40
    assert groups.nth(1).locator("button.group-toggle").get_attribute("aria-expanded") == "false"
    page.evaluate("repoviz.app.tabs.review.selectFile(repoviz.app.tabs.review.fileOrder[9])")  # the last of c0
    page.locator("body").press("j")
    assert page.evaluate("repoviz.app.tabs.review.selectedFile").startswith("c2/")
    # o collapses the current file's group (and expands it again); j carries on after the group.
    page.locator("body").press("o")
    assert card.locator("tr.in-group").count() == 30
    page.locator("body").press("j")
    assert page.evaluate("repoviz.app.tabs.review.selectedFile").startswith("c3/")
    page.locator("body").press("k")
    assert page.evaluate("repoviz.app.tabs.review.selectedFile").startswith("c0/")  # c1 and c2 are collapsed
    # Remembered after a reload; the switch turns grouping off.
    page.reload()
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    assert card.locator("tr.in-group").count() == 30
    card.locator("label.check:has-text('group by component') input").uncheck()
    assert card.locator("tr.group-row").count() == 0 and len(page.evaluate(VISIBLE_FILES)) == 50
    assert card.locator("td:nth-child(4) span[title='c0/mod0.py']").inner_text() == "c0/mod0.py"  # the full path
    assert page.errors == []  # type: ignore[attr-defined]


def test_review_files_search_and_component_filter(page, make_repo, tmp_path: Path) -> None:
    page.goto(fifty_files(make_repo, tmp_path) + "#tab=review")
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    card = page.locator(FILES_LIST)
    page.locator("body").press("/")  # focuses the search box
    assert page.evaluate("document.activeElement.classList.contains('list-search')")
    page.keyboard.type("mod3")
    page.wait_for_function("() => repoviz.app.tabs.review.fileOrder.length === 5", timeout=5_000)
    assert card.locator(".list-count").inner_text() == "5 of 50 files"
    chips = card.locator(".facets button.facet")
    assert chips.count() == 5 and chips.nth(0).inner_text().replace("\n", "") in {f"c{i} 1" for i in range(5)}
    chips.filter(has_text="c2").click()  # the filter combines with the search
    assert page.evaluate(VISIBLE_FILES) == ["c2/mod3.py"] and chips.filter(has_text="c2").get_attribute("aria-pressed") == "true"
    assert "✓" in chips.filter(has_text="c2").inner_text()  # not colour alone
    page.reload()  # both are remembered
    page.wait_for_function(ALL_RENDERED, arg="review", timeout=60_000)
    assert card.locator("input.list-search").input_value() == "mod3" and page.evaluate(VISIBLE_FILES) == ["c2/mod3.py"]
    card.locator("button:has-text('Clear filter')").click()
    card.locator("input.list-search").fill("")
    page.wait_for_function("() => repoviz.app.tabs.review.fileOrder.length === 50", timeout=5_000)
    card.locator("input.list-search").fill("no such file")
    page.wait_for_function("() => repoviz.app.tabs.review.fileOrder.length === 0", timeout=5_000)
    assert "Nothing matches the search or filter." in card.inner_text()
    assert page.errors == []  # type: ignore[attr-defined]


def test_changed_nodes_rollups_relevance_and_search(page, make_repo, tmp_path: Path) -> None:
    page.goto(fifty_files(make_repo, tmp_path) + "#tab=changes")
    page.wait_for_function(ALL_RENDERED, arg="changes", timeout=60_000)
    card = page.locator("#tab-changes .card", has=page.locator("h3", has_text="Changed nodes"))
    rollups = card.locator("label.check:has-text('show folder rollups')")
    assert rollups.inner_text().strip() == "show folder rollups (6)"  # the 5 packages and the repository
    assert card.locator(".list-count").inner_text() == "100 of 106 nodes"
    why = "() => Array.from(document.querySelectorAll('#tab-changes tbody tr')).filter(r => r.cells.length > 5 && r.cells[5].textContent === 'contents changed').length"
    assert page.evaluate(why) == 0
    # Relevance: in each group, the new functions (API) come before the modules whose body changed.
    rows = card.locator("tbody tr.in-group").all_inner_texts()
    assert "added" in rows[0] and "modified" in rows[-1]
    rollups.locator("input").check()
    assert card.locator(".list-count").inner_text() == "106 nodes" and page.evaluate(why) == 6
    card.locator("input.list-search").fill("mod7.f7")  # name, path or reason
    page.wait_for_function("() => document.querySelector('#tab-changes .list-count').textContent === '5 of 106 nodes'", timeout=5_000)
    card.locator("input.list-search").fill("c3.mod7.f7")
    page.wait_for_function("() => document.querySelector('#tab-changes .list-count').textContent === '1 of 106 nodes'", timeout=5_000)
    assert "c3.mod7.f7" in card.locator("tbody").inner_text()
    assert page.errors == []  # type: ignore[attr-defined]


def test_lists_fit_a_phone_screen(browser, make_repo, tmp_path: Path) -> None:
    uri = fifty_files(make_repo, tmp_path)
    for tab in ("review", "changes"):
        pg = browser.new_page(viewport={"width": 390, "height": 800})
        try:
            pg.goto(uri + f"#tab={tab}")
            pg.wait_for_function(ALL_RENDERED, arg=tab, timeout=60_000)
            assert pg.evaluate("document.documentElement.scrollWidth") <= 390, tab
            if tab == "review":  # long paths are shortened in the middle, the full path in the tooltip
                long = "a" * 30 + "/" + "b" * 30 + "/component_file_name.py"
                cell = "(p) => { const c = repoviz.app.tabs.review.fileCell({path: p}, false); return [c.textContent, c.title]; }"
                text, title = pg.evaluate(cell, long)
                assert text.count("…") == 1 and text.endswith("file_name.py") and title == long
        finally:
            pg.close()


# --------------------------------------------------------------------------- Why and blast radius (#28)

NODE_NAMED = "(q) => [...repoviz.app.snapshotIndex.nodes.values()].filter(n => n.qualified_name === q).sort((a, b) => (a.category === 'component' ? 0 : 1) - (b.category === 'component' ? 0 : 1))[0].id"
EDGE_EVENT = """([a, b, kind]) => { const el = document.querySelector(`#tab-dependencies path.flowchart-link[data-id^="L_${a}_${b}_"]`);
  el.dispatchEvent(new MouseEvent(kind, { bubbles: true, cancelable: true, button: kind === 'contextmenu' ? 2 : 0 })); }"""


def _chain_report(make_repo, tmp_path: Path):
    from test_query import CHAIN

    repo = make_repo(CHAIN)
    report = tmp_path / "chain.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    return report.as_uri(), repo


def test_why_panel_and_chain_highlight(page, make_repo, tmp_path: Path) -> None:
    from repoviz.query import why

    uri, repo = _chain_report(make_repo, tmp_path)
    page.goto(uri + "#tab=dependencies")
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=60_000)
    ui, services, db = (page.evaluate(NODE_NAMED, x) for x in ("ui", "services", "db"))
    page.evaluate(EDGE_EVENT, [services, db, "contextmenu"])  # right-click an edge
    side = page.locator("#tab-dependencies .split > .card")
    page.wait_for_function("() => document.querySelector('#tab-dependencies .split > .card').textContent.includes('Why does services depend on db?')", timeout=10_000)
    assert "services.orders imports db.models" in side.inner_text() and "services/orders.py:1" in side.inner_text()
    assert "from db import models" in side.inner_text()
    assert page.locator("#tab-dependencies g.node.is-chain").count() == 2 and page.locator("#tab-dependencies path.is-chain").count() == 1
    note = page.locator("#tab-dependencies .spot-note")
    assert "Chain 1 of 1: services.orders → db.models" in note.inner_text() and "outlined" in note.inner_text()  # not colour alone
    page.keyboard.press("Escape")
    assert page.locator("#tab-dependencies .is-chain").count() == 0
    # click an edge, then w; a longer chain across three components
    page.evaluate(EDGE_EVENT, [ui, services, "click"])
    assert "Why does ui depend on services?" in side.inner_text()  # the button on the edge details
    page.locator("body").press("w")
    page.wait_for_function("() => document.querySelector('#tab-dependencies .spot-note').textContent.includes('Chain 1')", timeout=10_000)
    page.evaluate("([a, b]) => repoviz.app.tabs.dependencies.showWhy(a, b)", [ui, db])
    page.wait_for_function("() => document.querySelectorAll('#tab-dependencies g.node.is-chain').length === 3", timeout=10_000)
    assert page.locator("#tab-dependencies path.is-chain").count() == 2
    assert "ui.forms → services.orders → db.models" in side.inner_text()
    # the page computes the same chains as the engine, both ways round
    idx = Repository(repo.path).graph_index()
    for a, b in ((ui, db), (db, ui), (page.evaluate(NODE_NAMED, "ui.views.page"), page.evaluate(NODE_NAMED, "db.models.query"))):
        js = page.evaluate("([a, b]) => repoviz.whyPaths(repoviz.app.snapshotIndex, a, b)", [a, b])
        py = why(idx, idx.nodes[a], idx.nodes[b])
        assert js["summary"] == py["summary"] and js["level"] == py["level"]
        for key in ("paths", "reverse_paths"):
            assert [[(x["id"], x.get("evidence")) for x in p] for p in js.get(key, [])] == \
                [[(x["id"], x.get("evidence")) for x in p] for p in py.get(key, [])]
    assert page.errors == []  # type: ignore[attr-defined]


def test_blast_radius_view(page, make_repo, tmp_path: Path) -> None:
    from test_query import CHAIN

    from repoviz.query import blast_radius

    repo = make_repo(CHAIN)
    report = tmp_path / "blast.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.goto(report.as_uri() + "#tab=dependencies")
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=60_000)
    db = page.evaluate(NODE_NAMED, "db")
    page.click(f"#tab-dependencies g.node[data-node-id='{db}']")
    page.locator("body").press("b")  # blast radius of the selected node
    page.wait_for_function("() => repoviz.app.tabs.dependencies.diagram.view.title === 'Blast radius'", timeout=10_000)
    page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=30_000)
    note = page.locator("#tab-dependencies .blast-note")
    assert note.is_visible() and "Changing db can affect 4 modules in 3 components, 1 entry point, 2 tests." in note.inner_text()
    texts = page.evaluate("Array.from(document.querySelectorAll('#tab-dependencies g.node')).map(g => g.textContent)")
    assert any("ring 1" in t for t in texts) and any("ring 2" in t for t in texts) and any("ring 3+" in t for t in texts)
    assert page.locator("#tab-dependencies g.node i.rvi-play").count() >= 1 and page.locator("#tab-dependencies g.node i.rvi-flask").count() >= 1
    assert "ring 1: uses it directly" in page.locator("#tab-dependencies .legend").inner_text()
    # the page's answer is the engine's
    idx = Repository(repo.path).graph_index()
    for name in ("db", "db.models.query", "services.orders"):
        nid = page.evaluate(NODE_NAMED, name)
        js = page.evaluate("(id) => repoviz.blastRadius(repoviz.app.snapshotIndex, id, null, 200)", nid)
        py = blast_radius(idx, idx.nodes[nid], max_items=200)
        assert js["totals"] == py["totals"] and js["summary"] == py["summary"], name
        assert [(d["id"], d["distance"]) for d in js["dependents"]] == [(d["id"], d["distance"]) for d in py["dependents"]]
    # from the Structure tab's details, and back
    page.locator("#tab-dependencies button:has-text('Back to dependencies')").first.click()
    page.wait_for_function("() => repoviz.app.tabs.dependencies.diagram.view.title !== 'Blast radius'", timeout=10_000)
    assert page.locator("#tab-dependencies .blast-note").is_hidden()
    query = page.evaluate(NODE_NAMED, "db.models.query")
    page.evaluate("(id) => { repoviz.app.show('structure'); }", None)
    page.wait_for_function(ALL_RENDERED, arg="structure", timeout=30_000)
    page.evaluate("(id) => repoviz.app.tabs.structure.details.showNode(repoviz.app.snapshotIndex, id)", query)
    page.locator("#tab-structure button:has-text('Blast radius')").click()
    page.wait_for_function("() => repoviz.app.currentTab === 'dependencies' && repoviz.app.tabs.dependencies.diagram.view.title === 'Blast radius'", timeout=10_000)
    assert "Changing db.models.query can affect" in page.locator("#tab-dependencies .blast-note").inner_text()
    assert page.errors == []  # type: ignore[attr-defined]


def test_why_and_blast_in_the_live_app(page, make_repo) -> None:
    from test_query import CHAIN

    repo = make_repo(CHAIN)
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/#tab=dependencies")
        page.wait_for_function(ALL_RENDERED, arg="dependencies", timeout=60_000)
        assert page.evaluate("repoviz.app.api.live") is True
        services, db = page.evaluate(NODE_NAMED, "services"), page.evaluate(NODE_NAMED, "db")
        page.evaluate(EDGE_EVENT, [services, db, "contextmenu"])
        page.wait_for_function("() => document.querySelector('#tab-dependencies .split > .card').textContent.includes('services.orders imports db.models')", timeout=10_000)
        page.evaluate("(id) => repoviz.app.tabs.dependencies.showBlast(id)", page.evaluate(NODE_NAMED, "db.models.query"))
        page.wait_for_function("() => document.querySelector('#tab-dependencies .blast-note') && document.querySelector('#tab-dependencies .blast-note').textContent.includes('1 entry point, 1 test')", timeout=10_000)
        assert page.errors == []  # type: ignore[attr-defined]
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------------------- History comparisons (#31)

def test_changes_opens_on_history_when_the_checkout_is_clean(page, make_repo, tmp_path: Path) -> None:
    from test_changes_activity import merge_history_repo

    repo = merge_history_repo(make_repo)
    report = tmp_path / "clean.html"
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.goto(report.as_uri() + "#tab=changes")
    page.wait_for_function(ALL_RENDERED, arg="changes", timeout=60_000)
    note = page.locator("#tab-changes .clean-note")
    assert note.is_visible() and "Working tree is clean, showing the last commit instead" in note.inner_text()
    assert page.locator("#tab-changes g.node").count() > 0  # not an empty diagram
    groups = page.evaluate("[...document.querySelectorAll('#tab-changes select optgroup')].map(g => [g.label, [...g.children].map(o => o.textContent)])")
    assert groups[0][0] == "Uncommitted" and groups[0][1][0].endswith("(no changes)")
    assert groups[1] == ["History", ["Last commit: Merge feature (2 files)"]]
    note.locator("a").click()  # "Choose another comparison" focuses the picker
    assert page.evaluate("document.activeElement.tagName") == "SELECT"
    # with uncommitted work, the tab opens on it, as before
    repo.write({"app/core.py": "X = 2\n"})
    report.write_text(render_static_html(build_bundle(Repository(repo.path))), encoding="utf-8")
    page.reload()
    page.wait_for_function(ALL_RENDERED, arg="changes", timeout=60_000)
    assert page.locator("#tab-changes .clean-note").is_hidden()
    assert page.evaluate("document.querySelector('#tab-changes select').selectedOptions[0].textContent").startswith("HEAD vs working tree")
    assert page.errors == []  # type: ignore[attr-defined]


def test_live_changes_offers_history_with_sizes(page, make_repo) -> None:
    from test_changes_activity import merge_history_repo

    repo = merge_history_repo(make_repo)
    srv = create_server(Repository(repo.path), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/#tab=changes")
        page.wait_for_function(ALL_RENDERED, arg="changes", timeout=60_000)
        # the last commit is the merge: offered once, as the last commit
        assert "showing the last commit instead" in page.locator("#tab-changes .clean-note").inner_text()
        assert page.evaluate("document.querySelector('#tab-changes select').value") == "last-commit"
        texts = page.evaluate("[...document.querySelectorAll('#tab-changes select option')].map(o => o.textContent)")
        assert "Last commit: Merge feature (2 files)" in texts and "Since a tag or date…" in texts
        assert not any(t.startswith("Last merge") for t in texts)
        page.select_option("#tab-changes select >> nth=0", "since")
        page.fill("#tab-changes input[aria-label^='Since']", "v0.1.0")
        page.click("#tab-changes button:has-text('Compare')")
        page.wait_for_function("() => document.querySelector('#tab-changes .toolbar .muted').textContent.startsWith('v0.1.0')", timeout=30_000)
        assert page.locator("#tab-changes g.node").count() > 0
        assert page.errors == []  # type: ignore[attr-defined]
    finally:
        srv.shutdown()
        srv.server_close()
