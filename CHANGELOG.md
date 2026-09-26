# Changelog

All notable changes to repoviz. Versions follow [semantic versioning](https://semver.org/).

## [Unreleased]

### Added

- **Expected changes: plan vs actual** ([#8](https://github.com/phillipecardenuto/repo-understanding/issues/8)).
  - A session (or one review) can list what the plan says will change: path
    globs, `symbol:<qualified name>`, or `test` / `migration` / `docs` /
    `changelog` (`--expect`, `repoviz session scope --expect`, or the AI
    Review tab's *Expected to change* list).
  - The review shows a **Plan vs actual** card: each item done (with the files
    or symbols that matched) or not changed. When the plan names files or
    symbols, it also lists large changes not in the plan.
  - New signal `expected-not-changed` (medium). The feedback prompt starts
    with "The plan listed X, but it was not changed."
  - `--plan PLAN.md` and *Import plan…* extract expectations from a Markdown
    plan: backticked paths and names, and list items about tests,
    migrations, docs or the changelog. Unresolved lines are listed, and
    nothing is saved until you confirm. Expectations are stored with the
    session.
- **Checkpoints and an edit timeline inside a wave** ([#7](https://github.com/phillipecardenuto/repo-understanding/issues/7)).
  - `repoviz session checkpoint [--label]` records the working tree as a step
    of the session. It stores the commit checked out plus private,
    deduplicated copies of the files that differ from it. Calling it when
    nothing changed does nothing.
  - Review one step with `repoviz review checkpoint:2-3`, or everything since
    one with `checkpoint:3`. The target list offers *Since checkpoint N* and
    *Last step* for the active session.
  - The live server records **automatic** checkpoints while its page is open
    (`[activity] checkpoint_seconds`, 30 s), and keeps at most
    `max_checkpoints` (200) per session, folding the oldest automatic ones
    into the next. `POST /api/session/checkpoint` and *Mark checkpoint*
    buttons record one by hand. `repoviz session prune --days N` cleans up
    old sessions.
  - **Agent hooks:** `repoviz session checkpoint --hook-input --quiet` reads a
    Claude Code `PostToolUse` payload and labels the checkpoint with the tool
    and file. The recipe is in `docs/review.md`. It runs without loading the
    analysis code, in about 0.12–0.16 s on a Django-sized tree.
    `repoviz session note` adds a timeline event.
  - The Activity tab shows the **timeline**: checkpoints and notes, files and
    ±lines per step, and *reworked ×N* for files changed in 3 or more steps.
    Clicking a checkpoint opens that step in AI Review. Static reports embed
    the timeline; reviewing a step needs `repoviz serve`.
  - The `repoviz` command now starts through `repoviz.entry`, so reinstall
    (`pip install -e .`) to update the script.
- **Third-party dependency changes with supply-chain signals** ([#11](https://github.com/phillipecardenuto/repo-understanding/issues/11)).
  - A changed manifest's card lists its dependencies, package by package:
    added, removed, upgraded ↑, downgraded ↓, moved between scopes, or now
    from a Git repository, a URL, a path outside the repository, an npm
    alias or another registry. Covers pyproject (PEP 621, Poetry, uv
    sources), requirements, Pipfile, package.json, Cargo, go.mod and more.
  - Lock files are compared too (npm v1 to v3, yarn classic and Berry, pnpm,
    poetry, uv, pdm, Pipfile.lock, Cargo, go.sum, composer). Direct
    dependencies are listed with their resolved versions, the rest counted.
    Parsing is text only, and lock files over 20 MB are skipped with a note.
  - New signals: `dependency-added`, `dependency-downgraded`,
    `dependency-unpinned`, `dependency-source-changed` (high),
    `lockfile-without-manifest` and `manifest-without-lockfile`. All can be
    disabled. `new-external-dependency` is now about code imports only, and
    the two point at each other.
  - The review header, `repoviz review`, its Markdown, the pull-request
    comment and the JSON count them: `dependencies: +2 −0 ↑1 ↓0`.
  - Manifest parsers now record where a dependency comes from
    (`DeclaredDependency.source`), and pip lines given by URL
    (`git+https://…#egg=name`) are dependencies too. The manifest analyzer
    version is bumped.
- **Python imports through `sys.path` edits, and requirements-only apps as projects** ([#25](https://github.com/phillipecardenuto/repo-understanding/issues/25)).
  - `sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))`
    followed by `import schemas` is now an internal import of
    `app/schemas.py`, not a third-party package called `schemas`. Calls
    through it resolve too.
  - The edits are read statically: `insert`, `append`, `extend` and
    `site.addsitedir`, with `os.path` or `pathlib` expressions over
    `__file__` (also through a variable), or relative literals. Environment
    variables, `os.getcwd()` and absolute paths are ignored.
  - A `conftest.py`'s edits apply to its directory tree, and the editing
    file's own directory is searched, in Python's order. Such imports are
    marked `via: sys.path` (a pill in the link's details), and a
    `sys-path-imports` diagnostic lists the files.
  - A `requirements*.txt` next to (or one level above) a top-level Python
    package, with no project manifest covering it, makes a project inferred
    from it, with its dependencies (`requirements-dev.txt` and
    `requirements/test.txt` as dev). Shown as *inferred from
    requirements.txt*.
  - **On ELIES:** 33 imports in 11 files resolve through `sys.path`, the
    app is project `elies`, and the false third-party packages `schemas` and
    `config` are gone. The Python and manifest analyzer versions are bumped.
- **Constants and settings, before → after** ([#14](https://github.com/phillipecardenuto/repo-understanding/issues/14)).
  - A file's key changes also list its values: `MAX_IMAGES: 20 → 200`. That
    covers module-level UPPER_CASE Python constants, settings-class defaults
    (`Settings.debug`), exported JS/TS constants, and scalar keys of
    configuration files under config-like paths and `.env.example`.
  - Values are parsed, never evaluated (literals only), shortened to 120
    characters and redacted. Secret-looking names show `•••`.
  - Shown in the AI Review file card, `repoviz review` ("Values changed"), its
    Markdown, the pull-request comment and the JSON (`files[].values`).
  - New signal `safety-flag-weakened` (medium): `DEBUG` → true, TLS or
    verification → false, `ALLOW_ALL*` → true, a timeout → 0 or none, CORS or
    allowed hosts → `*`, CSRF / auth / secure / rate limit → false. Not raised
    in tests. It can be disabled.
  - Extracted at review time from the two versions of each changed file:
    snapshots, caches and diagrams are unchanged.
- **Persistent parse cache** ([#34](https://github.com/phillipecardenuto/repo-understanding/issues/34)).
  - Per-file parse results (Python, JavaScript/TypeScript, Git churn) are kept
    in `parse-cache.sqlite` in the state directory. The next command, a
    restarted server, or a snapshot at another revision parses only file
    contents it has never seen.
  - The store is SQLite in WAL mode, holding compressed JSON (never pickle),
    with private files. It has a 500 MB cap with least-recently-used eviction
    and a schema version. A damaged store is moved aside and rebuilt, with a
    warning.
  - `repoviz cache [info|clear]`, `[cache] disk / max_mb`, and
    `REPOVIZ_NO_DISK_CACHE=1`. `repoviz mcp` without `--allow-writes` keeps
    it in memory.
  - **Measured.** A second process is 1.3–3× faster: Flask 0.69 → 0.47 s,
    ELIES 2.2 → 1.6 s, this repository 1.6 → 0.56 s. On Django, `repoviz
    review` goes from 20.8 to 14.4 s, because parsing is only about 30% of
    the work there (see `docs/architecture.md`).
  - Also faster cold: redaction checks excerpts with plain substring tests
    before running its patterns (about 7× cheaper), and the diff skips
    identical nodes in one comparison.
- **History comparisons, so a clean checkout is never an empty Changes tab** ([#31](https://github.com/phillipecardenuto/repo-understanding/issues/31)).
  - **New presets:**
    - `last-commit` (HEAD~1 → HEAD);
    - `last-merge` (the latest merge on HEAD, from its first parent);
    - `branch` (since it left the default branch, committed work only);
    - `since:<tag or date>` (`since:v0.1.0`, `since:2024-06-01`,
      `since:2 weeks ago`).

    Available in `repoviz diff`, the API and the Changes tab.
  - **Picker.** Grouped as *Uncommitted*, *History* and *Custom*, and each
    option says how many files it touches (counted by Git, cheaply).
  - **Clean checkout.** The tab opens on this branch, the last merge or the
    last commit (the first with changes) and shows a note. With uncommitted
    work it opens on that, as before. A comparison you pick is remembered;
    an automatic one is not.
  - **Static reports** include the last commit (and the last merge when it
    differs), so they are never empty.
- **Why does A depend on B? And what breaks if X changes?** ([#28](https://github.com/phillipecardenuto/repo-understanding/issues/28)).
  - **`repoviz why A B`.**
    - Prints up to 5 shortest import chains between two components,
      packages, modules or files, with file:line and code per hop.
    - Between two functions it uses call chains.
    - When A does not depend on B, it shows the reverse chains.
  - **`repoviz impact X`.**
    - Lists dependents by distance, then fan-in.
    - Adds the entry points and tests reached; for a function these match
      the affected flow.
  - **API.** `/api/path` and `/api/impact` serve the same answers.
  - **Dependencies tab.**
    - Right-click a link (or `w`) to list its chains; the first chain is
      outlined in the diagram.
    - `b` (or *Blast radius* in the details, also from Structure) draws the
      dependents by ring (1, 2, 3+), labelled on each node and set apart by
      border weight and dash, with entry points and tests.
    - Static reports compute both in the page.
  - **Bounds and speed.** At most 5 chains of 8 hops, and 5,000 nodes per
    walk. On Django a query takes under 30 ms (0.3 s to index a snapshot,
    once).
  - **MCP.** `dependency_path` and the new commands share `query.py`.
- **Lists that scale** ([#29](https://github.com/phillipecardenuto/repo-understanding/issues/29)).
  - The shared table gains a search box (`/`), component chips with counts,
    and collapsible groups with per-group totals. All of them are remembered
    per tab.
  - **AI Review files.**
    - Signals (highest severity first) and risk lead each row.
    - Waves of 20 files or more are grouped by component, and a submodule
      heads the group of its inner files.
    - Paths start at the component's folder and are shortened in the middle;
      the file card has *Copy path*.
    - `o` collapses the current group, and `j` / `k` skip collapsed groups.
  - **Changes.** The changed-nodes list is ordered by relevance. Folder
    rollups ("contents changed") are hidden behind a counted switch; on
    ELIES's current work that removes 10 of 22 rows.
  - No horizontal page scroll at 390 px.
- **Diagrams stay readable at scale** ([#27](https://github.com/phillipecardenuto/repo-understanding/issues/27)).
  - **Orientation from shape.** Changes, Structure and Dependencies turn a
    diagram when the other direction fits the screen at a clearly larger zoom.
    On ELIES the System view now stacks its 19 services instead of an
    11,000 px wide row. A ⇄ / ⇅ button overrides it, remembered per tab (and
    for the System view). Layers and nested boxes keep their layout.
  - **Readable Fit.** Fit never shrinks labels below 11 px. A larger diagram
    fits its width and is panned; a mini-map appears when it is more than twice
    the view, and a click on it moves the view.
  - **Folded leaf lists.** More than 8 leaves of one kind under one parent
    (test files, docs, modules, files) become one node, "+ 27 test files".
    A click expands it. Changed, selected and found nodes stay outside.
  - **Faint old cycles.** A cycle the change does not touch is drawn thin,
    dotted and faint, labelled "existing cycle". New cycles keep the strong
    style.
  - **Long names** are shortened in the middle, with the full name in a
    tooltip.
  - `repoviz mermaid` and `repoviz diff --format mermaid` gain
    `--direction auto|LR|TB`; `repoviz mermaid --view structure` gains
    `--fold N`.
- **Honest header counts and a smarter Dependencies default** ([#24](https://github.com/phillipecardenuto/repo-understanding/issues/24)).
  - The header counts code components, services (first-party), submodules,
    external packages and entry points apart. It used to show one
    "components" number that mixed them. Each chip has an icon and opens its
    view.
  - `repoviz discover` prints the same breakdown ("contents: …", and
    `breakdown` in `--json`).
  - *Auto* in the Dependencies tab shows packages when the code has fewer than
    three components, with a note and a "Switch to components" link. A stored
    level always wins.
  - Compose services' own links are left to the System view unless the new
    **services** option is on. `repoviz mermaid --services` does the same.
- **Runtime coupling from code: container images and service URLs** ([#23](https://github.com/phillipecardenuto/repo-understanding/issues/23)).
  - A new `runtime` analyzer reads Python and JavaScript without running them.
  - Code that starts an image this repository builds gets an
    `invokes-container` edge to the submodule or directory that builds it.
    That covers the Docker SDK `containers.run(IMAGE)`,
    `["docker", "run", …, IMAGE]` and `"docker run … IMAGE"` strings.
  - Code that calls one of its services gets a `talks-to` edge, labelled with
    protocol and port.
  - Constants are followed across imports and settings classes, including
    f-strings and `os.getenv` defaults. A variable a Compose file points at a
    service counts as that service.
  - Images come from Compose `build` + `image`, `docker build -t` in
    Makefiles and CI, and submodules with a Dockerfile.
  - Unknown images and hosts make no edge. They are listed as external runtime
    references on the module.
  - The Dependencies view has a **runtime (containers, HTTP)** filter, on by
    default, with dashed "runs image" and solid "talks to" lines. On ELIES,
    `app` is now linked to every ML submodule and service it uses.
  - The System view draws the same links between services.
  - New review signal: `new-runtime-dependency` (low).
- **Checked-out Git submodules analyzed as nested sub-projects** ([#21](https://github.com/phillipecardenuto/repo-understanding/issues/21)).
  - Snapshots read each checked-out submodule with the superproject: at the
    recorded commit, or its working tree for the working tree. Its modules,
    symbols, imports and calls are in the graphs under the submodule's node,
    which is their component. Nested projects stay components.
  - Each submodule root is an import root, so a module name defined both in
    the superproject and in a submodule resolves to the right copy. That
    includes the code a Compose service runs.
  - A superproject manifest depending on a submodule's package gets a
    `depends-on` edge marked `cross_repository`.
  - Changes inside submodules appear in Changes diagrams. Reviews still list
    each file once.
  - New `[submodules]` settings: `analyze`, `exclude`, `max_files` and
    `max_mb`. A submodule that is not analyzed says why (too large, excluded,
    not checked out). On ELIES, TruFor (210 MB) is skipped by size, and the
    snapshot of the eight others takes about 1.5 s.
  - The Structure tab draws submodules as groups. Each one shows its pinned
    commit, files, languages, "⬇ N behind origin/main" (from local refs;
    repoviz never fetches), "↦ moved" and "✎ N uncommitted". A header chip,
    "9 submodules (2 modified)", opens a table of their states.
  - Python and JavaScript files are now parsed in worker processes from 64
    files (Python used to start at 300 files, JavaScript never).
  - Compose files inside a submodule are ignored when the superproject has its
    own.
- **System view from docker-compose** ([#22](https://github.com/phillipecardenuto/repo-understanding/issues/22)).
  - Compose files in one directory are variants of one system, so a service
    declared in `docker-compose.yml` and `docker-compose.prod.yml` is one
    service with its variants and their differences.
  - A service is first-party when this repository builds it. Otherwise it is
    infrastructure, classified by image: database, cache, queue, object store,
    search, monitoring, proxy or coordination.
  - Services link to their code. `runs` edges come from `uvicorn`,
    `gunicorn`, `celery -A`, `python -m` and node commands, or from the
    Dockerfile `CMD`. `builds` edges go to the build context.
  - Services link to each other: `starts-after` (`depends_on`), `talks-to` (an
    environment URL or host names another service, labelled with protocol and
    port) and `shares-volume`.
  - The Structure tab opens on a new **System** view when services exist, with
    a legend. `repoviz mermaid --view system` prints the same diagram.
  - Entry points are one per first-party service, grouped by declaring file in
    the Structure tab. Infrastructure commands (`etcd`, `minio server`) are no
    longer listed.
  - Environment values are never kept, only variable names.
  - The Changes and Dependencies filters offer the new relationships.
- **Read-only MCP server for coding agents** ([#18](https://github.com/phillipecardenuto/repo-understanding/issues/18)).
  - `repoviz mcp` speaks the Model Context Protocol over stdio (JSON-RPC 2.0,
    standard library only).
  - Seven tools: `architecture_overview`, `where_does_this_go` (component,
    layer and allowed imports, even for a new file), `impact` (callers,
    importers, entry points and tests, with call sites), `dependency_path`
    (shortest import chains), `check_scope` (planned files against the session
    scope), `review_current` and `contracts_check`.
  - Two prompts: `plan_check` and `self_review`.
  - Answers are capped (`max_items`, and about 4k tokens) and deterministic.
    Paths must stay inside the repository.
  - Nothing is written unless `--allow-writes` is given, which adds
    `set_scope` for the active session.
  - Registration with `claude mcp add repoviz -- repoviz mcp -C .`; see
    `docs/mcp.md`.
- **Pull-request review in CI** ([#17](https://github.com/phillipecardenuto/repo-understanding/issues/17)).
  - `repoviz review --format sarif|github|pr-comment`: SARIF 2.1.0 with stable
    fingerprints, inline GitHub annotations, and a sticky pull-request comment
    with a Mermaid map of where the change went, the top signals and every file
    by risk (capped at 40 nodes and 65,000 characters).
  - `--from-report FILE` renders or gates a saved JSON report without
    analysing again; `--link-base URL` links files and lines in the comment.
  - `repoviz contracts --format github` annotates new contract violations.
  - A composite GitHub Action (`.github/actions/review`) posts and updates the
    comment, annotates, optionally uploads SARIF and gates with `fail-on`.
    repoviz reviews its own pull requests with it.
- **Architecture contracts with a known-violations baseline** ([#16](https://github.com/phillipecardenuto/repo-understanding/issues/16)).
  - `[[contracts]]` declares layers (optionally per container), independent
    modules, public interfaces, forbidden and required dependencies, and
    acyclic packages. Patterns are qualified names (`app.features.*`) or path
    globs, so every analyzed language works. `allow_indirect = false` follows
    import chains, and `ignore` entries that match nothing are reported as
    stale. `[[review.rules]]` are now `forbidden` contracts.
  - Reviews raise `contract-broken` for violations the change introduces, with
    the file, the line and the chain. It replaces `forbidden-dependency`, which
    still works in `disabled_checks`. Also new: `contract-fixed`,
    `contract-baseline-changed` and `contract-baseline-invalid`.
  - Known violations can be accepted in a committed baseline.
    `repoviz contracts --baseline` prints it: repoviz never writes into the
    repository.
  - New command `repoviz contracts`: text, JSON or SARIF; exit 3 on new
    violations; `--suggest` proposes a layers contract.
  - The Dependencies tab gets a **Contracts** overlay (violating imports thick,
    dashed and labelled, layers drawn as numbered groups) and a contracts card.
  - The Structure profile lists pass/fail per contract.
  - `repoviz mermaid --view dependencies --contracts` draws the overlay in
    Mermaid.
- **Code changes of churn hotspots in the Structure tab** ([#36](https://github.com/phillipecardenuto/repo-understanding/issues/36)).
  - Clicking a hotspot opens a drawer under the graph. It shows the file's last
    commits and the diff of the latest one, or of its uncommitted edits, with an
    explicit `+` / `−` on every changed line.
  - Esc or × closes it and keeps the graph's zoom and selection.
  - In the live app, any file offers *Show code changes*. This is backed by the
    new endpoint `GET /api/file/changes?path=&commit=`.
  - Static reports embed the latest change of the busiest hotspots, capped and
    marked "truncated for report size".
  - Only that file is read, so it stays fast on large repositories. Commit
    subjects and diff lines are redacted.
- **Click a node in Dependencies to spotlight it** ([#37](https://github.com/phillipecardenuto/repo-understanding/issues/37)).
  - Its direct neighbours stay:
    - modules that use it, with solid, thick links;
    - modules it uses, with dashed, thick links.

    Everything else fades.
  - A line above the diagram names both counts, so the result never relies on
    colour alone.
  - The layout and zoom do not change.
  - Esc, *Clear* or a click on the background restores the graph.
  - It works the same in static reports.
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

- Command targets: `gunicorn -b 0.0.0.0:8000 app.wsgi:application` resolved the
  bind address instead of `app.wsgi:application`; quoted specs
  (`gunicorn "app:create_app()"`) and dotted modules without a callable are
  now recognised too.
- A renamed nested function no longer reports `renamed-symbol-stale-references`
  for unrelated uses of its old name in other functions or modules: only its
  enclosing function is searched.
- A deep review of the rename, risk and branch work fixed:
  - **Class renames.** A method that only moved with its renamed class was
    reported as "renamed from" its own name, which hid the class rename.
  - **Moves with deletions.** A file moved while some of its functions were
    deleted lost those deletions and their signals.
  - **Removed dependencies of a moved module** disappeared from the Changes
    diagram.
  - **Unrelated files paired as one move.** Files sharing a single generic name
    (`class Migration`), and methods sharing only a trivial signature such as
    `(self)`, were paired as renames.
  - **`renamed-symbol-stale-references`** no longer matches strings,
    docstrings or `obj.name` attributes. It is *high* only with a resolved
    call, otherwise *medium*, and its work is bounded.
  - **Large files.** The code-changes drawer checks blob sizes before reading
    them.
  - **One definition of a churn hotspot** is now shared by the Structure tab,
    its drawer and the risk score. A renamed class now counts the callers of its
    constructor.
  - **One diff renderer** is shared by the review's file card and the Structure
    drawer.
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
