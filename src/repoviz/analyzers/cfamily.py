"""C and C++ analyzer (lightweight, #26).

Regular expressions over the text with comments and literals blanked (``textscan``); nothing is compiled or
preprocessed for real, and build files are only read.

* **Modules.** One per file, named by its path (``src/net/socket.c``): C has no packages.
* **Preprocessor.** Only the first branch of an ``#if`` / ``#ifdef`` is scanned for declarations (``#if 0`` blocks
  are skipped, their ``#else`` kept), so braces stay balanced.  ``#include`` lines are read in every branch but
  ``#if 0``; one inside a real conditional (not the include guard, not ``__cplusplus``) is *conditional*.
* **Includes.** ``#include "x.h"`` resolves next to the file, then under the include directories: those that CMake
  (``include_directories``, ``target_include_directories``), Makefiles (``-I``), Meson (``include_directories``)
  and Bazel (``includes =``) name, then every ``include/`` folder, then a unique file with that path suffix.
  ``#include <x.h>`` resolves under the include directories, or by path suffix when it names a folder
  (``<shop/order.hpp>``), never next to the file.  Anything else is the C or C++ standard library,
  a system header (POSIX, Windows, intrinsics), or an external library matched to CMake ``find_package``.
* **Broken includes.** A quoted, unconditional include of a missing file whose folder exists, and that no build
  file names (``configure_file``, generated headers), is ``unresolved-internal-import``.
* **Symbols.** Functions (with ``(params) -> return``), classes, structs, unions and enums, C++ namespaces as
  qualifiers (``shop::Order::total``), methods declared in class bodies (with their access) and defined out of line.
  Prototypes are symbols in headers only.  ``static`` and anonymous-namespace functions are not public.  ``main``
  is an entry point.  No call graph.
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
from .textscan import LineIndex, mask_pair, match_close, squash

MAX_SYMBOLS = 2000
MAX_INCLUDES = 500
MAX_SIGNATURE = 200
MAX_BUILD_FILES = 500

HEADER_EXTS = (".h", ".hh", ".hpp", ".hxx", ".h++", ".inl", ".ipp", ".tpp")

#: C and C++: comments (a // comment continues after a backslash), raw strings, strings and characters.
C_TOKENS = re.compile(r'//(?:[^\n\\]|\\.)*|/\*.*?(?:\*/|\Z)|(?:(?<!\w)(?:u8|[uUL]))?R"([^()\\\s]{0,16})\(.*?\)\1"|'
                      r'(?:(?<!\w)(?:u8|[uUL]))?"(?:\\.|[^"\\\n])*"?|'
                      r"(?:(?<!\w)(?:u8|[uUL]))?'(?:\\.|[^'\\\n])*'?", re.S)
_DIRECTIVE = re.compile(r"^[ \t]*#[ \t]*(\w+)(.*)$")
_INCLUDE = re.compile(r"^[ \t]*#[ \t]*(?:include|import|include_next)\s*([<\"])([^>\"\n]+)[>\"]", re.M)
_TYPE = re.compile(r"(?:[A-Z_][A-Z0-9_]*\b(?:\s*\((?:[^()]|\([^()]*\))*\))?\s*)*"  # export macros first
                   r"(class|struct|union|enum(?:\s+(?:class|struct))?)\b\s*((?:[A-Z_][A-Z0-9_]*(?:\([^()]*\))?\s+)*)"
                   r"([A-Za-z_]\w*(?:\s*::\s*[A-Za-z_]\w*)*)?\s*(?:<[^{;]*>)?\s*(final\b\s*)?(?::[^{;]*)?$", re.S)
_NOT_NAMES = {"if", "for", "while", "switch", "return", "sizeof", "case", "do", "else", "new", "delete", "throw",
              "catch", "decltype", "alignas", "alignof", "noexcept", "static_assert", "typeof", "__typeof__",
              "__attribute__", "__declspec", "_Alignas", "__asm__", "asm", "defined", "requires", "_Static_assert"}
_NAME_BEFORE = re.compile(r"((?:[A-Za-z_]\w*\s*(?:<[^()]*?>)?\s*::\s*)*~?\s*[A-Za-z_]\w*)\s*$")
_TYPE_WORDS = {"int", "char", "void", "long", "short", "unsigned", "signed", "float", "double", "bool", "auto",
               "const", "volatile", "struct", "union", "enum", "class", "typename", "_Bool", "wchar_t", "size_t"}
_SPECIFIERS = re.compile(r"\b(?:static|inline|extern|virtual|explicit|constexpr|consteval|constinit|friend|__inline|"
                         r"__inline__|__forceinline|_Noreturn|register|mutable|thread_local|__cdecl|__stdcall|"
                         r"WINAPI)\b")
#: What may follow a parameter list: qualifiers, attributes, macros (one token at a time: no backtracking).
_TAIL_TOKEN = re.compile(r"(?:(?:const|volatile|override|final|mutable|try)\b|&&|&|"
                         r"noexcept\b(?:\s*\((?:[^()]|\([^()]*\))*\))?|throw\s*\([^()]*\)|"
                         r"__attribute__\s*\(\((?:[^()]|\([^()]*\))*\)\)|\[\[[^\]]*\]\]|"
                         r"[A-Z_][A-Z0-9_]*\b(?:\s*\([^()]*\))?)\s*")
_GENERATED = re.compile(r"(?:\.pb|\.grpc\.pb|_generated|\.moc|config|version|export)\.h\w*$|^(?:ui|moc|qrc)_",
                        re.I)
_ATTRS = re.compile(r"\[\[.*?\]\]|__attribute__\s*\(\((?:[^()]|\([^()]*\))*\)\)|__declspec\s*\([^()]*\)|"
                    r"alignas\s*\([^()]*\)", re.S)

#: Headers of the C and C++ standard libraries and of the systems they run on.
C_STD = {"assert", "complex", "ctype", "errno", "fenv", "float", "inttypes", "iso646", "limits", "locale", "math",
         "setjmp", "signal", "stdalign", "stdarg", "stdatomic", "stdbit", "stdbool", "stdckdint", "stddef", "stdint",
         "stdio", "stdlib", "stdnoreturn", "string", "tgmath", "threads", "time", "uchar", "wchar", "wctype"}
CPP_STD = {"algorithm", "any", "array", "atomic", "barrier", "bit", "bitset", "cassert", "ccomplex", "cctype",
           "cerrno", "cfenv", "cfloat", "charconv", "chrono", "cinttypes", "ciso646", "climits", "clocale", "cmath",
           "codecvt", "compare", "complex", "concepts", "condition_variable", "coroutine", "csetjmp", "csignal",
           "cstdalign", "cstdarg", "cstdbool", "cstddef", "cstdint", "cstdio", "cstdlib", "cstring", "ctgmath",
           "ctime", "cuchar", "cwchar", "cwctype", "deque", "exception", "execution", "expected", "filesystem",
           "flat_map", "flat_set", "format", "forward_list", "fstream", "functional", "future", "generator",
           "initializer_list", "iomanip", "ios", "iosfwd", "iostream", "istream", "iterator", "latch", "limits",
           "list", "locale", "map", "mdspan", "memory", "memory_resource", "mutex", "new", "numbers", "numeric",
           "optional", "ostream", "print", "queue", "random", "ranges", "ratio", "regex", "scoped_allocator",
           "semaphore", "set", "shared_mutex", "source_location", "span", "spanstream", "sstream", "stack",
           "stacktrace", "stdexcept", "stdfloat", "stop_token", "streambuf", "string", "string_view", "strstream",
           "syncstream", "system_error", "thread", "tuple", "type_traits", "typeindex", "typeinfo",
           "unordered_map", "unordered_set", "utility", "valarray", "variant", "vector", "version", "meta", "hazard_pointer",
           "rcu", "debugging", "linalg", "simd", "text_encoding", "contracts", "inplace_vector"}
SYSTEM = {"unistd", "fcntl", "pthread", "dirent", "dlfcn", "poll", "sched", "semaphore", "strings", "syslog",
          "termios", "pwd", "grp", "netdb", "regex", "glob", "fnmatch", "libgen", "spawn", "utime", "utmp", "utmpx",
          "ifaddrs", "langinfo", "iconv", "nl_types", "execinfo", "err", "getopt", "paths", "sysexits", "endian",
          "byteswap", "features", "link", "elf", "mntent", "malloc", "alloca", "memory", "ucontext", "wordexp",
          "search", "ftw", "fts", "aio", "mqueue", "crypt", "shadow", "resolv", "xlocale", "stdio_ext", "values",
          "windows", "winsock", "winsock2", "ws2tcpip", "io", "process", "direct", "windef", "winbase", "tchar",
          "shlobj", "shellapi", "objbase", "wincrypt", "psapi", "tlhelp32", "dbghelp", "conio", "share", "crtdbg",
          "mswsock", "iphlpapi", "winternl", "ntstatus", "bcrypt", "intrin", "x86intrin", "immintrin", "emmintrin",
          "xmmintrin", "pmmintrin", "tmmintrin", "smmintrin", "nmmintrin", "wmmintrin", "avxintrin", "avx2intrin",
          "cpuid", "arm_neon", "arm_acle", "arm_sve", "TargetConditionals", "crt_externs", "AvailabilityMacros",
          "Availability", "dispatch", "libproc", "mach-o", "execinfo", "cxxabi", "unwind", "valgrind"}
SYSTEM_DIRS = {"sys", "netinet", "netinet6", "arpa", "net", "linux", "bits", "asm", "asm-generic", "mach", "machine",
               "libkern", "os", "malloc", "CoreFoundation", "CoreServices", "Security", "Foundation", "IOKit",
               "SystemConfiguration", "mach-o", "dispatch", "gnu", "ext", "tr1", "xlocale", "android", "uapi",
               "System", "Carbon", "ApplicationServices", "Cocoa", "sanitizer", "experimental"}


def _stem(path: str) -> str:
    base = posixpath.basename(path)
    return base.rsplit(".", 1)[0] if "." in base else base


def std_kind(path: str) -> str | None:
    """``c``, ``c++`` or ``system`` for a header of the standard libraries or the platform, else None."""
    first = path.split("/")[0]
    if "/" not in path:
        if path.endswith(".h") and _stem(path) in C_STD:
            return "c"
        if "." not in path and path in CPP_STD:
            return "c++"
        if _stem(path) in SYSTEM:
            return "system"
        return None
    return "system" if first in SYSTEM_DIRS else None


# --------------------------------------------------------------------------- parsing (pure, cached per content)


@dataclass
class _Open:
    kind: str  # namespace | type | extern | function | block
    decl: dict[str, Any] | None = None
    name: str = ""  # the qualifier this scope adds (namespace or type name)
    access: str = "public"
    hidden: bool = False  # an anonymous namespace (internal linkage) or a non-public type


@dataclass
class _Scan:
    code: str
    noc: str
    lines: LineIndex
    header_file: bool
    decls: list[dict[str, Any]] = field(default_factory=list)


def _preprocess(code: str, noc: str) -> tuple[str, list[list[Any]]]:
    """``(code, includes)``: directives and the branches not scanned blanked in ``code``; ``[kind, path, line,
    conditional]`` per ``#include`` (not those in ``#if 0``)."""
    lines = code.split("\n")
    nlines = noc.split("\n")
    out: list[str] = []
    includes: list[list[Any]] = []
    # one frame per open #if: [active, taken, zero, counts] (counts: it makes an include conditional)
    stack: list[list[Any]] = []
    seen = 0  # directives seen, #pragma aside
    guard: tuple[list[Any], str] | None = None  # the frame of #ifndef X, until #define X confirms the guard
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _DIRECTIVE.match(line)
        if m is None:
            out.append(line if all(f[0] for f in stack) else " " * len(line))
            i += 1
            continue
        start = i  # a directive, with its continuation lines
        while lines[i].rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
        for k in range(start, i + 1):
            out.append(" " * len(lines[k]))
        word = m.group(1)
        rest = " ".join(lines[k].rstrip().rstrip("\\") for k in range(start, i + 1))
        rest = rest[rest.index(word) + len(word):].strip()
        i += 1
        if word == "pragma":
            continue
        seen += 1
        if guard is not None and seen == 2:
            if not (word == "define" and rest.split()[:1] == [guard[1]]):
                guard[0][3] = True  # not an include guard after all
            guard = None
        parent = all(f[0] for f in stack)
        if word in ("if", "ifdef", "ifndef"):
            zero = word == "if" and rest in ("0", "false", "(0)")
            frame = [parent and not zero, not zero, zero, "__cplusplus" not in rest]
            if word == "ifndef" and seen == 1 and rest.split():
                frame[3] = False
                guard = (frame, rest.split()[0])
            stack.append(frame)
        elif word in ("elif", "else", "elifdef", "elifndef") and stack:
            f = stack[-1]
            outer = all(g[0] for g in stack[:-1])
            if f[1]:
                f[0] = False
            else:
                f[0], f[1] = outer, True
            f[2] = False
        elif word == "endif" and stack:
            stack.pop()
        elif word in ("include", "import", "include_next") and not any(f[2] for f in stack):
            im = _INCLUDE.match(nlines[start])
            if im and len(includes) < MAX_INCLUDES:
                includes.append(["system" if im.group(1) == "<" else "local", im.group(2).strip(), start + 1,
                                 any(f[3] for f in stack)])
    return "\n".join(out), includes


