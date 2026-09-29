"""Rust analyzer (lightweight, #26).

Regular expressions over the text with comments and literals blanked (``textscan``); nothing is compiled or run.
Rust's modules are files, so the analyzer rebuilds the module tree the compiler would:

* **Crates.** Every Cargo package: ``src/lib.rs`` and ``src/main.rs``, ``src/bin/*``, ``tests/*``, ``examples/*``
  and ``benches/*`` are crate roots (named after the package, with ``-`` as ``_``, or after the file).
* **Module tree.** From each root, ``mod x;`` means ``x.rs`` or ``x/mod.rs`` next to it (``#[path = "…"]``
  overrides); ``mod x { … }`` is an inline module of the same file.  A module is named by its path
  (``shop::orders::store``).  Files no root reaches are named by their folder.
* **Paths.** Every ``use`` tree (groups, ``*``, ``as``, ``pub use``) and every path in code that starts with
  ``crate``, ``self``, ``super``, a child module or another crate of the workspace (``crate::db::open()``) is
  resolved segment by segment down the tree; the edge goes to the file of the deepest module named.  Anything
  else names an external crate: ``std``, ``core``, ``alloc`` are standard, the rest match ``Cargo.toml``
  dependencies (``serde_json`` for ``serde-json``).
* **Symbols.** Functions, structs, enums, unions, traits, type aliases and macros (``macro_rules!``), and the
  methods of ``impl`` blocks (``Order::total``, ``<Order as Display>::fmt``), with signatures; ``pub`` is public,
  ``pub(crate)`` is not.  ``fn main`` of a binary root is an entry point.  No call graph.
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
from .textscan import LineIndex, mask_pair, squash

MAX_SYMBOLS = 2000
MAX_PATHS = 2000
MAX_SIGNATURE = 200
STD_CRATES = {"std", "core", "alloc", "proc_macro", "test"}

#: Rust: comments, raw strings (r#"…"#, br"…"), strings (they may span lines), byte strings and characters.  A
#: character is exactly one character or escape between quotes, so lifetimes ('a, 'static) stay code.
RUST_TOKENS = re.compile(r"//[^\n]*|/\*.*?(?:\*/|\Z)|\bb?r(#*)\".*?\"\1|b?\"(?:\\.|[^\"\\])*\"?|"
                         r"b?'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]{1,6}\}|.)|[^'\\\n])'", re.S)
_ATTR = re.compile(r"#!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]")
_PATH_ATTR = re.compile(r'#\[\s*path\s*=\s*"([^"]+)"\s*\]')
_VIS = r"(?:pub(?:\s*\(\s*(?:crate|super|self|in\s+[\w:]+)\s*\))?\s+)?"
_MOD = re.compile(rf"^\s*{_VIS}mod\s+([A-Za-z_]\w*)\s*$")
_FN = re.compile(r"(?<![\w])fn\s+([A-Za-z_]\w*)\s*(?:<(?:[^<>]|<[^<>]*>)*>)?\s*\(")
_ITEM = re.compile(r"(?<![\w])(struct|enum|union|trait|type)\s+([A-Za-z_]\w*)")
_MACRO = re.compile(r"(?<![\w])macro_rules!\s*([A-Za-z_]\w*)")
_IMPL = re.compile(r"(?<![\w])impl\b\s*(?:<(?:[^<>]|<[^<>]*>)*>)?\s*(.*?)\s*(?:\bwhere\b.*)?$", re.S)
_USE = re.compile(rf"(?<![\w:]){_VIS}use\s+")
_EXTERN = re.compile(r"(?<![\w])extern\s+crate\s+([A-Za-z_]\w*)(?:\s+as\s+([A-Za-z_]\w*))?\s*;")
#: An item-level macro invocation with braces (``cfg_rt! {``), and an if / else branch inside one (``cfg_if!``).
_INVOKE = re.compile(r"\s*(?!macro_rules!)[A-Za-z_][\w:]*!\s*$")
_BRANCH = re.compile(r"\s*(?:if\b.*|else(?:\s+if\b.*)?)\s*$", re.S)
_PATH = re.compile(r"(?<![\w:])((?:crate|super|self|[a-z_][a-z0-9_]*)(?:::[A-Za-z_]\w*)+)")


# --------------------------------------------------------------------------- parsing (pure, cached per content)


@dataclass
class _Open:
    kind: str  # mod | impl | trait | macro (items inside belong to the enclosing module) | block
    name: str = ""
    decl: dict[str, Any] | None = None


@dataclass
class _Scan:
    code: str
    noc: str  # comments blanked, strings kept (#[path = "…"])
    lines: LineIndex
    decls: list[dict[str, Any]] = field(default_factory=list)
    mods: list[dict[str, Any]] = field(default_factory=list)  # mod x; declarations (a file module)
    inline: list[dict[str, Any]] = field(default_factory=list)  # mod x { … }: name, path, span


def _public(header: str) -> bool:
    return bool(re.match(r"\s*pub\s", header)) and not re.match(r"\s*pub\s*\(", header)


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


def _type_name(text: str) -> str:
    """``&'a mut Vec<T>`` → ``Vec``; ``crate::a::Order<T>`` → ``Order``."""
    t = re.sub(r"<.*", "", text.strip().lstrip("&").strip())
    t = re.sub(r"^(?:mut\s+|dyn\s+|'\w+\s+)+", "", t).strip()
    return t.split("::")[-1].strip() or text.strip()


