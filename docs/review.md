# Reviewing AI-agent work

The **AI Review** tab (the default tab) and `repoviz review` help you supervise
coding agents across features or implementation *waves*:

1. **See where the agent went.** A map of touched components, packages or files
   shows lines changed, scope violations and high-severity signals.
2. **Dig into a module.** Each changed file has a change card with its *key
   changes* (functions/classes added, modified or removed, with before/after
   signatures), dependency changes, tests that exercise it, and the diff.
3. **Spot what should not have happened.** Files outside the agreed scope or in
   protected areas are flagged, as are heuristic *review signals* that point at
   likely mistakes.
4. **Tell the agent.** Leave notes on signals, files or individual diff lines,
   each with a verdict, and export them as a numbered, `file:line`-referenced
   prompt to paste back to the agent.

## Waves (sessions)

```bash
repoviz session start --label "wave 3: billing" \
    --allow "src/billing/**" --allow "tests/billing/**" \
    --protect "src/auth/**" --protect "migrations/**"
# ... the agent works, possibly committing ...
repoviz review                      # review the current wave
repoviz review --format prompt      # feedback to paste back to the agent
repoviz session end                 # freezes the wave's end state
repoviz review --list               # current session, past waves, branch, last commit
repoviz review session:<id>         # re-review a past wave at any time
```

A session records the working tree when it starts, including already-dirty
files. Ending it records the end state (end commit plus dirty files). A past
wave is therefore always reviewed exactly as the agent left it, even after you
keep working. Without sessions you can review:

- uncommitted changes (`all`);
- a branch since its merge base (`branch`);
- the last commit (`last-commit`);
- any range (`main...HEAD`, `v1..v2`, or `--base/--head`).

### Submodules

Each Git submodule is a component of its own. Reviews look inside checked-out
submodules:

- **Moved to another commit:** the review lists the commits in between and the
  files they changed.
- **Uncommitted edits inside:** the review lists those files.

Either way, every changed file appears with its full path, for example
`system_modules/cbir/src/config.py`. Scope rules, signals, notes and diffs apply
to it like any other file. A session also records each submodule's commit and any
files already modified inside it, so earlier work is not attributed to the agent.
When the previous commit is not available locally (shallow clone) or the submodule
is not checked out, the review says so instead of listing files.

## Reviewing branches

Agents often work on their own branches (`claude/…`, `feature/…`), sometimes
several at once. You can review **any branch against any other branch**, like a
pull request, without checking it out.

- **Since they diverged** (`A...B`, the default in the app) reviews what `B`
  added since it left `A`: the merge base of `A` and `B` against `B`. Work that
  landed on `A` in the meantime is not shown.
- **Exact difference** (`A..B`) compares the two trees as they are. If `A` moved
  on after `B` was created, `A`'s newer work shows up as if `B` had undone it,
  with the signals that go with it (for example `public-api-removed`). Use it to
  compare two releases or tags.

| Where | How |
|---|---|
| Target list | The current branch against the default branch, plus up to five other recently updated branches that have commits the default branch lacks, as "Branch X vs main (since merge base)". Remote-tracking branches count too (a fresh clone often has only `origin/…`), unless a local branch has the same name. Merged branches are left out. |
| Review tab (live app) | **…or compare any two branches**: base and target boxes suggest local and remote branches, tags, recent commits, `HEAD`, `WORKTREE` and `INDEX`. **⇄** swaps them, and a menu picks *since they diverged* or *exact difference*. The comparison shows up as **⇄ …** in the target list and survives a reload. An unknown branch shows an error and keeps the current review. |
| CLI | `repoviz review main...feature`, `repoviz review main..feature` or `--base A --head B` (exact). |
| API | `GET /api/review?base=A&target=B&mode=merge-base` (or `mode=exact`). An unknown revision returns 400 with the reason. |
| Static report | `repoviz report --review main...feature` (repeatable) adds that review and opens it first. Other comparisons need `repoviz serve`, and the page says so. |

