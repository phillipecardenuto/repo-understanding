"""Constant and configuration values, before → after (#14)."""

from __future__ import annotations

import textwrap

from repoviz.values import HIDDEN, MAX_VALUE, config_format, value_changes, values_of


def shown(path: str, text: str, language: str | None) -> dict[str, str]:
    return {name: v.display for name, v in values_of(path, textwrap.dedent(text), language).items()}


def changes(path: str, before: str, after: str, language: str | None = None) -> dict[str, dict]:
    return {c["name"]: c for c in value_changes(path, textwrap.dedent(before), textwrap.dedent(after), language)}


def test_python_constants_are_literals_only() -> None:
    values = shown("app/limits.py", """
        import os

        MAX_IMAGES = 20
        TIMEOUT: float = 2.5
        MODES = ("fast", "safe")
        _PRIVATE_LIMIT = -1
        DEFAULT = None
        FROM_ENV = os.environ.get("X", "1")   # a call: never evaluated, not listed
        DERIVED = MAX_IMAGES * 2              # a name: not listed
        lower_case = 3                        # not a constant
        __all__ = ["MAX_IMAGES"]
        A, B = 1, 2                           # unpacking: not listed


        def f():
            INSIDE = 1                        # not module level
    """, "python")
    assert values == {"MAX_IMAGES": "20", "TIMEOUT": "2.5", "MODES": "('fast', 'safe')", "_PRIVATE_LIMIT": "-1",
                      "DEFAULT": "None"}


def test_python_settings_classes() -> None:
    values = shown("app/settings.py", """
        from dataclasses import dataclass
        from pydantic import BaseModel
        from pydantic_settings import BaseSettings


        class Settings(BaseSettings):
            debug: bool = False
            port: int = 8000
            database_url: str
            workers = 4


        @dataclass
        class CacheConfig:
            ttl: int = 60


        class ApiConfig(BaseModel):
            retries: int = 3


        class User(BaseModel):
            name: str = "anonymous"        # a model, not settings
    """, "python")
    assert values == {"Settings.debug": "False", "Settings.port": "8000", "Settings.workers": "4",
                      "CacheConfig.ttl": "60", "ApiConfig.retries": "3"}


def test_javascript_and_typescript_constants() -> None:
    values = shown("web/config.ts", """
        export const maxUploads = 10;
        export const API_BASE: string = "https://example.test/api";
        const RETRY_DELAY_MS = 1_500
        const local = 5;
        export const handler = () => 1;
        export const greeting = `hello ${name}`;
        export const FLAGS = { a: 1 };
        export const ENABLED = true; // on by default
    """, "typescript")
    assert values == {"maxUploads": "10", "API_BASE": '"https://example.test/api"', "RETRY_DELAY_MS": "1_500",
                      "ENABLED": "true"}


def test_configuration_files_by_path() -> None:
    assert config_format("config/app.toml") == "toml" and config_format("settings/prod.yaml") == "yaml"
    assert config_format("vite.config.json") == "json" and config_format("appsettings.Development.json") == "json"
    assert config_format(".env.example") == "dotenv"
    for ignored in ("package.json", "pyproject.toml", "data/items.json", ".github/workflows/ci.yml", ".env"):
        assert config_format(ignored) is None, ignored
    toml = shown("config/app.toml", """
        name = "svc"
        [server]
        port = 80
        hosts = ["a", "b"]
        [server.tls]
        cert = "x"
    """, None)
    assert toml == {"name": '"svc"', "server.port": "80", "server.hosts": '["a", "b"]'}
    yaml = shown("config/app.yaml", "debug: false\nlimits:\n  max_images: 20\n", None)
    assert yaml == {"debug": "false", "limits.max_images": "20"}
    assert shown("config/broken.json", "{not json", None) == {}
    env = shown(".env.example", """
        # comment
        export DEBUG=false
        SECRET_KEY="change-me"
        MAX_TOKENS=4096
    """, None)
    assert env == {"DEBUG": "false", "SECRET_KEY": HIDDEN, "MAX_TOKENS": "4096"}


def test_changes_show_before_and_after_and_catch_changes_past_the_truncation() -> None:
    long_a, long_b = "x" * 300 + "a", "x" * 300 + "b"
    got = changes("app/limits.py", f"""
        MAX_IMAGES = 20
        GONE = 1
        SAME = "s"
        LONG = "{long_a}"
    """, f"""
        MAX_IMAGES = 200
        SAME = "s"
        LONG = "{long_b}"
        NEW = [1, 2]
    """, "python")
    assert set(got) == {"MAX_IMAGES", "GONE", "LONG", "NEW"}
    assert (got["MAX_IMAGES"]["value_before"], got["MAX_IMAGES"]["value"], got["MAX_IMAGES"]["status"]) == ("20", "200", "modified")
    assert got["MAX_IMAGES"]["line"] == 2 and got["MAX_IMAGES"]["kind"] == "constant"
    assert got["GONE"]["status"] == "removed" and got["GONE"]["value"] is None
    assert got["NEW"]["status"] == "added" and got["NEW"]["value_before"] is None
    assert got["LONG"]["value_before"] == got["LONG"]["value"] and len(got["LONG"]["value"]) == MAX_VALUE
    assert not any("weakens" in c for c in got.values())


