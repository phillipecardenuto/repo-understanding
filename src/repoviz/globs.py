"""Small, dependency-free glob matcher with ``**`` support.

Semantics (close to ``.gitignore``):

* ``*`` matches within one path segment, ``?`` one character, ``**`` any number
  of segments (including none).
* A pattern without ``/`` matches a file or directory name at any depth, and
  everything beneath a matching directory (``node_modules`` excludes the whole
  tree).
* A trailing ``/`` restricts the pattern to directories (and their contents).
* A leading ``/`` anchors the pattern at the repository root.
"""

from __future__ import annotations

import functools
import re


_REPEATED_GLOBSTAR = re.compile(r"(?:\*\*/)+(?:\*\*(?=/|$))?")


@functools.lru_cache(maxsize=4096)
def _compile(pattern: str, subtree: bool = True) -> re.Pattern[str]:
    try:
        return _compile_unsafe(pattern, subtree)
    except re.error:  # e.g. a malformed character class: match the pattern literally
        return re.compile("^" + re.escape(pattern.strip().strip("/")) + ("(?:/.*)?" if subtree else "") + "$")


def _compile_unsafe(pattern: str, subtree: bool) -> re.Pattern[str]:
    # "**/**/x" means the same as "**/x"; collapsing it keeps matching linear.
    pat = _REPEATED_GLOBSTAR.sub(lambda m: "**/" if m.group(0).endswith("/") else "**", pattern.strip()[:1000])
    dir_only = pat.endswith("/")
    pat = pat.strip("/") if pat.startswith("/") else pat.rstrip("/")
    anchored = pattern.strip().startswith("/") or "/" in pat
    out: list[str] = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                i += 2
                if i < len(pat) and pat[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pat[i + 1:j].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    body = "".join(out)
    prefix = "" if anchored else "(?:.*/)?"
    # Matching a directory matches everything beneath it.
    suffix = "/.+" if dir_only else "(?:/.*)?"
    if not subtree:
        suffix = ""
    return re.compile(f"^{prefix}{body}{suffix}$", re.DOTALL)


def match(path: str, pattern: str) -> bool:
    if not pattern:
        return False
    return _compile(pattern).match(path.strip("/")) is not None


def match_exact(path: str, pattern: str) -> bool:
    """Like :func:`match` but the pattern must match ``path`` itself, not an ancestor."""
    if not pattern:
        return False
    return _compile(pattern.rstrip("/") or pattern, False).match(path.strip("/")) is not None


def match_any(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(match(path, p) for p in patterns)


def match_dir(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    """True when directory ``path`` (and therefore its whole subtree) matches."""
    path = path.strip("/")
    for p in patterns:
        if match(path, p) or (p.strip().endswith("/") and match(path + "/x", p)):
            return True
    return False
