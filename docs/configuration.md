# Configuration reference

Discovery is automatic; configuration only overrides it. Settings are read, from
lowest to highest priority, from:

1. built-in defaults;
2. `[tool.repoviz]` in the repository's `pyproject.toml`;
3. `.repoviz.toml` in the repository root (top-level keys);
4. `--config FILE` (either form);
5. command-line flags (`--exclude`, `--source-root`, `--no-grimp`).

Unknown keys are ignored and listed in the discovery profile's
"configuration" line.

```toml
# ----- what to analyze -------------------------------------------------------
include = []                      # if set, only matching paths are analyzed
exclude = ["legacy/**", "*.min.js"]   # added to the built-in excludes (node_modules/, .venv/, ...)
generated = ["**/*_generated.py"] # extra generated-code patterns
include_generated = false         # analyze generated code anyway
include_vendored = false          # analyze vendor/, third_party/ ...
max_file_bytes = 2000000          # larger files are structural only

# ----- discovery overrides --------------------------------------------------------
source_roots = ["src", "lib"]     # Python import roots etc. (replaces discovery)
test_roots = ["tests"]            # everything below groups into the test-root component
test_patterns = ["**/checks/*.py"]
docs_roots = ["docs"]
default_branch = "trunk"          # otherwise: remote HEAD, init.defaultBranch, main/master/...
state_dir = "~/.cache/repoviz"    # sessions, observations and review notes (also REPOVIZ_STATE_DIR);
                                  # created owner-only (0700), because it holds copies of source files

[languages]                       # extension -> language
".pyi" = "python"
".jsm" = "javascript"

# ----- explicit components -------------------------------------------------------------
[components.billing]
paths = ["services/billing/**", "libs/payments/**"]
type = "service"
description = "Billing and payments"

# (array form works too)
# [[components]]
# name = "api"
# paths = ["api/**"]

# ----- analyzers ------------------------------------------------------------------
[analyzers]
disabled = ["go"]                 # filesystem and git are mandatory
# enabled = ["python", "manifest", "callflow"]   # allow-list alternative

[python]
use_grimp = "auto"                # auto | always (also add grimp-only edges) | never

[submodules]                      # Git submodules (see "Submodules" below)
analyze = true                    # analyze checked-out submodules as nested sub-projects
exclude = ["vendor/huge-models"]  # submodule paths (globs) never analyzed
max_files = 5000                  # larger submodules stay one box, "not analyzed (too large)"
max_mb = 50                       # the same for their total size (model weights, datasets…)

[cycles]
include_type_checking = false     # TYPE_CHECKING-only imports form cycles?
include_lazy = true               # imports inside functions form cycles?

# ----- UI / activity -------------------------------------------------------------------
[ui]
max_diagram_nodes = 250
external_dependencies = false

[activity]
poll_seconds = 3                  # live app auto-refresh
churn_commits = 300               # history window for hotspots (0 disables)

# ----- change coupling from Git history (see review.md) --------------------------------------
[history]
commits = 300                     # recent commits to learn from (0 disables)
min_revs = 5                      # a file needs this many commits before its habits count
min_shared = 3                    # commits two files must share
min_degree = 0.5                  # share of the file's commits that also changed the partner
max_files_per_commit = 30         # larger commits (bulk renames, formatting) are ignored
min_commits = 20                  # fewer usable commits (e.g. a shallow clone): no coupling signals

# ----- reviewing agent work (see review.md) -----------------------------------------------
[review]
allowed = ["src/**", "tests/**"]  # files outside are flagged "out of scope"
protected = ["src/auth/**"]       # files here are flagged "protected area modified"
sensitive = true                  # flag CI / lock files / deployment / migrations / .env
disabled_checks = []              # e.g. ["todo", "debug-output"]
wiring_ignore = []                # new files that need no importer (loaded by a framework), e.g. ["src/plugins/**"]

[review.risk]                     # weights of the risk score (normalised to 100; 0 ignores a factor)
signals = 30                      # most severe signal on the file
fan_in = 20                       # places calling (or importing) the changed code
entry_points = 15                 # entry points reaching the changed code
tests = 10                        # no test reaches it, or none was updated
sensitive = 10                    # protected, sensitive, security-related or out-of-scope path
churn = 5                         # churn hotspot (top 20% of modules by recent commits, at least 2)
size = 10                         # lines added and removed
high = 40                         # score from which a file is "high" risk
medium = 20                       # score from which it is "medium"

[[review.rules]]                  # forbidden dependencies (a "forbidden" contract over path globs; see below)
from = ["src/ui/**"]
to = ["src/db/**"]
message = "UI must go through the service layer"
severity = "high"                 # high | medium | low
```