_SPACE = re.compile(r"\s*")
_TEMPLATE = re.compile(r"template\s*<")
_PREFIX = re.compile(r'(typedef\b|export\b|extern\s*"[^"\n]*"(?:\s*\+\+)?)')


def _tail_ok(tail: str) -> bool:
    """Whether ``tail`` (after the parameter list) fits a function: ``const override``, ``-> T``, ``= 0``, an
    initializer list (``: a_(1)``) or ``requires …``."""
    t = tail.strip()
    pos = 0
    while pos < len(t):
        m = _TAIL_TOKEN.match(t, pos)
        if m is None or m.end() == pos:
            break
        pos = m.end()
    rest = t[pos:]
    return not rest or rest.startswith((":", "requires")) or \
        (rest.startswith("->") and not re.search(r"[;{]", rest)) or \
        re.fullmatch(r"=\s*(?:0|default|delete)", rest) is not None


def _blank(s: str) -> str:
    return re.sub(r"[^\n]", " ", s)


def _strip(header: str) -> tuple[str, set[str]]:
    """The header with ``template<…>``, attributes, ``typedef``, ``export`` and ``extern "C"`` blanked (offsets
    kept); and the flags seen."""
    flags: set[str] = set()
    h = _ATTRS.sub(lambda m: _blank(m.group()), header)
    pos = 0
    while True:
        pos = _SPACE.match(h, pos).end()
        m = _TEMPLATE.match(h, pos)
        if m:
            depth, k = 0, m.end() - 1
            while k < len(h):
                if h[k] == "<":
                    depth += 1
                elif h[k] == ">":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            h = h[:pos] + _blank(h[pos:k + 1]) + h[k + 1:]
            flags.add("template")
            continue
        m = _PREFIX.match(h, pos)
        if m:
            flags.add(re.match(r"\w+", m.group(1)).group())
            h = h[:pos] + _blank(m.group()) + h[m.end():]
            continue
        return h, flags


