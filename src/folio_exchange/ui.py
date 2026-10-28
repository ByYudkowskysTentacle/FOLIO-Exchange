"""`folio-exchange ui`: a small web page on this computer for looking at feeds and running them.

The page is served by Python's own HTTP server, so nothing else is installed and nothing is fetched from the
internet. It listens on 127.0.0.1 only, which any program or Windows account on this computer can reach, so
being on this computer is not enough:

- Each run of the server has a host name of its own, ``fx-<random>.localhost`` (browsers send any
  ``*.localhost`` name to this computer themselves). The browser is opened at a one-time link on that name
  (``http://fx-<random>.localhost:<port>/?t=<code>``). The server swaps a good code for an HttpOnly,
  SameSite=Strict cookie that belongs to that host name alone (no Domain), so the browser never sends it to
  127.0.0.1, localhost or any other name another server on this computer answers to, and sends the browser on
  to ``/``. The name is random and only the browser and this server's own window are told it. The page, and every ``/api/`` request, need that cookie; without it ``/`` is a short page saying how
  to open it properly. Only ``/static/`` files and ``/api/whoami`` are open.
- Every action (POST) also carries the per-session token written into the page, and a request from another
  web site is refused, so another site open in the same browser cannot start runs.
- Every request must name this computer in its Host header (a guard against DNS rebinding): the server's own
  host name, or 127.0.0.1 or localhost with its port (for tools, which send the cookie themselves).
- Starting the page again for the same site first asks the server on the port to prove it is this site's
  page: ``/api/whoami?nonce=`` answers with an HMAC of the nonce under a secret kept in the site's data folder,
  which only this Windows account can read. Only then does it ask that server (``POST /api/launch``, which
  needs the same kind of proof) to open the browser at a fresh one-time link itself, or to show one in its
  own window; the link never goes to the asker. Another program, or another Windows account, on the port can
  neither pass for the page nor make this person's browser open.

The page never sees a secret: it is told where each secret comes from, never its value.

What the page shows, and the bodies of its actions, are in ``views.py``, and long actions (check, preview, run,
release) run one at a time through ``jobs.py``, so the desktop app shares both. The page polls
``/api/jobs/<id>`` for their log lines and result.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from collections.abc import Callable
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from . import __version__, setupops, siteops, sources, views
from .config import SITE_FILE, load_site
from .errors import ConfigError, FxError
from .jobs import Jobs

log = logging.getLogger(__name__)

WEB = Path(__file__).parent / "web"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
STATIC = ("app.css", "app.js", "logo.svg")
MAX_BODY = 64 * 1024                 # the largest request body (a private key is the biggest thing sent)
CODE_SECONDS = 600                   # how long a one-time link to the page works
LAUNCH_GAP = 5.0                     # seconds between two launches that may open the browser
SECRET_FILE = "page.secret"          # in the site's data folder: proves which page is this site's
CHALLENGE_SECONDS = 60               # how long a launch challenge from /api/whoami works
NONCE = re.compile(r"[A-Za-z0-9_-]{16,128}")
SIGNED_OUT = ("This page is no longer connected to FOLIO-Exchange, usually because its window was closed or "
              "restarted. Use the FOLIO-Exchange launcher to open the page again.")


# ------------------------------------------------------------------ helpers
def _text(body: dict, key: str) -> str:
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{key}: expected text.")
    return value


def _dict(body: dict, key: str) -> dict:
    value = body.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: expected a set of settings.")
    return value


def _int(query: dict, key: str, default: int, low: int, high: int) -> int:
    """A whole-number query parameter, kept within low..high (not a number: ValueError, a bad request)."""
    return max(low, min(int(query.get(key, default)), high))


def _same(given: str | None, expected: str) -> bool:
    """Compare a header's value with a secret in constant time; any text is safe to pass."""
    return secrets.compare_digest((given or "").encode("utf-8"), expected.encode("utf-8"))


