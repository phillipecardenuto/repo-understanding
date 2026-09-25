"""JavaScript / TypeScript analyzer (no Node.js required).

A small position-preserving lexer masks comments, string, template and regular
expression literals so that imports, declarations and call sites can be found
with regular expressions without being fooled by commented-out code or string
contents.  Module specifiers are resolved like bundlers do: relative paths with
extension and ``index`` probing (including the TypeScript ``./x.js`` ->
``x.ts`` convention), ``tsconfig.json`` ``baseUrl``/``paths``, and workspace
packages (by ``package.json`` name, preferring source over build output).

Symbol and call extraction is heuristic (``confidence`` < 1) but the import
graph is precise for static ``import``/``export ... from``/``require``.
"""

from __future__ import annotations

import dataclasses
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from .. import classify
from ..ids import stable_hash
from ..model import CATEGORY_MODULE, CATEGORY_SYMBOL, REL_IMPORTS, ComponentNode
from .base import (
    CAP_CALLS,
    CAP_DEPENDENCIES,
    CAP_DIAGNOSTICS,
    CAP_EVIDENCE,
    CAP_MODULES,
    CAP_SYMBOLS,
    AnalysisContext,
    Analyzer,
    Detection,
    SnapshotBuilder,
    parse_parallel,
)
from .callflow import ModuleScope, RawCall, get_index

JS_LANGS = ("javascript", "typescript", "vue", "svelte")
SOURCE_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")
PROBE_EXTS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte", ".json")
NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants", "crypto", "dgram",
    "diagnostics_channel", "dns", "domain", "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring", "readline", "repl", "stream", "string_decoder",
    "sys", "timers", "tls", "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib", "test",
}
KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "typeof", "delete", "void", "await",
            "yield", "super", "import", "export", "class", "constructor", "else", "do", "try", "in", "of",
            "instanceof", "case", "throw", "with", "new", "get", "set", "static", "async", "let", "const", "var"}
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KEYWORDS = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw", "yield",
                   "await"}


# --------------------------------------------------------------------------
# Lexing
# --------------------------------------------------------------------------


def _parse_js_item(item: tuple[str, str]) -> tuple[str, "JsFileInfo"]:
    path, text = item
    return path, parse_js(text)


def mask(text: str) -> tuple[str, str]:
    """Return ``(code, nocomment)``.

    ``code`` has comments and the *contents* of literals replaced by spaces;
    ``nocomment`` only has comments blanked.  Newlines and offsets are kept.
    """
    n = len(text)
    code = list(text)
    noc = list(text)
    i = 0
    stack: list[int] = []  # brace depth at which each open template expression started
    depth = 0
    last_sig = ""  # last significant character (for regex detection)
    last_word = ""

    def blank(a: int, b: int, both: bool) -> None:
        for k in range(a, min(b, n)):
            if text[k] != "\n":
                code[k] = " "
                if both:
                    noc[k] = " "

    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j, True)
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j, True)
            i = j
            continue
        if c in "'\"":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            blank(i + 1, j, False)
            i = j + 1
            last_sig = c
            continue
        if c == "`" or (c == "}" and stack and stack[-1] == depth):
            if c == "}":
                stack.pop()
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "`":
                    break
                if text[j] == "$" and j + 1 < n and text[j + 1] == "{":
                    break
                j += 1
            blank(i + 1, j, False)
            if j < n and text[j] == "$":
                stack.append(depth)
                i = j + 2
                last_sig = "{"
                continue
            i = j + 1
            last_sig = "`"
            continue
        if c == "/" and (last_sig in _REGEX_PREV or last_sig == "" or last_word in _REGEX_KEYWORDS):
            j = i + 1
            in_class = False
            while j < n and text[j] != "\n":
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    break
                j += 1
            if j < n and text[j] == "/":
                blank(i + 1, j, False)
                i = j + 1
                last_sig = "/"
                continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if not c.isspace():
            if c.isalnum() or c in "_$":
                j = i
                while j < n and (text[j].isalnum() or text[j] in "_$"):
                    j += 1
                last_word = text[i:j]
                last_sig = "a"
                i = j
                continue
            last_sig = c
            last_word = ""
        i += 1
    return "".join(code), "".join(noc)