def _params_types(params: str) -> str:
    """``(const std::string& name, int n = 1)`` → ``const std::string&,int`` (to tell overloads apart)."""
    out = []
    depth, cur = 0, ""
    for ch in params + ",":
        if ch in "(<[{":
            depth += 1
        elif ch in ")>]}":
            depth -= 1
        if ch == "," and depth == 0:
            p = cur.split("=")[0].strip()
            toks = re.findall(r"[A-Za-z_]\w*|::|[*&]+|<[^>]*>|\.\.\.", p)
            if len(toks) >= 2 and re.match(r"[A-Za-z_]\w*$", toks[-1]) and toks[-2] not in ("::",):
                p = p[:p.rfind(toks[-1])].strip()
            if p and p != "void":
                out.append(squash(p))
            cur = ""
        else:
            cur += ch
    return ",".join(out)


def _function(header: str, hstart: int, scan: _Scan, owner: _Open | None) -> dict[str, Any] | None:
    """A function or method named by ``header`` (the text before its ``{`` or ``;``, starting at ``hstart``)."""
    h, flags = _strip(header)
    lead = len(h) - len(h.lstrip())  # blanked comments before the declaration
    h, hstart = h[lead:].rstrip(), hstart + lead
    if not h or len(h) > 4000 or "typedef" in flags or \
            re.match(r"(?:using|friend|static_assert|namespace|template|return)\b", h):
        return None
    open_ = -1
    name = ""
    name_start = 0
    op = re.search(r"\boperator\b\s*(\(\s*\)|\[\s*\]|(?:new|delete)(?:\s*\[\s*\])?|[^\w\s(]+|"
                   r"[A-Za-z_][\w:\s<>*&]*?)\s*\(", h)
    if op:
        open_ = op.end() - 1
        name_start = op.start()
        name = "operator" + re.sub(r"\s+", "", op.group(1)) if re.match(r"[^\w]", op.group(1)) else \
            "operator " + squash(op.group(1))
        q = re.compile(r"((?:[A-Za-z_]\w*(?:<[^()]*?>)?\s*::\s*)+)$").search(h, max(0, name_start - 300), name_start)
        if q:
            name_start = q.start(1)
            name = re.sub(r"\s+", "", q.group(1)) + name
    else:
        angle = k = 0
        while k < len(h):
            ch = h[k]
            if ch == "<":
                angle += 1
            elif ch == ">" and angle:
                angle -= 1
            elif ch == "(" and angle == 0:
                cut = max(0, k - 300)
                m = _NAME_BEFORE.search(h, cut, k)
                word = m.group(1).split("::")[-1].strip() if m else ""
                close = match_close(h, k, "()")
                later = re.search(r"[A-Za-z_]\w*\s*\(", h[close + 1:]) if close < len(h) else None
                if m is None or word in _NOT_NAMES or (re.fullmatch(r"[A-Z_][A-Z0-9_]+", word) and later):
                    if close >= len(h) - 1 and h[close:close + 1] != ")":
                        return None
                    k = close + 1  # an attribute or a macro with arguments: the name comes later
                    continue
                if word in _TYPE_WORDS:
                    return None  # int (*fp)(int): a pointer to a function, not a function
                open_ = k
                name_start = m.start(1)
                name = re.sub(r"\s+", "", m.group(1))
                break
            elif ch in ";{}=" and angle == 0:
                return None
            k += 1
    if open_ < 0:
        return None
    close = match_close(h, open_, "()")
    if close >= len(h) or h[close] != ")":
        return None
    tail = h[close + 1:]
    if not _tail_ok(tail):
        return None
    ret_raw = h[:name_start]
    if re.search(r"(?<![=!<>])=(?!=)", ret_raw) or re.search(r"\b(?:return|typedef|using|case|goto)\b", ret_raw):
        return None
    static = bool(re.search(r"\bstatic\b", ret_raw))
    toks = squash(_SPECIFIERS.sub(" ", ret_raw)).split()
    while len(toks) > 1 and re.fullmatch(r"[A-Z_][A-Z0-9_]+", toks[0]) and toks[0] not in ("BOOL", "DWORD", "HRESULT"):
        toks.pop(0)  # an export macro: LEVELDB_EXPORT Status Open(…)
    ret = " ".join(toks)
    if "operator" in name:
        base = name[:name.index("operator")]
        name = re.sub(r"<[^<>]*>", "", base) + name[len(base):]
    else:
        while "<" in name and re.search(r"<[^<>]*>", name):
            name = re.sub(r"<[^<>]*>", "", name)
    segs = name.split("::")
    short = segs[-1]
    qualified = len(segs) > 1
    in_type = owner is not None and owner.kind == "type"
    if not ret:
        ok = short.startswith("operator") or \
            (in_type and short.lstrip("~") == owner.name.split("::")[-1]) or \
            (qualified and short.lstrip("~") == segs[-2])
        if not ok:
            return None  # TEST(Suite, Name) { … }: a macro, not a function
    if "(" in ret or ")" in ret or ret in ("else", "do"):
        return None
    arrow = re.search(r"->\s*([^{;:]+?)\s*(?:requires\b.*)?$", tail, re.S)
    if ret == "auto" and arrow:
        ret = squash(arrow.group(1))
    params = squash(scan.noc[hstart + open_ + 1:hstart + close])
    return {"name": name, "short": short, "ret": ret, "params": params, "static": static, "qualified": qualified}


