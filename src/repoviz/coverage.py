"""Existing coverage reports, read (never produced), to tell which changed lines a test executed.

``untested-change`` says "no test is statically connected to this code": a proxy.  Many projects already have a
coverage report from CI or a local run; reading it gives line-level truth for the lines an agent changed.

* **Reports.**  ``[review.coverage] paths`` (default :data:`DEFAULT_PATHS`, relative to the repository; absolute
  paths may be configured).  Regular files only, at most ``max_mb`` (50 MB).  Formats: Cobertura XML (coverage.py,
  many JavaScript tools), JaCoCo XML, LCOV, Istanbul ``coverage-final.json`` and Go ``cover.out``.  XML with entity
  declarations is refused (entity-expansion bombs, external entities).
* **Paths.**  A report's paths are mapped to repository paths: as they are, under Cobertura's ``<source>`` roots,
  else by the longest path suffix that names exactly one file; an ambiguous suffix is skipped and reported.
* **Freshness.**  A report describes the code as it was when it was written (its own timestamp when it has one,
  else its modification time).  It applies to a changed file only when the file has not changed since, on disk,
  and the reviewed state of the file is what is on disk; otherwise the changed lines are "unknown".

Nothing is executed: repoviz never runs the test suite.
"""

from __future__ import annotations

import json
import os
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

DEFAULT_PATHS = ("coverage.xml", ".coverage.xml", "coverage/cobertura-coverage.xml", "coverage/lcov.info",
                 "lcov.info", "coverage/coverage-final.json", "cover.out", "coverage.out", "jacoco.xml")
MAX_REPORT_MB = 50.0
MAX_REPORT_FILES = 50_000  # files described by one report
MAX_LISTED_LINES = 200  # covered / uncovered line numbers kept per file for the page


@dataclass
class Report:
    """One coverage report: for each file it describes, whether each executable line ran."""

    path: str  # as configured (shown to the user)
    format: str  # cobertura | jacoco | lcov | istanbul | go
    time: float  # when it was written: its own timestamp, else the file's modification time
    files: dict[str, dict[int, bool]] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)  # Cobertura <source> directories
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        out = {"path": self.path, "format": self.format, "time": self.time, "files": len(self.files)}
        if self.error:
            out["error"] = self.error
        return out


# --------------------------------------------------------------------------- parsers


_ENTITY = re.compile(rb"<!ENTITY", re.IGNORECASE)


def _xml(data: bytes) -> ET.Element:
    """Parse an XML report; entity declarations (entity-expansion bombs, external entities) are refused.  An external
    DTD reference (JaCoCo's) is harmless: the parser never fetches it."""
    if _ENTITY.search(data):
        raise ValueError("XML entity declarations are not supported")
    root = ET.fromstring(data)
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _mark(lines: dict[int, bool], number: Any, hit: bool) -> None:
    try:
        n = int(number)
    except (TypeError, ValueError):
        return
    if n > 0:
        lines[n] = lines.get(n, False) or hit


def parse_cobertura(root: ET.Element, report: Report) -> None:
    ts = root.get("timestamp")
    if ts and ts.isdigit():
        t = int(ts)
        report.time = t / 1000 if t > 10**11 else float(t)  # coverage.py writes milliseconds
    report.roots = [s.text.strip() for s in root.iter("source") if s.text and s.text.strip()]
    for cls in root.iter("class"):
        name = cls.get("filename")
        if not name:
            continue
        lines = report.files.setdefault(name.replace("\\", "/"), {})
        for line in cls.iter("line"):
            try:
                hits = int(line.get("hits") or 0)
            except ValueError:
                hits = 0
            _mark(lines, line.get("number"), hits > 0)
        if len(report.files) >= MAX_REPORT_FILES:
            break


def parse_jacoco(root: ET.Element, report: Report) -> None:
    session = root.find("sessioninfo")
    if session is not None and (session.get("dump") or "").isdigit():
        report.time = int(session.get("dump") or 0) / 1000
    for pkg in root.iter("package"):
        base = (pkg.get("name") or "").strip("/")
        for src in pkg.iter("sourcefile"):
            name = f"{base}/{src.get('name')}" if base else str(src.get("name"))
            lines = report.files.setdefault(name, {})
            for line in src.iter("line"):
                try:
                    covered = int(line.get("ci") or 0) > 0
                except ValueError:
                    covered = False
                _mark(lines, line.get("nr"), covered)
            if len(report.files) >= MAX_REPORT_FILES:
                return


