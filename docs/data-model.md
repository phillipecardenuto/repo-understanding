# Data model

The JSON produced by `repoviz snapshot`, `repoviz diff --format json`,
`repoviz activity --json` and the `/api/*` endpoints uses the normalized model
in `src/repoviz/model.py`. It is independent of Mermaid and of any programming
language. Keys with `null` or empty values are omitted. `schema_version` is `1`.

## RepositorySnapshot

| Field | Description |
|---|---|
| `repository_id` | stable across clones: hash of the root commit(s); path hash for non-Git directories |
| `repository_name`, `root` | directory name and absolute path |
| `revision`, `label` | human label (`HEAD (1a2b3c…)`, `working tree`, `index (staged)` …) |
| `revision_id` | commit SHA; content digest for working tree / index; `session:<id>` |
| `kind` | `commit`, `worktree`, `index`, `session-baseline`, `filesystem`, `empty` |
| `generated_at` | ISO 8601 UTC |
| `components`, `modules`, `symbols` | `ComponentNode` lists (split by `category`) |
| `containment_edges` | `contains` edges derived from `parent_id` |
| `dependency_edges` | `imports`, `depends-on`, `invokes`, `builds`, `runs`, `starts-after`, `talks-to`, `shares-volume` … (direct **and** component-level aggregated edges, `direct: false`) |
| `call_edges` | `calls` between symbols (or from module-level code) |
| `cycles` | `Cycle` list (module, component and project level) |
| `diagnostics` | discovery and analyzer diagnostics |
| `analyzers` | `AnalyzerRun` per analyzer: applicable, reason, capabilities, duration, stats |
| `profile` | the discovery profile |
| `metadata` | Git info, churn window, tool version, config fingerprint, timings |

## ComponentNode

| Field | Description |
|---|---|
| `id` | stable ID derived from `key` (e.g. `file_3f9a…`) |
| `name`, `qualified_name` | display name; qualified name (`pkg.mod.Class.meth`, `@scope/pkg`, `example.com/m/pkg`) |
| `component_type` | `repository`, `directory`, `project`, `workspace-member`, `package`, `namespace-package`, `module`, `file`, `class`, `function`, `method`, `main-block`, `entry-point`, `service`, `container`, `compose`, `ci-pipeline`, `external-package`, `container-image`, `submodule`, `component` (configured) … |
| `category` | `component`, `module` or `symbol` |
| `language`, `path` | language and repository-relative path (`""` = root) |
| `parent_id` | containment parent |
| `analyzer`, `analyzers` | first and all contributing analyzers |
| `key` | canonical identity key the ID is derived from |
| `fingerprint` | Git blob hash (files) or normalized-source hash (symbols) used for change detection |
| `start_line`, `end_line` | for symbols and services |
| `tags` | roles: `test`, `docs`, `generated`, `vendored`, `config`, `manifest`, `ci`, `container`, `entry-point`, `external`, `stdlib`, `unsupported`, `component`, `project`, `inferred` (a project inferred from `requirements.txt`), `top-level`, `source-root` … |
| `metadata` | analyzer-specific: `component_id`, `project_id`, `semantic_fingerprint`, `signature`, `decorators`, `churn`, `version`, `role`, `entry_kind`, `dependency_details` … |

## DependencyEdge