def _add(scan: _Scan, qual: str, name: str, kind: str, start: int, end: int | None, signature: str | None,
         public: bool, body: bool, types: str | None = None) -> dict[str, Any]:
    d = {"qual": qual, "name": name, "kind": kind, "start": start, "end": end, "signature": signature,
         "public": public, "body": body, "types": types}
    if len(scan.decls) < MAX_SYMBOLS:
        scan.decls.append(d)
    return d


def _prefix(stack: list[_Open]) -> str:
    return "::".join(o.name for o in stack if o.name and o.kind in ("namespace", "type"))


def _brace_init(hb: str) -> bool:
    """Whether a ``{`` after the (blanked) header ``hb`` starts an initializer rather than a scope."""
    if hb.count("(") > hb.count(")") or hb.count("[") > hb.count("]"):
        return True  # inside parentheses: a default argument, a lambda in a call
    head = hb.split("(")[0]
    if "operator" not in head and re.search(r"(?<![=!<>+\-*/%&|^])=(?![=>])", head):
        return True  # int table[] = { … }, auto f = [] { … }
    # Foo() : a_(1), b_{2} { … }: a member's brace-init in a constructor's initializer list
    return bool(re.search(r"\)\s*(?:noexcept\s*)?:", hb) and re.search(r"[\w>]\s*$", hb))


