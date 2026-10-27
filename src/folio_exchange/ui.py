"""`folio-exchange ui`: a small web page on this computer for looking at feeds and running them.

The page is served by Python's own HTTP server, so nothing else is installed and nothing is fetched from the
internet. It listens on 127.0.0.1 only. Every request must name this computer in its Host header (a guard
against DNS rebinding), and every action (POST) must carry the per-session token that is written into the
page, so another web site open in the same browser cannot start runs. The page never sees a secret: it is
told where each secret comes from, never its value.

Long actions (check, preview, run, release) run one at a time in a background thread; the page polls
``/api/jobs/<id>`` for their log lines and result.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, delivery, feedops, reads, runner, schedule
from .client import probe
from .config import Site, load_site, password_note
from .errors import ConfigError, FxError, LockBusy
from .fields import FIELDS
from .history import History
from .recipe import Feed, RecipeLibrary, load_all

log = logging.getLogger(__name__)

WEB = Path(__file__).parent / "web"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
STATIC = ("app.css", "app.js", "logo.svg")


# ------------------------------------------------------------------ jobs
class Job:
    def __init__(self, kind: str, feed: str | None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.feed = kind, feed
        self.status = "running"                     # running | done | error
        self.started = time.monotonic()
        self.seconds: float | None = None
        self.lines: list[str] = []
        self.result: Any = None
        self.error: str | None = None
        self.thread: threading.Thread | None = None

    def as_dict(self, since: int = 0) -> dict:
        fresh = self.lines[since:]               # one snapshot, so log and log_total always agree
        return {"id": self.id, "kind": self.kind, "feed": self.feed, "status": self.status,
                "seconds": round(self.seconds if self.seconds is not None else time.monotonic() - self.started, 1),
                "log": fresh, "log_total": since + len(fresh), "result": self.result, "error": self.error}


class _JobLog(logging.Handler):
    """Copies log records written by one job's thread into that job."""

    def __init__(self, job: Job, thread_id: int) -> None:
        super().__init__(logging.INFO)
        self.job, self.thread_id = job, thread_id
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread_id and record.name.startswith("folio_exchange"):
            self.job.lines.append(self.format(record))


class Jobs:
    """At most one job at a time: the site lock allows one run anyway, and one at a time is easier to follow."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None

    def start(self, kind: str, feed: str | None, work) -> Job:
        with self.lock:
            if self.current is not None and self.current.status == "running":
                raise FxError(f"Another action is still running ({self.current.kind}"
                              f"{' ' + self.current.feed if self.current.feed else ''}). Wait for it to finish.")
            job = Job(kind, feed)
            self.jobs[job.id] = job
            self.current = job

        def body() -> None:
            handler = _JobLog(job, threading.get_ident())
            root = logging.getLogger()
            root.addHandler(handler)
            try:
                job.result = work()
                job.status = "done"
            except LockBusy as exc:
                job.status, job.error = "error", str(exc)
            except FxError as exc:
                job.status, job.error = "error", str(exc)
            except Exception as exc:  # noqa: BLE001 - shown to the person; the details go to the log
                job.status, job.error = "error", f"{type(exc).__name__}: {exc}"
                log.debug("%s", traceback.format_exc())
            finally:
                job.seconds = round(time.monotonic() - job.started, 1)
                root.removeHandler(handler)

        job.thread = threading.Thread(target=body, name=f"fx-job-{job.id}", daemon=True)
        job.thread.start()
        return job

    def running(self) -> Job | None:
        job = self.current
        return job if job is not None and job.status == "running" else None


# ------------------------------------------------------------------ helpers
def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _load(site_root: Path) -> tuple[Site, RecipeLibrary, list[Feed], dict[str, str]]:
    site = load_site(site_root)
    library = RecipeLibrary(site.recipe_dirs)
    feeds, errors = load_all(site, library)
    return site, library, feeds, errors


def _feed(site_root: Path, name: str) -> tuple[Site, RecipeLibrary, Feed, list[Feed]]:
    site, library, feeds, errors = _load(site_root)
    for f in feeds:
        if f.name == name:
            return site, library, f, feeds
    if name in errors:
        raise ConfigError(errors[name])
    raise ConfigError(f"No feed called {name!r}")


def _run_row(r: dict) -> dict:
    """A history row for the page: no report body (it can be large), but the summary fields."""
    rep = r.get("report") or {}
    return {
        "id": r["id"], "feed": r["feed"], "status": r["status"], "trigger": r.get("trigger"),
        "started": r.get("started"), "finished": r.get("finished"), "items": r.get("items"), "bibs": r.get("bibs"),
        "recipe": f"{r.get('recipe')}@{r.get('recipe_version')}", "error": r.get("error"),
        "folder": Path(r["run_dir"]).name if r.get("run_dir") else None, "delivered": bool(r.get("delivered")),
        "files": r.get("files") or [], "destinations": r.get("destinations") or [], "next": rep.get("next"),
        "held": rep.get("held"), "released_by": rep.get("released_by"), "released_after": rep.get("released_after"),
    }


def last_real_run(hist: History, feed: str) -> dict | None:
    """The latest run that was not a preview: what the feed's status means to a person."""
    return next((r for r in hist.runs(feed, 100) if r.get("status") != "preview"), None)