Commit by commit works on branch ranges too: the **Commits** panel lists the
branch's own commits. Notes are keyed by the comparison (`range:main...feature`),
so the app, the listed target and the CLI share them. Revisions are resolved with
`git rev-parse --end-of-options`, so a name can never act as an option, and
nothing is checked out.

## Scope

The scope says which paths the agent was **allowed** to change and which it
**must not touch** (protected). Every changed file is classified as:

| Scope | Meaning | Signal |
|---|---|---|
| `protected` | matches a protected glob | high: *Protected area modified* |
| `out-of-scope` | allowed globs exist and none matches | medium: *Change outside the agreed scope* |
| `allowed` | matches an allowed glob | – |
| `unscoped` | no allowed globs defined | – |

The scope comes from three places, which are merged:

1. `[review]` in `.repoviz.toml` / `pyproject.toml`: the project-wide default.
2. The session: `--allow/--protect` at `session start`, `repoviz session scope`,
   or "Save to session" in the live app.
3. `repoviz review --allow/--protect`, or the scope boxes in the UI. UI edits
   apply instantly, also in static reports.

```toml
[review]
allowed = ["src/**", "tests/**"]
protected = ["src/auth/**", "migrations/**", ".github/**"]
sensitive = true                  # flag CI, lock files, deployment, migrations, .env files
disabled_checks = ["todo"]        # silence signal kinds you do not care about
wiring_ignore = ["src/plugins/**"] # new files a framework loads without an import

[[review.rules]]                  # forbidden dependencies (architecture guardrails)
from = ["src/ui/**"]
to = ["src/db/**"]
message = "UI must go through the service layer"
severity = "high"
```

## Review signals

Signals are **heuristics that guide your attention**, not proof of a bug. Each
has a severity, a category, a location, an excerpt and usually a suggestion.
Credential-like values are always redacted in excerpts and diffs.

