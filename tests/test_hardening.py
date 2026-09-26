"""Security and robustness guarantees: untrusted repositories, pathological input, private state."""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from repoviz import globs
from repoviz.activity import observe
from repoviz.manifests import parse_msbuild_project, parse_pom
from repoviz.redact import contains_secret, redact
from repoviz.render.html import build_bundle, render_static_html
from repoviz.repo import Repository
from repoviz.review import build_review, resolve_target


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX shell commands in git config")
def test_repository_config_cannot_run_commands(make_repo, tmp_path: Path) -> None:
    """A repository's own .git/config must not make analysis execute programs."""
    repo = make_repo({"src/app.py": "import os\n", ".gitattributes": "* filter=evil\n"})
    marker = tmp_path / "PWNED"
    repo.git("config", "core.fsmonitor", f"touch {marker}-fsmonitor; echo")
    repo.git("config", "filter.evil.clean", f"touch {marker}-filter; cat")
    repo.git("config", "filter.evil.required", "true")
    repo.git("config", "log.showSignature", "true")
    repo.git("config", "gpg.program", f"sh -c 'touch {marker}-gpg'")
    # Re-write a tracked file so its stat data no longer matches the index (what triggers clean filters).
    time.sleep(1.1)
    Path(repo.path, "src/app.py").write_text("import os\nimport sys\n")
    r = Repository(repo.path)
    r.git.status()
    comp, diff = r.compare(mode="all")
    assert diff.summary()["nodes"]["modified"] >= 1
    observe(r, record=False)
    build_review(r, resolve_target(r, "all"))
    r.git.recent_commits(5)
    assert not list(tmp_path.glob("PWNED*"))


def test_xml_entity_declarations_are_refused() -> None:
    bomb = ('<?xml version="1.0"?><!DOCTYPE p [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;">]>'
            "<project><artifactId>&b;</artifactId></project>")
    md = parse_pom("pom.xml", bomb, lambda p: False)
    assert md.errors and "entity" in md.errors[0].lower() and not md.name
    md = parse_msbuild_project("a.csproj", bomb.replace("project", "Project"), lambda p: False)
    assert md.errors


def test_globs_stay_linear_and_tolerate_bad_classes() -> None:
    started = time.perf_counter()
    assert not globs.match("a/" * 5000 + "y", "**/**/**/**/**/**/**/**/**/x")
    assert globs.match("a/b/x", "**/**/x") and globs.match("src/a.py", "src/**/**/*.py")
    assert not globs.match("lib/a.py", "src/**/**/*.py")
    assert not globs.match("q", "[z-a]")  # invalid range: treated literally instead of raising
    assert time.perf_counter() - started < 1.0