def _header(scan: _Scan, header: str, offset: int, top: _Open | None, inline_path: list[str],
            ch: str) -> tuple[str, Any] | None:
    """What one item header declares: ("mod", name), ("impl", (type, trait)), ("trait", decl), ("item", decl)."""
    raw = header
    attrs = [m.group(1) for m in _PATH_ATTR.finditer(scan.noc[offset:offset + len(raw)])]
    clean = _ATTR.sub(lambda m: " " * len(m.group()), raw)
    stripped = clean.strip()
    if not stripped:
        return None
    start = offset + len(raw) - len(raw.lstrip())
    in_impl = top is not None and top.kind in ("impl", "trait")
    parent = top.decl if in_impl else None
    m = _MOD.match(clean)
    if m is not None and not in_impl:
        if ch == ";":
            scan.mods.append({"name": m.group(1), "path_attr": attrs[-1] if attrs else None,
                              "cfg": bool(re.search(r"#\[\s*cfg(?:_attr)?\s*\(", raw)),
                              "inline": list(inline_path), "line": scan.lines.line(start + clean.find("mod"))})
            return None
        return ("mod", m.group(1))
    if ch == "{" and not in_impl and re.match(r"\s*(?:unsafe\s+)?impl\b", clean):
        im = _IMPL.match(clean.strip())
        target = (im.group(1) if im else "").strip()
        trait = None
        if re.search(r"\sfor\s", " " + target):
            trait, target = [x.strip() for x in re.split(r"\s+for\s+", target, maxsplit=1)]
            trait = _type_name(trait.lstrip("!"))
        return ("impl", (_type_name(target), trait))
    fm = _FN.search(clean)
    if fm is not None and not re.search(r"=\s*$|\blet\b", clean[:fm.start()]):
        close = _close(clean, fm.end() - 1)
        if close < 0:
            return None
        params = squash(clean[fm.end():close])
        rest = clean[close + 1:]
        ret = re.match(r"\s*->\s*(.+?)\s*(?:\bwhere\b.*)?$", rest, re.S)
        sig = f"({params})" + (f" -> {squash(ret.group(1))}" if ret else "")
        if top is not None and top.kind == "impl":
            type_name, trait = top.name
            qual_parent = f"<{type_name} as {trait}>" if trait else type_name
            public = True if trait else _public(clean)
            d = _add(scan, None, fm.group(1), "method", start, None if ch == "{" else offset + len(raw), sig, public,
                     inline_path, qual_prefix=qual_parent, params=params)
        elif top is not None and top.kind == "trait":
            d = _add(scan, parent, fm.group(1), "method", start, None if ch == "{" else offset + len(raw), sig,
                     True, inline_path, params=params)
        else:
            d = _add(scan, None, fm.group(1), "function", start, None if ch == "{" else offset + len(raw), sig,
                     _public(clean), inline_path, params=params)
        return ("item", d)
    if in_impl:
        return None
    im2 = _ITEM.search(clean)
    if im2 is not None and not re.search(r"=|\blet\b", clean[:im2.start()]):
        kind, name = im2.group(1), im2.group(2)
        end = None if ch == "{" else offset + len(raw)
        d = _add(scan, None, name, kind, start, end, None, _public(clean), inline_path)
        return ("trait", d) if kind == "trait" and ch == "{" else ("item", d)
    mm = _MACRO.search(clean)
    if mm is not None:
        return ("item", _add(scan, None, mm.group(1), "macro", start, None if ch == "{" else offset + len(raw), None,
                             "macro_export" in raw, inline_path))
    return None