def site_id(site_root: Path) -> str:
    """Which site a page serves, as a hash: enough to recognise it, without saying where it is."""
    return hashlib.sha256(os.path.normcase(str(Path(site_root).resolve())).encode("utf-8")).hexdigest()


def _data_folder(site_root: Path) -> Path:
    """The site's data folder: as site.yaml sets it, or ``data`` while there is no site.yaml that loads."""
    try:
        return load_site(site_root).history_path.parent
    except (FxError, OSError, ValueError):
        return Path(site_root) / "data"


def page_secret(site_root: Path) -> bytes:
    """This site's page secret, made the first time it is needed. Only this Windows account can read the file
    (as for a saved private key), so only this account's own pages and starts can prove they are this site's."""
    folder = _data_folder(Path(site_root))
    path = folder / SECRET_FILE
    if not path.is_file():
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=".page.", suffix=".tmp")    # a new file of its own
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            setupops._owner_only(tmp)                   # before the secret is written into it
            tmp.write_text(secrets.token_hex(32), encoding="ascii")
            try:
                os.link(tmp, path)                      # never replaces a secret another start made meanwhile
            except FileExistsError:
                pass
            except OSError:                             # no hard links on this drive
                try:
                    os.rename(tmp, path)
                except FileExistsError:
                    pass
        finally:
            tmp.unlink(missing_ok=True)
    secret = bytes.fromhex(path.read_text(encoding="ascii").strip())
    if len(secret) < 16:
        raise ValueError(f"{path.name} is damaged; delete it and start the page again.")
    return secret


def proof(secret: bytes, purpose: str, port: int, nonce: str) -> str:
    """The answer to a challenge. It names the port that was asked, so a program on another port cannot pass a
    challenge on to the real page and hand back its answer as its own."""
    return hmac.new(secret, f"folio-exchange {purpose} {port} {nonce}".encode(), hashlib.sha256).hexdigest()


ADDRESS_HELP = ("(The address ends in .localhost, which Chrome, Edge and Firefox send to this computer themselves. "
                "If the browser says it cannot find it, use one of those, or ask IT to let *.localhost bypass "
                "the proxy.)")


def _print_link(url: str) -> str:
    print(f"Open the page with this link. It works once, within {CODE_SECONDS // 60} minutes:\n  {url}\n"
          f"{ADDRESS_HELP}", flush=True)
    return "window"


