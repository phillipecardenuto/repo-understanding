# repoviz for agents: the MCP server

`repoviz mcp` is a read-only [Model Context Protocol](https://modelcontextprotocol.io)
server. The rest of repoviz helps a person supervise a coding agent. This server
lets the agent ask the same questions itself, before it edits:

- where does a new file belong, and what may its layer import?
- what depends on this function, and which tests cover it?
- is this path in scope for the current work session?

Before it hands over, the agent can also check its own work: the review signals
and the contract violations.

## Register it

With Claude Code, run this from the repository:

```bash
claude mcp add repoviz -- repoviz mcp -C .
```

Other clients (Cursor, Codex CLI, and so on) take the same command in their MCP
configuration, for example:

```json
{
  "mcpServers": {
    "repoviz": {"command": "repoviz", "args": ["mcp", "-C", "/path/to/repository"]}
  }
}
```

The server speaks the MCP stdio transport: JSON-RPC 2.0, one message per line.
It supports protocol revisions `2025-11-25`, `2025-06-18`, `2025-03-26` and
`2024-11-05`. It answers `initialize`, `ping`, `tools/list`, `tools/call`,
`prompts/list` and `prompts/get`. Logs go to stderr.

## Tools

Every tool returns a one-line summary followed by JSON. From protocol
`2025-06-18` on, the same JSON is also in `structuredContent`. Lists are capped
by `max_items` (1–200, default 20), and each answer by about 4,000 tokens (16,000
characters). When a cap applies, the answer gives the totals and says what was
left out in `truncated`. Answers are deterministic.

| Tool | Arguments | Answers |
|---|---|---|
| `architecture_overview` | `max_items` | Languages, projects, components and the imports between them, entry points, the most used external packages, import cycles, the contracts and whether they pass. |
| `where_does_this_go` | `target` | For a file, directory or symbol, or a file that does not exist yet: component, project, layer and what it may and may not import, the contract rules that apply (with their `message`), the scope verdict and the rule behind it, sensitive areas, and whether it is a test or an entry point. |
| `impact` | `target`, `depth` (1–6, default 2), `max_items` | Callers and importers up to `depth` steps away, nearest first. Each comes with `how` (calls, imports, runs), the call or import site and the code. Also the entry points reached, the tests affected, and the modules that import the target's module. |
| `dependency_path` | `source`, `target`, `max_paths` (1–5, default 3) | The shortest import chains from `source` to `target` (call chains between two functions), each step with its file, line and code. When there is none, it gives the chains in the other direction, if any. The same answer as `repoviz why`. |
| `check_scope` | `paths` (1–200) | For each planned file: `allowed`, `protected` (do not edit), `out-of-scope` (ask first) or `unscoped`, with the matching glob, the component and sensitive areas (migrations, CI, deployment). The scope comes from the active session and `[review]`. |
| `review_current` | `target`, `min_severity` (default `medium`), `max_items` | The review of the current work: the active session, else the uncommitted changes, or any target of `repoviz review` (`branch`, `main...HEAD`…). It gives the risk, the signals by severity, the findings and the riskiest files. |
| `contracts_check` | `max_items` | Each contract's status, and the violations that are not in the known-violations baseline, with file, line and import chain. Without contracts, it gives a suggested layers contract. |

`target` is a repository-relative path, a qualified name (`app.services.billing.charge`)
or a unique name (`charge`, `billing.charge`). An ambiguous name lists its
matches. Absolute paths are accepted when they are inside the repository.

## Prompts

- `self_review` (optional `target`): asks the agent to call `review_current`, fix
  the high signals, justify or fix the medium ones, call `contracts_check`, check
  the `impact` of changed signatures, and summarise.
- `plan_check` (`files`, one per line or comma-separated): runs `check_scope` on
  the planned files and embeds the result, with instructions: do not touch
  protected files, ask before editing out-of-scope files, follow the layer rules.

## Safety

- **Read-only.** No tool writes to the repository or to the state directory. The
  only exception is `set_scope`, which replaces the active session's allowed and
  protected globs. It exists only when the server is started with
  `repoviz mcp --allow-writes`, and even then it writes only to the state
  directory.
- **Repository code is never run.** The server reads files and Git objects, as
  the rest of repoviz does.
- **Paths stay inside the repository.** `..`, `.git/…` and absolute paths
  outside the repository are refused.
- **Excerpts are redacted**, as everywhere else.

### Errors

- Protocol errors are JSON-RPC errors: an unknown method is `-32601`, an unknown
  tool or malformed params `-32602`, a line that is not JSON `-32700`, and a
  batch `-32600`.
- Problems the agent can fix are tool results with `isError: true`, so the
  model sees them: an unknown name, a path outside the repository, a wrong
  argument.

## Freshness and speed

- Each call analyzes the working tree as it is now, so the agent's own edits
  count.
- The analysis is cached, keyed by the working tree's contents. On a
  Django-sized repository the first call takes as long as a cold snapshot
  (about 10 s). After that, `impact`, `where_does_this_go`, `dependency_path` and
  `check_scope` answer in about 0.2 s.
- `review_current` takes about 1 s warm for a 30-file wave.
- The configuration (`.repoviz.toml`) is read when the server starts; restart
  the server after changing it.

## Sample transcript

The agent plans to add `app/services/refunds.py` in a repository with a layers
contract (`app.routes` → `app.services` → `app.models`) and a protected
`app/models/**`. Each line is one message; the responses are shortened.

```text
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"claude-code","version":"2"}}}
← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{"listChanged":false},"prompts":{"listChanged":false}},"serverInfo":{"name":"repoviz","version":"0.1.0"},"instructions":"repoviz knows this repository's architecture…"}}
→ {"jsonrpc":"2.0","method":"notifications/initialized"}
→ {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"where_does_this_go","arguments":{"target":"app/services/refunds.py"}}}
```

The result text of call 2:

```text
app/services/refunds.py (new file); component app; layer 'app.services'; 1 contract rule(s).

{
 "exists": false,
 "module": "app.services.refunds",
 "component": "app",
 "layer": {"contract": "Layers", "layer": "app.services", "may_import": ["app.models"], "must_not_import": ["app.routes"]},
 "contracts": [{"contract": "Layers", "type": "layers",
                "rule": "layer 'app.services': may import app.models; must not import app.routes",
                "why": "Keep the API thin."}],
 "scope": {"verdict": "unscoped", "rule": null, "session": null, "sensitive": null},
 "note": "new file: placed by its directory (app/services)"
}
```

Then the agent checks the files it plans to edit (call 3, `check_scope`):

```text
1 protected, 1 unscoped. Do not edit: app/models/user.py.
```

And it asks what depends on the function it wants to change (call 4, `impact`
with `"target": "charge"`):

```text
app.services.billing.charge: 1 caller(s) and 0 importer(s) within 2 step(s), 0 entry point(s) reached, 1 test file(s) affected.

{
 "callers": [{"name": "app.routes.api.post", "kind": "function", "at": "app/routes/api.py:4", "distance": 1,
              "how": "calls", "evidence": "app/routes/api.py:5", "code": "    return charge(order)"}],
 "tests_affected": ["tests/test_billing.py"],
 …
}
```