def _layout(feed: Feed) -> list[dict]:
    out = []
    for o in feed.outputs:
        entry: dict[str, Any] = {"name": o["name"], "format": o["format"], "file_name": o.get("file_name")}
        if o["format"] == "delimited":
            entry.update({
                "extension": o.get("extension"), "delimiter": o.get("delimiter"), "header": o.get("header"),
                "empty_value": o.get("empty_value"), "date_format": o.get("date_format"),
                "columns": [{"header": c["header"], "field": c["field"], "required": bool(c.get("required")),
                             "description": FIELDS[c["field"]].description if c["field"] in FIELDS else ""}
                            for c in o["columns"]],
            })
        else:
            itf = o.get("item_field") or {}
            entry.update({"embed_items": o.get("embed_items"), "split_every": o.get("split_every"),
                          "item_field": {"tag": itf.get("tag"), "subfields": itf.get("subfields") or {}}})
        out.append(entry)
    return out


def feed_detail(site: Site, library: RecipeLibrary, feed: Feed, hist: History) -> dict:
    now = datetime.now(timezone.utc)
    s = schedule.parse(feed.schedule)
    last = last_real_run(hist, feed.name)
    newer = library.newer_major(feed.uses)
    try:
        waiting = runner.releasable_run(hist, feed, None)
    except FxError:
        waiting = None
    return {
        "name": feed.name, "title": feed.title, "enabled": feed.enabled, "uses": feed.uses,
        "recipe": {"ref": feed.recipe.ref, "title": feed.recipe.title, "vendor": feed.recipe.data.get("vendor")},
        "schedule": {"text": feed.schedule, "describe": schedule.describe(s),
                     "next": _iso(schedule.next_slot(s, now, site.timezone)) if feed.enabled else None},
        "delivery": {"transport": feed.delivery.get("transport"), "where": delivery.describe_destination(feed.delivery),
                     "delivers": feed.delivers},
        "file_prefix": feed.file_prefix,
        "filters": {k: v for k, v in feed.selection.items() if v not in ([], None)},
        "options": feed.options, "checks": feed.checks,
        "legacy": {k: v for k, v in feed.legacy.items() if v},
        "layout": _layout(feed),
        "last_run": _run_row(last) if last else None,
        "waiting": _run_row(waiting) if waiting else None,
        "newer_recipe": newer.ref if newer else None,
        "overdue": runner.overdue(site, feed, hist, now),
        "file": str(feed.path),
    }


def site_summary(site_root: Path) -> dict:
    site, library, feeds, errors = _load(site_root)
    with History(site.history_path) as hist:
        items = []
        for f in feeds:
            last = last_real_run(hist, f.name)
            items.append({"name": f.name, "title": f.title, "recipe": f.recipe.ref, "enabled": f.enabled,
                          "last_status": last["status"] if last else None,
                          "last_started": last["started"] if last else None})
        warnings = feedops.site_warnings(site, library, feeds, errors, hist, datetime.now(timezone.utc))
    return {
        "warnings": warnings,
        "version": __version__, "name": site.name, "root": str(site.root),
        "folio": {"base_url": site.folio["base_url"], "tenant": site.folio["tenant"],
                  "username": site.folio["username"], "password": site.folio_password_source()},
        "alerts": {"on": site.alerts.get("on"), "webhook": bool(site.alerts.get("webhook")),
                   "email": bool(site.alerts.get("email"))},
        "feeds": items, "problems": errors, "recipe_problems": library.problems(),
    }


