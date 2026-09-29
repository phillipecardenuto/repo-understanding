"""C# analyzer (lightweight, #26).

Regular expressions over the text with comments and strings blanked (``textscan``); nothing is compiled or run.

* **Modules.** One per ``.cs`` file, named by its namespace and file name (``Shop.Web.OrdersController``).  A
  folder whose files declare one namespace becomes that namespace.
* **Name resolution, as the compiler sees it.** ``using A.B;`` brings a whole namespace into scope, so it links
  only the types of ``A.B`` the file uses (and, for a static class of extension methods, the files whose extension
  methods it calls).  A file also sees its own namespace and every enclosing one (``Shop.Web`` sees ``Shop``)
  without a ``using``, and the ``global using`` directives of its project (the nearest ``.csproj``).
  ``using static A.B.C;`` and ``using X = A.B.C;`` link the file of ``C``; fully qualified names in code count
  too.  An attribute ``[Audited]`` is the type ``AuditedAttribute``.  When several files declare a name (partial
  classes, two projects), the one nearest to the using file wins.
* **External namespaces.** Matched to the NuGet packages the projects reference: a package id is usually the
  namespace or a prefix of it (``Newtonsoft.Json`` for ``Newtonsoft.Json.Linq``, ``xunit`` for ``Xunit``).
  ``System.*``, ``Microsoft.Extensions.*``, ``Microsoft.AspNetCore.*`` and the other framework namespaces are
  standard; others are named by their first two segments.
* **Symbols.** Classes, structs, interfaces, enums, records and delegates (nested too), methods and
  constructors with their signature (expression-bodied members included); overloads carry their parameter types.
  Entry points: ``static Main`` and top-level statements.  No call graph.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..ids import stable_hash
from ..model import CATEGORY_MODULE, CATEGORY_SYMBOL, REL_IMPORTS, ComponentNode
from .base import (CAP_DEPENDENCIES, CAP_ENTRY_POINTS, CAP_MODULES, CAP_SYMBOLS, AnalysisContext, Analyzer,
                   Detection, SnapshotBuilder, parse_parallel)
from .textscan import LineIndex, mask_pair, match_open, package_roots, squash, top_level

MAX_SYMBOLS = 2000
MAX_REFS = 5000
MAX_SIGNATURE = 200

#: C#: comments, raw strings (\"\"\"…), verbatim strings (@"…" with "" inside, $@ / @$ interpolated), strings and
#: characters.
CS_TOKENS = re.compile(r'//[^\n]*|/\*.*?(?:\*/|\Z)|"""+.*?(?:"""+|\Z)|\$?@\$?"(?:[^"]|"")*"?|'
                       r'"(?:\\.|[^"\\\n])*"?|\'(?:\\.|[^\'\\\n])*\'?', re.S)
_USING = re.compile(r"^[ \t]*(global\s+)?using\s+(static\s+)?(?:([A-Za-z_]\w*)\s*=\s*)?"
                    r"((?:global::)?[A-Za-z_][\w.]*(?:<[^;>]*>)?)\s*;", re.M)
_NAMESPACE = re.compile(r"\bnamespace\s+([A-Za-z_][\w.]*)\s*$")
_ATTR = re.compile(r"\[\s*(?:\w+\s*:\s*)?[A-Za-z_][\w.]*(?:\s*\((?:[^()]|\([^()]*\))*\))?"
                   r"(?:\s*,\s*[A-Za-z_][\w.]*(?:\s*\((?:[^()]|\([^()]*\))*\))?)*\s*\]")
_TYPE = re.compile(r"(?<![\w.])(class|struct|interface|enum|record(?:\s+(?:class|struct))?)\s+([A-Za-z_]\w*)")
_DELEGATE = re.compile(r"(?<![\w.])delegate\s+[\w.<>\[\]?,\s]+?\s+([A-Za-z_]\w*)\s*(?:<[^()]*>)?\s*\(")
_CTOR_INIT = re.compile(r"\)\s*:\s*(?:base|this)\s*\((?:[^()]|\([^()]*\))*\)\s*$")
_WHERE = re.compile(r"\)\s*where\s+\w+\s*:[^()]*$")
_MODS = {"public", "protected", "private", "internal", "static", "virtual", "override", "abstract", "sealed", "async",
         "partial", "readonly", "extern", "unsafe", "new", "volatile", "const", "required", "file", "ref"}
_CONTROL = {"if", "for", "foreach", "while", "switch", "catch", "using", "lock", "fixed", "return", "new", "throw",
            "else", "do", "try", "yield", "await", "nameof", "typeof", "sizeof", "default", "checked", "unchecked",
            "when", "base", "this"}
_CAP_REF = re.compile(r"(?<![\w.])([A-Z]\w*)")
_MEMBER_CALL = re.compile(r"\.\s*([A-Z]\w*)\s*(?:<[^()<>]*>)?\s*\(")
_QUALIFIED = re.compile(r"(?<![\w.])((?:[A-Z]\w*\.){2,}[A-Z]\w*)")
_PROPERTY = re.compile(r"(?<![\w.])([A-Z]\w*)\s*(?:\{\s*(?:get|set|init|private|protected|internal)\b|=>)")
#: Where only a type can stand (a name ``{n}``): new X(…), X x, X?, X[], X<…>, <X>, typeof(X), is / as X, a base
#: list, a cast, an attribute.
_TYPE_USE = (r"\bnew\s+{n}\b|(?<![\w.]){n}\s*(?:\?|\[\s*\]|<)|(?<![\w.]){n}\s+[A-Za-z_@]\w*\s*(?:[=;,){{]|=>)|"
             r"[<,]\s*{n}\s*[>,]|typeof\(\s*{n}\s*\)|\b(?:is|as)\s+{n}\b|\(\s*{n}\s*\)\s*[\w(]|"
             r"(?:class|struct|interface|record)\s+\w+(?:<[^>]*>)?\s*:\s*[^{{]*\b{n}\b|\[\s*{n}\s*[\](]")
_MAIN = re.compile(r"\bstatic\s+(?:async\s+)?(?:void|int|Task(?:\s*<\s*int\s*>)?)\s+Main\s*\(")
#: Framework namespaces (the SDKs ship them; ASP.NET Core and the Extensions come with the web SDK).
_STDLIB = ("System", "Microsoft.CSharp", "Microsoft.Win32", "Microsoft.VisualBasic", "Microsoft.AspNetCore",
           "Microsoft.Extensions", "Microsoft.JSInterop", "Microsoft.Net", "Windows")


# --------------------------------------------------------------------------- parsing (pure, cached per content)


@dataclass
class _Open:
    kind: str  # namespace | type | member | block
    decl: dict[str, Any] | None = None
    namespace: str = ""


@dataclass
class _Scan:
    code: str
    lines: LineIndex
    decls: list[dict[str, Any]] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    top_level_statements: bool = False


def _add(scan: _Scan, parent: dict[str, Any] | None, ns: str, name: str, kind: str, start: int, end: int | None,
         signature: str | None = None, public: bool = True, params: str | None = None) -> dict[str, Any]:
    d = {"name": name, "kind": kind, "ns": parent["ns"] if parent else ns, "parent": parent["qual"] if parent else None,
         "qual": f"{parent['qual']}.{name}" if parent else name, "depth": (parent["depth"] + 1) if parent else 0,
         "start": start, "end": end, "signature": signature, "public": public and (parent is None or parent["public"]),
         "params": params, "interface": kind == "interface", "static_class": False, "extension": False}
    if len(scan.decls) < MAX_SYMBOLS:
        scan.decls.append(d)
    return d


def _param_types(params: str) -> str:
    out, depth, cur = [], 0, ""
    for ch in params + ",":
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            p = _ATTR.sub(" ", cur).split("=", 1)[0].strip()
            words = [w for w in p.split() if w not in ("this", "ref", "out", "in", "params", "scoped")]
            if words:
                out.append(squash(" ".join(words[:-1]) if len(words) > 1 else words[0]))
            cur = ""
        else:
            cur += ch
    return ", ".join(out)


def _header(scan: _Scan, header: str, offset: int, parent: dict[str, Any] | None, ns: str,
            ch: str) -> dict[str, Any] | None:
    clean = _ATTR.sub(lambda m: " " * len(m.group()), header)
    clean = re.sub(r"(?m)^[ \t]*#.*$", lambda m: " " * len(m.group()), clean)  # #region, #if …
    stripped = clean.strip()
    if not stripped:
        return None
    start = offset + len(header) - len(header.lstrip())
    t = _TYPE.search(clean)
    if t is not None and (ch == "{" or t.group(1).startswith("record")):
        mods = set(clean[:t.start()].split())
        public = bool({"public", "protected"} & mods) or bool(parent and parent["interface"])
        kind = "record" if t.group(1).startswith("record") else t.group(1)
        d = _add(scan, parent, ns, t.group(2), kind, start, None if ch == "{" else offset + len(header), public=public)
        d["static_class"] = "static" in mods
        return d
    dm = _DELEGATE.search(clean)
    if dm is not None and ch == ";":
        mods = set(clean[:dm.start()].split())
        return _add(scan, parent, ns, dm.group(1), "delegate", start, offset + len(header),
                    public=bool({"public", "protected"} & mods))
    if parent is None:
        if ch in ";{" and not re.match(r"(global\s+)?using\b|extern\s+alias\b|namespace\b", stripped) and \
                not _NAMESPACE.search(clean):
            scan.top_level_statements = True  # statements outside any type: C# 9 top-level statements
        return None
    h = clean.rstrip()
    arrow = _top_level_arrow(h)
    if arrow >= 0:  # an expression-bodied member: int Twice(int x) => x * 2;
        h = h[:arrow].rstrip()
    elif ch != "{" and not h.endswith(")"):
        return None
    h = _WHERE.sub(")", h)
    h = _CTOR_INIT.sub(")", h)
    if not h.endswith(")"):
        return None
    open_ = match_open(h, len(h) - 1)
    if open_ < 0:
        return None
    before = h[:open_]
    nm = re.search(r"([A-Za-z_]\w*)\s*(?:<[^()]*>)?\s*$", before)
    if nm is None or nm.group(1) in _CONTROL or before[:nm.start()].rstrip().endswith("~"):
        return None
    prefix = before[:nm.start()]
    if prefix.count(")") > prefix.count("("):  # the tail of an attribute the pattern missed: keep what follows
        prefix = prefix[prefix.rindex(")") + 1:]
    words = prefix.split()
    mods = {w for w in words if w in _MODS}
    ret = " ".join(w for w in words if w not in _MODS).strip()
    if top_level(prefix, "=") or (ret and not re.fullmatch(r"[\w.<>\[\]?,()\s]+", ret)):
        return None
    name = nm.group(1)
    if not ret and name != parent["name"]:
        return None
    if ret.endswith("operator") or "operator" in ret.split():
        return None
    params = squash(h[open_ + 1:-1])
    public = bool({"public", "protected"} & mods) or parent["interface"]
    if "private" in mods:
        public = False
    sig = f"({params})" + (f" -> {squash(ret)}" if ret else "")
    d = _add(scan, parent, ns, name, "method" if ret else "constructor", start,
             None if ch == "{" and arrow < 0 else offset + len(header), signature=sig, public=public, params=params)
    d["extension"] = bool(re.match(r"\s*this\s", params)) and parent.get("static_class", False)
    return d


def _top_level_arrow(text: str) -> int:
    depth = 0
    for i, c in enumerate(text):
        if c in "(<[":
            depth += 1
        elif c in ")]":
            depth = max(0, depth - 1)
        elif c == ">" and not (i and text[i - 1] == "="):
            depth = max(0, depth - 1)
        elif c == "=" and depth == 0 and text[i + 1:i + 2] == ">":
            return i
    return -1


def _declarations(scan: _Scan, file_ns: str) -> None:
    code = scan.code
    stack: list[_Open] = []
    boundary = -1
    ns = file_ns  # a file-scoped namespace applies to the whole file
    if file_ns:
        scan.namespaces.append(file_ns)
    for m in re.finditer(r"[{};]", code):
        pos, ch = m.start(), m.group()
        top = stack[-1] if stack else None
        cur_ns = top.namespace if top is not None else ns
        parent = top.decl if top is not None and top.kind == "type" else None
        in_body = top is None or top.kind == "namespace" or parent is not None
        header = code[boundary + 1:pos]
        decl = None
        opens_namespace = None
        if in_body and ch == "{":
            nsm = _NAMESPACE.search(_ATTR.sub(" ", header))
            if nsm is not None:
                opens_namespace = f"{cur_ns}.{nsm.group(1)}" if cur_ns and top is not None else nsm.group(1)
        if in_body and opens_namespace is None and ch != "}":
            decl = _header(scan, header, boundary + 1, parent, cur_ns, ch)
        if ch == "}":
            if stack:
                closed = stack.pop()
                if closed.decl is not None and closed.decl["end"] is None:
                    closed.decl["end"] = pos
        elif ch == "{":
            if opens_namespace is not None:
                stack.append(_Open("namespace", None, opens_namespace))
                if opens_namespace not in scan.namespaces:
                    scan.namespaces.append(opens_namespace)
            else:
                kind = "block"
                if decl is not None:
                    kind = "type" if decl["kind"] in ("class", "struct", "interface", "enum", "record") else "member"
                stack.append(_Open(kind, decl, cur_ns))
        boundary = pos
    for d in scan.decls:
        if d["end"] is None:
            d["end"] = len(code) - 1


def parse_csharp(text: str) -> dict[str, Any]:
    """Everything the analyzer needs from one C# file, as plain JSON."""
    if text.startswith("\ufeff"):  # a byte-order mark: a blank, so the first using still starts its line
        text = " " + text[1:]
    code, noc = mask_pair(text, CS_TOKENS)
    lines = LineIndex(text)
    usings: list[list[Any]] = []  # [name, global, static, alias, line]
    body = list(code)
    for m in _USING.finditer(code):
        name = m.group(4).replace("global::", "")
        usings.append([re.sub(r"<.*", "", name), bool(m.group(1)), bool(m.group(2)), m.group(3), lines.line(m.start(4))])
        body[m.start():m.end()] = " " * (m.end() - m.start())
    file_ns = ""
    for m in re.finditer(r"(?m)^[ \t]*namespace\s+([A-Za-z_][\w.]*)\s*([;{])", code):
        if m.group(2) == ";":
            file_ns = m.group(1)
        a, b = m.span(1)
        body[a:b] = " " * (b - a)
    body_code = "".join(body)
    scan = _Scan(code, lines)
    _declarations(scan, file_ns)
    count = Counter((d["ns"], d["parent"], d["name"]) for d in scan.decls if d["params"] is not None)
    for d in scan.decls:
        if d["params"] is not None and count[(d["ns"], d["parent"], d["name"])] > 1:
            d["qual"] = f"{d['qual']}({_param_types(d['params'])})"
    seen: Counter[tuple[str, str]] = Counter()
    for d in scan.decls:
        seen[(d["ns"], d["qual"])] += 1
        if seen[(d["ns"], d["qual"])] > 1:
            d["qual"] = f"{d['qual']}#{seen[(d['ns'], d['qual'])]}"
    symbols = []
    for d in scan.decls:
        a, b = d["start"], d["end"] + 1
        sig = d["signature"]
        symbols.append({"qual": d["qual"], "ns": d["ns"], "name": d["name"], "kind": d["kind"], "parent": d["parent"],
                        "line": lines.line(a), "end_line": lines.line(max(a, b - 1)),
                        "fingerprint": stable_hash(text[a:b]), "semantic": stable_hash(squash(noc[a:b])),
                        "signature": sig if sig is None or len(sig) <= MAX_SIGNATURE else sig[:MAX_SIGNATURE - 3] + "...",
                        "signature_id": stable_hash("sig", sig, length=12) if sig else None, "public": d["public"],
                        **({"extension": True} if d["extension"] else {})})
    refs: dict[str, int] = {}
    for m in _CAP_REF.finditer(body_code):
        if len(refs) >= MAX_REFS:
            break
        refs.setdefault(m.group(1), m.start())
    # C# members are PascalCase like types: a property or method named like a type (``public string Name { get;
    # set; }``) is not a use of the type ``Name`` unless the name also appears where only a type can be
    members = {d["name"] for d in scan.decls if d["kind"] == "method"} | set(_PROPERTY.findall(body_code))
    for name in members & refs.keys():
        if not re.search(_TYPE_USE.format(n=re.escape(name)), body_code):
            del refs[name]
    calls: dict[str, int] = {}
    for m in _MEMBER_CALL.finditer(body_code):
        if len(calls) >= MAX_REFS:
            break
        calls.setdefault(m.group(1), m.start())
    qualified: dict[str, int] = {}
    for m in _QUALIFIED.finditer(body_code):
        if len(qualified) >= 500:
            break
        qualified.setdefault(m.group(1), m.start())
    entry = "dotnet Main method" if _MAIN.search(code) else ("top-level statements" if scan.top_level_statements
                                                             else None)
    return {"namespaces": scan.namespaces, "usings": usings, "symbols": symbols,
            "types": sorted({(d["ns"], d["qual"]) for d in scan.decls
                             if d["kind"] in ("class", "struct", "interface", "enum", "record", "delegate")}),
            "refs": {k: lines.line(v) for k, v in refs.items()}, "calls": {k: lines.line(v) for k, v in calls.items()},
            "qualified": {k: lines.line(v) for k, v in qualified.items()}, "entry": entry,
            "semantic": stable_hash(squash(noc)), "loc": text.count("\n") + (0 if text.endswith("\n") or not text else 1)}


def _parse_item(item: tuple[str, str]) -> tuple[str, dict[str, Any]]:
    return item[0], parse_csharp(item[1])


# --------------------------------------------------------------------------- the analyzer


def _stdlib(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in _STDLIB)


def _enclosing(ns: str) -> list[str]:
    """``A.B.C`` → ``A.B.C``, ``A.B``, ``A``, and the global namespace."""
    parts = ns.split(".") if ns else []
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)] + [""]


