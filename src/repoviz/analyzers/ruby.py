"""Ruby analyzer (lightweight, #26).

Regular expressions over the text with comments, strings, heredocs and ``=begin`` blocks blanked (``textscan``);
nothing is run.  Ruby blocks close with ``end``, so a small scanner pairs each ``end`` with its opener (``class``,
``module``, ``def``, ``do``, ``begin``, ``case`` and statement ``if`` / ``unless`` / ``while`` / ``until`` / ``for``;
the modifier forms ``return if x`` open nothing).

* **Modules.** One per file, named after the class or module it defines (``Admin::UsersController``), else the
  file.  Classes and modules nest (``module Admin; class User``) or name their parent (``class Admin::User``).
* **Constants.** Most Ruby code, and every Rails application (Zeitwerk), uses constants rather than ``require``:
  ``User.find``, ``Admin::Audit.log``.  A constant is resolved as Ruby does, through the lexical nesting
  (``Admin::User``, then ``User``), against the classes, modules and constant assignments the repository defines.
  A namespace reopened in many files (``module MyGem``) links to its conventional file (``my_gem.rb``) only.
* **Requires.** ``require_relative``, and ``require`` / ``load`` / ``autoload`` of a path found under ``lib/``,
  ``app/``, the file's gem ``lib/``, ``test/`` or ``spec/``.  Anything else is a gem (matched to ``Gemfile`` /
  gemspec dependencies, ``active_support/…`` → ``activesupport``) or the standard library (``json``,
  ``net/http``…).
* **Broken requires.** A ``require_relative`` of a missing file, or a ``require`` of a missing file of the
  repository's own library (``shop_kit/money`` with ``lib/shop_kit.rb`` here), is ``unresolved-internal-import``.
* **Symbols.** Classes, modules, methods (``User#save``, ``User.find`` for ``def self.find``) with their parameters.
  A file with ``if __FILE__ == $0`` is an entry point.  No call graph.
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
MAX_REFS = 5000
MAX_SIGNATURE = 200

#: Ruby: =begin … =end, # comments (not #{ inside strings: strings are matched first when they start first),
#: heredocs (<<~EOS … EOS), %-literals with brackets, and quoted strings.
RUBY_TOKENS = re.compile(
    r"^=begin\b.*?^=end\b[^\n]*|#[^\n]*|<<[~-]?(['\"`]?)([A-Z_][A-Z0-9_]*)\1[^\n]*\n.*?^[ \t]*\2\b|"
    r"%[qQwWiIrsx]?\((?:[^()\\]|\\.|\([^()]*\))*\)|%[qQwWiIrsx]?\[(?:[^\[\]\\]|\\.)*\]|"
    r"%[qQwWiIrsx]?\{(?:[^{}\\]|\\.|\{[^{}]*\})*\}|%[qQwWiIrsx]?<(?:[^<>\\]|\\.)*>|"
    r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`", re.S | re.M)
_OPENERS = re.compile(r"(?<![\w.:@$?!])(class|module|def|do|begin|case|if|unless|while|until|for|end)"
                      r"(?![\w?!]|:(?!:))")  # not a hash key (end: 1) nor a method name (end?)
_CONST_REF = re.compile(r"(?<![\w:.@$])(::)?([A-Z]\w*(?:::[A-Z]\w*)*)")
_REQUIRE = re.compile(r"(?<![\w.])(require_relative|require|load|autoload)\s*\(?\s*(?::\w+\s*,\s*)?['\"]([^'\"]+)['\"]")
_CONST_ASSIGN = re.compile(r"^[ \t]*([A-Z]\w*)\s*=[^=~]", re.M)
#: The standard library (default gems and extensions shipped with Ruby).
STDLIB = {"json", "set", "net", "uri", "time", "date", "yaml", "psych", "fileutils", "pathname", "securerandom",
          "digest", "openssl", "logger", "erb", "csv", "optparse", "ostruct", "open3", "tempfile", "tmpdir",
          "stringio", "socket", "english", "benchmark", "forwardable", "singleton", "observer", "timeout", "base64",
          "zlib", "open-uri", "shellwords", "pp", "prettyprint", "delegate", "weakref", "monitor", "thread", "etc",
          "io", "bigdecimal", "rbconfig", "resolv", "ipaddr", "abbrev", "find", "getoptlong", "coverage",
          "objspace", "ripper", "racc", "strscan", "cgi", "webrick", "rdoc", "irb", "readline", "reline", "fiber",
          "continuation", "mkmf", "bundler", "rubygems", "matrix", "prime", "tsort", "un", "drb", "expect", "pty",
          "syslog", "win32ole", "fiddle", "securerandom", "random", "digest/md5", "digest/sha1"}


# --------------------------------------------------------------------------- parsing (pure, cached per content)


@dataclass
class _Open:
    kind: str  # class | module | def | block
    decl: dict[str, Any] | None = None
    name: str = ""  # the constant path this class / module adds to the nesting


@dataclass
class _Scan:
    code: str
    noc: str  # comments blanked, strings kept (default values in signatures)
    lines: LineIndex
    decls: list[dict[str, Any]] = field(default_factory=list)
    defined: list[tuple[str, int]] = field(default_factory=list)  # (full constant name, line)


def _statement_start(code: str, pos: int) -> bool:
    """Whether the keyword at ``pos`` starts a statement (``if x`` opens a block, ``return if x`` does not)."""
    line_start = code.rfind("\n", 0, pos) + 1
    before = code[line_start:pos].rstrip()
    if not before:
        return True
    semi = before.rfind(";")
    if semi >= 0 and not before[semi + 1:].strip():
        return True
    return before.endswith(("=", "(", ",", "||", "&&", "[", "{", "?", ":", "<<", "then", "do", "begin", "else",
                            " and", " or", " not", "!"))


def _declarations(scan: _Scan) -> list[tuple[int, tuple[str, ...]]]:
    """Blocks and definitions; returns the nesting (enclosing constant names) at every position where it changes."""
    code = scan.code
    stack: list[_Open] = []
    changes: list[tuple[int, tuple[str, ...]]] = [(0, ())]

    def nesting() -> tuple[str, ...]:
        out: list[str] = []
        for o in stack:
            if o.kind in ("class", "module") and o.name:
                out = [o.name[2:]] if o.name.startswith("::") else out + [o.name]
        return tuple(out)

    for m in _OPENERS.finditer(code):
        word, pos = m.group(1), m.start()
        if word == "end":
            if stack:
                closed = stack.pop()
                if closed.decl is not None:
                    closed.decl["end"] = m.end() - 1
                if closed.kind in ("class", "module"):
                    changes.append((m.end(), nesting()))
            continue
        if word in ("if", "unless", "while", "until", "for") and not _statement_start(code, pos):
            continue  # a modifier: `x if y`
        if word == "do" and re.match(r"[ \t]*$", code[code.rfind("\n", 0, pos) + 1:pos]) is None and \
                re.search(r"\b(?:while|until|for)\b[^\n]*$", code[code.rfind("\n", 0, pos) + 1:pos]):
            continue  # `while x do`: the do belongs to the loop
        rest = code[m.end():code.find("\n", m.end()) if code.find("\n", m.end()) >= 0 else len(code)]
        if word in ("class", "module"):
            cm = re.match(r"\s*(<<\s*self|(?:::)?[A-Z]\w*(?:::[A-Z]\w*)*)(\s*<\s*([^;\n]+))?", rest)
            if cm is None:
                continue  # `klass.class`, `class_eval`: not a definition
            if cm.group(1).replace(" ", "").startswith("<<"):
                stack.append(_Open("block"))  # class << self: singleton methods, same nesting
                stack[-1].kind = "singleton"
                continue
            name = cm.group(1)
            parent = nesting()
            full = name[2:] if name.startswith("::") else "::".join(parent + (name,))
            line = scan.lines.line(pos)
            decl = _add(scan, full, name.split("::")[-1], word, pos, None, None, True)
            scan.defined.append((full, line))
            stack.append(_Open(word, decl, name))
            changes.append((m.end(), nesting()))
            if re.match(r"\s*(?:<<\s*self|(?:::)?[A-Z][\w:]*)(?:\s*<\s*[^;\n]+)?\s*;\s*end\b", rest):
                pass  # `class Foo < Bar; end`: the scanner meets the `end` next
            continue
        if word == "def":
            dm = re.match(r"\s*(self\.)?([A-Za-z_]\w*[?!=]?|\[\]=?|[+\-*/%<>=!~^&|]+)\s*(\(([^)]*)\)|([^\n;=]*))?", rest)
            if dm is None:
                continue
            after = rest[dm.end():]
            singleton = bool(dm.group(1)) or any(o.kind == "singleton" for o in stack)
            group = 4 if dm.group(4) is not None else 5  # read from the text with strings kept
            params = squash(scan.noc[m.end() + dm.start(group):m.end() + dm.end(group)] if dm.group(group) else "")
            owner = "::".join(nesting())
            qual = f"{owner}{'.' if singleton else '#'}{dm.group(2)}" if owner else dm.group(2)
            endless = bool(re.match(r"\s*=(?!=)", after))  # def square(x) = x * x
            public = not re.search(r"\b(?:private|protected)\s+$", code[code.rfind("\n", 0, pos) + 1:pos])
            decl = _add(scan, qual, dm.group(2), "method" if owner else "function", pos,
                        (code.find("\n", m.end()) if code.find("\n", m.end()) >= 0 else len(code)) - 1 if endless
                        else None, f"({params})", public)
            if not endless:
                stack.append(_Open("def", decl))
            continue
        stack.append(_Open("block"))
    for d in scan.decls:
        if d["end"] is None:
            d["end"] = len(code) - 1
    return changes


def _add(scan: _Scan, qual: str, name: str, kind: str, start: int, end: int | None, signature: str | None,
         public: bool) -> dict[str, Any]:
    d = {"qual": qual, "name": name, "kind": kind, "start": start, "end": end, "signature": signature,
         "public": public}
    if len(scan.decls) < MAX_SYMBOLS:
        scan.decls.append(d)
    return d


def parse_ruby(text: str) -> dict[str, Any]:
    """Everything the analyzer needs from one Ruby file, as plain JSON."""
    code, noc = mask_pair(text, RUBY_TOKENS, ("#", "=begin"))
    lines = LineIndex(text)
    scan = _Scan(code, noc, lines)
    changes = _declarations(scan)
    positions = [c[0] for c in changes]

    import bisect

    def nesting_at(pos: int) -> tuple[str, ...]:
        return changes[bisect.bisect_right(positions, pos) - 1][1]

    private_ranges: list[tuple[int, int]] = []  # `private` on its own line: the methods after it
    for m in re.finditer(r"(?m)^[ \t]*(private|protected|public)[ \t]*$", code):
        private_ranges.append((m.start(), 1 if m.group(1) != "public" else 0))
    for d in scan.decls:
        if d["kind"] in ("method", "function"):
            owner_start = max((o["start"] for o in scan.decls if o["kind"] in ("class", "module")
                               and o["start"] < d["start"] <= o["end"]), default=-1)
            before = [flag for start, flag in private_ranges if owner_start < start < d["start"]]
            if before and before[-1]:
                d["public"] = False
    refs: dict[str, list[Any]] = {}
    for m in _CONST_REF.finditer(code):
        line = lines.line(m.start())
        before = code[code.rfind("\n", 0, m.start()) + 1:m.start()]
        if re.search(r"(?<![\w:])(?:class|module)\s+$", before):
            continue  # the name being defined
        name = m.group(2)
        if len(refs) >= MAX_REFS:
            break
        nest = nesting_at(m.start())
        key = f"{'::' if m.group(1) else ''}{name}|{'::'.join(nest)}"
        if key not in refs:
            refs[key] = [name, bool(m.group(1)), list(nest), line]
    for m in _CONST_ASSIGN.finditer(code):
        nest = nesting_at(m.start())
        scan.defined.append(("::".join(nest + (m.group(1),)), lines.line(m.start())))
    requires = [[m.group(1), m.group(2), lines.line(m.start())] for m in _REQUIRE.finditer(noc)
                if code[m.start()] not in " "]  # not inside a comment or string (blanked in code)
    seen: Counter[str] = Counter()
    symbols = []
    for d in scan.decls:
        seen[d["qual"]] += 1
        if seen[d["qual"]] > 1:
            if d["kind"] in ("class", "module"):
                continue  # a class reopened in the same file: one symbol
            d["qual"] = f"{d['qual']}~{seen[d['qual']]}"
        a, b = d["start"], d["end"] + 1
        sig = d["signature"]
        symbols.append({"qual": d["qual"], "name": d["name"], "kind": d["kind"], "line": lines.line(a),
                        "end_line": lines.line(max(a, b - 1)), "fingerprint": stable_hash(text[a:b]),
                        "semantic": stable_hash(squash(noc[a:b])),
                        "signature": sig if sig is None or len(sig) <= MAX_SIGNATURE else sig[:MAX_SIGNATURE - 3] + "...",
                        "signature_id": stable_hash("sig", sig, length=12) if sig else None, "public": d["public"]})
    main = bool(re.search(r"if\s+(?:__FILE__\s*==\s*\$(?:0|PROGRAM_NAME)|\$(?:0|PROGRAM_NAME)\s*==\s*__FILE__)", noc))
    return {"defined": sorted(set(scan.defined)), "refs": list(refs.values()), "requires": requires,
            "symbols": symbols, "main": main, "semantic": stable_hash(squash(noc)),
            "loc": text.count("\n") + (0 if text.endswith("\n") or not text else 1)}


def _parse_item(item: tuple[str, str]) -> tuple[str, dict[str, Any]]:
    return item[0], parse_ruby(item[1])


# --------------------------------------------------------------------------- the analyzer


def _underscore(name: str) -> str:
    """``Admin::UsersController`` → ``admin/users_controller`` (the Zeitwerk file name)."""
    parts = []
    for seg in name.split("::"):
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", seg)
        parts.append(re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s).lower())
    return "/".join(parts)


def _gem_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class RubyAnalyzer(Analyzer):
    name = "ruby"
    version = "1"  # bump when the parse result or the graph changes (part of the cache keys)
    languages = ("ruby",)
    capabilities = (CAP_MODULES, CAP_SYMBOLS, CAP_DEPENDENCIES, CAP_ENTRY_POINTS)

    def detect(self, ctx: AnalysisContext) -> Detection:
        n = len(ctx.files("ruby"))
        return Detection(bool(n), f"{n} Ruby file(s)" if n else "no Ruby files")

    def discover_modules(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        texts: dict[str, tuple[tuple[Any, ...], str]] = {}
        manifests = ctx.profile.manifest_data
        for f in sorted(ctx.files("ruby")):
            if f in manifests or posixpath.basename(f) in ("Gemfile", "Rakefile", "Vagrantfile", "Guardfile"):
                continue  # configuration, not code of the application
            text = ctx.text(f)
            if text is None:
                continue
            digest = ctx.source.content_hash(f) or stable_hash(text)
            texts[f] = (("ruby", self.version, digest), text)
        misses = [(f, text) for f, (key, text) in texts.items() if key not in ctx.file_cache]
        for f, parsed in parse_parallel(_parse_item, misses).items():
            ctx.file_cache[texts[f][0]] = parsed
        infos: dict[str, dict[str, Any]] = {}
        for f, (key, text) in texts.items():
            infos[f] = ctx.cached(key, lambda t=text: parse_ruby(t))
        ctx.shared["ruby.infos"] = infos
        for f, info in infos.items():
            stem = posixpath.splitext(posixpath.basename(f))[0]
            # named after the constant whose conventional file this is, else the first one it defines
            names = [n for n, _l in info["defined"]]
            conventional = next((n for n in names if _underscore(n).endswith(stem) and
                                 f[:-3].endswith(_underscore(n))), None)
            qual = conventional or next((n for n in names if _underscore(n.split("::")[-1]) == stem), None) or \
                (names[0] if names and len(names) == 1 else stem)
            node = b.ensure_file(f, self.name)
            tags = ["ruby"] + (["test"] if ctx.profile.is_test(f) else [])
            if info["main"] and "test" not in tags:
                tags.append("entry-point")
            b.add_node(ComponentNode(
                id=node.id, name=posixpath.basename(f), qualified_name=qual, component_type="module",
                category=CATEGORY_MODULE, language="ruby", path=f, analyzer=self.name, key=node.key, tags=tags,
                metadata={"loc": info["loc"], "semantic_fingerprint": info["semantic"],
                          **({"entry_kind": "ruby script"} if info["main"] and "test" not in tags else {})}))
            node.category = CATEGORY_MODULE
        b.stat(self.name, "modules", len(infos))

    def discover_symbols(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, dict[str, Any]] = ctx.shared.get("ruby.infos", {})
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
                    id=b.symbol_id(f, s["qual"]), name=s["name"], qualified_name=s["qual"], component_type=ctype,
                    category=CATEGORY_SYMBOL, language="ruby", path=f, parent_id=module.id, analyzer=self.name,
                    key=f"symbol:{f}:{s['qual']}", fingerprint=s["fingerprint"], start_line=s["line"],
                    end_line=s["end_line"], metadata=meta))
                b.stat(self.name, "symbols")
        if infos:
            b.diagnostic("info", "callflow-unsupported", "Call-flow extraction is not implemented for Ruby; the "
                         "Activity tab falls back to module-level import impact.", self.name)

    # -- dependencies -------------------------------------------------------------------------------------------

    def discover_dependencies(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, dict[str, Any]] = ctx.shared.get("ruby.infos", {})
        if not infos:
            return
        where: dict[str, list[str]] = {}  # constant -> files defining (or reopening) it
        for f, info in infos.items():
            for name, _line in info["defined"]:
                files = where.setdefault(name, [])
                if f not in files:
                    files.append(f)
        files = set(infos)
        gems: dict[str, str] = {}  # normalized gem name -> declared name
        gem_roots: list[str] = []  # folders of the repository's own gems (their lib/ is a load path)
        for md in ctx.profile.manifest_data.values():
            if md.ecosystem == "ruby":
                for dep in md.dependencies:
                    gems[_gem_key(dep.name)] = dep.name
                if md.kind in ("gemspec", "gemfile"):
                    gem_roots.append(md.dir)
        load_paths = sorted({posixpath.join(r, sub) if r else sub for r in gem_roots + [""]
                             for sub in ("lib", "app", "test", "spec")} | {""}, key=len, reverse=True)
        present = ctx.source.file_set()
        lib_paths = [lp for lp in load_paths if posixpath.basename(lp) == "lib"]
        edges = 0

        own_gems = {_gem_key(md.name) for md in ctx.profile.manifest_data.values()
                    if md.ecosystem == "ruby" and md.kind == "gemspec" and md.name}

        def own_lib(first: str) -> bool:
            """Whether ``first`` is a library of this repository: ``lib/shop_kit.rb`` is here, and no other gem
            shares the name (Sinatra's ``lib/rack/protection`` lives in the rack gem's ``rack/`` folder)."""
            key = _gem_key(first)
            return (key not in gems or key in own_gems) and \
                any(posixpath.join(lp, first + ".rb") in present for lp in lib_paths)

        def owner_file(const: str, f: str) -> str | None:
            """The file of a constant: the one file defining it, else its conventional file (``my_gem.rb`` for
            MyGem), else the nearest of a few; a namespace reopened in many files has none."""
            files_ = where.get(const)
            if not files_:
                return None
            if len(files_) == 1:
                return files_[0]
            conv = _underscore(const)
            hit = [x for x in files_ if x[:-3].endswith(conv)]
            if hit:
                return min(hit, key=len)
            if len(files_) <= 3:
                return max(files_, key=lambda x: (len(posixpath.commonprefix([x, f])), -len(x)))
            return None

        for f, info in infos.items():
            src = b.file_id(f)
            test = ctx.profile.is_test(f)
            done: set[str] = set()

            def link(target: str, line: int, construct: str, name: str, confidence: float) -> None:
                nonlocal edges
                if target == f or target in done:
                    return
                meta: dict[str, Any] = {"imported_names": [name]}
                if test:
                    meta["test_only"] = True
                b.add_edge(src, b.file_id(target), REL_IMPORTS, analyzer=self.name,
                           evidence=[self.evidence(ctx, f, line, line, construct)], confidence=confidence,
                           metadata=meta)
                done.add(target)
                edges += 1

            for kind, path, line in info["requires"]:
                rel = path if path.endswith(".rb") else path + ".rb"
                if kind == "require_relative":
                    cands = [posixpath.normpath(posixpath.join(posixpath.dirname(f), rel))]
                else:
                    cands = [posixpath.normpath(posixpath.join(lp, rel)) if lp else rel for lp in load_paths]
                target = next((c for c in cands if c in files), None)
                if target is not None:
                    link(target, line, kind.replace("_", "-"), path, 1.0)
                elif any(c in present for c in cands) or path in present:
                    continue  # there, but not analyzed (too large, excluded, or not Ruby)
                elif kind == "require_relative":
                    if not cands[0].startswith("../"):
                        b.diagnostic("warning", "unresolved-internal-import", f"`require_relative \"{path}\"`: "
                                     f"{cands[0]} does not exist.", self.name, f, line)
                elif "/" in path and own_lib(path.split("/")[0]):
                    b.diagnostic("warning", "unresolved-internal-import", f"`{kind} \"{path}\"`: the repository's "
                                 f"own library has no {rel}.", self.name, f, line)
                else:
                    self._external(ctx, b, src, f, path, line, gems, test)
            for name, absolute, nest, line in info["refs"]:  # constants, looked up as Ruby does
                first, *rest = name.split("::")
                scopes = [[]] if absolute else [nest[:i] for i in range(len(nest), -1, -1)]
                full = next(("::".join(sc + [first] + rest) for sc in scopes if "::".join(sc + [first]) in where),
                            None)  # the innermost scope that defines the first segment, then the rest inside it
                if full is None:
                    continue  # not defined in the repository: a gem's or Ruby's own
                parts = full.split("::")
                k = next(k for k in range(len(parts), 0, -1) if "::".join(parts[:k]) in where)
                target = owner_file("::".join(parts[:k]), f)  # Admin::User::ROLES → the file of Admin::User
                if target is not None:
                    link(target, line, "constant", "::".join(parts[:k]), 0.9)
        b.stat(self.name, "import_edges", edges)

    def _external(self, ctx: AnalysisContext, b: SnapshotBuilder, src: str, f: str, path: str, line: int,
                  gems: dict[str, str], test: bool) -> None:
        first = path.split("/")[0]
        std = path.lower() in STDLIB or first.lower() in STDLIB
        gem = None
        if not std:
            key = _gem_key(first)
            gem = gems.get(key) or next((d for k, d in gems.items() if k == _gem_key(path.replace("/", ""))), None)
        eco = "ruby-std" if std else "ruby"
        ext_name = first if std else (gem or first)
        key_name = ext_name.lower()
        ext_id = b.external_id(eco, key_name)
        if ext_id not in b.nodes:
            b.add_node(ComponentNode(id=ext_id, name=ext_name, qualified_name=ext_name,
                                     component_type="external-package", analyzer=self.name,
                                     key=f"external:{eco}:{key_name}", tags=["external"] + (["stdlib"] if std else []),
                                     metadata={"ecosystem": "ruby"}))
        meta: dict[str, Any] = {"external": True, "imported_names": [path]}
        if test:
            meta["test_only"] = True
        b.add_edge(src, ext_id, REL_IMPORTS, analyzer=self.name,
                   evidence=[self.evidence(ctx, f, line, line, "require")], confidence=0.9, metadata=meta)
