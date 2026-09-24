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
