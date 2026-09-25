"""The persistent parse cache (#34)."""

from __future__ import annotations

import ast
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

from repoviz.cli import main
from repoviz.diskcache import FILE_NAME, DiskCache, TwoLevelCache
from repoviz.repo import Repository

FILES = {"pkg/__init__.py": "", "pkg/a.py": "def a():\n    return 1\n", "pkg/b.py": "from pkg.a import a\n\n\ndef b():\n    return a()\n",
         "web/app.js": "import { x } from './lib';\nexport function run() { return x(); }\n", "web/lib.js": "export const x = () => 1;\n"}


def fresh(repo) -> Repository:
    """A new process, as far as caches go: nothing in memory, the same state directory."""
    return Repository(repo.path)


def test_second_process_reads_the_disk_and_one_change_parses_one_file(make_repo) -> None:
    repo = make_repo(FILES)
    first = fresh(repo)
    first.snapshot("WORKTREE")
    assert first.file_cache.stats["misses"] == 6 and first.file_cache.stats["written"] == 6  # 5 files + HEAD's churn
    second = fresh(repo)
    snap = second.snapshot("WORKTREE")
    assert second.file_cache.stats["misses"] == 0 and second.file_cache.stats["disk_hits"] >= 5
    assert any(n.qualified_name == "pkg.b.b" for n in snap.symbols)
    repo.write({"pkg/b.py": "from pkg.a import a\n\n\ndef b():\n    return a() + 1\n"})
    third = fresh(repo)
    third.snapshot("WORKTREE")
    assert third.file_cache.stats["misses"] == 1  # exactly the changed file is parsed again
    # the same content at another revision is the same entry (keys are content hashes)
    fourth = fresh(repo)
    fourth.snapshot("HEAD")
    assert fourth.file_cache.stats["misses"] == 0


def test_versions_and_config_invalidate_only_what_they_affect(make_repo, monkeypatch) -> None:
    from repoviz.analyzers.python import PythonAnalyzer

    repo = make_repo(FILES)
    fresh(repo).snapshot("WORKTREE")
    with monkeypatch.context() as m:
        m.setattr(PythonAnalyzer, "version", PythonAnalyzer.version + "-bumped")
        bumped = fresh(repo)
        bumped.snapshot("WORKTREE")
        assert bumped.file_cache.stats["misses"] == 3  # the Python files only: JavaScript entries still hit
    repo.write({".repoviz.toml": "exclude = ['docs/**']\n"})  # another configuration: parsing does not depend on it
    configured = fresh(repo)
    configured.snapshot("WORKTREE")
    assert configured.file_cache.stats["misses"] == 0


def test_eviction_schema_and_corruption(make_repo, tmp_path) -> None:
    disk = DiskCache(tmp_path / "c", max_bytes=10_000)
    cache = TwoLevelCache(disk)
    cache._codecs = {"t": (lambda v: v, lambda d: d)}
    for i in range(40):
        cache[("t", "1", f"k{i}")] = {"payload": os.urandom(300).hex()}  # does not compress
        cache.flush()
    info = disk.info()
    assert 0 < info["entries"] < 40 and info["payload_mb"] * 1e6 <= 10_000  # oldest entries went first
    assert disk.get(("t", "1", "k39"), lambda d: d)[0] and not disk.get(("t", "1", "k0"), lambda d: d)[0]
    disk.close()
    # a schema from another version: dropped and rebuilt
    import sqlite3

    conn = sqlite3.connect(tmp_path / "c" / FILE_NAME)
    conn.execute("UPDATE meta SET value = '0' WHERE name = 'schema'")
    conn.commit()
    conn.close()
    again = DiskCache(tmp_path / "c", max_bytes=10_000)
    assert again.info()["entries"] == 0
    again.close()
    # a corrupt file: moved aside, rebuilt, and the next snapshot says so; nothing crashes
    repo = make_repo(FILES)
    fresh(repo).snapshot("WORKTREE")
    db = fresh(repo).file_cache.disk.path
    db.write_bytes(b"this is not a database" * 100)
    for extra in ("-wal", "-shm"):
        Path(str(db) + extra).unlink(missing_ok=True)
    r = fresh(repo)
    snap = r.snapshot("WORKTREE")
    assert any(d.code == "cache-reset" for d in snap.diagnostics)
    assert Path(str(db) + ".corrupt").exists() and r.file_cache.stats["misses"] == 6
    assert fresh(repo).snapshot("WORKTREE") is not None and fresh(repo).file_cache.disk.info()["entries"] >= 5


