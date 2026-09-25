"""Review outputs for continuous integration: SARIF, GitHub workflow commands and a pull-request comment.

* :func:`review_sarif` – SARIF 2.1.0 for code scanning: one rule per signal kind, one result per signal,
  ``partialFingerprints`` from the signal's stable ID so a finding is tracked across pushes;
* :func:`github_commands` – ``::error file=…,line=…::…`` workflow commands, for inline annotations without
  code scanning (escaped as GitHub requires);
* :func:`pr_comment` – compact Markdown for a pull request: the risk and counts, a Mermaid "where the
  agent went" map (at most :data:`MAX_MAP_NODES` nodes), the top signals with links to the PR head, every file
  in a collapsible block, and a hidden marker so a bot can update its comment in place.

Everything here formats a review report (``review.build_review``); nothing reads or runs repository code.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

DOCS_URL = "https://github.com/phillipecardenuto/repo-understanding/blob/main/docs/review.md#review-signals"
COMMENT_MARKER = "<!-- repoviz-review -->"
MAX_VALUE_ROWS = 30  # constants and settings listed in a pull-request comment
MAX_COMMENT_CHARS = 65_000  # GitHub refuses comments over 65,536 characters
MAX_MAP_NODES = 40
TOP_SIGNALS = 10
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
SARIF_LEVEL = {"high": "error", "medium": "warning", "low": "note", "info": "note"}
GITHUB_LEVEL = {"high": "error", "medium": "warning", "low": "notice", "info": "notice"}
SEVERITY_MARK = {"high": "🔴 high", "medium": "🟠 medium", "low": "🔵 low", "info": "⚪ info"}


# --------------------------------------------------------------------------- SARIF


def review_sarif(report: dict[str, Any], version: str) -> dict[str, Any]:
    """SARIF 2.1.0: one rule per signal kind, one result per signal."""
    rules: dict[str, dict[str, Any]] = {}
    results = []
    for f in report.get("findings", []):
        kind = f["kind"]
        if kind not in rules:
            rules[kind] = {"id": kind, "name": _camel(kind), "shortDescription": {"text": f["title"]},
                           "helpUri": DOCS_URL, "help": {"text": f"See the `{kind}` signal in docs/review.md."},
                           "defaultConfiguration": {"level": SARIF_LEVEL.get(f["severity"], "note")},
                           "properties": {"category": f.get("category", ""), "tags": ["repoviz", f.get("category", "")]}}
        text = f["title"] + (f": {f['detail']}" if f.get("detail") else "")
        if f.get("suggestion"):
            text += f" Suggestion: {f['suggestion']}"
        result: dict[str, Any] = {"ruleId": kind, "level": SARIF_LEVEL.get(f["severity"], "note"),
                                  "message": {"text": text}, "partialFingerprints": {"repovizFinding/v1": f["id"]},
                                  "properties": {"severity": f["severity"]}}
        if f.get("path"):
            loc: dict[str, Any] = {"artifactLocation": {"uri": f["path"], "uriBaseId": "%SRCROOT%"}}
            if f.get("line"):
                loc["region"] = {"startLine": int(f["line"])}
            result["locations"] = [{"physicalLocation": loc}]
        results.append(result)
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": "repoviz", "version": version, "informationUri":
                                          "https://github.com/phillipecardenuto/repo-understanding",
                                          "rules": list(rules.values())}},
                      "results": results,
                      "properties": {"base": report.get("base", {}).get("label"),
                                     "head": report.get("head", {}).get("label")}}]}


def _camel(kind: str) -> str:
    return "".join(part.capitalize() for part in kind.split("-"))


# --------------------------------------------------------------------------- GitHub workflow commands


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def github_commands(findings: list[dict[str, Any]], min_severity: str = "low") -> str:
    """One ``::error`` / ``::warning`` / ``::notice`` workflow command per signal at or above ``min_severity``."""
    limit = SEVERITY_ORDER.get(min_severity, 2)
    lines = []
    for f in findings:
        if SEVERITY_ORDER.get(f["severity"], 9) > limit:
            continue
        props = [f"title={_escape_property('repoviz: ' + f['title'])}"]
        if f.get("path"):
            props.insert(0, f"file={_escape_property(f['path'])}")
            if f.get("line"):
                props.insert(1, f"line={int(f['line'])}")
        message = f.get("detail") or f["title"]
        if f.get("suggestion"):
            message += f"\nSuggestion: {f['suggestion']}"
        lines.append(f"::{GITHUB_LEVEL.get(f['severity'], 'notice')} {','.join(props)}::{_escape_data(message)}")
    return "\n".join(lines) + ("\n" if lines else "")


# --------------------------------------------------------------------------- pull-request comment


def _mermaid_text(text: str, limit: int = 60) -> str:
    text = text if len(text) <= limit else text[:limit - 1] + "…"
    return re.sub(r'["<>`]', "", text).replace("|", "¦")


def review_map(report: dict[str, Any], max_nodes: int = MAX_MAP_NODES) -> tuple[str, int]:
    """Mermaid "where the agent went": touched components (or files, when few), with lines, scope and high
    signals as text (GitHub renders Mermaid in comments).  Returns the diagram and how many nodes were left out."""
    comps = report.get("components", [])
    files = report.get("files", [])
    high_by_path: dict[str, int] = {}
    for f in report.get("findings", []):
        if f["severity"] == "high" and f.get("path"):
            high_by_path[f["path"]] = high_by_path.get(f["path"], 0) + 1
    lines = ["flowchart LR"]
    ids: dict[str, str] = {}
    by_files = len(comps) <= 1 and len(files) > 1
    items = ([{"id": f["path"], "name": f["path"], "status": f["status"], "added": f.get("lines_added") or 0,
               "removed": f.get("lines_removed") or 0, "files": None, "protected": f["scope"] == "protected",
               "out": f["scope"] == "out-of-scope", "high": high_by_path.get(f["path"], 0)} for f in files]
             if by_files else
             [{"id": c["id"], "name": c["name"], "status": c.get("status", "modified"), "added": c["lines_added"],
               "removed": c["lines_removed"], "files": c["files"], "protected": bool(c["scope"].get("protected")),
               "out": bool(c["scope"].get("out-of-scope")), "high": c["findings"].get("high", 0)} for c in comps])
    items.sort(key=lambda x: (-(x["protected"] * 1000 + x["high"] * 100), -(x["added"] + x["removed"]), x["name"]))
    shown = items if len(items) <= max_nodes else items[:max_nodes - 1]  # keep one node for "… N more"
    hidden = len(items) - len(shown)
    for i, it in enumerate(shown):
        nid = f"n{i}"
        ids[it["id"]] = nid
        parts = [f"+{it['added']} −{it['removed']}"]
        if it["files"] is not None:
            parts.append(f"{it['files']} file{'s' if it['files'] != 1 else ''}")
        if it["protected"]:
            parts.append("🔒 protected")
        if it["out"]:
            parts.append("⚠ out of scope")
        if it["high"]:
            parts.append(f"❗ {it['high']} high")
        status = {"added": "✚ ", "removed": "✖ ", "renamed": "↦ "}.get(it["status"], "")
        lines.append(f'  {nid}["{status}{_mermaid_text(it["name"])}<br/>{_mermaid_text(" · ".join(parts), 80)}"]')
        cls = "protected" if it["protected"] else "outscope" if it["out"] else it["status"] if it["status"] in (
            "added", "removed") else "modified"
        lines.append(f"  class {nid} {cls}")
    if hidden:
        lines.append(f'  more["… {hidden} more"]')
    if not by_files:
        for e in report.get("component_edges", []):
            s, t = ids.get(e["source"]), ids.get(e["target"])
            if not s or not t or s == t:
                continue
            if e["status"] == "added":
                lines.append(f'  {s} ==>|"+ new{" ⟲ cycle" if e.get("new_cycle") else ""}"| {t}')
            elif e["status"] == "removed":
                lines.append(f'  {s} -.->|"− removed"| {t}')
            else:
                lines.append(f"  {s} --> {t}")
    lines += ["  classDef added fill:#dcfce7,stroke:#15803d,stroke-width:2px",
              "  classDef removed fill:#fee2e2,stroke:#b91c1c,stroke-dasharray:5 3",
              "  classDef modified fill:#fef3c7,stroke:#b45309",
              "  classDef protected fill:#fecaca,stroke:#7f1d1d,stroke-width:4px",
              "  classDef outscope fill:#ffedd5,stroke:#c2410c,stroke-width:3px"]
    return "\n".join(lines), hidden


def _link(path: str | None, line: int | None, link_base: str | None) -> str:
    if not path:
        return ""
    label = f"{path}:{line}" if line else path
    if not link_base:
        return f"`{label}`"
    return f"[`{label}`]({link_base.rstrip('/')}/{quote(path)}{f'#L{line}' if line else ''})"


def _code(text: str) -> str:
    """Inline code for a table cell: backticks and pipes cannot break out of it."""
    text = str(text).replace("|", "\\|")
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def pr_comment(report: dict[str, Any], *, link_base: str | None = None, max_chars: int = MAX_COMMENT_CHARS,
               max_nodes: int = MAX_MAP_NODES, top: int = TOP_SIGNALS) -> str:
    """Markdown for a pull-request comment, under ``max_chars`` (details are dropped first, with a note)."""
    s = report["summary"]
    risk = report.get("risk") or {}
    counts = ", ".join(f"{v} {k}" for k, v in s["findings"].items() if v) or "no signals"
    head = [COMMENT_MARKER, f"### repoviz review: {report['target']['label']}", "",
            f"`{report['base']['label']}` → `{report['head']['label']}` · {s['files']} file(s) in {s['components']} "
            f"component(s) · +{s['lines_added']} −{s['lines_removed']} · {counts}"]
    from .review import dependency_summary

    if dependency_summary(s):
        head[-1] += f" · dependencies {dependency_summary(s)}"
    if risk.get("path"):
        icon = {"high": "🔴", "medium": "🟠"}.get(risk["level"], "🟢")
        head.append(f"\n**Risk: {icon} {risk['level']} ({risk['score']}/100)**, because of "
                    f"{_link(risk['path'], None, link_base)}")
    if s.get("protected") or s.get("out_of_scope"):
        head.append(f"\n🔒 {s['protected']} protected · ⚠ {s['out_of_scope']} out-of-scope file(s)")
    diagram, hidden = review_map(report, max_nodes)
    body = ["", "#### Where the agent went", "", "```mermaid", diagram, "```"]
    if hidden:
        body.append(f"_{hidden} more not drawn (the map shows at most {max_nodes} nodes)._")
    findings = sorted(report.get("findings", []), key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9),
                                                                   f.get("path") or "", f.get("line") or 0))
    signals = ["", f"#### Top signals ({min(top, len(findings))} of {len(findings)})", ""]
    if findings:
        signals += ["| Severity | Signal | Where |", "|---|---|---|"]
        for f in findings[:top]:
            detail = (f.get("detail") or "").replace("|", "\\|").replace("\n", " ")
            if len(detail) > 220:
                detail = detail[:219] + "…"
            signals.append(f"| {SEVERITY_MARK.get(f['severity'], f['severity'])} | **{f['title']}**"
                           f"{' — ' + detail if detail else ''} | {_link(f.get('path'), f.get('line'), link_base)} |")
    else:
        signals.append("No review signal.")
    packages = [(f, pk) for f in report.get("files", []) for pk in f.get("packages") or [] if not pk.get("declared_in")]
    if packages:
        from .review import DEPENDENCY_WORD, dependency_summary

        signals += ["", f"<details><summary>Dependencies changed: {dependency_summary(s) or len(packages)}</summary>",
                    "", "| Package | Change | Before → after | Where |", "|---|---|---|---|"]
        for f, pk in packages[:MAX_VALUE_ROWS]:
            before = _code(pk["before"]) if pk.get("before") else "_(none)_"
            after = _code(pk["after"]) if pk.get("after") else "_(none)_"
            if pk.get("resolved"):
                after += f" (resolved {_code(pk['resolved'])})"
            word = DEPENDENCY_WORD.get(pk["status"], pk["status"]) + (" · unpinned" if pk.get("unpinned") else "")
            signals.append(f"| {_code(pk['name'])} | {word} | {before} → {after} | "
                           f"{_link(f['path'], pk.get('line'), link_base)} |")
        if len(packages) > MAX_VALUE_ROWS:
            signals.append(f"\n_{len(packages) - MAX_VALUE_ROWS} more; run `repoviz review` for all of them._")
        signals += ["", "</details>"]
    values = [(f, v) for f in report.get("files", []) for v in f.get("values") or []]
    if values:
        signals += ["", f"<details><summary>Values changed ({len(values)})</summary>", "",
                    "| Value | Before → after | Where |", "|---|---|---|"]
        for f, v in values[:MAX_VALUE_ROWS]:
            change = f"{_code(v['value_before']) if v['value_before'] is not None else '_(new)_'} → " \
                     f"{_code(v['value']) if v['value'] is not None else '_(removed)_'}"
            if v.get("weakens"):
                change += f" ⚠ {v['weakens']}"
            signals.append(f"| {_code(v['name'])} | {change} | {_link(f['path'], v.get('line'), link_base)} |")
        if len(values) > MAX_VALUE_ROWS:
            signals.append(f"\n_{len(values) - MAX_VALUE_ROWS} more; run `repoviz review` for all of them._")
        signals += ["", "</details>"]
    files = ["", f"<details><summary>All changed files ({len(report.get('files', []))})</summary>", "",
             "| File | Change | +/− | Risk | Signals |", "|---|---|---:|---|---:|"]
    for f in sorted(report.get("files", []), key=lambda f: -((f.get("risk") or {}).get("score") or 0)):
        r = f.get("risk") or {}
        files.append(f"| {_link(f['path'], None, link_base)} | {f['status']} | +{f.get('lines_added') or 0} "
                     f"−{f.get('lines_removed') or 0} | {r.get('score', '')} {r.get('level', '')} | "
                     f"{len(f.get('findings', []))} |")
    files += ["", "</details>"]
    foot = ["", "<sub>Generated by repoviz. Signals are heuristics to guide the review, not proof of a bug. "
                "The analysis reads Git only; it never runs the repository's code.</sub>"]
    out = "\n".join(head + body + signals + files + foot)
    if len(out) <= max_chars:
        return out + "\n"
    # Too long: drop the file list rows from the end, then the signal details.
    note = ["", "_The file list was shortened to fit in a GitHub comment; run `repoviz review` for all of it._"]
    while len(files) > 6 and len("\n".join(head + body + signals + files + note + foot)) > max_chars:
        files.pop(-3)
    out = "\n".join(head + body + signals + files + note + foot)
    if len(out) > max_chars:
        out = out[:max_chars - 200] + "\n\n_Truncated to fit in a GitHub comment._\n"
    return out + "\n"
