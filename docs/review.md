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
5. **Close the loop.** End with a verdict on the wave (approve, request changes
   or reject). An agent waiting with `repoviz review --wait` gets it at once, and
   `repoviz gate` keeps it from pushing until a fresh approval exists
   ([details](#the-verdict-and-the-gate)).

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

### Plan vs actual: expected changes

Scope catches an agent that goes where it should not. The opposite failure is
work it did **not** do: the plan said "update the client, add a test", and the
client was never touched. List what the plan says will change:

```bash
repoviz session start --expect app/routes/reports.py --expect symbol:app.main.create_app --expect test
repoviz session scope --expect migration        # replaces the active session's list
repoviz session start --plan PLAN.md            # extract them from a Markdown plan (asks first; --yes saves)
repoviz review --expect docs                    # one review only (replaces the session's list)
```

An expectation is one of:

- **A path glob:** `app/client.py`, `src/billing/**`, `docs/` (a directory
  means everything under it). It is done when a changed file matches (added,
  modified, removed or moved).
- **A symbol:** `symbol:app.services.images.list_images`. It is done only when
  that function or class is in the wave's key changes, added or modified.
- **A kind:** `test` (a test file with code added or modified), `migration`
  (`migrations/`, `alembic/versions/`, `db/migrate/`…), `docs` (`docs/`,
  Markdown, reStructuredText), `changelog` (`CHANGELOG`, `CHANGES`, `NEWS`,
  `changelog.d/`).

The review then shows:

- a **Plan vs actual** card, with each expectation **done** (with the files or
  symbols that matched) or **not changed**;
- the `expected-not-changed` signal (medium, category `plan`) for each missing
  one. The feedback prompt starts with "The plan listed X, but it was not
  changed.";
- **not in the plan**: when the plan names files or symbols (not only kinds),
  files with more than 50 changed lines that no expectation covers.

**Importing a plan.** `--plan PLAN.md` (or *Import plan…* in the live app)
reads the Markdown as text. Nothing is executed.

- **Backticked paths** that exist become expectations. So do new ones on a
  line that says *create*, *new* or *add*.
- **Backticked dotted names** that resolve to a function, class or module
  become expectations. A unique suffix is enough, for example
  `routes.reports.export`.
- **List items** that mention tests, migrations, docs or the changelog add
  those kinds.
- **Code blocks are skipped.** What cannot be resolved (a missing file, an
  unknown or ambiguous name) is listed as *unresolved*, with its line, rather
  than dropped.
- **Confirmation first.** The CLI prints the list and asks (or needs `--yes`);
  the page shows it before *Use these*.

Expectations and the plan's unresolved lines are stored with the session, so
past waves keep their plan. The static report shows the card; its list can be
edited on the page, but importing and saving need `repoviz serve`.

### Checkpoints: one step at a time

Agents often work for a long time without committing. A **checkpoint** records
the working tree at one moment of a session, so you can review what happened
between two moments instead of the whole wave:

```bash
repoviz session checkpoint --label "after step 2"   # record the working tree now
repoviz session timeline                             # checkpoints and notes, oldest first
repoviz review checkpoint:2-3                        # checkpoint 2 → 3: that step alone
repoviz review checkpoint:3                          # everything since checkpoint 3
repoviz review checkpoint:0-1                        # the baseline → checkpoint 1
repoviz session note --tool Edit --file app/x.py --message "added retry"   # a timeline event, no files
```

- **Idempotent.** Recording when nothing changed since the previous checkpoint
  does nothing, so calling it often is harmless. A checkpoint stores the commit
  checked out plus copies of the files that differ from it (private, 0600, in
  the state directory; identical contents are stored once). Commits made in
  between are part of the step.
- **Automatic.** While the live app's page is open, `repoviz serve` records an
  *automatic* checkpoint whenever the working tree changed, at most every
  `[activity] checkpoint_seconds` (30 s; 0 turns it off).
- **Bounded.** A session keeps at most `[activity] max_checkpoints` (200). The
  oldest automatic ones go first, and a removed checkpoint's changes are folded
  into the next one, so the timeline still adds up. `repoviz session prune
  --days 30` removes the checkpoints of sessions that ended more than 30 days
  ago; their baseline and end state stay, so those waves can still be
  reviewed.
- **Review targets.** `repoviz review --list` and the AI Review tab offer
  *Since checkpoint N* and *Last step: checkpoint N-1 → N* for the active
  session. Any other step is `checkpoint:A-B` (or
  `checkpoint:<session id>:A-B` for a past session).
- **In the live app:**
  - The Activity tab shows a timeline of the checkpoints and notes, newest
    first. Each entry shows the files changed since the previous checkpoint and
    their ±lines. Files changed in 3 or more checkpoints get a **reworked ×N**
    pill.
  - Clicking a checkpoint opens that step in AI Review.
  - *Mark checkpoint* (Activity and AI Review tabs) records one now.
- **In a static report:** the timeline of the active or last session is
  embedded. Reviewing one step needs `repoviz serve`, and the page says so.

**Agent hook recipe (Claude Code).** A `PostToolUse` hook records a checkpoint
after every edit, labelled with the tool and the file. Add this to
`.claude/settings.json` in the repository (or to your user settings):

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|MultiEdit|Write|NotebookEdit",
        "hooks": [
          { "type": "command", "command": "repoviz session checkpoint --hook-input --quiet" }
        ]
      }
    ]
  }
}
```

`--hook-input` reads the hook's JSON from stdin (`tool_name`,
`tool_input.file_path`, `cwd`), and `--quiet` prints nothing. Without an
active session the command does nothing and exits 0, so the hook never gets in
the agent's way.

The command is answered without loading the analysis code. On a Django-sized
working tree it takes about 0.12 s when nothing changed and 0.16 s when it
records a checkpoint, because only files whose size or modification time
changed are read again. Other agents with post-edit hooks can call the same
command, with `--label` or `--tool` / `--file` instead of `--hook-input`.

The hook is the agent calling repoviz. repoviz itself never runs repository
code, and a checkpoint only reads files.

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

## Parallel agents (worktrees)

When several agents work at once, each in its own `git worktree` on its own
branch, repoviz shows all of them:

```bash
git worktree add ../wt-search -b agent/search      # one worktree per agent (repoviz never creates them)
repoviz fleet                                      # every worktree, and where their work overlaps
repoviz fleet --json                               # the same, machine-readable
repoviz fleet --risk                               # also each wave's risk (reviews every worktree: slower)
```

```text
3 worktree(s); default branch main

