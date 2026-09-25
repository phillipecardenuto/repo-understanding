"""Constant and configuration values, before → after.

Agents tweak limits and flags to make things pass (``MAX_IMAGES = 20 → 200``, ``VERIFY_SSL = True → False``).  A
review lists each changed value in the file's key changes, and flags a safety setting switched the risky way:

* **Python.** Module-level UPPER_CASE names bound to a literal (``NAME = 20``, ``NAME: int = 20``), and the literal
  defaults of settings classes (a subclass of ``BaseSettings`` or ``…Settings``, a dataclass named ``…Settings`` /
  ``…Config``): ``Settings.debug``.
* **JavaScript / TypeScript.** ``export const name = literal`` and top-level ``const UPPER_NAME = literal``.
* **Configuration files** under config-like paths (``config/``, ``settings/``, ``*.config.*``, ``settings.*``) and
  ``.env.example``: the scalar keys of TOML, JSON and YAML files (top level and one section down) and dotenv lines.

Values are parsed, never evaluated: ``ast.literal_eval`` on the syntax tree, ``json``, ``tomllib`` and the YAML
subset.  They are shown at most :data:`MAX_VALUE` characters long and redacted; a value whose name looks secret
(``API_KEY``, ``password``…) is shown as ``•••``.  Snapshots are not involved: a review reads both versions of each
changed file anyway.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
import sys
from dataclasses import dataclass
from typing import Any

from . import yamlish
from .ids import stable_hash
from .redact import redact

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

MAX_VALUE = 120
MAX_VALUES = 500  # per file
MAX_TEXT = 500_000  # larger files are not inspected
HIDDEN = "•••"
_UPPER = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
_SECRET_NAME = re.compile(r"(?i)(secret|passw|token|api[_-]?key|private[_-]?key|credential|access[_-]?key|auth[_-]?key)")
_QUANTITY = re.compile(r"(?i)(max|min|limit|size|count|len|ttl|timeout|expir|lifetime|retries|window|budget)")
_MISSING = object()


@dataclass
class Value:
    name: str
    kind: str  # constant | setting
    display: str
    fingerprint: str
    line: int | None
    parsed: Any = _MISSING  # the literal, for the safety rules (never for secret-looking names)


def _show(name: str, text: str, parsed: Any) -> tuple[str, bool]:
    """What a report may show of a value, and whether the value itself may be looked at.

    A secret-looking name hides its value, unless it is a flag or a quantity (``MAX_TOKENS = 4096``).
    """
    if _SECRET_NAME.search(name):
        number = isinstance(parsed, (int, float)) and not isinstance(parsed, bool)
        if not (parsed is None or isinstance(parsed, bool) or (number and _QUANTITY.search(name))):
            return HIDDEN, False
    text = redact(" ".join(text.split()))
    return (text if len(text) <= MAX_VALUE else text[:MAX_VALUE - 1] + "…"), True


def _value(name: str, kind: str, text: str, line: int | None, parsed: Any) -> Value:
    display, visible = _show(name, text, parsed)
    return Value(name, kind, display, stable_hash("value", text, length=16), line, parsed if visible else _MISSING)


# --------------------------------------------------------------------------- Python

def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)  # literals only: never calls, names or attribute access
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return _MISSING


def _base_names(cls: ast.ClassDef) -> list[str]:
    out = []
    for b in cls.bases:
        if isinstance(b, ast.Name):
            out.append(b.id)
        elif isinstance(b, ast.Attribute):
            out.append(b.attr)
    return out


def _is_settings_class(cls: ast.ClassDef) -> bool:
    bases = _base_names(cls)
    if any(b == "BaseSettings" or b.endswith("Settings") for b in bases):
        return True
    decorated = any((isinstance(d, ast.Name) and d.id == "dataclass") or
                    (isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "dataclass")
                    for d in cls.decorator_list)
    return cls.name.endswith(("Settings", "Config")) and (decorated or "BaseModel" in bases)


def _python_values(text: str) -> dict[str, Value]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return {}
    out: dict[str, Value] = {}

    def take(target: ast.AST, value: ast.AST | None, prefix: str, kind: str, any_case: bool) -> None:
        if value is None or not isinstance(target, ast.Name) or len(out) >= MAX_VALUES:
            return
        name = target.id
        if not any_case and not _UPPER.match(name) or name.startswith("__"):
            return
        parsed = _literal(value)
        if parsed is _MISSING:
            return
        full = prefix + name
        out[full] = _value(full, kind, ast.unparse(value), getattr(target, "lineno", None), parsed)

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            take(stmt.targets[0], stmt.value, "", "constant", False)
        elif isinstance(stmt, ast.AnnAssign):
            take(stmt.target, stmt.value, "", "constant", False)
        elif isinstance(stmt, ast.ClassDef) and _is_settings_class(stmt):
            for inner in stmt.body:
                if isinstance(inner, ast.AnnAssign):
                    take(inner.target, inner.value, stmt.name + ".", "setting", True)
                elif isinstance(inner, ast.Assign) and len(inner.targets) == 1:
                    take(inner.targets[0], inner.value, stmt.name + ".", "setting", True)
    return out


# --------------------------------------------------------------------------- JavaScript / TypeScript

_JS_CONST = re.compile(r"^(export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=]{1,80})?=\s*(.{1,400})$", re.M)
_JS_NUMBER = re.compile(r"^-?(?:0[xX][0-9a-fA-F_]+|(?:\d[\d_]*)?\.?\d[\d_]*(?:[eE][+-]?\d+)?)$")


def _js_literal(text: str) -> Any:
    t = text.strip()
    if t in ("true", "false"):
        return t == "true"
    if t in ("null", "undefined"):
        return None
    if _JS_NUMBER.match(t):
        try:
            clean = t.replace("_", "")
            return int(clean, 16) if clean.lower().lstrip("-").startswith("0x") else float(clean) if any(
                c in clean for c in ".eE") else int(clean)
        except ValueError:
            return _MISSING
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"`" and t[0] not in t[1:-1].replace("\\" + t[0], ""):
        if t[0] == "`" and "${" in t:
            return _MISSING
        return t[1:-1]
    return _MISSING


def _js_code(rest: str) -> str:
    """The rest of a line without its trailing ``;`` and ``// comment`` (a ``//`` inside a string stays)."""
    quote = ""
    for i, c in enumerate(rest):
        if quote:
            if c == "\\":
                continue
            if c == quote and rest[i - 1] != "\\":
                quote = ""
        elif c in "'\"`":
            quote = c
        elif rest.startswith("//", i):
            rest = rest[:i]
            break
    return rest.strip().rstrip(";").strip()