def _declarations(scan: _Scan) -> None:
    code = scan.code
    stack: list[_Open] = []
    boundary = -1
    tokens = re.compile(r"[{};]|\b(public|private|protected)\s*:(?!:)")
    pos = 0
    while True:
        m = tokens.search(code, pos)
        if m is None:
            break
        ch, at = m.group(), m.start()
        pos = m.end()
        top = stack[-1] if stack else None
        if ch == "}":
            if stack:
                closed = stack.pop()
                if closed.decl is not None:
                    closed.decl["end"] = at
                    if closed.kind == "type" and not closed.decl["name"]:  # typedef struct { … } Name;
                        am = re.match(r"\s*\**\s*([A-Za-z_]\w*)\s*[;,\[]", code[at + 1:at + 200])
                        if am:
                            prefix = _prefix(stack)
                            closed.decl["name"] = am.group(1)
                            closed.decl["qual"] = f"{prefix}::{am.group(1)}" if prefix else am.group(1)
            boundary = at
            continue
        if top is not None and top.kind not in ("namespace", "type", "extern"):
            if ch == "{":
                stack.append(_Open("block"))  # inside a function: only the nesting matters
            continue
        if m.group(1):  # public: / private: / protected:
            if top is not None and top.kind == "type":
                top.access = m.group(1)
            boundary = m.end() - 1
            continue
        hstart = boundary + 1
        header = code[hstart:at]
        hb, flags = _strip(header)
        if ch == "{" and _brace_init(hb):
            pos = match_close(code, at) + 1  # not a scope: the statement goes on after it
            continue
        boundary = at
        hidden = any(o.hidden for o in stack)
        prefix = _prefix(stack)
        in_type = top is not None and top.kind == "type"
        access_ok = not in_type or top.access != "private"  # protected members are API for subclasses
        h = hb.strip()
        start = hstart + len(header) - len(header.lstrip())
        if ch == ";":
            if (scan.header_file or in_type) and not re.match(r"(?:class|struct|union|enum)\b[^()]*$", h):
                fn = _function(header, hstart, scan, top)
                if fn is not None and not (fn["qualified"] and not in_type):
                    qual = f"{prefix}::{fn['name']}" if prefix else fn["name"]
                    sig = f"({fn['params']})" + (f" -> {fn['ret']}" if fn["ret"] else "")
                    public = not hidden and access_ok and (in_type or not fn["static"] or scan.header_file)
                    _add(scan, qual, fn["short"], "method" if in_type else "function", start, at, sig, public, False,
                         _params_types(fn["params"]))
            continue
        nm = re.fullmatch(r"(inline\s+)?namespace\b\s*([\w:\s]*)", h)
        if nm:
            name = "" if nm.group(1) else re.sub(r"\s+", "", nm.group(2))  # inline namespaces are transparent
            stack.append(_Open("namespace", None, name, hidden=not name and not nm.group(1)))
            continue
        if not h and "extern" in flags:
            stack.append(_Open("extern"))  # extern "C" { … }
            continue
        tm = _TYPE.match(h)
        if tm:
            word = tm.group(1).split()[0]
            name = tm.group(3) or ""
            if not name and tm.group(2).split():
                name = re.sub(r"\(.*", "", tm.group(2).split()[-1])  # class FOO {: the name, not a macro
            name = re.sub(r"\s+", "", name)
            qual = f"{prefix}::{name}" if prefix and name else name
            public = scan.header_file and not hidden and access_ok
            decl = _add(scan, qual, name.split("::")[-1], word, start, None, None, public, True)
            stack.append(_Open("type", decl, name, "private" if word == "class" else "public", hidden=not public))
            continue
        fn = _function(header, hstart, scan, top) if not re.match(r"friend\b", h) else None
        if fn is None:
            stack.append(_Open("block"))
            continue
        if in_type:
            kind, public = "method", not hidden and access_ok
        elif fn["qualified"]:
            kind, public = "method", False  # defined out of line: the declaration in the class is the API
        else:
            kind, public = "function", not hidden and (not fn["static"] or scan.header_file)  # static inline in a header
        qual = f"{prefix}::{fn['name']}" if prefix else fn["name"]
        sig = f"({fn['params']})" + (f" -> {fn['ret']}" if fn["ret"] else "")
        decl = _add(scan, qual, fn["short"], kind, start, None, sig, public, True, _params_types(fn["params"]))
        stack.append(_Open("function", decl))
    for d in scan.decls:
        if d["end"] is None:
            d["end"] = len(code) - 1


def parse_c(text: str, path: str = "") -> dict[str, Any]:
    """Everything the analyzer needs from one C or C++ file, as plain JSON."""
    if text.startswith("\ufeff"):
        text = " " + text[1:]
    code, noc = mask_pair(text, C_TOKENS)
    lines = LineIndex(text)
    code, includes = _preprocess(code, noc)
    scan = _Scan(code, noc, lines, path.lower().endswith(HEADER_EXTS))
    _declarations(scan)
    defined = {(d["qual"], d["types"]) for d in scan.decls if d["body"]}
    decls = [d for d in scan.decls if d["body"] or (d["qual"], d["types"]) not in defined]
    count = Counter(d["qual"] for d in decls if d["kind"] in ("function", "method"))
    seen: Counter[str] = Counter()
    symbols = []
    main = False
    for d in decls:
        if not d["name"]:
            continue  # an anonymous struct or enum
        qual = d["qual"]
        if d["kind"] in ("function", "method") and count[qual] > 1:
            qual = f"{qual}({d['types']})"
        seen[qual] += 1
        if seen[qual] > 1:
            if d["kind"] not in ("function", "method"):
                continue  # a type declared twice in #if branches: one symbol
            qual = f"{qual}~{seen[qual]}"
        if d["kind"] == "function" and d["name"] in ("main", "wmain", "WinMain", "wWinMain") and d["body"] \
                and "::" not in d["qual"]:
            main = True
        a, b = d["start"], d["end"] + 1
        sig = d["signature"]
        symbols.append({"qual": qual, "name": d["name"], "kind": d["kind"], "line": lines.line(a),
                        "end_line": lines.line(max(a, b - 1)), "fingerprint": stable_hash(text[a:b]),
                        "semantic": stable_hash(squash(noc[a:b])),
                        "signature": sig if sig is None or len(sig) <= MAX_SIGNATURE else sig[:MAX_SIGNATURE - 3] + "...",
                        "signature_id": stable_hash("sig", sig, length=12) if sig else None, "public": d["public"]})
    return {"includes": includes, "symbols": symbols, "main": main, "semantic": stable_hash(squash(noc)),
            "loc": text.count("\n") + (0 if text.endswith("\n") or not text else 1)}