def test_private_files_and_json_only(make_repo) -> None:
    repo = make_repo(FILES)
    r = fresh(repo)
    r.snapshot("WORKTREE")
    db = r.file_cache.disk.path
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    for extra in ("-wal", "-shm"):
        p = Path(str(db) + extra)
        if p.exists():
            assert stat.S_IMODE(p.stat().st_mode) == 0o600
    # reading an entry never unpickles or executes: the module imports neither pickle nor marshal
    import repoviz.diskcache as dc

    tree = ast.parse(Path(dc.__file__).read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"pickle", "cPickle", "marshal", "shelve", "dill"}


def test_concurrent_writers_do_not_corrupt_the_store(make_repo) -> None:
    repo = make_repo(FILES)
    fresh(repo).snapshot("WORKTREE")
    db = fresh(repo).file_cache.disk.path
    script = (
        "import sys\n"
        "from repoviz.diskcache import DiskCache, TwoLevelCache\n"
        "c = TwoLevelCache(DiskCache(sys.argv[1], 10**9)); c._codecs = {'t': (lambda v: v, lambda d: d)}\n"
        "for i in range(60):\n"
        "    c[('t', sys.argv[2], str(i))] = {'v': i}\n"
        "    c.flush()\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", script, str(db.parent), f"p{j}"]) for j in range(3)]

    def threads_write(tag: str) -> None:
        c = TwoLevelCache(DiskCache(db.parent, 10**9))
        c._codecs = {"t": (lambda v: v, lambda d: d)}
        for i in range(60):
            c[("t", tag, str(i))] = {"v": i}
            c.flush()

    workers = [threading.Thread(target=threads_write, args=(f"t{j}",)) for j in range(3)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert all(p.wait(timeout=120) == 0 for p in procs)
    import sqlite3

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE ns = 't'").fetchone()[0] == 6 * 60
    conn.close()


def test_opt_out_and_cli(make_repo, monkeypatch, capsys) -> None:
    repo = make_repo(FILES)
    monkeypatch.setenv("REPOVIZ_NO_DISK_CACHE", "1")
    assert isinstance(fresh(repo).file_cache, dict)
    monkeypatch.delenv("REPOVIZ_NO_DISK_CACHE")
    repo.write({".repoviz.toml": "[cache]\ndisk = false\n"})
    assert isinstance(fresh(repo).file_cache, dict)
    repo.write({".repoviz.toml": "[cache]\nmax_mb = 5\n"})
    r = fresh(repo)
    assert r.file_cache.disk.max_bytes == 5_000_000
    r.snapshot("WORKTREE")
    assert main(["cache", "-C", repo.path]) == 0
    out = capsys.readouterr().out
    assert "Parse cache:" in out and "python: 3 entries" in out and "cap 5.0 MB" in out
    assert main(["cache", "clear", "-C", repo.path]) == 0
    assert "Removed" in capsys.readouterr().out
    assert fresh(repo).file_cache.disk.info()["entries"] == 0


def test_no_writable_state_dir_means_no_disk_cache(make_repo, tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("REPOVIZ_STATE_DIR", str(blocker / "state"))  # cannot be created, even by root
    repo = make_repo(FILES)
    r = fresh(repo)
    snap = r.snapshot("WORKTREE")  # the analysis still works, from memory
    assert snap.symbols and r.file_cache.stats["misses"] == 6
    assert "unavailable" in r.file_cache.disk.info()["note"]
