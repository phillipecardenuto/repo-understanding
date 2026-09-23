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
