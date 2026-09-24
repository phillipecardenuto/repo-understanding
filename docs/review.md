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
| `forbidden-dependency` | rule | architecture | a new dependency violates a `[[review.rules]]` rule |
| `new-component-dependency` | medium | architecture | two components are now coupled |
| `new-package-dependency` | low | architecture | a package now imports a package it never used before |
| `undeclared-dependency` | medium | architecture | an import of a package not declared in any manifest |
| `public-api-removed` | medium | architecture | a public function/class/method was removed |
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

## Working through a review

- **Files are ranked for attention.** The file table sorts files with signals
  first. The map shows the most relevant files or directories (scope
  violations, high-severity signals, then the largest changes). The rest are
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

## Guardrails in automation

```bash
repoviz review session --fail-on protected --fail-on high      # exit 3 on violations
repoviz review main...HEAD --format markdown > review.md       # for a PR description
repoviz diff session --fail-on new-cycle --fail-on new-component-dependency
```
