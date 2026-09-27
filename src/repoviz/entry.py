"""The ``repoviz`` command.

``repoviz session checkpoint`` and ``repoviz session note`` run after every edit when an agent hook calls them,
so they are answered without importing the analysis code (about a third of the start-up time).  Everything
else goes to :mod:`repoviz.cli`.
"""

from __future__ import annotations

import sys


def _session_action(args: list[str]) -> str | None:
    """The action after ``session`` (``start``, ``checkpoint``…), skipping options and the values they take:
    ``session start --label note`` is a start, not a note."""
    rest = iter(args[1:])
    for a in rest:
        if a in ("-C", "--repo", "--config"):
            next(rest, None)
        elif not a.startswith("-"):
            return a
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["session"] and _session_action(args) in ("checkpoint", "note"):
        from .checkpoint_cli import fast_main

        code = fast_main(args)
        if code is not None:
            return code
    from .cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