def extract_script(text: str) -> str:
    """Keep only ``<script>`` blocks of a Vue/Svelte file, preserving line numbers."""
    out = []
    pos = 0
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", text, re.S | re.I):
        out.append(re.sub(r"[^\n]", " ", text[pos:m.start(1)]))
        out.append(m.group(1))
        pos = m.end(1)
    out.append(re.sub(r"[^\n]", " ", text[pos:]))
    return "".join(out)


def match_brace(code: str, open_pos: int) -> int:
    depth = 0
    for k in range(open_pos, len(code)):
        ch = code[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return k
    return len(code) - 1


@dataclass
class JsImport:
    specifier: str
    kind: str  # import | export-from | require | dynamic | side-effect
    line: int
    type_only: bool = False
    bindings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    star: bool = False


@dataclass
class JsSymbol:
    qualname: str
    name: str
    kind: str
    start: int
    end: int
    line: int
    end_line: int
    fingerprint: str
    parent: str | None
    exported: bool = False
    default: bool = False
    body_fingerprint: str = ""  # the declaration without its own name: survives a rename


@dataclass
class JsFileInfo:
    imports: list[JsImport] = field(default_factory=list)
    symbols: list[JsSymbol] = field(default_factory=list)
    calls: list[tuple[str | None, str | None, tuple[str, ...], int]] = field(default_factory=list)
    local_exports: dict[str, tuple[str, ...]] = field(default_factory=dict)
    generated: bool = False
    fingerprint: str = ""
    loc: int = 0

    def to_json(self) -> dict[str, Any]:
        """Plain data for the persistent parse cache (``diskcache``); :meth:`from_json` reverses it."""
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "JsFileInfo":
        return cls(imports=[JsImport(**{**i, "bindings": {k: tuple(v) for k, v in i["bindings"].items()}})
                            for i in d["imports"]],
                   symbols=[JsSymbol(**s) for s in d["symbols"]],
                   calls=[(c[0], c[1], tuple(c[2]), c[3]) for c in d["calls"]],
                   local_exports={k: tuple(v) for k, v in d["local_exports"].items()},
                   generated=d["generated"], fingerprint=d["fingerprint"], loc=d["loc"])


_IMPORT_FROM = re.compile(r"\bimport\s+(type\s+)?([\w$*{}\s,]*?)\s*\bfrom\s*(['\"])([^'\"\n]+)\3")
_IMPORT_BARE = re.compile(r"\bimport\s*(['\"])([^'\"\n]+)\1")
_EXPORT_FROM = re.compile(r"\bexport\s+(type\s+)?(\*(?:\s+as\s+[\w$]+)?|\{[^}]*\})\s*from\s*(['\"])([^'\"\n]+)\3")
_REQUIRE = re.compile(r"\brequire\s*\(\s*(['\"])([^'\"\n]+)\1\s*\)")
_DYNAMIC = re.compile(r"\bimport\s*\(\s*(['\"])([^'\"\n]+)\1\s*\)")
_REQUIRE_BIND = re.compile(r"\b(?:const|let|var)\s+([\w$]+|\{[^}]*\})\s*=\s*require\s*\(\s*(['\"])([^'\"\n]+)\2")
_FUNC = re.compile(r"\b(export\s+)?(default\s+)?(async\s+)?function\s*\*?\s*([\w$]+)\s*(?:<[^>{]*>)?\s*\(")
_CLASS = re.compile(r"\b(export\s+)?(default\s+)?(?:abstract\s+)?class\s+([\w$]+)")
_ARROW = re.compile(r"\b(export\s+)?(?:const|let|var)\s+([\w$]+)\s*(?::[^=\n]+)?=\s*(?:async\s+)?"
                    r"(?:function\b|(?:<[^>]*>\s*)?(?:\([^()]*(?:\([^()]*\)[^()]*)*\)|[\w$]+)\s*(?::\s*[^=\n]+?)?=>)")
_METHOD = re.compile(r"^[ \t]*(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*"
                     r"\*?\s*(#?[\w$]+)\s*(?:<[^>{]{0,200}>)?\s*\([^;{]{0,500}\)\s*(?::\s*[^{;]{1,200})?\{", re.M)
_CALL = re.compile(r"(?<![\w$.])(new\s+)?((?:this|[\w$]+)(?:\s*\.\s*[\w$]+)*)\s*\(")
_EXPORT_LIST = re.compile(r"\bexport\s*\{([^}]*)\}\s*(?!\s*from)")
_EXPORT_DEFAULT_ID = re.compile(r"\bexport\s+default\s+([\w$]+)\s*;?\s*$", re.M)


def _parse_clause(clause: str) -> tuple[dict[str, tuple[str, ...]], bool]:
    bindings: dict[str, tuple[str, ...]] = {}
    clause = clause.strip()
    star = False
    named = re.search(r"\{([^}]*)\}", clause)
    if named:
        for part in named.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            part = re.sub(r"^type\s+", "", part)
            orig, _, alias = part.partition(" as ")
            bindings[(alias or orig).strip()] = (orig.strip(),)
        clause = clause[: named.start()] + clause[named.end():]
    ns = re.search(r"\*\s+as\s+([\w$]+)", clause)
    if ns:
        bindings[ns.group(1)] = ()
        clause = clause[: ns.start()] + clause[ns.end():]
    default = clause.strip().strip(",").strip()
    if default and re.fullmatch(r"[\w$]+", default):
        bindings[default] = ("default",)
    return bindings, star


def parse_js(text: str) -> JsFileInfo:
    info = JsFileInfo(loc=text.count("\n") + 1, generated=classify.has_generated_marker(text))
    code, noc = mask(text)
    info.fingerprint = stable_hash(" ".join(noc.split()), length=32)
    line_starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line_of(pos: int) -> int:
        lo, hi = 0, len(line_starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if line_starts[mid] <= pos:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def is_code(pos: int) -> bool:
        return code[pos] == noc[pos] and noc[pos] != " "

    seen_spans: set[int] = set()
    for m in _IMPORT_FROM.finditer(noc):
        if not is_code(m.start()):
            continue
        bindings, _ = _parse_clause(m.group(2))
        info.imports.append(JsImport(m.group(4), "import", line_of(m.start()), bool(m.group(1)), bindings))
        seen_spans.add(m.start())
    for m in _EXPORT_FROM.finditer(noc):
        if not is_code(m.start()):
            continue
        clause = m.group(2).strip()
        imp = JsImport(m.group(4), "export-from", line_of(m.start()), bool(m.group(1)))
        if clause.startswith("*"):
            alias = re.search(r"as\s+([\w$]+)", clause)
            if alias:
                info.local_exports[alias.group(1)] = ("@import", m.group(4))
            else:
                imp.star = True
        else:
            for part in clause.strip("{} ").split(","):
                part = re.sub(r"^type\s+", "", part.strip())
                if part:
                    orig, _, alias = part.partition(" as ")
                    info.local_exports[(alias or orig).strip()] = ("@import", m.group(4), orig.strip())
        info.imports.append(imp)
    for m in _IMPORT_BARE.finditer(noc):
        if is_code(m.start()) and m.start() not in seen_spans:
            info.imports.append(JsImport(m.group(2), "side-effect", line_of(m.start())))
    require_bindings: dict[int, dict[str, tuple[str, ...]]] = {}
    for m in _REQUIRE_BIND.finditer(noc):
        target = m.group(1)
        b: dict[str, tuple[str, ...]] = {}
        if target.startswith("{"):
            for part in target.strip("{} ").split(","):
                part = part.strip()
                if part:
                    orig, _, alias = part.partition(":")
                    b[(alias or orig).strip()] = (orig.strip(),)
        else:
            b[target] = ()
        require_bindings[m.start(3) - 1] = b
    for m in _REQUIRE.finditer(noc):
        if not is_code(m.start()):
            continue
        binds = require_bindings.get(m.start(2) - 1, {})
        info.imports.append(JsImport(m.group(2), "require", line_of(m.start()), bindings=binds))
    for m in _DYNAMIC.finditer(noc):
        if is_code(m.start()):
            info.imports.append(JsImport(m.group(2), "dynamic", line_of(m.start())))

    # Declarations.
    depth_at = _depths(code)
    spans: list[JsSymbol] = []

    def add(qual: str, name: str, kind: str, start: int, body_open: int | None, parent: str | None,
            exported: bool = False, default: bool = False) -> None:
        if body_open is None:
            nl = code.find("\n", start)
            end = len(code) - 1 if nl < 0 else nl
        else:
            end = match_brace(code, body_open)
        segment = " ".join(noc[start:end + 1].split())
        body = re.sub(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", "_", segment, count=1)
        spans.append(JsSymbol(qual, name, kind, start, end, line_of(start), line_of(end),
                              stable_hash(segment), parent, exported, default, stable_hash(body)))

    for m in _FUNC.finditer(code):
        if depth_at[m.start()] != 0:
            continue
        brace = _next_brace(code, m.end())
        add(m.group(4), m.group(4), "function", m.start(), brace, None, bool(m.group(1)), bool(m.group(2)))
    for m in _ARROW.finditer(code):
        if depth_at[m.start()] != 0:
            continue
        arrow_end = m.end()
        rest = code[arrow_end:arrow_end + 200].lstrip()
        brace = arrow_end + (len(code[arrow_end:arrow_end + 200]) - len(rest)) if rest.startswith("{") else None
        if code[m.end() - 8:m.end()].strip().endswith("function"):
            brace = _next_brace(code, m.end())
        add(m.group(2), m.group(2), "function", m.start(), brace, None, bool(m.group(1)))
    for m in _CLASS.finditer(code):
        if depth_at[m.start()] != 0:
            continue
        brace = _next_brace(code, m.end())
        if brace is None:
            continue
        cls = m.group(3)
        add(cls, cls, "class", m.start(), brace, None, bool(m.group(1)), bool(m.group(2)))
        body_end = match_brace(code, brace)
        for mm in _METHOD.finditer(code, brace + 1, body_end):
            name = mm.group(1)
            if name in KEYWORDS - {"constructor", "get", "set"} or depth_at[mm.start(1)] != depth_at[brace] + 1:
                continue
            add(f"{cls}.{name}", name, "method", mm.start(1), mm.end() - 1, cls)
    # Deduplicate by qualname (keep first) and index.
    seen: dict[str, int] = {}
    for s in sorted(spans, key=lambda s: s.start):
        seen[s.qualname] = seen.get(s.qualname, 0) + 1
        if seen[s.qualname] > 1:
            s.qualname = f"{s.qualname}#{seen[s.qualname]}"
        info.symbols.append(s)
    for m in _EXPORT_LIST.finditer(code):
        for part in noc[m.start(1):m.end(1)].split(","):
            part = re.sub(r"^type\s+", "", part.strip())
            if part:
                orig, _, alias = part.partition(" as ")
                info.local_exports[(alias or orig).strip()] = (orig.strip(),)
    for m in _EXPORT_DEFAULT_ID.finditer(code):
        if m.group(1) not in ("function", "class", "async"):
            info.local_exports["default"] = (m.group(1),)

    # Call sites, attributed to the innermost enclosing symbol.
    ordered = sorted(info.symbols, key=lambda s: (s.start, -s.end))
    decl_names = {s.start for s in info.symbols}
    for m in _CALL.finditer(code):
        chain = re.sub(r"\s+", "", m.group(2))
        parts = tuple(chain.split("."))
        if parts[0] in KEYWORDS and parts[0] != "this" or parts[-1] in ("if", "for", "while", "switch", "catch"):
            continue
        pos = m.start(2)
        if pos in decl_names:
            continue
        before = code[max(0, pos - 12):pos]
        if re.search(r"(function\s*\*?\s*|class\s+)$", before):
            continue
        owner = None
        for s in ordered:
            if s.start <= pos <= s.end:
                owner = s
            elif s.start > pos:
                break
        class_qual = None
        if owner is not None:
            class_qual = owner.parent if owner.kind == "method" else (owner.qualname if owner.kind == "class" else None)
        if parts[0] == "this":
            parts = ("self",) + parts[1:]
        info.calls.append((owner.qualname if owner else None, class_qual, parts, line_of(pos)))
    return info


def _depths(code: str) -> list[int]:
    out = [0] * (len(code) + 1)
    d = 0
    for i, ch in enumerate(code):
        out[i] = d
        if ch == "{":
            d += 1
        elif ch == "}":
            d = max(0, d - 1)
    out[len(code)] = d
    return out


def _next_brace(code: str, start: int) -> int | None:
    """Position of the ``{`` opening a declaration body (skipping parameter lists)."""
    depth = 0
    for k in range(start, min(len(code), start + 4000)):
        ch = code[k]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "{" and depth <= 0:
            return k
        elif ch == ";" and depth <= 0:
            return None
    return None


# --------------------------------------------------------------------------
# Analyzer
# --------------------------------------------------------------------------


class JavaScriptAnalyzer(Analyzer):
    name = "javascript"
    version = "2"
    languages = JS_LANGS
    capabilities = (CAP_MODULES, CAP_SYMBOLS, CAP_DEPENDENCIES, CAP_CALLS, CAP_EVIDENCE, CAP_DIAGNOSTICS)

    def detect(self, ctx: AnalysisContext) -> Detection:
        n = len(self._files(ctx))
        return Detection(bool(n), f"{n} JavaScript/TypeScript file(s)" if n else "no JavaScript/TypeScript files")

    @staticmethod
    def _files(ctx: AnalysisContext) -> list[str]:
        return [f for f in ctx.profile.included_files
                if ctx.profile.file_languages.get(f, (None, None))[0] in JS_LANGS]

    def discover_modules(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, JsFileInfo] = {}
        texts: dict[str, tuple[tuple[Any, ...], str]] = {}
        for f in self._files(ctx):
            text = ctx.text(f)
            if text is None:
                continue
            if f.endswith((".vue", ".svelte")):
                text = extract_script(text)
            digest = ctx.source.content_hash(f) or stable_hash(text)
            texts[f] = (("javascript", self.version, digest, f.endswith((".vue", ".svelte"))), text)
        misses = [(f, text) for f, (key, text) in texts.items() if key not in ctx.file_cache]
        for f, parsed in parse_parallel(_parse_js_item, misses).items():  # large batches in worker processes
            ctx.file_cache[texts[f][0]] = parsed
        for f, (key, text) in texts.items():
            info = ctx.cached(key, lambda t=text: parse_js(t))
            infos[f] = info
            node = b.ensure_file(f, self.name)
            lang = ctx.profile.file_languages.get(f, (None, None))[0]
            tags = [lang or "javascript"]
            if info.generated:
                tags.append("generated")
            if f.endswith(".d.ts"):
                tags.append("types")
            if ctx.profile.is_test(f):
                tags.append("test")
            b.add_node(ComponentNode(
                id=node.id, name=posixpath.basename(f), qualified_name=_module_name(f), component_type="module",
                category=CATEGORY_MODULE, language=lang, path=f, analyzer=self.name, key=node.key, tags=tags,
                metadata={"loc": info.loc, "semantic_fingerprint": info.fingerprint}))
            node.category = CATEGORY_MODULE
        ctx.shared["javascript.files"] = infos
        ctx.shared["javascript.resolver"] = _Resolver(ctx)
        b.stat(self.name, "modules", len(infos))
        for proj in ctx.profile.projects:
            if proj["ecosystem"] in ("npm", "deno") and proj["path"]:
                node = b.get(b.dir_id(proj["path"]))
                if node is not None and "component" not in node.tags:
                    node.tags.append("component")

    def discover_symbols(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, JsFileInfo] = ctx.shared.get("javascript.files", {})
        index = get_index(ctx)
        resolver: _Resolver = ctx.shared["javascript.resolver"]
        for f, info in infos.items():
            mod_id = b.file_id(f)
            scope = ModuleScope(key=f"js:{f}", node_id=mod_id, language="javascript", path=f)
            for sym in info.symbols:
                sid = b.symbol_id(f, sym.qualname)
                b.add_node(ComponentNode(
                    id=sid, name=sym.name, qualified_name=f"{_module_name(f)}:{sym.qualname.split('#')[0]}",
                    component_type=sym.kind, category=CATEGORY_SYMBOL, language=b.nodes[mod_id].language,
                    path=f, parent_id=b.symbol_id(f, sym.parent) if sym.parent else mod_id, analyzer=self.name,
                    key=f"symbol:{f}:{sym.qualname}", fingerprint=sym.fingerprint, start_line=sym.line,
                    end_line=sym.end_line, tags=(["test"] if ctx.profile.is_test(f) else []),
                    metadata={"kind": sym.kind, "exported": sym.exported, "body_fingerprint": sym.body_fingerprint,
                              **({"default_export": True} if sym.default else {})}))
                scope.symbols[sym.qualname] = sid
                scope.kinds[sym.qualname] = sym.kind
                if sym.default:
                    scope.symbols["default"] = sid
                    scope.kinds["default"] = sym.kind
            for imp in info.imports:
                target = resolver.resolve(imp.specifier, f)
                key = f"js:{target[1]}" if target[0] == "internal" else f"ext:{imp.specifier}"
                for local, attrs in imp.bindings.items():
                    scope.bindings[local] = (key, attrs)
                if imp.star and target[0] == "internal":
                    scope.star_imports.append(key)
            for exported, ref in info.local_exports.items():
                if ref and ref[0] == "@import":
                    target = resolver.resolve(ref[1], f)
                    if target[0] == "internal":
                        scope.bindings.setdefault(exported, (f"js:{target[1]}", tuple(ref[2:3])))
                elif ref and exported != ref[0] and ref[0] in scope.symbols:
                    scope.bindings.setdefault(exported, (scope.key, ref))
            index.add_module(scope)
            for caller_qual, class_qual, parts, line in info.calls:
                caller_id = b.symbol_id(f, caller_qual) if caller_qual else mod_id
                index.calls.append(RawCall(caller_id, scope.key, class_qual, parts, f, line, ctx.excerpt(f, line),
                                           caller_qual=caller_qual))

    def discover_dependencies(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, JsFileInfo] = ctx.shared.get("javascript.files", {})
        resolver: _Resolver | None = ctx.shared.get("javascript.resolver")
        if resolver is None:
            return
        unresolved = 0
        for f, info in infos.items():
            src = b.file_id(f)
            for imp in info.imports:
                kind, target = resolver.resolve(imp.specifier, f)
                ev = self.evidence(ctx, f, imp.line, imp.line, imp.kind)
                meta: dict[str, Any] = {"specifier": imp.specifier}
                if imp.type_only:
                    meta["type_checking_only"] = True
                if imp.kind == "dynamic":
                    meta["dynamic_only"] = True
                    meta["lazy_only"] = True
                if ctx.profile.is_test(f):
                    meta["test_only"] = True
                if kind == "internal":
                    node = b.ensure_file(target, self.name)
                    if node.id == src:
                        continue
                    b.add_edge(src, node.id, REL_IMPORTS, analyzer=self.name, evidence=[ev],
                               confidence=0.95, metadata=meta)
                elif kind == "unresolved":
                    unresolved += 1
                    b.diagnostic("info", "unresolved-import", f"Cannot resolve '{imp.specifier}'.", self.name, f,
                                 imp.line)
                else:
                    builtin = kind == "builtin"
                    eco = "node-builtin" if builtin else "npm"
                    ext_id = b.external_id(eco, target)
                    if ext_id not in b.nodes:
                        b.add_node(ComponentNode(
                            id=ext_id, name=target, qualified_name=target, component_type="external-package",
                            language="javascript", analyzer=self.name, key=f"external:{eco}:{target}",
                            tags=["external"] + (["stdlib"] if builtin else []), metadata={"ecosystem": "npm"}))
                    meta["external"] = True
                    b.add_edge(src, ext_id, REL_IMPORTS, analyzer=self.name, evidence=[ev], confidence=0.9,
                               metadata=meta)
        b.stat(self.name, "unresolved_imports", unresolved)


def _join(*parts: str) -> str:
    joined = posixpath.normpath(posixpath.join(*parts))
    return "" if joined == "." else joined


def _module_name(path: str) -> str:
    stem = path
    for ext in (".d.ts", *SOURCE_EXTS):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    return stem


class _Resolver:
    """Resolves module specifiers to repository files or external packages."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self.all_files = ctx.source.file_set()
        self.packages: dict[str, tuple[str, Any]] = {}  # npm name -> (dir, manifest)
        self.tsconfigs: list[tuple[str, str | None, dict[str, Any]]] = []  # (dir, baseUrl, paths)
        for path, md in ctx.profile.manifest_data.items():
            if md.kind == "package.json" and md.name:
                self.packages[md.name] = (md.dir, md)
            elif md.kind == "tsconfig":
                self.tsconfigs.append((md.dir, md.metadata.get("baseUrl"), md.metadata.get("paths") or {}))
        self.tsconfigs.sort(key=lambda t: -len(t[0]))
        self._cache: dict[tuple[str, str], tuple[str, str]] = {}

    def _probe(self, base: str) -> str | None:
        base = posixpath.normpath(base)
        if base.startswith("../") or base == "..":
            return None
        base = "" if base == "." else base
        candidates = [base] if base else []
        stem, ext = posixpath.splitext(base)
        if ext in (".js", ".jsx", ".mjs", ".cjs"):
            ts_map = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}[ext]
            candidates += [stem + e for e in ts_map]
        candidates += [base + e for e in PROBE_EXTS]
        candidates += [posixpath.join(base, "index" + e) for e in PROBE_EXTS]
        for c in candidates:
            if c in self.all_files:
                return c
        return None

    def _package_entry(self, pkg_dir: str, md: Any, subpath: str) -> str | None:
        if subpath:
            for base in (_join(pkg_dir, subpath), _join(pkg_dir, "src", subpath)):
                found = self._probe(base)
                if found:
                    return found
            return None
        entries: list[str] = []
        exports = md.metadata.get("exports")
        if isinstance(exports, str):
            entries.append(exports)
        elif isinstance(exports, dict):
            dot = exports.get(".", exports)
            if isinstance(dot, str):
                entries.append(dot)
            elif isinstance(dot, dict):
                for cond in ("source", "import", "default", "require", "types", "node"):
                    v = dot.get(cond)
                    if isinstance(v, str):
                        entries.append(v)
                    elif isinstance(v, dict) and isinstance(v.get("default"), str):
                        entries.append(v["default"])
        for key in ("module", "main", "types"):
            v = md.metadata.get(key)
            if isinstance(v, str):
                entries.append(posixpath.relpath(v, pkg_dir) if pkg_dir and v.startswith(pkg_dir + "/") else v)
        for e in entries:
            rel = e[2:] if e.startswith("./") else e
            for variant in (rel, re.sub(r"^(dist|lib|build|out|esm|cjs)/", "src/", rel)):
                found = self._probe(_join(pkg_dir, variant))
                if found:
                    return found
                stem = posixpath.splitext(variant)[0]
                found = self._probe(_join(pkg_dir, stem))
                if found:
                    return found
        for fallback in ("src/index", "index", "src/main", "main"):
            found = self._probe(_join(pkg_dir, fallback))
            if found:
                return found
        return None

    def resolve(self, spec: str, importer: str) -> tuple[str, str]:
        key = (spec, importer)
        if key in self._cache:
            return self._cache[key]
        result = self._resolve(spec, importer)
        self._cache[key] = result
        return result

    def _resolve(self, spec: str, importer: str) -> tuple[str, str]:
        spec = spec.split("?", 1)[0].split("#", 1)[0] if not spec.startswith("#") else spec
        if spec.startswith((".", "/")):
            base = posixpath.join(posixpath.dirname(importer), spec) if spec.startswith(".") else spec.lstrip("/")
            found = self._probe(base)
            return ("internal", found) if found else ("unresolved", spec)
        if spec.startswith("node:") or spec.split("/")[0] in NODE_BUILTINS:
            return ("builtin", spec.removeprefix("node:").split("/")[0])
        for tdir, base_url, paths in self.tsconfigs:
            if tdir and not importer.startswith(tdir + "/"):
                continue
            base_dir = base_url if base_url is not None else tdir
            for pattern, targets in paths.items():
                if pattern.endswith("*"):
                    prefix = pattern[:-1]
                    if not spec.startswith(prefix):
                        continue
                    rest = spec[len(prefix):]
                elif pattern == spec:
                    rest = ""
                else:
                    continue
                for t in targets if isinstance(targets, list) else [targets]:
                    found = self._probe(posixpath.join(base_dir or "", t.replace("*", rest)))
                    if found:
                        return ("internal", found)
            if base_url is not None:
                found = self._probe(posixpath.join(base_url, spec))
                if found:
                    return ("internal", found)
            break
        parts = spec.split("/")
        name = "/".join(parts[:2]) if spec.startswith("@") and len(parts) > 1 else parts[0]
        subpath = "/".join(parts[2:] if spec.startswith("@") else parts[1:])
        if name in self.packages:
            pkg_dir, md = self.packages[name]
            found = self._package_entry(pkg_dir, md, subpath)
            if found:
                return ("internal", found)
        return ("external", name)