def parse_lcov(text: str, report: Report) -> None:
    lines: dict[int, bool] | None = None
    for raw in text.splitlines():
        if raw.startswith("SF:"):
            if len(report.files) >= MAX_REPORT_FILES:
                return
            lines = report.files.setdefault(raw[3:].strip().replace("\\", "/"), {})
        elif raw.startswith("DA:") and lines is not None:
            parts = raw[3:].split(",")
            if len(parts) >= 2:
                try:
                    hits = int(float(parts[1]))
                except ValueError:
                    hits = 0
                _mark(lines, parts[0], hits > 0)
        elif raw.startswith("end_of_record"):
            lines = None


def parse_istanbul(data: Any, report: Report) -> None:
    if not isinstance(data, dict):
        raise ValueError("not an Istanbul coverage-final.json")
    for key, entry in data.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("statementMap"), dict):
            continue
        name = str(entry.get("path") or key).replace("\\", "/")
        lines = report.files.setdefault(name, {})
        counts = entry.get("s") if isinstance(entry.get("s"), dict) else {}
        for sid, loc in entry["statementMap"].items():
            start = (loc or {}).get("start") or {}
            count = counts.get(sid, 0)
            _mark(lines, start.get("line"), isinstance(count, (int, float)) and count > 0)
        if len(report.files) >= MAX_REPORT_FILES:
            return


_GO_BLOCK = re.compile(r"^(.+?):(\d+)\.\d+,(\d+)\.\d+ \d+ (\d+)$")


def parse_go(text: str, report: Report) -> None:
    for raw in text.splitlines():
        m = _GO_BLOCK.match(raw.strip())
        if not m:
            continue
        name = m.group(1)
        if name not in report.files and len(report.files) >= MAX_REPORT_FILES:
            continue
        lines = report.files.setdefault(name, {})
        start, end, count = int(m.group(2)), int(m.group(3)), int(m.group(4))
        for n in range(start, min(end, start + 10_000) + 1):
            _mark(lines, n, count > 0)


def parse(path: Path, display: str, max_bytes: int) -> Report:
    """Read one report (whatever its format)."""
    try:
        st = path.stat()
    except OSError as exc:
        return Report(display, "unknown", 0.0, error=str(exc))
    report = Report(display, "unknown", st.st_mtime)
    if st.st_size > max_bytes:
        report.error = f"larger than {max_bytes // 1_000_000} MB: ignored"
        return report
    try:
        data = path.read_bytes()
        stripped = data.lstrip()
        if stripped.startswith(b"<"):
            root = _xml(data)
            if root.tag == "coverage":
                report.format = "cobertura"
                parse_cobertura(root, report)
            elif root.tag == "report":
                report.format = "jacoco"
                parse_jacoco(root, report)
            else:
                raise ValueError(f"unknown XML report <{root.tag}>")
        elif stripped.startswith(b"{"):
            report.format = "istanbul"
            parse_istanbul(json.loads(data), report)
        elif stripped.startswith(b"mode:"):
            report.format = "go"
            parse_go(data.decode("utf-8", errors="replace"), report)
        else:
            report.format = "lcov"
            parse_lcov(data.decode("utf-8", errors="replace"), report)
            if not report.files:
                raise ValueError("not a coverage report (no SF: records)")
    except (ValueError, ET.ParseError, RecursionError, UnicodeDecodeError) as exc:
        report.files = {}
        report.error = f"cannot read it: {exc}"[:300]
    return report


_CACHE: dict[tuple[str, int, int, int], Report] = {}
_CACHE_LOCK = threading.Lock()


def load(path: Path, display: str, max_bytes: int) -> Report:
    """:func:`parse`, cached per file state (path, size, modification time)."""
    try:
        st = path.stat()
    except OSError as exc:
        return Report(display, "unknown", 0.0, error=str(exc))
    key = (str(path), st.st_size, st.st_mtime_ns, max_bytes)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is None:
        hit = parse(path, display, max_bytes)
        with _CACHE_LOCK:
            if len(_CACHE) > 32:
                _CACHE.clear()
            _CACHE[key] = hit
    return hit


# --------------------------------------------------------------------------- reports of a repository


def find_reports(root: Path, paths: Iterable[str] | None) -> list[tuple[Path, str]]:
    """Existing report files: configured paths (relative to the repository, or absolute), else the defaults.
    Relative paths must stay inside the repository; symbolic links and other non-regular files are skipped."""
    out = []
    root = root.resolve()
    for entry in (list(paths) if paths else DEFAULT_PATHS):
        p = Path(os.path.expanduser(entry))
        full = p if p.is_absolute() else root / p
        try:
            if full.is_symlink() or not full.is_file():
                continue
            if not p.is_absolute() and root not in full.resolve().parents:
                continue
        except OSError:
            continue
        out.append((full, entry))
    return out


