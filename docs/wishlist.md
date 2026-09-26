# Feature wishlist

What to build next. This list comes from two sources:

- a survey, in September 2026, of about 25 open-source tools that visualize
  codebases, check architecture rules or help people review AI coding agents;
- the evaluation on a large multi-service system
  ([evaluation-elies.md](evaluation-elies.md)).

Every item is a GitHub issue labelled `wishlist`, and tracking issue
[#1](https://github.com/phillipecardenuto/repo-understanding/issues/1) lists
them all. Each issue stands on its own: why it matters, which tools inspired it,
where the code is today, the proposal, acceptance criteria and tests.

**Before implementing an item**, read [AGENTS.md](../AGENTS.md). It covers the
invariants every change must keep:
- read-only;
- never execute repository code;
- standard library only;
- offline, CSP-locked UI;
- never colour alone;
- static report parity.

It also covers how to test and when an issue counts as done.

Labels:
- `priority: P1` would noticeably change how useful repoviz is, `P2` is a clear
  improvement, and `P3` is polish or niche.
- `area: …` names the part of the code the item touches.

## The list

**Status:** 9 items done:
- #2, #3, #4, #5 and #6, which complete the core agent review group;
- #16, which starts the guardrails group;
- three added later from user feedback: #35, and #36 and #37, written by the
  maintainer.

#19 is not planned. The tracking issue
[#1](https://github.com/phillipecardenuto/repo-understanding/issues/1) is always up to date.

### Supervising AI agents

| # | Item | Priority | Size |
|---|---|---|---|
| [#2](https://github.com/phillipecardenuto/repo-understanding/issues/2) | Review a wave commit by commit (commits panel and per-commit filter) · **✓ done** | P1 | M |
| [#3](https://github.com/phillipecardenuto/repo-understanding/issues/3) | Detect renamed and moved symbols, files and submodules · **✓ done** | P1 | L |
| [#4](https://github.com/phillipecardenuto/repo-understanding/issues/4) | Signal new code that is not wired in · **✓ done** | P1 | M |
| [#5](https://github.com/phillipecardenuto/repo-understanding/issues/5) | Explainable risk score per file and per wave to order the review · **✓ done** | P1 | M |
| [#6](https://github.com/phillipecardenuto/repo-understanding/issues/6) | Change coupling from Git history, and a missed-companion signal · **✓ done** | P1 | M |
| [#7](https://github.com/phillipecardenuto/repo-understanding/issues/7) | Checkpoints and an edit timeline inside a wave (agent-hook recipe) · **✓ done** | P2 | M |
| [#8](https://github.com/phillipecardenuto/repo-understanding/issues/8) | Expected changes: compare the agent's plan with what it changed · **✓ done** | P2 | M |
| [#9](https://github.com/phillipecardenuto/repo-understanding/issues/9) | Blocking review, structured verdict and a review-complete gate | P2 | M |
| [#10](https://github.com/phillipecardenuto/repo-understanding/issues/10) | Parallel agents: Git worktrees and overlap between concurrent waves | P2 | L |
| [#11](https://github.com/phillipecardenuto/repo-understanding/issues/11) | Third-party dependency changes (manifests, lock files) with supply-chain signals · **✓ done** | P2 | M |
| [#12](https://github.com/phillipecardenuto/repo-understanding/issues/12) | Read existing coverage reports to flag changed lines no test runs | P2 | M |
| [#13](https://github.com/phillipecardenuto/repo-understanding/issues/13) | Cross-service contract changes: HTTP routes, background tasks, env vars | P2 | L |
| [#14](https://github.com/phillipecardenuto/repo-understanding/issues/14) | Constant and configuration value changes, before → after · **✓ done** | P2 | S |
| [#15](https://github.com/phillipecardenuto/repo-understanding/issues/15) | Submodule edge cases: fetch hint for missing history, nested submodules | P3 | S |
| [#20](https://github.com/phillipecardenuto/repo-understanding/issues/20) | Standing guidance on components, exported as agent context | P3 | S |
| [#35](https://github.com/phillipecardenuto/repo-understanding/issues/35) | Review any branch against any other branch (since they diverged, or exact) · **✓ done** | P1 | M |

### Architecture rules and integrations

| # | Item | Priority | Size |
|---|---|---|---|
| [#16](https://github.com/phillipecardenuto/repo-understanding/issues/16) | Architecture contracts (layers, independence, public interfaces, acyclic) with a known-violations baseline · **✓ done** | P1 | L |
| [#17](https://github.com/phillipecardenuto/repo-understanding/issues/17) | CI: SARIF, GitHub annotations, a PR comment with a Mermaid diagram, a reusable Action · **✓ done** | P2 | M |
| [#18](https://github.com/phillipecardenuto/repo-understanding/issues/18) | Read-only MCP server: agents ask about architecture, impact and scope before editing · **✓ done** | P2 | M |
| [#19](https://github.com/phillipecardenuto/repo-understanding/issues/19) | Export architecture docs and other diagram formats (C4/PlantUML, Structurizr, draw.io) · *not planned* | P3 | M |

### Project and system awareness

| # | Item | Priority | Size |
|---|---|---|---|
| [#21](https://github.com/phillipecardenuto/repo-understanding/issues/21) | Analyze checked-out submodules as nested sub-projects · **✓ done** | P1 | L |
| [#22](https://github.com/phillipecardenuto/repo-understanding/issues/22) | System view from docker-compose: services linked to code, dev/prod merged, infrastructure separated · **✓ done** | P1 | L |
| [#23](https://github.com/phillipecardenuto/repo-understanding/issues/23) | Runtime coupling edges: container images and service URLs in code · **✓ done** | P1 | M |
| [#25](https://github.com/phillipecardenuto/repo-understanding/issues/25) | Python: imports through `sys.path` edits; requirements.txt-only apps as projects · **✓ done** | P2 | S |
| [#26](https://github.com/phillipecardenuto/repo-understanding/issues/26) | Import-level analyzers for Java/Kotlin, C#, Rust, Ruby, PHP, C/C++ | P3 | L |

### User interface

| # | Item | Priority | Size |
|---|---|---|---|
| [#27](https://github.com/phillipecardenuto/repo-understanding/issues/27) | Diagram readability at scale: orientation, minimum zoom, folded leaf lists, faint old cycles · **✓ done** | P1 | M |
| [#24](https://github.com/phillipecardenuto/repo-understanding/issues/24) | Smarter Dependencies-tab defaults and honest header counts · **✓ done** | P2 | S |
| [#28](https://github.com/phillipecardenuto/repo-understanding/issues/28) | "Why does A depend on B?" path finder and blast radius on click · **✓ done** | P2 | M |
| [#29](https://github.com/phillipecardenuto/repo-understanding/issues/29) | Lists that scale: grouping, relevance sort, filter and search · **✓ done** | P2 | M |
| [#30](https://github.com/phillipecardenuto/repo-understanding/issues/30) | Health metrics and hotspot overlays (complexity × churn, fan-in/out, ownership) | P2 | M |
| [#31](https://github.com/phillipecardenuto/repo-understanding/issues/31) | History-based comparisons for clean checkouts on the Changes tab · **✓ done** | P2 | S |
| [#32](https://github.com/phillipecardenuto/repo-understanding/issues/32) | Architecture over time: a drift timeline across tags, releases or waves | P3 | M |
| [#33](https://github.com/phillipecardenuto/repo-understanding/issues/33) | Moved-code detection and word-level highlights in diffs | P3 | M |
| [#36](https://github.com/phillipecardenuto/repo-understanding/issues/36) | Inspect the code changes of churn hotspots in the Structure tab (maintainer's issue) · **✓ done** | P1 | M |
| [#37](https://github.com/phillipecardenuto/repo-understanding/issues/37) | Spotlight a module and its direct links on click in Dependencies (maintainer's issue) · **✓ done** | P1 | S |

### Performance

| # | Item | Priority | Size |
|---|---|---|---|
| [#34](https://github.com/phillipecardenuto/repo-understanding/issues/34) | Persistent on-disk parse cache (SQLite, JSON payloads) · **✓ closed as is**: a second process is 1.3–3× faster; the 5× Django target would need incremental graph building, not planned | P2 | M |

## Suggested order

1. **Core agent review:**
   - #2 (commits), done;
   - #4 (unwired code), done;
   - #3 (renames), done;
   - #5 (risk), done;
   - #6 (co-change), done.

   These change the most for someone reviewing agent waves every day, and they
   have no prerequisites.
2. **Guardrails:** #16 (contracts), #17 (CI) and #18 (MCP), done.
3. **Multi-service systems:** #22 (compose system view), #21 (submodules as
   sub-projects), #23 (runtime edges) and #24 (Dependencies defaults, header
   counts), all done.
4. **UI at scale:** #27 (diagram readability), #29 (lists), #28 (why and blast
   radius) and #31 (history comparisons), all done.
5. **Anytime:** #34 (parse cache; closed as is), #14 (values before →
   after), #25 (`sys.path` imports, inferred projects) and #11 (dependency
   changes), done.
6. **Next:** the other P2 items in number order: #7 (checkpoints) and #8
   (plan vs actual), done; then #9, #10, #12, #13 and #30, then the P3 items.

## Dependencies between items

| Item | Builds on | Relationship |
|---|---|---|
| #23 | #22 | Needs its service model (images built from submodules, service names). |
| #24 | #22 | Uses the System view it adds. |
| #17 | #16, #5 | Includes contract violations and the risk score. |
| #18 | #16, #28 | Exposes contracts and dependency paths. |
| #10 | #5, #9 | Shows risk and verdicts per worktree. |
| #32 | #34 | Relies on the cache. |
| #30 | #5 | Feeds the risk score. |
| #33 | #3 | Shares symbol-move detection. |
| #20 | #18 | Exported through the MCP server. |

## What similar tools do, and what we took from them

| Tool | What it is | Ideas taken |
|---|---|---|
| [tirth8205/code-review-graph](https://github.com/tirth8205/code-review-graph) | Local code graph + MCP server to cut AI review context | Risk-scored changes (#5), MCP tools and prompts (#18), incremental re-parse (#34), sticky PR comment (#17) |
| [sverklo/sverklo](https://github.com/sverklo/sverklo) | Repository memory for coding agents (MCP) | Risk = importance × coverage × churn (#5, #12), git-pinned decisions (#20), ranked blast radius (#28) |
| [optave/ops-codegraph-tool](https://github.com/optave/ops-codegraph-tool) | Function-level graph for 34 languages, CI gates, MCP | Co-change coupling (#6), boundary rules (#16), branch compare with caller impact (#3), three-tier incremental builds (#34), complexity metrics (#30) |
| [sverweij/dependency-cruiser](https://github.com/sverweij/dependency-cruiser) | Validate and visualize JS/TS dependencies | Forbidden/allowed/required rules and a known-violations baseline (#16), orphans (#4), focus/reaches (#28) |
| [MH4GF/dependency-cruiser-report-action](https://github.com/MH4GF/dependency-cruiser-report-action) | Dependency diagrams of changed files on each PR | PR comment with Mermaid (#17) |
| [seddonym/import-linter](https://github.com/seddonym/import-linter) | "Lint your Python architecture" | Layers, independence, forbidden, protected and acyclic contracts, unmatched-ignore alerts (#16) |
| [gauge-sh/tach](https://github.com/gauge-sh/tach) | Python module boundaries | Public interfaces, deprecated dependencies, incremental adoption (#16) |
| [glato/emerge](https://github.com/glato/emerge) | Multi-language dependency graphs and metrics | Fan-in/out, heatmaps, git metrics (#30); lightweight multi-language parsing (#26); clustering (#27) |
| [adamtornhill/code-maat](https://github.com/adamtornhill/code-maat) | Mining version-control data | Logical coupling (#6), hotspots, age, ownership (#30) |
| [git-truck/git-truck](https://github.com/git-truck/git-truck) | Git history visualizations | Most-changed and last-changed files, per-file commits (#30, #2) |
| [braedonsaunders/codeflow](https://github.com/braedonsaunders/codeflow) | In-browser architecture map | "What breaks if I change this" (#28), health grade and churn layout (#30) |
| [CodeBoarding/CodeBoarding](https://github.com/CodeBoarding/CodeBoarding) | Nested architecture diagrams, docs output | Drill-down into components (#21), Markdown + Mermaid docs with incremental re-render (#19) |
| [likec4/likec4](https://github.com/likec4/likec4) | Architecture-as-code (C4) | Nested views and drill-down (#21), exports (#19) |
| [olgasafonova/ridge](https://github.com/olgasafonova/ridge) | Architecture graph and drift between refs | Drift timeline (#32), endpoint and HTTP-client edges with confidence (#13, #23), many export formats (#19) |
| [Wilfred/difftastic](https://github.com/Wilfred/difftastic) | Structural diffs | Token-level highlights, move-aware diffs (#33, #3) |
| [marinsokol5/change-review](https://github.com/marinsokol5/change-review) | Human review step for agent changes | Blocking CLI with a JSON verdict and exit codes (#9) |
| [owndiff/own-your-diff](https://github.com/owndiff/own-your-diff) | Human review gate before agents push | Gate file checked before push (#9) |
| [jsdnaasd/patch-risk-matrix](https://github.com/jsdnaasd/patch-risk-matrix) | Deterministic risk checklist for agent diffs | Deterministic risk factors (#5), checklist framing (#8) |
| [cfal/garcon](https://github.com/cfal/garcon), [warpforgehq/warpforge](https://github.com/warpforgehq/warpforge), [ShreyPaharia/octomux](https://github.com/ShreyPaharia/octomux) | Workspaces for running agents in parallel | Worktree fleet view (#10), lineage and checkpoints (#7), per-step review (#2) |
| [thisisnsh/planx](https://github.com/thisisnsh/planx), [Qiuner/birdview](https://github.com/Qiuner/birdview) | Planning before agents change code | Plan vs actual (#8) |
| [fculmone/relay.nvim](https://github.com/fculmone/relay.nvim) | Follow an agent's changes live | Edit timeline (#7) |
| [thebjorn/pydeps](https://github.com/thebjorn/pydeps) | Python module dependency graphs | Distance-limited path queries (#28) |

## Considered and not planned

- **LLM-generated diagrams and explanations** (gitdiagram, CodeBoarding).
  repoviz is deterministic, offline and never sends code anywhere. A summary
  generated from repoviz's own JSON could come later as an optional plugin.
- **3D or force-directed "code city" maps** (CodeCharta, dep-tree). They look
  striking, but reviews need readable, labelled diagrams; #27 and #30 cover the
  same questions.
- **Running tests or coverage, and staging, applying or reverting hunks**
  (agent workspaces, change-review's per-chunk apply). These conflict with
  "read-only, never execute repository code". #12 reads coverage files that
  already exist.
- **Tree-sitter parsers.** They are a native dependency, which breaks the
  standard-library-only runtime. #26 uses lightweight parsing instead.
- **LLM-generated comprehension quizzes** (own-your-diff). They need a model.
  #9 offers a deterministic gate instead.
- **Ideas for later, not yet filed:**
  - suggested reviewers from `CODEOWNERS` (ops-codegraph-tool);
  - a VS Code extension;
  - semantic search over symbols.