def _add(scan: _Scan, parent: dict[str, Any] | None, name: str, kind: str, start: int, end: int | None,
         signature: str | None, public: bool, inline_path: list[str], *, qual_prefix: str | None = None,
         params: str | None = None) -> dict[str, Any]:
    base = "::".join(inline_path)
    if parent is not None:
        qual = f"{parent['qual']}::{name}"
    elif qual_prefix:
        qual = f"{base}::{qual_prefix}::{name}" if base else f"{qual_prefix}::{name}"
    else:
        qual = f"{base}::{name}" if base else name
    d = {"name": name, "kind": kind, "qual": qual, "parent": parent["qual"] if parent else None, "start": start,
         "end": end, "signature": signature, "public": public, "params": params}
    if len(scan.decls) < MAX_SYMBOLS:
        scan.decls.append(d)
    return d


def _declarations(scan: _Scan) -> None:
    code = scan.code
    stack: list[_Open] = []
    boundary = -1
    depth = 0  # parentheses and brackets: a ; inside [u8; 4] ends nothing
    for m in re.finditer(r"[{};()\[\]]", code):
        pos, ch = m.start(), m.group()
        if ch in "([":
            depth += 1
            continue
        if ch in ")]":
            depth = max(0, depth - 1)
            continue
        if ch == ";" and depth:
            continue
        depth = 0
        top = stack[-1] if stack else None
        in_body = top is None or top.kind in ("mod", "impl", "trait", "macro")
        inline_path = [o.name for o in stack if o.kind == "mod"]
        header = code[boundary + 1:pos]
        found = None
        if in_body and ch != "}":
            found = _header(scan, header, boundary + 1, top if top is None or top.kind != "macro" else
                            next((o for o in reversed(stack) if o.kind != "macro"), None), inline_path, ch)
            bare = _ATTR.sub(" ", header)
            if found is None and ch == "{" and (_INVOKE.match(bare) or (top is not None and top.kind == "macro" and
                                                                        _BRANCH.match(bare))):
                found = ("macro", None)
        if ch == "}":
            if stack:
                closed = stack.pop()
                if closed.decl is not None and closed.decl["end"] is None:
                    closed.decl["end"] = pos
                if closed.kind == "mod":
                    for rec in scan.inline:
                        if rec["path"] == inline_path and rec.get("end") is None:
                            rec["end"] = pos
        elif ch == "{":
            if found is None:
                stack.append(_Open("block"))
            elif found[0] == "mod":
                scan.inline.append({"name": found[1], "path": inline_path + [found[1]], "start": pos, "end": None,
                                    "test": bool(re.search(r"#\[\s*cfg\s*\(\s*test\s*\)\s*\]", header))})
                stack.append(_Open("mod", found[1]))
            elif found[0] == "impl":
                stack.append(_Open("impl", found[1]))
            elif found[0] == "macro":
                stack.append(_Open("macro"))
            elif found[0] == "trait":
                stack.append(_Open("trait", found[1]["name"], found[1]))
            else:
                stack.append(_Open("block", "", found[1]))
        boundary = pos
    for d in scan.decls:
        if d["end"] is None:
            d["end"] = len(code) - 1


def _use_trees(text: str) -> list[tuple[list[str], bool, str | None]]:
    """``a::{b, c::{d as e, *}}`` → ``(["a","b"], False, None)``, ``(["a","c","d"], False, "e")``,
    ``(["a","c"], True, None)``."""
    out: list[tuple[list[str], bool, str | None]] = []

    def walk(s: str, prefix: list[str]) -> None:
        s = s.strip()
        if not s:
            return
        if "{" in s:
            head, _, rest = s.partition("{")
            inner = rest[:rest.rfind("}")] if "}" in rest else rest
            base = [x for x in head.strip().rstrip(":").split("::") if x.strip()]
            for part in _split_top(inner):
                walk(part, prefix + [x.strip() for x in base])
            return
        alias = None
        am = re.match(r"(.*?)\s+as\s+(\w+)\s*$", s, re.S)
        if am:
            s, alias = am.group(1), am.group(2)
        segs = [x.strip() for x in s.split("::") if x.strip()]
        if segs and segs[-1] == "*":
            out.append((prefix + segs[:-1], True, None))
        elif segs == ["self"]:
            out.append((prefix, False, alias))
        elif segs:
            out.append((prefix + segs, False, alias))

    walk(text, [])
    return out


