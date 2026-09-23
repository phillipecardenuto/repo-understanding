"""Command-line interface.

Examples::

    repoviz serve                         # live web app for the current repository
    repoviz report -o report.html         # self-contained, offline HTML report
    repoviz diff                          # HEAD vs working tree summary
    repoviz diff main...                  # changes since the merge base with main
    repoviz diff v1.0..v2.0 --format markdown
    repoviz diff --mode staged --fail-on new-cycle
    repoviz mermaid --view dependencies --level module
    repoviz discover
    repoviz session start --label "wave 3" --allow "src/billing/**" --protect "src/auth/**"
    repoviz review                        # what did the agent touch? what looks wrong?
    repoviz review --format prompt        # feedback to paste back to the agent
    repoviz activity
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import ConfigError
from .gitutil import GitError
from .model import RepositoryDiff, RepositorySnapshot
from .repo import PRESETS, Repository, RepositoryError

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_GATE = 0, 1, 2, 3
FAIL_CONDITIONS = ("new-cycle", "new-dependency", "new-component-dependency", "new-external-dependency",
                   "removed-dependency", "any-change")


def _write(text: str, output: str | None) -> None:
    if output and output != "-":
        Path(output).write_text(text, encoding="utf-8")
        print(f"wrote {output} ({len(text.encode('utf-8')):,} bytes)", file=sys.stderr)
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _open(args: argparse.Namespace) -> Repository:
    overrides: dict[str, Any] = {}
    if getattr(args, "exclude", None):
        overrides["exclude"] = list(args.exclude)
    if getattr(args, "source_root", None):
        overrides["source_roots"] = list(args.source_root)
    if getattr(args, "no_grimp", False):
        overrides["python"] = {"use_grimp": "never"}
    return Repository(args.repo, config_file=args.config, overrides=overrides or None)


# --------------------------------------------------------------------------- formatting


def format_diff_text(diff: RepositoryDiff, label: str) -> str:
    s = diff.summary()
    nodes = diff.nodes
    name = lambda i: nodes[i].node.qualified_name if i in nodes else i  # noqa: E731
    out = [f"Comparing {diff.base.label} → {diff.target.label}" + (f"  [{label}]" if label else ""),
           f"  nodes:  +{s['nodes']['added']}  −{s['nodes']['removed']}  ~{s['nodes']['modified']}   "
           f"relationships: +{s['edges']['added']}  −{s['edges']['removed']}  ~{s['edges']['modified']}   "
           f"cycles: +{s['cycles']['introduced']} −{s['cycles']['resolved']}"]
    by_cat = s["nodes_by_category"]
    for status in ("added", "removed", "modified"):
        if by_cat[status]:
            out.append(f"  {status:9s} " + ", ".join(f"{v} {k}(s)" for k, v in sorted(by_cat[status].items())))
    if diff.new_dependencies:
        out.append("\nNew dependencies:")
        for d in diff.new_dependencies:
            flags = [f for f, on in (("external", d["external"] and not d["stdlib"]), ("stdlib", d["stdlib"]),
                                     ("⟲ in cycle", d["in_cycle"]), ("type-only", d["type_checking_only"])) if on]
            where = f"  ({', '.join(d['evidence'][:2])})" if d["evidence"] else ""
            note = f" — {d['note']}" if d.get("note") else ""
            out.append(f"  [{d['level']}] {d['source']} → {d['target']}"
                       f"{' [' + ', '.join(flags) + ']' if flags else ''}{note}{where}")
    if diff.removed_dependencies:
        out.append("\nRemoved dependencies:")
        for d in diff.removed_dependencies:
            out.append(f"  [{d['level']}] {d['source']} → {d['target']}")
    if diff.introduced_cycles:
        out.append("\nCycles introduced:")
        for c in diff.introduced_cycles:
            out.append(f"  [{c.level}] " + " → ".join(name(m) for m in (c.example_path or c.members)))
    if diff.resolved_cycles:
        out.append("\nCycles resolved:")
        for c in diff.resolved_cycles:
            out.append(f"  [{c.level}] " + " → ".join(name(m) for m in (c.example_path or c.members)))
    if diff.changed_cycles:
        out.append("\nCycles changed:")
        for c in diff.changed_cycles:
            out.append(f"  [{c['level']}] members: " + ", ".join(name(m) for m in c["members"])
                       + (f"; added {', '.join(name(m) for m in c['added_members'])}" if c["added_members"] else "")
                       + (f"; removed {', '.join(name(m) for m in c['removed_members'])}" if c["removed_members"] else ""))
    changed_modules = [nodes[i] for i in diff.added_nodes + diff.removed_nodes + diff.modified_nodes
                       if nodes[i].node.category == "module"]
    if changed_modules:
        out.append("\nChanged modules:")
        for c in sorted(changed_modules, key=lambda c: c.node.qualified_name)[:200]:
            marker = {"added": "+", "removed": "−", "modified": "~"}[c.status]
            reason = f"  ({'; '.join(c.reasons)})" if c.reasons and c.status == "modified" else ""
            out.append(f"  {marker} {c.node.qualified_name}{reason}")
    for d in diff.diagnostics:
        out.append(f"\n{d.severity}: {d.message}")
    return "\n".join(out) + "\n"


def format_diff_markdown(diff: RepositoryDiff, label: str, mermaid_text: str) -> str:
    s = diff.summary()
    lines = [f"### Architecture changes: `{diff.base.label}` → `{diff.target.label}`", "",
             "| | added | removed | modified |", "|---|---:|---:|---:|",
             f"| nodes | {s['nodes']['added']} | {s['nodes']['removed']} | {s['nodes']['modified']} |",
             f"| relationships | {s['edges']['added']} | {s['edges']['removed']} | {s['edges']['modified']} |",
             f"| cycles | {s['cycles']['introduced']} introduced | {s['cycles']['resolved']} resolved | "
             f"{s['cycles']['changed']} changed |", ""]
    if diff.new_dependencies:
        lines += ["**New dependencies**", ""]
        for d in diff.new_dependencies[:50]:
            lines.append(f"- `{d['source']}` → `{d['target']}` ({d['level']}"
                         f"{', stdlib' if d['stdlib'] else ''}{', ⟲ cycle' if d['in_cycle'] else ''})"
                         + (f" — {d['note']}" if d.get("note") else ""))
        lines.append("")
    if diff.introduced_cycles:
        nodes = diff.nodes
        lines += ["**Cycles introduced**", ""]
        for c in diff.introduced_cycles:
            lines.append("- " + " → ".join(f"`{nodes[m].node.qualified_name}`" for m in (c.example_path or c.members)))
        lines.append("")
    lines += ["```mermaid", mermaid_text.rstrip(), "```", ""]
    return "\n".join(lines)


def gate(diff: RepositoryDiff, conditions: list[str]) -> list[str]:
    reasons = []
    for cond in conditions:
        if cond == "new-cycle" and (diff.introduced_cycles or any(c["added_members"] for c in diff.changed_cycles)):
            reasons.append(f"{len(diff.introduced_cycles)} dependency cycle(s) introduced")
        elif cond == "new-dependency" and [d for d in diff.new_dependencies if not d["stdlib"]]:
            reasons.append(f"{len(diff.new_dependencies)} new dependency relationship(s)")
        elif cond == "new-component-dependency" and [d for d in diff.new_dependencies if d["level"] in ("component", "project")]:
            reasons.append("new dependency between components")
        elif cond == "new-external-dependency" and [d for d in diff.new_dependencies if d["external"] and not d["stdlib"]]:
            reasons.append("new external dependency")
        elif cond == "removed-dependency" and diff.removed_dependencies:
            reasons.append(f"{len(diff.removed_dependencies)} dependency relationship(s) removed")
        elif cond == "any-change" and diff.has_changes:
            reasons.append("the architecture changed")
    return reasons


# --------------------------------------------------------------------------- commands


def cmd_discover(args: argparse.Namespace) -> int:
    repo = _open(args)
    source = repo.open_source(args.rev)
    prof = repo.discover(source)
    data = prof.to_dict()
    if args.json:
        _write(json.dumps(data, indent=2, default=str), args.output)
        return EXIT_OK
    out = [f"Repository: {prof.root}  ({'Git' if prof.is_git else 'plain directory'})",
           f"  branch {prof.branch or '-'}  HEAD {(prof.head or '-')[:12]}  default branch {prof.default_branch or 'unknown'}",
           f"  files: {prof.file_count} total, {prof.analyzed_file_count} analyzed, {prof.excluded_count} excluded",
           "", "Languages:"]
    for l in prof.languages[:15]:
        support = "analyzed" if l["supported"] else ("structure only" if l["kind"] == "programming" else l["kind"])
        out.append(f"  {l['display']:<16} {l['files']:>6} files   {support}")

    def section(title: str, items: list[Any], fmt: Any) -> None:
        out.append(f"\n{title} ({len(items)}):")
        if not items:
            out.append("  (none)")
        for it in items[:40]:
            out.append("  " + fmt(it))
        if len(items) > 40:
            out.append(f"  … {len(items) - 40} more")

    section("Projects", prof.projects, lambda p: f"{p['path'] or '.'}: {p['name']} [{p['ecosystem']}]"
            + (f" {p['role']}" if p.get("role") else "") + (" (workspace member)" if p.get("workspace") else ""))
    section("Workspaces", prof.workspaces, lambda w: f"{w['path']} [{w['kind']}] members: {', '.join(w['members']) or '-'}")
    section("Manifests", prof.manifests, lambda m: f"{m['path']} [{m['kind']}]")
    section("Lock files", prof.lockfiles, lambda m: f"{m['path']} [{m['kind']}]")
    section("Source roots", prof.source_roots, lambda r: f"{r['path'] or '.'}  ({r['origin']})")
    section("Test roots", prof.test_roots, lambda r: f"{r['path']}  {r['files']} files ({r['origin']})")
    section("Generated / vendored", prof.generated + prof.vendored, lambda g: f"{g['path']}  ({g.get('reason', '')})")
    section("Documentation", prof.docs, lambda d: f"{d['path']}  ({d['reason']})")
    section("Entry points", prof.entry_points, lambda e: f"{e['name']} [{e['kind']}] → {e['target']}")
    section("Containers", prof.containers, lambda c: f"{c['path']} [{c['kind']}]")
    section("Deployment", prof.deployment, lambda d: f"{d['path']} [{d['kind']}]")
    section("CI", prof.ci, lambda c: f"{c['path']} [{c['provider']}] {len(c.get('jobs') or [])} job(s)")
    section("Architecture configuration", prof.architecture_config, lambda a: f"{a['path']} [{a['tool']}]")
    section("Dependency-analysis tools", prof.dependency_tools, lambda t: f"{t['tool']}: {', '.join(t['evidence'])}")
    if prof.diagnostics:
        out.append("\nDiagnostics:")
        for d in prof.diagnostics:
            out.append(f"  {d.severity}: {d.message}")
    _write("\n".join(out), args.output)
    return EXIT_OK


def cmd_snapshot(args: argparse.Namespace) -> int:
    repo = _open(args)
    snap = repo.snapshot(args.rev)
    _write(json.dumps(snap.to_dict(), indent=None if args.compact else 1, default=str), args.output)
    return EXIT_OK


def _load_diff(args: argparse.Namespace) -> tuple[str, RepositoryDiff]:
    from .diff import diff_snapshots

    if args.base_snapshot or args.target_snapshot:
        if not (args.base_snapshot and args.target_snapshot):
            raise SystemExit("--base-snapshot and --target-snapshot must be used together")
        base = RepositorySnapshot.from_dict(json.loads(Path(args.base_snapshot).read_text()))
        target = RepositorySnapshot.from_dict(json.loads(Path(args.target_snapshot).read_text()))
        return "snapshot files", diff_snapshots(base, target)
    repo = _open(args)
    comp, diff = repo.compare(args.base, args.target, mode=args.mode, spec=args.spec)
    return comp.label, diff


def cmd_diff(args: argparse.Namespace) -> int:
    from .render import mermaid, views

    label, diff = _load_diff(args)
    if args.format == "json":
        text = json.dumps(diff.to_dict(), indent=1, default=str)
    elif args.format in ("mermaid", "markdown"):
        view = views.changes_view(diff, level=args.level, scope=args.scope, include_external=args.external,
                                  max_nodes=args.max_nodes, icons=mermaid.theme()["icons"])
        mm = mermaid.to_mermaid(view)
        text = mm if args.format == "mermaid" else format_diff_markdown(diff, label, mm)
    else:
        text = format_diff_text(diff, label)
    _write(text, args.output)
    reasons = gate(diff, args.fail_on or [])
    if reasons:
        print("repoviz: gate failed: " + "; ".join(reasons), file=sys.stderr)
        return EXIT_GATE
    return EXIT_OK


def cmd_mermaid(args: argparse.Namespace) -> int:
    from .flow import affected_flow
    from .render import mermaid, views

    icons = mermaid.theme()["icons"]
    repo = _open(args)
    if args.view == "changes" or args.view == "flow":
        comp, diff = repo.compare(args.base, args.target, mode=args.mode, spec=args.spec)
        if args.view == "changes":
            view = views.changes_view(diff, level=args.level, scope=args.scope, include_external=args.external,
                                      max_nodes=args.max_nodes, icons=icons)
        else:
            view = views.flow_view(affected_flow(diff).to_dict())
    else:
        snap = repo.snapshot(args.rev)
        focus = None
        if args.focus:
            match = [n for n in snap.nodes() if args.focus in (n.qualified_name, n.path, n.name, n.id)]
            if not match:
                print(f"repoviz: no node named {args.focus!r}", file=sys.stderr)
                return EXIT_USAGE
            focus = match[0].id
        if args.view == "dependencies":
            view = views.dependency_view(snap, level=args.level, include_external=args.external, focus=focus,
                                         depth=args.depth, max_nodes=args.max_nodes, icons=icons,
                                         relationships=args.relationships or ("imports", "depends-on"))
        else:
            view = views.structure_view(snap, root=focus, depth=args.depth, include_files=args.files,
                                        max_nodes=args.max_nodes, icons=icons)
    _write(mermaid.to_mermaid(view), args.output)
    if view.truncated:
        print(f"repoviz: {view.truncated} node(s) omitted (raise --max-nodes)", file=sys.stderr)
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    from .render.html import build_bundle, render_static_html

    repo = _open(args)
    comps = [] if args.no_default_comparisons else repo.default_comparisons()
    for spec in args.compare or []:
        comps.append(repo.resolve_comparison(spec=spec))
    bundle = build_bundle(repo, comparisons=comps, include_activity=not args.no_activity)
    compress = True if args.compress else (False if args.no_compress else None)
    html = render_static_html(bundle, compress=compress)
    output = args.output or "repoviz-report.html"
    _write(html, output)
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    repo = _open(args)
    serve(repo, host=args.host, port=args.port, open_browser=args.open, auto_session=args.session,
          allowed_hosts=args.allow_host)
    return EXIT_OK


def cmd_activity(args: argparse.Namespace) -> int:
    from .activity import observe

    repo = _open(args)
    report = observe(repo, use_session=not args.no_session)
    if args.json:
        from .render.html import to_jsonable

        report = dict(report)
        if not args.include_diff:
            report.pop("diff", None)
        _write(json.dumps(to_jsonable(report), indent=1, default=str), args.output)
        return EXIT_OK
    b = report["baseline"]
    s = report["summary"]
    out = [f"Baseline: {b['label']}",
           f"{s.get('files', 0)} file(s) changed, +{s.get('lines_added', 0)} −{s.get('lines_removed', 0)} lines; "
           f"{s.get('new_dependencies', 0)} new dependencies, {s.get('cycles_introduced', 0)} cycle(s) introduced"]
    for e in report["events"]:
        lines = "bin" if e.get("lines_added") is None else f"+{e['lines_added']} −{e['lines_removed']}"
        flags = [f for f, on in (("test", e["is_test"]), ("config", e["configuration_affected"])) if on]
        out.append(f"  {e['git_status']:<10} {e['path']}  [{e.get('owning_component_name') or '-'}]  {lines}  "
                   f"impact={e['impact_level']}" + (f"  ({', '.join(flags)})" if flags else "")
                   + (f"  tests affected: {len(e['tests_affected'])}" if e["tests_affected"] and not e["is_test"] else ""))
        for item in e["architecture_impact"]:
            if item.get("severity") != "none":
                out.append(f"      - {item['kind']}: {item['detail']}")
    flow = report.get("flow") or {}
    if flow.get("entry_points") or flow.get("tests"):
        out.append("\nEntry points / tests reaching the changes:")
        for ep in (flow.get("entry_points") or [])[:20] + (flow.get("tests") or [])[:20]:
            out.append(f"  {ep['name']} [{ep['kind']}] (distance {ep['distance']})")
    _write("\n".join(out), args.output)
    return EXIT_OK


def cmd_session(args: argparse.Namespace) -> int:
    repo = _open(args)
    if args.action == "start":
        if not repo.is_git:
            print("repoviz: sessions need a Git repository", file=sys.stderr)
            return EXIT_ERROR
        s = repo.state.start_session(repo.git, repo.root, label=args.label or "", allowed=args.allow,
                                     protected=args.protect)
        print(f"session {s.id} started at {s.started_at} (baseline {(s.baseline_head or 'empty')[:12]}, "
              f"{len(s.overrides)} dirty file(s) captured)")
        if s.allowed or s.protected:
            print(f"  scope: allowed {s.allowed or '(any)'}; protected {s.protected or '(none)'}")
    elif args.action == "scope":
        s = repo.current_session()
        if s is None:
            print("no active session", file=sys.stderr)
            return EXIT_ERROR
        s = repo.state.update_scope(s, args.allow or None, args.protect or None)
        print(f"session {s.id} scope: allowed {s.allowed or '(any)'}; protected {s.protected or '(none)'}")
    elif args.action == "end":
        s = repo.state.end_session(repo.git, repo.root)
        print(f"session {s.id} ended; review it later with: repoviz review session:{s.id}" if s else "no active session")
    elif args.action == "list":
        for s in repo.state.list_sessions():
            print(f"{s.id}  {s.started_at}  {'active' if s.active else 'ended ' + (s.ended_at or '')}  {s.label}")
    else:
        s = repo.current_session()
        if s is None:
            print("no active session")
        else:
            print(json.dumps(s.to_dict(), indent=2))
        print(f"state directory: {repo.state.dir}")
    return EXIT_OK


def cmd_review(args: argparse.Namespace) -> int:
    from .render.html import dumps
    from .review import (SEVERITY_ORDER, build_review, feedback_markdown, format_review_text, resolve_target,
                         review_targets, scope_for)

    repo = _open(args)
    if args.list:
        for t in review_targets(repo):
            print(f"{t.id:<40} {t.label}")
        return EXIT_OK
    try:
        target = resolve_target(repo, args.target, args.base, args.head_rev)
    except ValueError as exc:
        print(f"repoviz: {exc}", file=sys.stderr)
        return EXIT_ERROR
    report = build_review(repo, target, scope=scope_for(repo, target, args.allow, args.protect))
    notes = repo.state.load_notes(target.key)
    if args.format == "json":
        report["notes"] = notes
        _write(dumps(report), args.output)
    elif args.format == "prompt":
        _write(feedback_markdown(report, notes, min_severity=args.min_severity), args.output)
    elif args.format == "markdown":
        _write(format_review_markdown(report), args.output)
    else:
        _write(format_review_text(report), args.output)
    failed = []
    for cond in args.fail_on:
        if cond in SEVERITY_ORDER:
            n = sum(1 for f in report["findings"] if SEVERITY_ORDER[f["severity"]] <= SEVERITY_ORDER[cond])
            if n:
                failed.append(f"{n} finding(s) at or above {cond}")
        elif cond in ("protected", "out-of-scope"):
            key = "protected" if cond == "protected" else "out_of_scope"
            if report["summary"][key]:
                failed.append(f"{report['summary'][key]} {cond} file(s)")
        elif any(f["kind"] == cond for f in report["findings"]):
            failed.append(f"finding '{cond}' present")
    if failed:
        print("repoviz: review gate failed: " + "; ".join(failed), file=sys.stderr)
        return EXIT_GATE
    return EXIT_OK


def format_review_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [f"### AI change review: {report['target']['label']}", "",
             f"`{report['base']['label']}` → `{report['head']['label']}` · {s['files']} files · "
             f"{s['components']} components · +{s['lines_added']} −{s['lines_removed']} · "
             + (", ".join(f"{v} {k}" for k, v in s["findings"].items() if v) or "no findings"), ""]
    lines += ["| Component | Files | +/− | Scope | Findings |", "|---|---:|---:|---|---|"]
    for c in report["components"]:
        scope = ", ".join(f"{v} {k}" for k, v in c["scope"].items())
        found = ", ".join(f"{v} {k}" for k, v in c["findings"].items() if v) or "–"
        lines.append(f"| {c['name']} | {c['files']} | +{c['lines_added']} −{c['lines_removed']} | {scope} | {found} |")
    if report["findings"]:
        lines += ["", "| Severity | Finding | Location |", "|---|---|---|"]
        for f in report["findings"]:
            loc = f"`{f['path']}:{f['line']}`" if f.get("path") and f.get("line") else (f"`{f['path']}`" if f.get("path") else "")
            detail = str(f.get("detail", "")).replace("|", "\\|")
            lines.append(f"| {f['severity']} | **{f['title']}** — {detail} | {loc} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- parser


def _comparison_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("spec", nargs="?", help="comparison: a preset (" + ", ".join(k for k in PRESETS if k != "working")
                   + "), A..B, A...B (merge base of A and B vs B) or A (A vs working tree). Default: all")
    p.add_argument("--base", help="base revision (commit, branch, tag, HEAD, INDEX, SESSION, EMPTY)")
    p.add_argument("--target", help="target revision (default WORKTREE)")
    p.add_argument("--mode", choices=sorted(set(PRESETS) | {"merge-base"}),
                   help="preset comparison; merge-base uses --base (default: the default branch)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repoviz", description="Read-only repository architecture, change and activity "
                                     "visualizer (Mermaid diagrams, live web app and offline HTML reports).")
    parser.add_argument("--version", action="version", version=f"repoviz {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", "--repo", default=".", help="repository (or directory) to analyze (default: .)")
    common.add_argument("--config", help="explicit configuration file (TOML)")
    common.add_argument("--exclude", action="append", metavar="GLOB", help="additional exclude pattern (repeatable)")
    common.add_argument("--source-root", action="append", metavar="DIR", help="override source roots (repeatable)")
    common.add_argument("--no-grimp", action="store_true", help="never use grimp for the Python cross-check")
    common.add_argument("-o", "--output", help="write output to a file instead of stdout")
    common.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("discover", parents=[common], help="show what discovery found in the repository")
    p.add_argument("--rev", default="WORKTREE", help="revision to inspect (default: WORKTREE)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("snapshot", parents=[common], help="write the normalized graph of one revision as JSON")
    p.add_argument("--rev", default="WORKTREE", help="revision (commit, branch, tag, WORKTREE, INDEX, SESSION)")
    p.add_argument("--compact", action="store_true", help="no indentation")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("diff", parents=[common], help="compare two repository states")
    _comparison_args(p)
    p.add_argument("--format", choices=("text", "json", "markdown", "mermaid"), default="text")
    p.add_argument("--level", choices=("component", "package", "module", "project"), default="component",
                   help="aggregation level for the mermaid/markdown diagram")
    p.add_argument("--scope", choices=("changed", "neighbors", "all"), default="neighbors")
    p.add_argument("--external", action="store_true", help="include external packages in diagrams")
    p.add_argument("--max-nodes", type=int, default=150)
    p.add_argument("--fail-on", action="append", choices=FAIL_CONDITIONS, metavar="CONDITION",
                   help="exit with status 3 when the condition holds (" + ", ".join(FAIL_CONDITIONS) + "); repeatable")
    p.add_argument("--base-snapshot", help="compare two saved snapshot files instead of revisions")
    p.add_argument("--target-snapshot")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("mermaid", parents=[common], help="print a Mermaid diagram")
    p.add_argument("--view", choices=("changes", "dependencies", "structure", "flow"), default="dependencies")
    _comparison_args(p)
    p.add_argument("--rev", default="WORKTREE", help="revision for dependencies/structure views")
    p.add_argument("--level", choices=("component", "package", "module", "project"), default="component")
    p.add_argument("--scope", choices=("changed", "neighbors", "all"), default="neighbors")
    p.add_argument("--relationships", nargs="+", metavar="REL", help="relationships to include (imports, depends-on, "
                   "calls, invokes, builds)")
    p.add_argument("--focus", help="node (qualified name or path) to focus on / structure root")
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--files", action="store_true", help="structure view: include modules/files")
    p.add_argument("--external", action="store_true")
    p.add_argument("--max-nodes", type=int, default=200)
    p.set_defaults(func=cmd_mermaid)

    p = sub.add_parser("report", parents=[common], help="write a self-contained interactive HTML report")
    p.add_argument("--compare", action="append", metavar="SPEC",
                   help="additional comparison to precompute (preset, A..B, A...B); repeatable")
    p.add_argument("--no-default-comparisons", action="store_true",
                   help="only include comparisons given with --compare")
    p.add_argument("--no-activity", action="store_true", help="omit the activity tab data")
    p.add_argument("--compress", action="store_true", help="always gzip the embedded data")
    p.add_argument("--no-compress", action="store_true", help="never gzip the embedded data")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("serve", parents=[common], help="run the live web application")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765, help="port (0 = pick a free port)")
    p.add_argument("--open", action="store_true", help="open a browser")
    p.add_argument("--session", action="store_true", help="start a work session if none is active")
    p.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                   help="additional Host header value to accept (when binding a non-loopback address)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("activity", parents=[common], help="show files currently being modified and their impact")
    p.add_argument("--json", action="store_true")
    p.add_argument("--include-diff", action="store_true", help="include the full diff in --json output")
    p.add_argument("--no-session", action="store_true", help="compare with HEAD even if a session is active")
    p.set_defaults(func=cmd_activity)

    p = sub.add_parser("session", parents=[common], help="manage work sessions (waves of agent work)")
    p.add_argument("action", choices=("start", "status", "scope", "end", "list"))
    p.add_argument("--label", help="label for a new session (e.g. the feature or wave name)")
    p.add_argument("--allow", action="append", default=[], metavar="GLOB",
                   help="paths the agent may change in this session (repeatable)")
    p.add_argument("--protect", action="append", default=[], metavar="GLOB",
                   help="paths the agent must not change in this session (repeatable)")
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("review", parents=[common], help="review what an AI agent changed (scope, findings, feedback)")
    p.add_argument("target", nargs="?", help="what to review: session (current), session:<id> (a past wave), all, "
                   "branch, last-commit, or a comparison such as main...HEAD. Default: the current session, else "
                   "uncommitted changes")
    p.add_argument("--base", help="custom base revision")
    p.add_argument("--head", dest="head_rev", help="custom target revision")
    p.add_argument("--allow", action="append", default=[], metavar="GLOB", help="additional allowed path (repeatable)")
    p.add_argument("--protect", action="append", default=[], metavar="GLOB",
                   help="additional protected path (repeatable)")
    p.add_argument("--format", choices=("text", "json", "markdown", "prompt"), default="text",
                   help="prompt = feedback for the agent (reviewer notes + automated signals)")
    p.add_argument("--min-severity", choices=("high", "medium", "low", "info"), default="medium",
                   help="lowest severity of automated signals included in --format prompt")
    p.add_argument("--list", action="store_true", help="list reviewable targets (sessions, waves, presets)")
    p.add_argument("--fail-on", action="append", default=[], metavar="CONDITION",
                   help="exit 3 when: high | medium | low (findings at or above), protected, out-of-scope, "
                   "or a finding kind such as new-cycle; repeatable")
    p.set_defaults(func=cmd_review)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    try:
        return int(args.func(args) or 0)
    except (RepositoryError, GitError, ConfigError) as exc:
        print(f"repoviz: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:  # pragma: no cover - e.g. piping into head
        return EXIT_OK
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
