"""Minimal YAML reader.

Uses PyYAML's ``safe_load`` when it is installed.  Otherwise falls back to a
small parser that understands the subset of YAML found in compose files, CI
pipelines, workspace files and Kubernetes manifests: block mappings and
sequences, flow collections, quoted and plain scalars, block scalars and
comments.  Anchors, tags and multi-line flow collections are not supported;
unparseable input raises :class:`YamlError`.
"""

from __future__ import annotations

import re
from typing import Any

try:  # pragma: no cover - depends on the environment
    import yaml as _pyyaml  # type: ignore[import-untyped]
except Exception:  # pragma: no cover
    _pyyaml = None


class YamlError(ValueError):
    pass


def safe_load(text: str, *, force_builtin: bool = False) -> Any:
    docs = safe_load_all(text, force_builtin=force_builtin)
    return docs[0] if docs else None


def safe_load_all(text: str, *, force_builtin: bool = False) -> list[Any]:
    if _pyyaml is not None and not force_builtin:
        try:
            return [d for d in _pyyaml.safe_load_all(text)]
        except Exception as exc:  # pragma: no cover - depends on PyYAML
            raise YamlError(str(exc)) from exc
    docs: list[Any] = []
    for chunk in re.split(r"^---[ \t]*(?:#.*)?$", text, flags=re.M):
        lines = _prepare(chunk)
        if not lines:
            continue
        parser = _Parser(lines)
        docs.append(parser.parse_block(lines[0][0]))
    return docs


def _strip_comment(line: str) -> str:
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _prepare(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    raw = text.splitlines()
    i = 0
    while i < len(raw):
        line = raw[i]
        i += 1
        if line.strip().startswith("%") or line.strip() == "...":
            continue
        stripped = _strip_comment(line)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.strip()
        # Block scalars: fold following, more indented lines into one string.
        m = re.search(r"(:\s*|^-\s*)([|>])[-+0-9]*$", content)
        if m:
            block: list[str] = []
            while i < len(raw):
                nxt = raw[i]
                if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= indent:
                    break
                block.append(nxt.strip())
                i += 1
            joiner = "\n" if m.group(2) == "|" else " "
            content = content[: m.start(2)] + _quote(joiner.join(block).strip())
        out.append((indent, content))
    return out


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


_KEY_RE = re.compile(r"""^(?P<key>"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^'"\s][^:]*?)\s*:(?:\s+(?P<value>.*)|$)""")


class _Parser:
    def __init__(self, lines: list[tuple[int, str]]) -> None:
        self.lines = lines
        self.pos = 0

    def peek(self) -> tuple[int, str] | None:
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def parse_block(self, indent: int) -> Any:
        line = self.peek()
        if line is None:
            return None
        if line[1] == "-" or line[1].startswith("- "):
            return self.parse_seq(line[0])
        if _KEY_RE.match(line[1]):
            return self.parse_map(line[0])
        self.pos += 1
        return _scalar(line[1])

    def parse_seq(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while True:
            line = self.peek()
            if line is None or line[0] != indent or not (line[1] == "-" or line[1].startswith("- ")):
                if line is not None and line[0] > indent:
                    raise YamlError(f"unexpected indentation: {line[1]!r}")
                return items
            self.pos += 1
            rest = line[1][1:].strip()
            if not rest:
                nxt = self.peek()
                items.append(self.parse_block(nxt[0]) if nxt and nxt[0] > indent else None)
                continue
            if _KEY_RE.match(rest) and not rest.startswith(("{", "[")):
                # "- key: value" starts a mapping whose other keys are indented further.
                child_indent = indent + 1 + (len(line[1]) - len(line[1][1:].lstrip()) - 1)
                self.lines.insert(self.pos, (child_indent, rest))
                items.append(self.parse_map(child_indent))
                continue
            items.append(_scalar(rest))

    def parse_map(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            line = self.peek()
            if line is None or line[0] < indent:
                return result
            if line[0] > indent:
                raise YamlError(f"unexpected indentation: {line[1]!r}")
            m = _KEY_RE.match(line[1])
            if not m:
                if line[1].startswith("-"):
                    return result
                raise YamlError(f"expected 'key: value', got {line[1]!r}")
            self.pos += 1
            key = _unquote(m.group("key").strip())
            value = m.group("value")
            if value is not None and value.strip():
                result[key] = _scalar(value.strip())
                continue
            nxt = self.peek()
            if nxt is None:
                result[key] = None
            elif nxt[0] > indent:
                result[key] = self.parse_block(nxt[0])
            elif nxt[0] == indent and (nxt[1] == "-" or nxt[1].startswith("- ")):
                result[key] = self.parse_seq(indent)
            else:
                result[key] = None


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return bytes(s[1:-1], "utf-8").decode("unicode_escape") if "\\" in s else s[1:-1]
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    return s


def _split_flow(body: str) -> list[str]:
    parts, depth, cur, quote = [], 0, [], ""
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _scalar(s: str) -> Any:
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        return [_scalar(p) for p in _split_flow(s[1:-1])]
    if s.startswith("{") and s.endswith("}"):
        out: dict[str, Any] = {}
        for part in _split_flow(s[1:-1]):
            k, _, v = part.partition(":")
            out[_unquote(k.strip())] = _scalar(v) if v.strip() else None
        return out
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return _unquote(s)
    if s.startswith(("&", "*", "!")):
        s = s.split(" ", 1)[1] if " " in s else ""
        return _scalar(s) if s else None
    low = s.lower()
    if low in ("null", "~", ""):
        return None
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"[-+]?\d+", s):
        return int(s)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?", s):
        return float(s)
    return s