def _split_top(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def parse_rust(text: str) -> dict[str, Any]:
    """Everything the analyzer needs from one Rust file, as plain JSON."""
    if text.startswith("\ufeff"):
        text = " " + text[1:]
    code, noc = mask_pair(text, RUST_TOKENS)
    lines = LineIndex(text)
    scan = _Scan(code, noc, lines)
    _declarations(scan)

    def inline_at(pos: int) -> list[str]:
        best: list[str] = []
        for rec in scan.inline:
            if rec["start"] <= pos <= (rec["end"] or len(code)) and len(rec["path"]) > len(best):
                best = rec["path"]
        return best

    uses: list[list[Any]] = []  # [segments, glob, alias, line, inline path]
    body = list(code)
    for m in _USE.finditer(code):
        end = code.find(";", m.end())
        if end < 0:
            continue
        for segs, glob, alias in _use_trees(code[m.end():end]):
            if segs:
                uses.append([segs, glob, alias, lines.line(m.start()), inline_at(m.start())])
        body[m.start():end] = " " * (end - m.start())
    externs = []
    for m in _EXTERN.finditer(code):
        externs.append([m.group(1), lines.line(m.start()), inline_at(m.start())])
        body[m.start():m.end()] = " " * (m.end() - m.start())
    body_code = "".join(body)
    paths: dict[str, list[Any]] = {}
    for m in _PATH.finditer(body_code):
        if len(paths) >= MAX_PATHS:
            break
        where = inline_at(m.start())
        key = "::".join(where) + "|" + m.group(1)
        if key not in paths:
            paths[key] = [m.group(1).split("::"), lines.line(m.start()), where]
    # overloads do not exist in Rust, but trait methods of two impls can share a name within one impl prefix
    seen: Counter[str] = Counter()
    for d in scan.decls:
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
                        "signature_id": stable_hash("sig", sig, length=12) if sig else None, "public": d["public"]})
    has_main = any(d["name"] == "main" and d["kind"] == "function" and "::" not in d["qual"] for d in scan.decls)
    return {"mods": scan.mods, "inline": [{"name": r["name"], "path": r["path"], "test": r["test"]} for r in scan.inline],
            "uses": uses, "externs": externs, "paths": list(paths.values()), "symbols": symbols, "main": has_main,
            "semantic": stable_hash(squash(noc)), "loc": text.count("\n") + (0 if text.endswith("\n") or not text else 1)}


def _parse_item(item: tuple[str, str]) -> tuple[str, dict[str, Any]]:
    return item[0], parse_rust(item[1])


# --------------------------------------------------------------------------- module tree


@dataclass
class _Module:
    crate: str  # crate id (its root file)
    path: tuple[str, ...]  # module names below the crate root
    file: str
    children: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)


def _norm(name: str) -> str:
    return name.replace("-", "_").lower()


