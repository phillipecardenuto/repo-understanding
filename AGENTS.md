# Working on repoviz: guide for coding agents

repoviz is a local, read-only tool that maps a repository's architecture and
shows what changed. Its main job is helping a person supervise AI coding agents
wave by wave. It has three front ends over one engine: the CLI, a live web app
(`repoviz serve`) and a self-contained HTML report (`repoviz report`).

Planned work is in [docs/wishlist.md](docs/wishlist.md) and the GitHub issues
labelled `wishlist`. Each issue says why, what to build, where in the code, and
how to test it.

## Map of the code

| Area | Files | Read first |
|---|---|---|
| Read-only Git and trees | `gitutil.py`, `sources.py`, `submodules.py` | `docs/architecture.md` ("Layers", "Git hardening") |
| Discovery and parsers | `discovery.py`, `classify.py`, `manifests.py`, `yamlish.py`, `config.py` | `docs/configuration.md` |
| Graph building | `analyzers/*.py`, `pipeline.py`, `graph.py`, `ids.py`, `model.py` | `docs/data-model.md`, "Analyzer interface" |
| Comparing states | `diff.py`, `flow.py`, `activity.py`, `session.py`, `repo.py`, `filechanges.py` (one file's recent changes) | "Caching and performance" |
| Agent review | `review.py` (targets, scope, signals, feedback prompt), `risk.py` (risk score), `contracts.py` (architecture contracts, baseline) | `docs/review.md`, `docs/configuration.md` ("Architecture contracts") |
| Front ends | `cli.py`, `server.py`, `render/html.py`, `render/views.py`, `render/mermaid.py` | "Web application" |
| Browser app | `web/app.js` (tabs, diagrams, icons, in-app guide), `web/app.css`, `web/index.html` | the `HELP` array in `app.js` |

`render/views.py` + `render/mermaid.py` (Python, used by the CLI) and `web/app.js`
(browser) build the same view graphs. Change both when a view changes.

## Invariants (never break these)

1. **Read-only.** Never write inside the analyzed repository. Session baselines,
   notes and caches go to the state directory (`session.py`, owner-only
   permissions).
2. **Never execute repository code.** Don't import it, run its tests or build
   tools, or evaluate its config files. Parse text only (AST, regex, TOML/JSON/
   YAML-subset). Call Git only through `gitutil.Git`, which disables fsmonitor,
   hooks, filters, textconv and signature programs. Coverage reports and similar
   artifacts may be *read* if they already exist, never produced.
3. **Standard library only at runtime** (plus `tomli` on Python < 3.11).
   Optional extras such as `grimp` must degrade gracefully when missing.
4. **Offline and locked-down UI.**
   - No network requests from the page; Mermaid is vendored.
   - The CSP is `script-src 'self'`: no inline scripts, no `eval`, no new CDNs.
   - Mermaid runs with `securityLevel: "strict"`.
5. **Never colour alone.** Every status must also show as an icon, a line
   style, a shape or text. Check light and dark themes.
6. **Static report parity.** A feature in the live app must also work in the
   static report (`StaticApi` in `app.js`), or say clearly that it needs
   `repoviz serve`.
7. **Stable IDs.** Node and edge IDs come from `ids.py` and must stay
   comparable across revisions. If an analyzer's output changes, bump that
   analyzer's `version`; it is part of cache keys.
8. **Server hardening.**
   - Every `/api/*` request needs the `X-Repoviz: 1` header, and the Host
     allow-list stays in place.
   - New POST endpoints go through `_guard` and write under `write_lock`.
   - Handle `CLIENT_GONE` (the client disconnected) the way existing handlers do.
9. **Secrets.** Pass every excerpt, diff line or prompt text through
   `redact.py`.
10. **Bounded work.**
    - Cap file sizes, lines, nodes and commits, and say in the UI when a cap
      applies.
    - Large repositories must stay fast. Measure a Django-sized repository
      before and after (about 8 s for a cold snapshot, 0.1 s cached), and route
      expensive work through the caches in `repo.py` (single-flight).

## Build and test

```bash
pip install -e '.[test,grimp,browser]'
pytest -q -m "not browser"                           # fast suite
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers pytest -q  # everything, including browser tests
```

- **Fixtures.** `tests/conftest.py` provides `make_repo({...})`, which creates a
  throw-away Git repository with `write`, `commit`, `stage`, `delete` and
  `git`. The state directory and the Git config are isolated per test.
- **Where tests go.**

  | Area | File |
  |---|---|
  | Review signals | `tests/test_review.py` |
  | Diff, activity, sessions | `tests/test_changes_activity.py` |
  | Analyzers | `tests/test_analyzers.py` |
  | Discovery, manifests | `tests/test_discovery_manifests.py` |
  | Server, report, CLI | `tests/test_outputs.py` |
  | Security | `tests/test_hardening.py` |
  | Submodules, large repositories | `tests/test_large_repo_fixes.py` |
  | UI end to end (Playwright) | `tests/test_browser.py` |

- **Check the result by hand, not only with tests.**
  - Run `repoviz discover`, `repoviz review last-commit` and `repoviz serve`
    on a real repository.
  - Take a headless-browser screenshot of each tab you touched, in both themes.

## Definition of done for a wishlist issue

- The acceptance criteria in the issue are met and covered by tests.
- The docs are updated:
  - `README.md` for user-visible behaviour;
  - `docs/*.md` for design, configuration and signals;
  - the in-app guide (the `HELP` array in `web/app.js`) for anything in the UI.
- `CHANGELOG.md` has an entry under `## [Unreleased]`.
- New review signals are listed in `docs/review.md`, with severity, whether
  they can be disabled (`review.disabled_checks`) and an example.
- One issue per pull request. Reference it with `Closes #N`.
