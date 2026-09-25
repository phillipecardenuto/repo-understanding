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
| `tags` | roles: `test`, `docs`, `generated`, `vendored`, `config`, `manifest`, `ci`, `container`, `entry-point`, `external`, `stdlib`, `unsupported`, `component`, `project`, `top-level`, `source-root` … |
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
| `metadata` | `type_checking_only`, `conditional_only`, `lazy_only`, `dynamic_only`, `test_only`, `external`, `imported_names`, `scope`, `spec`, `confirmed_by` … |

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

## SourceEvidence

`path`, `start_line`, `end_line`, `construct` (`import`, `from-import`,
`relative-from-import`, `dynamic-import`, `call`, `manifest-dependency`, `FROM`,
`COPY`, `console-script` …), `analyzer`, optional `excerpt`.

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
