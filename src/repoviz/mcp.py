"""``repoviz mcp``: a read-only Model Context Protocol server for coding agents.

repoviz helps a person supervise an agent; this lets the agent itself ask before it edits (where does this file
belong, what depends on this function, is this path in scope, may this layer import that one) and check its own
work before it hands over (review signals, contract violations).

Transport: MCP over stdio, that is JSON-RPC 2.0 with one message per line on stdin and stdout, written with the
standard library only.  Methods: ``initialize``, ``ping``, ``tools/list``, ``tools/call``, ``prompts/list`` and
``prompts/get``.  Every tool reads; nothing is written to the repository, and nothing to the state directory
unless the server was started with ``--allow-writes``, which adds ``set_scope`` (the current session's scope).
Answers are capped (``max_items``, then :data:`MAX_RESULT_CHARS` in total), deterministic and redacted.  Paths
must stay inside the repository.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from pathlib import Path
from typing import IO, Any, Callable

from . import __version__, classify
from .activity import nodes_by_path
from .filechanges import checked_path
from .graph import shortest_paths
from .ids import make_id
from .model import CATEGORY_MODULE, CATEGORY_SYMBOL, REL_CALLS, REL_IMPORTS, REL_INVOKES
from .redact import redact

log = logging.getLogger(__name__)

#: Protocol revisions this server speaks, newest first (the client's is used when it is one of them).
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_RESULT_CHARS = 16_000  # about 4k tokens
MAX_MESSAGE_BYTES = 1_000_000
DEFAULT_ITEMS = 20
MAX_ITEMS = 200
MAX_DEPTH = 6
MAX_IMPACT_NODES = 5_000
SEVERITIES = ("high", "medium", "low", "info")

INSTRUCTIONS = (
    "repoviz knows this repository's architecture: components, layers and contracts, imports, calls, tests and "
    "the agreed scope of the current work session. Before editing, call check_scope with the files you plan to "
    "touch and where_does_this_go for a file you want to create. Before changing a function's signature or a "
    "module's interface, call impact. Before handing over, call review_current and contracts_check and fix the "
    "high signals. Every tool is read-only."
)

# JSON-RPC error codes
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603


class ToolError(Exception):
    """A problem the agent can fix (unknown name, a path outside the repository…): returned as a tool result
    with ``isError`` so the model sees it, not as a protocol error."""


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


# --------------------------------------------------------------------------- tool definitions

def _items(default: int = DEFAULT_ITEMS) -> dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": MAX_ITEMS, "default": default,
            "description": f"Longest list returned (1–{MAX_ITEMS})."}


_TARGET = {"type": "string", "description": "A repository-relative path (file or directory) or a qualified name "
                                           "(`pkg.module`, `pkg.module.function`, or just a unique name)."}

TOOLS: dict[str, dict[str, Any]] = {
    "architecture_overview": {
        "title": "Architecture overview",
        "description": "The repository at a glance: languages, projects, components and the imports between them, "
                       "entry points, the architecture contracts (layers…) and whether they pass, import cycles.",
        "inputSchema": {"type": "object", "properties": {"max_items": _items()}},
    },
    "where_does_this_go": {
        "title": "Where does this go?",
        "description": "Where a file, directory or symbol sits (or would sit, for a new file): component, project, "
                       "layer and what that layer may and may not import, the contracts that apply, the scope "
                       "verdict (allowed, protected, out of scope) and whether it is a test or an entry point.",
        "inputSchema": {"type": "object", "properties": {"target": _TARGET}, "required": ["target"]},
    },
    "impact": {
        "title": "Impact of a change",
        "description": "What depends on a file, module or symbol: callers and importers up to `depth` steps away, "
                       "the entry points reached and the tests affected, nearest first, with file:line evidence.",
        "inputSchema": {"type": "object", "properties": {
            "target": _TARGET,
            "depth": {"type": "integer", "minimum": 1, "maximum": MAX_DEPTH, "default": 2,
                      "description": f"How many steps of callers and importers to follow (1–{MAX_DEPTH})."},
            "max_items": _items()}, "required": ["target"]},
    },
    "dependency_path": {
        "title": "Why does A depend on B?",
        "description": "The shortest import chains from `source` to `target` (modules, or every module of a "
                       "component or directory), each step with the import's file:line.",
        "inputSchema": {"type": "object", "properties": {
            "source": dict(_TARGET, description="Where the chain starts (path or qualified name)."),
            "target": dict(_TARGET, description="Where it ends (path or qualified name)."),
            "max_paths": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3,
                          "description": "How many chains to return (1–10)."}},
            "required": ["source", "target"]},
    },
    "check_scope": {
        "title": "Check planned edits against the scope",
        "description": "For each file you plan to create or edit: allowed, protected (do not edit), out of scope "
                       "(ask first) or unscoped, with the matching rule, the component and sensitive areas "
                       "(migrations, CI, deployment). The scope comes from the active session and the "
                       "configuration.",
        "inputSchema": {"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_ITEMS,
                      "description": "Repository-relative paths."}}, "required": ["paths"]},
    },
    "review_current": {
        "title": "Review the current work",
        "description": "The review signals of the current work (the active session, else the uncommitted "
                       "changes, or any `target`), riskiest files first: scope violations, broken imports, "
                       "callers left behind, new cycles, contract violations, weakened tests, secrets…",
        "inputSchema": {"type": "object", "properties": {
            "target": {"type": "string", "description": "What to review, as for `repoviz review`: `session`, "
                                                        "`branch`, `last-commit`, `main...HEAD`… (default: "
                                                        "the current work)."},
            "min_severity": {"type": "string", "enum": list(SEVERITIES), "default": "medium",
                             "description": "Lowest severity returned."},
            "max_items": _items()}},
    },
    "contracts_check": {
        "title": "Check the architecture contracts",
        "description": "The architecture contracts on the working tree: each contract's status and every "
                       "violation that is not in the known-violations baseline, with file:line and the import "
                       "chain. Without contracts, a suggested layers contract.",
        "inputSchema": {"type": "object", "properties": {"max_items": _items()}},
    },
}

SET_SCOPE = {
    "title": "Set the session scope",
    "description": "Replace the active session's allowed and protected globs (only with --allow-writes). "
                   "Omit a list to keep it.",
    "inputSchema": {"type": "object", "properties": {
        "allowed": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_ITEMS},
        "protected": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_ITEMS}}},
}

PROMPTS: dict[str, dict[str, Any]] = {
    "self_review": {
        "title": "Self-review before handing over",
        "description": "Check your own work with repoviz and fix the high signals before handing it over.",
        "arguments": [{"name": "target", "required": False,
                       "description": "What to review (default: the current work), e.g. `branch` or `main...HEAD`."}],
    },
    "plan_check": {
        "title": "Check a plan's files",
        "description": "Check the files you plan to touch against the scope and the architecture before editing.",
        "arguments": [{"name": "files", "required": True,
                       "description": "The files you plan to create or edit, one per line or comma-separated."}],
    },
}


# --------------------------------------------------------------------------- helpers

def _check_args(schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """The arguments with defaults filled in, or ``ToolError`` (unknown or missing argument, wrong type)."""
    props = schema.get("properties", {})
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolError(f"unknown argument(s): {', '.join(unknown)}; expected: {', '.join(props) or 'none'}")
    out: dict[str, Any] = {}
    for name, spec in props.items():
        if name not in args or args[name] is None:
            if name in schema.get("required", ()):
                raise ToolError(f"missing argument {name!r}")
            if "default" in spec:
                out[name] = spec["default"]
            continue
        value, kind = args[name], spec["type"]
        if kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError(f"{name!r} must be an integer")
            value = max(spec.get("minimum", value), min(spec.get("maximum", value), value))
        elif kind == "string":
            if not isinstance(value, str) or not value.strip():
                raise ToolError(f"{name!r} must be a non-empty string")
            if "enum" in spec and value not in spec["enum"]:
                raise ToolError(f"{name!r} must be one of {', '.join(spec['enum'])}")
        elif kind == "array":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ToolError(f"{name!r} must be a list of strings")
            if len(value) > spec.get("maxItems", len(value)) or len(value) < spec.get("minItems", 0):
                raise ToolError(f"{name!r} must have {spec.get('minItems', 0)} to {spec.get('maxItems')} items")
        out[name] = value
    return out


def _size(value: Any) -> int:
    return len(json.dumps(value, indent=1, ensure_ascii=False))  # as the text content shows it


def _largest(value: Any, where: str = "", skip: str = "truncated") -> tuple[int, str, Any]:
    """The largest list (by serialized size) inside ``value``, as ``(size, where, list)``."""
    best: tuple[int, str, Any] = (-1, "", None)
    if isinstance(value, list):
        if len(value) > 1:
            best = (_size(value), where or "result", value)
        items = enumerate(value)
    elif isinstance(value, dict):
        items = ((k, v) for k, v in value.items() if not (k == skip and not where))  # type: ignore[assignment]
    else:
        return best
    for k, v in items:
        cand = _largest(v, f"{where}.{k}" if where else str(k))
        if cand[0] > best[0]:
            best = cand
    return best


def fit(data: dict[str, Any], limit: int = MAX_RESULT_CHARS) -> dict[str, Any]:
    """``data`` cut down to at most ``limit`` characters of (indented) JSON: the largest list is halved until it
    fits, then the longest strings, and ``truncated`` says what was left out.  Deterministic."""
    notes: list[str] = []
    while _size(data) > limit:
        size, where, lst = _largest(data)
        if lst is None:
            break
        keep = len(lst) // 2
        notes.append(f"{where}: {keep} of {len(lst)} kept")
        del lst[keep:]
        data["truncated"] = notes
    while _size(data) > limit:  # one huge string (a suggested contract, a long detail)
        path, parent, key, text = _longest_string(data)
        if parent is None or len(text) < 200:
            break
        parent[key] = text[: len(text) // 2] + " …"
        notes.append(f"{path}: shortened")
        data["truncated"] = notes
    return data


def _longest_string(value: Any, where: str = "", skip: str = "truncated") -> tuple[str, Any, Any, str]:
    best: tuple[str, Any, Any, str] = ("", None, None, "")
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for k, v in items:
        if k == skip and not where:
            continue
        here = f"{where}.{k}" if where else str(k)
        cand = (here, value, k, v) if isinstance(v, str) else _longest_string(v, here)
        if cand[1] is not None and len(cand[3]) > len(best[3]):
            best = cand
    return best


def _loc(path: str | None, line: int | None) -> str | None:
    return f"{path}:{line}" if path and line else path


def _evidence(edge: Any) -> dict[str, Any]:
    ev = edge.evidence[0] if edge is not None and edge.evidence else None
    if ev is None:
        return {}
    out = {"evidence": _loc(ev.path, ev.start_line)}
    if ev.excerpt:
        out["code"] = redact(ev.excerpt)[:200]
    return out


# --------------------------------------------------------------------------- graph index

FILE_LIKE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


class _Index:
    """Lookups over one snapshot, built once per working-tree state."""

    def __init__(self, snap: Any) -> None:
        self.snap = snap
        self.nodes = snap.node_index()
        self.by_path = nodes_by_path(snap)
        self.by_name: dict[str, list[Any]] = {}
        for n in snap.nodes():
            self.by_name.setdefault(n.qualified_name, []).append(n)
        # who uses X: X -> {user: edge}, over direct imports, calls and entry-point invocations
        self.users: dict[str, dict[str, Any]] = {}
        # module imports (internal, direct): for dependency paths
        self.imports: dict[str, set[str]] = {}
        self.import_edge: dict[tuple[str, str], Any] = {}
        for e in snap.edges():
            if not e.direct or e.source_id == e.target_id or e.relationship not in (REL_IMPORTS, REL_CALLS, REL_INVOKES):
                continue
            self.users.setdefault(e.target_id, {}).setdefault(e.source_id, e)
            if e.relationship == REL_IMPORTS and e.target_id in self.nodes and "external" not in self.nodes[e.target_id].tags:
                self.imports.setdefault(e.source_id, set()).add(e.target_id)
                self.import_edge.setdefault((e.source_id, e.target_id), e)
        self.children: dict[str, list[str]] = {}
        for n in snap.nodes():
            if n.parent_id:
                self.children.setdefault(n.parent_id, []).append(n.id)

    def dir_node(self, path: str) -> Any:
        return self.nodes.get(make_id("dir", f"path:dir:{path}"))

    def nearest_dir(self, path: str) -> Any:
        parts = path.split("/")[:-1]
        for i in range(len(parts), -1, -1):
            d = self.dir_node("/".join(parts[:i]))
            if d is not None:
                return d
        return None

    def component(self, node: Any) -> Any:
        for _ in range(64):
            if node is None:
                return None
            cid = node.metadata.get("component_id") or (node.id if "component" in node.tags else None)
            if cid and cid in self.nodes:
                return self.nodes[cid]
            node = self.nodes.get(node.parent_id or "")
        return None

    def module_of(self, node: Any) -> Any:
        for _ in range(64):
            if node is None or node.category != CATEGORY_SYMBOL:
                return node
            node = self.nodes.get(node.parent_id or "")
        return None

    def modules_under(self, node: Any) -> list[str]:
        """The modules a node stands for: itself, its module (a symbol), or every module below it."""
        if node.category == CATEGORY_MODULE:
            return [node.id]
        if node.category == CATEGORY_SYMBOL:
            m = self.module_of(node)
            return [m.id] if m is not None else []
        out, stack = [], [node.id]
        while stack and len(out) < MAX_IMPACT_NODES:
            for c in self.children.get(stack.pop(), ()):
                n = self.nodes[c]
                if n.category == CATEGORY_MODULE:
                    out.append(c)
                elif n.category != CATEGORY_SYMBOL:
                    stack.append(c)
        return sorted(out)


def describe(n: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"name": n.qualified_name, "kind": n.component_type}
    if n.path:
        out["at"] = _loc(n.path, n.start_line)
    return out


# --------------------------------------------------------------------------- server

class McpServer:
    """Handles MCP messages for one repository (:meth:`handle`), and serves them over stdio (:meth:`serve`)."""

    def __init__(self, repo: Any, *, allow_writes: bool = False) -> None:
        self.repo = repo
        self.allow_writes = allow_writes
        self.version = PROTOCOL_VERSIONS[0]
        self._index: tuple[tuple[Any, ...], _Index] | None = None
        self.handlers: dict[str, Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]] = {
            "architecture_overview": self.architecture_overview, "where_does_this_go": self.where_does_this_go,
            "impact": self.impact, "dependency_path": self.dependency_path, "check_scope": self.check_scope,
            "review_current": self.review_current, "contracts_check": self.contracts_check}
        self.tools = dict(TOOLS)
        if allow_writes:
            self.tools["set_scope"] = SET_SCOPE
            self.handlers["set_scope"] = self.set_scope

    # -- transport ---------------------------------------------------------------------------------------------

    def serve(self, stdin: IO[bytes], stdout: IO[bytes]) -> None:
        """Read one JSON-RPC message per line until end of input; write one response per request."""
        while True:
            line = stdin.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_MESSAGE_BYTES and not line.endswith(b"\n"):
                while True:  # skip the rest of the oversized message
                    chunk = stdin.readline(MAX_MESSAGE_BYTES + 1)
                    if not chunk or chunk.endswith(b"\n"):
                        break
                self._send(stdout, _error(None, INVALID_REQUEST, f"message larger than {MAX_MESSAGE_BYTES} bytes"))
                continue
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._send(stdout, _error(None, PARSE_ERROR, "parse error: not JSON"))
                continue
            response = self.handle(message)
            if response is not None:
                self._send(stdout, response)

    @staticmethod
    def _send(stdout: IO[bytes], message: dict[str, Any]) -> None:
        stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        stdout.flush()

    def handle(self, message: Any) -> dict[str, Any] | None:
        """The response to one message (``None`` for notifications and for responses sent by the client)."""
        if isinstance(message, list):
            return _error(None, INVALID_REQUEST, "batches are not supported")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST,
                          "invalid request: expected a JSON-RPC 2.0 object")
        method = message.get("method")
        if "id" not in message:
            return None  # a notification (initialized, cancelled…): nothing to answer
        mid = message["id"]
        if isinstance(mid, bool) or not isinstance(mid, (str, int)):
            return _error(None, INVALID_REQUEST, "invalid request: id must be a string or an integer")
        if not isinstance(method, str):
            if "result" in message or "error" in message:
                return None  # a response to a request we never send
            return _error(mid, INVALID_REQUEST, "invalid request: missing method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(mid, INVALID_PARAMS, "params must be an object")
        try:
            return {"jsonrpc": "2.0", "id": mid, "result": self.dispatch(method, params)}
        except _RpcError as exc:
            return _error(mid, exc.code, exc.message)
        except Exception as exc:  # never crash the session over one request
            log.exception("repoviz mcp: %s failed", method)
            return _error(mid, INTERNAL_ERROR, f"internal error: {type(exc).__name__}")

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion")
            self.version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {"protocolVersion": self.version,
                    "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
                    "serverInfo": {"name": "repoviz", "title": "repoviz", "version": __version__},
                    "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{"name": name, **spec, "annotations": {
                "title": spec["title"], "readOnlyHint": name != "set_scope", "destructiveHint": False,
                "idempotentHint": True, "openWorldHint": False}} for name, spec in self.tools.items()]}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments", {})
            args = {} if args is None else args
            if name not in self.handlers:
                raise _RpcError(INVALID_PARAMS, f"unknown tool {name!r}")
            if not isinstance(args, dict):
                raise _RpcError(INVALID_PARAMS, "arguments must be an object")
            return self.call_tool(name, args)
        if method == "prompts/list":
            return {"prompts": [{"name": name, **spec} for name, spec in PROMPTS.items()]}
        if method == "prompts/get":
            args = params.get("arguments", {})
            return self.get_prompt(params.get("name"), {} if args is None else args)
        raise _RpcError(METHOD_NOT_FOUND, f"method not found: {method}")

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            summary, data = self.handlers[name](_check_args(self.tools[name]["inputSchema"], args))
        except ToolError as exc:
            return {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}
        data = fit(data, MAX_RESULT_CHARS - len(summary) - 2)
        result: dict[str, Any] = {"content": [{"type": "text", "text": summary + "\n\n" + json.dumps(
            data, indent=1, ensure_ascii=False)}], "isError": False}
        if self.version >= "2025-06-18":
            result["structuredContent"] = data
        return result

    def get_prompt(self, name: Any, args: Any) -> dict[str, Any]:
        if name not in PROMPTS:
            raise _RpcError(INVALID_PARAMS, f"unknown prompt {name!r}")
        if not isinstance(args, dict) or not all(isinstance(v, str) for v in args.values()):
            raise _RpcError(INVALID_PARAMS, "prompt arguments must be strings")
        if name == "self_review":
            target = (args.get("target") or "").strip()
            call = f"review_current with target {target!r}" if target else "review_current"
            text = (f"Before you hand this work over, check it with repoviz:\n\n"
                    f"1. Call {call} (min_severity: medium).\n"
                    "2. Fix every high signal. For each medium signal, fix it or say in one line why it is fine.\n"
                    "3. Call contracts_check and fix every new violation (or explain why it must stay).\n"
                    "4. If you changed a function's signature or a module's interface, call impact on it and "
                    "update the callers and tests it lists.\n"
                    "5. Finish with a short summary: what you changed, the signals you left and why.")
            return {"description": PROMPTS[name]["description"], "messages": [_user(text)]}
        files = [f.strip() for f in re.split(r"[\n,]", args.get("files") or "") if f.strip()]
        if not files:
            raise _RpcError(INVALID_PARAMS, "files: list at least one file")
        try:
            summary, data = self.check_scope({"paths": list(dict.fromkeys(files))[:MAX_ITEMS]})
        except ToolError as exc:
            raise _RpcError(INVALID_PARAMS, str(exc)) from exc
        text = ("I plan to create or edit these files:\n" + "\n".join(f"- {f}" for f in files[:MAX_ITEMS])
                + "\n\nrepoviz says:\n" + summary + "\n\n```json\n" + json.dumps(fit(data), indent=1)
                + "\n```\n\nBefore editing: do not touch protected files; ask before editing files that are out "
                "of scope; follow the layer rules of each file (call where_does_this_go for a new file). "
                "Then go ahead with the plan.")
        return {"description": PROMPTS[name]["description"], "messages": [_user(text)]}

    # -- shared lookups ----------------------------------------------------------------------------------------

    def index(self) -> _Index:
        snap = self.repo.snapshot("WORKTREE")
        key = (snap.kind, snap.revision_id, self.repo.config.fingerprint())
        if self._index is None or self._index[0] != key:
            self._index = (key, _Index(snap))
        return self._index[1]

    def rel_path(self, text: str) -> str:
        """A repository-relative path from ``text`` (absolute paths inside the repository are accepted)."""
        t = text.strip()
        if t.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", t):
            try:
                t = Path(t).resolve().relative_to(Path(self.repo.root).resolve()).as_posix()
            except (ValueError, OSError):
                raise ToolError(f"refused: {text!r} is outside the repository") from None
        t = t.rstrip("/") if t not in ("", "/") else t
        if t.startswith("./"):
            t = t[2:]
        if t in ("", "."):
            return ""
        try:
            return checked_path(t)
        except ValueError:
            raise ToolError(f"refused: {text!r} is not a path inside the repository") from None

    def resolve(self, idx: _Index, text: str) -> Any:
        t = text.strip()
        exact = idx.by_name.get(t)
        if exact:
            return sorted(exact, key=lambda n: ({"module": 0, "component": 1}.get(n.category, 2), n.id))[0]
        looks_like_path = "/" in t or "\\" in t or FILE_LIKE.search(t) is not None or t.startswith(".")
        if looks_like_path:
            p = self.rel_path(t)
            n = idx.by_path.get(p) or idx.dir_node(p)
            if n is not None:
                return n
            if "/" in t or t.startswith("."):
                raise ToolError(f"no file or directory {p!r} in the analyzed tree (excluded, ignored or new?)")
        suffix = [n for name, ns in idx.by_name.items() if name.endswith("." + t) or name.endswith(":" + t)
                  for n in ns]
        if len(suffix) == 1:
            return suffix[0]
        if suffix:
            names = sorted({n.qualified_name for n in suffix})
            raise ToolError(f"{t!r} is ambiguous ({len(names)} matches): " + ", ".join(names[:8])
                            + (" …" if len(names) > 8 else "") + "; pass the qualified name")
        raise ToolError(f"nothing named {t!r}; pass a repository-relative path or a qualified name such as "
                        "`package.module.function`")

    def scope(self) -> tuple[Any, Any]:
        from .review import ReviewTarget, scope_for

        session = self.repo.current_session()
        target = ReviewTarget("session", "", "SESSION", "WORKTREE", "session", "", session.id if session else None)
        return scope_for(self.repo, target), session

    def contract_rules(self, qname: str | None, path: str | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """The contracts that constrain a module (``qname``/``path``), as plain rules; and its layer, if any."""
        from .contracts import _join, contracts_of, matches, unit_of

        rules: list[dict[str, Any]] = []
        layer: dict[str, Any] | None = None
        q = qname or ""

        def hit(patterns: list[str]) -> str | None:
            return next((p for p in patterns if matches(q, path, p)), None)

        for c in contracts_of(self.repo.config):
            base = {"contract": c.name, "type": c.type}
            if c.type == "layers":
                stacks = [[_join(ctr, layer_) for layer_ in c.layers] for ctr in c.containers] or [list(c.layers)]
                for stack in stacks:
                    i = next((i for i, p in enumerate(stack) if matches(q, path, p)), None)
                    if i is None:
                        continue
                    info = {**base, "layer": stack[i], "may_import": stack[i + 1:], "must_not_import": stack[:i]}
                    layer = layer or info
                    rules.append({**base, "rule": f"layer {stack[i]!r}: may import "
                                  + (", ".join(stack[i + 1:]) or "no lower layer")
                                  + (f"; must not import {', '.join(stack[:i])}" if i else "")})
                    break
            elif c.type in ("independence", "acyclic"):
                unit = next((u for p in c.modules if (u := unit_of(q, path, p))), None)
                if unit:
                    rules.append({**base, "unit": unit, "rule": (
                        f"{unit} must not import the other independent modules ({', '.join(c.modules)})"
                        if c.type == "independence" else f"no import cycle between the parts of {', '.join(c.modules)}")})
            elif c.type in ("forbidden", "required"):
                if hit(c.source):
                    verb = "must not import" if c.type == "forbidden" else "must import"
                    rules.append({**base, "rule": f"{verb} {', '.join(c.target)}"})
                elif c.type == "forbidden" and hit(c.target):
                    rules.append({**base, "rule": f"must not be imported by {', '.join(c.source)}"})
            elif c.type == "public-interface" and c.module and matches(q, path, c.module):
                rules.append({**base, "rule": f"other code may import only {', '.join(c.public) or c.module} "
                                              f"from {c.module}"})
            if rules and c.message and rules[-1]["contract"] == c.name:
                rules[-1]["why"] = c.message
        return rules, layer

    # -- tools -------------------------------------------------------------------------------------------------

    def architecture_overview(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        idx = self.index()
        snap, k = idx.snap, args["max_items"]
        prof = snap.profile or {}
        comp_of_module = {m.id: idx.component(m) for m in snap.modules}
        comps = sorted({n.id: n for n in [*(n for n in snap.components if "component" in n.tags),
                                          *(c for c in comp_of_module.values() if c is not None)]}.values(),
                       key=lambda n: n.qualified_name)
        counts: dict[str, int] = {}
        for m, c in comp_of_module.items():
            if c is not None:
                counts[c.id] = counts.get(c.id, 0) + 1
        links: dict[tuple[str, str], int] = {}
        for (s, t), _e in idx.import_edge.items():
            cs, ct = comp_of_module.get(s), comp_of_module.get(t)
            if cs is not None and ct is not None and cs.id != ct.id:
                links[(cs.qualified_name, ct.qualified_name)] = links.get((cs.qualified_name, ct.qualified_name), 0) + 1
        external: dict[str, int] = {}
        for n in snap.nodes():
            if "external" in n.tags and "stdlib" not in n.tags:
                external[n.qualified_name] = len({u for u in idx.users.get(n.id, {})})
        contracts = self._contracts_report()
        data: dict[str, Any] = {
            "repository": {"name": prof.get("name") or self.repo.name, "branch": prof.get("branch"),
                           "head": (prof.get("head") or "")[:10] or None, "files": prof.get("file_count"),
                           "languages": [{"language": lang.get("display") or lang.get("language"),
                                          "files": lang.get("files")}
                                         for lang in prof.get("languages", []) if lang.get("kind") == "programming"][:k]},
            "projects": [{"name": p.get("name"), "path": p.get("path") or ".", "ecosystem": p.get("ecosystem"),
                          "role": p.get("role")} for p in prof.get("projects", [])][:k],
            "components": [{"name": c.qualified_name, "path": c.path or ".", "kind": c.component_type,
                            "modules": counts.get(c.id, 0)} for c in comps][:k],
            "component_imports": [{"from": a, "to": b, "imports": n} for (a, b), n in
                                  sorted(links.items(), key=lambda kv: (-kv[1], kv[0]))][:k],
            "entry_points": [{"name": e.get("name"), "kind": e.get("kind"), "target": e.get("target")}
                             for e in prof.get("entry_points", [])][:k],
            "external_packages": [{"name": n, "used_by": u} for n, u in
                                  sorted(external.items(), key=lambda kv: (-kv[1], kv[0]))][:k],
            "import_cycles": len([c for c in snap.cycles if c.level == "module"]),
            "contracts": contracts["contracts"] if contracts else [],
            "counts": {"components": len(comps), "modules": len(snap.modules), "symbols": len(snap.symbols)},
        }
        if contracts is None:
            data["contracts_note"] = "no contracts configured ([[contracts]] in .repoviz.toml)"
        failing = sum(1 for c in data["contracts"] if c["status"] == "fail")
        summary = (f"{data['repository']['name']}: {len(comps)} components, {len(snap.modules)} modules"
                   + (f", languages {', '.join(str(lang['language']) for lang in data['repository']['languages'][:3])}"
                      if data["repository"]["languages"] else "")
                   + (f"; {len(data['contracts'])} contracts, {failing} failing" if data["contracts"] else "")
                   + (f"; {data['import_cycles']} import cycles" if data["import_cycles"] else "") + ".")
        return summary, data

    def where_does_this_go(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        from .review import _first_match, _sensitive_kind

        idx = self.index()
        text = args["target"]
        node, path, new = None, None, False
        try:
            node = self.resolve(idx, text)
            path = node.path
        except ToolError:
            if not ("/" in text or classify.language_of(text.strip())[0]):  # a name, not a new file
                raise
            path, new = self.rel_path(text), True  # a file that does not exist yet
        module = idx.module_of(node) if node is not None else None
        qname = module.qualified_name if module is not None else None
        anchor = node
        if new:
            anchor = idx.nearest_dir(path)
            if anchor is not None and path.endswith(".py") and anchor.component_type == "package":
                qname = f"{anchor.qualified_name}.{posixpath.splitext(posixpath.basename(path))[0]}"
        comp = idx.component(anchor)
        rules, layer = self.contract_rules(qname or (node.qualified_name if node is not None else None), path)
        policy, session = self.scope()
        verdict = policy.classify(path) if path else "unscoped"
        project = None
        for p in (idx.snap.profile or {}).get("projects", []):
            root = p.get("path") or ""
            if path is not None and (not root or path == root or path.startswith(root + "/")):
                if project is None or len(root) > len(project.get("path") or ""):
                    project = p
        data: dict[str, Any] = {
            "target": text, "exists": not new,
            "node": describe(node) if node is not None else None,
            "module": qname, "path": path,
            "component": comp.qualified_name if comp is not None else None,
            "project": ({"name": project.get("name"), "path": project.get("path") or ".",
                         "ecosystem": project.get("ecosystem")} if project else None),
            "layer": layer, "contracts": rules,
            "scope": {"verdict": verdict, "rule": _first_match(path, policy.protected if verdict == "protected"
                                                               else policy.allowed) if path and verdict in
                      ("protected", "allowed") else None,
                      "session": (session.label or session.id) if session else None,
                      "sensitive": _sensitive_kind(path) if path else None},
            "is_test": bool(node is not None and "test" in node.tags),
            "is_entry_point": bool(node is not None and "entry-point" in node.tags and "test" not in node.tags),
        }
        if new:
            data["note"] = "new file: placed by its directory" + (f" ({anchor.path or '.'})" if anchor else "")
        parts = [f"{path or text}" + (" (new file)" if new else ""),
                 f"component {data['component']}" if data["component"] else "no component",
                 f"layer {layer['layer']!r}" if layer else None,
                 {"protected": "PROTECTED: do not edit", "out-of-scope": "out of scope: ask before editing",
                  "allowed": "in scope", "unscoped": None}[verdict],
                 f"{len(rules)} contract rule(s)" if rules else None]
        return "; ".join(p for p in parts if p) + ".", data

    def impact(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        idx = self.index()
        node = self.resolve(idx, args["target"])
        depth, k = args["depth"], args["max_items"]
        starts = [node.id]
        if node.category != CATEGORY_SYMBOL:  # a file or a directory: its modules and their symbols
            mods = idx.modules_under(node) if node.category != CATEGORY_MODULE else [node.id]
            starts = list(dict.fromkeys(mods + [c for m in mods for c in idx.children.get(m, ())
                                                if idx.nodes[c].category == CATEGORY_SYMBOL]))
        inside = set(starts)
        dist: dict[str, int] = {s: 0 for s in starts}
        via: dict[str, Any] = {}
        frontier = sorted(starts)
        for d in range(1, depth + 1):
            nxt = []
            for cur in frontier:
                for user, edge in sorted(idx.users.get(cur, {}).items()):
                    if user in dist or len(dist) >= MAX_IMPACT_NODES:
                        continue
                    dist[user], via[user] = d, edge
                    nxt.append(user)
            frontier = nxt
        # a symbol's module is imported by code that may use the symbol without a resolved call
        module_importers: list[str] = []
        if node.category == CATEGORY_SYMBOL:
            m = idx.module_of(node)
            if m is not None:
                module_importers = sorted(u for u, e in idx.users.get(m.id, {}).items()
                                          if e.relationship == REL_IMPORTS and u not in dist)
        reached = sorted((n for n in dist if n not in inside and n in idx.nodes),
                         key=lambda n: (dist[n], idx.nodes[n].qualified_name))

        def item(nid: str) -> dict[str, Any]:
            n, e = idx.nodes[nid], via.get(nid)
            out = {**describe(n), "distance": dist[nid]}
            if e is not None:
                out["how"] = {REL_IMPORTS: "imports", REL_CALLS: "calls", REL_INVOKES: "runs"}[e.relationship]
                out.update(_evidence(e))
            return out

        users = [n for n in reached if "test" not in idx.nodes[n].tags]
        callers = [n for n in users if idx.nodes[n].category == CATEGORY_SYMBOL]
        importers = [n for n in users if idx.nodes[n].category != CATEGORY_SYMBOL]
        entry = [n for n in users if "entry-point" in idx.nodes[n].tags]
        tests: dict[str, int] = {}
        for n in reached + module_importers:
            t = idx.nodes[n]
            if "test" in t.tags and t.path:
                tests.setdefault(t.path, dist.get(n, 1))
        data = {
            "target": describe(node), "depth": depth,
            "totals": {"callers": len(callers), "importers": len(importers), "entry_points": len(entry),
                       "tests": len(tests)},
            "callers": [item(n) for n in callers[:k]],
            "importers": [item(n) for n in importers[:k]],
            "importers_of_its_module": [describe(idx.nodes[n]) for n in module_importers
                                        if "test" not in idx.nodes[n].tags][:k],
            "entry_points_reached": [item(n) for n in entry[:k]],
            "tests_affected": [p for p, _ in sorted(tests.items(), key=lambda kv: (kv[1], kv[0]))][:k],
        }
        if len(dist) >= MAX_IMPACT_NODES:
            data["capped"] = f"stopped after {MAX_IMPACT_NODES} nodes"
        t = data["totals"]
        summary = (f"{node.qualified_name}: {t['callers']} caller(s) and {t['importers']} importer(s) within "
                   f"{depth} step(s), {t['entry_points']} entry point(s) reached, {t['tests']} test file(s) affected.")
        if not callers and not importers and not module_importers:
            summary += " Nothing in the analyzed code uses it (dynamic uses, such as getattr or string imports, " \
                       "are not seen)."
        return summary, data

    def dependency_path(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        idx = self.index()
        a, b = self.resolve(idx, args["source"]), self.resolve(idx, args["target"])
        starts, goals = idx.modules_under(a), idx.modules_under(b)
        if not starts or not goals:
            raise ToolError("both ends must be (or contain) modules of the analyzed code")
        goals = [g for g in goals if g not in set(starts)] or goals

        def chains(src: list[str], dst: list[str]) -> list[list[dict[str, Any]]]:
            out = []
            for path in shortest_paths(src, dst, idx.imports, max_paths=args["max_paths"]):
                steps = [{"module": idx.nodes[path[0]].qualified_name}]
                for s, t in zip(path, path[1:]):
                    steps.append({"module": idx.nodes[t].qualified_name, **_evidence(idx.import_edge.get((s, t)))})
                out.append(steps)
            return out

        found = chains(starts, goals)
        data: dict[str, Any] = {"source": describe(a), "target": describe(b), "paths": found}
        if found:
            hops = len(found[0]) - 1
            summary = (f"{a.qualified_name} depends on {b.qualified_name}: {len(found)} shortest chain(s) of "
                       f"{hops} import(s)" + (" (direct)" if hops == 1 else "") + ".")
        else:
            back = chains(goals, starts)
            data["reverse_paths"] = back
            summary = (f"{a.qualified_name} does not import {b.qualified_name}, directly or indirectly"
                       + (f"; but {b.qualified_name} depends on {a.qualified_name} (see reverse_paths)." if back
                          else "."))
        return summary, data

    def check_scope(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        from .review import _first_match, _sensitive_kind

        idx = self.index()
        policy, session = self.scope()
        rows, counts = [], {"allowed": 0, "protected": 0, "out-of-scope": 0, "unscoped": 0}
        for raw in args["paths"]:
            path = self.rel_path(raw)
            verdict = policy.classify(path)
            counts[verdict] += 1
            node = idx.by_path.get(path) or idx.dir_node(path)
            comp = idx.component(node if node is not None else idx.nearest_dir(path))
            row: dict[str, Any] = {"path": path, "verdict": verdict, "exists": node is not None}
            if verdict in ("protected", "allowed"):
                row["rule"] = _first_match(path, policy.protected if verdict == "protected" else policy.allowed)
            if comp is not None:
                row["component"] = comp.qualified_name
            sensitive = _sensitive_kind(path)
            if sensitive:
                row["sensitive"] = sensitive
            rows.append(row)
        data = {"session": (session.label or session.id) if session else None,
                "scope": {"allowed": policy.allowed, "protected": policy.protected, "from": policy.origin},
                "files": rows, "counts": counts}
        if not policy.allowed and not policy.protected:
            summary = ("No scope is set (no active session scope, no [review] allowed/protected in the "
                       "configuration): every path is unscoped.")
        else:
            bits = [f"{n} {v}" for v, n in counts.items() if n]
            summary = ", ".join(bits) + "."
            bad = [r["path"] for r in rows if r["verdict"] == "protected"]
            if bad:
                summary += " Do not edit: " + ", ".join(bad[:5]) + ("…" if len(bad) > 5 else "") + "."
            out = [r["path"] for r in rows if r["verdict"] == "out-of-scope"]
            if out:
                summary += " Ask before editing: " + ", ".join(out[:5]) + ("…" if len(out) > 5 else "") + "."
        sens = [r["path"] for r in rows if r.get("sensitive")]
        if sens:
            summary += " Sensitive: " + ", ".join(sens[:5]) + "."
        return summary, data

    def review_current(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        from .repo import RepositoryError
        from .review import SEVERITY_ORDER, build_review, resolve_target

        try:
            target = resolve_target(self.repo, args.get("target"))
            report = build_review(self.repo, target)
        except (ValueError, RepositoryError, RuntimeError) as exc:
            raise ToolError(str(exc)) from exc
        k, floor = args["max_items"], SEVERITY_ORDER[args["min_severity"]]
        findings = sorted((f for f in report["findings"] if SEVERITY_ORDER.get(f["severity"], 9) <= floor),
                          key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f.get("path") or "",
                                         f.get("line") or 0, f["id"]))
        keep = ("id", "kind", "severity", "title", "detail", "path", "line", "excerpt", "suggestion")
        files = sorted(report["files"], key=lambda f: (-(f.get("risk") or {}).get("score", 0), f["path"]))
        s, risk = report["summary"], report.get("risk") or {}
        by_sev = {sev: sum(1 for f in report["findings"] if f["severity"] == sev) for sev in SEVERITIES}
        data = {
            "target": {"label": report["target"].get("label"), "base": report.get("base"), "head": report.get("head")},
            "risk": {key: risk.get(key) for key in ("level", "score", "path", "summary")},
            "signals": by_sev,
            "findings": [{key: (redact(str(f[key])) if key in ("excerpt", "detail") else f[key])
                          for key in keep if f.get(key) not in (None, "")} for f in findings[:k]],
            "files": [{"path": f["path"], "status": f["status"], "scope": f.get("scope"),
                       "risk": (f.get("risk") or {}).get("level"), "lines": f"+{f.get('lines_added') or 0} "
                       f"-{f.get('lines_removed') or 0}"} for f in files[:k]],
            "totals": {"files": len(report["files"]), "findings_shown": min(k, len(findings)),
                       "findings_at_or_above": len(findings)},
        }
        summary = (f"{report['target'].get('label')}: {s.get('files', len(report['files']))} file(s), risk "
                   f"{risk.get('level', 'low')}; {by_sev['high']} high, {by_sev['medium']} medium, "
                   f"{by_sev['low']} low signal(s).")
        if by_sev["high"]:
            summary += " Fix the high signals before handing over."
        return summary, data

    def _contracts_report(self) -> dict[str, Any] | None:
        from .contracts import check, contracts_of, load_baseline, report

        contracts = contracts_of(self.repo.config)
        if not contracts:
            return None
        snap = self.index().snap
        path = self.repo.config.contracts_baseline
        text = self.repo.open_source("WORKTREE").read_text(path) if path else None
        known, problem = load_baseline(text)
        return report(check(snap, contracts), known, problem, path)

    def contracts_check(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        from .contracts import suggest_layers

        rep = self._contracts_report()
        if rep is None:
            data = {"contracts": [], "suggested_contract": suggest_layers(self.index().snap)}
            return ("No contracts configured. suggested_contract is a layers contract inferred from the current "
                    "imports, to add to .repoviz.toml if it matches the intended architecture."), data
        k = args["max_items"]
        new = [v for v in rep["violations"] if not v["known"]]
        keep = ("contract", "type", "severity", "source", "target", "detail", "path", "line", "excerpt", "chain")
        data = {"contracts": rep["contracts"],
                "new_violations": [{key: (redact(v[key]) if key == "excerpt" and v[key] else v[key])
                                    for key in keep if v.get(key) not in (None, "", [])} for v in new[:k]],
                "known_violations": sum(1 for v in rep["violations"] if v["known"]),
                "fixed": rep["fixed"][:k], "baseline": rep["baseline"]}
        failing = [c["name"] for c in rep["contracts"] if c["status"] == "fail"]
        summary = (f"{len(rep['contracts'])} contract(s): "
                   + (f"{len(new)} new violation(s) in {', '.join(failing)}." if new else "no new violation.")
                   + (f" {data['known_violations']} known violation(s) accepted in the baseline."
                      if data["known_violations"] else "")
                   + (f" Baseline problem: {rep['baseline']['problem']}." if rep["baseline"].get("problem") else ""))
        return summary, data

    def set_scope(self, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        session = self.repo.current_session()
        if session is None:
            raise ToolError("no active session; start one with `repoviz session start`")
        if "allowed" not in args and "protected" not in args:
            raise ToolError("pass allowed, protected or both")
        s = self.repo.state.update_scope(session, args.get("allowed"), args.get("protected"))
        return (f"Scope of session {s.label or s.id} updated.",
                {"session": s.label or s.id, "allowed": s.allowed, "protected": s.protected})


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": {"type": "text", "text": text}}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