def _parse_item(item: tuple[str, str]) -> tuple[str, dict[str, Any]]:
    return item[0], parse_c(item[1], item[0])


# --------------------------------------------------------------------------- build files (read, never run)

_CMAKE_INCLUDES = re.compile(r"\b(target_include_directories|include_directories)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)",
                             re.I)
_CMAKE_KEYWORDS = {"PUBLIC", "PRIVATE", "INTERFACE", "SYSTEM", "BEFORE", "AFTER"}


def _cmake_dirs(path: str, text: str, project_dir: str) -> list[str]:
    """The include directories a CMake file names, as repository paths."""
    here = posixpath.dirname(path)
    text = re.sub(r"#[^\n]*", " ", text)
    out = []
    for m in _CMAKE_INCLUDES.finditer(text):
        args = re.findall(r'"[^"]*"|[^\s"]+', m.group(2))
        if m.group(1).lower() == "target_include_directories":
            args = args[1:]  # the target
        for raw in args:
            item = raw.strip('"')
            if item.upper() in _CMAKE_KEYWORDS:
                continue
            bm = re.fullmatch(r"\$<BUILD_INTERFACE:(.*)>", item)
            if bm:
                item = bm.group(1)
            elif item.startswith("$<"):
                continue  # $<INSTALL_INTERFACE:…> and other generator expressions
            root = None  # the folder the item is relative to
            vm = re.match(r"\$\{(\w+)\}/?", item)
            if vm:
                var = vm.group(1)
                if var in ("CMAKE_CURRENT_SOURCE_DIR", "CMAKE_CURRENT_LIST_DIR"):
                    root = here
                elif var == "CMAKE_SOURCE_DIR":
                    root = ""
                elif var == "PROJECT_SOURCE_DIR" or var.endswith("_SOURCE_DIR"):
                    root = project_dir
                else:
                    continue  # the build tree, or a variable set elsewhere
                item = item[vm.end():]
            else:
                root = here
            if "${" in item or "$<" in item or item.startswith("/"):
                continue
            out.append(posixpath.normpath(posixpath.join(root, item)) if root or item else ".")
    return out


def _make_dirs(path: str, text: str) -> list[str]:
    here = posixpath.dirname(path)
    out = []
    for m in re.finditer(r"(?<![\w-])-I\s*([^\s$()\"']+)", text):
        item = m.group(1)
        if not item.startswith("/"):
            out.append(posixpath.normpath(posixpath.join(here, item)))
    return out


def _meson_dirs(path: str, text: str) -> list[str]:
    here = posixpath.dirname(path)
    out = []
    for m in re.finditer(r"include_directories\s*\(([^)]*)\)", text):
        for item in re.findall(r"'([^']*)'", m.group(1)):
            if not item.startswith("/"):
                out.append(posixpath.normpath(posixpath.join(here, item)))
    return out


def _bazel_dirs(path: str, text: str) -> list[str]:
    here = posixpath.dirname(path)
    out = []
    for m in re.finditer(r"\bincludes\s*=\s*\[([^\]]*)\]", text):
        for item in re.findall(r"[\"']([^\"']*)[\"']", m.group(1)):
            if not item.startswith("/"):
                out.append(posixpath.normpath(posixpath.join(here, item)))
    return out


def _build_kind(path: str) -> str | None:
    base = posixpath.basename(path)
    if base == "CMakeLists.txt" or base.endswith(".cmake"):
        return "cmake"
    if base in ("Makefile", "GNUmakefile", "makefile", "Makefile.am", "Makefile.in") or base.endswith(".mk"):
        return "make"
    if base in ("meson.build",):
        return "meson"
    if base in ("BUILD", "BUILD.bazel"):
        return "bazel"
    if base in ("configure.ac", "configure.in", "SConstruct", "SConscript", "premake5.lua", "xmake.lua") or \
            base.endswith((".sh", ".gn", ".gni", ".bzl", ".pro", ".pri", ".vcxproj")):
        return "other"
    return None


# --------------------------------------------------------------------------- the analyzer