## Submodules

A Git submodule is a separate repository pinned to a commit. A checked-out
submodule is analyzed with the superproject, as a nested sub-project, unless:

- it is listed in `[submodules] exclude`;
- it is larger than `max_files` or `max_mb`;
- the commit a state records is not in the local clone (the analysis never
  fetches).

**What is read.** Its files keep their superproject paths
(`system_modules/cbir/src/main.py`) and are read at the commit the state
records. For the working tree, the submodule's own working tree is read, so
uncommitted edits count. Nothing inside the submodule is written, and its
code is never run.

**How it appears.**

- **Structure.** The submodule node holds its code. Its line shows the pinned
  commit, file count, languages and local edits. It also shows how many commits
  it is behind its remote's default branch, from the local remote-tracking ref
  only. A submodule that is not analyzed says why.
- **Components.** The submodule is the component of its code: its top-level
  packages are drill-down detail. A manifest nested deeper inside it adds a
  project of its own.
- **Imports.** Its root is an import root (`from src.engine import run`
  resolves inside it). A module name defined both in the superproject and in a
  submodule resolves to the importer's own copy.
- **Manifests.** A superproject manifest that depends on a package the
  submodule provides (a matching name, or a path dependency) gets a
  `depends-on` edge to it, marked `cross_repository`.
- **Compose.** Compose files inside a submodule describe how it runs on its
  own, so they are ignored when the superproject has Compose files.
