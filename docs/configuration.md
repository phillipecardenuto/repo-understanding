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

[[review.rules]]                  # forbidden dependencies
from = ["src/ui/**"]
to = ["src/db/**"]
message = "UI must go through the service layer"
severity = "high"                 # high | medium | low
```

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
| `REPOVIZ_STATE_DIR` | where sessions and observations are stored |
| `REPOVIZ_NO_PARALLEL=1` | parse Python files in-process (no worker pool) |
