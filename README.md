# repoviz

A local, **read-only** tool that analyzes any Git repository and shows its
structure, dependencies, architectural changes and current development activity
as interactive **Mermaid** diagrams. It runs either as:

1. a **live web application** backed by a small local analysis server
   (`repoviz serve`), or
2. a **self-contained HTML report** that opens offline from `file://`
   (`repoviz report`).

It is not tailored to any language, layout, namespace, build system, branch
name or hosting provider. It discovers what the repository contains, and
configuration can override every inference. The runtime needs only Python ≥ 3.10
and `git`: no Node.js, no Graphviz, no network (Mermaid is vendored).

It is built for **supervising AI coding agents** across features and
implementation waves, and for understanding any codebase. It answers questions like:

| Question | Where |
|---|---|
| Which modules did the agent touch? Did it touch anything it should not have? | **AI Review** tab, `repoviz review` |
| What are the key changes in a module, and do they look right? | **AI Review** → change cards (symbols, signatures, diff) |
| What should I tell the agent to fix, complete or revert? | **AI Review** → notes → feedback prompt, `repoviz review --format prompt` |
| What modules, packages, components and services exist? How are they organised? | **Structure** tab, `repoviz discover` |
| What depends on what? Why? (file:line evidence) | **Dependencies** tab, `repoviz mermaid --view dependencies` |
| Why does A depend on B? Which import chain, in which files? | **Dependencies** → right-click a link (or `w`), `repoviz why A B` |
| If this changes, what may break? (blast radius) | **Dependencies** → `b` on a node, `repoviz impact X` |
| What changed since another commit / branch / tag / merge base? | **Changes** tab, `repoviz diff` |
| Did a change introduce a new dependency or a dependency cycle? Resolve one? | **Changes** tab, `repoviz diff --fail-on new-cycle` |
| Which parts of the repository are being modified right now? | **Activity & Flow** tab, `repoviz activity` |
| Which execution/call flow, entry points and tests may be affected? | **Activity & Flow** tab (affected flow) |
| What has an AI coding agent changed during its current work session? | `repoviz session start` + **Activity & Flow** tab |

## Quick start

```bash
pip install .                      # or: pip install -e .   (no runtime dependencies)
repoviz serve --open               # live app for the repository in the current directory
repoviz report -o report.html      # offline, single-file interactive report
repoviz diff                       # HEAD vs working tree, as text
```

Not sure where to start? Press **Help** (or `?`) in the app for a searchable guide:
the recommended workflow, how to get the most out of each tab, the diagram
conventions and keyboard shortcuts. Each tab also opens with a one-line hint that
links to its section.

Released versions are listed in [CHANGELOG.md](CHANGELOG.md). Each release attaches
a wheel you can install directly, for example
`pipx install https://github.com/phillipecardenuto/repo-understanding/releases/download/v0.1.0/repoviz-0.1.0-py3-none-any.whl`.

To run without installing, use `PYTHONPATH=src python -m repoviz …` from a checkout.
On a machine without network access, use `pip install --no-build-isolation .`
(it needs only the `setuptools` already present in the environment).

Optional extras: `pip install '.[grimp]'` adds a grimp cross-check of the Python
import graph, and `'.[test]'` / `'.[browser]'` install test dependencies.

## Commands