class RustAnalyzer(Analyzer):
    name = "rust"
    version = "1"  # bump when the parse result or the graph changes (part of the cache keys)
    languages = ("rust",)
    capabilities = (CAP_MODULES, CAP_SYMBOLS, CAP_DEPENDENCIES, CAP_ENTRY_POINTS)

    def detect(self, ctx: AnalysisContext) -> Detection:
        n = len(ctx.files("rust"))
        return Detection(bool(n), f"{n} Rust file(s)" if n else "no Rust files")

    # -- crates and the module tree ----------------------------------------------------------------------------

    def _crates(self, ctx: AnalysisContext, files: set[str]) -> list[tuple[str, str, str, str]]:
        """``(root file, crate name, package folder, kind)`` of every crate: lib, bin, test, example, bench."""
        out: list[tuple[str, str, str, str]] = []
        packages = {md.dir: md for md in ctx.profile.manifest_data.values() if md.kind == "cargo" and md.name}
        for d, md in sorted(packages.items()):
            name = md.name.replace("-", "_")
            pre = f"{d}/" if d else ""
            for kind, tname, path in md.metadata.get("targets") or []:  # [lib] / [[bin]] … with a path
                if path in files:
                    stem = posixpath.splitext(posixpath.basename(path))[0]
                    out.append((path, (tname or (name if kind == "lib" else stem)).replace("-", "_"), d, kind))
            for rel, kind in (("src/lib.rs", "lib"), ("src/main.rs", "bin")):
                if pre + rel in files:
                    out.append((pre + rel, name, d, kind))
            for f in sorted(files):
                if not f.startswith(pre) or f in (pre + "src/lib.rs", pre + "src/main.rs"):
                    continue
                rest = f[len(pre):]
                m = re.fullmatch(r"(src/bin|tests|examples|benches)/([^/]+?)(?:\.rs|/main\.rs)", rest)
                if m:
                    kind = {"src/bin": "bin", "tests": "test", "examples": "example", "benches": "bench"}[m.group(1)]
                    out.append((f, m.group(2).replace("-", "_"), d, kind))
        if not packages:  # no Cargo.toml: lib.rs / main.rs roots named after their folder
            for f in sorted(files):
                if posixpath.basename(f) in ("lib.rs", "main.rs"):
                    folder = posixpath.dirname(f)
                    base = posixpath.basename(posixpath.dirname(folder) if folder.endswith("src") else folder)
                    out.append((f, (base or "crate").replace("-", "_"), posixpath.dirname(folder), "lib"))
        return out

    def discover_modules(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        texts: dict[str, tuple[tuple[Any, ...], str]] = {}
        for f in sorted(ctx.files("rust")):
            text = ctx.text(f)
            if text is None:
                continue
            digest = ctx.source.content_hash(f) or stable_hash(text)
            texts[f] = (("rust", self.version, digest), text)
        misses = [(f, text) for f, (key, text) in texts.items() if key not in ctx.file_cache]
        for f, parsed in parse_parallel(_parse_item, misses).items():
            ctx.file_cache[texts[f][0]] = parsed
        infos: dict[str, dict[str, Any]] = {}
        for f, (key, text) in texts.items():
            infos[f] = ctx.cached(key, lambda t=text: parse_rust(t))
        files = set(infos)
        crates = self._crates(ctx, files)
        modules: dict[tuple[str, tuple[str, ...]], _Module] = {}
        module_of: dict[str, tuple[str, tuple[str, ...]]] = {}  # file -> its module
        crate_info: dict[str, dict[str, Any]] = {}
        for root, cname, pkg, kind in crates:
            if root in module_of:
                continue
            crate_info[root] = {"name": cname, "package": pkg, "kind": kind}
            queue = [(root, ())]
            while queue:
                f, path = queue.pop()
                if f in module_of or f not in infos:
                    continue
                key = (root, path)
                modules[key] = _Module(root, path, f)
                module_of[f] = key
                info = infos[f]
                for rec in info["inline"]:  # mod x { … }: a module of this file
                    ikey = (root, path + tuple(rec["path"]))
                    modules.setdefault(ikey, _Module(root, ikey[1], f))
                    parent = modules.get((root, path + tuple(rec["path"][:-1])))
                    if parent is not None:
                        parent.children[rec["name"]] = ikey
                base = posixpath.dirname(f)
                stem = posixpath.splitext(posixpath.basename(f))[0]
                mod_dir = base if (f == root or stem == "mod") else posixpath.join(base, stem)
                for decl in info["mods"]:
                    where = posixpath.join(mod_dir, *decl["inline"])
                    if decl["path_attr"]:
                        cands = [posixpath.normpath(posixpath.join(base if not decl["inline"] else where,
                                                                   decl["path_attr"]))]
                    else:
                        cands = [posixpath.join(where, decl["name"] + ".rs"),
                                 posixpath.join(where, decl["name"], "mod.rs")]
                    child_path = path + tuple(decl["inline"]) + (decl["name"],)
                    target = next((c for c in cands if c in infos), None)
                    owner = modules.get((root, path + tuple(decl["inline"])))
                    if owner is not None:
                        owner.children[decl["name"]] = (root, child_path)
                    if target is not None:
                        queue.append((target, child_path))
                    elif not decl.get("cfg"):  # a compile error, unless only some configurations build it
                        b.diagnostic("warning", "unresolved-internal-import", f"`mod {decl['name']};` has no file: "
                                     f"{' or '.join(cands)} does not exist.", self.name, f, decl["line"])
        ctx.shared["rust.infos"] = infos
        ctx.shared["rust.tree"] = (modules, module_of, crate_info)
        for f, info in infos.items():
            key = module_of.get(f)
            if key is not None:
                cname = crate_info[key[0]]["name"]
                qual = "::".join((cname,) + key[1])
            else:  # not reached from any crate root: named by its folder
                stem = posixpath.splitext(posixpath.basename(f))[0]
                qual = "::".join([p for p in posixpath.dirname(f).split("/") if p and p != "src"] +
                                 ([] if stem == "mod" else [stem])).replace("-", "_")
            node = b.ensure_file(f, self.name)
            tags = ["rust"] + (["test"] if ctx.profile.is_test(f) or
                               (key is not None and crate_info[key[0]]["kind"] == "test") else [])
            entry = None
            if key is not None and not key[1] and crate_info[key[0]]["kind"] in ("bin", "example") and info["main"]:
                entry = "rust binary" if crate_info[key[0]]["kind"] == "bin" else "rust example"
            if entry and "test" not in tags:
                tags.append("entry-point")
            b.add_node(ComponentNode(
                id=node.id, name=posixpath.basename(f), qualified_name=qual, component_type="module",
                category=CATEGORY_MODULE, language="rust", path=f, analyzer=self.name, key=node.key, tags=tags,
                metadata={"loc": info["loc"], "semantic_fingerprint": info["semantic"],
                          **({"entry_kind": entry} if entry and "test" not in tags else {}),
                          **({"crate": crate_info[key[0]]["name"]} if key is not None else {})}))
            node.category = CATEGORY_MODULE
        b.stat(self.name, "modules", len(infos))
        b.stat(self.name, "unreached_files", sum(1 for f in infos if f not in module_of))

    def discover_symbols(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, dict[str, Any]] = ctx.shared.get("rust.infos", {})
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
                    id=b.symbol_id(f, s["qual"]), name=s["name"], qualified_name=f"{module.qualified_name}::{s['qual']}",
                    component_type=ctype, category=CATEGORY_SYMBOL, language="rust", path=f,
                    parent_id=b.symbol_id(f, s["parent"]) if s["parent"] else module.id, analyzer=self.name,
                    key=f"symbol:{f}:{s['qual']}", fingerprint=s["fingerprint"], start_line=s["line"],
                    end_line=s["end_line"], metadata=meta))
                b.stat(self.name, "symbols")
        if infos:
            b.diagnostic("info", "callflow-unsupported", "Call-flow extraction is not implemented for Rust; the "
                         "Activity tab falls back to module-level import impact.", self.name)

    # -- dependencies -------------------------------------------------------------------------------------------

    def discover_dependencies(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, dict[str, Any]] = ctx.shared.get("rust.infos", {})
        if not infos:
            return
        modules, module_of, crate_info = ctx.shared["rust.tree"]
        lib_roots = {info["name"]: root for root, info in crate_info.items() if info["kind"] == "lib"}
        lib_of_package = {info["package"]: root for root, info in crate_info.items() if info["kind"] == "lib"}
        declared: dict[str, dict[str, str]] = {}  # package folder -> crate name as used in code -> declared name
        for md in ctx.profile.manifest_data.values():
            if md.kind == "cargo":
                for dep in md.dependencies:
                    declared.setdefault(md.dir, {})[_norm(dep.name)] = dep.name
                    target = lib_of_package.get((dep.local_path or "").rstrip("/")) if dep.local_path else None
                    if target is not None:  # a path dependency on a workspace crate, maybe renamed in Cargo.toml
                        lib_roots.setdefault(_norm(dep.name), target)
        edges = 0

        def resolve(segs: list[str], root: str, cur: tuple[str, ...]) -> tuple[str | None, str | None]:
            """(file of the deepest module named, external crate) for a path used in module ``cur``."""
            if not segs:
                return None, None
            first = segs[0]
            node: tuple[str, tuple[str, ...]] | None
            i = 0
            if first == "crate":
                node, i = (root, ()), 1
            elif first in ("self", "super"):
                node = (root, cur)
                while i < len(segs) and segs[i] in ("self", "super"):
                    if segs[i] == "super":
                        node = (root, node[1][:-1])
                    i += 1
            elif first in (modules.get((root, cur)) or _Module("", (), "")).children:
                node = (root, cur)
            elif first in (modules.get((root, ())) or _Module("", (), "")).children and cur:
                node = (root, ())  # 2015-edition paths start at the crate root
            elif first in lib_roots and lib_roots[first] != root:  # a workspace crate, or a binary's own library
                node, i = (lib_roots[first], ()), 1
            else:
                return None, first
            while i < len(segs):
                mod = modules.get(node)
                if mod is None or segs[i] not in mod.children:
                    break
                node = mod.children[segs[i]]
                i += 1
            mod = modules.get(node)
            return (mod.file if mod is not None else None), None

        for f, info in infos.items():
            key = module_of.get(f)
            src = b.file_id(f)
            test = ctx.profile.is_test(f)
            pkg = crate_info[key[0]]["package"] if key is not None else None
            done: dict[str, bool] = {}

            def link(target: str, line: int, construct: str, name: str, confidence: float) -> None:
                nonlocal edges
                if target == f:
                    return
                meta: dict[str, Any] = {"imported_names": [name]}
                if test:
                    meta["test_only"] = True
                b.add_edge(src, b.file_id(target), REL_IMPORTS, analyzer=self.name,
                           evidence=[self.evidence(ctx, f, line, line, construct)], confidence=confidence, metadata=meta)
                done[target] = True
                edges += 1

            def external(crate: str, line: int, name: str, construct: str = "use") -> None:
                std = crate in STD_CRATES
                decl = None if std else (declared.get(pkg or "", {}).get(_norm(crate)) or
                                         next((v[_norm(crate)] for v in declared.values() if _norm(crate) in v), None))
                eco = "rust-std" if std else "cargo"
                ext_name = crate if std else (decl or crate)
                key_name = ext_name.lower() if eco == "cargo" else ext_name
                ext_id = b.external_id(eco, key_name)
                if ext_id not in b.nodes:
                    b.add_node(ComponentNode(id=ext_id, name=ext_name, qualified_name=ext_name,
                                             component_type="external-package", analyzer=self.name,
                                             key=f"external:{eco}:{key_name}",
                                             tags=["external"] + (["stdlib"] if std else []),
                                             metadata={"ecosystem": "cargo"}))
                meta: dict[str, Any] = {"external": True, "imported_names": [name]}
                if test:
                    meta["test_only"] = True
                b.add_edge(src, ext_id, REL_IMPORTS, analyzer=self.name,
                           evidence=[self.evidence(ctx, f, line, line, construct)], confidence=0.9, metadata=meta)

            root, cur = key if key is not None else (None, ())
            deps = {_norm(n) for n in declared.get(pkg or "", {})} or {_norm(n) for v in declared.values() for n in v}
            # names brought into scope by a use (``use a::b; use b::c;``, ``use Shape::*``) are not crates
            local_names = {(alias or segs[-1]) for segs, _g, alias, _l, _i in info["uses"] if segs}
            externs = {name for name, _l, _i in info["externs"]}

            def is_crate(name: str, strict: bool) -> bool:
                if name in STD_CRATES or _norm(name) in deps or name in externs:
                    return True
                return not strict and name[:1].islower() and name not in local_names

            seen_ext: set[str] = set()
            for segs, glob, _alias, line, inline in info["uses"]:
                name = "::".join(segs) + ("::*" if glob else "")
                if root is None:
                    target, crate = None, (None if segs[0] in ("crate", "self", "super") else segs[0])
                else:
                    target, crate = resolve(segs, root, tuple(cur) + tuple(inline))
                if target is not None:
                    link(target, line, "use", name, 1.0)
                elif crate is not None and is_crate(crate, strict=False):
                    external(crate, line, name)
                    seen_ext.add(crate)
            for crate, line, _inline in info["externs"]:
                external(crate, line, f"extern crate {crate}", "extern-crate")
                seen_ext.add(crate)
            for segs, line, inline in info["paths"]:  # crate::db::open(), super::model::Order, serde_json::to_string
                if segs[0] in local_names and segs[0] not in ("crate", "self", "super"):
                    continue  # an imported name (a type, an enum), not a module
                if root is None:
                    target, crate = None, segs[0]
                else:
                    target, crate = resolve(segs[:-1] if len(segs) > 1 else segs, root, tuple(cur) + tuple(inline))
                if target is not None:
                    if target not in done:
                        link(target, line, "path", "::".join(segs), 0.9)
                elif crate is not None and crate not in seen_ext and is_crate(crate, strict=True):
                    external(crate, line, "::".join(segs), "path")
                    seen_ext.add(crate)
        b.stat(self.name, "import_edges", edges)