class CFamilyAnalyzer(Analyzer):
    name = "cfamily"
    version = "1"  # bump when the parse result or the graph changes (part of the cache keys)
    languages = ("c", "cpp")
    capabilities = (CAP_MODULES, CAP_SYMBOLS, CAP_DEPENDENCIES, CAP_ENTRY_POINTS)

    def detect(self, ctx: AnalysisContext) -> Detection:
        n = len(ctx.files("c")) + len(ctx.files("cpp"))
        return Detection(bool(n), f"{n} C/C++ file(s)" if n else "no C or C++ files")

    def discover_modules(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        texts: dict[str, tuple[tuple[Any, ...], str]] = {}
        for f in sorted(ctx.files("c") + ctx.files("cpp")):
            text = ctx.text(f)
            if text is None:
                continue
            digest = ctx.source.content_hash(f) or stable_hash(text)
            texts[f] = (("cfamily", self.version, digest, f.lower().endswith(HEADER_EXTS)), text)
        misses = [(f, text) for f, (key, text) in texts.items() if key not in ctx.file_cache]
        for f, parsed in parse_parallel(_parse_item, misses).items():
            ctx.file_cache[texts[f][0]] = parsed
        infos: dict[str, dict[str, Any]] = {}
        for f, (key, text) in texts.items():
            infos[f] = ctx.cached(key, lambda t=text, p=f: parse_c(t, p))
        ctx.shared["cfamily.infos"] = infos
        for f, info in infos.items():
            node = b.ensure_file(f, self.name)
            lang = ctx.profile.file_languages.get(f, ("c", None))[0] or "c"
            tags = [lang] + (["test"] if ctx.profile.is_test(f) else [])
            if info["main"] and "test" not in tags:
                tags.append("entry-point")
            b.add_node(ComponentNode(
                id=node.id, name=posixpath.basename(f), qualified_name=f, component_type="module",
                category=CATEGORY_MODULE, language=lang, path=f, analyzer=self.name, key=node.key, tags=tags,
                metadata={"loc": info["loc"], "semantic_fingerprint": info["semantic"],
                          **({"entry_kind": f"{'C++' if lang == 'cpp' else 'C'} program"}
                             if info["main"] and "test" not in tags else {})}))
            node.category = CATEGORY_MODULE
        b.stat(self.name, "modules", len(infos))

    def discover_symbols(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, dict[str, Any]] = ctx.shared.get("cfamily.infos", {})
        for f, info in infos.items():
            module = b.nodes[b.file_id(f)]
            for s in info["symbols"]:
                kind = s["kind"]
                ctype = kind if kind in ("method", "function") else "class"
                meta: dict[str, Any] = {"kind": kind, "public": s["public"], "semantic_fingerprint": s["semantic"]}
                if s["signature"]:
                    meta["signature"] = s["signature"]
                    meta["signature_id"] = s["signature_id"]
                b.add_node(ComponentNode(
                    id=b.symbol_id(f, s["qual"]), name=s["name"], qualified_name=s["qual"], component_type=ctype,
                    category=CATEGORY_SYMBOL, language=module.language, path=f, parent_id=module.id,
                    analyzer=self.name, key=f"symbol:{f}:{s['qual']}", fingerprint=s["fingerprint"],
                    start_line=s["line"], end_line=s["end_line"], metadata=meta))
                b.stat(self.name, "symbols")
        if infos:
            b.diagnostic("info", "callflow-unsupported", "Call-flow extraction is not implemented for C and C++; the "
                         "Activity tab falls back to module-level import impact.", self.name)

    # -- dependencies -------------------------------------------------------------------------------------------

    def _build_info(self, ctx: AnalysisContext) -> tuple[list[str], set[str]]:
        """Include directories from the build files, and the header names they mention (generated ones)."""
        present = ctx.source.file_set()
        build_files = sorted(f for f in present if _build_kind(f))[:MAX_BUILD_FILES]
        projects = {posixpath.dirname(p) for p, md in ctx.profile.manifest_data.items()
                    if md.kind == "cmake" and md.name}
        dirs: list[str] = []
        mentioned: set[str] = set()
        for f in build_files:
            text = ctx.text(f)
            if text is None:
                continue
            kind = _build_kind(f)
            here = posixpath.dirname(f)
            project = here
            while project and project not in projects:
                project = posixpath.dirname(project)
            if kind == "cmake":
                found = _cmake_dirs(f, text, project)
            elif kind == "make":
                found = _make_dirs(f, text)
            elif kind == "meson":
                found = _meson_dirs(f, text)
            elif kind == "bazel":
                found = _bazel_dirs(f, text)
            else:
                found = []
            dirs.extend("" if d == "." else d for d in found if not d.startswith(".."))
            mentioned.update(posixpath.basename(x) for x in
                             re.findall(r"[\w./+-]+\.(?:h|hh|hpp|hxx|inc|def|inl)\b", text))
        return list(dict.fromkeys(dirs)), mentioned

    def discover_dependencies(self, ctx: AnalysisContext, b: SnapshotBuilder) -> None:
        infos: dict[str, dict[str, Any]] = ctx.shared.get("cfamily.infos", {})
        if not infos:
            return
        present = ctx.source.file_set()
        files = set(infos)
        build_dirs, mentioned = self._build_info(ctx)
        folders = {posixpath.dirname(f) for f in present}
        all_dirs = set()
        for d in folders:
            while d and d not in all_dirs:
                all_dirs.add(d)
                d = posixpath.dirname(d)
        conventional = sorted((d for d in all_dirs if posixpath.basename(d) == "include"), key=len)
        include_dirs = list(dict.fromkeys(build_dirs + conventional))
        by_suffix: dict[str, list[str]] = {}
        for f in files:
            parts = f.split("/")
            for k in range(len(parts)):
                by_suffix.setdefault("/".join(parts[k:]), []).append(f)
        packages: dict[str, str] = {}  # normalized find_package name -> declared name
        for md in ctx.profile.manifest_data.values():
            if md.ecosystem == "cmake":
                for dep in md.dependencies:
                    packages[re.sub(r"[^a-z0-9]", "", dep.name.lower())] = dep.name
        templates = {posixpath.basename(f) for f in present}
        edges = 0
        cache: dict[tuple[str, str, str], str | None] = {}

        def resolve(f: str, kind: str, path: str) -> tuple[str | None, float]:
            here = posixpath.dirname(f)
            if kind == "local":
                cand = posixpath.normpath(posixpath.join(here, path))
                if cand in files:
                    return cand, 1.0
            key = (kind, path, here if kind == "local" else "")
            if key not in cache:
                hit = None
                for d in include_dirs:
                    cand = posixpath.normpath(posixpath.join(d, path)) if d else posixpath.normpath(path)
                    if cand in files:
                        hit = cand
                        break
                cache[key] = hit
            if cache[key] is not None:
                return cache[key], 1.0
            if kind == "system" and ("/" not in path or path.split("/")[0] in SYSTEM_DIRS or std_kind(path)):
                return None, 0.0
            cands = by_suffix.get(posixpath.normpath(path), [])
            if len(cands) == 1:
                return cands[0], 0.7
            if len(cands) > 1:  # the nearest one, if one is nearest
                ranked = sorted(cands, key=lambda c: -len(posixpath.commonprefix([c, f])))
                if len(posixpath.commonprefix([ranked[0], f])) > len(posixpath.commonprefix([ranked[1], f])):
                    return ranked[0], 0.6
            return None, 0.0

        for f, info in infos.items():
            src = b.file_id(f)
            test = ctx.profile.is_test(f)
            here = posixpath.dirname(f)
            for kind, path, line, conditional in info["includes"]:
                target, confidence = resolve(f, kind, path)
                if target is not None:
                    if target == f:
                        continue
                    meta: dict[str, Any] = {"imported_names": [path]}
                    if test:
                        meta["test_only"] = True
                    if conditional:
                        meta["conditional"] = True
                    b.add_edge(src, b.file_id(target), REL_IMPORTS, analyzer=self.name,
                               evidence=[self.evidence(ctx, f, line, line, "include")], confidence=confidence,
                               metadata=meta)
                    edges += 1
                    continue
                norm = posixpath.normpath(path)
                beside = posixpath.normpath(posixpath.join(here, path))
                if norm in present and norm != f or kind == "local" and beside in present and beside != f:
                    continue  # there, but not a C/C++ file (.inc, .def, too large)
                std = std_kind(path)
                folder = posixpath.dirname(norm)
                if std is None and (kind == "local" or folder):
                    base = posixpath.basename(norm)
                    roots = ([here] if kind == "local" else []) + include_dirs
                    folder_here = kind == "local" and not folder or bool(folder) and any(
                        (posixpath.normpath(posixpath.join(d, folder)) if d else folder) in all_dirs for d in roots)
                    generated = base in mentioned or _GENERATED.search(base) or any(
                        t in templates for t in (base + ".in", base + ".cmake", base + ".cmakein",
                                                 _stem(base) + ".in" + base[len(_stem(base)):]))
                    if generated and kind == "local":
                        continue  # made by the build (configure_file, a .in template): nothing to link
                    if folder_here and not generated:
                        if not conditional:
                            b.diagnostic("warning", "unresolved-internal-import",
                                         f"`#include {'<' if kind == 'system' else chr(34)}{path}"
                                         f"{'>' if kind == 'system' else chr(34)}` does not exist in the repository "
                                         f"(its folder does, next to the file or under an include directory).",
                                         self.name, f, line)
                        continue
                    if kind == "local" and not folder:
                        continue  # "config.h" from the machine, or a header of another platform
                self._external(b, src, f, ctx, path, line, std, packages, test)
        b.stat(self.name, "import_edges", edges)
        b.stat(self.name, "include_dirs", len(include_dirs))

    def _external(self, b: SnapshotBuilder, src: str, f: str, ctx: AnalysisContext, path: str, line: int,
                  std: str | None, packages: dict[str, str], test: bool) -> None:
        if std:
            eco, key_name, ext_name = "c-std", std, {"c": "C standard library", "c++": "C++ standard library",
                                                     "system": "System headers"}[std]
        else:
            first = path.split("/")[0] if "/" in path else _stem(path)
            norm = re.sub(r"[^a-z0-9]", "", first.lower())
            alias = {"gmock": "gtest", "googletest": "gtest", "boost": "boost", "qt": "qt"}.get(norm, norm)
            dep = packages.get(alias) or packages.get(norm) or next(
                (d for k, d in packages.items() if k and (k.startswith(alias) or alias.startswith(k)) and len(k) > 2),
                None)
            if dep is None and re.match(r"Q[A-Z]|Qt[A-Z]", first):
                dep = next((d for k, d in packages.items() if k.startswith("qt")), None)
            if dep is not None:
                eco, key_name, ext_name = "cmake", dep.lower(), dep
            else:
                eco, key_name, ext_name = "c", first.lower(), first
        ext_id = b.external_id(eco, key_name)
        if ext_id not in b.nodes:
            b.add_node(ComponentNode(id=ext_id, name=ext_name, qualified_name=ext_name,
                                     component_type="external-package", analyzer=self.name,
                                     key=f"external:{eco}:{key_name}", tags=["external"] + (["stdlib"] if std else []),
                                     metadata={"ecosystem": "cmake" if eco == "cmake" else "c"}))
        meta: dict[str, Any] = {"external": True, "imported_names": [path]}
        if test:
            meta["test_only"] = True
        b.add_edge(src, ext_id, REL_IMPORTS, analyzer=self.name,
                   evidence=[self.evidence(ctx, f, line, line, "include")], confidence=0.9, metadata=meta)
