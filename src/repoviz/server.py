"""Local analysis server for the live web application.

Standard library only (``http.server``).  The server is read-only with
respect to the repository; the only state it writes is the per-user session
and observation data in the state directory.

Security posture for a local tool:

* binds to ``127.0.0.1`` by default;
* rejects requests whose ``Host`` header is not a loopback name or the bound
  address (defeats DNS-rebinding attacks from web pages);
* every ``/api/*`` request needs an ``X-Repoviz`` header, which browsers cannot
  add to cross-site requests without a CORS preflight that this server never
  grants (so other sites can neither trigger nor embed API calls), and
  state-changing endpoints additionally require ``POST``;
* responses carry a strict Content-Security-Policy (no inline or evaluated
  script), ``nosniff`` and ``Cross-Origin-Resource-Policy: same-origin``;
* revisions coming from the UI are validated by :meth:`Git.resolve` and never
  reach a shell.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import traceback
import webbrowser
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .activity import observe
from .gitutil import GitError
from .model import RepositoryDiff
from .render.html import (
    asset_bytes,
    build_bundle,
    compact_diff_of,
    compact_snapshot,
    dumps,
    flow_node_ids,
    render_live_html,
)
from .repo import Repository, RepositoryError

log = logging.getLogger("repoviz.server")

ASSETS = {
    "/assets/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/mermaid.min.js": ("vendor/mermaid.min.js", "application/javascript; charset=utf-8"),
    "/assets/theme.json": ("theme.json", "application/json; charset=utf-8"),
}
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}
#: The browser went away mid-request (page reload, tab closed, cancelled fetch): nothing to answer, nothing to report.
CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" '
           b'fill="#4f46e5"/><circle cx="9" cy="10" r="4" fill="#fff"/><circle cx="23" cy="10" r="4" fill="#fff"/>'
           b'<circle cx="16" cy="23" r="4" fill="#fff"/><path d="M9 10L16 23L23 10" stroke="#fff" stroke-width="2" '
           b'fill="none"/></svg>')


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _splice(extra: dict[str, Any], body: bytes) -> bytes:
    """Prepend keys to a serialized JSON object without re-serializing it."""
    head = dumps(extra).encode("utf-8")
    if head == b"{}":
        return body
    if body == b"{}":
        return head
    return head[:-1] + b"," + body[1:]


class _Lru(OrderedDict):
    def __init__(self, size: int) -> None:
        super().__init__()
        self.size = size

    def put(self, key: Any, value: Any) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > self.size:
            self.popitem(last=False)


class AppState:
    """Shared state of a running server.

    Read-only analysis runs concurrently: the repository's snapshot and diff
    caches compute each result once even when requests race for it, so a slow
    review does not block the activity poll or static assets.  Only writes to
    the state directory (sessions, scope, notes, observations) are serialized.
    """

    def __init__(self, repo: Repository, *, auto_session: bool = False) -> None:
        self.repo = repo
        self.write_lock = threading.Lock()
        self.activity_lock = threading.Lock()
        self.cache_lock = threading.Lock()
        self.started_at = time.time()
        self._activity_cache: dict[str, Any] = {}
        self._activity_body: tuple[str, bytes] | None = None
        self._diff_bodies = _Lru(8)
        self._snapshot_body: tuple[Any, bytes] | None = None
        self._reviews = _Lru(6)
        if auto_session and repo.is_git and repo.current_session() is None:
            repo.state.start_session(repo.git, repo.root, label="started with repoviz serve")

    def bundle(self) -> bytes:
        """The page's initial data; the large snapshot part is serialized once per working-tree state."""
        snap = self.repo.snapshot("WORKTREE", "working tree")
        key = (snap.revision_id, snap.label, snap.metadata.get("config_fingerprint"))
        with self.cache_lock:
            hit = self._snapshot_body if self._snapshot_body and self._snapshot_body[0] == key else None
        if hit is None:
            hit = (key, dumps(compact_snapshot(snap.to_dict())).encode("utf-8"))
            with self.cache_lock:
                self._snapshot_body = hit
        data = build_bundle(self.repo, comparisons=[], include_activity=False, mode="live", embed_snapshot=False)
        return _splice(data, b'{"snapshot":' + hit[1] + b"}")

    def diff(self, query: dict[str, str]) -> bytes:
        mode = query.get("mode") or None
        base, target, spec = query.get("base") or None, query.get("target") or None, query.get("spec") or None
        if mode and mode not in ("all", "working", "staged", "unstaged", "session", "merge-base"):
            raise ApiError(400, f"unknown comparison mode {mode!r}")
        try:
            comp, diff = self.repo.compare(base, target, mode=mode, spec=spec)
        except (RepositoryError, GitError) as exc:
            raise ApiError(400, str(exc)) from exc
        with self.cache_lock:
            hit = self._diff_bodies.get(id(diff))
        if hit is None or hit[0] is not diff:
            hit = (diff, dumps({"diff": compact_diff_of(diff)}).encode("utf-8"))
            with self.cache_lock:
                self._diff_bodies.put(id(diff), hit)
        return _splice({"id": "live", "label": comp.label, "mode": comp.mode, "base": comp.base, "target": comp.target,
                        "base_label": comp.base_label, "target_label": comp.target_label}, hit[1])

    def snapshot(self, query: dict[str, str]) -> dict[str, Any]:
        try:
            return compact_snapshot(self.repo.snapshot(query.get("rev") or "WORKTREE").to_dict())
        except (RepositoryError, GitError) as exc:
            raise ApiError(400, str(exc)) from exc

    def activity(self) -> tuple[str, bytes]:
        """Return ``(etag, body)``; the body is only re-serialized when the repository changed."""
        with self.activity_lock:  # observe() records observation times
            result = observe(self.repo, cache=self._activity_cache)
            etag = result.get("etag") or ""
            if self._activity_body is None or self._activity_body[0] != etag:
                payload = {k: v for k, v in result.items() if k != "generated_at"}
                if isinstance(payload.get("diff"), RepositoryDiff):
                    payload["diff"] = compact_diff_of(payload["diff"], flow_node_ids(payload.get("flow")))
                self._activity_body = (etag, dumps(payload).encode("utf-8"))
            body = self._activity_body[1]
        return etag, _splice({"generated_at": result["generated_at"]}, body)

    def session_start(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.repo.is_git:
            raise ApiError(400, "sessions need a Git repository")
        label = str(body.get("label") or "")[:200]
        with self.write_lock:
            return self.repo.state.start_session(self.repo.git, self.repo.root, label=label).to_dict()

    def session_end(self) -> dict[str, Any]:
        with self.write_lock:
            s = self.repo.state.end_session(self.repo.git, self.repo.root)
        return s.to_dict() if s else {}

    # -- review ------------------------------------------------------------------------

    def review_targets(self) -> list[dict[str, Any]]:
        from .review import review_targets

        return [t.to_dict() for t in review_targets(self.repo)]

    def review(self, query: dict[str, str]) -> bytes:
        from .review import build_review, resolve_target, scope_for

        try:
            target = resolve_target(self.repo, query.get("id") or None, query.get("base") or None,
                                    query.get("target") or None, query.get("mode") or None)
            scope = scope_for(self.repo, target)
            commit = (query.get("commit") or "").strip() or None  # one step of the range, reviewed on its own
            sources = (self.repo.open_source(target.base), self.repo.open_source(target.target))
            key = (target.key, target.base, target.target, sources[0].revision_id, sources[1].revision_id,
                   tuple(scope.allowed), tuple(scope.protected), self.repo.config.fingerprint(), commit,
                   self.repo.git.head() if self.repo.git else None)  # commits and uncommitted work depend on HEAD
            with self.cache_lock:
                body = self._reviews.get(key)
            if body is None:
                report = (build_review(self.repo, target, scope=scope, commit=commit) if commit
                          else build_review(self.repo, target, scope=scope, sources=sources))
                report.pop("notes", None)
                body = dumps(report).encode("utf-8")
                with self.cache_lock:
                    self._reviews.put(key, body)
        except (RepositoryError, GitError, ValueError) as exc:
            raise ApiError(400, str(exc)) from exc
        return _splice({"notes": self.repo.state.load_notes(target.key)}, body)

    def notes(self, query: dict[str, str]) -> dict[str, Any]:
        key = query.get("key") or ""
        if not key:
            raise ApiError(400, "missing key")
        return {"key": key, "notes": self.repo.state.load_notes(key)}

    def save_notes(self, body: dict[str, Any]) -> dict[str, Any]:
        key, notes = body.get("key"), body.get("notes")
        if not isinstance(key, str) or not key or len(key) > 200 or not isinstance(notes, list) or len(notes) > 2000:
            raise ApiError(400, "expected {key, notes: [...]}")
        clean = [n for n in notes if isinstance(n, dict)]
        with self.write_lock:
            self.repo.state.save_notes(key, clean)
        return {"key": key, "saved": len(clean)}

    def save_scope(self, body: dict[str, Any]) -> dict[str, Any]:
        def globs_of(value: Any) -> list[str] | None:
            if value is None:
                return None
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ApiError(400, "scope patterns must be lists of strings")
            return [v.strip()[:500] for v in value if v.strip()][:200]

        with self.write_lock:
            session = self.repo.state.load_session(str(body.get("session_id") or ""))
            if session is None:
                raise ApiError(400, "unknown session")
            session = self.repo.state.update_scope(session, globs_of(body.get("allowed")),
                                                   globs_of(body.get("protected")))
        return session.to_dict()


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
}


