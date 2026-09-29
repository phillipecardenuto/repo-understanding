"""Text scanning helpers for the lightweight language analyzers (Java, Kotlin, and the other C-like languages).

Nothing here parses a language fully: comments and string contents are blanked with one regular expression per
language (the scan runs in C, so large repositories stay fast), and braces give the nesting.  Offsets and newlines
are kept, so a position in the masked text is the same position in the source.
"""

from __future__ import annotations

import bisect
import re
from typing import Iterable, Pattern

#: Java and Kotlin: line and block comments, text blocks / raw strings, strings and characters.
JVM_TOKENS = re.compile(r'//[^\n]*|/\*.*?(?:\*/|\Z)|""".*?(?:"""|\Z)|"(?:\\.|[^"\\\n])*"?|\'(?:\\.|[^\'\\\n])*\'?',
                        re.S)


def _blank(s: str) -> str:
    return " " * len(s) if "\n" not in s else "".join("\n" if c == "\n" else " " for c in s)


def mask_pair(text: str, tokens: Pattern[str] = JVM_TOKENS) -> tuple[str, str]:
    """``(code, nocomment)``: ``code`` has comments and the insides of literals blanked (a string keeps its quote
    characters), ``nocomment`` only has comments blanked.  Both keep every offset and newline."""
    code: list[str] = []
    noc: list[str] = []
    last = 0
    for m in tokens.finditer(text):
        a, s = m.start(), m.group()
        if a > last:
            chunk = text[last:a]
            code.append(chunk)
            noc.append(chunk)
        if s[0] == "/" and len(s) > 1 and s[1] in "/*":  # a comment
            blank = _blank(s)
            code.append(blank)
            noc.append(blank)
        else:
            q = 3 if s.startswith(('"""', "'''")) else 1
            inner = s[q:-q] if len(s) >= 2 * q and s.endswith(s[:q]) else s[q:]
            code.append(s[:q] + _blank(inner) + s[q + len(inner):])
            noc.append(s)
        last = m.end()
    if last < len(text):
        code.append(text[last:])
        noc.append(text[last:])
    return "".join(code), "".join(noc)


class LineIndex:
    """1-based line numbers of offsets."""

    def __init__(self, text: str) -> None:
        self.starts = [m.end() for m in re.finditer("\n", text)]

    def line(self, pos: int) -> int:
        return bisect.bisect_right(self.starts, pos) + 1


def match_close(code: str, open_pos: int, pairs: str = "{}") -> int:
    """Offset of the bracket closing the one at ``open_pos`` (the end of the text when unbalanced)."""
    op, cl = pairs[0], pairs[1]
    depth = 0
    for k in range(open_pos, len(code)):
        ch = code[k]
        if ch == op:
            depth += 1
        elif ch == cl:
            depth -= 1
            if depth == 0:
                return k
    return len(code) - 1


def match_open(code: str, close_pos: int, pairs: str = "()") -> int:
    """Offset of the bracket opening the one at ``close_pos`` (-1 when unbalanced)."""
    op, cl = pairs[0], pairs[1]
    depth = 0
    for k in range(close_pos, -1, -1):
        ch = code[k]
        if ch == cl:
            depth += 1
        elif ch == op:
            depth -= 1
            if depth == 0:
                return k
    return -1


def top_level(text: str, chars: str = ",=") -> bool:
    """Whether one of ``chars`` appears outside every (), <> and [] pair of ``text``."""
    depth = 0
    for ch in text:
        if ch in "(<[":
            depth += 1
        elif ch in ")>]":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in chars:
            return True
    return False


def squash(text: str) -> str:
    """Whitespace runs as one space (for signatures and semantic fingerprints)."""
    return " ".join(text.split())


def package_roots(packages: Iterable[str]) -> set[str]:
    """The repository's own package roots: per first three segments, the longest prefix all its packages share
    (``org.apache.commons.lang3`` for commons-lang, so ``org.apache.commons.io`` stays external; Guava's
    ``com.google.common`` and ``com.google.thirdparty…`` leave ``com.google.errorprone`` external)."""
    groups: dict[str, list[list[str]]] = {}
    for p in packages:
        segs = p.split(".")
        if len(segs) >= 2:
            groups.setdefault(".".join(segs[:3]), []).append(segs)
    out = set()
    for members in groups.values():
        common = members[0]
        for segs in members[1:]:
            n = 0
            while n < min(len(common), len(segs)) and common[n] == segs[n]:
                n += 1
            common = common[:n]
        out.add(".".join(common))
    return out