def test_secret_patterns_stay_linear() -> None:
    started = time.perf_counter()
    contains_secret("token" + "-a" * 50_000)
    redact("tokenx" * 50_000 + " = 'abcdefghij'")
    assert time.perf_counter() - started < 2.0
    assert redact('api_key = "abcdefghijk123"') == 'api_key = "abc…[redacted]"'


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_state_directory_is_private(make_repo) -> None:
    repo = make_repo({"a.py": "x = 1\n"})
    Path(repo.path, "a.py").write_text("x = 2  # dirty before the session\n")
    r = Repository(repo.path)
    session = r.state.start_session(r.git, r.root, "w")
    r.state.save_notes("k", [{"verdict": "ok"}])
    session_dir = r.state.dir / "sessions" / session.id
    assert stat.S_IMODE(os.stat(r.state.dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(session_dir).st_mode) == 0o700
    copies = list((session_dir / "files").iterdir())
    assert copies and all(stat.S_IMODE(os.stat(c).st_mode) == 0o600 for c in copies)
    assert stat.S_IMODE(os.stat(session_dir / "session.json").st_mode) == 0o600
    # Checkpoints keep the same permissions: copies, state files, the timeline, the stat cache and the lock.
    from repoviz.checkpoints import create, note

    Path(repo.path, "a.py").write_text("x = 3\n")
    assert create(r.state, r.git, r.root, session)[1]
    note(r.state, session, message="edited a.py")
    for d in (session_dir / "files", session_dir / "checkpoints"):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    written = [session_dir / n for n in ("timeline.json", "statcache.json", ".lock")]
    written += list((session_dir / "checkpoints").iterdir()) + list((session_dir / "files").iterdir())
    assert all(stat.S_IMODE(os.stat(f).st_mode) == 0o600 for f in written)


def test_static_report_hides_home_directory(make_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo({"a.py": "import os\n"})
    home = str(Path(repo.path).parent)
    monkeypatch.setenv("HOME", home)
    page = render_static_html(build_bundle(Repository(repo.path)), compress=False)
    assert home not in page and "~/" in page



def test_parse_cache_entries_are_never_unpickled_or_run(make_repo, tmp_path) -> None:
    """A cache entry is JSON decoded into dataclasses: a planted pickle (or code in a string) is data, never run."""
    import pickle
    import sqlite3
    import zlib

    from repoviz.diskcache import _key_text

    marker = tmp_path / "pwned"

    class Evil:
        def __reduce__(self):
            return (open, (str(marker), "w"))

    repo = make_repo({"m.py": "def f():\n    return 1\n"})
    r = Repository(repo.path)
    r.snapshot("WORKTREE")
    db = r.file_cache.disk.path
    r.file_cache.disk.close()
    conn = sqlite3.connect(db)
    keys = [k for (k,) in conn.execute("SELECT key FROM entries WHERE ns = 'python'")]
    assert keys
    conn.execute("UPDATE entries SET payload = ? WHERE key = ?", (zlib.compress(pickle.dumps(Evil())), keys[0]))
    code = '{"error": "__import__(\'os\').system(\'touch %s\')", "x": 1}' % marker
    conn.execute("INSERT OR REPLACE INTO entries VALUES (?, 'python', ?, 1, 0, 0)",
                 (_key_text(("python", "0", "planted")), zlib.compress(code.encode())))
    conn.commit()
    conn.close()
    again = Repository(repo.path)
    snap = again.snapshot("WORKTREE")  # the damaged entry is a miss: parsed again
    assert again.file_cache.stats["misses"] >= 1 and any(n.qualified_name == "m.f" for n in snap.symbols)
    assert again.file_cache.disk.get(("python", "0", "planted"), lambda d: d)[1]["error"].startswith("__import__")
    assert not marker.exists()


def test_coverage_reports_are_parsed_safely_and_bounded(make_repo) -> None:
    from repoviz import coverage
    from repoviz.review import build_review, resolve_target
    from repoviz.repo import Repository
    from test_review import COV_APP, COV_DIV

    repo = make_repo(COV_APP)
    repo.write({"app/calc.py": COV_DIV})
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;">'
            '<!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">]><coverage><packages><package><classes>'
            '<class filename="app/calc.py"><lines><line number="5" hits="&lol2;"/></lines></class>'
            "</classes></package></packages></coverage>")
    Path(repo.path, "coverage.xml").write_text(bomb)
    t = time.time() + 100
    os.utime(Path(repo.path, "coverage.xml"), (t, t))
    report = coverage.parse(Path(repo.path, "coverage.xml"), "coverage.xml", 10**6)
    assert report.files == {} and "entity declarations are not supported" in (report.error or "")
    r = Repository(repo.path)
    rev = build_review(r, resolve_target(r, "all"))  # the review goes on, and says why the report was not used
    assert "entity declarations" in rev["coverage"]["reports"][0]["error"]
    assert not [f for f in rev["findings"] if f["kind"] == "changed-lines-uncovered"]
    # too large: ignored, with a note (never read)
    Path(repo.path, "coverage.xml").write_text("SF:app/calc.py\n" + "DA:1,1\n" * 200_000)
    big = coverage.parse(Path(repo.path, "coverage.xml"), "coverage.xml", 1_000_000)
    assert big.files == {} and "larger than 1 MB" in (big.error or "")
    # a symbolic link is not followed
    Path(repo.path, "coverage.xml").unlink()
    Path(repo.path, "lcov.info").symlink_to("/etc/hostname")
    assert coverage.find_reports(Path(repo.path), None) == []
