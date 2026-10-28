"""What an interface shows about a site, and the bodies of its actions.

The local page and the desktop app both call these, so they show the same facts and act the same way. Nothing
here knows about HTTP or Qt: lookups return plain data, an unknown run is a LookupError, and a problem a person
can fix is a ConfigError or another FxError with words they can act on. Long actions (``preview``, ``run``,
``release``, ``check_delivery``, ``check_connection``) are meant for a job thread (see ``jobs.py``).
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, credentials, delivery, feedops, reads, runner, schedule, setupops, sources
from .client import probe
from .config import BUNDLED_RECIPES, Site, load_site, password_note
from .errors import ConfigError, FxError
from .fields import FIELDS
from .history import History
from .recipe import Feed, RecipeLibrary, load_all

PREVIEW_MOST = 500                   # the most instances a preview samples


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def load(site_root: Path) -> tuple[Site, RecipeLibrary, list[Feed], dict[str, str]]:
    site = load_site(site_root)
    library = RecipeLibrary(site.recipe_dirs)
    feeds, errors = load_all(site, library)
    return site, library, feeds, errors


def find_feed(site_root: Path, name: str) -> tuple[Site, RecipeLibrary, Feed, list[Feed]]:
    site, library, feeds, errors = load(site_root)
    for f in feeds:
        if f.name == name:
            return site, library, f, feeds
    if name in errors:
        raise ConfigError(errors[name])
    raise ConfigError(f"No feed called {name!r}")


def run_row(r: dict) -> dict:
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


def layout(feed: Feed) -> list[dict]:
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
                     "next": iso(schedule.next_slot(s, now, site.timezone)) if feed.enabled else None},
        "delivery": {"transport": feed.delivery.get("transport"), "where": delivery.describe_destination(feed.delivery),
                     "delivers": feed.delivers},
        "file_prefix": feed.file_prefix,
        "filters": {k: v for k, v in feed.selection.items() if v not in ([], None)},
        "options": feed.options, "checks": feed.checks,
        "legacy": {k: v for k, v in feed.legacy.items() if v},
        "layout": layout(feed),
        "last_run": run_row(last) if last else None,
        "waiting": run_row(waiting) if waiting else None,
        "newer_recipe": newer.ref if newer else None,
        "overdue": runner.overdue(site, feed, hist, now),
        "file": str(feed.path),
    }


def site_summary(site_root: Path) -> dict:
    site, library, feeds, errors = load(site_root)
    with History(site.history_path) as hist:
        items = []
        for f in feeds:
            last = last_real_run(hist, f.name)
            newer = library.newer_major(f.uses)
            items.append({"name": f.name, "title": f.title, "recipe": f.recipe.ref, "enabled": f.enabled,
                          "last_status": last["status"] if last else None,
                          "last_started": last["started"] if last else None,
                          "newer_recipe": newer.ref if newer else None})
        now = datetime.now(timezone.utc)
        warnings = feedops.site_warnings(site, library, feeds, errors, hist, now)
        task = feedops.task_status(hist, feeds, now, lock_path=site.lock_path)
    return {
        "warnings": warnings, "task": task, "setup": None, "keyring": credentials.keyring_available(),
        "version": __version__, "name": site.name, "root": str(site.root), "parent": str(site.root.resolve().parent),
        "folio": {"base_url": site.folio["base_url"], "tenant": site.folio["tenant"],
                  "username": site.folio["username"], "password": site.folio_password_source()},
        "alerts": {"on": site.alerts.get("on"), "webhook": bool(site.alerts.get("webhook")),
                   "email": bool(site.alerts.get("email"))},
        "feeds": items, "problems": errors, "recipe_problems": library.problems(),
    }


def recipes_overview(site_root: Path) -> dict:
    """Installed recipes (with where each version came from and which feeds use it) and the vendor sources."""
    site, library, feeds, _errors = load(site_root)
    got = sources.provenance(site)
    origin = {f: sid for sid, entry in got.items() for f in (entry.get("files") or {})}
    bundled = BUNDLED_RECIPES.resolve()

    def came_from(r) -> str:
        key = f"{r.id}/{r.version_str}.yaml"
        if key in origin:
            return origin[key]
        return "bundled" if r.path.resolve().is_relative_to(bundled) else "this site"

    items = []
    for rid in library.ids():
        versions = library.versions(rid)
        latest = versions[-1]
        items.append({
            "id": rid, "title": latest.title, "vendor": latest.data.get("vendor"),
            "description": latest.data.get("description"), "reads": latest.data.get("reads"),
            "default_uses": f"{rid}@^{latest.version[0]}.{latest.version[1]}",
            "default_name": (latest.data.get("file_prefix") or rid.split("-")[0]).lower().replace("_", "-"),
            "selection": {k: (latest.data.get("selection") or {}).get(k) for k in setupops.FILTER_KEYS},
            "versions": [{"version": r.version_str, "from": came_from(r)} for r in versions],
            "feeds": [f.name for f in feeds if f.recipe.id == rid],
        })
    srcs = [{"id": s.id, "vendor": s.vendor, "title": s.title, "description": s.description, "repo": s.repo,
             "branch": s.branch, "homepage": s.homepage, "origin": s.origin, "recipes": s.recipes,
             "commit": (got.get(s.id) or {}).get("commit"), "installed": (got.get(s.id) or {}).get("installed")}
            for s in sources.load_sources(site)]
    return {"recipes": items, "sources": srcs, "problems": library.problems()}


def check_connection(site_root: Path) -> dict:
    """Log in and read one page of every endpoint the enabled feeds need (structured, for the page)."""
    site, _library, feeds, errors = load(site_root)
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
    path = runner.run_path(site, row["run_dir"]) / name
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


def run_folder(site: Site, row: dict) -> Path:
    """A run's own folder, only when it is a folder inside the site's output folder."""
    if not row.get("run_dir"):
        raise ConfigError("This run has no folder.")
    out = site.out_dir.resolve()
    path = runner.run_path(site, row["run_dir"]).resolve()     # history may name the place before a move
    if path == out or not path.is_relative_to(out):
        raise ConfigError("That folder is outside the site's output folder, so the page will not open it.")
    if not path.is_dir():
        raise ConfigError("The run's folder is no longer there; older runs are tidied away.")
    return path


def open_folder(path: Path) -> None:
    """Show a folder in the system file browser. The path is passed as one argument, never through a shell."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - a checked folder, not a command
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


