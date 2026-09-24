# Evaluation on a large, multi-module system: ELIES

This evaluates repoviz on [researchintegrity/elies](https://github.com/researchintegrity/elies)
(commit `b129421`). It lists what worked, what was fixed during the evaluation, and
every remaining improvement for large repositories, in priority order.

## The system

ELIES is large as a system rather than by file count:

- **Superproject:** a FastAPI API and Celery workers (`app/`, 78 Python modules),
  27 test files, 21 docs, and two docker-compose files (dev and prod) with about
  17 services.
- **9 Git submodules** under `system_modules/` hold most of the code:
  - the React frontend: 141 files;
  - TruFor: 98 files and 201 MB, including model weights;
  - panel extraction, PDF image extraction, CBIR, provenance analysis, two
    copy-move detectors and watermark removal: 8–82 files each.
- **Coupling is mostly at runtime, not through imports.** Workers start the modules
  as Docker containers by image name (`trufor:latest`, `panel-extractor:latest`, …),
  compose builds services from submodule directories, and the backend calls
  services over HTTP.

## Method

1. `repoviz discover`, `snapshot`, static report and live server, with timings.
2. Review the last merge (`dev` → `main`, 45 changed paths per git).
3. A simulated agent wave.
   - **Session scope:** allowed `app/routes/**`, `app/services/**`, `tests/**`;
     protected `app/config/**`, `system_modules/**`, `docker-compose*.yml`.
   - **Edits:**
     - a new FastAPI route;
     - a renamed parameter in a shared service function (`limit` → `page_size`);
     - edits to a protected config file and a compose file;
     - an edit inside the CBIR submodule;
     - a `console.log` in the frontend submodule;
     - a trivial test.
4. Screenshots of every tab, before and after the fixes.

## What worked

- **Speed.** Snapshot 0.6 s. Static report 1.4 s (4 MB). Live server: bundle
  0.67 s cold / 0.04 s warm, review 0.26 s / 0.04 s, activity poll 0.02 s (304).
  A slow review never blocks other requests (health ≤ 1 ms).
- **Scope violations.** Protected config and compose edits were flagged, and so
  were `assert True`, `TODO` and `print(`.
- **Affected flow.** It showed the new endpoint and the two existing API routes
  that reach the changed `list_images`.
- **The AI Review map.** Grouping by component was the most useful overview of
  the merge.

## Fixed during this evaluation

| # | Problem (evidence) | Fix |
|---|---|---|
| 1 | **Phantom changes.** A clean checkout showed 9 submodules + `system_modules` as *removed* and 14 removed relationships. The working-tree source dropped gitlinks. | Every source reports submodules and their commits. |
| 2 | **Edits inside submodules were invisible.** The wave's protected CBIR edit and the frontend `console.log` went unreported. The merge review showed 40 of 45 changed paths, missing the CBIR, copy-move and provenance code changes and the new `elies-frontend` submodule. | Reviews and activity look inside checked-out submodules: commit range (commits, count, changed files) and uncommitted files, each a normal review file with scope rules, signals and diff. New signals: `submodule-added`, `-removed`, `-updated`, `-uncommitted`. Sessions record submodule commits and pre-existing edits, so earlier work is not blamed on the agent. Each submodule is its own component. |
| 3 | **False "broken import".** `app/config/` has no `__init__.py` but sits inside package `app`, so Python imports it as `app.config.settings`. repoviz named it `settings`, causing 32 false `unresolved-internal-import` warnings, a high "Broken import" signal and missing edges. | Implicit namespace subpackages inside a regular package are named like Python does. |
| 4 | **Wrong source root.** Discovery listed `app/` as a source root although it is a package. | Conventional directory names are ignored when they are packages. |
| 5 | **Missed breaking change.** Signatures longer than 200 characters were compared after truncation, so `limit` → `page_size` on a FastAPI-style function went unnoticed. | Changes are detected on a hash of the full signature. The signal names the parameters, e.g. *"parameters removed: limit; added: page_size; called from unchanged code: …"*. |
| 6 | **7 false high-severity signals.** Renaming base class `ELISException` → `ELIESException` produced "call to a removed function" for every `super().__init__()`. | A caller that now calls a symbol of the same name was redirected, not left dangling. |
| 7 | **Noisy dependency signal.** "New third-party dependency: fastapi" fired for a new route although the app uses FastAPI everywhere. | Only new to the repository or to the component. |
| 8 | **Changes tab ignored the session.** It opened on *HEAD vs working tree* while a session was active. | It defaults to the session. |
| 9 | **Empty default review.** On a clean checkout the Review tab opened on the empty "uncommitted" target. | Opens the first review with changes. |
| 10 | **"null" in the UI.** The Dependencies "Cycles" card (and other optional parts) printed `null`. | Optional DOM children are skipped. |
| 11 | **Wrong owner for compose files.** `docker-compose.yml` was attributed to one of its own services. | The file's own node wins over nodes that share its path. |

## Remaining improvements

Priorities: **P1** would noticeably change the usefulness on repositories like this
one, **P2** is a clear improvement, **P3** is polish.

### A. Project awareness

1. **P1: Analyze submodules as sub-projects.** Reviews look inside submodules, but
   Structure and Dependencies still show each one as a single box. Snapshot every
   checked-out submodule with its own discovery and analyzers, nest it as a
   component, and let users drill into it: the React frontend (93 JSX files) and
   the ML services are most of the system. Show cross-repository links when a
   submodule is installed as a package.
2. **P1: A system view from compose files.**
   - Link each service to its code: `build: .` + `command: uvicorn app.main:app`
     → `app.main`; `build.context: system_modules/provenance-analysis` → that
     submodule.
   - Merge a service's dev and prod definitions (today 35 service nodes, most
     duplicated) into one service with variants.
   - Separate first-party services from infrastructure images (redis, mongo,
     minio, etcd, milvus, flower, attu).
   - Draw `depends_on`, shared volumes and URLs from environment variables
     (`http://cbir-service:8000`).