def _js_values(text: str) -> dict[str, Value]:
    out: dict[str, Value] = {}
    for m in _JS_CONST.finditer(text):
        exported, name, raw = bool(m.group(1)), m.group(2), _js_code(m.group(3))
        if not exported and not _UPPER.match(name):
            continue
        parsed = _js_literal(raw)
        if parsed is _MISSING:
            continue
        line = text.count("\n", 0, m.start()) + 1
        out[name] = _value(name, "constant", raw.strip(), line, parsed)
        if len(out) >= MAX_VALUES:
            break
    return out


# --------------------------------------------------------------------------- configuration files

_ENV_FILES = (".env.example", ".env.sample", ".env.template", ".env.dist")
_CONFIG_DIRS = {"config", "configs", "conf", "settings"}


def config_format(path: str) -> str | None:
    """``toml`` / ``json`` / ``yaml`` / ``dotenv`` for a configuration file whose values are worth listing."""
    base = posixpath.basename(path).lower()
    if base in _ENV_FILES:
        return "dotenv"
    ext = posixpath.splitext(base)[1]
    fmt = {".toml": "toml", ".json": "json", ".yaml": "yaml", ".yml": "yaml"}.get(ext)
    if fmt is None:
        return None
    dirs = set(path.lower().split("/")[:-1])
    if dirs & _CONFIG_DIRS or ".config." in base or base.startswith(("config.", "settings.", "appsettings")):
        return fmt
    return None