def check_connection(site_root: Path) -> dict:
    """Log in and read one page of every endpoint the enabled feeds need (structured, for the page)."""
    site, _library, feeds, errors = _load(site_root)
    needed = set().union(*(set(f.reads) for f in feeds if f.enabled)) or set(reads.READS)
    out: dict[str, Any] = {"password": site.folio_password_source(), "note": password_note(site),
                           "endpoints": [], "denied": [], "problems": errors, "logged_in": False}
    client = runner.client_for(site)
    client.connect()
    out["logged_in"] = True
    out["endpoints"] = probe(client, needed)
    out["denied"] = [r["permission"] for r in out["endpoints"] if r["status"] == "denied"]
    out["ok"] = all(r["status"] == "ok" for r in out["endpoints"])
    return out


def read_rows(site: Site, feed: Feed, row: dict, output: str | None, limit: int = 200) -> dict:
    """The first rows of a run's delimited output, for the page's table. Only files recorded for the run."""
    rep = row.get("report") or {}
    outputs = (rep.get("build") or {}).get("outputs") or {}
    delimited = [o for o in feed.outputs if o["format"] == "delimited" and outputs.get(o["name"])]
    if not delimited:
        return {"output": None, "header": [], "rows": [], "total": 0}
    o = next((d for d in delimited if d["name"] == output), delimited[0])
    name = outputs[o["name"]][0]
    if name not in {f["name"] for f in row.get("files") or []}:
        raise ConfigError("That file is not part of this run.")
    path = Path(row["run_dir"]) / name
    if not path.is_file():
        return {"output": o["name"], "file": name, "header": [], "rows": [], "total": 0, "missing": True}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter=o["delimiter"])
        header = next(reader, []) if o.get("header", True) else [c["header"] for c in o["columns"]]
        rows, total = [], 0
        for r in reader:
            total += 1
            if len(rows) < limit:
                rows.append(r)
    return {"output": o["name"], "outputs": [d["name"] for d in delimited], "file": name, "header": header,
            "rows": rows, "total": total}