| Field | Description |
|---|---|
| `id` | hash of `(relationship, source, target)` (aggregated edges are distinct) |
| `source_id`, `target_id`, `relationship` | the relationship |
| `evidence` | `SourceEvidence` list |
| `occurrences` | number of distinct source sites |
| `direct` | `false` for aggregated edges (`metadata.level`, `underlying_edges`, `underlying_count`) |
| `cycle_ids` | cycles this edge participates in |
| `confidence` | 0–1: 1.0 for resolved static imports, lower for dynamic imports, heuristic JS resolution, `self` calls… |
| `metadata` | `type_checking_only`, `conditional_only`, `lazy_only`, `dynamic_only`, `test_only`, `external`, `imported_names`, `scope`, `spec`, `confirmed_by`, `via` (`sys.path`: resolved only through a `sys.path` edit) and `sys_path_edit` (its `file:line`), `same_package` (Java / Kotlin: a type of the same package used without an import; C#: of the own or an enclosing namespace) … |

## Submodules

A submodule node (`component_type: submodule`, tags `submodule` and
`component`, plus `project` when it has a manifest at its root) keeps the
superproject path as its name.

**Metadata.**

- `commit`, and `recorded_commit` when it is checked out at another commit;
- `url`, `uncommitted_files`;
- `analyzed`, with `not_analyzed` giving the reason when it is not;
- `languages`, `files`;
- `behind` and `behind_ref`, from the local remote-tracking ref.

When it is analyzed, its directories and modules are its descendants, and it is
their `component_id`.

A `depends-on` edge into or out of an analyzed submodule has
`metadata.cross_repository: true`.

A nested submodule (a submodule's own submodule, up to 3 levels deep and 50 in
all) is a submodule node too, named by its full path (`outer/inner`), whose
parent is the submodule that contains it. Nested submodules left out by those
caps are listed in the `nested-submodules-capped` diagnostic.

## Services (Compose)

`services.py` merges a directory's Compose files, and the manifest analyzer adds
one `service` node per service name, whatever the number of variants. Its key is
`service:<compose dir>:<name>`.

**Tags.** A service node is tagged `service`, `component` and `deployment`, and
either `first-party` or `infrastructure`.

**Metadata.**

- `service_kind`: `first-party`, `database`, `cache`, `queue`, `object-store`,
  `search`, `monitoring`, `proxy`, `coordination` or `other`.
- `variants` and `variant_files`.
- `differences`: field → variant → value, for `image`, `command`, `ports` and
  `build_context`.
- `image`, `build_context`, `dockerfile`, `command`, `entrypoint`, `ports`,
  `volumes` (named volumes only), `networks`, `env_keys` (names only, never
  values), `env_files`, `profiles`.
- `runs` and `runs_from`.

**Edges.**

| Relationship | From → to | Meaning |
|---|---|---|
| `builds` | service → directory, submodule or Dockerfile | its `build` context |
| `runs` | service → module, callable or file | what its command runs (resolved like entry points), or its Dockerfile's `CMD` / `ENTRYPOINT` |
| `depends-on` | infrastructure service → container image | its `image` |
| `starts-after` | service → service | `depends_on` |
| `talks-to` | service → service | an environment value names the other service (by name, `container_name` or `hostname`); `metadata.label` is the protocol and port, `metadata.env_keys` the variable names |
| `shares-volume` | service → service | the same named volume (listed in `metadata.label`); a volume shared by more than 8 services is ignored |

## Runtime coupling (from code)

The `runtime` analyzer reads Python and JavaScript. It never runs them.

| Relationship | From → to | Meaning |
|---|---|---|
| `invokes-container` | module → the directory or submodule that builds the image (the service, when built from the repository root) | the module starts that image: `containers.run(IMAGE)`, `["docker", "run", …, IMAGE]`, `"docker run … IMAGE"` |
| `talks-to` | module → service | the module uses a URL or host naming the service (`http://cbir-service:8000/search`), directly or through constants |

**Edge metadata.**

- `label`: the image, or the protocol and port.
- `image` or `host`.
- `via`: the constant or `literal`.
- `provided_by`: the Compose file, Makefile or Dockerfile that provides the image.
- `provider_service`: the service built from it.

Confidence is 0.8 for literals and 0.7 for resolved constants.

**Where values come from.** Constants are resolved through imports, class
attributes (settings classes), f-strings, `+` concatenation and
`os.getenv("KEY", default)`. When a Compose file sets `KEY` to another
service's name or URL, `KEY` counts as that service.

**Where images come from:**

- Compose `build` + `image` pairs;
- `docker build -t IMAGE CONTEXT` in Makefiles and CI files;
- a submodule holding a Dockerfile, which provides the image named after it
  (confidence 0.6).

**Service-to-service copies.** In the last phase, each edge is also drawn
between services, with `metadata.from_code: true` and `metadata.via` (the
module): from each first-party service whose code includes the module (its
build context, narrowed to the services whose command reaches the module
through imports), to the service it calls or whose image it starts. The System
view draws these; the Dependencies view leaves them out.

**Unknown values.** An image or host that nothing in the repository provides
makes no edge. The module keeps it in `metadata.external_runtime_references`
(`kind`, a redacted `value`, `line`, `via`), with at most 10 per module.

## Cross-service contracts (routes, tasks, environment)

The `interfaces` analyzer (`analyzers/interfaces.py` over `interfaces.py`) reads
Python and JavaScript text for the contracts between services that imports do
not show. It never runs them.

**Nodes** (category `symbol`, tag `api`, parent: the module that defines them).

| `component_type` | Key | From | Metadata |
|---|---|---|---|
| `http-route` | `route:<file>:<METHOD> <template>` | FastAPI / Starlette (`@app.get`, `@router.post`, `api_route`, `websocket`), Flask (`@app.route`, `@bp.get`) and Express (`app.get`, `router.post`) | `method`, `path` (as written, with its prefixes), `template` (normalised: `/api/images/{}`), `handler`, `confidence`, `mounted` |
| `task` | `task:<name>` | Celery `@app.task`, `@shared_task` (the `name=` argument, or the function's dotted name) | `func`, `positional`, `required`, `kwonly`, `var_positional`, `var_keyword`, `signature`, `bind` |

A route's path joins its decorator with its router's prefix and the prefix the
router is mounted with:

- FastAPI `APIRouter(prefix=…)` and `app.include_router(router, prefix=…)`,
  resolved through the module's imports, up to 6 levels deep;
- Flask `Blueprint(url_prefix=…)` and `app.register_blueprint(bp, url_prefix=…)`
  (the second replaces the first, as in Flask);
- Express `app.use('/prefix', router)`, in the same file or through an
  `import` / `require` of another file.

Confidence is 0.85 when the route's router is mounted by an application, or has
no router, and 0.7 when its router is never seen mounted.

**Edges** (all with `metadata.label`).

| Relationship | From → to | Meaning | Confidence |
|---|---|---|---|
| `calls-http` | module → `http-route` | a call (`requests`, `httpx`, `fetch`, `axios`, an `axios.create` instance) whose URL template matches the route's; the method must match when the call gives one | the call's (0.7 for a literal URL, 0.6 with a dynamic part), × 0.8 when it matches more than one route |
| `enqueues` | module → `task` | `task.delay(…)`, `task.apply_async(…)`, `send_task("name", …)` | 0.8 |
| `reads-env` | module → the service, env file, Dockerfile or manifest that declares the variable | `os.environ[…]`, `os.getenv`, `environ.get`, pydantic `BaseSettings` fields (with `env_prefix`), `process.env.X`, `import.meta.env.X`; `metadata.env_keys` lists the names | 0.9 |

A URL is normalised to a path template: the scheme, host and query are
dropped, `{id}`, `:id`, `<int:id>` and dynamic parts (`${id}`, f-string fields)
become `{}`, and a trailing slash is ignored. A route parameter matches any
segment; a dynamic part of a call matches only a route parameter. A URL with no literal path segment is ignored rather than guessed. A
route in the same file as its caller makes no edge. One call links to at most 3
routes.

**Consumers on the module node** (kept whether they match or not, at most 50
per kind): `metadata.http_calls` (`method`, `template`, `line`, `url`,
`confidence`), `metadata.enqueues` (`kind`, `target` or `name`, `line`, the
number of `positional` arguments and the `keywords`, and the resolved `task`), `metadata.env_reads`
(`name`, `line`, `default`: whether the read has a default).

**Declared variables on the repository node.** `metadata.env_declared` maps each
variable (at most 1000) to up to 5 places that set it: a Compose service's
`environment` (`env_keys`), `.env.example`-style files, Dockerfile `ENV` / `ARG`
and Kubernetes-style `env: - name:` lists. Values are never read or stored.

The review compares these between the base and the target: see
"Cross-service contracts" in [review.md](review.md).

## Health metrics

The `metrics` analyzer runs last and measures every module (and every
programming file of a language repoviz does not parse). It never runs code. Its
results are in `metadata.metrics`:

| Field | Meaning |
|---|---|
| `sloc` | Code lines: not blank, not only a comment. |
| `complexity`, `complexity_kind` | `cyclomatic` for Python: the sum of its functions' McCabe complexity plus the decision points of module-level code. `whitespace` for other languages: the sum of the indentation levels of the code lines (Tornhill), in the file's own indentation unit, with a tab as one level. |
| `max_complexity`, `max_complexity_symbol` | Python: the most complex function or method. |
| `max_nesting` | The deepest block nesting (Python: `if`, `for`, `while`, `try` and `with`; an `elif` stays at its `if`'s depth), or the deepest indentation level. |
| `fan_in`, `fan_out`, `instability` | Modules that import it (test modules not counted), modules it imports, and `fan_out / (fan_in + fan_out)` (`null` when both are 0). |
| `hotspot_top`, `hotspot_score` | Complexity × churn. Both are ranked (complexity within its kind), and the product is ranked again: `hotspot_top` is the share of modules at or above this one, as a percentage (`3` = the top 3 %). Only modules changed at least twice in the churn window take part, and tests are not ranked. |

**Python cyclomatic complexity** is 1 plus one for each `if` / `elif`,
conditional expression, `for` / `while` loop, `except` handler, `match` case,
extra operand of `and` / `or`, and `for` / `if` clause of a comprehension.
`else`, `try`, `finally` and `with` add nothing. A nested function or class is
measured on its own. Functions and methods carry their own `complexity` and
`nesting` in their metadata. The Python analyzer counts all this in the walk
that already collects calls, so it is cached with the parse, per file content.
Whitespace complexity is cached per file content too.

**Rolled up.** Directories, packages, projects, submodules and the repository
get `metrics` with `modules`, `sloc` and `complexity` (sums), `fan_in` /
`fan_out` (modules *outside* it that import something inside it / that
something inside it imports), `hotspot_top` and `hottest` (its hottest module),
and `owner_share` (weighted by commits). The live app and reports roll up
configured components the same way, from their modules.

**History.** A file's `metadata.churn` has `commits`, `last_commit`,
`authors` (how many people made those commits), `owner_share` (the share of
the most active one) and `spark` (commits in 12 equal slices of the churn
window, oldest first). The snapshot's `metadata.churn_span` gives the window's
`start`, `end` and team size (`authors`). **No author name is stored in a
snapshot.** The live app asks `GET /api/owners` for names, which follows
`[privacy] show_authors` (see
[configuration.md](configuration.md#author-names)).

## SourceEvidence

`path`, `start_line`, `end_line`, `construct` (`import`, `from-import`,
`relative-from-import`, `dynamic-import`, `call`, `manifest-dependency`, `FROM`,
`COPY`, `console-script`; for Java and Kotlin `static-import`, `import-on-demand`,
`same-package`, `qualified-name`; for C# `using`, `global-using`, `using-static`,
`using-alias`, `same-namespace`, `extension-method`; for Rust `use`, `path`, `extern-crate` …), `analyzer`, optional
`excerpt`.

When deciding whether evidence *changed*, only `(path, construct, normalized
excerpt)` is compared: line numbers shift with every edit above them.

## Cycle

`id` (hash of level + members), `level` (`module`, `component`, `project`),
`relationship`, `members`, `edge_ids`, `example_path` (a shortest cycle
through the first member).

## RepositoryDiff

| Field | Description |
|---|---|
| `base`, `target` | `SnapshotRef` (`repository_id`, `revision`, `revision_id`, `kind`, `label`, `generated_at`) |
| `added_nodes`, `removed_nodes`, `modified_nodes`, `unchanged_nodes` | node IDs (counts in compact report data) |
| `added_edges`, `removed_edges`, `modified_edges`, `unchanged_edges` | edge IDs (containment excluded) |
| `introduced_cycles`, `resolved_cycles` | `Cycle` lists |
| `changed_cycles` | overlapping cycles with `added_members` / `removed_members` |
| `new_dependencies`, `removed_dependencies` | summaries: `level` (`component`, `project`, `module`, `external`), `source`, `target`, `evidence`, `in_cycle`, `scope`, `note` (e.g. "type-checking-only import became a runtime import") |
| `nodes` | union of both snapshots' nodes, each with `status`, `change_reasons` and `before` (previous values). A renamed or moved node appears once, at its new ID, as `modified`, with `before.previous_id`, `before.name`, `before.qualified_name` and (when it moved) `before.path` |
| `edges` | union of edges, each with `status`, `change_reasons`, `in_base_cycle`, `in_target_cycle`, `base_evidence`, `base_flags`. An edge that continues a base edge whose endpoint was renamed has `previous_id` and is `unchanged` (or `modified` for other changes); it is not a new or removed dependency |
| `renames` | removed + added pairs folded into one node: `kind` (`file`, `folder`, `submodule`, `symbol`), `old_id` / `new_id`, `old_name` / `new_name`, `old_path` / `new_path`, `how` (`same content`, `similar content`, `same name`, `same body, new name`, `similar name`, `same signature and size`, `moved with its folder`, ...), `similarity`, `renamed`, `moved` |
| `summary` | counts by status, category and relationship |
| `diagnostics` | e.g. different repositories or configurations |

Node reasons: `content changed`, `formatting or comments only`, `contents
changed` (a descendant changed), `moved`, `renamed`, `renamed from <name>`,
`moved from <path>`, `type a → b`, `<key> changed`, `roles changed: …`.

Renames are found without reading files (`renames.py`).
- **Files** are paired by identical content, or, for code, by the share of
  top-level names they have in common.
- **Folders** are paired when most of their files moved together.
- **Submodules** are paired by the same URL or commit, or, when the upstream repository
  was renamed too, by a similar name in the same folder (`elis-frontend` → `elies-frontend`).
- **Symbols** are paired within their (renamed) parent. The same name, the same
  body without its own name (`body_fingerprint`), a similar name, or the same
  signature and size all count.

Cycles are matched through renames, so a cycle whose modules moved is not
"introduced". Edge reasons: `evidence changed`, `occurrences a → b`,
`<flag>: a → b`.

## ActivityEvent

| Field | Description |
|---|---|
| `path`, `previous_path` | file (and rename source) |
| `first_observed`, `last_observed` | when the tool first saw the change and when the content last changed (persisted per session) |
| `git_status` | `modified`, `added`, `deleted`, `renamed`, `untracked`, `conflicted`, `committed` (changed within a session and committed) … |
| `staged`, `unstaged`, `in_session` | flags |
| `owning_component`, `owning_component_name`, `module_id` | where the file belongs |
| `lines_added`, `lines_removed` | vs the activity baseline (`null` for binary/huge files) |
| `architecture_impact` | items `{kind, detail, severity}`: `dependency-added/removed`, `component-dependency-added/removed`, `dependency-kind-changed`, `cycle-introduced/resolved`, `module-added/removed`, `public-symbol-added/removed`, `cosmetic` |
| `impact_level` | `none`, `low`, `medium`, `high` |
| `tests_affected`, `is_test` | test files that (transitively) import the module, or the file itself |
| `configuration_affected`, `configuration_kind` | manifest, lockfile, CI, container, deployment, config |
| `changed_symbols` | innermost changed symbols in the file |
| `last_modified` | file modification time |

The activity report wraps events with `baseline` (session or HEAD), `summary`,
`diff` and `flow` (the affected-flow result: `nodes` with roles `changed`,
`caller`, `callee`, `path`, `entry`, `test`; `edges`; `entry_points` and `tests`
with shortest paths; `notes`).