def _config_values(text: str, fmt: str) -> dict[str, Value]:
    out: dict[str, Value] = {}
    if fmt == "dotenv":
        for i, line in enumerate(text.splitlines(), 1):
            m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m and len(out) < MAX_VALUES:
                raw = m.group(2)
                if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
                    raw = raw[1:-1]
                low = raw.lower()
                parsed: Any = True if low in ("true", "yes", "on") else False if low in ("false", "no", "off") else (
                    int(raw) if re.fullmatch(r"-?\d{1,18}", raw) else raw)
                out[m.group(1)] = _value(m.group(1), "setting", raw, i, parsed)
        return out
    try:
        data = tomllib.loads(text) if fmt == "toml" else json.loads(text) if fmt == "json" else yamlish.safe_load(text)
    except Exception:  # noqa: BLE001 - a file that does not parse has no values to list
        return {}
    if not isinstance(data, dict):
        return {}
    lines = text.splitlines()

    def line_of(key: str) -> int | None:
        pat = re.compile(r'^\s*["\']?' + re.escape(key) + r'["\']?\s*[:=]')
        return next((i for i, x in enumerate(lines, 1) if pat.match(x)), None)

    def scalar(v: Any) -> bool:
        return v is None or isinstance(v, (str, int, float, bool)) or (
            isinstance(v, list) and len(v) <= 20 and all(isinstance(x, (str, int, float, bool)) for x in v))

    for key, v in data.items():
        if len(out) >= MAX_VALUES:
            break
        if scalar(v):
            out[str(key)] = _value(str(key), "setting", json.dumps(v, ensure_ascii=False), line_of(str(key)), v)
        elif isinstance(v, dict):  # one section down: [server] port = 80 → server.port
            for sub, w in v.items():
                if scalar(w) and len(out) < MAX_VALUES:
                    name = f"{key}.{sub}"
                    out[name] = _value(name, "setting", json.dumps(w, ensure_ascii=False), line_of(str(sub)), w)
    return out


# --------------------------------------------------------------------------- one file, two versions

def values_of(path: str, text: str | None, language: str | None) -> dict[str, Value]:
    if not text or len(text) > MAX_TEXT:
        return {}
    if language == "python":
        return _python_values(text)
    if language in ("javascript", "typescript"):
        return _js_values(text)
    fmt = config_format(path)
    return _config_values(text, fmt) if fmt else {}


def value_changes(path: str, before: str | None, after: str | None, language: str | None) -> list[dict[str, Any]]:
    """The values that were added, removed or changed between two versions of a file."""
    old, new = values_of(path, before, language), values_of(path, after, language)
    out: list[dict[str, Any]] = []
    for name in sorted(set(old) | set(new), key=lambda n: ((new.get(n) or old[n]).line or 0, n)):
        a, b = old.get(name), new.get(name)
        if a is not None and b is not None and a.fingerprint == b.fingerprint:
            continue
        cur = b or a
        item: dict[str, Any] = {"name": name, "kind": cur.kind, "line": cur.line,
                                "status": "added" if a is None else "removed" if b is None else "modified",
                                "value_before": a.display if a else None, "value": b.display if b else None}
        rule = _weakened(name, a, b)
        if rule:
            item["weakens"] = rule
        out.append(item)
    return out


# --------------------------------------------------------------------------- safety flags

def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "1", "yes", "on"))


def _falsy(v: Any) -> bool:
    return v is False or (isinstance(v, str) and v.strip().lower() in ("false", "0", "no", "off"))


def _wildcard(v: Any) -> bool:
    if isinstance(v, str):
        return "*" in (x.strip() for x in v.split(","))
    return isinstance(v, (list, tuple, set)) and "*" in v


def _no_timeout(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("0", "none", "null")
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0)


#: Well-known safety settings and the risky way to switch them: (name pattern on the last part of the name,
#: test on the new value, what it means).  A change is flagged when the new value is risky and the old one was not.
SAFETY_FLAGS: list[tuple[re.Pattern[str], Any, str]] = [
    (re.compile(r"(?i)^(debug|[a-z0-9_]*_debug)$"), _truthy, "debug mode switched on"),
    (re.compile(r"(?i)((^|_)(ssl|tls)(_|$)|check_certs?|cert_reqs)"), _falsy, "TLS or certificate verification switched off"),
    (re.compile(r"(?i)(^|_)verify(_|$)|verification"), _falsy, "verification switched off"),
    (re.compile(r"(?i)allow_all"), _truthy, "allow-all switched on"),
    (re.compile(r"(?i)(^|_)timeout$"), _no_timeout, "timeout removed (0 or none)"),
    (re.compile(r"(?i)(cors|allowed_origins|allowed_hosts|allow_origins)"), _wildcard, "any origin or host allowed (*)"),
    (re.compile(r"(?i)(^|_)(csrf|auth|secure|rate_limit)(_|$)"), _falsy, "a security check switched off"),
]


def _weakened(name: str, before: Value | None, after: Value | None) -> str | None:
    if after is None or after.parsed is _MISSING:
        return None
    leaf = name.rsplit(".", 1)[-1]
    for pattern, risky, meaning in SAFETY_FLAGS:
        if pattern.search(leaf) and risky(after.parsed):
            if before is not None and before.parsed is not _MISSING and risky(before.parsed):
                return None  # it already was
            return meaning
    return None
