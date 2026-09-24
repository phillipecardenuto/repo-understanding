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
| `repoviz review [TARGET] [--format text\|markdown\|prompt\|json] [--fail-on …]` | Review agent work: current session, past wave (`session:<id>`), `branch`, `last-commit` or any range. |
| `repoviz serve [--port 8765] [--open] [--session]` | Live web app (binds 127.0.0.1). `--session` starts a work session if none is active. |
| `repoviz report [-o FILE] [--compare SPEC …]` | Self-contained HTML report. Includes default comparisons: uncommitted changes; staged and unstaged when something is staged; the branch vs its merge base with the default branch; the active session. |
| `repoviz diff [SPEC] [--format text\|json\|markdown\|mermaid] [--fail-on …]` | Compare two states; `--fail-on new-cycle,new-dependency,…` exits with status 3 (for CI and agent guardrails). |
| `repoviz mermaid --view changes\|dependencies\|structure\|flow` | Print Mermaid text (paste into docs or PRs). |
| `repoviz discover [--json]` | What discovery found: languages, manifests, roots, entry points, CI… |
| `repoviz snapshot [--rev REV] -o snap.json` | The normalized graph of one state as JSON. |
| `repoviz activity [--json]` | Files being modified now, with impact, tests and config flags. |
| `repoviz session start\|status\|scope\|end\|list [--allow G] [--protect G]` | Manage work sessions (waves) and their scope. |

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
| `A..B` | commit, branch or tag vs another one, e.g. `v1.0..v2.0`, `main..feature` |
| `A...B` | merge base of A and B vs B, e.g. `main...` = changes since branching from main |
| `A` | revision A vs working tree |

Special revisions are `WORKTREE`, `WORKTREE-TRACKED`, `INDEX` (or `STAGED`),
`SESSION`, `EMPTY` and `HEAD`. Revisions are validated with
`git rev-parse --end-of-options`, and nothing is checked out: every state is read
straight from the object database, the index or the disk.

## The tabs

**AI Review** (default). Pick the current work session, a past wave, the branch,
the last commit or any range.

- **Scope:** set what the agent may change and what it must not touch.
- **"Where the agent went" map:** touched components, packages or files, with
  lines changed. Protected and out-of-scope areas are highlighted.
- **Review signals:** triage each one (dismiss, annotate, or send to the agent).
- **Changed-modules table.** Each file opens a change card:
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
resolved or changed, and every changed node with the reason it changed. The live
app accepts any comparison; a static report offers the precomputed ones.

**Structure.** Containment from the repository root down to modules and symbols,
as a tree or as nested boxes, with churn hotspots from Git history. Double-click
a node to drill down. Below it, the discovery profile lists:

- languages, with whether each is analyzed or structure-only
- projects, workspaces, manifests and lock files
- source, test and docs roots
- generated and vendored code
- entry points
- containers, deployment and CI definitions
- existing architecture configuration and dependency tooling
- analyzer runs and diagnostics

**Dependencies.** The dependency graph at any level, filtered by relationship
(`imports`, `depends-on` from manifests, `calls`, `invokes`, `builds`). Options
cover external packages, stdlib, tests and type-only imports. You can focus on a
node, choose depth and direction (depends on / used by), and highlight or isolate
cycles. Clicking an edge label explains *why* one thing depends on another, with
file:line evidence and source excerpts for every underlying import.

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

Every diagram also carries an `accTitle`/`accDescr`, all data is available in
tables, and diagrams can be navigated with the keyboard (arrows, +/-, 0 to fit).

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
repoviz review --fail-on protected --fail-on high   # guardrail for scripted loops (exit 3)
repoviz session end                     # freezes the wave; later: repoviz review session:<id>
```

Review signals flag likely problems for a human to check:

- protected or out-of-scope files;
- new cycles or forbidden dependencies;
- broken imports, and calls to functions the agent removed;
- changed signatures whose callers were not updated;
- untested changes and weakened tests (skips, removed assertions);
- swallowed exceptions, debugger statements and possible secrets.

See [docs/review.md](docs/review.md) for the full list and the configuration
(`[review]` scope and forbidden-dependency rules).

`repoviz serve --session` starts a session automatically, and the Activity tab
can start or restart sessions. Session data lives in `~/.cache/repoviz`
(override with `REPOVIZ_STATE_DIR` or `state_dir`), never inside the repository.

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
- The state directory (session baselines are copies of your files, plus review
  notes) is created with owner-only permissions (`0700` / `0600`).
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
[evaluation on a large multi-module system](docs/evaluation-elies.md).

Mermaid 11.17.2 is vendored in `src/repoviz/web/vendor/` under the MIT licence
(`LICENSE-mermaid.txt`).