# ------------------------------------------------------------------ server
class App:
    def __init__(self, site_root: Path, show: Callable[[str], str | None] | None = None,
                 jobs: Jobs | None = None) -> None:
        self.site_root = Path(site_root)
        self.token = secrets.token_urlsafe(24)       # written into the page; every POST carries it
        self.session = secrets.token_urlsafe(32)     # the cookie a one-time link is swapped for
        self.codes: dict[str, float] = {}            # one-time link codes, and when each stops working
        self.show = show or _print_link              # puts a link in front of the person: "browser" or "window"
        self.last_launch = float("-inf")
        self.challenges: dict[str, float] = {}       # launch challenges from /api/whoami, and when each expires
        self.lock = threading.Lock()
        self.jobs = jobs or Jobs()                   # shared with another interface in this process, if given
        self.port = 0
        self.host = f"fx-{secrets.token_hex(8)}.localhost"    # this run's own name, so its cookie goes nowhere else

    # -- signing the browser in
    @property
    def cookie(self) -> str:
        return f"fx_{self.port}"

    @property
    def address(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def hosts(self) -> set[str]:
        """The Host headers this server answers to."""
        return {f"{self.host}:{self.port}", f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def link(self) -> str:
        """A new one-time link to the page."""
        code = secrets.token_urlsafe(24)
        with self.lock:
            now = time.monotonic()
            self.codes = {c: until for c, until in self.codes.items() if until > now}
            self.codes[code] = now + CODE_SECONDS
        return f"{self.address}?t={code}"

    def redeem(self, code: str) -> bool:
        """Whether ``code`` is a link code still in date; each works once."""
        with self.lock:
            until = self.codes.pop(code, None)
        return until is not None and until > time.monotonic()

    def signed_in(self, cookie_headers: list[str]) -> bool:
        for header in cookie_headers:
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == self.cookie and _same(value, self.session):
                    return True
        return False

    def whoami(self, nonce: str | None = None) -> dict:
        """For a second start of the page: which site this is and, given a nonce, proof that this server can
        read the site's secret, with a challenge for ``launch``. Nothing in it is private."""
        out = {"app": "folio-exchange", "version": __version__, "site": site_id(self.site_root)}
        if nonce is None or not NONCE.fullmatch(nonce):
            return out
        try:
            secret = page_secret(self.site_root)
        except (FxError, OSError, ValueError) as exc:
            log.warning("The page cannot read its secret, so a second start cannot recognise it: %s", exc)
            return out
        challenge = secrets.token_urlsafe(24)
        with self.lock:
            now = time.monotonic()
            self.challenges = {c: until for c, until in self.challenges.items() if until > now}
            if len(self.challenges) >= 32:              # however often it is asked, only a few are kept
                self.challenges.pop(next(iter(self.challenges)))
            self.challenges[challenge] = now + CHALLENGE_SECONDS
        # never the host name: a browser sends the cookie to any port under it, so it stays unknown to others
        return {**out, "proof": proof(secret, "whoami", self.port, nonce), "challenge": challenge}

    def check_launch(self, body: dict) -> None:
        """Before ``launch``: the asker must answer a challenge from ``whoami`` with the site's secret, so another
        program or Windows account cannot make the browser open. Raises PermissionError if it does not."""
        challenge, answer = body.get("challenge"), body.get("proof")
        if not (isinstance(challenge, str) and isinstance(answer, str)):
            raise PermissionError("Prove this is the same site's page first.")
        with self.lock:
            until = self.challenges.pop(challenge, None)          # each challenge works once
        if until is None or until <= time.monotonic():
            raise PermissionError("That challenge is unknown or out of date.")
        try:
            expected = proof(page_secret(self.site_root), "launch", self.port, challenge)
        except (FxError, OSError, ValueError):
            raise PermissionError("This page cannot read its secret.") from None
        if not _same(answer, expected):
            raise PermissionError("Wrong proof.")

    def launch(self) -> dict:
        """The page was started again for this site (``check_launch`` has passed): show it from here at a new
        link. The link never goes back to the asker, and the browser opens at most once every few seconds
        however often this is asked."""
        with self.lock:
            now = time.monotonic()
            if now - self.last_launch < LAUNCH_GAP:
                return {"shown": None}
            self.last_launch = now
        return {"shown": self.show(self.link())}

    # -- GET
    def _ready(self) -> bool:
        return (self.site_root / SITE_FILE).is_file()

    def get(self, path: str, query: dict) -> Any:
        root = self.site_root
        if path == "/api/site":
            job = self.jobs.running()
            state = setupops.site_state(root)
            if not state["ready"]:                      # first run, or a site.yaml that does not load
                return {"setup": state, "version": __version__, "root": str(root), "job": None}
            return {**views.site_summary(root), "job": job.as_dict() if job else None}
        if path == "/api/setup":
            return setupops.site_state(root)
        if path == "/api/choices":
            return setupops.tenant_choices(load_site(root))
        if path == "/api/recipes":
            return views.recipes_overview(root)
        if path == "/api/permissions":
            site, library, _feeds, _errors = views.load(root)
            return setupops.recipe_permissions(site, library, str(query.get("uses") or ""))
        if m := re.fullmatch(r"/api/feeds/([a-z0-9-]+)/settings", path):
            site, library, _feeds, _errors = views.load(root)
            return setupops.feed_settings(site, library, m.group(1))
        if m := re.fullmatch(r"/api/feeds/([a-z0-9-]+)", path):
            return views.feed_view(root, m.group(1))
        if m := re.fullmatch(r"/api/feeds/([a-z0-9-]+)/runs", path):
            return views.feed_runs(root, m.group(1), _int(query, "limit", 50, 1, 500))
        if m := re.fullmatch(r"/api/runs/(\d+)", path):
            return views.run_detail(root, int(m.group(1)))
        if m := re.fullmatch(r"/api/runs/(\d+)/rows", path):
            return views.run_rows(root, int(m.group(1)), query.get("output"), _int(query, "limit", 200, 1, 1000))
        if path == "/api/audit":
            return views.audit(root, _int(query, "limit", 50, 1, 500))
        if m := re.fullmatch(r"/api/jobs/([0-9a-f]+)", path):
            job = self.jobs.jobs.get(m.group(1))
            if job is None:
                raise LookupError("No such job")
            return job.as_dict(_int(query, "since", 0, 0, len(job.lines)))
        raise LookupError("Not found")

    # -- POST
    def post(self, path: str, body: dict) -> Any:
        root = self.site_root
        if path == "/api/setup/connection":
            return setupops.connect(root, _text(body, "base_url"), _text(body, "tenant"), _text(body, "username"),
                                    _text(body, "password") or None)
        if path == "/api/site/rename":
            def refuse(job) -> str:
                return (f"Wait for the {job.kind}{' of ' + job.feed if job.feed else ''} to finish, then rename "
                        "the folder.")

            # The rename is the running action, so no other starts meanwhile (here or in the window); the jobs'
            # lock is not held for it, which would freeze a window that asks while schtasks is slow to answer.
            with self.jobs.holding("rename", refuse):
                res = siteops.rename_site(root, _text(body, "name"), via="ui")
                self.site_root = Path(res["root"])      # this page now serves the renamed folder
            return res
        if path == "/api/recipes/check":
            site, library, _feeds, _errors = views.load(root)
            return sources.check(site, library, _text(body, "source"))
        if path == "/api/recipes/install":
            site, library, _feeds, _errors = views.load(root)
            return sources.install(site, library, _text(body, "source"), _text(body, "commit"), via="ui")
        if path == "/api/feeds":
            site, library, _feeds, _errors = views.load(root)
            return setupops.add_feed(site, library, _text(body, "name"), _text(body, "uses"),
                                     _dict(body, "values"))
        if m := re.fullmatch(r"/api/feeds/([a-z0-9-]+)/(settings|delivery|password|private-key|test-delivery)",
                             path):
            name, what = m.group(1), m.group(2)
            if what == "test-delivery":
                return self.jobs.start("test-delivery", name, lambda: views.check_delivery(root, name)).as_dict()
            site, library, _feeds, _errors = views.load(root)
            if what == "settings":
                return setupops.save_feed(site, library, name, _dict(body, "values"), version=_text(body, "version"))
            if what == "delivery":
                return setupops.save_delivery(site, library, name, _dict(body, "form"), version=_text(body, "version"))
            if what == "password":
                return setupops.store_sftp_password(site, library, name, _text(body, "password"))
            return setupops.store_private_key(site, library, name, _text(body, "key"),
                                              _text(body, "passphrase") or None)
        if path == "/api/check":
            return self.jobs.start("check", None, lambda: views.check_connection(root)).as_dict()
        if m := re.fullmatch(r"/api/runs/(\d+)/open-folder", path):
            return views.open_run_folder(root, int(m.group(1)))
        if m := re.fullmatch(r"/api/feeds/([a-z0-9-]+)/(preview|run|build|release|release-preview|pause|resume)",
                             path):
            name, action = m.group(1), m.group(2)
            if action in ("pause", "resume"):
                return views.set_enabled(root, name, action == "resume", via="ui")
            if action == "release-preview":
                return views.release_summary(root, name)
            if action == "preview":
                n = int(body.get("instances", 25))      # not a number: a bad request, before any job starts

                def work():
                    return views.preview(root, name, n)
            elif action in ("run", "build"):
                def work():
                    return views.run(root, name, deliver=action == "run")
            else:                                      # release
                if "force" in body:
                    raise ConfigError("The page never skips the safety checks. To send anyway, use the command "
                                      "line's release with --force, which is recorded in the activity log.")
                run_dir = body.get("run_dir")
                if not run_dir:
                    raise ConfigError("Say which run to release.")

                def work():
                    return views.release(root, name, run_dir)
            return self.jobs.start(action, name, work).as_dict()
        raise LookupError("Not found")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"folio-exchange/{__version__}"
        timeout = 30                                   # a request that stops half way does not hold a thread

        def log_message(self, fmt, *args) -> None:
            log.debug("ui %s", fmt % args)

        # -- plumbing
        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in app.hosts()

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, code: int, message: str) -> None:
            self._json(code, {"error": message})

        def _signed_in(self) -> bool:
            return app.signed_in(self.headers.get_all("Cookie") or [])

        def _signed_out(self) -> None:
            self._json(403, {"error": SIGNED_OUT, "signed_out": True})

        def _swap(self, code: str) -> None:
            """A one-time link: a good code becomes the cookie. Either way, go on to / so the code leaves the
            address bar; / then shows the page or says how to open it. The cookie has no Domain, so it belongs to
            the host name the link used (this run's own fx-….localhost name) and to nothing else."""
            extra = {"Location": "/"}
            if app.redeem(code):
                extra["Set-Cookie"] = f"{app.cookie}={app.session}; Path=/; HttpOnly; SameSite=Strict"
            self._send(303, b"", "text/plain; charset=utf-8", extra)

        def _body(self) -> dict | None:
            """The request's JSON object, or None once a 400 or 413 has been sent. The length must be plain
            digits and at most MAX_BODY: a bad one is refused before anything is read."""
            self.close_connection = True
            try:
                raw = (self.headers.get("Content-Length") or "0").strip()
                if not (raw.isascii() and raw.isdigit()):
                    raise ValueError(raw)
                length = int(raw)
            except ValueError:
                self._error(400, "Bad request: the request's length is not a number.")
                return None
            if length > MAX_BODY:
                self._error(413, f"That request is too large (more than {MAX_BODY // 1024} KB).")
                return None
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._error(400, "Not valid JSON.")
                return None
            if not isinstance(body, dict):
                self._error(400, "Expected a JSON object.")
                return None
            return body

        def _call(self, fn, *args) -> None:
            try:
                self._json(200, fn(*args))
            except LookupError as exc:
                self._error(404, str(exc).strip("'\""))
            except FxError as exc:
                self._error(409 if "still running" in str(exc) else 400, str(exc))
            except ValueError as exc:                  # e.g. ?limit=abc
                self._error(400, f"Bad request: {exc}")
            except Exception as exc:  # noqa: BLE001 - the page shows the message; details go to the log
                log.error("UI request failed: %s", traceback.format_exc())
                self._error(500, f"{type(exc).__name__}: {exc}")

        # -- verbs
        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            if not self._host_ok():
                return self._error(403, "Open the page at the address the folio-exchange window printed.")
            u = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path.startswith("/static/"):          # the package's own files: nothing private in them
                name = u.path[len("/static/"):]
                if name not in STATIC:
                    return self._error(404, "Not found")
                return self._send(200, (WEB / name).read_bytes(), CONTENT_TYPES[Path(name).suffix])
            if u.path == "/api/whoami":
                return self._json(200, app.whoami(query.get("nonce")))
            if u.path in ("/", "/index.html"):
                if "t" in query:
                    return self._swap(query["t"])
                if not self._signed_in():                # never the token without the cookie
                    return self._send(403, (WEB / "signed-out.html").read_bytes(), CONTENT_TYPES[".html"])
                html = (WEB / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", app.token)
                return self._send(200, html.encode("utf-8"), CONTENT_TYPES[".html"])
            if u.path.startswith("/api/"):
                if not self._signed_in():
                    return self._signed_out()
                return self._call(app.get, u.path, query)
            return self._error(404, "Not found")

        do_HEAD = do_GET

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._error(403, "Wrong address.")
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{h}" for h in app.hosts()}:
                return self._error(403, "Requests from other web sites are refused.")
            u = urlparse(self.path)
            if u.path == "/api/launch":                 # needs the site's secret, not the cookie; the link never comes back
                body = self._body()
                if body is None:
                    return None
                try:
                    app.check_launch(body)
                except PermissionError as exc:
                    return self._error(403, str(exc))
                return self._call(app.launch)
            if not self._signed_in():
                return self._signed_out()
            if not _same(self.headers.get("X-FX-Token"), app.token):
                return self._error(403, "Missing or wrong page token; reload the page.")
            body = self._body()
            if body is None:
                return None
            return self._call(app.post, u.path, body)

    return Handler


DEFAULT_PORT = 8765


class _Server(ThreadingHTTPServer):
    """Owns its port alone. On Windows, SO_REUSEADDR (http.server's default) lets a second server bind a port
    that is already listening, and requests then reach either one; SO_EXCLUSIVEADDRUSE refuses that."""

    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _ask(port: int, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(f"http://127.0.0.1:{port}{path}", data=data, method="GET" if body is None else "POST",
                  headers={"Content-Type": "application/json"} if body is not None else {})
    with build_opener(ProxyHandler({})).open(req, timeout=5) as r:
        return json.loads(r.read())


def _verify(port: int, site_root: Path) -> dict | None:
    """The answer of the page on ``port`` if it proved it is this site's page (it knows the site's secret),
    else None. Whatever else answers there is not trusted, however it describes itself."""
    nonce = secrets.token_urlsafe(24)
    try:
        secret = page_secret(site_root)
        found = _ask(port, f"/api/whoami?nonce={nonce}")
    except (OSError, HTTPException, ValueError, FxError):
        return None
    if not isinstance(found, dict) or found.get("site") != site_id(site_root):
        return None
    answer = found.get("proof")
    if not isinstance(answer, str) or not _same(answer, proof(secret, "whoami", port, nonce)):
        return None
    return found


def _open_for(port: int, site_root: Path) -> bool:
    """Whether the page already listening on ``port`` is this site's, proved with the site's secret."""
    return _verify(port, site_root) is not None


def _launch(port: int, site_root: Path, challenge: object) -> str | None:
    """Ask the page on ``port`` (already verified) to show itself at a new link, answering its challenge:
    "browser", "window", or None (just shown, or it refused)."""
    try:
        if not isinstance(challenge, str):
            return None
        body = {"challenge": challenge, "proof": proof(page_secret(site_root), "launch", port, challenge)}
        return _ask(port, "/api/launch", body).get("shown")
    except (OSError, HTTPException, ValueError, AttributeError, FxError):
        return None


def _already_open(port: int, shown: str | None, want_browser: bool) -> str:
    text = f"The page for this site is already open on port {port}"
    if shown == "browser":
        return text + "; opening it in your browser."
    if shown == "window":
        return text + ". The FOLIO-Exchange window that is already running shows a new link to it."
    if want_browser:
        return (text + ". It was opened a moment ago. If no browser window appeared, close the other "
                "FOLIO-Exchange window (Ctrl+C) and start it again.")
    return text + ". A browser already showing it can carry on; to open it anew, start this without --no-browser."


def _shower(want_browser: bool, link: Callable[[], str]) -> Callable[[str], str]:
    """How this server shows a one-time link: in the browser (with a second link printed in its own window, for
    another browser), or printed in its own window."""
    def show(url: str) -> str:
        if want_browser and webbrowser.open(url):
            print(f"To open it in another browser on this computer, use this link once within "
                  f"{CODE_SECONDS // 60} minutes:\n  {link()}\n{ADDRESS_HELP}", flush=True)
            return "browser"
        return _print_link(url)
    return show


def serve(site_root: Path, port: int | None = None, open_browser: bool = True) -> None:
    """Serve the page until Ctrl+C. Port 0 picks a free port; no port means 8765, or a free one if that is taken.
    When this site's page is already open on the port (and proves it), ask it to open the browser instead of
    starting another."""
    want_browser = open_browser and not os.environ.get("FOLIO_EXCHANGE_NO_BROWSER")
    app = App(site_root)
    app.show = _shower(want_browser, app.link)
    try:
        httpd = _Server(("127.0.0.1", DEFAULT_PORT if port is None else port), make_handler(app))
    except OSError as exc:
        taken = DEFAULT_PORT if port is None else port
        found = _verify(taken, Path(site_root)) if taken else None
        if found is not None:
            shown = _launch(taken, Path(site_root), found.get("challenge")) if want_browser else None
            print(_already_open(taken, shown, want_browser), flush=True)
            return
        if port is not None:
            raise FxError(f"Could not listen on 127.0.0.1:{port} ({exc.strerror or exc}). Try another port with "
                          "--port.") from None
        httpd = _Server(("127.0.0.1", 0), make_handler(app))
        print(f"Port {DEFAULT_PORT} is in use by something that is not this site's page, so the page uses port "
              f"{httpd.server_address[1]} instead. Open the page only from the links in this window: whatever "
              f"holds port {DEFAULT_PORT} could pretend to be this page and see what you type into it.", flush=True)
    app.port = httpd.server_address[1]
    print(f"FOLIO-Exchange is open at {app.address}  (this computer only). Press Ctrl+C to stop.", flush=True)
    if want_browser:
        threading.Timer(0.5, lambda: app.show(app.link())).start()
    else:
        _print_link(app.link())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _finish_running_job(app)
        print("\nStopped.", file=sys.stderr)
    finally:
        httpd.server_close()


def _finish_running_job(app: App) -> None:
    """On Ctrl+C, let a running run or release finish rather than cut a delivery off half way."""
    job = app.jobs.running()
    if job is None or job.thread is None:
        return
    what = f"{job.kind} of {job.feed}" if job.feed else job.kind
    print(f"\nA {what} is still running; waiting for it to finish. Press Ctrl+C again to stop anyway.",
          file=sys.stderr, flush=True)
    try:
        while job.thread.is_alive():
            job.thread.join(0.5)
        print(f"The {what} finished: {job.status}.", file=sys.stderr)
    except KeyboardInterrupt:
        print("Stopped while it was still running; check `folio-exchange history` for what it did.", file=sys.stderr)


def _in_thread(app: App) -> ThreadingHTTPServer:
    httpd = _Server(("127.0.0.1", 0), make_handler(app))
    app.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, name="fx-page", daemon=True).start()
    return httpd


def serve_in_thread(site_root: Path, jobs: Jobs | None = None) -> tuple[ThreadingHTTPServer, App]:
    """For an app in this process (the desktop app, for views it does not have yet): serve the page on a free
    port in a daemon thread, printing nothing. Pass the app's ``jobs`` so the page and the app run one action at
    a time between them; open the page at ``app.link()``. Stop it with ``httpd.shutdown()``.

    A `folio-exchange ui` started for the same site meanwhile looks only on its own port, so it does not find
    this page: it serves one of its own, with its own job runner (the site's run lock still stops a feed being
    sent twice at once). That stays so until the page is retired (D8 of docs/desktop-plan.md)."""
    app = App(site_root, show=lambda url: None, jobs=jobs)
    return _in_thread(app), app


def start_in_thread(site_root: Path, show: Callable[[str], str] | None = None) -> tuple[ThreadingHTTPServer, App]:
    """For tests: serve on a free port in a background thread. Nothing is opened; ``show`` gets any new link."""
    app = App(site_root, show=show)
    return _in_thread(app), app