| Command | Purpose |
|---|---|
| `repoviz review [TARGET] [--format text\|markdown\|prompt\|json\|sarif\|github\|pr-comment] [--fail-on …] [--commit SHA] [--by-commit] [--from-report FILE]` | Review agent work: current session, past wave (`session:<id>`), `branch`, `last-commit` or any range. `--commit` reviews one of its commits alone; `--by-commit` groups the text output by commit. `sarif`, `github` (inline annotations) and `pr-comment` are for CI; `--from-report` re-renders a saved JSON report without analysing again. |
| `repoviz serve [--port 8765] [--open] [--session]` | Live web app (binds 127.0.0.1). `--session` starts a work session if none is active. |
| `repoviz report [-o FILE] [--compare SPEC …]` | Self-contained HTML report. Includes default comparisons: uncommitted changes; staged and unstaged when something is staged; the branch vs its merge base with the default branch; the active session. |
| `repoviz diff [SPEC] [--format text\|json\|markdown\|mermaid] [--fail-on …]` | Compare two states; `--fail-on new-cycle,new-dependency,…` exits with status 3 (for CI and agent guardrails). History presets: `last-commit`, `last-merge`, `branch` (since it left the default branch) and `since:<tag or date>` (`since:v0.1.0`, `since:2024-06-01`). |
| `repoviz cache [info\|clear]` | The persistent parse cache in the state directory: its size per analyzer, or empty it. Off with `REPOVIZ_NO_DISK_CACHE=1` or `[cache] disk = false`. |
| `repoviz why A B [--json]` | Why A depends on B: up to 5 shortest import (or call) chains, each hop with file:line and code; the reverse direction when A does not depend on B. |
| `repoviz impact X [--depth N] [--json]` | Blast radius: what uses X, transitively, by distance and fan-in, with the entry points and tests it reaches. |
| `repoviz mermaid --view changes\|dependencies\|structure\|flow\|system` | Print Mermaid text (paste into docs or PRs). `system` draws the services from the Compose files. `--direction auto\|LR\|TB` sets the orientation (auto: from the diagram's shape); `--fold N` folds long lists of leaves in the structure view. |
| `repoviz discover [--json]` | What discovery found: languages, manifests, roots, entry points, CI… |
| `repoviz snapshot [--rev REV] -o snap.json` | The normalized graph of one state as JSON. |
| `repoviz activity [--json]` | Files being modified now, with impact, tests and config flags. |
| `repoviz coupling [--path FILE] [--json]` | Files that usually change together, learned from Git history. |
| `repoviz contracts [--format text\|json\|sarif\|github] [--baseline] [--suggest]` | Check the architecture contracts (`[[contracts]]`: layers, independence, forbidden, public interface, acyclic, required). Exit 3 on a violation not in the known-violations baseline. `--baseline` prints the baseline to commit; `--suggest` proposes a layers contract. |
| `repoviz session start\|status\|scope\|end\|list [--allow G] [--protect G]` | Manage work sessions (waves) and their scope. |
| `repoviz mcp [--allow-writes]` | Read-only MCP server over stdio: agents ask where code belongs, what depends on it, whether a path is in scope, and review their own work. See [docs/mcp.md](docs/mcp.md). |

Every command takes `-C PATH` (repository), `--config FILE`, `--exclude GLOB`,
`--source-root DIR` and `-o FILE`.

### Comparisons

The default comparison is **HEAD vs the working tree**. `SPEC` accepts:

| Spec | Meaning |
|---|---|
| `all` (default) | HEAD vs working tree: staged + unstaged + untracked |
| `staged` | HEAD vs index: staged changes only |
| `unstaged` | index vs working tree (tracked files): unstaged changes only |
| `session` | session baseline vs working tree |
| `A..B` | exact difference between two commits, branches or tags, e.g. `v1.0..v2.0`, `main..feature` |
| `A...B` | what B added since it left A (merge base of A and B vs B, like a pull request), e.g. `main...feature`; `main...` = the working tree since it left main |
| `A` | revision A vs working tree |

Special revisions are `WORKTREE`, `WORKTREE-TRACKED`, `INDEX` (or `STAGED`),
`SESSION`, `EMPTY` and `HEAD`. Revisions are validated with
`git rev-parse --end-of-options`, and nothing is checked out: every state is read
straight from the object database, the index or the disk.

## The tabs

**AI Review** (default). Pick the current work session, a past wave, the branch,
the last commit or a recently updated branch, or **compare any two branches**
(live app): local or remote branches, tags or commits, either *since they
diverged* (what the target added since it left the base, like a pull request)
or as the *exact difference* between the two trees.

- **Scope:** set what the agent may change and what it must not touch.
- **"Where the agent went" map:** touched components, packages or files, with
  lines changed. Protected and out-of-scope areas are highlighted.
- **Review signals:** triage each one (dismiss, annotate, or send to the agent).
- **Risk:** a wave risk badge and a Risk column (0–100, *high* / *medium* /
  *low*) that orders the files, riskiest first, and lists why: signal severity,
  callers, entry points reached, missing tests, sensitive paths, churn hotspots
  and size. `j` / `k` follow that order.
- **Changed-files table.** Signals (highest severity first) and risk lead each
  row.
  - **Grouping.** From 20 files, rows are grouped by component. Each group
    shows its files, lines and highest signal. **▾** or `o` collapses a group,
    and `j` / `k` skip collapsed groups. A submodule heads the group of the
    files changed inside it.
  - **Paths** start at the component's folder (`…/cbir/src/search.py`) and are
    shortened in the middle; hover for the full path, or use **Copy path**.
  - **Search** (`/`) and **component chips** narrow the table, together. The
    search, chips and groups are remembered.
  - **Change card.** Each file opens one with:
    - key changes: functions and classes with before/after signatures;
    - dependency changes;
    - tests that exercise it;
    - the diff, where any line can be annotated.
- **Notes → prompt:** notes become a feedback prompt for the agent. See
  [docs/review.md](docs/review.md).

**Changes.** Compares two states at an aggregation level: Auto, Components,
Projects, Packages/directories, or Modules/files. Shows changed nodes plus their
strongest neighbours (hubs are summarised), everything, or only changes. Summary
cards cover nodes, relationships, new dependencies and cycles. Below the diagram
are tables of new and removed dependencies (with evidence), cycles introduced,
resolved or changed, and every changed node with the reason it changed.

The changed-nodes list puts the most relevant first:

1. new dependencies, cycles and role changes;
2. API changes (added, removed, renamed, signatures);
3. dependency changes;
4. body changes;
5. formatting-only.

Each comparison in the picker says how many files it touches. The picker
groups them as *Uncommitted*, *History* and *Custom*. On a clean checkout the
tab opens on this branch, the last merge or the last commit (the first that has
changes), with a note. The history comparisons are `last-commit`, `last-merge`,
`branch` and `since:<tag or date>`. Reports include the last commit and this
branch.

Folders listed only because something inside changed are hidden behind **show
folder rollups (N)**. As in AI Review, you can search it (`/`), filter it with
component chips, or group it by component. The live app accepts any comparison;
a static report offers the precomputed ones.

**The header** counts what the repository holds, each count apart:

- code components (those holding modules);
- services (first-party or infrastructure);
- submodules;
- third-party packages;
- entry points.

Each chip has an icon and opens the matching view. `repoviz discover` prints
the same numbers ("contents: …", and `breakdown` in `--json`).

**Structure.** When the repository has Compose files, the tab opens on the
**System** view: the running system, from `docker-compose*.yml` / `compose*.yaml`.

- **One service per name.** A service declared in several files
  (`docker-compose.yml`, `docker-compose.prod.yml`…) is one service with
  variants. Click it to see what differs between them (image, command, ports).
- **First-party services** (built from this repository) are boxes holding the
  code they run. `uvicorn app.main:app` points to `app.main`,
  `celery -A app.worker` to `app.worker`, a Dockerfile `CMD` to its script, and a
  build context to its directory or submodule.
- **Infrastructure** sits in its own group, with an icon and a word per kind:
  database, cache, queue, object store, search, monitoring, proxy and
  coordination.
- **Links between services:**
  - **talks to** (thick): an environment value names the other service, such as
    `redis://redis:6379` or `API_HOST=api`. The line is labelled with the
    protocol and port.
  - **starts after** (dashed): `depends_on`.
  - **shares volume** (dotted): a named volume they both mount.
- **Privacy.** Environment values are never read into the model, only variable
  names.
- **Entry points.** Only first-party services are entry points (one per service,
  with its variants). Infrastructure commands such as `etcd` or `minio server`
  are not.

The **Files and components** view shows containment from the repository root
down to modules and symbols,
as a tree or as nested boxes, with churn hotspots from Git history. Double-click
a node to drill down. Click a churn hotspot to open its **code changes** under
the graph (its last commits and the diff of one of them, with `+` / `−` on
every changed line); Esc closes it and the graph stays as it was. Any other file
offers *Show code changes* in the live app. Below it, the discovery profile
lists:

- languages, with whether each is analyzed or structure-only
- projects, workspaces, manifests and lock files
- source, test and docs roots
- generated and vendored code
- entry points
- containers, deployment and CI definitions
- existing architecture configuration and dependency tooling
- analyzer runs and diagnostics

**Dependencies.** The dependency graph at any level, filtered by relationship.
The relationships are `imports`, `depends-on` from manifests, `calls`,
`invokes`, `builds`, and **runtime (containers, HTTP)**. Options cover external
packages, stdlib, tests and type-only imports.

The *Auto* level shows components when the code has at least three of them.
Otherwise it shows packages, with a note saying why and a link to switch. A
level you pick is remembered and always wins. Compose services appear only
where the code calls them; the **services** option adds their own links,
which the System view shows anyway. You can focus on a
node, choose depth and direction (depends on / used by), and highlight or isolate
cycles. The **Contracts** overlay marks imports that break an architecture
contract (thick, dashed, "⚠ contract name") and draws a layers contract's
layers as numbered groups (Layer 1 is the highest); a card lists every contract and its violations.
Clicking a node spotlights its direct neighbourhood without redrawing:
modules that use it (solid, thick links), modules it uses (dashed, thick links),
and everything else faded; Esc or a click on the background restores the graph.
Clicking an edge label explains *why* one thing depends on another, with
file:line evidence and source excerpts for every underlying import.

**Why does A depend on B?** Right-click a link, or click it and press `w`. The
side panel lists up to 5 shortest import chains (call chains between two
functions), for example `ui.forms → services.orders → db.models`, each step with
file:line and code. The first chain is outlined in the diagram and the rest
fades.

**Blast radius.** Press `b` on a selected node, or use *Blast radius* in its
details (also from the Structure tab). The diagram then shows what may break if
the node changes:

- its dependents by distance ring (ring 1 uses it directly), with border weight
  and dash per ring, and the ring written on each node;
- the entry points (play icon) and tests (flask) it reaches;
- a summary: "Changing `images.list_images` can affect 14 modules in 4
  components, 3 entry points, 6 tests".

Static reports compute both in the page.

**Activity & Flow.** For each file currently being modified: Git status,
owning component, lines added and removed, and first/last observation times.
It also shows architecture impact (dependencies added or removed, component
dependencies, cycles introduced, public API added or removed), affected tests and
configuration changes. An activity map groups the files by component. The
affected-flow diagram shows changed symbols, their callers and callees, and the
entry points and tests that can reach them (static call graph; module level for
languages without call data). The live app refreshes automatically.

### Visual conventions (never colour alone)

| | Fill / line | Border / style | Text marker |
|---|---|---|---|
| Added node | green | solid, thick | `✚` + "added" |
| Removed node | red | **dashed** | `✖` + "removed" |
| Modified node | amber | solid, thick | `✎` + "modified" |
| Unchanged node | neutral | thin | – |
| Added edge | green | thick arrow `==>` | "+ new" |
| Removed edge | red | dashed | "− removed" |
| Evidence changed | amber | solid | "~ changed" |
| Unchanged edge | gray | thin | – |
| Edge in a cycle | purple | dashed | "⟲ cycle" / "⟲ new cycle" |
| Old cycle the change does not touch | purple, faint | thin, dotted | "existing cycle" |

Every diagram also carries an `accTitle`/`accDescr`, all data is available in
tables, and diagrams can be navigated with the keyboard (arrows, +/-, 0 to fit).

**Large diagrams stay readable.**

- **Orientation from shape.** Changes, Structure (both views) and Dependencies
  turn a diagram when the other direction fits the screen at a clearly larger
  zoom: a deep tree runs left to right, a long thin chain top to bottom, and a
  System view with many services stacks them instead of lining them up. The
  ⇄ / ⇅ button cycles *auto*, left to right and top to bottom, and is
  remembered per tab (the System view has its own). A layers contract's layers
  and nested boxes keep their layout.
- **Readable Fit.** **Fit** never shrinks labels below 11 px. A larger diagram
  fits its width and you pan the rest; when it is more than twice the view, a
  mini-map in the corner shows where you are, and a click there moves the view.
- **Folded leaf lists.** More than 8 leaves of one kind under one parent (test
  files, docs, modules, files) become one node, "+ 27 test files". Click it to
  expand. Changed files, the selected node and find matches always stay outside
  a fold.
- **Long names** are shortened in the middle (`app.services…images`); the full
  name is in the tooltip and the details panel.
- **Old cycles** the change does not touch (no member changed) are drawn thin,
  dotted and faint, labelled "existing cycle", so new cycles stand out.

`repoviz mermaid` and `repoviz diff --format mermaid` do the same with
`--direction auto|LR|TB` (default `auto`), and `--fold N` for the structure view
(default 8; `0` never folds).

Nodes carry a kind icon from repoviz's own line-icon set, each in its own colour:
house (repository), package (project / component), folder (package /
directory), code file (module), flask (tests), link (external), play (entry
point), class, function, container, CI, configuration and docs. The icons are
drawn with CSS, so they look the same on every OS and in both themes; they are
also kept in downloaded SVGs. `repoviz mermaid` output for Markdown (e.g. GitHub)
uses emoji instead, because it can't carry the stylesheet.

## Supervising AI coding agents

A **work session** ("wave") records the working tree when the agent starts,
including already-dirty files. Everything changed afterwards is attributed to
the session, even if the agent commits along the way. The session also records
the agreed **scope**.

```bash
repoviz session start --label "wave 3: billing" --allow "src/billing/**" --protect "src/auth/**"
# ... the agent works (and maybe commits) ...
repoviz review                          # where it went, scope violations, review signals
repoviz review --format prompt          # numbered file:line feedback to paste back to the agent
repoviz review --by-commit              # the same, step by step (the UI's Commits panel)
repoviz review main...claude/feature    # any branch against any other, since it left main
repoviz review --fail-on protected --fail-on high   # guardrail for scripted loops (exit 3)
repoviz review --fail-on risk:high      # exit 3 when the wave risk is high
repoviz session end                     # freezes the wave; later: repoviz review session:<id>
```

Review signals flag likely problems for a human to check:

- protected or out-of-scope files;
- new cycles or forbidden dependencies;
- broken imports, and calls to functions the agent removed;
- changed signatures whose callers were not updated;
- new code that is not wired in (a router never registered, a module nothing imports);
- a rename that left code using the old name (renames and moves are recognised
  instead of showing as a removal plus an addition);
- a usual companion change that is missing: a file that almost always changes
  together with an edited one (a migration, a test, a client) was left untouched;
- untested changes and weakened tests (skips, removed assertions);
- swallowed exceptions, debugger statements and possible secrets.

**Runtime coupling** is visible too. Code that starts a container this
repository builds (Docker SDK, `docker run`) or calls one of its services over
HTTP gets **runs image** and **talks to** edges, so the Dependencies view shows,
for example, "API → ML containers" even though nothing imports them. The
`new-runtime-dependency` signal reports such a link added by a wave.

**Architecture contracts** tell the agent to respect the architecture and check it
on every wave. Declare layers (`routes → services → models`), independent
features, public interfaces, forbidden or required dependencies and acyclic
packages in `[[contracts]]`. A review reports each new violation as
`contract-broken`, with the file, the line and the import chain. Existing
violations can be accepted once in a committed baseline
(`repoviz contracts --baseline > .repoviz-known-violations.json`), so only new
ones count. See [docs/configuration.md](docs/configuration.md#architecture-contracts).

Each changed file also gets an explainable **risk score**, and the wave's risk is
its riskiest file. The text output starts with "Review first (riskiest files)",
the feedback prompt lists the riskiest files to double-check, and
`--fail-on risk:high` (or `risk:medium`) gates on it. Weights live in
`[review.risk]`.

See [docs/review.md](docs/review.md) for the full list and the configuration
(`[review]` scope, forbidden-dependency rules and risk weights).

`repoviz serve --session` starts a session automatically, and the Activity tab
can start or restart sessions. Session data lives in `~/.cache/repoviz`
(override with `REPOVIZ_STATE_DIR` or `state_dir`), never inside the repository.
Parse results are kept there too, so the next command, or a restarted
`repoviz serve`, re-parses only files whose content changed. See
[docs/configuration.md](docs/configuration.md#parse-cache).

### Let the agent ask first: the MCP server

`repoviz mcp` gives the agent the same map through the Model Context Protocol.
It answers:

- `where_does_this_go`: the component, layer and allowed imports of a file, even
  a new one;
- `impact`: callers, importers, entry points and tests;
- `dependency_path`: why A depends on B;
- `check_scope`: whether planned files are allowed, protected or out of scope;
- `review_current` and `contracts_check`: a self-review before handing over;
- `architecture_overview`.

Every tool is read-only and capped, and it never runs repository code.

```bash
claude mcp add repoviz -- repoviz mcp -C .
```

The prompts `plan_check` and `self_review` wrap the usual before-and-after
checks. See [docs/mcp.md](docs/mcp.md) for the tools, the safety rules and a
sample transcript.

With Claude Code, you can record activity while the agent edits by adding a hook
in `.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": "repoviz activity > /dev/null"}]}
    ]
  }
}
```

### In CI: review every pull request

The composite action in `.github/actions/review` runs `repoviz review` on each
pull request. It posts one comment that is updated on every push, with a
Mermaid map of where the change went, the riskiest signals and every file, and
it adds inline annotations on the changed lines. It can also upload SARIF to code
scanning and fail the check on the conditions you choose. It reads the
repository through Git only and never runs its code.

```yaml
# .github/workflows/repoviz.yml
name: repoviz review
on: pull_request
permissions:
  contents: read
  pull-requests: write        # the sticky comment
  # security-events: write    # only with sarif: "true"
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0      # repoviz needs the history of both ends
      - uses: phillipecardenuto/repo-understanding/.github/actions/review@main   # pin a tag or commit
        with:
          fail-on: "protected risk:high contract-broken"   # optional gate (exit 3)
          min-severity: medium                             # lowest severity annotated inline
          # sarif: "true"
```

The same outputs work in any CI:

```bash
repoviz review "$BASE...$HEAD" --format json -o review.json          # analyse once
repoviz review --from-report review.json --format pr-comment --link-base "$URL/blob/$SHA"
repoviz review --from-report review.json --format sarif -o repoviz.sarif
repoviz review --from-report review.json --fail-on high             # gate without analysing again
```

## Supported ecosystems

| Area | Support |
|---|---|
| Structure (every language) | Filesystem and Git analyzers: directories, files, roles, churn. Unsupported languages appear as structural nodes plus a diagnostic. |
| Python | AST analyzer. Handles src/flat/namespace layouts, absolute, relative, `TYPE_CHECKING`, conditional, lazy and `importlib` imports, symbols, call flow (aliases, re-exports, inheritance, nested functions), entry points and test functions. Optional grimp cross-check. |
| JavaScript / TypeScript | Lexer-based analyzer. Handles ES modules, `export … from`, `require`, dynamic `import()`, extension and `index` probing, `.js`→`.ts`, `tsconfig` `paths`/`baseUrl`, and workspace packages (source preferred over `dist`). Also Vue/Svelte `<script>` blocks, symbols and heuristic call flow. |
| Go | Package-level imports resolved against `go.mod` module paths; functions and methods. |
| Manifests | `pyproject.toml` (PEP 621, Poetry, PDM, Hatch, setuptools, uv workspaces), `setup.py` (read with `ast`, never executed), `setup.cfg`, requirements, Pipfile, conda, `package.json` and npm/yarn/pnpm/lerna/rush workspaces, `tsconfig`, Deno, `Cargo.toml` and workspaces, `go.mod`/`go.work`, Maven, Gradle (+ settings), `.sln`/`.csproj`, Composer, Gemfile/gemspec, pubspec, mix, SwiftPM, CMake. Lock files are recognized. |
| Containers, deployment, CI | Dockerfiles (base images, COPY sources, CMD/ENTRYPOINT), Compose services and `depends_on`, Kubernetes/Helm/Kustomize/Terraform/Procfile and more, GitHub Actions, GitLab CI, CircleCI, Jenkins, Azure, Buildkite… |

New languages plug in through the analyzer interface; see
[docs/architecture.md](docs/architecture.md).

## Configuration

Discovery is automatic. Use `[tool.repoviz]` in `pyproject.toml` or a
`.repoviz.toml` file to override it:

```toml
exclude = ["legacy/**"]
source_roots = ["src", "lib"]
test_roots = ["tests", "integration"]
generated = ["**/*_generated.py"]

[components.billing]
paths = ["services/billing/**", "libs/payments/**"]
type = "service"

[cycles]
include_type_checking = false   # TYPE_CHECKING-only imports do not form cycles (default)

[analyzers]
disabled = ["go"]
```

See [docs/configuration.md](docs/configuration.md) for every option.

## Safety and privacy

- The tool never modifies the repository. Git runs with `GIT_OPTIONAL_LOCKS=0`
  (even `git status` does not rewrite the index), and the test suite checks that
  every file, including `.git`, is untouched after analysis.
- Repository code is never imported or executed. Python is parsed with `ast`
  and `setup.py` is read, not run. The optional grimp check is static too.
- A repository you did not create can't make the analysis run programs. Every
  git call overrides the settings in `.git/config` that would do so:
  `core.fsmonitor`, clean/smudge filter drivers, `log.showSignature` (GPG) and
  submodule recursion.
- Checked-out Git submodules are analyzed with the superproject (see
  [configuration](docs/configuration.md#submodules)). Git runs inside them with
  the same overrides, and nothing is ever fetched.
- Build files are parsed without entity expansion. Pathological inputs (huge
  minified lines, adversarial globs) can't cause catastrophic regex
  backtracking.
- The server binds to 127.0.0.1 and rejects foreign `Host` headers (DNS
  rebinding).
  - Every `/api/*` call needs a custom header, so other web pages can't trigger
    or embed API calls (CSRF / cross-site reads).
  - Pages carry a strict Content-Security-Policy with no inline or evaluated
    script, plus `nosniff` and `Cross-Origin-Resource-Policy: same-origin`.
  - Revisions from the UI can never be interpreted as git options.
- Mermaid runs with `securityLevel: "strict"`, and labels built from file names
  are escaped.
- Credential-like values are redacted from excerpts and diffs. Static reports
  show your home directory as `~`.
- The state directory holds session baselines (copies of your files), review
  notes and the parse cache. It is created with owner-only permissions
  (`0700` / `0600`). The parse cache stores compressed JSON only, so reading it
  cannot run anything.
- If Git refuses a repository ("dubious ownership"), the tool respects that and
  analyzes it as a plain directory, with a diagnostic explaining why.

## Development

Releases: bump `version` in `pyproject.toml` and `src/repoviz/__init__.py`, add a
`CHANGELOG.md` section, then push a tag (`git tag -a v0.2.0 -m "repoviz 0.2.0" &&
git push origin v0.2.0`), or run the *Release* workflow manually (Actions → Release
→ Run workflow) with the tag name. The workflow tests, builds and publishes the
GitHub Release with the wheel, the sdist and the changelog section as notes.

```bash
pip install -e '.[test,grimp,browser]'
pytest                                   # unit, integration and (if Chromium is present) browser tests
PLAYWRIGHT_BROWSERS_PATH=/path/to/browsers pytest tests/test_browser.py
```

Documentation: [reviewing agent work](docs/review.md) · [architecture](docs/architecture.md) ·
[configuration](docs/configuration.md) · [data model](docs/data-model.md) ·
[evaluation on a large multi-module system](docs/evaluation-elies.md) ·
[feature wishlist](docs/wishlist.md).

Contributing, by people or coding agents: start with [AGENTS.md](AGENTS.md). It
covers the invariants, how to test and when a change counts as done. Planned work
is in the GitHub issues labelled `wishlist`.

Mermaid 11.17.2 is vendored in `src/repoviz/web/vendor/` under the MIT licence
(`LICENSE-mermaid.txt`).
