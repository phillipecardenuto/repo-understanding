"""Local analysis server for the live web application.

Standard library only (``http.server``).  The server is read-only with
respect to the repository; the only state it writes is the per-user session
and observation data in the state directory.

Security posture for a local tool:

* binds to ``127.0.0.1`` by default;
* rejects requests whose ``Host`` header is not a loopback name or the bound
  address (defeats DNS-rebinding attacks from web pages);
* state-changing endpoints require ``POST`` with an ``X-Repoviz`` header, which
  browsers cannot add to cross-site requests without a CORS preflight that
  this server never grants;
* revisions coming from the UI are validated by :meth:`Git.resolve` and never
  reach a shell.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .activity import observe
from .gitutil import GitError
from .render.html import asset_bytes, build_bundle, dumps, render_live_html
from .repo import Repository, RepositoryError

log = logging.getLogger("repoviz.server")

ASSETS = {
    "/assets/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/mermaid.min.js": ("vendor/mermaid.min.js", "application/javascript; charset=utf-8"),
    "/assets/theme.json": ("theme.json", "application/json; charset=utf-8"),
}
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}
FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" '
           b'fill="#4f46e5"/><circle cx="9" cy="10" r="4" fill="#fff"/><circle cx="23" cy="10" r="4" fill="#fff"/>'
           b'<circle cx="16" cy="23" r="4" fill="#fff"/><path d="M9 10L16 23L23 10" stroke="#fff" stroke-width="2" '
           b'fill="none"/></svg>')


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class AppState:
    """Shared, thread-safe state of a running server."""

    def __init__(self, repo: Repository, *, auto_session: bool = False) -> None:
        self.repo = repo
        self.lock = threading.RLock()
        self.started_at = time.time()
        if auto_session and repo.is_git and repo.current_session() is None:
            repo.state.start_session(repo.git, repo.root, label="started with repoviz serve")

    def bundle(self) -> dict[str, Any]:
        with self.lock:
            data = build_bundle(self.repo, comparisons=[], include_activity=False, mode="live")
        data["comparisons"] = []
        return data

    def diff(self, query: dict[str, str]) -> dict[str, Any]:
        mode = query.get("mode") or None
        base, target, spec = query.get("base") or None, query.get("target") or None, query.get("spec") or None
        if mode and mode not in ("all", "working", "staged", "unstaged", "session", "merge-base"):
            raise ApiError(400, f"unknown comparison mode {mode!r}")
        with self.lock:
            try:
                comp, diff = self.repo.compare(base, target, mode=mode, spec=spec)
            except (RepositoryError, GitError) as exc:
                raise ApiError(400, str(exc)) from exc
        return {"id": "live", "label": comp.label, "mode": comp.mode, "base": comp.base, "target": comp.target,
                "base_label": comp.base_label, "target_label": comp.target_label, "diff": diff.to_dict()}

    def snapshot(self, query: dict[str, str]) -> dict[str, Any]:
        with self.lock:
            try:
                return self.repo.snapshot(query.get("rev") or "WORKTREE").to_dict()
            except (RepositoryError, GitError) as exc:
                raise ApiError(400, str(exc)) from exc

    def activity(self) -> dict[str, Any]:
        with self.lock:
            return observe(self.repo)

    def session_start(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.repo.is_git:
            raise ApiError(400, "sessions need a Git repository")
        label = str(body.get("label") or "")[:200]
        with self.lock:
            return self.repo.state.start_session(self.repo.git, self.repo.root, label=label).to_dict()

    def session_end(self) -> dict[str, Any]:
        with self.lock:
            s = self.repo.state.end_session(self.repo.git, self.repo.root)
        return s.to_dict() if s else {}

    # -- review ------------------------------------------------------------------------

    def review_targets(self) -> list[dict[str, Any]]:
        from .review import review_targets

        with self.lock:
            return [t.to_dict() for t in review_targets(self.repo)]

    def review(self, query: dict[str, str]) -> dict[str, Any]:
        from .review import build_review, resolve_target

        with self.lock:
            try:
                target = resolve_target(self.repo, query.get("id") or None, query.get("base") or None,
                                        query.get("target") or None)
                report = build_review(self.repo, target)
            except (RepositoryError, GitError, ValueError) as exc:
                raise ApiError(400, str(exc)) from exc
            report["notes"] = self.repo.state.load_notes(target.key)
        return report

    def notes(self, query: dict[str, str]) -> dict[str, Any]:
        key = query.get("key") or ""
        if not key:
            raise ApiError(400, "missing key")
        return {"key": key, "notes": self.repo.state.load_notes(key)}

    def save_notes(self, body: dict[str, Any]) -> dict[str, Any]:
        key, notes = body.get("key"), body.get("notes")
        if not isinstance(key, str) or not key or not isinstance(notes, list) or len(notes) > 2000:
            raise ApiError(400, "expected {key, notes: [...]}")
        clean = [n for n in notes if isinstance(n, dict)]
        with self.lock:
            self.repo.state.save_notes(key, clean)
        return {"key": key, "saved": len(clean)}

    def save_scope(self, body: dict[str, Any]) -> dict[str, Any]:
        def globs_of(value: Any) -> list[str] | None:
            if value is None:
                return None
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ApiError(400, "scope patterns must be lists of strings")
            return [v.strip() for v in value if v.strip()][:200]

        with self.lock:
            session = self.repo.state.load_session(str(body.get("session_id") or ""))
            if session is None:
                raise ApiError(400, "unknown session")
            session = self.repo.state.update_scope(session, globs_of(body.get("allowed")),
                                                   globs_of(body.get("protected")))
        return session.to_dict()


def make_handler(state: AppState, allowed_hosts: set[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"repoviz/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # route through logging
            log.info("%s - %s", self.address_string(), fmt % args)

        # -- helpers -------------------------------------------------------------
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            return name in allowed_hosts

        def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def _query(self) -> dict[str, str]:
            q = parse_qs(urlparse(self.path).query)
            return {k: v[-1] for k, v in q.items()}

        # -- routing -----------------------------------------------------------------
        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            if not self._host_ok():
                self._json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
                return
            path = urlparse(self.path).path
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
                    self._json(200, state.activity())
                elif path == "/api/profile":
                    self._json(200, state.repo.discover().to_dict())
                elif path == "/api/review/targets":
                    self._json(200, state.review_targets())
                elif path == "/api/review":
                    self._json(200, state.review(self._query()))
                elif path == "/api/review/notes":
                    self._json(200, state.notes(self._query()))
                else:
                    ctype = mimetypes.guess_type(path)[0] or "text/plain"
                    self._json(404, {"error": f"not found: {path}", "type": ctype})
            except ApiError as exc:
                self._json(exc.status, {"error": str(exc)})
            except Exception as exc:  # never leak a traceback page; log it instead
                log.error("error handling %s: %s\n%s", path, exc, traceback.format_exc())
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_POST(self) -> None:
            if not self._host_ok():
                self._json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
                return
            if self.headers.get("X-Repoviz") != "1":
                self._json(HTTPStatus.FORBIDDEN, {"error": "missing X-Repoviz header"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > 2_000_000:
                self._json(413, {"error": "request too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(body, dict):
                    raise ApiError(400, "expected a JSON object")
                path = urlparse(self.path).path
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
                self._json(exc.status, {"error": str(exc)})
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON"})
            except Exception as exc:
                log.error("error handling POST %s: %s", self.path, exc)
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def create_server(repo: Repository, host: str = "127.0.0.1", port: int = 8765, *, auto_session: bool = False,
                  allowed_hosts: list[str] | None = None) -> ThreadingHTTPServer:
    state = AppState(repo, auto_session=auto_session)
    allowed = set(LOOPBACK_NAMES) | {host.lower()} | {h.lower() for h in allowed_hosts or []}
    server = ThreadingHTTPServer((host, port), make_handler(state, allowed))
    server.daemon_threads = True
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