| Kind | Severity | Category | What it means |
|---|---|---|---|
| `protected-touched` | high | scope | a protected path was modified |
| `out-of-scope` | medium | scope | a file outside the allowed globs was modified |
| `sensitive-file` | medium | scope | CI, lock file, container, deployment, migration or `.env` changed |
| `parse-error` | high | correctness | the change introduced a syntax error |
| `unresolved-internal-import` / `unresolved-import` | high | correctness | an import now points to a module that does not exist |
| `dangling-call` | high | correctness | a removed function is still called, possibly from a file the agent never touched |
| `stale-callers` | medium | correctness | a signature changed and callers in untouched files were not updated |
| `swallowed-exception` | medium | correctness | `except: pass` / empty `catch {}` added |
| `stub` | medium | correctness | `NotImplementedError`, `todo!()`, "not implemented" added |
| `unwired-module` | medium (low in a library) | correctness | new code that nothing imports or refers to, or a new router (`APIRouter`, `Blueprint`, `express.Router`) that is never registered; see [New code that is not wired in](#new-code-that-is-not-wired-in) |
| `unreachable-from-entry` | info | correctness | a new module imported only by tests, or by other new code that nothing uses |
| `missed-companion` | low (medium when ≥ 80% of ≥ 8 commits) | correctness | a changed file usually changes together with another file that this change left untouched; see [Files that usually change together](#files-that-usually-change-together) |
| `error-handling-removed` | low | correctness | more raise/except/throw/catch lines removed than added |
| `new-cycle` / `cycle-grown` | high | architecture | a dependency cycle was introduced or extended |
| `contract-broken` | contract (default high) | architecture | a new import breaks an architecture contract (`[[contracts]]` or `[[review.rules]]`), with the file, line and, for an indirect violation, the chain. Violations already in the base or in the baseline are not reported. `forbidden-dependency` (its old name) still disables it |
| `contract-fixed` | info | architecture | a violation present in the base is gone |
| `contract-baseline-changed` | medium / info | architecture | the known-violations baseline changed; *medium* when it newly accepts violations, which are then no longer reported |
| `contract-baseline-invalid` | low | architecture | the baseline file is not valid, so every violation is reported |
| `new-component-dependency` | medium | architecture | two components are now coupled |
| `new-package-dependency` | low | architecture | a package now imports a package it never used before |
| `undeclared-dependency` | medium | architecture | an import of a package not declared in any manifest |
| `public-api-removed` | medium | architecture | a public function/class/method was removed (a rename is not a removal: see below) |
| `renamed-symbol-stale-references` | high / medium | correctness | a function or class was renamed, but code still uses the old name, with each location. *High* when a call the analyzer resolved to the old symbol still uses the old name; *medium* when the old name only appears as a word (an import, a use as a value) |
| `renamed-symbol` | low | architecture | a function or class was renamed and no reference to the old name is left |
| `submodule-moved` | medium | architecture | a submodule now lives at another path (same URL or commit, or a similar name in the same folder) |
| `new-external-dependency` | low | architecture | a new third-party import |
| `untested-change` | medium | tests | changed code that no test imports, even indirectly |
| `tests-not-updated` | low | tests | new public code while the tests covering the module were not touched |
| `test-disabled` | high | tests | `skip`/`xfail`/`.only`/`@Disabled` added |
| `assertions-removed` | medium | tests | more assertions removed than added in a test |
| `trivial-assertion` | medium | tests | `assert True`, `expect(true).toBe(true)` |
| `test-deleted` | medium | tests | a test file was removed |
| `secret` | high | security | a credential-like value was added |
| `debugger` | medium | hygiene | `breakpoint()`, `pdb.set_trace()`, `debugger;` |
| `debug-output` | low | hygiene | `print(`, `console.log(` … in non-test code |
| `suppression` | low | hygiene | `# type: ignore`, `# noqa`, `eslint-disable`, `@ts-ignore` … |
| `todo` | low | hygiene | TODO / FIXME / XXX / HACK added |
| `commented-code` | low | hygiene | three or more commented-out code lines |
| `unwired-symbol` | low | hygiene | a new top-level function or class whose name appears nowhere outside its definition |
| `large-change` | info | hygiene | more than 400 lines added to one file |
| `reverted-within-wave` | info | hygiene | commits in the range changed a file that ends up as it started (see [Commit by commit](#commit-by-commit)) |
| `submodule-added` / `submodule-removed` | medium | architecture | a Git submodule was added or removed |
| `submodule-updated` | medium | architecture | a submodule now points to another commit (commits listed when available) |
| `submodule-uncommitted` | medium | correctness | files changed inside a submodule are not committed there, so the superproject cannot record them |

### Renames and moves

A rename used to look like one removal plus one addition, with a "public symbol
removed" signal and a "call to a removed function" for every caller. repoviz now
pairs them again. The file card shows the file once, at its new path
("↦ moved from …"), and its diff is only the actual edit. Key changes say
"↦ was `old_name`", and diagrams label the node "↦ was …". The review asks one
question: is the old name still used anywhere?

```text
[high] Renamed, but the old name is still used: app.errs.ELISException was renamed to
       app.errs.ELIESException, but `ELISException` is still called at app/api.py:5.
```

The old name is looked for in two places:

- **Calls that no longer resolve.** A call the base resolved to the old symbol
  that still uses the old name gives *high*.
- **The renamed symbol's module and its importers,** word by word: imports and
  uses as a value give *medium* ("may still be used").

  Comments, string literals, docstrings and attribute accesses such as
  `parser.parse(...)` on another object are ignored. Methods are only checked
  through calls, because a method name alone is too ambiguous.

The check is bounded: at most 200 renamed symbols per review, and 60 files, each
read once. Beyond that, the `renamed-symbol` signal says the references were not
checked.

Pairing is deliberately cautious:

- Two code files are one file moved only when they share at least two top-level
  names, or have the same file name. Two unrelated migrations that each define
  `class Migration` stay a removal plus an addition.
- A symbol pairs with another by "same signature and size" only when that
  signature has a real parameter and is the only one of its kind on both sides.
- Symbols that only moved with a renamed class or module (same name, same code)
  are not changes. The class rename is reported once.
- A file moved in the same change that deletes some of its functions still lists
  those deletions, and their signals, on its card.

Moving a protected file counts as touching it.

### New code that is not wired in

A frequent agent mistake is to write the new piece and forget to connect it. In
this example the route looks finished in the diff, yet nothing serves it:

```text
[medium] New router is never registered: app/routes/reports.py defines APIRouter `router`,
         but nothing registers it, so its routes are never served.
         Suggestion: Register it in the application (e.g. `app.include_router(reports.router)`
         in `app/main.py`).
[low   ] New code is never used: app.services.report_service.export_pdf is not called or
         referenced anywhere (its module, or the modules that import it).
[info  ] New module not reachable from the application: app/services/report_service.py is
         imported only by app/routes/reports.py, tests/test_report_service.py, which no entry
         point or existing code reaches.
```

Only code *added* in the reviewed range is checked, in Python, JavaScript and
TypeScript. (Go imports whole packages, so a new file has no importer of its own.)

Each of the following counts as wired in:

- **Imports.** Another module imports it. For a router module, an importer must
  also use it; importing it alone does not register it.
- **Entry points.** It runs by itself: `__main__.py`, an `if __name__ ==
  "__main__"` block, a console script, a `package.json` `bin`, and so on.
  Decorated handlers such as `@router.get` do not count, because the module
  still has to be imported to register them.
- **References by name or path.** Its dotted name or path appears in code or
  configuration (not documentation), outside import lines. Examples:
  `INSTALLED_APPS`, a Celery `include` list, a Dockerfile `CMD`, `index.html`,
  `asset('app.js')`.
- **Conventions.** Frameworks load some files without an import:
  - `__init__.py`, `conftest.py`, `settings.py`, `urls.py`, `models.py`, `admin.py`,
    `tasks.py`;
  - migrations and Alembic versions, Django management commands;
  - `scripts/`, `bin/`, `examples/`, `docs/`;
  - Next.js/SvelteKit/Remix route files;
  - `*.config.*`, `*.d.ts`, `*.stories.*`.

  The full list is in `wiring.py` (`WIRED_BY_CONVENTION`). Add your own with
  `review.wiring_ignore`.
- **Loaded like its neighbours.** Some files are loaded by a computed name,
  such as plugins, locales and backends. A new `locale/xx/formats.py` counts as
  wired when none of the existing `locale/*/formats.py` files is imported either.
  The same applies to a new file in a folder whose existing modules nothing imports.

In a **library**, new public modules that only tests use (or nobody uses yet) are
often new API. The repository counts as an application when it has containers,
compose services or a Procfile, or when the new module's component has route or
task handlers. Otherwise `unwired-module` is low severity and the tests-only case
is not reported.

A top-level function or class counts as used when its name appears anywhere
outside its definition: in its module, in the modules that import it, or in the
modules that import those (packages that re-export it). This includes use as a
value, such as `Depends(get_db)` or `callbacks=[handler]`. Methods, decorated
functions, names in `__all__` and conventional names (`main`, `create_app`,
`handler`, ...) are not checked.

### Files that usually change together

Imports miss a whole class of coupling:

- a route and the frontend call that uses it;
- a model and its migration;
- a settings key and `.env.example`;
- a module and its test.

Git history knows these pairs. repoviz reads the file lists of the last
`history.commits` (300) commits, with a single `git log`. It skips merge commits
and commits that touch more than 30 files (renames, reformatting). For each file,
it keeps the partners that changed in at least half of that file's commits:

```text
[medium] Usual companion change missing: app/tasks/panel_extraction.py changed together with
         app/tasks/image_extraction.py in 11 of its last 13 commits; also often with
         app/routes/images.py (10 of 13); those files are untouched in this change.
```

- **Direction.** The share is measured from the changed file's side ("when this
  file changes, does that one change too?"). A file edited in 100 commits is not
  tied to a partner it met 5 times.
- **Which history.** The history is the one before the change: the session's
  baseline commit, the base of a range, or `HEAD` for uncommitted work.
- **Where it shows.**
  - Each review file card lists **Usually changes with**, marking each partner as
    changed or not changed in this review.
  - The **Activity** tab shows the partners of the files being edited that are not
    touched yet (column *Often with*, and the details panel). You can tell the
    agent before it finishes.
- **What is left out.** Generated and vendored partners are never reported, and
  lock files are only reported for manifests.
- **Short history.** A repository with fewer than `history.min_commits` (20)
  usable commits, such as a shallow clone, produces no signal. The report's
  `history` field explains why.
- **From the CLI.** `repoviz coupling` lists the strongest pairs;
  `repoviz coupling --path FILE` lists one file's partners.

Thresholds are in [`[history]`](configuration.md). On Django, learning from 300
commits takes about 0.05 s, and a cached lookup takes a few milliseconds.

## Risk score

On a 50-file wave your attention is the scarce resource. Every changed file gets
a **risk score** from 0 to 100 and a level, so you know which files to read
first and why. It is a small deterministic model (the same input always gives
the same score), not a verdict: each factor that adds points is listed with its
reason.

| Factor | Default points | What counts |
|---|---:|---|
| `signals` | 0–30 | The most severe signal on the file: high 30, medium 15, low 5 (info 0). |
| `fan_in` | 0–20 | Places outside the file that call the changed functions or classes (log scale, full at 32). Callers inside the file pass the change on, so their outside callers count. Test code does not count. For languages without call data, and for module-level changes, modules that import the file count instead. |
| `entry_points` | 0–15 | Entry points (console scripts, `__main__`, route handlers, container commands…) that reach the changed code through calls (log scale, full at 8). |
| `tests` | 0–10 | Changed behaviour that no test imports or calls (10), or whose tests were not updated in this wave (5). |
| `sensitive` | 0–10 | A protected path (10); a sensitive file, such as CI, a lock file, a migration, deployment or `.env` (7); a security-related path such as `auth/`, `permissions`, `crypto` or `login` (7); a path outside the allowed scope (5). |
| `churn` | 0–5 | A churn hotspot before the wave: at or above the 80th percentile of modules by commits in the churn window, and changed at least twice. This is the same rule the Structure tab uses to mark hotspots. |
| `size` | 0–10 | Lines added and removed (log scale, full at 400). A file too large to diff gets full points. |

- **Levels:** *high* from 40, *medium* from 20, *low* below that.
- **Wave risk:** the score and level of the riskiest file. For example, "high
  risk, because of `app/auth/tokens.py` (high signal: Protected area modified;
  protected area)".
- **Bounded:** callers are followed for at most 5,000 nodes per file and
  200,000 per review. When the cap applies, the review says so, and the
  entry-point count is a lower bound.

Where the score shows up:

| Place | What you get |
|---|---|
| AI Review tab | A **wave risk** badge (icon, level and score; click it to open that file), a **Risk** column that sorts the files table by default, and the factors on hover and in each file card. `j` / `k` follow the table order. |
| `repoviz review` | A `risk:` line and "Review first (riskiest files)" with each factor's points. |
| `--format markdown` | The wave risk and a table of the top three files. |
| `--format json` | `risk` for the wave (`score`, `level`, `path`, `summary`, `top`, `counts`, `notes`) and `files[].risk` (`score`, `level`, `factors[]` with `factor`, `points` and `text`). |
| Feedback prompt | "Riskiest files (double-check them)": the top three files at *medium* or *high*, when untriaged signals are included. |
| Automation | `--fail-on risk:high` exits with 3 when the wave risk is high; `risk:medium` when it is medium or high. |

The weights live in `[review.risk]`. They are normalised, so the maximum score is
always 100; set a weight to 0 to ignore a factor. `high` and `medium` move the
level thresholds.

```toml
[review.risk]
signals = 30
fan_in = 20
entry_points = 15
tests = 10
sensitive = 10
churn = 5
size = 10
high = 40      # score from which a file is "high" risk
medium = 20
```

Weights must be numbers ≥ 0, and at least one must be above 0. Thresholds must
be between 0 and 100, with `medium` not above `high`. Unknown keys are reported
in the configuration line of `repoviz discover` and the Structure tab.

## Working through a review

- **Files are ranked for attention.** The file table sorts files by
  [risk](#risk-score), riskiest first. Click a column header to sort by
  something else; your choice is kept while you work. The map shows the most
  relevant files or directories (scope violations, high-severity signals, then
  the largest changes). The rest are
  folded into a "… N more" node, which lists them when clicked.
- **Keyboard:** `j` / `k` open the next / previous file in table order, and `m`
  marks the open file reviewed and moves to the next unreviewed one. The file
  card also has *‹ Prev*, *Next ›* and *✓ Reviewed & next* buttons.
- **Progress:** reviewed files get a ✓ and count towards "N / M reviewed".
  A mark belongs to the file's content: if the agent changes the file again,
  it shows ↻ ("changed since you reviewed it") and must be reviewed again.
  Marks are kept in the browser.
- **Scope edits** apply instantly. *Reset* returns to the configured / session
  scope. *Save to session* (live app) stores your edits with the wave.
  Ctrl+Enter in a scope box applies it.
- **Live app:** coming back to the AI Review tab re-checks the repository
  (instant when nothing changed) and keeps the open file. *↻ Refresh* does the
  same on demand.
- **Nothing to review?** The tab explains how to start a session or which
  target to pick, instead of showing empty diagrams.

### Commit by commit

A wave is often several commits, and a merge can bring in many. When the
reviewed range contains commits, the **Commits** panel lists them oldest first.
Uncommitted work comes last, as "Uncommitted changes" (or "Uncommitted at the
end of the session" for a past wave). Each row shows its files, lines and signals.

- **Review one step.** Click a commit, or use `[` / `]` to step through them.
  Stepping past either end shows the whole wave again, and **Show all** does
  the same.
  - In the live app, the server reviews that commit alone (`parent → commit`):
    its own diff, key changes and signals. This catches a problem that a later
    commit hid, such as a debug `print` added and then removed.
  - In a static report, the wave's files and signals are filtered to the files
    the commit touched. The page says that per-commit diffs need
    `repoviz serve`.
- **Notes stay with the wave.** A note taken while looking at one commit goes
  to the wave's feedback prompt. Scope and reviewed marks are the wave's too.
- **Which commits.**
  - A session lists the commits since its baseline.
  - A range lists `base..target`.
  - `last-commit` on a merge lists the merged commits.

  Merge commits themselves are not listed (a note counts them), and neither are
  commits beyond the 200 most recent ("N earlier commits not shown").
  Each file entry in the JSON report lists the commits that touched it
  (`commits`).
- **Changed, then changed back** (`reverted-within-wave`, info). A file that
  commits changed but that ends up as it started. The agent went back and
  forth: check that the undone work was meant to go.
- **Missing history.** In a shallow clone or with missing objects, the panel
  explains why commits are missing, and the review still works.

```bash
repoviz review last-commit --by-commit       # text output: files and signals grouped by commit
repoviz review session --commit 1a2b3c4d     # one commit of the session on its own
repoviz review session --commit WORKTREE     # only its uncommitted work
```

## Notes and verdicts

| Verdict | Use it when… | Prompt section |
|---|---|---|
| Should not have been modified | the agent touched something outside its task | *Revert* |
| Logic error | the change is wrong | *Fix* |
| Missed / incomplete | a requirement or case is missing | *Complete* |
| Should be improved | it works but should be done differently | *Improve* |
| Question | you need an explanation | *Answer* |
| Looks good / not an issue | dismisses a signal | not included |

Where notes are stored:

- **Live app:** notes are saved in the state directory and shared with
  `repoviz review --format prompt`.
- **Static report:** notes stay in your browser (localStorage).

Either way, **Copy prompt** or **Download .md** exports them.

The prompt lists your notes first, then, optionally, untriaged signals at or
above a chosen severity, and restates the allowed and protected scope.

### Architecture contracts

Contracts (see [configuration.md](configuration.md#architecture-contracts)) turn
"respect the architecture" into something checked on every wave:

```text
[high] Contract broken: Layered backend: app.models.user imports app.routes.api:
       layer 'app.models' is below 'app.routes' and may not import it.  (app/models/user.py:1)
[high] Contract broken: Layered backend: app.models.user imports app.routes.api (through
       app.util.helpers): ...                                          (with allow_indirect = false)
[medium] Known-violations baseline changed: .repoviz-known-violations.json now accepts
       1 more violation(s): Layered backend::app.models.new::app.routes.api.
```

A review reports only what the change introduces: violations in the target that
are neither in the base nor in the baseline. A violation that disappears is
`contract-fixed`. All four signals can be turned off in `review.disabled_checks`.

`repoviz contracts` checks a whole tree, for CI:

```bash
repoviz contracts                        # exit 3 when a violation is not in the baseline
repoviz contracts --format sarif > contracts.sarif   # for code-scanning tools
repoviz contracts --format json
repoviz contracts --baseline > .repoviz-known-violations.json   # accept today's violations
repoviz contracts --suggest              # a layers (or acyclic) contract from the current imports
repoviz mermaid --view dependencies --level module --contracts   # the overlay as Mermaid
```

## Guardrails in automation

```bash
repoviz review session --fail-on protected --fail-on high      # exit 3 on violations
repoviz review session --fail-on risk:high                     # exit 3 when the wave risk is high
repoviz review session --fail-on contract-broken               # exit 3 when the wave breaks a contract
repoviz contracts --format sarif > contracts.sarif             # every violation not in the baseline
repoviz review main...HEAD --format markdown > review.md       # for a PR description
repoviz diff session --fail-on new-cycle --fail-on new-component-dependency
```

### Pull requests and CI

`repoviz review` has three formats for automation. Each one is made from the
same report, so you can analyse once with `--format json -o review.json` and
render the rest with `--from-report review.json` (no Git access, no analysis).
`--fail-on` also works with `--from-report`.

| Format | What it is |
|---|---|
| `sarif` | SARIF 2.1.0 for code scanning: one rule per signal kind and one result per finding. Its level is `error` for high, `warning` for medium and `note` otherwise. Each result carries `partialFingerprints.repovizFinding/v1`, the stable finding id, so code scanning tracks it across pushes. |
| `github` | GitHub workflow commands (`::error file=…,line=…::…`) that show as inline annotations on the changed lines. `--min-severity` (default `low`) sets the lowest severity shown. Values are escaped as GitHub requires. |
| `pr-comment` | Markdown for a pull-request comment. It starts with the hidden marker `<!-- repoviz-review -->`, so a bot can find and update its own comment. It has a Mermaid map of the touched components, or of the files when only one component changed. Protected (🔒), out-of-scope (⚠) and high-signal (❗) nodes are marked in text, and new dependencies use thick labelled arrows. Then come the top signals and every file sorted by risk, folded in `<details>`. `--link-base URL` links each file and line (for example `https://github.com/OWNER/REPO/blob/SHA`). The map is capped at 40 nodes ("… N more") and the comment at 65,000 characters: the file list is shortened first, and the comment says so. |

`repoviz contracts --format github` annotates new contract violations (the ones
not in the baseline) the same way.

The composite action `.github/actions/review` combines these. It makes one
analysis with `--format json`, then:

- writes the comment into the job summary and posts it on the pull request,
  editing the same comment on each push;
- prints the annotations;
- uploads SARIF (optional; needs `security-events: write`);
- applies the `fail-on` gate.

The action takes the inputs `base`, `head`, `fail-on`, `min-severity`,
`comment`, `annotations`, `sarif`, `token` and `python-version`, and has the
outputs `risk` and `report`. It installs repoviz from the action's own ref,
never from the analyzed repository. Check out with `fetch-depth: 0` so both
ends of the pull request are available. The README has a copy-paste workflow.