# ------------------------------------------------------------------ server
class App:
    def __init__(self, site_root: Path) -> None:
        self.site_root = Path(site_root)
        self.token = secrets.token_urlsafe(24)
        self.jobs = Jobs()
        self.port = 0

    # -- GET
    def get(self, path: str, query: dict) -> Any:
        if path == "/api/site":
            job = self.jobs.running()
            return {**site_summary(self.site_root), "job": job.as_dict() if job else None}
        if m := re.fullmatch(r"/api/feeds/([a-z0-9-]+)", path):
            site, library, feed, _feeds = _feed(self.site_root, m.group(1))
            with History(site.history_path) as hist:
                return feed_detail(site, library, feed, hist)
        if m := re.fullmatch(r"/api/feeds/([a-z0-9-]+)/runs", path):
            site, _library, feed, _feeds = _feed(self.site_root, m.group(1))
            with History(site.history_path) as hist:
                return [_run_row(r) for r in hist.runs(feed.name, int(query.get("limit", 50)))]
        if m := re.fullmatch(r"/api/runs/(\d+)", path):
            site = load_site(self.site_root)
            with History(site.history_path) as hist:
                row = hist.get(int(m.group(1)))
            if row is None:
                raise LookupError("No such run")
            return {**_run_row(row), "report": row.get("report") or {}}
        if m := re.fullmatch(r"/api/runs/(\d+)/rows", path):
            site = load_site(self.site_root)
            with History(site.history_path) as hist:
                row = hist.get(int(m.group(1)))
            if row is None:
                raise LookupError("No such run")
            _site, _library, feed, _feeds = _feed(self.site_root, row["feed"])
            return read_rows(site, feed, row, query.get("output"), min(int(query.get("limit", 200)), 1000))
        if path == "/api/audit":
            site = load_site(self.site_root)
            with History(site.history_path) as hist:
                return hist.audit_log(int(query.get("limit", 50)))
        if m := re.fullmatch(r"/api/jobs/([0-9a-f]+)", path):
            job = self.jobs.jobs.get(m.group(1))
            if job is None:
                raise LookupError("No such job")
            return job.as_dict(int(query.get("since", 0)))
        raise LookupError("Not found")

    # -- POST
    def post(self, path: str, body: dict) -> Any:
        root = self.site_root
        if path == "/api/check":
            return self.jobs.start("check", None, lambda: check_connection(root)).as_dict()
        if m := re.fullmatch(r"/api/feeds/([a-z0-9-]+)/(preview|run|build|release|release-preview|pause|resume)",
                             path):
            name, action = m.group(1), m.group(2)
            if action in ("pause", "resume"):
                return self._pause(name, action)
            if action == "preview":
                n = max(1, min(int(body.get("instances", 25)), 500))

                def work():
                    site, _library, feed, _feeds = _feed(root, name)
                    res = runner.preview(site, feed, n)
                    return self._result(site, feed, res)
            elif action in ("run", "build"):
                def work():
                    site, _library, feed, feeds = _feed(root, name)
                    opts = runner.RunOptions(no_deliver=action == "build", trigger="manual")
                    res = runner.run_many(site, [feed], opts, all_feeds=feeds)[0]
                    return self._result(site, feed, res)
            elif action == "release-preview":
                site, _library, feed, _feeds = _feed(root, name)
                captured: dict = {}

                def look(summary: dict) -> bool:
                    captured.update(summary)
                    return False                      # look only; nothing is sent

                runner.release(site, feed, None, confirm=look)
                return captured
            else:                                      # release
                run_dir = body.get("run_dir")
                if not run_dir:
                    raise ConfigError("Say which run to release.")
                force = bool(body.get("force"))

                def work():
                    site, _library, feed, _feeds = _feed(root, name)
                    res = runner.release(site, feed, Path(run_dir), force=force)
                    return self._result(site, feed, res)
            return self.jobs.start(action, name, work).as_dict()
        raise LookupError("Not found")

    def _pause(self, name: str, action: str) -> dict:
        site = load_site(self.site_root)
        res = feedops.set_enabled(site, name, action == "resume", via="ui")
        return {"name": name, "enabled": res["enabled"]}

    @staticmethod
    def _result(site: Site, feed: Feed, res: runner.RunResult) -> dict:
        with History(site.history_path) as hist:
            row = hist.run_by_dir(res.run_dir) if res.run_dir else hist.last_run(feed.name)
        return {"status": res.status, "code": res.code, "run": _run_row(row) if row else None,
                "report": {k: v for k, v in res.report.items() if k not in ("sync",)}}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"folio-exchange/{__version__}"

        def log_message(self, fmt, *args) -> None:
            log.debug("ui %s", fmt % args)

        # -- plumbing
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}

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
            if u.path in ("/", "/index.html"):
                html = (WEB / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", app.token)
                return self._send(200, html.encode("utf-8"), CONTENT_TYPES[".html"])
            if u.path.startswith("/static/"):
                name = u.path[len("/static/"):]
                if name not in STATIC:
                    return self._error(404, "Not found")
                return self._send(200, (WEB / name).read_bytes(), CONTENT_TYPES[Path(name).suffix])
            if u.path.startswith("/api/"):
                return self._call(app.get, u.path, query)
            return self._error(404, "Not found")

        do_HEAD = do_GET

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._error(403, "Wrong address.")
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{app.port}", f"http://localhost:{app.port}"}:
                return self._error(403, "Requests from other web sites are refused.")
            if not secrets.compare_digest(self.headers.get("X-FX-Token") or "", app.token):
                return self._error(403, "Missing or wrong page token; reload the page.")
            length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._error(400, "Not valid JSON.")
            if not isinstance(body, dict):
                return self._error(400, "Expected a JSON object.")
            u = urlparse(self.path)
            return self._call(app.post, u.path, body)

    return Handler


def serve(site_root: Path, port: int = 8765, open_browser: bool = True) -> None:
    """Serve the page until Ctrl+C. Port 0 picks a free port."""
    app = App(site_root)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    except OSError as exc:
        raise FxError(f"Could not listen on 127.0.0.1:{port} ({exc.strerror or exc}). Try another port with "
                      "--port.") from None
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]
    url = f"http://127.0.0.1:{app.port}/"
    print(f"FOLIO-Exchange is open at {url}  (this computer only). Press Ctrl+C to stop.", flush=True)
    if open_browser and not os.environ.get("FOLIO_EXCHANGE_NO_BROWSER"):
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
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


def start_in_thread(site_root: Path) -> tuple[ThreadingHTTPServer, App]:
    """For tests: serve on a free port in a background thread."""
    app = App(site_root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, app
