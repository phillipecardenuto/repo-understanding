# Changelog

All notable changes to repoviz. Versions follow [semantic versioning](https://semver.org/).

## [Unreleased]

### Added

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