def make_handler(state: AppState, allowed_hosts: set[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"repoviz/{__version__}"
        protocol_version = "HTTP/1.1"
        timeout = 60  # a client that stops sending cannot pin a thread forever

        def log_message(self, fmt: str, *args: Any) -> None:  # route through logging
            log.info("%s - %s", self.address_string(), fmt % args)

        # -- helpers -------------------------------------------------------------
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            return name in allowed_hosts

        def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self._responded = True  # from here on, a second (error) response would corrupt the stream
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            headers = {"Cache-Control": "no-store", **SECURITY_HEADERS, **(extra or {})}
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            body = payload if isinstance(payload, bytes) else dumps(payload).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            """Answer with an error unless a response already started or the client is gone."""
            if getattr(self, "_responded", False):
                self.close_connection = True
                return
            try:
                self._json(status, {"error": message})
            except CLIENT_GONE:
                self.close_connection = True

        def _client_gone(self, path: str) -> None:
            self.close_connection = True
            log.debug("client closed the connection during %s", path)

        def _query(self) -> dict[str, str]:
            q = parse_qs(urlparse(self.path).query, max_num_fields=50)
            return {k: v[-1][:2000] for k, v in q.items()}

        def _guard(self, path: str) -> bool:
            """Host check for everything; the X-Repoviz header for every API call."""
            if not self._host_ok():
                self._json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
                return False
            if (path.startswith("/api/") or self.command == "POST") and self.headers.get("X-Repoviz") != "1":
                # Browsers cannot add this header to cross-site requests without a CORS
                # preflight, which this server never grants.
                self._json(HTTPStatus.FORBIDDEN, {"error": "missing X-Repoviz header"})
                return False
            return True

        # -- routing -----------------------------------------------------------------
        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            self._responded = False
            path = urlparse(self.path).path
            try:
                if not self._guard(path):
                    return
            except CLIENT_GONE:
                self._client_gone(path)
                return
            try:
                if path in ("/", "/index.html"):
                    self._send(200, render_live_html(state.repo.name).encode("utf-8"), "text/html; charset=utf-8")
                elif path in ASSETS:
                    name, ctype = ASSETS[path]
                    self._send(200, asset_bytes(name), ctype, {"Cache-Control": "max-age=300"})
                elif path == "/favicon.ico" or path == "/favicon.svg":
                    self._send(200, FAVICON, "image/svg+xml", {"Cache-Control": "max-age=86400"})
                elif path == "/api/health":
                    self._json(200, {"ok": True, "version": __version__, "repository": str(state.repo.root)})
                elif path == "/api/bundle":
                    self._json(200, state.bundle())
                elif path == "/api/revisions":
                    self._json(200, state.repo.revisions())
                elif path == "/api/diff":
                    self._json(200, state.diff(self._query()))
                elif path == "/api/snapshot":
                    self._json(200, state.snapshot(self._query()))
                elif path == "/api/activity":
                    etag, body = state.activity()
                    tag = f'"{etag}"'
                    if tag in (self.headers.get("If-None-Match") or ""):
                        self._send(HTTPStatus.NOT_MODIFIED, b"", "application/json; charset=utf-8", {"ETag": tag})
                    else:
                        self._send(200, body, "application/json; charset=utf-8", {"ETag": tag})
                elif path == "/api/profile":
                    self._json(200, state.repo.discover().to_dict())
                elif path == "/api/review/targets":
                    self._json(200, state.review_targets())
                elif path == "/api/review":
                    self._json(200, state.review(self._query()))
                elif path == "/api/review/notes":
                    self._json(200, state.notes(self._query()))
                else:
                    self._json(404, {"error": f"not found: {path}"})
            except ApiError as exc:
                self._error(exc.status, str(exc))
            except CLIENT_GONE:
                self._client_gone(path)
            except Exception as exc:  # never leak a traceback page; log it instead
                log.error("error handling %s: %s\n%s", path, exc, traceback.format_exc())
                self._error(500, f"{type(exc).__name__}: {exc}")

        def do_POST(self) -> None:
            self._responded = False
            path = urlparse(self.path).path
            try:
                if not self._guard(path):
                    return
            except CLIENT_GONE:
                self._client_gone(path)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 2_000_000:
                self.close_connection = True
                self._error(413 if length > 0 else 400, "invalid or too large request body")
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(body, dict):
                    raise ApiError(400, "expected a JSON object")
                if path == "/api/session/start":
                    self._json(200, state.session_start(body))
                elif path == "/api/session/end":
                    self._json(200, state.session_end())
                elif path == "/api/review/notes":
                    self._json(200, state.save_notes(body))
                elif path == "/api/session/scope":
                    self._json(200, state.save_scope(body))
                else:
                    self._json(404, {"error": f"not found: {path}"})
            except ApiError as exc:
                self._error(exc.status, str(exc))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._error(400, "invalid JSON")
            except CLIENT_GONE:
                self._client_gone(path)
            except Exception as exc:
                log.error("error handling POST %s: %s\n%s", path, exc, traceback.format_exc())
                self._error(500, f"{type(exc).__name__}: {exc}")

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that disconnects is routine for a browser app; anything else keeps the default report.
        if isinstance(sys.exc_info()[1], CLIENT_GONE):
            log.debug("client %s closed the connection", client_address)
            return
        super().handle_error(request, client_address)


def create_server(repo: Repository, host: str = "127.0.0.1", port: int = 8765, *, auto_session: bool = False,
                  allowed_hosts: list[str] | None = None) -> ThreadingHTTPServer:
    state = AppState(repo, auto_session=auto_session)
    allowed = set(LOOPBACK_NAMES) | {host.lower()} | {h.lower() for h in allowed_hosts or []}
    server = _Server((host, port), make_handler(state, allowed))
    bound_port = server.server_address[1]
    server.repoviz_url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{bound_port}/"  # type: ignore[attr-defined]
    return server


def serve(repo: Repository, host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = False,
          auto_session: bool = False, allowed_hosts: list[str] | None = None) -> None:
    server = create_server(repo, host, port, auto_session=auto_session, allowed_hosts=allowed_hosts)
    url = server.repoviz_url  # type: ignore[attr-defined]
    print(f"repoviz {__version__} serving {repo.root} at {url}  (Ctrl+C to stop)", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: the server is reachable from other machines; it exposes repository content read-only.",
              flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.server_close()
        repo.close()
