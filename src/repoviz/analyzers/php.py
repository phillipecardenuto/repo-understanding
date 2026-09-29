"""PHP analyzer (lightweight, #26).

Regular expressions over the text with comments, strings, heredocs and the HTML outside ``<?php … ?>`` blanked
(``textscan``); nothing is run.

* **Modules.** One per ``.php`` file, named by its namespace and file name (``App\\Http\\Controllers\\UserController``).
  A folder whose files declare one namespace becomes that namespace.
* **Name resolution, as PHP does it.** ``use A\\B\\C;`` (``as`` aliases, group uses ``use A\\{B, C};``,
  ``use function``, ``use const``) imports a name; an unqualified class name is an import, else a class of the
  current namespace; ``\\A\\B`` is fully qualified.  Class names are read where only a class can stand (``new``,
  ``::``, ``extends`` / ``implements``, ``instanceof``, ``catch``, type declarations, attributes, trait ``use``).
  A name resolves to the file that declares it, or through the project's Composer PSR-4 prefixes
  (``App\\`` → ``app/``).  ``require`` / ``include`` of a literal path (``__DIR__ . '/x.php'``) links that file.
* **External namespaces.** Matched to Composer packages by the PSR-4 prefixes ``composer.lock`` records for each
  (``Illuminate\\`` → ``laravel/framework``), else by name (``Monolog`` → ``monolog/monolog``); a one-segment name
  (``Exception``, ``PDO``) is PHP's own.
* **Broken imports.** A class under one of the project's PSR-4 prefixes whose file does not exist (the folder
  does) is ``unresolved-internal-import``.
* **Symbols.** Classes, interfaces, traits and enums, their methods, and functions, with signatures.  An
  ``index.php`` in a web root is an entry point.  No call graph.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..ids import stable_hash
from ..model import CATEGORY_MODULE, CATEGORY_SYMBOL, REL_IMPORTS, ComponentNode
from .base import (CAP_DEPENDENCIES, CAP_ENTRY_POINTS, CAP_MODULES, CAP_SYMBOLS, AnalysisContext, Analyzer,
                   Detection, SnapshotBuilder, parse_parallel)
from .textscan import LineIndex, mask_pair, squash

MAX_SYMBOLS = 2000
MAX_REFS = 5000
MAX_SIGNATURE = 200
MAX_LOCK_BYTES = 8_000_000

#: PHP: comments (# but not #[ attributes), strings, heredoc / nowdoc.
PHP_TOKENS = re.compile(r"//[^\n]*|#(?!\[)[^\n]*|/\*.*?(?:\*/|\Z)|<<<[ \t]*['\"]?(\w+)['\"]?[ \t]*\n.*?\n[ \t]*\1\b|"
                        r"'(?:\\.|[^'\\])*'?|\"(?:\\.|[^\"\\])*\"?", re.S)
_NAME = r"\\?[A-Za-z_][\w]*(?:\\[A-Za-z_]\w*)*"
_NAMESPACE = re.compile(r"(?<![\w$>\\])namespace\s+([A-Za-z_][\w\\]*)\s*([;{])")
_USE = re.compile(r"(?<![\w$>\\])use\s+(function\s+|const\s+)?(?=\\?[A-Za-z_])([^;{}()]+(?:\{[^}]*\})?)\s*;")
_TYPE = re.compile(r"(?<![\w$>\\])(class|interface|trait|enum)\s+([A-Za-z_]\w*)")
_FUNCTION = re.compile(r"(?<![\w$>\\])function\s+&?\s*([A-Za-z_]\w*)\s*\(")
_MODS = {"public", "protected", "private", "static", "abstract", "final", "readonly"}
#: Where a class name stands: new X, X::, extends / implements / instanceof X, catch (X, #[X], type declarations.
_CLASS_REFS = [
    re.compile(rf"(?<![\w$>\\])new\s+({_NAME})"),
    re.compile(rf"(?<![\w$>\\:])({_NAME})\s*::"),
    re.compile(rf"(?<![\w$>\\])(?:extends|instanceof|insteadof)\s+({_NAME})"),
    re.compile(rf"(?<![\w$>\\])implements\s+({_NAME}(?:\s*,\s*{_NAME})*)"),
    re.compile(rf"catch\s*\(\s*({_NAME}(?:\s*\|\s*{_NAME})*)"),
    re.compile(rf"#\[\s*({_NAME})"),
    re.compile(rf"[(,]\s*\??({_NAME})\s+(?:&\s*)?(?:\.\.\.\s*)?\$"),  # a parameter type
    re.compile(rf"\)\s*:\s*\??({_NAME})"),  # a return type
    re.compile(rf"(?:public|protected|private|readonly|var)\s+(?:static\s+|readonly\s+)*\??({_NAME})\s+\$"),
]
_TRAIT_USE = re.compile(rf"^[ \t]*use\s+({_NAME}(?:\s*,\s*{_NAME})*)\s*[;{{]", re.M)
_FN_CALL = re.compile(rf"(?<![\w$>\\:])({_NAME})\s*\(")
_REQUIRE = re.compile(r"(?<![\w$>])(?:require|include)(?:_once)?\s*\(?\s*(__DIR__\s*\.\s*|dirname\(\s*__FILE__\s*\)\s*\.\s*)?"
                      r"['\"]([^'\"]+\.php)['\"]")
_SCALARS = {"self", "static", "parent", "array", "string", "int", "float", "bool", "callable", "iterable", "object",
            "mixed", "void", "null", "never", "false", "true", "integer", "boolean", "double", "resource"}


# --------------------------------------------------------------------------- parsing (pure, cached per content)


def _mask(text: str) -> tuple[str, str]:
    """Blank what is not PHP (the HTML around ``<?php … ?>``), then comments and literals."""
    out = []
    pos = 0
    for m in re.finditer(r"<\?(?:php\b|=)?(.*?)(?:\?>|\Z)", text, re.S):
        out.append(_blankish(text[pos:m.start(1)]))
        out.append(text[m.start(1):m.end(1)])
        pos = m.end(1)
    out.append(_blankish(text[pos:]))
    php = "".join(out)
    return mask_pair(php, PHP_TOKENS, ("//", "/*", "#"))


def _blankish(s: str) -> str:
    return re.sub(r"[^\n]", " ", s)


@dataclass
class _Open:
    kind: str  # namespace | type | member | block
    decl: dict[str, Any] | None = None
    namespace: str = ""


@dataclass
class _Scan:
    code: str
    noc: str  # comments blanked, strings kept (default values in signatures)
    lines: LineIndex
    decls: list[dict[str, Any]] = field(default_factory=list)
    namespaces: list[tuple[int, int, str]] = field(default_factory=list)  # (start, end, name)


def _add(scan: _Scan, parent: dict[str, Any] | None, ns: str, name: str, kind: str, start: int, end: int | None,
         signature: str | None, public: bool, params: str | None = None) -> dict[str, Any]:
    d = {"name": name, "kind": kind, "ns": parent["ns"] if parent else ns, "parent": parent["qual"] if parent else None,
         "qual": f"{parent['qual']}::{name}" if parent else name, "start": start, "end": end, "signature": signature,
         "public": public and (parent is None or parent["public"]), "params": params}
    if len(scan.decls) < MAX_SYMBOLS:
        scan.decls.append(d)
    return d


def _header(scan: _Scan, header: str, offset: int, parent: dict[str, Any] | None, ns: str,
            ch: str) -> dict[str, Any] | None:
    clean = re.sub(r"#\[(?:[^\[\]]|\[[^\]]*\])*\]", lambda m: " " * len(m.group()), header)
    start = offset + len(header) - len(header.lstrip())
    t = _TYPE.search(clean)
    if t is not None and ch == "{" and parent is None and not re.search(r"(?:new|::)\s*$", clean[:t.start()]):
        return _add(scan, None, ns, t.group(2), t.group(1), start, None, None, True)
    f = _FUNCTION.search(clean)
    if f is None or re.search(r"=|\breturn\b|\(\s*$", clean[:f.start()]):
        return None
    close = _close(clean, f.end() - 1)
    if close < 0:
        return None
    params = squash(scan.noc[offset + f.end():offset + close])  # string defaults kept
    ret = re.match(r"\s*:\s*(\??[\w\\|&?]+)", clean[close + 1:])
    sig = f"({params})" + (f": {ret.group(1)}" if ret else "")
    mods = set(clean[:f.start()].split())
    kind = "method" if parent is not None else "function"
    return _add(scan, parent, ns, f.group(1), kind, start, None if ch == "{" else offset + len(header), sig,
                "private" not in mods and "protected" not in mods, params)


def _close(text: str, open_pos: int) -> int:
    depth = 0
    for k in range(open_pos, len(text)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return -1


def _declarations(scan: _Scan) -> None:
    code = scan.code
    stack: list[_Open] = []
    boundary = -1
    ns = ""  # a `namespace X;` applies until the next one
    for m in re.finditer(r"[{};]", code):
        pos, ch = m.start(), m.group()
        top = stack[-1] if stack else None
        parent = top.decl if top is not None and top.kind == "type" else None
        in_body = top is None or top.kind == "namespace" or parent is not None
        header = code[boundary + 1:pos]
        decl = None
        nsm = _NAMESPACE.search(header + ch) if in_body and parent is None else None
        if nsm is not None:
            if ch == ";":  # until the end of the file, or the next namespace (the last one that covers wins)
                ns = nsm.group(1)
                scan.namespaces.append((pos, len(code), ns))
            else:
                stack.append(_Open("namespace", None, nsm.group(1)))
                scan.namespaces.append((pos, -1, nsm.group(1)))
            boundary = pos
            continue
        cur_ns = top.namespace if top is not None and top.kind == "namespace" else (
            next((o.namespace for o in reversed(stack) if o.kind == "namespace"), ns))
        if in_body and ch != "}":
            decl = _header(scan, header, boundary + 1, parent, cur_ns, ch)
        if ch == "}":
            if stack:
                closed = stack.pop()
                if closed.decl is not None and closed.decl["end"] is None:
                    closed.decl["end"] = pos
                if closed.kind == "namespace":
                    for i, (s0, e0, n0) in enumerate(scan.namespaces):
                        if e0 == -1 and n0 == closed.namespace:
                            scan.namespaces[i] = (s0, pos, n0)
                            break
        elif ch == "{":
            kind = "block"
            if decl is not None:
                kind = "type" if decl["kind"] in ("class", "interface", "trait", "enum") else "member"
            stack.append(_Open(kind, decl, cur_ns))
        boundary = pos
    for d in scan.decls:
        if d["end"] is None:
            d["end"] = len(code) - 1


def _expand_use(body: str) -> list[tuple[str, str | None]]:
    """``A\\{B, C as D}`` → ``[("A\\B", None), ("A\\C", "D")]``; ``A\\B as C, D`` → both."""
    out: list[tuple[str, str | None]] = []
    group = re.match(r"\s*([\w\\]*)\\\s*\{(.*)\}\s*$", body, re.S)
    if group:
        prefix = group.group(1).strip("\\")
        parts = [(prefix + "\\" + p.strip()) for p in group.group(2).split(",") if p.strip()]
    else:
        parts = [p.strip() for p in body.split(",") if p.strip()]
    for p in parts:
        p = re.sub(r"^(?:function|const)\s+", "", p)
        am = re.match(r"(.+?)\s+as\s+(\w+)\s*$", p, re.S)
        name, alias = (am.group(1), am.group(2)) if am else (p, None)
        name = name.strip().strip("\\")
        if name:
            out.append((name, alias))
    return out


def parse_php(text: str) -> dict[str, Any]:
    """Everything the analyzer needs from one PHP file, as plain JSON."""
    code, noc = _mask(text)
    lines = LineIndex(text)
    scan = _Scan(code, noc, lines)
    _declarations(scan)

    def ns_at(pos: int) -> str:
        best = ""
        for s0, e0, n0 in scan.namespaces:
            if s0 <= pos and (e0 == -1 or pos <= e0):
                best = n0
        return best

    type_spans = [(d["start"], d["end"]) for d in scan.decls if d["kind"] in ("class", "interface", "trait", "enum")]
    uses: list[list[Any]] = []  # [name, alias, kind (class / function / const), line, namespace]
    body = list(code)
    for m in _USE.finditer(code):
        if any(a <= m.start() <= b for a, b in type_spans):
            continue  # `use SomeTrait;` inside a class body: a trait, read below
        kind = (m.group(1) or "class").strip()
        for name, alias in _expand_use(m.group(2)):
            uses.append([name, alias, kind, lines.line(m.start()), ns_at(m.start())])
        body[m.start():m.end()] = " " * (m.end() - m.start())
    body_code = "".join(body)
    refs: dict[str, list[Any]] = {}  # "namespace|name" -> [name, line, namespace]

    def ref(name: str, pos: int) -> None:
        name = name.strip()
        if not name or name.lower() in _SCALARS or len(refs) >= MAX_REFS:
            return
        where = ns_at(pos)
        refs.setdefault(f"{where}|{name}", [name, lines.line(pos), where])

    for pattern in _CLASS_REFS:
        for m in pattern.finditer(body_code):
            for name in re.split(r"\s*[,|]\s*", m.group(1)):
                ref(name, m.start(1))
    for m in _TRAIT_USE.finditer(body_code):
        if any(a <= m.start() <= b for a, b in type_spans):
            for name in re.split(r"\s*,\s*", m.group(1)):
                ref(name, m.start(1))
    calls: dict[str, list[Any]] = {}
    for m in _FN_CALL.finditer(body_code):
        name = m.group(1)
        if name.lower() in ("function", "fn", "if", "elseif", "while", "for", "foreach", "switch", "match", "array",
                            "list", "isset", "unset", "empty", "echo", "print", "return", "catch", "new", "use", "and",
                            "or", "declare", "exit", "die", "eval", "include", "require"):
            continue
        if len(calls) < MAX_REFS:
            calls.setdefault(f"{ns_at(m.start())}|{name}", [name, lines.line(m.start()), ns_at(m.start())])
    requires = [[m.group(2), bool(m.group(1)), lines.line(m.start())] for m in _REQUIRE.finditer(noc)]
    seen: Counter[tuple[str, str]] = Counter()
    symbols = []
    for d in scan.decls:  # PHP has no overloads: a second declaration (behind an if) is name#2
        key = (d["ns"], d["qual"])
        seen[key] += 1
        if seen[key] > 1:
            d["qual"] = f"{d['qual']}#{seen[key]}"
        a, b = d["start"], d["end"] + 1
        sig = d["signature"]
        symbols.append({"qual": d["qual"], "ns": d["ns"], "name": d["name"], "kind": d["kind"], "parent": d["parent"],
                        "line": lines.line(a), "end_line": lines.line(max(a, b - 1)),
                        "fingerprint": stable_hash(text[a:b]), "semantic": stable_hash(squash(noc[a:b])),
                        "signature": sig if sig is None or len(sig) <= MAX_SIGNATURE else sig[:MAX_SIGNATURE - 3] + "...",
                        "signature_id": stable_hash("sig", sig, length=12) if sig else None, "public": d["public"]})
    namespaces = list(dict.fromkeys(n for _s, _e, n in scan.namespaces))
    return {"namespaces": namespaces, "uses": uses, "refs": list(refs.values()), "calls": list(calls.values()),
            "requires": requires, "symbols": symbols,
            "types": sorted({(d["ns"], d["name"]) for d in scan.decls if d["kind"] in ("class", "interface", "trait",
                                                                                     "enum")}),
            "functions": sorted({(d["ns"], d["name"]) for d in scan.decls if d["kind"] == "function"}),
            "semantic": stable_hash(squash(noc)), "loc": text.count("\n") + (0 if text.endswith("\n") or not text else 1)}


def _parse_item(item: tuple[str, str]) -> tuple[str, dict[str, Any]]:
    return item[0], parse_php(item[1])


# --------------------------------------------------------------------------- the analyzer


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _package_for(name: str, vendor: list[tuple[str, str]], declared: dict[str, str]) -> str | None:
    """The Composer package a namespaced name comes from: the PSR-4 prefixes ``composer.lock`` records, else the
    declared package whose vendor or name is the first segment (``Monolog`` → ``monolog/monolog``, ``GuzzleHttp`` →
    ``guzzlehttp/guzzle``), several of them told apart by later segments (``Symfony\\Component\\HttpFoundation``
    → ``symfony/http-foundation``, ``Psr\\Http\\Message`` → ``psr/http-message``)."""
    hit = next((p for prefix, p in vendor if name == prefix or name.startswith(prefix + "\\")), None)
    if hit is not None:
        return hit
    segs = [_key(s) for s in name.split("\\")]
    cands = [d for k, d in declared.items() if segs[0] in (_key(k.split("/")[0]), _key(k.split("/")[-1]))]
    if len(cands) > 1:
        joined = {"".join(segs[i:j]) for i in range(1, len(segs)) for j in range(i + 1, min(len(segs), i + 3) + 1)}
        def fits(package: str) -> bool:  # GuzzleHttp\\Promise → guzzlehttp/promises
            key = _key(package.split("/")[-1])
            return any(key in (j, j.removeprefix(segs[0])) or
                       (len(j) >= 4 and abs(len(key) - len(j)) <= 2 and (key.startswith(j) or j.startswith(key)))
                       for j in joined)

        cands = [d for d in cands if fits(d)]
    return cands[0] if len(cands) == 1 else None


class PhpAnalyzer(Analyzer):
    name = "php"
    version = "2"  # bump when the parse result or the graph changes (part of the cache keys)
    languages = ("php",)
    capabilities = (CAP_MODULES, CAP_SYMBOLS, CAP_DEPENDENCIES, CAP_ENTRY_POINTS)

    def detect(self, ctx: AnalysisContext) -> Detection:
        n = len(ctx.files("php"))
        return Detection(bool(n), f"{n} PHP file(s)" if n else "no PHP files")

    def discover_modules(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        texts: dict[str, tuple[tuple[Any, ...], str]] = {}
        for f in sorted(ctx.files("php")):
            text = ctx.text(f)
            if text is None:
                continue
            digest = ctx.source.content_hash(f) or stable_hash(text)
            texts[f] = (("php", self.version, digest), text)
        misses = [(f, text) for f, (key, text) in texts.items() if key not in ctx.file_cache]
        for f, parsed in parse_parallel(_parse_item, misses).items():
            ctx.file_cache[texts[f][0]] = parsed
        infos: dict[str, dict[str, Any]] = {}
        for f, (key, text) in texts.items():
            infos[f] = ctx.cached(key, lambda t=text: parse_php(t))
        ctx.shared["php.infos"] = infos
        by_dir: dict[str, Counter[str]] = {}
        for f, info in infos.items():
            stem = posixpath.splitext(posixpath.basename(f))[0]
            ns = info["namespaces"][0] if info["namespaces"] else ""
            node = b.ensure_file(f, self.name)
            tags = ["php"] + (["test"] if ctx.profile.is_test(f) else [])
            entry = None
            if posixpath.basename(f) == "index.php" and posixpath.basename(posixpath.dirname(f)) in (
                    "", "public", "web", "www", "public_html", "htdocs"):
                entry = "php front controller"
                if "test" not in tags:
                    tags.append("entry-point")
            b.add_node(ComponentNode(
                id=node.id, name=posixpath.basename(f), qualified_name=f"{ns}\\{stem}" if ns else stem,
                component_type="module", category=CATEGORY_MODULE, language="php", path=f, analyzer=self.name,
                key=node.key, tags=tags,
                metadata={"namespace": ns or None, "loc": info["loc"], "semantic_fingerprint": info["semantic"],
                          **({"entry_kind": entry} if entry and "test" not in tags else {})}))
            node.category = CATEGORY_MODULE
            if ns:
                by_dir.setdefault(posixpath.dirname(f), Counter())[ns] += 1
        for d, spaces in by_dir.items():
            if d:
                ns = spaces.most_common(1)[0][0]
                dnode = b.nodes[b.ensure_dir(d, self.name)]
                b.add_node(ComponentNode(
                    id=dnode.id, name=dnode.name, qualified_name=ns, component_type="package", path=d,
                    analyzer=self.name, key=dnode.key, metadata={"qualified_name_authoritative": True,
                                                                 "php_namespace": ns}))
        b.stat(self.name, "modules", len(infos))

    def discover_symbols(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, dict[str, Any]] = ctx.shared.get("php.infos", {})
        for f, info in infos.items():
            module = b.nodes[b.file_id(f)]
            for s in info["symbols"]:
                kind = s["kind"]
                ctype = "method" if kind == "method" else ("function" if kind == "function" else "class")
                meta: dict[str, Any] = {"kind": kind, "public": s["public"], "semantic_fingerprint": s["semantic"]}
                if s["signature"]:
                    meta["signature"] = s["signature"]
                    meta["signature_id"] = s["signature_id"]
                b.add_node(ComponentNode(
                    id=b.symbol_id(f, s["qual"]), name=s["name"],
                    qualified_name=f"{s['ns']}\\{s['qual']}" if s["ns"] else s["qual"], component_type=ctype,
                    category=CATEGORY_SYMBOL, language="php", path=f,
                    parent_id=b.symbol_id(f, s["parent"]) if s["parent"] else module.id, analyzer=self.name,
                    key=f"symbol:{f}:{s['qual']}", fingerprint=s["fingerprint"], start_line=s["line"],
                    end_line=s["end_line"], metadata=meta))
                b.stat(self.name, "symbols")
        if infos:
            b.diagnostic("info", "callflow-unsupported", "Call-flow extraction is not implemented for PHP; the "
                         "Activity tab falls back to module-level import impact.", self.name)

    # -- dependencies -------------------------------------------------------------------------------------------

    def _vendor_prefixes(self, ctx: AnalysisContext) -> list[tuple[str, str]]:
        """``(namespace prefix, package)`` from every ``composer.lock``: what each installed package autoloads."""
        out: list[tuple[str, str]] = []
        for f in ctx.profile.included_files:
            if posixpath.basename(f) != "composer.lock":
                continue
            size = ctx.source.size(f)
            if size is not None and size > MAX_LOCK_BYTES:
                continue
            try:
                data = json.loads(ctx.source.read_text(f, MAX_LOCK_BYTES) or "{}")
            except ValueError:
                continue
            for pkg in (data.get("packages") or []) + (data.get("packages-dev") or []):
                if not isinstance(pkg, dict) or not isinstance(pkg.get("name"), str):
                    continue
                for kind in ("psr-4", "psr-0"):
                    for prefix in ((pkg.get("autoload") or {}).get(kind) or {}):
                        if isinstance(prefix, str) and prefix.strip("\\_"):
                            out.append((prefix.strip("\\"), pkg["name"]))
        return sorted(set(out), key=lambda x: -len(x[0]))

    def discover_dependencies(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, dict[str, Any]] = ctx.shared.get("php.infos", {})
        if not infos:
            return
        classes: dict[str, list[str]] = {}  # lower-case fully qualified class -> files (PHP names are case-insensitive)
        functions: dict[str, list[str]] = {}
        for f, info in infos.items():
            for ns, name in info["types"]:
                classes.setdefault(f"{ns}\\{name}".strip("\\").lower(), []).append(f)
            for ns, name in info["functions"]:
                functions.setdefault(f"{ns}\\{name}".strip("\\").lower(), []).append(f)
        psr4: list[tuple[str, str]] = []  # (prefix, folder)
        declared: dict[str, str] = {}  # normalized package name -> declared name
        for md in ctx.profile.manifest_data.values():
            if md.kind == "composer":
                for prefix, dirs in (md.metadata.get("psr4") or {}).items():
                    psr4 += [(prefix, d) for d in dirs]
                for dep in md.dependencies:
                    declared[dep.name.lower()] = dep.name
        psr4.sort(key=lambda x: -len(x[0]))
        own_prefixes = {p for p, _d in psr4 if p}
        vendor = self._vendor_prefixes(ctx)
        files = set(infos)
        folders = {posixpath.dirname(x) for x in files}
        edges = 0

        def by_psr4(fq: str) -> tuple[str | None, bool]:
            """(file, whether a prefix of the project covers the name and its folder exists)."""
            for prefix, folder in psr4:
                if prefix and not (fq == prefix or fq.startswith(prefix + "\\")):
                    continue
                rel = fq[len(prefix):].strip("\\").replace("\\", "/")
                cand = posixpath.normpath(posixpath.join(folder, rel + ".php")) if folder else rel + ".php"
                if cand in files:
                    return cand, True
                if posixpath.dirname(cand) in folders or posixpath.dirname(cand) == folder:
                    return None, bool(prefix)
            return None, False

        def resolve_class(fq: str, f: str) -> str | None:
            hit = classes.get(fq.lower())
            if hit:
                return hit[0] if len(hit) == 1 else max(hit, key=lambda x: len(posixpath.commonprefix([x, f])))
            return by_psr4(fq)[0]

        for f, info in infos.items():
            src = b.file_id(f)
            test = ctx.profile.is_test(f)
            done: set[str] = set()
            imports: dict[tuple[str, str], str] = {}  # (namespace, alias lower) -> imported name

            def link(target: str, line: int, construct: str, name: str, confidence: float,
                     extra: dict[str, Any] | None = None) -> None:
                nonlocal edges
                if target == f:
                    return
                meta: dict[str, Any] = {"imported_names": [name], **(extra or {})}
                if test:
                    meta["test_only"] = True
                b.add_edge(src, b.file_id(target), REL_IMPORTS, analyzer=self.name,
                           evidence=[self.evidence(ctx, f, line, line, construct)], confidence=confidence,
                           metadata=meta)
                done.add(target)
                edges += 1

            for name, alias, kind, line, ns in info["uses"]:
                imports[(ns, (alias or name.rsplit("\\", 1)[-1]).lower())] = name
                if kind == "function":
                    hit = functions.get(name.lower())
                    target = hit[0] if hit else None
                elif kind == "const":
                    target = None
                else:
                    target = resolve_class(name, f)
                if target is not None:
                    link(target, line, "use", name, 1.0)
                    continue
                if kind == "class":
                    covered = by_psr4(name)[1] and any(name == p or name.startswith(p + "\\") for p in own_prefixes)
                    used_as_namespace = any(r[0].lower().startswith((alias or name.rsplit("\\", 1)[-1]).lower() + "\\")
                                            for r in info["refs"])
                    pkg = _package_for(name, vendor, declared)
                    if covered and name.lower() not in classes and not used_as_namespace and pkg is None:
                        b.diagnostic("warning", "unresolved-internal-import",
                                     f"'{name}' does not exist in the repository (no class declares it and its "
                                     f"PSR-4 file is missing).", self.name, f, line)
                        continue
                    if pkg is None and any(name == p or name.startswith(p + "\\") for p in own_prefixes):
                        continue  # the project's own namespace, not declared here: generated or not analyzed
                self._external(ctx, b, src, f, name, line, vendor, declared, test)
            for name, line, ns in info["refs"]:  # class names in code: imports, else the current namespace
                imported = None
                if name.startswith("\\"):
                    fq, construct = name.strip("\\"), "qualified-name"
                else:
                    head, _, rest = name.partition("\\")
                    imported = imports.get((ns, head.lower()))
                    fq = (imported + ("\\" + rest if rest else "")) if imported else (f"{ns}\\{name}" if ns else name)
                    construct = "use" if imported else "same-namespace"
                target = resolve_class(fq, f)
                if target is not None and target not in done:
                    link(target, line, construct, fq, 0.85 if construct == "same-namespace" else 0.95,
                         {"same_package": True} if construct == "same-namespace" else None)
            for name, line, ns in info["calls"]:  # functions of the same namespace, or imported with `use function`
                if name.startswith("\\"):
                    fq = name.strip("\\")
                else:
                    imported = imports.get((ns, name.lower()))
                    fq = imported or (f"{ns}\\{name}" if ns else name)
                hit = functions.get(fq.lower())
                if hit and hit[0] not in done:
                    link(hit[0], line, "function-call", fq, 0.8)
            for rel, from_dir, line in info["requires"]:  # require __DIR__ . '/x.php'
                base = posixpath.dirname(f)
                cands = [posixpath.normpath(posixpath.join(base, rel.lstrip("/")))] if from_dir else \
                    [posixpath.normpath(posixpath.join(base, rel)), posixpath.normpath(rel)]
                target = next((c for c in cands if c in files), None)
                if target is not None and target not in done:
                    link(target, line, "require", rel, 1.0)
        b.stat(self.name, "import_edges", edges)

    def _external(self, ctx: AnalysisContext, b: SnapshotBuilder, src: str, f: str, name: str, line: int,
                  vendor: list[tuple[str, str]], declared: dict[str, str], test: bool) -> None:
        std = "\\" not in name
        pkg = None if std else _package_for(name, vendor, declared)
        eco = "php-builtin" if std else "composer"
        ext_name = "php" if std else (pkg or "\\".join(name.split("\\")[:2]))
        key_name = ext_name.lower()
        ext_id = b.external_id(eco, key_name)
        if ext_id not in b.nodes:
            b.add_node(ComponentNode(id=ext_id, name=ext_name, qualified_name=ext_name,
                                     component_type="external-package", analyzer=self.name,
                                     key=f"external:{eco}:{key_name}", tags=["external"] + (["stdlib"] if std else []),
                                     metadata={"ecosystem": "php"}))
        meta: dict[str, Any] = {"external": True, "imported_names": [name]}
        if test:
            meta["test_only"] = True
        b.add_edge(src, ext_id, REL_IMPORTS, analyzer=self.name,
                   evidence=[self.evidence(ctx, f, line, line, "use")], confidence=0.9, metadata=meta)
