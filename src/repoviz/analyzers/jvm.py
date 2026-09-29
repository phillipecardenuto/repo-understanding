"""Java and Kotlin analyzer (lightweight, #26).

Regular expressions over the text with comments and strings blanked (``textscan``); nothing is compiled or run.

* **Modules.** One per file, named by its ``package`` and file name (``com.acme.web.UserController``).  A folder
  whose files declare one package becomes that package.
* **Imports.** ``import a.b.C`` (a nested class, ``import static a.b.C.m`` and Kotlin top-level functions resolve
  to the file declaring the outer type or function), and ``import a.b.*``, which counts only for the types of
  ``a.b`` the file uses.  Java and Kotlin need no import for the types of their own package: using one is an
  edge too (``same_package``), and so is a fully qualified name in code.  When several files declare a name
  (two modules, main and test sources), the one nearest to the importing file wins.
* **External packages.** The JDK, Kotlin and Android platform packages are standard.  Other imports are matched
  to the Maven / Gradle artifacts the manifests declare, by the longest shared package prefix and then the words
  of the artifact name (``com.fasterxml.jackson.databind`` → ``com.fasterxml.jackson.core:jackson-databind``);
  otherwise they are named by their first package segments (``org.junit.jupiter``).
* **Broken imports.** An import of a missing type from a package that exists here is ``unresolved-internal-import``,
  except names that build tools generate (``R``, ``BuildConfig``, ``Dagger…``, ``*Binding``, ``*Grpc``…).
* **Symbols.** Classes, interfaces, enums, records and objects (nested too), methods, constructors and Kotlin
  functions, with their signature; overloads carry their parameter types.  No call graph.
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
from .textscan import LineIndex, mask_pair, match_open, package_roots as _roots, squash, top_level

LANGS = ("java", "kotlin")
MAX_SYMBOLS = 2000  # per file
MAX_REFS = 5000  # names used, per file
MAX_SIGNATURE = 200

_PACKAGE = re.compile(r"^[ \t]*package\s+([\w.`]+)", re.M)
_JAVA_IMPORT = re.compile(r"^[ \t]*import\s+(static\s+)?([\w.$]+?)(\.\*)?\s*;", re.M)
_KT_IMPORT = re.compile(r"^[ \t]*import\s+([\w.`]+?)(\.\*)?(?:\s+as\s+`?\w+`?)?[ \t]*;?[ \t]*$", re.M)
_ANNOT = re.compile(r"@(?!interface\b)[\w.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?")
_JAVA_TYPE = re.compile(r"(?<![\w.$@])(class|interface|enum|record|@interface)\s+([A-Za-z_$][\w$]*)")
_KT_TYPE = re.compile(r"(?<![\w.`])(?:(companion)\s+)?(class|interface|object)\b[ \t]*(`[^`\n]+`|[A-Za-z_]\w*)?")
_KT_FUN = re.compile(r"(?<![\w.`])fun\b\s*(?:<(?:[^<>]|<[^<>]*>)*>\s*)?(?:([\w.<>?, *]+?)\s*\.\s*)?"
                     r"(`[^`\n]+`|[A-Za-z_]\w*)\s*\(")
_KT_PROP = re.compile(r"^[ \t]*(?:(?:private|internal|public|protected|const|lateinit|override|open|actual|expect)\s+)*"
                      r"(?:val|var)\s+(?:<[^>\n]*>\s*)?(?:[\w.<>?]+\.)?(`[^`\n]+`|[A-Za-z_]\w*)", re.M)
_THROWS = re.compile(r"\bthrows\b[\w.$,\s<>]*$")
_CAP_REF = re.compile(r"(?<![\w.$])([A-Z][\w$]*)")
_CALL_REF = re.compile(r"(?<![\w.$])([a-z_][\w$]*)\s*\(")
_QUALIFIED = re.compile(r"(?<![\w.$])((?:[a-z_][\w$]*\.)+[A-Z][\w$]*)")
_JAVA_MAIN = re.compile(r"\bstatic\s+(?:final\s+)?void\s+main\s*\(|\bvoid\s+main\s*\(\s*(?:final\s+)?String")
_JAVA_MODS = {"public", "protected", "private", "static", "final", "abstract", "synchronized", "native", "default",
              "strictfp", "transient", "volatile", "sealed", "non-sealed"}
_CONTROL = {"if", "for", "while", "switch", "catch", "synchronized", "try", "do", "else", "return", "new", "throw",
            "when", "case", "super", "this", "assert", "yield"}
_TYPE_KIND = {"@interface": "annotation"}
#: JDK and platform packages (``javax`` only where the JDK ships it).
_STDLIB = ("java.", "jdk.", "sun.", "com.sun.", "org.w3c.dom", "org.xml.sax", "org.ietf.jgss", "org.omg.", "kotlin.",
           "android.", "dalvik.", "platform.", "kotlinx.cinterop", "javax.annotation.processing", "javax.crypto", "javax.imageio", "javax.lang.model",
           "javax.management", "javax.naming", "javax.net", "javax.print", "javax.script", "javax.security",
           "javax.smartcardio", "javax.sound", "javax.sql", "javax.swing", "javax.tools", "javax.transaction.xa",
           "javax.xml", "javax.accessibility", "javax.rmi")
#: Classes build tools generate into the project's own packages: importing one is not a broken import.
_GENERATED = re.compile(r"^(R|BuildConfig|Manifest|Dagger\w+|Hilt_\w+|AutoValue_\w+|Q[A-Z]\w*|\w+_|\w+Binding(Impl)?|"
                        r"\w+_(Factory|MembersInjector|Impl)|\w+(Proto|OuterClass|Grpc|MapperImpl|Builder))$")


# --------------------------------------------------------------------------- parsing (pure, cached per content)


@dataclass
class _Open:
    kind: str  # type | member | block
    decl: dict[str, Any] | None = None


@dataclass
class _Scan:
    code: str
    noc: str
    text: str
    lang: str
    lines: LineIndex
    decls: list[dict[str, Any]] = field(default_factory=list)


def _add(scan: _Scan, parent: dict[str, Any] | None, name: str, kind: str, start: int, end: int | None,
         signature: str | None = None, public: bool = True, params: str | None = None) -> dict[str, Any]:
    name = name.strip("`")
    d = {"name": name, "kind": kind, "parent": parent["qual"] if parent else None,
         "qual": f"{parent['qual']}.{name}" if parent else name, "depth": (parent["depth"] + 1) if parent else 0,
         "start": start, "end": end, "signature": signature, "public": public and (parent is None or parent["public"]),
         "params": params, "interface": kind in ("interface", "annotation")}
    if len(scan.decls) < MAX_SYMBOLS:
        scan.decls.append(d)
    return d


def _start(header: str, offset: int) -> int:
    """Offset of the first non-blank character of a declaration header (its annotations included)."""
    stripped = len(header) - len(header.lstrip())
    return offset + stripped


def _param_types(params: str, lang: str) -> str:
    out = []
    depth, cur = 0, ""
    for ch in params + ",":
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            p = _ANNOT.sub(" ", cur).strip()
            if p:
                if lang == "kotlin":
                    p = p.split(":", 1)[1] if ":" in p else p
                    p = p.split("=", 1)[0]
                else:
                    p = re.sub(r"\bfinal\s+", "", p).rsplit(None, 1)[0] if len(p.split()) > 1 else p
                out.append(squash(p))
            cur = ""
        else:
            cur += ch
    return ", ".join(out)


def _java_header(scan: _Scan, header: str, offset: int, parent: dict[str, Any] | None, ch: str) -> dict[str, Any] | None:
    clean = _ANNOT.sub(lambda m: " " * len(m.group()), header)
    t = _JAVA_TYPE.search(clean)
    if t is not None:
        if ch != "{":
            return None
        mods = set(clean[:t.start()].split())
        public = "public" in mods or "protected" in mods or bool(parent and parent["interface"])
        kind = _TYPE_KIND.get(t.group(1), t.group(1))
        return _add(scan, parent, t.group(2), kind, _start(header, offset), None, public=public)
    if parent is None:
        return None
    h = _THROWS.sub("", clean.rstrip()).rstrip()
    if not h.endswith(")"):
        return None
    open_ = match_open(h, len(h) - 1)
    if open_ < 0:
        return None
    nm = re.search(r"([A-Za-z_$][\w$]*)\s*$", h[:open_])
    if nm is None or nm.group(1) in _CONTROL:
        return None
    prefix = h[:nm.start()]
    if ")" in prefix:  # the tail of an annotation array or a statement: keep what follows it
        prefix = prefix[prefix.rindex(")") + 1:]
    words = prefix.split()
    mods = {w for w in words if w in _JAVA_MODS}
    ret = " ".join(w for w in words if w not in _JAVA_MODS).strip()
    if ret.startswith("<"):  # type parameters of a generic method
        depth = 0
        for i, c in enumerate(ret):
            depth += (c == "<") - (c == ">")
            if depth == 0:
                ret = ret[i + 1:].strip()
                break
    if top_level(prefix, "=") or (ret and not re.fullmatch(r"[\w$.<>\[\]?,&\s]+", ret)):
        return None
    name = nm.group(1)
    if not ret and name != parent["name"]:
        return None  # neither a method (no return type) nor a constructor: an enum constant with a body
    params = squash(h[open_ + 1:-1])
    public = "public" in mods or "protected" in mods or parent["interface"]
    if "private" in mods:
        public = False
    sig = f"({params})" + (f" -> {squash(ret)}" if ret else "")
    return _add(scan, parent, name, "method" if ret else "constructor", _start(header, offset),
                None if ch == "{" else offset + len(header), signature=sig, public=public, params=params)


def _kotlin_decls(scan: _Scan, header: str, offset: int, parent: dict[str, Any] | None,
                  brace: bool) -> dict[str, Any] | None:
    """The declarations of one stretch of Kotlin between two braces / semicolons; returns the one that owns the
    brace that ends it (its body), if any.  The others have no body (``fun x() = 1``, ``data class P(val x: Int)``,
    an abstract ``fun``)."""
    found: list[tuple[int, str, Any]] = []
    for m in _KT_TYPE.finditer(header):
        if m.group(3) or m.group(1):
            found.append((m.start(), "type", m))
    for m in _KT_FUN.finditer(header):
        found.append((m.start(), "fun", m))
    found.sort(key=lambda x: x[0])
    owner = None
    for i, (pos, what, m) in enumerate(found):
        seg_end = found[i + 1][0] if i + 1 < len(found) else len(header)
        last = i + 1 == len(found)
        before = header[:pos]
        line_start = before.rfind("\n") + 1
        mods = set(re.sub(r"@[\w.]+(\([^)]*\))?", " ", before[line_start:]).split())
        public = not ({"private", "internal"} & mods)
        start = offset + line_start + (len(before[line_start:]) - len(before[line_start:].lstrip()))
        if what == "type":
            name = m.group(3) or "Companion"
            kind = "object" if m.group(2) == "object" else ("enum" if "enum" in mods else
                                                           "annotation" if "annotation" in mods else m.group(2))
            tail = header[m.end():seg_end]
            owns = brace and last and not re.search(r"\n\s*(?!where\b)[A-Za-z_@]", _strip_parens(tail))
            stop = m.end()
            if tail.lstrip().startswith("(") or re.match(r"\s*<", tail):  # type parameters, primary constructor
                paren = header.find("(", m.end(), seg_end)
                stop = (_close_paren(header, paren) if paren >= 0 else -1) + 1 or m.end()
            end = None if owns else offset + _line_end(header, stop, seg_end)
            d = _add(scan, parent, name, kind, start, end, public=public)
        else:
            close = _close_paren(header, m.end() - 1)
            if close < 0:
                continue
            params = squash(scan.noc[offset + m.end():offset + close])  # string defaults kept
            tail = header[close + 1:seg_end]
            ret = re.match(r"\s*:\s*([^={\n]+)", tail)
            sig = f"({params})" + (f": {squash(ret.group(1))}" if ret else "")
            owns = brace and last and not re.search(r"\n\s*(?!where\b)[A-Za-z_@]", tail)
            end = None if owns else offset + _line_end(header, close, seg_end)
            kind = "method" if parent is not None else "function"
            d = _add(scan, parent, m.group(2), kind, start, end, signature=sig, public=public, params=params)
            if m.group(1):
                d["receiver"] = squash(m.group(1))
        if owns:
            owner = d
    return owner


def _line_end(text: str, pos: int, limit: int) -> int:
    """Where a declaration without a body that ends its signature at ``pos`` stops: the end of that line, or of the
    next one when the line ends with ``=`` (``fun f() =`` + an expression on the next line)."""
    nl = text.find("\n", pos, limit)
    end = limit if nl < 0 else nl
    if text[pos:end].rstrip().endswith("=") and nl >= 0:
        nxt = text.find("\n", nl + 1, limit)
        end = limit if nxt < 0 else nxt
    return len(text[:end].rstrip()) - 1


def _strip_parens(text: str) -> str:
    """``text`` with the insides of (…) removed (a primary constructor spanning lines)."""
    out, depth = [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _close_paren(text: str, open_pos: int) -> int:
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
    for m in re.finditer(r"[{};]", code):
        pos, ch = m.start(), m.group()
        top = stack[-1] if stack else None
        parent = top.decl if top is not None and top.kind == "type" else None
        in_body = top is None or parent is not None  # at file level, or right inside a type's body
        header = code[boundary + 1:pos]
        decl = None
        if in_body and header.strip():
            if scan.lang == "kotlin":
                decl = _kotlin_decls(scan, header, boundary + 1, parent, ch == "{")
            elif ch != "}":
                decl = _java_header(scan, header, boundary + 1, parent, ch)
        if ch == "}":
            if stack:
                closed = stack.pop()
                if closed.decl is not None:
                    closed.decl["end"] = pos
        elif ch == "{":
            kind = "block"
            if decl is not None:
                kind = "type" if decl["kind"] in ("class", "interface", "enum", "record", "annotation", "object") \
                    else "member"
            stack.append(_Open(kind, decl))
        boundary = pos
    if scan.lang == "kotlin" and code[boundary + 1:].strip():  # declarations after the last brace: fun f() = 1
        top = stack[-1] if stack else None
        parent = top.decl if top is not None and top.kind == "type" else None
        if top is None or parent is not None:
            _kotlin_decls(scan, code[boundary + 1:], boundary + 1, parent, False)
    for d in scan.decls:  # unclosed (a truncated file): up to the end
        if d["end"] is None:
            d["end"] = len(code) - 1


def parse_jvm(text: str, lang: str) -> dict[str, Any]:
    """Everything the analyzer needs from one Java or Kotlin file, as plain JSON."""
    if text.startswith("\ufeff"):  # a byte-order mark: a blank, so the package line still starts its line
        text = " " + text[1:]
    code, noc = mask_pair(text)
    lines = LineIndex(text)
    pkg = _PACKAGE.search(code)
    package = pkg.group(1).replace("`", "") if pkg else ""
    imports: list[list[Any]] = []
    body = list(code)
    spans = []
    if pkg:
        spans.append((pkg.start(), pkg.end()))
    if lang == "java":
        for m in _JAVA_IMPORT.finditer(code):
            imports.append([m.group(2), bool(m.group(3)), bool(m.group(1)), lines.line(m.start(2))])
            spans.append((m.start(), m.end()))
    else:
        for m in _KT_IMPORT.finditer(code):
            imports.append([m.group(1).replace("`", ""), bool(m.group(2)), False, lines.line(m.start(1))])
            spans.append((m.start(), m.end()))
    for a, b in spans:  # names in the package and import lines are not uses
        body[a:b] = " " * (b - a)
    if lang == "kotlin":  # nor is the name of a function where it is declared
        for m in _KT_FUN.finditer(code):
            a, b = m.span(2)
            body[a:b] = " " * (b - a)
    body_code = "".join(body)
    scan = _Scan(code, noc, text, lang, lines)
    _declarations(scan)
    # overloads: the parameter types tell them apart (a lone method keeps its bare name)
    count = Counter((d["parent"], d["name"]) for d in scan.decls if d["params"] is not None)
    for d in scan.decls:
        if d["params"] is not None and count[(d["parent"], d["name"])] > 1:
            receiver = f"{d['receiver']}." if d.get("receiver") else ""
            d["qual"] = f"{d['qual']}({receiver}{_param_types(d['params'], lang)})"
    seen: Counter[str] = Counter()
    for d in scan.decls:  # still the same (a conditional declaration twice): the second one is name#2
        seen[d["qual"]] += 1
        if seen[d["qual"]] > 1:
            d["qual"] = f"{d['qual']}#{seen[d['qual']]}"
    symbols = []
    for d in scan.decls:
        a, b = d["start"], d["end"] + 1
        sig = d["signature"]
        symbols.append({"qual": d["qual"], "name": d["name"], "kind": d["kind"], "parent": d["parent"],
                        "line": lines.line(a), "end_line": lines.line(max(a, b - 1)),
                        "fingerprint": stable_hash(text[a:b]), "semantic": stable_hash(squash(noc[a:b])),
                        "signature": sig if sig is None or len(sig) <= MAX_SIGNATURE else sig[:MAX_SIGNATURE - 3] + "...",
                        "signature_id": stable_hash("sig", sig, length=12) if sig else None, "public": d["public"],
                        **({"receiver": d["receiver"]} if d.get("receiver") else {})})
    declared = sorted({d["name"] for d in scan.decls if d["depth"] == 0})
    if lang == "kotlin":  # top-level properties can be imported too
        depth0 = _depth_zero(code)
        declared = sorted(set(declared) | {m.group(1).strip("`") for m in _KT_PROP.finditer(code) if depth0(m.start())})
    refs: dict[str, int] = {}
    for m in _CAP_REF.finditer(body_code):
        if len(refs) >= MAX_REFS:
            break
        refs.setdefault(m.group(1), m.start())
    if lang == "kotlin":
        for m in _CALL_REF.finditer(body_code):
            if len(refs) >= MAX_REFS:
                break
            refs.setdefault(m.group(1), m.start())
    qualified: dict[str, int] = {}
    for m in _QUALIFIED.finditer(body_code):
        if len(qualified) >= 500:
            break
        qualified.setdefault(m.group(1), m.start())
    entry = None
    if lang == "java" and _JAVA_MAIN.search(code):
        entry = "java main method"
    elif lang == "kotlin" and any(d["name"] == "main" and d["depth"] == 0 and d["kind"] == "function" for d in scan.decls):
        entry = "kotlin main function"
    if re.search(r"@SpringBootApplication\b", code):
        entry = "Spring Boot application"
    return {"package": package, "imports": imports, "symbols": symbols, "declared": declared,
            "refs": {k: lines.line(v) for k, v in refs.items()},
            "qualified": {k: lines.line(v) for k, v in qualified.items()}, "entry": entry,
            "semantic": stable_hash(squash(noc)), "loc": text.count("\n") + (0 if text.endswith("\n") or not text else 1)}


def _depth_zero(code: str) -> Any:
    """A test for "this offset is outside every brace"."""
    import bisect

    marks: list[int] = []
    depths: list[int] = []
    depth = 0
    for m in re.finditer(r"[{}]", code):
        depth += 1 if m.group() == "{" else -1
        marks.append(m.start())
        depths.append(depth)
    return lambda pos: (depths[i - 1] if (i := bisect.bisect_right(marks, pos)) else 0) <= 0


def _parse_item(item: tuple[str, str]) -> tuple[str, dict[str, Any]]:
    path, text = item
    return path, parse_jvm(text, "kotlin" if path.endswith((".kt", ".kts")) else "java")


# --------------------------------------------------------------------------- the analyzer


def _stdlib(name: str) -> bool:
    return any(name == p.rstrip(".") or name.startswith(p if p.endswith(".") else p + ".") for p in _STDLIB) \
        or name in ("kotlin", "java")


def _import_package(name: str, star: bool) -> str:
    """The package an import names: up to its first type (a segment that starts upper-case); without one, a Kotlin
    top-level function or property (``kotlinx.coroutines.launch``), so its last segment is dropped."""
    if star:
        return name
    parts = name.split(".")
    for i, seg in enumerate(parts):
        if seg[:1].isupper():
            return ".".join(parts[:i]) or name
    return ".".join(parts[:-1]) or name


class JvmAnalyzer(Analyzer):
    name = "jvm"
    version = "3"  # bump when the parse result or the graph changes (part of the cache keys)
    languages = LANGS
    capabilities = (CAP_MODULES, CAP_SYMBOLS, CAP_DEPENDENCIES, CAP_ENTRY_POINTS)

    def detect(self, ctx: AnalysisContext) -> Detection:
        n = sum(len(ctx.files(lang)) for lang in LANGS)
        return Detection(bool(n), f"{n} Java/Kotlin file(s)" if n else "no Java or Kotlin files")

    def discover_modules(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        texts: dict[str, tuple[tuple[Any, ...], str]] = {}
        manifests = ctx.profile.manifest_data
        for f in sorted(set(ctx.files("java")) | set(ctx.files("kotlin"))):
            if f in manifests or f.endswith(".gradle.kts"):  # build scripts are configuration, not modules
                continue
            text = ctx.text(f)
            if text is None:
                continue
            digest = ctx.source.content_hash(f) or stable_hash(text)
            texts[f] = (("jvm", self.version, digest, f.endswith((".kt", ".kts"))), text)
        misses = [(f, text) for f, (key, text) in texts.items() if key not in ctx.file_cache]
        for f, parsed in parse_parallel(_parse_item, misses).items():
            ctx.file_cache[texts[f][0]] = parsed
        infos: dict[str, dict[str, Any]] = {}
        for f, (key, text) in texts.items():
            infos[f] = ctx.cached(key, lambda f=f, t=text: _parse_item((f, t))[1])
        ctx.shared["jvm.infos"] = infos
        by_dir: dict[str, Counter[str]] = {}
        for f, info in infos.items():
            lang = "kotlin" if f.endswith((".kt", ".kts")) else "java"
            stem = posixpath.splitext(posixpath.basename(f))[0]
            pkg = info["package"]
            node = b.ensure_file(f, self.name)
            tags = [lang] + (["test"] if ctx.profile.is_test(f) else [])
            if info["entry"] and "test" not in tags:
                tags.append("entry-point")
            b.add_node(ComponentNode(
                id=node.id, name=posixpath.basename(f), qualified_name=f"{pkg}.{stem}" if pkg else stem,
                component_type="module", category=CATEGORY_MODULE, language=lang, path=f, analyzer=self.name,
                key=node.key, tags=tags,
                metadata={"package": pkg or None, "loc": info["loc"], "semantic_fingerprint": info["semantic"],
                          **({"entry_kind": info["entry"]} if info["entry"] and "test" not in tags else {})}))
            node.category = CATEGORY_MODULE
            if pkg:
                by_dir.setdefault(posixpath.dirname(f), Counter())[pkg] += 1
        for d, pkgs in by_dir.items():  # a folder of one package is that package
            if not d:
                continue
            pkg = pkgs.most_common(1)[0][0]
            dnode = b.nodes[b.ensure_dir(d, self.name)]
            b.add_node(ComponentNode(
                id=dnode.id, name=dnode.name, qualified_name=pkg, component_type="package", path=d,
                analyzer=self.name, key=dnode.key, language=None,
                metadata={"qualified_name_authoritative": True, "jvm_package": pkg}))
        b.stat(self.name, "modules", len(infos))

    def discover_symbols(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, dict[str, Any]] = ctx.shared.get("jvm.infos", {})
        for f, info in infos.items():
            lang = "kotlin" if f.endswith((".kt", ".kts")) else "java"
            module = b.nodes[b.file_id(f)]
            prefix = module.qualified_name.rpartition(".")[0] if info["package"] else ""
            for s in info["symbols"]:
                kind = s["kind"]
                ctype = "class" if kind not in ("method", "constructor", "function") else \
                    ("function" if kind == "function" else "method")
                meta: dict[str, Any] = {"kind": kind, "public": s["public"], "semantic_fingerprint": s["semantic"]}
                if s["signature"]:
                    meta["signature"] = s["signature"]
                    meta["signature_id"] = s["signature_id"]
                if s.get("receiver"):
                    meta["receiver"] = s["receiver"]
                qual = f"{prefix}.{s['qual']}" if prefix else s["qual"]
                b.add_node(ComponentNode(
                    id=b.symbol_id(f, s["qual"]), name=s["name"], qualified_name=qual, component_type=ctype,
                    category=CATEGORY_SYMBOL, language=lang, path=f,
                    parent_id=b.symbol_id(f, s["parent"]) if s["parent"] else module.id, analyzer=self.name,
                    key=f"symbol:{f}:{s['qual']}", fingerprint=s["fingerprint"], start_line=s["line"],
                    end_line=s["end_line"], metadata=meta))
                b.stat(self.name, "symbols")
        if infos:
            b.diagnostic("info", "callflow-unsupported", "Call-flow extraction is not implemented for Java and "
                         "Kotlin; the Activity tab falls back to module-level import impact.", self.name)

    # -- dependencies -------------------------------------------------------------------------------------------

    def _declared_artifacts(self, ctx: AnalysisContext) -> list[tuple[tuple[str, ...], frozenset[str], tuple[str, ...], str]]:
        """Every Maven / Gradle dependency once: ``(groupId segments, words of the artifactId, the manifest folders
        that declare it, declared name)``."""
        found: dict[str, tuple[tuple[str, ...], frozenset[str], set[str]]] = {}
        for md in ctx.profile.manifest_data.values():
            if md.ecosystem not in ("maven", "gradle"):
                continue
            for dep in md.dependencies:
                if getattr(dep, "workspace", False) or ":" not in dep.name:
                    continue
                group, artifact = dep.name.split(":")[:2]
                if not group or group.startswith("$"):
                    continue
                key = dep.name.lower()
                if key not in found:
                    words = frozenset(w for w in re.split(r"[-._]", artifact.lower()) if len(w) > 2)
                    found[key] = (tuple(group.split(".")), words, set())
                found[key][2].add(md.dir)
        return [(g, w, tuple(sorted(dirs)), name) for name, (g, w, dirs) in sorted(found.items())]

    @staticmethod
    def _match_artifact(name: str, folder: str,
                        artifacts: list[tuple[tuple[str, ...], frozenset[str], tuple[str, ...], str]]) -> str | None:
        """The declared artifact an external import (its package ``name``, from a file in ``folder``) most likely
        comes from: the longest groupId prefix shared, then the artifact's words in the package, then a manifest
        that covers the file.  A tie is no match."""
        segs = name.split(".")
        lower = {x.lower() for x in segs}
        best: tuple[int, int, int] | None = None
        pick = None
        tied = False
        for group, words, dirs, declared in artifacts:
            shared = 0
            for a, c in zip(group, segs):
                if a != c:
                    break
                shared += 1
            hits = len(words & lower)
            if shared < 2 and hits < 2:  # kotlinx.coroutines: org.jetbrains.kotlinx:kotlinx-coroutines-core
                continue
            near = 1 if any(not d or folder == d or folder.startswith(d + "/") for d in dirs) else 0
            score = (shared, hits, near)
            if best is None or score > best:
                best, pick, tied = score, declared, False
            elif score == best:
                tied = True
        return None if tied else pick

    def discover_dependencies(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, dict[str, Any]] = ctx.shared.get("jvm.infos", {})
        if not infos:
            return
        types: dict[str, list[str]] = {}  # fully qualified top-level name -> files declaring it
        nested: dict[str, list[str]] = {}  # fully qualified nested type -> files
        packages: dict[str, list[str]] = {}
        for f, info in infos.items():
            pkg = info["package"]
            packages.setdefault(pkg, []).append(f)
            for name in info["declared"]:
                types.setdefault(f"{pkg}.{name}" if pkg else name, []).append(f)
            for s in info["symbols"]:
                if s["parent"] and s["kind"] not in ("method", "constructor", "function"):
                    nested.setdefault(f"{pkg}.{s['qual']}" if pkg else s["qual"], []).append(f)
        pkg_types: dict[str, dict[str, list[str]]] = {}
        for fq, files in types.items():
            pkg, _, simple = fq.rpartition(".")
            pkg_types.setdefault(pkg, {})[simple] = files
        roots = _roots(packages)
        artifacts = self._declared_artifacts(ctx)
        memo: dict[tuple[str, bool, str], tuple[Any, ...]] = {}  # what an import names, per package folder
        edges = 0

        near_memo: dict[tuple[int, str], str] = {}

        def nearest(files: list[str], f: str) -> str:
            """The declaring file closest to ``f``; between Kotlin multiplatform source sets, the common one."""
            if len(files) == 1:
                return files[0]
            key = (id(files), posixpath.dirname(f))
            if key not in near_memo:
                near_memo[key] = max(files, key=lambda x: (len(posixpath.commonprefix([x, key[1] + "/"])),
                                                           "/commonMain/" in x, -len(x), x))
            return near_memo[key]

        def resolve(fq: str) -> tuple[str | None, str]:
            """The file declaring ``fq`` (a type, a nested type, a member of one, or a Kotlin top-level name)."""
            parts = fq.split(".")
            for i in range(len(parts), 0, -1):
                cand = ".".join(parts[:i])
                if cand in types:
                    return cand, "type"
                if cand in nested:
                    return cand, "nested"
            return None, ""

        for f, info in infos.items():
            src = b.file_id(f)
            test = ctx.profile.is_test(f)
            pkg = info["package"]
            done: set[str] = set()
            refs = info["refs"]
            imported = {n.rsplit(".", 1)[-1] for n, star, _s, _l in info["imports"] if not star}

            def link(target_file: str, line: int, construct: str, names: list[str], confidence: float,
                     extra: dict[str, Any] | None = None) -> None:
                nonlocal edges
                if target_file == f:
                    return
                meta: dict[str, Any] = {"imported_names": names, **(extra or {})}
                if test:
                    meta["test_only"] = True
                b.add_edge(src, b.file_id(target_file), REL_IMPORTS, analyzer=self.name,
                           evidence=[self.evidence(ctx, f, line, line, construct)], confidence=confidence,
                           metadata=meta)
                done.add(target_file)
                edges += 1

            for name, star, static, line in info["imports"]:
                if star and not static:  # a whole package: only the types this file uses
                    members = pkg_types.get(name)
                    if members is not None:
                        for simple in sorted(refs.keys() & members.keys()):
                            link(nearest(members[simple], f), line, "import-on-demand", [f"{name}.{simple}"], 0.9)
                        continue
                target, how = resolve(name)
                if target is not None:
                    files = types.get(target) or nested.get(target) or []
                    link(nearest(files, f), line, "static-import" if static else "import", [name],
                         1.0 if how == "type" and (target == name or static or star) else 0.95)
                    continue
                self._external_or_broken(ctx, b, f, src, name, star, line, packages, roots, artifacts, test, memo)
            # the types (and Kotlin top-level functions) of its own package, used without an import
            members = pkg_types.get(pkg) or {}
            for simple in sorted((refs.keys() & members.keys()) - imported):
                line = refs[simple]
                target_file = nearest(members[simple], f)
                if target_file not in done:  # a function by its name alone is less certain than a type
                    link(target_file, line, "same-package", [f"{pkg}.{simple}" if pkg else simple],
                         0.85 if simple[:1].isupper() else 0.7, {"same_package": True})
            for fq, line in info["qualified"].items():  # com.acme.util.Strings.join(…) without an import
                target, _how = resolve(fq)
                if target is not None:
                    target_file = nearest(types.get(target) or nested.get(target) or [], f)
                    if target_file not in done:
                        link(target_file, line, "qualified-name", [fq], 0.9)
        b.stat(self.name, "import_edges", edges)

    def _external_or_broken(self, ctx: AnalysisContext, b: SnapshotBuilder, f: str, src: str, name: str, star: bool,
                            line: int, packages: dict[str, list[str]], roots: set[str], artifacts: list[Any],
                            test: bool, memo: dict[tuple[str, bool, str], tuple[Any, ...]]) -> None:
        folder = posixpath.dirname(f)
        key = (name, star, folder)
        if key not in memo:
            memo[key] = self._classify(name, star, folder, packages, roots, artifacts)
        what = memo[key]
        if what[0] == "internal":  # the repository's own package root, but nothing declares it here
            b.stat(self.name, "unresolved_internal_like")
            return
        if what[0] == "broken":
            b.diagnostic("warning", "unresolved-internal-import",
                         f"'{name}' does not exist in the repository (package '{what[1]}' has no type "
                         f"'{what[2]}').", self.name, f, line)
            return
        _kind, eco, ext_name, std = what
        meta: dict[str, Any] = {"external": True, "imported_names": [name]}
        if test:
            meta["test_only"] = True
        key_name = ext_name.lower() if eco == "maven" else ext_name
        ext_id = b.external_id(eco, key_name)
        if ext_id not in b.nodes:
            b.add_node(ComponentNode(id=ext_id, name=ext_name, qualified_name=ext_name,
                                     component_type="external-package", analyzer=self.name,
                                     key=f"external:{eco}:{key_name}", tags=["external"] + (["stdlib"] if std else []),
                                     metadata={"ecosystem": "maven" if eco == "maven" else "jvm"}))
        b.add_edge(src, ext_id, REL_IMPORTS, analyzer=self.name,
                   evidence=[self.evidence(ctx, f, line, line, "import")], confidence=0.9, metadata=meta)

    def _classify(self, name: str, star: bool, folder: str, packages: dict[str, list[str]], roots: set[str],
                  artifacts: list[Any]) -> tuple[Any, ...]:
        """What an unresolved import names: ``("broken", package, type)``, ``("internal",)`` (under the
        repository's own package root), or ``("external", ecosystem, name, stdlib)``."""
        pkg = _import_package(name, star)
        if pkg in packages or any(pkg == r or pkg.startswith(r + ".") for r in roots):
            missing = "" if star else (name[len(pkg) + 1:].split(".")[0] if name.startswith(pkg + ".") else "")
            if pkg in packages and missing[:1].isupper() and not _GENERATED.match(missing):
                return ("broken", pkg, missing)
            return ("internal",)
        if _stdlib(name):
            return ("external", "jvm-stdlib", ".".join(pkg.split(".")[:2]), True)
        declared = self._match_artifact(pkg, folder, artifacts)
        return ("external", "maven", declared or ".".join(pkg.split(".")[:3]), False)
