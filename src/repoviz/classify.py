"""Path classification tables: languages, manifests, tests, generated code...

These tables are deliberately generic: they recognise common conventions of
many ecosystems and never assume a particular layout.  Every decision made
here can be overridden through configuration.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from . import globs

# --------------------------------------------------------------------------
# Languages
# --------------------------------------------------------------------------

#: extension -> (language, kind) where kind is programming | markup | data | docs | config
EXTENSIONS: dict[str, tuple[str, str]] = {
    ".py": ("python", "programming"), ".pyi": ("python", "programming"), ".pyw": ("python", "programming"),
    ".pyx": ("cython", "programming"), ".pxd": ("cython", "programming"),
    ".js": ("javascript", "programming"), ".mjs": ("javascript", "programming"),
    ".cjs": ("javascript", "programming"), ".jsx": ("javascript", "programming"),
    ".ts": ("typescript", "programming"), ".tsx": ("typescript", "programming"),
    ".mts": ("typescript", "programming"), ".cts": ("typescript", "programming"),
    ".vue": ("vue", "programming"), ".svelte": ("svelte", "programming"),
    ".go": ("go", "programming"), ".rs": ("rust", "programming"),
    ".java": ("java", "programming"), ".kt": ("kotlin", "programming"), ".kts": ("kotlin", "programming"),
    ".scala": ("scala", "programming"), ".sc": ("scala", "programming"), ".groovy": ("groovy", "programming"),
    ".clj": ("clojure", "programming"), ".cljs": ("clojure", "programming"), ".cljc": ("clojure", "programming"),
    ".cs": ("csharp", "programming"), ".fs": ("fsharp", "programming"), ".fsx": ("fsharp", "programming"),
    ".vb": ("visualbasic", "programming"),
    ".c": ("c", "programming"), ".h": ("c", "programming"),
    ".cc": ("cpp", "programming"), ".cpp": ("cpp", "programming"), ".cxx": ("cpp", "programming"),
    ".hpp": ("cpp", "programming"), ".hh": ("cpp", "programming"), ".hxx": ("cpp", "programming"),
    ".m": ("objective-c", "programming"), ".mm": ("objective-c", "programming"),
    ".swift": ("swift", "programming"), ".rb": ("ruby", "programming"), ".rake": ("ruby", "programming"),
    ".php": ("php", "programming"), ".pl": ("perl", "programming"), ".pm": ("perl", "programming"),
    ".lua": ("lua", "programming"), ".r": ("r", "programming"), ".jl": ("julia", "programming"),
    ".dart": ("dart", "programming"), ".ex": ("elixir", "programming"), ".exs": ("elixir", "programming"),
    ".erl": ("erlang", "programming"), ".hrl": ("erlang", "programming"), ".hs": ("haskell", "programming"),
    ".ml": ("ocaml", "programming"), ".mli": ("ocaml", "programming"), ".elm": ("elm", "programming"),
    ".zig": ("zig", "programming"), ".nim": ("nim", "programming"), ".cr": ("crystal", "programming"),
    ".sol": ("solidity", "programming"), ".v": ("verilog", "programming"), ".vhd": ("vhdl", "programming"),
    ".sh": ("shell", "programming"), ".bash": ("shell", "programming"), ".zsh": ("shell", "programming"),
    ".fish": ("shell", "programming"), ".ps1": ("powershell", "programming"), ".bat": ("batch", "programming"),
    ".sql": ("sql", "programming"), ".proto": ("protobuf", "programming"), ".thrift": ("thrift", "programming"),
    ".graphql": ("graphql", "programming"), ".gql": ("graphql", "programming"),
    ".tf": ("terraform", "programming"), ".hcl": ("hcl", "config"), ".nix": ("nix", "programming"),
    ".ipynb": ("jupyter", "programming"),
    ".html": ("html", "markup"), ".htm": ("html", "markup"), ".css": ("css", "markup"),
    ".scss": ("scss", "markup"), ".sass": ("sass", "markup"), ".less": ("less", "markup"),
    ".xml": ("xml", "data"), ".json": ("json", "data"), ".jsonc": ("json", "data"), ".csv": ("csv", "data"),
    ".yaml": ("yaml", "config"), ".yml": ("yaml", "config"), ".toml": ("toml", "config"),
    ".ini": ("ini", "config"), ".cfg": ("ini", "config"), ".conf": ("config", "config"),
    ".properties": ("properties", "config"), ".env": ("dotenv", "config"),
    ".md": ("markdown", "docs"), ".markdown": ("markdown", "docs"), ".rst": ("restructuredtext", "docs"),
    ".adoc": ("asciidoc", "docs"), ".txt": ("text", "docs"), ".tex": ("latex", "docs"),
}

FILENAMES: dict[str, tuple[str, str]] = {
    "Dockerfile": ("dockerfile", "config"), "Containerfile": ("dockerfile", "config"),
    "Makefile": ("make", "programming"), "GNUmakefile": ("make", "programming"),
    "CMakeLists.txt": ("cmake", "config"), "Jenkinsfile": ("groovy", "config"),
    "Rakefile": ("ruby", "programming"), "Gemfile": ("ruby", "config"), "Vagrantfile": ("ruby", "config"),
    "BUILD": ("starlark", "config"), "BUILD.bazel": ("starlark", "config"), "WORKSPACE": ("starlark", "config"),
    "Procfile": ("procfile", "config"), "Justfile": ("just", "config"), "justfile": ("just", "config"),
}

#: Human readable names.
DISPLAY_NAMES = {
    "python": "Python", "javascript": "JavaScript", "typescript": "TypeScript", "go": "Go", "rust": "Rust",
    "java": "Java", "kotlin": "Kotlin", "csharp": "C#", "fsharp": "F#", "cpp": "C++", "c": "C",
    "objective-c": "Objective-C", "ruby": "Ruby", "php": "PHP", "shell": "Shell", "dockerfile": "Dockerfile",
}


def display_language(lang: str | None) -> str:
    if not lang:
        return "unknown"
    return DISPLAY_NAMES.get(lang, lang.replace("-", " ").title() if lang.islower() else lang)


def language_of(path: str, overrides: dict[str, str] | None = None) -> tuple[str | None, str | None]:
    """Return ``(language, kind)`` for a path, or ``(None, None)``."""
    name = posixpath.basename(path)
    if name in FILENAMES:
        return FILENAMES[name]
    if name.startswith("Dockerfile.") or name.endswith(".dockerfile") or name.endswith(".Dockerfile"):
        return ("dockerfile", "config")
    ext = posixpath.splitext(name)[1].lower()
    if overrides and ext in overrides:
        lang = overrides[ext]
        return (lang, EXTENSIONS.get(ext, (lang, "programming"))[1] if ext in EXTENSIONS else "programming")
    return EXTENSIONS.get(ext, (None, None))


# --------------------------------------------------------------------------
# Manifests and project files
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestKind:
    kind: str
    ecosystem: str
    lockfile: bool = False
    workspace: bool = False


_MANIFESTS_EXACT: dict[str, ManifestKind] = {
    "pyproject.toml": ManifestKind("pyproject", "python"),
    "setup.py": ManifestKind("setup.py", "python"),
    "setup.cfg": ManifestKind("setup.cfg", "python"),
    "Pipfile": ManifestKind("pipfile", "python"),
    "environment.yml": ManifestKind("conda", "python"), "environment.yaml": ManifestKind("conda", "python"),
    "package.json": ManifestKind("package.json", "npm"),
    "pnpm-workspace.yaml": ManifestKind("pnpm-workspace", "npm", workspace=True),
    "lerna.json": ManifestKind("lerna", "npm", workspace=True),
    "nx.json": ManifestKind("nx", "npm", workspace=True),
    "turbo.json": ManifestKind("turbo", "npm", workspace=True),
    "rush.json": ManifestKind("rush", "npm", workspace=True),
    "deno.json": ManifestKind("deno", "deno"), "deno.jsonc": ManifestKind("deno", "deno"),
    "tsconfig.json": ManifestKind("tsconfig", "npm"),
    "Cargo.toml": ManifestKind("cargo", "cargo"),
    "go.mod": ManifestKind("go.mod", "go"), "go.work": ManifestKind("go.work", "go", workspace=True),
    "pom.xml": ManifestKind("maven", "maven"),
    "build.gradle": ManifestKind("gradle", "gradle"), "build.gradle.kts": ManifestKind("gradle", "gradle"),
    "settings.gradle": ManifestKind("gradle-settings", "gradle", workspace=True),
    "settings.gradle.kts": ManifestKind("gradle-settings", "gradle", workspace=True),
    "build.sbt": ManifestKind("sbt", "scala"),
    "composer.json": ManifestKind("composer", "php"), "Gemfile": ManifestKind("gemfile", "ruby"),
    "mix.exs": ManifestKind("mix", "elixir"), "pubspec.yaml": ManifestKind("pubspec", "dart"),
    "Package.swift": ManifestKind("swiftpm", "swift"), "CMakeLists.txt": ManifestKind("cmake", "cmake"),
    "meson.build": ManifestKind("meson", "meson"), "WORKSPACE": ManifestKind("bazel", "bazel", workspace=True),
    "MODULE.bazel": ManifestKind("bazel", "bazel", workspace=True),
    "Directory.Build.props": ManifestKind("msbuild-props", "dotnet"),
    # lock files
    "poetry.lock": ManifestKind("poetry.lock", "python", lockfile=True),
    "uv.lock": ManifestKind("uv.lock", "python", lockfile=True),
    "Pipfile.lock": ManifestKind("pipfile.lock", "python", lockfile=True),
    "pdm.lock": ManifestKind("pdm.lock", "python", lockfile=True),
    "package-lock.json": ManifestKind("package-lock", "npm", lockfile=True),
    "npm-shrinkwrap.json": ManifestKind("npm-shrinkwrap", "npm", lockfile=True),
    "yarn.lock": ManifestKind("yarn.lock", "npm", lockfile=True),
    "pnpm-lock.yaml": ManifestKind("pnpm-lock", "npm", lockfile=True),
    "bun.lockb": ManifestKind("bun.lockb", "npm", lockfile=True), "bun.lock": ManifestKind("bun.lock", "npm", lockfile=True),
    "Cargo.lock": ManifestKind("cargo.lock", "cargo", lockfile=True),
    "go.sum": ManifestKind("go.sum", "go", lockfile=True),
    "Gemfile.lock": ManifestKind("gemfile.lock", "ruby", lockfile=True),
    "composer.lock": ManifestKind("composer.lock", "php", lockfile=True),
    "gradle.lockfile": ManifestKind("gradle.lockfile", "gradle", lockfile=True),
    "packages.lock.json": ManifestKind("nuget.lock", "dotnet", lockfile=True),
    "pubspec.lock": ManifestKind("pubspec.lock", "dart", lockfile=True),
    "mix.lock": ManifestKind("mix.lock", "elixir", lockfile=True),
}

_REQUIREMENTS_RE = re.compile(r"(^|/)(requirements[\w.-]*|[\w.-]*requirements|constraints[\w.-]*)\.(txt|in)$", re.I)
_REQUIREMENTS_DIR_RE = re.compile(r"(^|/)requirements/[\w.-]+\.(txt|in)$", re.I)


def manifest_kind(path: str) -> ManifestKind | None:
    name = posixpath.basename(path)
    if name in _MANIFESTS_EXACT:
        return _MANIFESTS_EXACT[name]
    if _REQUIREMENTS_RE.search(path) or _REQUIREMENTS_DIR_RE.search(path):
        return ManifestKind("requirements", "python")
    lower = name.lower()
    if lower.endswith(".sln"):
        return ManifestKind("sln", "dotnet", workspace=True)
    if lower.endswith((".csproj", ".fsproj", ".vbproj")):
        return ManifestKind("msbuild-project", "dotnet")
    if lower.endswith(".gemspec"):
        return ManifestKind("gemspec", "ruby")
    if lower.endswith(".cabal"):
        return ManifestKind("cabal", "haskell")
    return None


# --------------------------------------------------------------------------
# CI, containers, deployment, docs, architecture configuration
# --------------------------------------------------------------------------


def ci_provider(path: str) -> str | None:
    if re.match(r"^\.github/workflows/[^/]+\.ya?ml$", path):
        return "github-actions"
    name = posixpath.basename(path)
    table = {
        ".gitlab-ci.yml": "gitlab-ci", ".travis.yml": "travis", "azure-pipelines.yml": "azure-pipelines",
        "bitbucket-pipelines.yml": "bitbucket-pipelines", "Jenkinsfile": "jenkins", ".drone.yml": "drone",
        "appveyor.yml": "appveyor", ".appveyor.yml": "appveyor", "cloudbuild.yaml": "google-cloud-build",
        "cloudbuild.yml": "google-cloud-build", "buildspec.yml": "aws-codebuild", ".woodpecker.yml": "woodpecker",
    }
    if name in table:
        return table[name]
    if path.startswith(".circleci/") and name in ("config.yml", "config.yaml"):
        return "circleci"
    if path.startswith(".buildkite/") and name.endswith((".yml", ".yaml")):
        return "buildkite"
    if path.startswith(".gitlab/ci/") and name.endswith((".yml", ".yaml")):
        return "gitlab-ci"
    if path.startswith(".azure-pipelines/") and name.endswith((".yml", ".yaml")):
        return "azure-pipelines"
    return None


def container_kind(path: str) -> str | None:
    name = posixpath.basename(path)
    lower = name.lower()
    if name in ("Dockerfile", "Containerfile") or name.startswith("Dockerfile.") or lower.endswith(".dockerfile"):
        return "dockerfile"
    if re.match(r"^(docker-)?compose([.-][\w.-]+)?\.ya?ml$", lower):
        return "compose"
    if lower == ".dockerignore":
        return "dockerignore"
    return None


def compose_variant(path: str) -> str:
    """The variant a compose file declares: ``docker-compose.prod.yml`` and ``docker-compose-prod.yml`` →
    ``prod``, ``compose.override.yaml`` → ``override``, the plain file → ``base``."""
    m = re.match(r"^(?:docker-)?compose[.-]([\w.-]+?)\.ya?ml$", posixpath.basename(path).lower())
    return m.group(1) if m else "base"


#: Infrastructure image names → kind (the image's last path segment, without tag, is matched; see image_kind).
IMAGE_KINDS: dict[str, tuple[str, ...]] = {
    "database": ("postgres", "postgis", "timescaledb", "mysql", "mariadb", "percona", "mongo", "mongodb",
                 "cassandra", "scylla", "cockroach", "couchdb", "couchbase", "neo4j", "arangodb", "clickhouse",
                 "mssql", "sqlserver", "oracle", "influxdb", "questdb", "surrealdb", "dynamodb-local", "cockroachdb",
                 "supabase", "edgedb", "yugabyte"),
    "cache": ("redis", "memcached", "valkey", "keydb", "dragonfly", "varnish"),
    "queue": ("rabbitmq", "kafka", "redpanda", "nats", "activemq", "artemis", "pulsar", "nsq", "mosquitto",
              "emqx", "elasticmq", "beanstalkd"),
    "object-store": ("minio", "azurite", "seaweedfs", "localstack", "fake-gcs-server", "garage", "ceph"),
    "search": ("elasticsearch", "opensearch", "solr", "meilisearch", "typesense", "milvus", "qdrant", "weaviate",
               "chroma", "chromadb", "vespa", "manticore", "zincsearch"),
    "monitoring": ("prometheus", "grafana", "jaeger", "all-in-one", "zipkin", "loki", "promtail", "tempo",
                   "alertmanager", "flower", "kibana", "attu", "pgadmin", "pgadmin4", "adminer", "mongo-express",
                   "redisinsight", "redis-commander", "portainer", "cadvisor", "node-exporter", "otel-collector",
                   "opentelemetry-collector", "opentelemetry-collector-contrib", "datadog", "netdata",
                   "uptime-kuma", "sentry", "mailhog", "mailpit", "phpmyadmin", "kafka-ui", "akhq"),
    "proxy": ("nginx", "traefik", "haproxy", "caddy", "envoy", "httpd", "kong", "apache", "openresty", "squid"),
    "coordination": ("etcd", "zookeeper", "consul", "vault"),
}
_IMAGE_KIND = {name: kind for kind, names in IMAGE_KINDS.items() for name in names}


def image_kind(image: str | None) -> str | None:
    """The infrastructure kind of a container image (``redis:7-alpine`` → ``cache``,
    ``quay.io/coreos/etcd:v3.5`` → ``coordination``), or ``None`` when the image is not a known one."""
    if not image:
        return None
    name = image.split("@")[0].rsplit("/", 1)[-1].split(":")[0].lower()
    if name in _IMAGE_KIND:
        return _IMAGE_KIND[name]
    base = re.sub(r"[-_](server|standalone|alpine|oss|community|ce|ee|db)$", "", name)
    return _IMAGE_KIND.get(base) or next((k for n, k in _IMAGE_KIND.items() if name.startswith(n + "-")), None)


def deployment_kind(path: str) -> str | None:
    name = posixpath.basename(path)
    lower = name.lower()
    table = {
        "chart.yaml": "helm", "kustomization.yaml": "kustomize", "kustomization.yml": "kustomize",
        "skaffold.yaml": "skaffold", "serverless.yml": "serverless", "serverless.yaml": "serverless",
        "procfile": "procfile", "fly.toml": "fly.io", "app.yaml": "app-engine", "vercel.json": "vercel",
        "netlify.toml": "netlify", "render.yaml": "render", "railway.json": "railway", "heroku.yml": "heroku",
        "tiltfile": "tilt", "nomad.hcl": "nomad", "samconfig.toml": "aws-sam", "template.yaml": None,
        "pulumi.yaml": "pulumi", "cdk.json": "aws-cdk", "ansible.cfg": "ansible", "docker-bake.hcl": "docker-bake",
    }
    parts = path.lower().split("/")
    # Directory context wins over file names (a k8s manifest may well be called app.yaml).
    if lower.endswith((".yaml", ".yml")) and lower not in ("chart.yaml", "kustomization.yaml", "kustomization.yml") \
            and any(p in ("k8s", "kubernetes", "manifests", "deploy", "deployment", "helm", "charts") for p in parts[:-1]):
        return "kubernetes"
    if lower in table and table[lower]:
        return table[lower]
    if lower.endswith(".tf") or lower.endswith(".tf.json"):
        return "terraform"
    return None


ARCHITECTURE_CONFIG = {
    ".importlinter": "import-linter", "importlinter.ini": "import-linter",
    ".dependency-cruiser.js": "dependency-cruiser", ".dependency-cruiser.cjs": "dependency-cruiser",
    ".dependency-cruiser.json": "dependency-cruiser", ".dependency-cruiser.mjs": "dependency-cruiser",
    ".madgerc": "madge", "tach.toml": "tach", "tach.yml": "tach", "tach.yaml": "tach",
    "packwerk.yml": "packwerk", "package.yml": "packwerk", ".pydeps": "pydeps", "deptry.toml": "deptry",
    "nx.json": "nx", "project.json": "nx", "workspace.dsl": "structurizr", "architecture.dsl": "structurizr",
    "CODEOWNERS": "codeowners", "archunit.properties": "archunit", "deny.toml": "cargo-deny",
    ".repoviz.toml": "repoviz", "sonar-project.properties": "sonarqube", "lint-staged.config.js": None,
}


def architecture_config_tool(path: str) -> str | None:
    name = posixpath.basename(path)
    tool = ARCHITECTURE_CONFIG.get(name)
    if tool:
        return tool
    lower = path.lower()
    if re.search(r"(^|/)(adr|adrs|decisions|architecture/decisions)/[^/]+\.(md|rst|adoc)$", lower):
        return "architecture-decision-records"
    if re.search(r"(^|/)(architecture|arch)\.(md|rst|adoc)$", lower):
        return "architecture-document"
    if lower.endswith((".c4", ".puml", ".plantuml")):
        return "diagram-source"
    return None


DOC_DIR_NAMES = {"docs", "doc", "documentation", "wiki", "manual", "guides", "website", "site"}
DOC_TOOL_FILES = {"mkdocs.yml": "mkdocs", "mkdocs.yaml": "mkdocs", "conf.py": "sphinx?", "docusaurus.config.js": "docusaurus",
                  "docusaurus.config.ts": "docusaurus", "book.toml": "mdbook", "antora.yml": "antora",
                  "_config.yml": "jekyll", "hugo.toml": "hugo", "Doxyfile": "doxygen", ".readthedocs.yaml": "readthedocs",
                  ".readthedocs.yml": "readthedocs"}

# --------------------------------------------------------------------------
# Tests, generated code, vendored code
# --------------------------------------------------------------------------

TEST_DIR_NAMES = {"test", "tests", "__tests__", "spec", "specs", "testing", "e2e", "integration_tests",
                  "unit_tests", "functional_tests", "test_suite", "testsuite", "cypress", "playwright"}
_TEST_FILE_RE = re.compile(
    r"(^test_.*\.py$|.*_test\.py$|^conftest\.py$|.*_test\.go$|.*\.(test|spec)\.[cm]?[jt]sx?$|"
    r".*(Test|Tests|IT)\.(java|kt|scala|cs|fs|vb)$|.*_spec\.rb$|.*_test\.rb$|.*Test\.php$|.*_test\.exs?$|"
    r".*_test\.dart$|.*_tests?\.rs$|^test.*\.(c|cc|cpp)$|.*_test\.(c|cc|cpp)$)"
)


def is_test_path(path: str, extra_patterns: list[str] | None = None) -> bool:
    name = posixpath.basename(path)
    if _TEST_FILE_RE.match(name):
        return True
    parts = path.split("/")[:-1]
    if any(p.lower() in TEST_DIR_NAMES for p in parts):
        return True
    if "src/test" in path or "src/androidTest" in path:
        return True
    return bool(extra_patterns and globs.match_any(path, extra_patterns))


GENERATED_DIR_PATTERNS = ["build/", "dist/", "out/", "target/", "**/bin/Debug/", "**/bin/Release/",
                          "generated/", "_generated/", "__generated__/", ".generated/", "coverage/",
                          "htmlcov/", "site-packages/", "*.egg-info/", ".next/", "storybook-static/"]
GENERATED_FILE_PATTERNS = ["*_pb2.py", "*_pb2_grpc.py", "*_pb2.pyi", "*.pb.go", "*.pb.gw.go", "*_grpc.pb.go",
                           "*.min.js", "*.min.css", "*.bundle.js", "*.map", "*.generated.*", "*.g.dart",
                           "*.freezed.dart", "*.designer.cs", "*.g.cs", "*_generated.go", "zz_generated*.go",
                           "*.gen.go", "*.gen.ts"]
VENDOR_DIR_PATTERNS = ["vendor/", "third_party/", "third-party/", "thirdparty/", "_vendor/"]
GENERATED_MARKERS = ("@generated", "DO NOT EDIT", "Code generated by", "AUTO-GENERATED", "autogenerated by",
                     "Autogenerated by", "This file is automatically generated", "Generated by the protocol buffer")


def generated_reason(path: str, extra: list[str] | None = None) -> str | None:
    if extra and globs.match_any(path, extra):
        return "configured"
    for p in GENERATED_FILE_PATTERNS:
        if globs.match(path, p):
            return f"file pattern {p}"
    for p in GENERATED_DIR_PATTERNS:
        if globs.match(path, p):
            return f"directory pattern {p}"
    return None


def has_generated_marker(text: str) -> bool:
    head = text[:1500]
    return any(m in head for m in GENERATED_MARKERS)


def vendored_reason(path: str) -> str | None:
    for p in VENDOR_DIR_PATTERNS:
        if globs.match(path, p):
            return f"directory pattern {p}"
    return None


CONFIG_EXTENSIONS = (".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".properties", ".env", ".json", ".jsonc")
CONFIG_NAMES = {".editorconfig", ".gitattributes", ".gitignore", ".npmrc", ".nvmrc", ".python-version",
                ".tool-versions", "Makefile", "justfile", "Justfile", "tox.ini", "noxfile.py", "pytest.ini",
                ".pre-commit-config.yaml", ".flake8", "mypy.ini", ".pylintrc", ".eslintrc", ".eslintrc.js",
                ".eslintrc.cjs", ".eslintrc.json", "eslint.config.js", "eslint.config.mjs", ".prettierrc",
                "babel.config.js", "vite.config.ts", "vite.config.js", "webpack.config.js", "jest.config.js",
                "jest.config.ts", "vitest.config.ts", "rollup.config.js", "tsconfig.json", ".babelrc",
                "settings.py", "config.py", "conftest.py", "manage.py", "alembic.ini", ".env.example"}


def config_kind(path: str) -> str | None:
    """Return a short description when ``path`` is configuration (build, CI, env...)."""
    mk = manifest_kind(path)
    if mk:
        return "lockfile" if mk.lockfile else "manifest"
    if ci_provider(path):
        return "ci"
    if container_kind(path):
        return "container"
    if deployment_kind(path):
        return "deployment"
    name = posixpath.basename(path)
    if name in CONFIG_NAMES or name.startswith(".env"):
        return "config"
    lower = name.lower()
    if lower.endswith((".json", ".jsonc")):
        if "config" in lower or name.startswith(".") or lower.endswith("rc.json"):
            return "config"
    elif lower.endswith(CONFIG_EXTENSIONS):
        return "config"
    if "/config/" in f"/{path}" or "/configs/" in f"/{path}" or "/settings/" in f"/{path}":
        return "config"
    return None