# ------------------------------------------------------------------ lookups
def feed_view(site_root: Path, name: str) -> dict:
    """One feed as its page shows it (see ``feed_detail``)."""
    site, library, feed, _feeds = find_feed(site_root, name)
    with History(site.history_path) as hist:
        return feed_detail(site, library, feed, hist)


def feed_runs(site_root: Path, name: str, limit: int = 50) -> list[dict]:
    """A feed's latest runs, newest first, previews included."""
    site, _library, feed, _feeds = find_feed(site_root, name)
    with History(site.history_path) as hist:
        return [run_row(r) for r in hist.runs(feed.name, limit)]


def _history_row(site: Site, run_id: int) -> dict:
    with History(site.history_path) as hist:
        row = hist.get(run_id)
    if row is None:
        raise LookupError("No such run")
    return row


def run_detail(site_root: Path, run_id: int) -> dict:
    """One run with its full report. LookupError when there is no such run."""
    row = _history_row(load_site(site_root), run_id)
    return {**run_row(row), "report": row.get("report") or {}}


def run_rows(site_root: Path, run_id: int, output: str | None = None, limit: int = 200) -> dict:
    """The first rows of a run's delimited output (see ``read_rows``). LookupError when there is no such run."""
    site = load_site(site_root)
    row = _history_row(site, run_id)
    _site, _library, feed, _feeds = find_feed(site_root, row["feed"])
    return read_rows(site, feed, row, output, limit)


def audit(site_root: Path, limit: int = 50) -> list[dict]:
    """The latest entries of the site's activity (audit) log, newest first."""
    site = load_site(site_root)
    with History(site.history_path) as hist:
        return hist.audit_log(limit)


# ------------------------------------------------------------------ actions
def run_result(site: Site, feed: Feed, res: runner.RunResult) -> dict:
    """What an interface shows after a preview, run or release: the outcome, its history row and its report."""
    with History(site.history_path) as hist:
        row = hist.run_by_dir(res.run_dir) if res.run_dir else hist.last_run(feed.name)
    return {"status": res.status, "code": res.code, "run": run_row(row) if row else None,
            "report": {k: v for k, v in res.report.items() if k not in ("sync",)}}


def preview(site_root: Path, name: str, instances: int = 25) -> dict:
    """Build sample files from live FOLIO data; nothing is sent. ``instances`` is kept within 1..500."""
    n = max(1, min(instances, PREVIEW_MOST))
    site, _library, feed, _feeds = find_feed(site_root, name)
    return run_result(site, feed, runner.preview(site, feed, n))


def run(site_root: Path, name: str, *, deliver: bool) -> dict:
    """Run a feed now (a manual run); with ``deliver=False`` it builds the files and sends nothing."""
    site, _library, feed, feeds = find_feed(site_root, name)
    opts = runner.RunOptions(no_deliver=not deliver, trigger="manual")
    res = runner.run_many(site, [feed], opts, all_feeds=feeds)[0]
    return run_result(site, feed, res)


def release_summary(site_root: Path, name: str) -> dict:
    """What ``release`` would send, for a person to confirm. Looks only; nothing is sent."""
    site, _library, feed, _feeds = find_feed(site_root, name)
    captured: dict = {}

    def look(summary: dict) -> bool:
        captured.update(summary)
        return False                      # look only; nothing is sent

    runner.release(site, feed, None, confirm=look)
    return captured


def release(site_root: Path, name: str, run_dir: Path | str) -> dict:
    """Send a held, blocked or built run after a person has looked at it, with every safety check."""
    site, _library, feed, _feeds = find_feed(site_root, name)
    res = runner.release(site, feed, Path(run_dir))
    return run_result(site, feed, res)


def check_delivery(site_root: Path, name: str) -> dict:
    """Log in to the feed's destination and list it; any failure becomes an FxError in plain words."""
    _site, _library, feed, _feeds = find_feed(site_root, name)
    try:
        listing = delivery.check_delivery(feed.delivery)
    except Exception as exc:  # noqa: BLE001 - every failure is put in plain words
        raise FxError(delivery.plain_error(exc, feed.delivery)) from None
    return {"ok": True, "where": delivery.describe_destination(feed.delivery),
            "entries": None if listing is None else len(listing)}


def set_enabled(site_root: Path, name: str, enabled: bool, *, via: str) -> dict:
    """Pause or resume a feed (see ``feedops.set_enabled``), audited as done ``via`` this interface."""
    res = feedops.set_enabled(load_site(site_root), name, enabled, via=via)
    return {"name": name, "enabled": res["enabled"]}


def open_run_folder(site_root: Path, run_id: int) -> dict:
    """Show a run's folder in the file browser, only when it is inside the site's output folder."""
    site = load_site(site_root)
    folder = run_folder(site, _history_row(site, run_id))
    open_folder(folder)
    return {"opened": folder.name}