class DotnetAnalyzer(Analyzer):
    name = "dotnet"
    version = "1"  # bump when the parse result or the graph changes (part of the cache keys)
    languages = ("csharp",)
    capabilities = (CAP_MODULES, CAP_SYMBOLS, CAP_DEPENDENCIES, CAP_ENTRY_POINTS)

    def detect(self, ctx: AnalysisContext) -> Detection:
        n = len(ctx.files("csharp"))
        return Detection(bool(n), f"{n} C# file(s)" if n else "no C# files")

    def discover_modules(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        texts: dict[str, tuple[tuple[Any, ...], str]] = {}
        for f in sorted(ctx.files("csharp")):
            text = ctx.text(f)
            if text is None:
                continue
            digest = ctx.source.content_hash(f) or stable_hash(text)
            texts[f] = (("dotnet", self.version, digest), text)
        misses = [(f, text) for f, (key, text) in texts.items() if key not in ctx.file_cache]
        for f, parsed in parse_parallel(_parse_item, misses).items():
            ctx.file_cache[texts[f][0]] = parsed
        infos: dict[str, dict[str, Any]] = {}
        for f, (key, text) in texts.items():
            infos[f] = ctx.cached(key, lambda t=text: parse_csharp(t))
        ctx.shared["dotnet.infos"] = infos
        by_dir: dict[str, Counter[str]] = {}
        for f, info in infos.items():
            stem = posixpath.splitext(posixpath.basename(f))[0]
            ns = info["namespaces"][0] if info["namespaces"] else ""
            node = b.ensure_file(f, self.name)
            tags = ["csharp"] + (["test"] if ctx.profile.is_test(f) else [])
            if info["entry"] and "test" not in tags:
                tags.append("entry-point")
            b.add_node(ComponentNode(
                id=node.id, name=posixpath.basename(f), qualified_name=f"{ns}.{stem}" if ns else stem,
                component_type="module", category=CATEGORY_MODULE, language="csharp", path=f, analyzer=self.name,
                key=node.key, tags=tags,
                metadata={"namespace": ns or None, "loc": info["loc"], "semantic_fingerprint": info["semantic"],
                          **({"entry_kind": info["entry"]} if info["entry"] and "test" not in tags else {})}))
            node.category = CATEGORY_MODULE
            if ns:
                by_dir.setdefault(posixpath.dirname(f), Counter())[ns] += 1
        for d, spaces in by_dir.items():
            if not d:
                continue
            ns = spaces.most_common(1)[0][0]
            dnode = b.nodes[b.ensure_dir(d, self.name)]
            b.add_node(ComponentNode(
                id=dnode.id, name=dnode.name, qualified_name=ns, component_type="package", path=d,
                analyzer=self.name, key=dnode.key,
                metadata={"qualified_name_authoritative": True, "dotnet_namespace": ns}))
        b.stat(self.name, "modules", len(infos))

    def discover_symbols(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, dict[str, Any]] = ctx.shared.get("dotnet.infos", {})
        for f, info in infos.items():
            module = b.nodes[b.file_id(f)]
            for s in info["symbols"]:
                kind = s["kind"]
                ctype = "method" if kind in ("method", "constructor") else "class"
                meta: dict[str, Any] = {"kind": kind, "public": s["public"], "semantic_fingerprint": s["semantic"]}
                if s["signature"]:
                    meta["signature"] = s["signature"]
                    meta["signature_id"] = s["signature_id"]
                if s.get("extension"):
                    meta["extension_method"] = True
                b.add_node(ComponentNode(
                    id=b.symbol_id(f, s["qual"]), name=s["name"],
                    qualified_name=f"{s['ns']}.{s['qual']}" if s["ns"] else s["qual"], component_type=ctype,
                    category=CATEGORY_SYMBOL, language="csharp", path=f,
                    parent_id=b.symbol_id(f, s["parent"]) if s["parent"] else module.id, analyzer=self.name,
                    key=f"symbol:{f}:{s['qual']}", fingerprint=s["fingerprint"], start_line=s["line"],
                    end_line=s["end_line"], metadata=meta))
                b.stat(self.name, "symbols")
        if infos:
            b.diagnostic("info", "callflow-unsupported", "Call-flow extraction is not implemented for C#; the "
                         "Activity tab falls back to module-level import impact.", self.name)

    # -- dependencies -------------------------------------------------------------------------------------------

    def _packages(self, ctx: AnalysisContext) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
        """NuGet packages the projects reference: ``(id, lower-case id segments, project folders)``."""
        found: dict[str, tuple[str, set[str]]] = {}
        for md in ctx.profile.manifest_data.values():
            if md.ecosystem != "dotnet":
                continue
            for dep in md.dependencies:
                if dep.local_path is None and not getattr(dep, "workspace", False):
                    found.setdefault(dep.name.lower(), (dep.name, set()))[1].add(md.dir)
        return [(name, tuple(key.split(".")), tuple(sorted(dirs))) for key, (name, dirs) in sorted(found.items())]

    @staticmethod
    def _match_package(ns: str, folder: str, packages: list[tuple[str, tuple[str, ...], tuple[str, ...]]],
                       *, prefix_only: bool = False) -> str | None:
        """The referenced package a namespace comes from: the one whose id is the namespace or its longest prefix
        (``Newtonsoft.Json`` → ``Newtonsoft.Json.Linq``, ``xunit`` → ``Xunit``); else, unless ``prefix_only``, a
        package whose id starts with the namespace (``Microsoft.EntityFrameworkCore`` from
        ``Microsoft.EntityFrameworkCore.SqlServer``).  A project that covers the file breaks ties; a tie left is
        no match."""
        segs = [s.lower() for s in ns.split(".")]
        best: tuple[int, ...] | None = None
        pick, tied = None, False
        for name, ids, dirs in packages:
            if len(ids) <= len(segs) and tuple(segs[:len(ids)]) == ids:
                rank = (1, len(ids))  # the id is a prefix of the namespace: the longer, the better
            elif not prefix_only and len(ids) > len(segs) and ids[:len(segs)] == tuple(segs):
                rank = (0, -len(ids))  # the namespace is a prefix of the id: the shorter id, the better
            else:
                continue
            near = 1 if any(not d or folder == d or folder.startswith(d + "/") for d in dirs) else 0
            score = (*rank, near)
            if best is None or score > best:
                best, pick, tied = score, name, False
            elif score == best:
                tied = True
        return None if tied else pick

    def discover_dependencies(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, dict[str, Any]] = ctx.shared.get("dotnet.infos", {})
        if not infos:
            return
        types: dict[str, list[str]] = {}  # fully qualified type (nested too) -> files
        ns_types: dict[str, dict[str, list[str]]] = {}  # namespace -> simple top-level type name -> files
        ns_ext: dict[str, dict[str, list[str]]] = {}  # namespace -> extension method name -> files
        for f, info in infos.items():
            for ns, qual in info["types"]:
                types.setdefault(f"{ns}.{qual}" if ns else qual, []).append(f)
                if "." not in qual:
                    ns_types.setdefault(ns, {}).setdefault(qual, []).append(f)
            for s in info["symbols"]:
                if s.get("extension"):
                    ns_ext.setdefault(s["ns"], {}).setdefault(s["name"], []).append(f)
        known_ns = set(ns_types) | {ns for info in infos.values() for ns in info["namespaces"]}
        roots = package_roots(known_ns)
        packages = self._packages(ctx)
        projects = sorted((md.dir for md in ctx.profile.manifest_data.values() if md.kind == "msbuild-project"),
                          key=len, reverse=True)

        def project_of(f: str) -> str | None:
            return next((d for d in projects if not d or f.startswith(d + "/")), None)

        global_usings: dict[str | None, list[list[Any]]] = {}
        for f, info in infos.items():
            for u in info["usings"]:
                if u[1] and not u[2] and not u[3]:
                    global_usings.setdefault(project_of(f), []).append([*u, f])
        near_memo: dict[tuple[int, str], str] = {}

        def nearest(files: list[str], f: str) -> str:
            if len(files) == 1:
                return files[0]
            key = (id(files), posixpath.dirname(f))
            if key not in near_memo:
                near_memo[key] = max(files, key=lambda x: (len(posixpath.commonprefix([x, key[1] + "/"])), -len(x), x))
            return near_memo[key]

        edges = 0
        ext_memo: dict[tuple[str, str], tuple[Any, ...]] = {}
        for f, info in infos.items():
            src = b.file_id(f)
            test = ctx.profile.is_test(f)
            refs, calls = info["refs"], info["calls"]
            done: set[str] = set()

            def link(target: str, line: int, construct: str, names: list[str], confidence: float,
                     extra: dict[str, Any] | None = None, where: str | None = None) -> None:
                nonlocal edges
                if target == f:
                    return
                meta: dict[str, Any] = {"imported_names": names, **(extra or {})}
                if test:
                    meta["test_only"] = True
                b.add_edge(src, b.file_id(target), REL_IMPORTS, analyzer=self.name,
                           evidence=[self.evidence(ctx, where or f, line, line, construct)], confidence=confidence,
                           metadata=meta)
                done.add(target)
                edges += 1

            def used(members: dict[str, list[str]]) -> list[str]:
                """The simple type names of ``members`` the file uses (``[Audited]`` uses ``AuditedAttribute``)."""
                out = [t for t in members if t in refs]
                out += [t for t in members if t.endswith("Attribute") and t[:-9] in refs and t not in refs]
                return sorted(out)

            # its own namespace and the enclosing ones: no using needed
            own = {e for ns in info["namespaces"] or [""] for e in _enclosing(ns)}
            for ns in sorted(own, key=len, reverse=True):
                members = ns_types.get(ns) or {}
                for t in used(members):
                    target = nearest(members[t], f)
                    if target not in done:
                        link(target, refs.get(t) or refs.get(t[:-9], 1), "same-namespace",
                             [f"{ns}.{t}" if ns else t], 0.85, {"same_package": True})
            directives = [[*u, f] for u in info["usings"] if not u[1] or u[2] or u[3]]
            directives += [u for u in info["usings"] if u[1] and (u[2] or u[3])]  # a global static / alias: here
            for name, is_global, static, alias, line, *where in directives + global_usings.get(project_of(f), []):
                where_file = where[0] if where else f
                if (static or alias) and name in types:  # using static A.B.C; / using X = A.B.C;
                    link(nearest(types[name], f), line, "using-static" if static else "using-alias", [name], 1.0,
                         where=where_file)
                    continue
                if not static and not alias and name in known_ns:
                    members = ns_types.get(name) or {}
                    hit = False
                    for t in used(members):
                        target = nearest(members[t], f)
                        if target not in done:
                            link(target, line, "global-using" if is_global and where_file != f else "using",
                                 [f"{name}.{t}"], 0.9, where=where_file)
                        hit = True
                    for method in sorted((ns_ext.get(name) or {}).keys() & calls.keys()):  # extension methods
                        for target in (ns_ext[name][method])[:3]:
                            if target not in done:
                                link(target, calls[method], "extension-method", [f"{name}.{method}"], 0.8)
                                hit = True
                    if not hit:
                        b.stat(self.name, "usings_unused")
                    continue
                if is_global and where_file != f:
                    continue  # an external global using counts once, in the file that declares it
                self._external(ctx, b, f, src, name, static, line, roots, packages, test, ext_memo)
            for fq, line in info["qualified"].items():  # Shop.Core.Order in code, without a using
                parts = fq.split(".")
                for i in range(len(parts), 1, -1):
                    cand = ".".join(parts[:i])
                    if cand in types:
                        target = nearest(types[cand], f)
                        if target not in done:
                            link(target, line, "qualified-name", [cand], 0.9)
                        break
        b.stat(self.name, "import_edges", edges)

    def _external(self, ctx: AnalysisContext, b: SnapshotBuilder, f: str, src: str, name: str, static: bool,
                  line: int, roots: set[str], packages: list[Any], test: bool,
                  memo: dict[tuple[str, str], tuple[Any, ...]]) -> None:
        folder = posixpath.dirname(f)
        ns = name.rsplit(".", 1)[0] if static and "." in name else name  # using static A.B.C: the namespace A.B
        key = (ns, folder)
        if key not in memo:
            if any(ns == r or ns.startswith(r + ".") for r in roots):
                memo[key] = ("internal",)  # generated code (gRPC, resources…) or a namespace not analyzed
            else:
                std = _stdlib(ns)  # a framework namespace goes to a package only when the id is its prefix
                pkg = self._match_package(ns, folder, packages, prefix_only=std)
                if pkg is not None:
                    memo[key] = ("dotnet", pkg, False)
                elif std:
                    memo[key] = ("dotnet-stdlib", ".".join(ns.split(".")[:2]), True)
                else:
                    memo[key] = ("dotnet", ".".join(ns.split(".")[:2]), False)
        what = memo[key]
        if what[0] == "internal":
            b.stat(self.name, "unresolved_internal_like")
            return
        eco, ext_name, std = what
        key_name = ext_name.lower() if eco == "dotnet" else ext_name
        ext_id = b.external_id(eco, key_name)
        if ext_id not in b.nodes:
            b.add_node(ComponentNode(id=ext_id, name=ext_name, qualified_name=ext_name,
                                     component_type="external-package", analyzer=self.name,
                                     key=f"external:{eco}:{key_name}", tags=["external"] + (["stdlib"] if std else []),
                                     metadata={"ecosystem": "dotnet"}))
        meta: dict[str, Any] = {"external": True, "imported_names": [name]}
        if test:
            meta["test_only"] = True
        b.add_edge(src, ext_id, REL_IMPORTS, analyzer=self.name,
                   evidence=[self.evidence(ctx, f, line, line, "using")], confidence=0.9, metadata=meta)