- **Comparisons and reviews.** Changes inside a submodule appear in Changes
  diagrams like any other change. Reviews already listed them file by file (see
  [review.md](review.md#submodules)).

`analyze = false` restores the previous behaviour: one box per submodule.

## Parse cache

Parsing a file depends on its content alone, so repoviz keeps each result in
`<state dir>/repos/<repository>/parse-cache.sqlite`. It is reused by the next
`repoviz` command, a restarted `repoviz serve`, and any revision that has the
same file content.

```toml
[cache]
disk = true      # false: keep parse results in memory only
max_mb = 500     # least recently used results go first beyond this size
```

**Hygiene.**

- Entries are keyed by analyzer, analyzer version and the file's Git blob
  hash. A new analyzer version starts its own entries, and the old ones age
  out.
- Configuration changes do not affect parsing, so they keep the cache.
- The files are private (`0700` directory, `0600` files).
- Payloads are compressed JSON, never pickle: reading an entry cannot run
  anything.
- A damaged file is moved aside (`parse-cache.sqlite.corrupt`), rebuilt, and
  reported as a `cache-reset` warning in the next snapshot.

**Commands.**

- `repoviz cache` prints the size per analyzer.
- `repoviz cache clear` empties it.
- `repoviz mcp` without `--allow-writes` never writes it.

## Architecture contracts

Contracts say who may import whom. repoviz checks them on the module import
graph of every analyzed language:
- **Reviews** report the violations a change introduces as `contract-broken`
  (see [review.md](review.md)).
- **`repoviz contracts`** checks a whole tree for CI.
- **The Dependencies tab** draws them with its **Contracts** overlay.

```toml
[[contracts]]
name = "Layered backend"            # unique; shown in signals and the UI
type = "layers"                     # layers | independence | forbidden | public-interface | acyclic | required
layers = ["app.routes", "app.services", "app.models"]   # high → low: a lower layer may not import a higher one
containers = []                     # optional: the same layers inside each container, e.g. ["svc.orders", "svc.users"]
ignore = ["app.services.legacy -> app.routes.compat"]   # imports allowed anyway ("importer -> imported")
allow_indirect = true               # false: also follow chains (models → util → routes)
severity = "high"                   # high | medium | low (default high)
message = "Keep the layers"         # optional, prefixed to every violation

[[contracts]]
name = "Features are independent"
type = "independence"
modules = ["app.features.*"]        # each feature is a unit; units never import each other

[[contracts]]
name = "Storage only through its API"
type = "public-interface"
module = "app.storage"              # code outside it...
public = ["app.storage.api", "app.storage.types"]   # ...imports only these

[[contracts]]
name = "UI does not touch the database"
type = "forbidden"
from = ["src/ui/**"]
to = ["src/db/**"]

[[contracts]]
name = "No package cycles"
type = "acyclic"
modules = ["app.*"]                 # no import cycle between these units

[[contracts]]
name = "Handlers go through the service layer"
type = "required"
from = ["app.handlers.*"]           # every module here...
to = ["app.services"]               # ...imports at least one of these

contracts_baseline = ".repoviz-known-violations.json"   # top-level key; the default
```

| Type | Keys | Broken when |
|---|---|---|
| `layers` | `layers` (2+, high → low), `containers` | a module in a lower layer imports one in a higher layer (within the same container) |
| `independence` | `modules` | a module of one unit imports a module of another |
| `forbidden` | `from`, `to` | a module matching `from` imports one matching `to` |
| `public-interface` | `module`, `public` | code outside `module` imports something inside it that is not `public` |
| `acyclic` | `modules` | the units import each other in a cycle (one violation per cycle, with an example) |
| `required` | `from`, `to` | a module matching `from` imports none of `to` |

**Patterns**
- **Names.** A pattern without `/` is a qualified name: `app.services` means that
  package and everything in it. Wildcards match names (`app.handlers.h*`).
- **Paths.** A pattern with `/` is a path glob anchored at the repository root
  (`src/features/*`, `src/storage/api.js`). This is how you write contracts for
  JavaScript, TypeScript or any path-named module.
- **Units.** `pkg.*` or `dir/*` in `modules` makes one unit per child package or
  folder.

**Scope.** Imports from test modules and `TYPE_CHECKING`-only imports are not
checked. By default only direct imports are checked. With
`allow_indirect = false` (layers, independence, forbidden), repoviz also follows
chains of imports:
- a chain only passes through modules the contract does not constrain;
- chains are at most 8 hops long;
- violations are reported with the chain.

**Ignores.** `ignore` entries use the same patterns on both sides. An entry that
matches nothing is reported as a *stale ignore*, so exceptions don't outlive the
code they excused.

**Checks on the configuration.** Unknown keys in a contract are reported with the
configuration sources. A contract with a wrong `type` or missing keys is an
error, as is a duplicate name.

**`[[review.rules]]`** keep working unchanged: each one is a `forbidden`
contract named "review rule #N". The signal is now `contract-broken`, and
`forbidden-dependency` in `review.disabled_checks` still turns it off.

### Known violations (baseline)

A new contract on legacy code often starts with violations. Record them once,
commit the file, and only new violations are reported from then on:

```bash
repoviz contracts --baseline > .repoviz-known-violations.json   # or: -o .repoviz-known-violations.json
git add .repoviz-known-violations.json
```

repoviz never writes into the repository on its own: `--baseline` prints the file
and you decide where it goes (see the read-only rule in `AGENTS.md`). The
baseline is read from the tree being checked, so it travels with the code:
- Reviews and `repoviz contracts` skip the violations it lists.
- Violations it lists that no longer exist are reported as *fixed*, so you can
  remove them from the file.
- A review that edits the baseline raises `contract-baseline-changed`: *medium*
  when it newly accepts violations. This matters because an agent editing the
  file is how violations get quietly accepted.

## Glob syntax

- `*` matches within a path segment, `?` matches one character, and `**` matches
  any number of segments.
- A pattern without `/` matches a name at any depth, along with everything
  beneath it (`node_modules`).
- A trailing `/` restricts the pattern to directories.
- A leading `/`, or a `/` in the middle, anchors the pattern at the repository
  root.
- Repeated `**/` segments are collapsed, so `**/**/x` is the same as `**/x`.

## Environment variables

| Variable | Effect |
|---|---|
| `REPOVIZ_STATE_DIR` | where sessions, observations and the parse cache are stored |
| `REPOVIZ_NO_PARALLEL=1` | parse Python files in-process (no worker pool) |
| `REPOVIZ_NO_DISK_CACHE=1` | keep parse results in memory only (no `parse-cache.sqlite`) |