def test_secrets_are_hidden_or_redacted() -> None:
    secret = "sk-live-abcdefghijklmnopqrstuvwxyz123456"
    got = changes("app/keys.py", 'API_KEY = "old"\nFALLBACK = "none"\nMAX_TOKENS = 1000\nAUTH_ENABLED = True\n',
                  f'API_KEY = "{secret}"\nFALLBACK = "{secret}"\nMAX_TOKENS = 4096\nAUTH_ENABLED = False\n', "python")
    assert got["API_KEY"]["value_before"] == got["API_KEY"]["value"] == HIDDEN  # a secret-looking name: never shown
    assert "[redacted]" in got["FALLBACK"]["value"] and secret not in str(got)  # a secret-looking value: redacted
    assert got["MAX_TOKENS"]["value"] == "4096"  # a quantity under a secret-looking word is shown
    assert got["AUTH_ENABLED"]["weakens"] == "a security check switched off"


def test_safety_flags_switched_the_risky_way() -> None:
    got = changes("app/settings.py", """
        DEBUG = False
        VERIFY_SSL = True
        VERIFY_EMAIL = True
        ALLOW_ALL_ORIGINS = False
        REQUEST_TIMEOUT = 30
        CORS_ALLOWED_ORIGINS = ["https://app.example"]
        ALLOWED_HOSTS = ["app.example"]
        CSRF_ENABLED = True
        SESSION_COOKIE_SECURE = True
        TEMPLATE_DEBUG = True
        UPLOAD_TIMEOUT = 0
        INSECURE_MODE = True
    """, """
        DEBUG = True
        VERIFY_SSL = False
        VERIFY_EMAIL = False
        ALLOW_ALL_ORIGINS = True
        REQUEST_TIMEOUT = None
        CORS_ALLOWED_ORIGINS = ["*"]
        ALLOWED_HOSTS = ["*"]
        CSRF_ENABLED = False
        SESSION_COOKIE_SECURE = False
        TEMPLATE_DEBUG = False
        UPLOAD_TIMEOUT = None
        INSECURE_MODE = False
    """, "python")
    weakens = {name: c.get("weakens") for name, c in got.items()}
    assert weakens == {
        "DEBUG": "debug mode switched on",
        "VERIFY_SSL": "TLS or certificate verification switched off",
        "VERIFY_EMAIL": "verification switched off",
        "ALLOW_ALL_ORIGINS": "allow-all switched on",
        "REQUEST_TIMEOUT": "timeout removed (0 or none)",
        "CORS_ALLOWED_ORIGINS": "any origin or host allowed (*)",
        "ALLOWED_HOSTS": "any origin or host allowed (*)",
        "CSRF_ENABLED": "a security check switched off",
        "SESSION_COOKIE_SECURE": "a security check switched off",
        "TEMPLATE_DEBUG": None,  # switched off: safer
        "UPLOAD_TIMEOUT": None,  # 0 → None: it already had no timeout
        "INSECURE_MODE": None,  # not a known safety setting
    }
    # Settings classes and configuration files use the last part of the name.
    assert changes("app/settings.py", "class Settings(BaseSettings):\n    debug: bool = False\n",
                   "class Settings(BaseSettings):\n    debug: bool = True\n", "python")["Settings.debug"]["weakens"]
    assert changes("config/app.toml", "[http]\nverify_ssl = true\n", "[http]\nverify_ssl = false\n")["http.verify_ssl"]["weakens"]
    env = changes(".env.example", "DEBUG=off\nREQUEST_TIMEOUT=30\n", "DEBUG=on\nREQUEST_TIMEOUT=0\n")
    assert env["DEBUG"]["weakens"] and env["REQUEST_TIMEOUT"]["weakens"]


def test_unparsable_or_huge_files_have_no_values() -> None:
    assert values_of("a.py", "MAX = (\n", "python") == {}
    assert values_of("a.py", "MAX = 1\n" + "#" * 600_000, "python") == {}
    assert values_of("a.go", "const Max = 1\n", "go") == {}
    assert value_changes("a.py", None, "MAX = 1\n", "python")[0]["status"] == "added"
