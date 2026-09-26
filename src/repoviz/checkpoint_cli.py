"""``repoviz session checkpoint`` and ``repoviz session note``, light enough for an agent hook after every edit.

Both work from an agent hook's JSON on stdin (``--hook-input``: Claude Code's ``PostToolUse`` payload with
``tool_name``, ``tool_input.file_path`` and ``cwd``) and stay silent with ``--quiet``: a hook must never get in the
agent's way, so without an active session they do nothing and exit 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import checkpoints
from .config import ConfigError, load_config
from .gitutil import Git, probe_repository
from .session import StateStore


def hook_payload() -> dict[str, Any]:
    try:
        data = json.loads(sys.stdin.read(1_000_000) or "{}")
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repoviz session", add_help=False)
    p.add_argument("-C", "--repo", default=".")
    p.add_argument("--config")
    p.add_argument("action", choices=("checkpoint", "note"))
    p.add_argument("--label", default="")
    p.add_argument("--tool", default="")
    p.add_argument("--file", default="")
    p.add_argument("--message", default="")
    p.add_argument("--hook-input", action="store_true")
    p.add_argument("--quiet", action="store_true")
    return p


def fast_main(argv: list[str]) -> int | None:
    """Run ``session checkpoint|note``; ``None`` when ``argv`` has options only the full CLI knows."""
    try:
        args, rest = parser().parse_known_args(argv[1:])
    except SystemExit:
        return None
    if rest:
        return None
    return run(args)


def run(args: argparse.Namespace) -> int:
    def say(text: str, error: bool = False) -> None:
        if not args.quiet:
            print(text, file=sys.stderr if error else sys.stdout)

    hook = hook_payload() if args.hook_input else {}
    where = args.repo
    if where == "." and isinstance(hook.get("cwd"), str) and hook["cwd"]:
        where = hook["cwd"]
    root, reason = probe_repository(where)
    if root is None:
        say(f"repoviz: checkpoints need a Git repository ({reason})", error=True)
        return 0 if args.quiet else 1
    try:
        config = load_config(root, args.config)
    except ConfigError as exc:
        say(f"repoviz: {exc}", error=True)
        return 0 if args.quiet else 1
    state = StateStore(root, root.name, config.state_dir)
    session = state.current_session()
    if session is None or not session.active:
        say("no active session: start one with 'repoviz session start'")
        return 0
    tool = args.tool or str(hook.get("tool_name") or "")
    tool_input = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
    file = args.file or str(tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or "")
    if file:
        try:
            file = Path(file).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            pass
    if args.action == "note":
        checkpoints.note(state, session, tool=tool, file=file, message=args.message)
        say("note added to the session timeline")
        return 0
    label = args.label or " ".join(x for x in (tool, file) if x)
    meta, created = checkpoints.create(state, Git(root), root, session, label=label,
                                       origin="hook" if args.hook_input else "manual",
                                       max_checkpoints=config.max_checkpoints)
    say(checkpoints.describe(meta, created))
    return 0