* shop       main              ahead   0  uncommitted   0  active 2026-09-26T14:53
  wt-a       agent/a           ahead   1  uncommitted   0  session “agent a” since 2026-09-26T14:55  verdict: approved
  wt-b       agent/b           ahead   0  uncommitted   3  active 2026-09-26T14:53

Overlaps between worktrees (each from its merge base with the default branch):
  wt-a ↔ wt-b: 1 symbol, 1 contract, 1 file
    [high] same symbol: search in app/search.py (wt-a line 1, wt-b line 1)
    [high] wt-a changes the signature of search (app/search.py:1) (q, limit=10) → (q, *, limit=10, offset=0); wt-b calls it at app/api.py:5
    [medium] same file: README.md
```

- **Which worktrees.** `git worktree list`, keeping only worktrees that still
  exist, pass Git's ownership check and belong to this repository (same
  `git rev-parse --git-common-dir`). The others are listed under "Not listed"
  with the reason.
- **Each worktree's work** is everything it changed since its merge base with
  the default branch, committed or not. The fleet shows, per worktree:
  - its branch and the commits ahead of the default branch;
  - the uncommitted files;
  - the active session, and the last activity (the latest commit or edit);
  - the verdict of its wave (session, else branch, else uncommitted changes),
    and whether it is stale;
  - with `--risk`, the wave's risk.
- **Overlaps** compare every pair of worktrees:
  - the same definition changed on both sides is `overlap-symbol`. Definitions
    are parsed as text: Python, JavaScript and TypeScript. A class counts only
    when its own code changed, not when two agents edit different methods;
  - a signature changed on one side while the other side's new lines call it
    is `overlap-contract`;
  - otherwise, the same file is `overlap-file`.
- **In a review**, the same three signals appear on the files this wave shares
  with another worktree, naming it ("also changed by worktree wt-b (branch
  agent/b)"). They are checked for reviews whose target is the working tree
  (session, uncommitted changes, branch), not for past commits. They can be
  disabled in `review.disabled_checks`.
- **Sessions, notes and verdicts are per worktree**: the state directory is
  keyed by the worktree's path. The parse cache is shared, since results are
  keyed by content, so a worktree opened for the first time is fast.

In the live app, the header shows **N worktrees** and a **Worktree** picker.
Every tab then reads the chosen worktree, for this browser tab. The server
only opens this repository's worktrees and refuses any other path. The
Activity tab's **Parallel agents** card lists the worktrees (click one to
switch) and shows the overlaps as a worktree × worktree matrix. A cell shows
what two waves share, with an icon and words; click it for the list. A report
embeds the same card, read-only.

On a Flask-sized repository with five worktrees, `repoviz fleet` takes about
0.4 s in a fresh process.

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
| `expected-not-changed` | medium | plan | something the plan listed was not changed: a path, a symbol, or a kind (test, migration, docs, changelog). Example: `Expected change missing: test — The plan listed test, but it was not changed.` See [Plan vs actual](#plan-vs-actual-expected-changes) |
| `new-external-dependency` | low | architecture | code imports a third-party package it did not use before (a package a manifest declares is `dependency-added`; the two point at each other) |
| `dependency-added` | low / medium | dependencies | a manifest declares a new direct dependency. *Medium* when it is a runtime dependency new to the repository. A new manifest gives one signal for all its packages. Example: `Dependency added: requests — requests >=2.31,<3 (runtime), resolved to 2.32.3; new to the repository. First imported by app/client.py:2.` See [Dependencies](#dependencies-declared-and-resolved) |
| `dependency-downgraded` | medium | dependencies | a declared or resolved version went down: `rich: ==13.7.0 → ==13.6.0` |
| `dependency-unpinned` | medium | dependencies | a spec went from pinned or bounded to one that accepts any future version (`*`, `latest`, no version, `>=x` with no upper bound), or a new dependency is unbounded while the rest of the file pins: `click: >=8,<9 → >=8` |
| `dependency-source-changed` | high | security | a dependency now comes from a Git repository, a URL, a path outside the repository, an npm alias or a non-default registry, or a package index was added: `lodash now comes from a Git repository: git+https://github.com/lodash/lodash.git (was ^4.17.21)` |
| `lockfile-without-manifest` | medium | dependencies | a lock file resolves other versions but no manifest it belongs to changed: an upgrade run or a manual edit |
| `manifest-without-lockfile` | low | dependencies | a manifest's dependencies changed but its lock file (same directory, or the nearest parent for workspaces) did not |
| `new-runtime-dependency` | low | architecture | code now starts a container built by this repository, or calls one of its services (`app.search now talks to the cbir-service service (http:8000)`), found without imports (see [data-model.md](data-model.md#runtime-coupling-from-code)) |
| `untested-change` | medium | tests | changed code that no test imports, even indirectly. Not raised for a file whose changed lines a fresh coverage report measured |
| `changed-lines-uncovered` | medium | tests | a fresh coverage report shows changed executable lines no test ran (at least `[review.coverage] min_uncovered`, default 1). Example: `Changed lines no test runs — 1 of 4 changed executable line(s) are not run by any test, according to coverage.xml (line(s) 7)` at `app/calc.py:7`. See [Coverage reports](#coverage-reports) |
| `coverage-stale` | info | tests | a coverage report is older than some changed files, so it cannot tell whether their new lines run: "re-run your test suite with coverage to refresh it" |
| `tests-not-updated` | low | tests | new public code while the tests covering the module were not touched |
| `test-disabled` | high | tests | `skip`/`xfail`/`.only`/`@Disabled` added |
| `assertions-removed` | medium | tests | more assertions removed than added in a test |
| `trivial-assertion` | medium | tests | `assert True`, `expect(true).toBe(true)` |
| `test-deleted` | medium | tests | a test file was removed |
| `secret` | high | security | a credential-like value was added |
| `safety-flag-weakened` | medium | security | a well-known safety setting was switched the risky way, in code or a configuration file (see [Values](#values-constants-and-settings) for the rules). Example: `Safety setting weakened: DEBUG — debug mode switched on (DEBUG: False → True)` at `app/settings.py:4`. Not raised in test files, nor when the value already was risky |
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
| `overlap-symbol` | high | coordination | another worktree changes the same function, method or class (Python, JavaScript, TypeScript). Example: `Same code changed in another worktree — search is also changed by worktree wt-a (branch agent/a)` at `app/search.py:1`. See [Parallel agents](#parallel-agents-worktrees) |
| `overlap-contract` | high | coordination | one worktree changes a function's signature while another one's new code calls it; reported on both sides. Example: `Calls a function another worktree is changing — worktree wt-a (branch agent/a) changes the signature of search (app/search.py) from (q, limit=10) to (q, *, limit=10, offset=0), and this wave calls it here` at `app/api.py:5` |
| `overlap-file` | medium | coordination | another worktree changes the same file, but not the same definition (or in a language repoviz does not parse for definitions) |

### Coverage reports

When a coverage report already exists (from CI, or `pytest --cov
--cov-report=xml`, `jest --coverage`, `go test -coverprofile=cover.out`…), the
review uses it for the lines the agent changed. repoviz only reads the report;
it never runs the tests.

- **Patch coverage.** Each changed file's card says how many of its changed
  executable lines a test ran: "3 of 4 changed executable line(s) run by a
  test · not run: line(s) 7". The diff marks those lines ● (run) or ○ (not
  run), each with a tooltip. The review header shows the wave's patch coverage
  ("75% · 3/4"), and so do the text, Markdown and pull-request outputs.
- **Signals.** `changed-lines-uncovered` flags files with changed lines no test
  ran. A file the report measured no longer gets the static `untested-change`
  guess. Its risk factor becomes the share of changed lines not run.
- **Only a fresh report counts.** A report older than a changed file describes
  the old code, so that file's changed lines are "unknown": the card says so,
  and one `coverage-stale` note asks you to re-run your tests with coverage.
  For a commit or a past wave, the file on disk must also be the reviewed
  content.
- **Setup.** See [configuration.md](configuration.md#coverage-reports) for the
  paths, formats and path mapping, and for `min_uncovered`.

### Values: constants and settings

Agents tweak limits and flags to make things pass. A file's key changes
therefore also list its values, before → after:

```text
app/services/extract.py:12  MAX_IMAGES_PER_EXTRACTION: 20 → 200
app/settings.py:4  DEBUG: False → True  ⚠ debug mode switched on
config/app.toml:5  server.port: 8000 → 9000
```

What counts as a value:

- **Python.** Module-level UPPER_CASE names bound to a literal (`NAME = 20`,
  `NAME: int = 20`), and the literal defaults of settings classes (subclasses of
  `BaseSettings` or `…Settings`, dataclasses and models named `…Settings` or
  `…Config`), such as `Settings.debug`.
- **JavaScript / TypeScript.** `export const name = literal` and top-level
  `const UPPER_NAME = literal`.
- **Configuration files** under config-like paths (`config/`, `settings/`,
  `*.config.*`, `settings.*`), and `.env.example` / `.env.sample`. Scalar keys
  at the top level and one section down (`server.port`) are listed.

Values are parsed, never evaluated: `ast.literal_eval` on the syntax tree,
`json`, `tomllib` and the YAML subset.

- A computed value (`URL = BASE + "/api"`) is not listed.
- Values are shortened to 120 characters and redacted. A change past the
  120th character still counts: values are compared by a hash of their full
  text.
- A value whose name looks secret (`API_KEY`, `password`, `token`…) shows as
  `•••`, unless it is a flag or a quantity (`MAX_TOKENS = 4096`).

Only files that exist on both sides are compared: the constants of a new file
are not listed one by one. At most 300 changed files are compared per review
(about 3 ms each). Beyond that, the file card and `repoviz review` say that
the rest were not compared.

**Safety rules.** `safety-flag-weakened` (medium, category security) is raised
when a value's last name part (`DEBUG`, `Settings.debug`, `server.verify_ssl`)
matches a rule and the new value is the risky one, while the old value was
not:

| Name | Risky value | Meaning |
|---|---|---|
| `DEBUG`, `*_DEBUG` | true, `"1"`, `"on"`… | debug mode switched on |
| `*SSL*`, `*TLS*`, `CHECK_CERT(S)`, `CERT_REQS` | false | TLS or certificate verification switched off |
| `VERIFY`, `VERIFY_*`, `*_VERIFY_*`, `*VERIFICATION*` | false | verification switched off |
| `*ALLOW_ALL*` | true | allow-all switched on |
| `TIMEOUT`, `*_TIMEOUT` | 0 or none | timeout removed |
| `*CORS*`, `ALLOWED_ORIGINS`, `ALLOW_ORIGINS`, `ALLOWED_HOSTS` | `*` | any origin or host allowed |
| `CSRF*`, `AUTH*`, `*_SECURE`, `RATE_LIMIT*` (as words) | false | a security check switched off |

The rules are `SAFETY_FLAGS` in `values.py`. Test files never raise it. Turn
it off with `review.disabled_checks = ["safety-flag-weakened"]`; the values are
still listed.

The review reads both versions of each changed file, so snapshots do not grow.
Values are not drawn in diagrams. They appear in:

- the file card's key changes, in the AI Review tab (live app and static
  report);
- `repoviz review` ("Values changed") and its Markdown;
- the pull-request comment (`--format pr-comment`, up to 30 rows);
- the JSON: `files[].values` (`name`, `kind`, `status`, `value_before`,
  `value`, `line`, and `weakens` when a safety rule trips) and
  `summary.values_changed`.

### Dependencies: declared and resolved

Agents add, upgrade and swap packages to get something working, and a
3,000-line lock-file diff hides which. A manifest's or lock file's card
therefore lists its **dependency changes**, package by package:

```text
dependencies: +1 −0 ↑0 ↓1, 2 other change(s)
pyproject.toml:9  requests: added >=2.31,<3 (resolved 2.32.3)
pyproject.toml:6  rich: downgraded ↓: ==13.7.0 → ==13.6.0 (resolved 13.7.0 → 13.6.0)
pyproject.toml:7  click: changed: >=8,<9 → >=8  [unpinned]
web/package.json:4  lodash: source changed ⚠: ^4.17.21 → git+https://github.com/lodash/lodash.git
uv.lock  … and 1 indirect package(s) resolved differently
```

**Manifests.** `pyproject.toml` (PEP 621, Poetry, uv sources), `setup.cfg`,
`setup.py`, `requirements*.txt`, `Pipfile`, conda, `package.json`,
`Cargo.toml`, `go.mod`, `composer.json`, `Gemfile` and `pubspec.yaml` are
compared by package name and scope:

- added, removed, upgraded, downgraded, changed;
- moved to another scope (dev ↔ runtime);
- **source changed**: now from a Git repository (`git+…`, `github:user/repo`,
  `user/repo`, `{ git = … }`), a URL, a path outside the repository, an npm
  alias (`npm:other@1`) or a non-default registry (`registry = …`, a Poetry or
  uv index);
- a package index added (`--index-url` / `--extra-index-url` /
  `--find-links` in `requirements*.txt`, `[[tool.uv.index]]`,
  `[[tool.poetry.source]]`).

Packages of this repository (workspace members, paths inside it) are not
third-party and are not listed.

**Versions** are compared best effort, with the standard library: a spec's
version is its highest lower bound or pin (`^4.17.21` → 4.17.21, `>=1.2,<2` →
1.2); pre-releases sort before releases.

**Lock files.** `package-lock.json` / `npm-shrinkwrap.json` (v1 to v3),
`yarn.lock` (classic and Berry), `pnpm-lock.yaml` (v5 to v9), `poetry.lock`,
`uv.lock`, `pdm.lock`, `Pipfile.lock`, `Cargo.lock`, `go.sum` and
`composer.lock`:

- They are parsed as text (JSON, TOML, line patterns), never run.
- Resolved versions of the **direct** dependencies are listed: the names the
  matching manifests declare, plus what the lock file records itself (npm,
  pnpm, uv). The others are counted ("indirect packages resolved
  differently").
- A package the manifest's change already lists shows its resolved version
  there (`resolved 2.32.3`) and is marked *declared in* on the lock file's
  card.
- A lock file of 20 MB or more is skipped, with a note; so is one that does
  not parse.

The review header, `repoviz review`, its Markdown, the pull-request comment
and the JSON (`summary.dependencies`, `files[].packages`, `files[].lock`)
all carry the counts: `+added −removed ↑upgraded ↓downgraded`, and other
changes. A package changed in a manifest and its lock file counts once.

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
- **Large waves are grouped.** From 20 files in at least two components, the
  table groups files by component. Each group line shows its files, lines added
  and removed, and its highest signal severity (icon and word).
  - A submodule's own entry heads the group of the files changed inside it.
  - Inside a group, paths start at the component's folder (`…/cbir/src/search.py`).
    The full path is in the tooltip and behind *Copy path* on the file card.
  - *group by component* turns grouping on or off.
- **Search and filter.** The search box (`/`) matches paths, components and
  signal titles. The component chips (with counts) filter the table, several at
  once, together with the search. The query, chips, grouping and collapsed
  groups are kept in the browser (`rv.list.review`).
- **Keyboard:** `j` / `k` open the next / previous file in table order, skipping
  collapsed groups. `o` collapses or expands the open file's group. `m` marks
  the open file reviewed and moves to the next unreviewed one. The file card
  also has *‹ Prev*, *Next ›* and *✓ Reviewed & next* buttons.
- **Progress:** reviewed files get a ✓ and count towards "N / M reviewed".
  A mark belongs to the file's content: if the agent changes the file again,
  it shows ↻ ("changed since you reviewed it") and must be reviewed again.
  Marks are kept in the browser and, in the live app, in the state directory,
  so they survive a change of browser and count towards
  `repoviz gate --require-all-reviewed`.
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

## The verdict and the gate

A review ends with a **verdict** on the whole wave: *Approve*, *Request
changes* or *Reject*, with an optional summary. The **Verdict** bar is at the end
of the AI Review tab in the live app. Once given, the verdict shows as a banner
at the top of the tab, with the reviewer and the time, and the bar lists the
earlier verdicts on the same review. Each verdict has its own icon and word.

A verdict is tied to the exact state you reviewed: a fingerprint of the
reviewed changes (every changed path with its content on both sides).

- **Stale.** If the files change afterwards, the verdict is stale. The banner
  says so (dashed border, alert icon, "Stale:"), and the gate refuses it.
- **Still fresh.** Committing the reviewed work inside a session, or ending the
  session, does not change what was reviewed, so the verdict stays fresh.
- **Submitted on an old page.** A verdict submitted from a page loaded before
  the agent changed something is recorded for what the page showed, so it is
  stale at once.

Verdicts, notes and "reviewed" marks live in the state directory
(`reviews/<key-hash>.verdict.json` and `.reviewed.json`, private files), never
in the repository.

### An agent waits for the verdict: `repoviz review --wait`

```bash
repoviz review --wait --format json            # blocks until you submit a verdict
repoviz review --wait --open --timeout 30m     # and opens the review in a browser
```

- **Server.** It reuses the running `repoviz serve` for this repository (the
  server registers itself in the state directory). Otherwise it starts one on
  `--port` (default 8765, or another free port) for as long as it waits.
- **Waiting.** It prints the link to the review on stderr
  (`http://127.0.0.1:8765/#tab=review&review=session`), then polls the state
  file. It opens no new network listener besides the local server.
- **Result.** When you submit a verdict, it prints it and exits:

  | Exit | Verdict |
  |---|---|
  | 0 | approve |
  | 2 | request changes |
  | 3 | reject |
  | 4 | none before `--timeout` (default `30m`; `0` waits forever) |

- **Asking again.** A verdict already given on exactly this state is returned at
  once, so asking again without changing anything gives the same answer.

```json
{
 "target": {"id": "session", "label": "Current session: wave 9: image limits", "key": "session:20260926T143459Z-6df4df"},
 "waited": true, "url": "http://127.0.0.1:8791/#tab=review&review=session",
 "verdict": "request-changes", "label": "Changes requested", "exit_code": 2,
 "summary": "Revert the settings change; the new helper is fine but needs a test.",
 "reviewer": "Phillipe", "at": "2026-09-26T14:35:46+00:00", "stale": false,
 "notes": [{"path": "app/config/settings.py", "line": 31, "verdict": "should-not-touch",
            "text": "Do not rename the extraction directory: existing documents point to it."}],
 "prompt": "# Review feedback: Current session: wave 9: image limits\n\n..."
}
```

- `prompt` is the feedback prompt the page showed when you submitted: the text
  *Copy prompt* copies.
- `--format text` prints the verdict, the summary and, unless the work is
  approved, the prompt.

Coding agents' shell tools often stop a command after a few minutes. If the
agent can, run the command in the background (Claude Code's Bash tool can, and
tells the agent when it exits). Otherwise use a short `--timeout` and call it
again on exit 4.

### Before a push: `repoviz gate`

```bash
repoviz gate                           # exit 0 when a fresh verdict approves the work, else 3
repoviz gate --require any             # any fresh verdict will do (the human looked)
repoviz gate --require-all-reviewed    # and every changed file is marked reviewed
repoviz gate --max-open high           # and no high signal is left without a note
repoviz gate --json                    # the result, machine-readable, on stdout
```

The gate is closed, with one line per reason, when:

- there is no verdict ("no verdict on “…”: ask the human to review it");
- the files changed since ("verdict is stale: files changed since review");
- the verdict is not an approval (with `--require approve`, the default);
- with `--require-all-reviewed`, a changed file is not marked reviewed at its
  current version;
- with `--max-open SEVERITY`, a signal at or above it has no note. *Send to
  agent*, a comment and *Not an issue* all count as a note.

Like `review`, it takes a target (default: the current session, else
uncommitted changes). Without a session, gate the branch (`repoviz gate branch`)
or review before committing: a verdict on "uncommitted changes" goes stale once
they are committed. On a Django-sized working tree the gate takes under a
second.

repoviz never installs hooks. Pick the ones you want:

- **Agent instructions** (`AGENTS.md`, `CLAUDE.md`):

  ```markdown
  ## Review before pushing
  - When a task is done, run `repoviz review --wait --format json` and wait for the verdict.
    Exit 0: approved. Exit 2: address `summary` and every item in `prompt`, then run it again.
    Exit 3: stop and ask the human. Exit 4: nobody answered yet; run it again.
  - Before `git push`, run `repoviz gate`. If it fails, do not push: ask the human to review.
  ```

- **Claude Code hook.** A `PreToolUse` hook blocks `git push` while the gate is
  closed, and the reasons go back to the agent. In `.claude/settings.json`:

  ```json
  {
    "hooks": {
      "PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "repoviz gate --hook-input"}]}
      ]
    }
  }
  ```

  `--hook-input` reads the hook's JSON from stdin. Only commands that run
  `git push` are gated (also `git -C dir push`). A closed gate exits 2, which
  blocks the command and shows the reasons to the agent. Otherwise the hook
  prints nothing.

- **Git pre-push hook** (`.git/hooks/pre-push`, executable):

  ```sh
  #!/bin/sh
  exec repoviz gate --quiet
  ```

The gate is a workflow guard, not a security boundary: anything that runs as
you can write the state directory. Only the page records verdicts (there is no
command that approves), and each verdict records who gave it and when.

### In a static report

A static report shows the verdict read-only, and whether it is stale, but
cannot record one: it writes no state. Its bar becomes **Copy verdict as
JSON**. Pick a verdict and copy it, with the summary, your notes, the prompt and
the state's fingerprint, to paste to the agent or attach to a pull request. The
page says that recording a verdict for `review --wait` and `gate` needs
`repoviz serve`.

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
| `github` | GitHub workflow commands (`::error file=…,line=…::…`) that show as inline annotations on the changed lines. `--min-severity` (default `medium`) sets the lowest severity shown. Values are escaped as GitHub requires. |
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