3. **P1: Runtime coupling edges.** `app/config/settings.py` names images
   (`TRUFOR_DOCKER_IMAGE = "trufor:latest"`) that are built from submodules, and
   `app/utils/docker_*.py` runs them. Match image names and HTTP service URLs in
   code to compose `image:`/`build:` entries, and add `invokes` edges from the
   calling module to the submodule. This is the real architecture: API → workers →
   ML containers.
4. **P2: Better defaults for the Dependencies tab.** At component level it shows
   23 nodes, 21 of them compose services, while the code is 2 boxes (`app`,
   `tests`). Default to package level for code when there are fewer than 3 code
   components, and move services to the system view.
5. **P2: Entry points.**
   - Group them by declaring file.
   - Don't list infrastructure images' commands as entry points (`etcd`, `minio
     server`).
   - Show one entry per service with its variants.
6. **P2: Tests that edit `sys.path`.** `tests/test_panel_extraction.py` inserts
   `app/` into `sys.path` and imports `schemas`/`utils`, which show up as external
   packages. Recognize simple `sys.path.insert(…, os.path.join(dirname(__file__),
   '..', 'app'))` patterns, or at least report "import only resolves through
   sys.path manipulation".
7. **P2: `requirements.txt`-only applications.** Treat a requirements file next to
   a top-level package as a project ("Projects (0)" today), so the backend has a
   name, dependencies and a place in the project level.
8. **P3: Honest header numbers.** "117 components" counts 40 external packages,
   35 compose services and 15 entry points. Show code components, services,
   submodules and external packages separately.

### B. Awareness of modifications

1. **P1: Review commit by commit.** A wave is usually several agent commits, and a
   merge can hold many. Add a commits panel for the reviewed range: subject, files
   per commit, and a filter "show only this commit". The data is available through
   `git log`.
2. **P1: Detect renames.** `ELISException` → `ELIESException` and
   `elis_exception_handler` → `elies_exception_handler` produce four "public symbol
   removed" signals plus removed/added nodes. Pair removed and added symbols of the
   same kind in the same parent with similar names or bodies, and report "renamed",
   listing callers in untouched files that still use the old name. Apply the same
   to submodules: `elis-frontend` → `elies-frontend` shows as removed + added.
3. **P1: Unwired code.** The new `app/routes/reports.py` router is never included
   in `app/main.py`, and nothing flags it. Signal new non-test modules that nothing
   imports, and are not entry points, as "new code is not wired in".
4. **P2: Cross-service contracts.**
   - Report added, removed or changed HTTP routes (FastAPI/Flask decorators) as
     API changes, and point at frontend callers of that path (JS `fetch`/axios
     strings in the frontend submodule).
   - Do the same for Celery task signatures vs `.delay()`/`.apply_async()` callers.
   - Flag compose environment variables vs `os.environ[...]` / settings readers.
5. **P2: Value changes in configuration.** `MAX_IMAGES_PER_EXTRACTION = 20 → 200`
   appears only as a scope signal. Show module-level constant changes as key
   changes with before and after values, like signatures.
6. **P2: History-based defaults on a clean tree.** The Changes tab offers only
   HEAD vs working tree, which is empty on a clean checkout. Precompute
   last-commit, last-merge and branch vs default-branch comparisons, as the review
   targets already do.
7. **P2: Missing history.** A submodule update whose old commit is absent (shallow
   clone) only says so. Suggest the exact command
   (`git -C <submodule> fetch --depth=… origin <sha>`) instead of silently
   degrading. repoviz stays read-only and never runs it.
8. **P3: Nested submodules** (submodules of submodules) are not inspected.

### C. User interface

1. **P1: Diagram readability at scale.**
   - The 50-node structure tree is tall and narrow, and "fit" shrinks it until
     it's unreadable. Pick the orientation from the shape, keep a minimum readable
     zoom (scroll instead), and fold long leaf lists (27 test files, 17 docs) into
     "N files" nodes.
   - On the Changes tab, the purple cycle edges of the existing
     `routes ↔ tasks ↔ services` cycle dominate even when the change doesn't touch
     it. Draw pre-existing cycles faintly unless the change touches them.
2. **P2: Changed-nodes list.**
   - Hide container rollups ("app/config — contents changed") by default, or
     collapse them under their component.
   - Sort by relevance, and add a component filter and a search box.
3. **P2: Review files table.**
   - Group by component with collapsible sections.
   - Show paths relative to the component, since long submodule paths wrap.
   - Put the signal count first.
4. **P2: Show submodule state everywhere.** Pinned commit, "N commits behind
   remote-tracking branch" (when known) and uncommitted state in the Structure
   view and the header, not only in discovery.
5. **P3:** An overview "system" diagram as the first thing on the Structure tab,
   once A1–A3 exist.

## Reproducing

```bash
git clone https://github.com/researchintegrity/elies && cd elies
git submodule update --init --depth 1
repoviz discover
repoviz review last-commit           # the dev → main merge, including submodule updates
repoviz session start --allow "app/routes/**" --protect "system_modules/**"
# ... edit files, including inside system_modules/<name>/ ...
repoviz review
repoviz serve --open
```