def map_paths(report: Report, repo_files: Iterable[str], root: Path) -> tuple[dict[str, dict[int, bool]], list[str]]:
    """The report's files as repository paths, and the report paths that match several files."""
    files = set(repo_files)
    by_name: dict[str, list[str]] = {}
    for f in files:
        by_name.setdefault(PurePosixPath(f).name, []).append(f)
    root_s = str(root.resolve()).replace("\\", "/").rstrip("/") + "/"
    roots = [r.replace("\\", "/").rstrip("/") for r in report.roots]
    out: dict[str, dict[int, bool]] = {}
    ambiguous: list[str] = []
    for name, lines in report.files.items():
        found = None
        candidates = [name] + [f"{r}/{name}" for r in roots]
        for c in candidates:
            rel = c[len(root_s):] if c.startswith(root_s) else c.removeprefix("./")
            if rel in files:
                found = rel
                break
        if found is None:
            parts = [x for x in name.split("/") if x and x != "."]
            pool = by_name.get(parts[-1], []) if parts else []
            for k in range(len(parts), 0, -1):  # the longest suffix that names a file decides
                suffix = "/".join(parts[-k:])
                hits = [f for f in pool if f == suffix or f.endswith("/" + suffix)]
                if len(hits) == 1:
                    found = hits[0]
                elif hits:  # several files end like this (a shorter suffix would only match more)
                    ambiguous.append(name)
                if hits:
                    break
        if found is not None:
            merged = out.setdefault(found, {})
            for n, hit in lines.items():
                merged[n] = merged.get(n, False) or hit
    return out, ambiguous


@dataclass
class CoverageSet:
    """The reports found for a review, mapped to repository paths."""

    reports: list[Report]
    lines: dict[str, tuple[Report, dict[int, bool]]]
    ambiguous: list[str]

    def summary(self) -> dict[str, Any]:
        return {"reports": [r.summary() for r in self.reports], "ambiguous": self.ambiguous[:20],
                "ambiguous_count": len(self.ambiguous)}


def coverage_for(root: Path, repo_files: Iterable[str], config: Any) -> CoverageSet | None:
    """The coverage reports of the repository at ``root`` (``None`` when there is none or it is turned off)."""
    if not getattr(config, "review_coverage_enabled", True):
        return None
    found = find_reports(root, getattr(config, "review_coverage_paths", None))
    if not found:
        return None
    max_bytes = int(getattr(config, "review_coverage_max_mb", MAX_REPORT_MB) * 1_000_000)
    reports = [load(path, display, max_bytes) for path, display in found]
    files = list(repo_files)
    lines: dict[str, tuple[Report, dict[int, bool]]] = {}
    ambiguous: list[str] = []
    for r in sorted(reports, key=lambda r: -r.time):  # the newest report wins for a file
        mapped, amb = map_paths(r, files, root)
        ambiguous += amb
        for path, cov in mapped.items():
            lines.setdefault(path, (r, cov))
    return CoverageSet(reports, lines, ambiguous)


def file_coverage(cov: CoverageSet, path: str, changed_lines: list[int], *, fresh: bool) -> dict[str, Any] | None:
    """Coverage of one changed file's new lines: ``None`` when no report describes the file."""
    hit = cov.lines.get(path)
    if hit is None:
        return None
    report, lines = hit
    if not fresh:
        return {"report": report.path, "fresh": False, "report_time": report.time}
    executable = [n for n in changed_lines if n in lines]
    covered = [n for n in executable if lines[n]]
    uncovered = [n for n in executable if not lines[n]]
    return {"report": report.path, "fresh": True, "report_time": report.time, "executable": len(executable),
            "covered": len(covered), "covered_lines": covered[:MAX_LISTED_LINES],
            "uncovered_lines": uncovered[:MAX_LISTED_LINES]}


def ranges(numbers: list[int], limit: int = 8) -> str:
    """``3, 7-9, 12`` (at most ``limit`` ranges)."""
    out: list[str] = []
    start = prev = None
    for n in sorted(numbers):
        if start is None:
            start = prev = n
        elif n == prev + 1:  # type: ignore[operator]
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out[:limit]) + (", …" if len(out) > limit else "")
