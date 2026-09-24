# Changelog

All notable changes to repoviz. Versions follow [semantic versioning](https://semver.org/).

## [Unreleased]

### Added

- **Review any branch against any other branch** ([#35](https://github.com/phillipecardenuto/repo-understanding/issues/35)).
  - The AI Review tab gets a **compare any two branches** control. Base and
    target suggest local and remote branches, tags, commits and the working
    tree. It has a ⇄ swap and two modes:
    - *since they diverged* (merge base, like a pull request; the default);
    - *exact difference*.

    The comparison is kept across reloads, and an unknown branch keeps the
    current review on screen.
  - The target list now includes recently updated branches with commits the
    default branch lacks.
  - Clear labels ("feature since it left main (merge base …)"), shared notes
    between the app and `repoviz review main...feature`, and
    `GET /api/review?...&mode=merge-base|exact`.
  - `repoviz report --review SPEC` adds comparisons to a static report, and the
    static page explains how to compare other branches.

  Before this, the app's base/target boxes always compared the exact trees. On a
  branch whose base had moved on, the base's newer work showed up as undone by
  the branch.
- **Explainable risk score per file and per wave** ([#5](https://github.com/phillipecardenuto/repo-understanding/issues/5)).
  - Every changed file gets a score from 0 to 100 and a level (high, medium,
    low), with each factor and its points: signal severity, callers of the
    changed code, entry points reaching it, missing or stale tests, protected or
    sensitive paths, churn hotspots and size. The wave's risk is its riskiest
    file.
  - The AI Review tab shows a wave risk badge and a Risk column that sorts the
    files by default; `j` / `k` follow that order and each file card lists the
    factors.
  - `repoviz review` prints "Review first (riskiest files)"; JSON and Markdown
    include the risk; the feedback prompt lists the riskiest files.
  - New gate `--fail-on risk:high` (or `risk:medium`); weights and thresholds
    in `[review.risk]`, validated.
- **Renames and moves are recognised** ([#3](https://github.com/phillipecardenuto/repo-understanding/issues/3)).
  - The diff pairs removed and added files, folders, submodules and
    functions/classes that are the same thing under a new name or path. They
    become one node (`before.previous_id`), and their edges and cycles carry
    over.
  - Reviews show a moved file once, with only its real edit.
  - New signals: `renamed-symbol-stale-references` (high: the old name is still
    used, with each location), `renamed-symbol` (low) and `submodule-moved`.
    A rename no longer raises `public-api-removed` or `dangling-call`.
  - Diagrams label renamed nodes "↦ was …".
- **Review a wave commit by commit** ([#2](https://github.com/phillipecardenuto/repo-understanding/issues/2)).
  - The AI Review tab gets a **Commits** panel: the range's commits, oldest
    first, then uncommitted work, each with files, lines and signals.
  - Selecting one (or pressing `[` / `]`) reviews that step alone. The live app
    shows its own diff and signals; a static report filters to the files it
    touched. Notes still go to the wave's feedback.
  - New info signal `reverted-within-wave` for files changed and later
    restored.
  - CLI: `repoviz review --commit SHA` and `--by-commit`.
  - API: `GET /api/review?...&commit=SHA`.
- **New code that is not wired in** ([#4](https://github.com/phillipecardenuto/repo-understanding/issues/4)).
  Three new review signals:
  - `unwired-module`: a new module nothing imports or refers to, or a new
    router (`APIRouter`, `Blueprint`, `express.Router`) that is never
    registered. The suggestion names the file where routers are registered.
  - `unwired-symbol`: a new function or class that is never used.
  - `unreachable-from-entry`: new code used only by tests or by other unused
    new code.

  Framework conventions, references by name or path, and plugin-style
  neighbours count as wiring. `review.wiring_ignore` adds your own exceptions.
- **Files that usually change together** ([#6](https://github.com/phillipecardenuto/repo-understanding/issues/6)).
  repoviz learns from recent Git history which files change together.
  - New review signal `missed-companion`: an edited file's usual partner was left
    untouched (a migration, a test, a client...).
  - Review file cards list **Usually changes with**; the Activity tab shows the
    partners not touched yet.
  - New command `repoviz coupling` lists the strongest pairs.
  - Thresholds go in the new `[history]` configuration table.

### Fixed

- Commit subjects and authors are redacted wherever reviews show them, including
  the "Last commit: …" target label.

### Documentation

- **Feature wishlist** ([docs/wishlist.md](docs/wishlist.md)). It surveys
  similar tools and tracks 33 improvements as GitHub issues, with priorities and
  a suggested order.
- **`AGENTS.md`**, a guide for coding agents (and people) working on repoviz:
  code map, invariants, tests and when a change counts as done. `CLAUDE.md`
  imports it.

## [0.1.0] - 2026-09-24

First release: a local, read-only tool to understand a repository's architecture
and to supervise AI coding agents feature by feature.

### Understand a repository

- **Automatic discovery for any repository.** It finds languages, manifests and
  lock files, projects and workspaces, source, test and docs roots, generated
  and vendored code, entry points, containers, CI, deployment files, and Git
  submodules. `.repoviz.toml` or `[tool.repoviz]` can override any of it.
- **Plugin analyzers.**
  - Filesystem and Git.
  - Manifests: Python, npm/pnpm/yarn, Cargo, Go, Maven/Gradle, .NET, Composer,
    Ruby, Dart, Elixir, Swift and CMake.
  - Python (AST, with an optional grimp cross-check), JavaScript/TypeScript,
    and Go.
  - Static call flow.

  Unsupported languages still appear as structure.
- **A normalized model** with collision-resistant IDs, source evidence and cycle
  detection at module, component and project level.

### Web app and reports

- **Five tabs, all drawn with Mermaid:** AI Review, Changes, Structure,
  Dependencies, and Activity & Flow.
- **Visual conventions that never rely on colour alone,** plus a colour-coded
  line-icon set that looks the same on every OS and theme.
- **Two ways to use it:**
  - a live app (`repoviz serve`) that analyzes on demand;
  - a self-contained offline HTML report (`repoviz report`).
- **An in-app guide.** The **Help** button (or `?`) opens a searchable guide
  covering the recommended workflow, each tab's best use, diagram conventions,
  keyboard shortcuts, live vs static, privacy and troubleshooting. Each tab
  starts with a one-line hint that links to its section.

### Compare states

- **Comparisons:** HEAD vs working tree, staged, unstaged, a work session, a
  branch since its merge base, or any two revisions.
- **For each change:** why a node changed, dependencies and cycles introduced
  or resolved, and changes limited to formatting.
- **Git submodules:** pointer moves, with the commits and files in between, and
  uncommitted work inside submodules.

### Supervise AI agents (AI Review)

- **Work sessions ("waves").** A session records a baseline, including files that
  are already modified and submodule state, so only the agent's work is reviewed.
  Ending it freezes the end state, so the wave can be reviewed again later.
- **An agreed scope** (allowed and protected globs), from the configuration, the
  session or the UI.
- **A map of where the agent went,** and a change card per file with key
  changes, dependency changes, affected tests and the diff.
- **Review signals.** Among them:
  - scope violations, broken imports, calls to removed functions, and callers
    not updated after a signature change;
  - cycles, disabled tests, secrets, swallowed exceptions and debug output;
  - submodule changes.
- **Triage and feedback.** Notes on signals, files and diff lines, review
  progress with `j` / `k` / `m`, and a numbered feedback prompt to paste back to
  the agent.
- **For automation:** `repoviz review --fail-on …` and
  `repoviz diff --fail-on …`.

### Safety and performance

- **Read-only.** It never modifies the repository and never executes its code.
  Git runs with overrides that stop a repository's own configuration (fsmonitor,
  filters, GPG) from starting programs.
- **Local server hardening:** a Host allow-list, a custom header required on
  every API call, and a strict CSP. Secrets are redacted in excerpts and diffs,
  the state directory is private, and reports show your home directory as `~`.
- **Speed.** Caches are single-flight, activity polls get ETag/304 answers and
  API payloads are compact. On Django, a cached page load takes about 0.5 s.

[Unreleased]: https://github.com/phillipecardenuto/repo-understanding/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/phillipecardenuto/repo-understanding/releases/tag/v0.1.0
