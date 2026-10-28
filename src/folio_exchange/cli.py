"""The `folio-exchange` command line.

Every command prints plain language for library staff; `run`, `sync`, `build` and
`release` also print a short JSON summary, as folio-chq-extract did, for scripts and
CI logs. Secrets are never printed: screens say where a secret comes from.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import platform
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, credentials, delivery, feedops, notify, reads, runner, schedule
from .client import probe
from .config import BUNDLED_RECIPES, SITE_FILE, Site, find_site, load_site, password_note, read_yaml, rewrite_key
from .errors import ConfigError, FxError, LockBusy, RecipeError
from .fields import FIELDS
from .history import History
from .recipe import (
    Feed,
    RecipeLibrary,
    load_all,
    load_feed_data,
    load_recipe_file,
    parse_uses,
    settings_changes,
)

log = logging.getLogger("folio_exchange")


# ------------------------------------------------------------------ setup
def _setup_logging(logs_dir: Path | None, verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for old in [h for h in root.handlers if getattr(h, "_folio_exchange", False)]:
        root.removeHandler(old)
        old.close()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logs_dir / "folio-exchange.log", encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        h._folio_exchange = True
        root.addHandler(h)
    for noisy in ("urllib3", "paramiko"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _site(args) -> Site:
    return load_site(find_site(args.site))


def _new_site_root(args) -> Path | None:
    """The folder named with --site or FOLIO_EXCHANGE_SITE when it has no site.yaml yet (a site still to be set
    up), else None. The current folder never counts, so a mistyped command cannot start a site anywhere."""
    import os

    named = args.site or os.environ.get("FOLIO_EXCHANGE_SITE")
    if not named:
        return None
    root = Path(named).expanduser()
    if root.name == SITE_FILE:
        root = root.parent
    if (root / SITE_FILE).is_file() or (root / "feeds").is_dir() or (root / "data" / "history.sqlite").is_file():
        return None              # a site (or one whose site.yaml has gone missing: that stays an error)
    return root.resolve()


def _make_site_folder(root: Path) -> None:
    """Create a new site's folder, but only inside a folder that exists, so a mistyped path is noticed."""
    try:
        root.mkdir(exist_ok=True)
    except FileNotFoundError:
        raise ConfigError(f"{root.parent} does not exist, so {root} was not created. Check the path.") from None


def _library(site: Site | None) -> RecipeLibrary:
    return RecipeLibrary(site.recipe_dirs if site else [BUNDLED_RECIPES])


def _feeds(site: Site, library: RecipeLibrary | None = None) -> tuple[list[Feed], dict[str, str]]:
    return load_all(site, library or _library(site))


def _pick(site: Site, names: list[str]) -> list[Feed]:
    feeds, errors = _feeds(site)
    by_name = {f.name: f for f in feeds}
    out = []
    for n in names:
        if n in errors:
            raise ConfigError(errors[n])
        if n not in by_name:
            known = ", ".join(sorted(set(by_name) | set(errors))) or "none yet; run `folio-exchange init`"
            raise ConfigError(f"No feed called {n!r} (feeds: {known})")
        out.append(by_name[n])
    return out


def _one(site: Site, name: str) -> Feed:
    return _pick(site, [name])[0]


def _summary(res: runner.RunResult) -> dict:
    out = {"feed": res.feed, "status": res.status, "run_dir": str(res.run_dir) if res.run_dir else None}
    for key in ("error", "held", "next"):
        if res.report.get(key):
            out[key] = res.report[key]
    return out


def _yes(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _not_sent(row: dict) -> str:
    """A marker for a successful run that sent nothing because its feed had no delivery (transport none)."""
    return " (not sent: transport none)" if row.get("status") == "success" and row.get("destination") == "none" else ""


def _when(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


# ------------------------------------------------------------------ check
def _short(text: str, width: int = 200) -> str:
    import textwrap

    return textwrap.shorten(" ".join(str(text).split()), width=width, placeholder=" ...")


def cmd_check(args) -> int:
    if args.delivery:
        return cmd_check_delivery(args)
    site = _site(args)
    if args.alerts:
        sent = runner.alert(site, None, "test", "This is a test alert from `folio-exchange check --alerts`.",
                            force=True)
        if not site.alerts.get("webhook") and not site.alerts.get("email"):
            print("No alert channel is set up (alerts.webhook or alerts.email in site.yaml).")
            return 1
        print(f"Test alert sent by: {', '.join(sent) if sent else 'nothing (see the log for why)'}")
        return 0 if sent else 1
    library = _library(site)
    feeds, errors = _feeds(site, library)
    chosen = [f for f in feeds if not args.feed or f.name == args.feed]
    if args.feed and not chosen and args.feed not in errors:
        raise ConfigError(f"No feed called {args.feed!r}")
    ok = True
    with History(site.history_path) as hist:
        changed = runner.audit_settings(site, hist)
        if changed:
            print(f"Settings changed since the last check (recorded in the audit log): {', '.join(changed)}")
        print(f"Site: {site.root}")
        now = datetime.now(timezone.utc)
        task = feedops.task_status(hist, chosen, now, lock_path=site.lock_path)
        if task["state"] in ("ok", "running"):
            print(task["text"])                 # stale and never-started come as warnings below
        for f in chosen:
            line = f"Feed {f.name}: {f.recipe.ref} ({f.uses}), {schedule.describe(schedule.parse(f.schedule))}"
            print(line + ("" if f.enabled else " [paused]"))
        warnings = feedops.site_warnings(site, library, chosen, errors, hist, now, only=args.feed)
    file_problems = any(w["code"] == "feed-file" for w in warnings)
    for w in warnings:
        label = {"problem": "PROBLEM ", "warning": "WARNING ", "note": "NOTE    "}[w["level"]]
        who = f"{w['feed']}: " if w["feed"] else ""
        print(f"{label} {who}" + w["text"].replace("\n", "\n         "))

    source = site.folio_password_source()
    print(f"FOLIO password: {source}")
    note = password_note(site)
    if note:
        print(f"NOTE     {note}")
    client = runner.client_for(site)
    client.connect()
    print(f"Logged in to {client.base_url} (tenant {client.tenant}) as {client.username}")
    needed = set().union(*(set(f.reads) for f in chosen)) if chosen else set(reads.READS)
    results = probe(client, needed)
    for r in results:
        label = f"{r['name']} ({r['path']})"
        if r["status"] == "ok":
            print(f"  OK       {label}" + (f": {r['count']:,} records" if r["count"] is not None else ""))
        elif r["status"] == "denied":
            print(f"  DENIED   {label}: the account lacks {r['permission']}")
        else:
            print(f"  FAILED   {label}: {_short(r['error'] or '', 160)}")
    ok = all(r["status"] == "ok" for r in results)
    denied = [r["permission"] for r in results if r["status"] == "denied"]
    offset = client.clock_offset
    if offset is not None and abs(offset.total_seconds()) > 300:
        print(f"WARNING  This computer's clock is {abs(int(offset.total_seconds() // 60))} minutes away from "
              "FOLIO's. Syncing uses FOLIO's time, but schedules use this computer's: set the clock right.")
    if denied:
        print("\nAsk your FOLIO administrator to add these permissions to the account (on Eureka, the matching "
              "capabilities in its role):")
        for perm in sorted(set(denied)):
            print(f"  {perm}")
    elif not ok:
        print("\nSome FOLIO requests failed (see FAILED above). Try again; if it repeats, check the address and "
              "the network.")
    if file_problems:
        print("\nFix the feed file problems shown above, then run `folio-exchange check` again.")
    return 0 if ok and not file_problems else 1


def cmd_check_delivery(args) -> int:
    site = _site(args)
    feeds, errors = _feeds(site)
    chosen = [f for f in feeds if not args.feed or f.name == args.feed]
    if args.feed and args.feed in errors:
        raise ConfigError(errors[args.feed])
    if not chosen:
        print("No feed to test." if not args.feed else f"No feed called {args.feed!r}")
        return 1
    worst = 0
    for f in chosen:
        d = f.delivery
        print(f"Feed {f.name}: {delivery.describe_destination(d)}" + ("" if f.enabled else " [paused]"))
        if d["transport"] == "sftp":
            pin = d.get("host_key_fingerprint") or f"known_hosts ({d.get('known_hosts')})"
            if (d.get("private_key") or "").strip():
                secret = "private key file " + d["private_key"]
            else:
                secret = "password: " + credentials.source(
                    d.get("secret"), credentials.SFTP_SERVICE, credentials.sftp_account(d["username"], d["host"]))
            if d.get("host_key_confirmed") is False:
                print(f"  Key not yet confirmed with the vendor; stored {d.get('host_key_fingerprint')}. "
                      "Nothing logs in until it is.")
                try:
                    read = delivery.host_key_fingerprints(d["host"], int(d.get("port") or 22))
                    offered = read.get("keys") or [read]
                    same = [k for k in offered if k["sha256"] == d.get("host_key_fingerprint")]
                    same.sort(key=lambda k: k["type"] != d.get("host_key_type"))     # the stored type first
                    if same:
                        print(f"  The server offers it as its {same[0]['type']} key (the same).")
                    else:
                        print("  The server offers none like it (DIFFERENT: ask the vendor). It offers:")
                        for k in offered:
                            print(f"    {k['type']}: {k['sha256']}")
                except Exception as exc:  # noqa: BLE001 - network errors of every kind
                    print(f"  Could not read the server's key: {type(exc).__name__}: {str(exc)[:160]}")
                print("  Compare it with the vendor's fingerprint, then run `folio-exchange init` again to confirm it.")
                worst = 1
                continue
            print(f"  Connecting to {d['host']}:{d.get('port') or 22} as {d['username']}")
            print(f"  host key {pin}; {secret}")
        elif d["transport"] == "none":
            print("  Nothing to test: this feed has no delivery set up (a dry run). When the vendor's details "
                  "are ready, run `folio-exchange init` again and choose how the files are delivered.")
            if not f.enabled:
                print(f"  Paused: `folio-exchange feeds resume {f.name}` lets it run on its schedule again.")
            continue
        try:
            names = delivery.check_delivery(d)
        except Exception as exc:  # noqa: BLE001 - paramiko raises many types; report them all the same way
            print(f"  FAILED   {type(exc).__name__}: {str(exc)[:200]}")
            if not f.enabled:
                print("  Paused: leave it paused until this test passes.")
            worst = 1
            continue
        folder = d.get("directory") if d["transport"] == "local" else (d.get("remote_dir") or ".")
        if names is None:
            print(f"  OK       logged in; listing {folder} is not permitted (normal for upload-only accounts). "
                  "Nothing was uploaded.")
        else:
            print(f"  OK       {'logged in; ' if d['transport'] == 'sftp' else ''}{folder} has {len(names)} "
                  "entries. Nothing was uploaded.")
            for name in names[:20]:
                print(f"           {name}")
        if not f.enabled:
            print(f"  Paused: the test passed. Resume it with `folio-exchange feeds resume {f.name}`. A first "
                  "delivery to a new destination is still held for you to check.")
    return worst


# ------------------------------------------------------------------ runs
def _recipe_sources(args, site: Site | None, library: RecipeLibrary) -> int:
    from . import sources

    if args.action == "sources":
        installed = sources.provenance(site) if site else {}
        for s in sources.load_sources(site):
            got = installed.get(s.id) or {}
            print(f"{s.id:<16} {s.title}  ({s.repo}, {s.branch}; recipes: {', '.join(s.recipes)})")
            print(f"{'':<16} " + (f"downloaded at commit {got['commit'][:12]} on {got.get('installed', '?')[:10]}"
                                  if got.get("commit") else "not downloaded yet"))
        return 0
    if site is None:
        raise ConfigError("Downloading recipes needs a site: give --site, or run it in the site folder.")
    if not args.target:
        raise ConfigError("Name the source to download from (see `folio-exchange recipes sources`).")
    result = sources.check(site, library, args.target)
    src = result["source"]
    print(f"{src['title']}: {src['repo']} ({src['branch']}) at commit {result['commit'][:12]}")
    for p in result["problems"]:
        print(f"  NOTE     {p}")
    for r in result["recipes"]:
        label = {"new": "NEW", "installed": "have", "conflict": "REFUSED", "invalid": "REFUSED"}[r["status"]]
        print(f"  {label:<8} {r['id']} {r['version']}" + (f": {r.get('title')}" if r.get("title") else ""))
        if r["status"] == "new":
            if r.get("changes"):
                print(f"           {_short(str(r['changes']), 300)}")
            print(f"           reads: {', '.join(r.get('reads') or [])}; patron data: {r.get('patron_data')}")
            if r.get("picked_up_by"):
                print(f"           feeds that use it from their next run: {', '.join(r['picked_up_by'])}")
        if r.get("problem"):
            print(f"           {_short(r['problem'], 400)}")
    if not result["new"]:
        print("Nothing new to install.")
        return 0
    if not (args.yes or _yes(f"Install {len(result['new'])} new version(s) into {site.recipe_dirs[0]}?")):
        print("Nothing installed.")
        return 2
    done = sources.install(site, library, args.target, result["commit"], via="cli")
    print(f"Installed: {', '.join(done['installed'])}. Recorded in the audit log.")
    return 0


def cmd_run(args) -> int:
    new = _new_site_root(args) if args.due else None
    if new is not None and new.is_dir():
        print(f"{new} is not set up yet (no {SITE_FILE}), so nothing is due. Open the page to set it up.")
        return 0
    site = _site(args)
    if not args.due:
        return _run(args, site)
    started = datetime.now(timezone.utc)
    previous: dict = {}
    try:                                   # beside the lock too, so a pass that cannot write history still shows
        previous = feedops.mark_pass_start(site.lock_path, started)
    except OSError as exc:
        log.warning("Could not note the start of this scheduled pass beside the run lock: %s", exc)
    try:
        with History(site.history_path) as hist:
            feedops.record_due_start(hist, started)
    except sqlite3.Error as exc:
        log.warning("Could not record the start of this scheduled pass: %s", exc)
    # The heartbeat goes at the start of the pass as well as the end, so a monitor hears from a long pass (a
    # first full sync can take hours) as soon as it starts; its period must still allow for the longest pass.
    notify.heartbeat(site.alerts, site.name)
    error, alerted = None, False
    try:
        return _run(args, site)
    except Exception as exc:  # noqa: BLE001 - alert first (without the history: it may be what failed), then stop
        error = f"{type(exc).__name__}: {exc}"
        alerted = _pass_failed(site, exc, error, previous)
        raise
    finally:
        try:
            feedops.mark_pass_end(site.lock_path, started, error, alerted)
        except OSError as exc:
            log.warning("Could not note how this scheduled pass ended: %s", exc)
        notify.heartbeat(site.alerts, site.name)


def _pass_failed(site: Site, exc: Exception, error: str, previous: dict) -> bool:
    """Alert that a scheduled pass failed, without needing the run history. The same failure as the last pass's,
    already alerted, is not sent again every hour. Returns whether the alert went (now or before)."""
    if not isinstance(exc, (FxError, OSError, sqlite3.Error)):
        log.exception("The scheduled pass failed: %s", error)        # main shows the others plainly
    if previous.get("error") == error and previous.get("alerted"):
        return True
    text = (f"The scheduled pass failed: {error}\nFeeds due in this pass may not have run or been recorded. If the "
            "disk is full, free some space; logs/folio-exchange.log has the details, and `folio-exchange check` "
            "shows the site's state.")
    return bool(runner.alert(site, None, "failed", text))


LOCK_STUCK = "lock_stuck_since"            # history meta: the lock holder's start already alerted about


def _lock_busy(site: Site, exc: LockBusy, feeds: list[Feed]) -> int:
    """A scheduled pass found the site's lock taken: skip this hour. When one run (a manual run or a release
    that stalled, a stuck process) has held it for hours while some feed is due, alert once for that run. A
    scheduled pass that stalls never meets this (Task Scheduler starts no second one): `check` and the page say
    it is stuck, the heartbeat stops, and once the task's 12-hour limit ends it the next pass reports it."""
    log.info("%s Skipping this hour; the next start will catch up.", exc)
    since = feedops.run_started(site.lock_path)
    now = datetime.now(timezone.utc)
    if since is None or now - since < feedops.LOCK_STUCK_AFTER:
        return 0
    stamp = since.isoformat(timespec="seconds")
    with History(site.history_path) as hist:
        if hist.get_meta(LOCK_STUCK) == stamp or not runner.due_feeds(site, feeds, now, hist):
            return 0                         # alerted already, or nothing would have run this hour anyway
        hist.set_meta(LOCK_STUCK, stamp)
    text = "Scheduled runs are being skipped. " + feedops.stuck_text(since, now)
    log.warning("%s", text)
    runner.alert(site, None, "failed", text)
    return 0


def _run(args, site: Site) -> int:
    feeds, errors = _feeds(site)
    opts = runner.RunOptions(full=args.full, no_deliver=args.no_deliver, skip_sync=args.skip_sync,
                             force=args.force, trigger="schedule" if args.due else "manual")
    bad: dict[str, str] = {}
    if args.due:
        bad = runner.active_problems(site, errors)
        try:
            with runner.RunLock(site.lock_path), History(site.history_path) as hist:
                runner.start_pass(site, hist)        # a pass killed on a slot's last attempt is reported now
                runner.audit_settings(site, hist)
                now = datetime.now(timezone.utc)
                runner.report_given_up(site, feeds, hist, now)
                chosen = runner.due_feeds(site, feeds, now, hist)
                for f in feeds:
                    late = runner.overdue(site, f, hist, now)
                    if late and f not in chosen:
                        log.warning("Feed %s is %s", f.name, late)
        except LockBusy as exc:
            return _lock_busy(site, exc, feeds)
        if not chosen and not bad:
            log.info("No feed is due.")
            return 0
    elif args.all:
        chosen = [f for f in feeds if f.enabled]
        bad = runner.active_problems(site, errors)
    elif args.feeds:
        chosen = _pick(site, args.feeds)
    else:
        raise ConfigError("Name the feeds to run, or use --all or --due (`folio-exchange feeds` lists them).")
    try:
        results = runner.run_many(site, chosen, opts, all_feeds=feeds, bad_feeds=bad)
    except LockBusy as exc:
        if args.due:
            return _lock_busy(site, exc, feeds)
        raise
    if args.due:
        with History(site.history_path) as hist:
            runner.report_given_up(site, feeds, hist, datetime.now(timezone.utc))
    print(json.dumps([_summary(r) for r in results] if len(results) != 1 else _summary(results[0]), indent=2))
    return runner.worst_code(results)


def cmd_build(args) -> int:
    site = _site(args)
    feed = _one(site, args.feed)
    results = runner.run_many(site, [feed], runner.RunOptions(skip_sync=True, no_deliver=True, force=args.force))
    res = results[0]
    print(json.dumps({**_summary(res), **(res.report.get("build") or {})}, indent=2, default=str))
    return res.code


def cmd_sync(args) -> int:
    from .recipe import load_all as _load
    from .sync import reads_to_sync

    site = _site(args)
    feeds, _errors = _load(site)
    wanted = reads_to_sync(feeds)
    if not wanted:
        raise ConfigError("No enabled feed, so there is nothing to sync. Set one up with `folio-exchange init`.")
    with runner.RunLock(site.lock_path):
        rep = runner.sync(site, wanted, args.full)
    print(json.dumps(rep, indent=2, default=str))
    return 0


def _show_release(summary: dict) -> None:
    print(f"Feed {summary['feed']}: {summary['status']} run built {_when(summary['built'])}, {summary['recipe']}")
    if summary.get("reason"):
        print(f"  Why it waited: {summary['reason']}")
    last = summary.get("last_delivered_items")
    print(f"  Bibs {summary.get('bibs') or 0:,}, items {summary.get('items') or 0:,}"
          + (f" (last delivery {_when(summary.get('last_delivered'))}: {last:,} items)" if last else
             " (nothing delivered there before)"))
    for name, size in summary["files"]:
        print(f"  {name}  {size:,} bytes")
    print(f"  Destination: {summary['destination']}")
    if summary.get("newer_runs"):
        print(f"  WARNING  a newer run was delivered since: {summary['newer_runs'][0]}")


def cmd_release(args) -> int:
    site = _site(args)
    feed = _one(site, args.feed)

    def confirm(summary: dict) -> bool:
        _show_release(summary)
        return args.yes or _yes("Send these files now?")

    res = runner.release(site, feed, Path(args.run_dir) if args.run_dir else None, force=args.force,
                         confirm=confirm)
    if res.report.get("cancelled"):
        print("Nothing was sent.")
        return 2
    sent = res.report.get("delivered") or []
    print(f"Sent {len(sent)} file(s) to {delivery.describe_destination(feed.delivery)}. Later runs to this "
          f"destination deliver without waiting. To run on a schedule: folio-exchange schedule")
    print(json.dumps({"run_dir": str(res.run_dir), "delivered": sent}, indent=2))
    return 0


def cmd_preview(args) -> int:
    site = _site(args)
    feed = _one(site, args.feed)
    res = runner.preview(site, feed, args.n)
    if res.status == "failed":
        print(f"Preview failed: {res.report.get('error')}")
        return 1
    b = res.report.get("build") or {}
    print(f"Preview of {feed.name} ({feed.recipe.ref}): {b.get('bibs_written', 0)} bibs, "
          f"{b.get('items_written', 0)} item rows, from a sample of live data.")
    skipped = b.get("items_skipped") or {}
    if skipped:
        print("  Items left out: " + ", ".join(f"{k} {v}" for k, v in skipped.items()))
    if not b.get("items_written"):
        fetched = (res.report.get("sync") or {}).get("fetched") or {}
        why = ("every sampled item was left out by the feed's filters (above)" if skipped else
               "the sampled records have no items" if fetched.get("instances") else "FOLIO returned no records")
        print(f"  No item rows came out: {why}. Try a larger sample with -n, or check the filters in "
              f"feeds/{feed.name}.yaml.")
    for o in feed.outputs:
        if o["format"] != "delimited":
            continue
        names = (b.get("outputs") or {}).get(o["name"]) or []
        if not names:
            continue
        import csv

        path = res.run_dir / names[0]
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh, delimiter=o["delimiter"]))
        header, body = (rows[0], rows[1:]) if o.get("header", True) and rows else ([c["header"] for c in
                                                                                  o["columns"]], rows)
        cols = [h for h in header if not args.columns or h in args.columns]
        width = max((len(h) for h in cols), default=0)
        for n, row in enumerate(body[: args.rows], 1):
            print(f"\n{o['name']} row {n}")
            for h, v in zip(header, row, strict=False):
                if h in cols:
                    print(f"  {h:<{width}}  {v}")
    for w in res.report.get("warnings") or []:
        print(f"\nWARNING  {w}")
    for p in res.report.get("conformance") or []:
        print(f"SAMPLE   {p}")
    print(f"\nFiles: {res.run_dir}\nThese are previews (PREVIEW_ in the name) and are never delivered. "
          "Don't edit delivered files in Excel; change the feed settings instead.")
    return 0


# ------------------------------------------------------------------ feeds
def cmd_feeds(args) -> int:
    site = _site(args)
    if args.action in ("pause", "resume"):
        if not args.feed:
            raise ConfigError(f"Name the feed to {args.action}.")
        res = feedops.set_enabled(site, args.feed, args.action == "resume")
        if not res["changed"]:
            print(f"Feed {args.feed} is already {'running' if res['enabled'] else 'paused'}.")
        else:
            print(f"Feed {args.feed} {args.action}d.")
        return 0
    library = _library(site)
    feeds, errors = _feeds(site, library)
    if not feeds and not errors:
        print("No feeds yet. Run `folio-exchange init` to set one up.")
        return 0
    now = datetime.now(timezone.utc)
    with History(site.history_path) as hist:
        task = feedops.task_status(hist, feeds, now, lock_path=site.lock_path)
        if task["state"] != "unneeded":
            warn = "WARNING  "
            label = {"ok": "", "running": "", "stuck": warn, "stale": warn, "never": "NOTE     "}[task["state"]]
            print(f"{label}{task['text']}\n")
        for f in feeds:
            s = schedule.parse(f.schedule)
            nxt = schedule.next_slot(s, now, site.timezone)
            last = hist.last_run(f.name)
            print(f"{f.name}{'' if f.enabled else '  [paused]'}  {f.title}")
            print(f"  recipe    {f.recipe.ref} (uses {f.uses})")
            print(f"  schedule  {schedule.describe(s)}"
                  + (f"; next {nxt.astimezone(site.timezone):%a %Y-%m-%d %H:%M}" if nxt and f.enabled else ""))
            print(f"  delivers  {delivery.describe_destination(f.delivery)}")
            if last:
                print(f"  last run  {_when(last['started'])}: {last['status']}{_not_sent(last)}"
                      + (f" ({Path(last['run_dir']).name})" if last.get("run_dir") else ""))
                if last.get("error"):
                    print(f"            {_short(last['error'], 300)}")
                if last["status"] in ("held", "blocked", "built", "failed") or not f.delivers:
                    step = runner.next_step(f, last["status"], Path(last["run_dir"]) if last.get("run_dir") else None)
                    if step:
                        print(f"  next      {step}")
            else:
                print("  last run  never")
                print(f"  next      folio-exchange run {f.name}"
                      + ("   (the first delivery is held for you to check)" if f.delivers else
                         " --no-deliver   (a dry run: the files stay in the run folder)"))
            if not f.enabled:
                test = f"test the delivery with `folio-exchange check --delivery {f.name}`, then " if f.delivers else ""
                print(f"  paused    scheduled runs skip it; {test}resume it with "
                      f"`folio-exchange feeds resume {f.name}`.")
            newer = library.newer_major(f.uses)
            if newer:
                print(f"  update    {newer.ref} is available (held for review; see `folio-exchange upgrade`)")
            late = runner.overdue(site, f, hist, now)
            if late:
                print(f"  WARNING   {late}")
            from . import sources

            local = sources.local_minor_warning(site, library, f)
            if local:
                print(f"  WARNING   {local}")
    for name, err in errors.items():
        print(f"{name}  PROBLEM\n  " + err.replace("\n", "\n  "))
    return 1 if errors else 0


def _layout(title: str, data: dict, outputs: list[dict], link: dict | None, extra: list[str]) -> list[str]:
    lines = [title, "=" * len(title)]
    lines += [x for x in extra if x]
    if link:
        lines.append(f"Items link to bibs: column {link['item_column']!r} = MARC {link['bib_field']} "
                     f"({link['value']}).")
    for o in outputs:
        lines.append("")
        if o["format"] == "marc21":
            lines.append(f"Output {o['name']}: MARC 21 (ISO 2709, UTF-8), file {o.get('file_name')}.mrc"
                         + (f", split every {o['split_every']:,} records" if o.get("split_every") else ""))
            lines.append("  One record per instance; 001 = instance HRID; 999 ff $i = instance UUID.")
            itf = o.get("item_field") or {}
            if o.get("embed_items"):
                subs = ", ".join(f"${k} {v}" for k, v in (itf.get("subfields") or {}).items())
                lines.append(f"  Items embedded in {itf.get('tag', '949')}: {subs} "
                             f"(at most {itf.get('max_per_record', 500)} per record).")
            elif itf.get("strip_existing", True):
                lines.append(f"  No item fields; any existing {itf.get('tag', '949')} is removed.")
        else:
            delim = {"\t": "tab", "|": "pipe (|)", ",": "comma"}.get(o.get("delimiter", ","), repr(o.get("delimiter")))
            lines.append(f"Output {o['name']}: delimited text, UTF-8, {delim}-separated, file "
                         f"{o.get('file_name')}.{o.get('extension', 'csv')}")
            lines.append(f"  Header row: {'yes' if o.get('header', True) else 'no'}; empty values: "
                         f"{o.get('empty_value', '')!r}; dates: {o.get('date_format', '%Y-%m-%d')}")
            width = max(len(str(c.get("header"))) for c in o.get("columns") or [{"header": ""}])
            for n, col in enumerate(o.get("columns") or [], 1):
                fd = FIELDS.get(col.get("field"))
                lines.append(f"  {n:>2}. {col.get('header'):<{width}}  {fd.description if fd else col.get('field')}"
                             + ("  (required)" if col.get("required") else ""))
    return lines


def cmd_recipes(args) -> int:
    site = None
    try:
        site = load_site(find_site(args.site))
    except ConfigError:
        pass
    library = _library(site)
    action = args.action or "list"
    if action == "list":
        for rid in library.ids():
            versions = library.versions(rid)
            latest = versions[-1]
            print(f"{rid:<24} {latest.title}  (versions: {', '.join(r.version_str for r in versions)})")
        for p in library.problems():
            print("PROBLEM " + p)
        for w in library.shadow_warnings():
            print("WARNING " + w)
        return 1 if library.problems() else 0
    if action in ("sources", "get"):
        return _recipe_sources(args, site, library)
    if action == "validate":
        if not args.target:
            raise ConfigError("Name the recipe file to validate.")
        r = load_recipe_file(Path(args.target))
        print(f"OK  {r.ref}: {r.title}")
        return 0
    if not args.target:
        raise ConfigError(f"Name a recipe (e.g. chq-academic) or a feed for `recipes {action}`.")
    if site is not None and (site.feeds_dir / f"{args.target}.yaml").is_file():
        feed = _one(site, args.target)
        data, outputs, link = feed.recipe.data, feed.outputs, feed.link
        title = f"{feed.title}: file layout for the vendor"
        extra = [f"Recipe {feed.recipe.ref}, with this library's local settings.",
                 f"Schedule: {schedule.describe(schedule.parse(feed.schedule))}."]
    else:
        target = args.target if "@" in args.target else f"{args.target}@>=0"
        rid = target.split("@", 1)[0]
        recipe = library.latest(rid) if target.endswith("@>=0") else library.resolve(target)
        if recipe is None:
            raise ConfigError(f"No recipe called {rid!r}")
        data, outputs, link = recipe.data, recipe.data.get("outputs") or [], recipe.data.get("link")
        title = f"{recipe.title}: file layout"
        extra = [f"Recipe {recipe.ref} for {data.get('vendor')}." + (f" Source: {data['spec_source']}."
                                                                      if data.get("spec_source") else "")]
    if action == "show":
        print(json.dumps(data, indent=2, default=str))
        return 0
    print("\n".join(_layout(title, data, outputs, link, extra)))
    return 0


def cmd_upgrade(args) -> int:
    site = _site(args)
    library = _library(site)
    feed = _one(site, args.feed)
    if args.to:
        target = args.to if "@" in args.to else f"{feed.recipe.id}@{args.to}"
    else:
        newer = library.newer_major(feed.uses) or library.latest(feed.recipe.id)
        if newer is None or newer.version <= feed.recipe.version:
            print(f"No newer version of {feed.recipe.id} is available; {feed.name} uses {feed.recipe.ref}.")
            return 0
        target = f"{newer.id}@^{newer.version[0]}.{newer.version[1]}"
    try:
        rid, _spec = parse_uses(target)
        library.resolve(target)
    except RecipeError as exc:
        raise ConfigError(f"--to {args.to or target}: {str(exc).removeprefix('uses: ')}") from None
    if rid != feed.recipe.id:
        raise ConfigError(f"{target} is a different recipe; set up a new feed for it with `folio-exchange init`.")
    raw = read_yaml(feed.path)
    raw["uses"] = target
    new = load_feed_data(raw, feed.path, library, site.root)
    print(f"Feed {feed.name}: {feed.recipe.ref} -> {new.recipe.ref}")
    if new.recipe.version == feed.recipe.version:
        print("Already on that version.")
        return 0
    for entry in new.recipe.data.get("changelog") or []:
        print(f"  {entry.get('version')} ({entry.get('date')}): {entry.get('changes')}")
    for o in new.outputs:
        try:
            old = feed.output(o["name"])
        except KeyError:
            print(f"  New output {o['name']} ({o['format']})")
            continue
        if o["format"] == "delimited":
            a, b = [c["header"] for c in old["columns"]], [c["header"] for c in o["columns"]]
            if a != b:
                print(f"  {o['name']}: columns change from\n    {a}\n  to\n    {b}")
    changes = settings_changes(feed, new)
    if changes:
        print(f"  What else changes for {feed.name}:")
        for text in changes:
            print(f"    - {text}")
    if new.recipe.version[0] != feed.recipe.version[0] and feed.delivers:
        print("  This is a new major version: the next delivery is held until you release it.")
    if not (args.yes or _yes(f"Change {feed.path.name} to uses: {target}?")):
        print("Nothing changed.")
        return 2
    rewrite_key(feed.path, "uses", target)
    with History(site.history_path) as hist:
        hist.audit(None, feed.name, "recipe upgraded", {"from": feed.recipe.ref, "to": new.recipe.ref,
                                                         "uses": target, "changes": changes})
        hist.set_meta(f"{runner.UPGRADE_META}{feed.name}",
                      json.dumps({"to": new.recipe.version_str, "changes": changes}))
        runner.audit_settings(site, hist)
    print(f"Done. Run `folio-exchange preview {feed.name}` to look at the new layout.")
    return 0


def cmd_fields(_args) -> int:
    width = max(map(len, FIELDS))
    for name, f in FIELDS.items():
        needs = "" if f.needs <= {"instances", "holdings", "items"} else "  [needs loans]"
        print(f"{name:<{width}}  {f.description}{needs}")
    return 0


def cmd_history(args) -> int:
    site = _site(args)
    with History(site.history_path) as hist:
        if args.audit:
            for a in reversed(hist.audit_log(args.n, args.feed)):
                print(f"{_when(a['ts'])}  {a.get('actor') or '?':<12} {a.get('feed') or '-':<12} {a['action']}  "
                      f"{json.dumps(a.get('detail') or {}, default=str)[:200]}")
            return 0
        if args.show:
            row = hist.get(int(args.show)) if str(args.show).isdigit() else hist.run_by_dir(Path(args.show))
            if row is None:
                raise ConfigError(f"No run {args.show!r} in the history.")
            print(f"Run {row['id']} of {row['feed']} ({row.get('recipe')}@{row.get('recipe_version')}), "
                  f"{row.get('trigger')}, started {_when(row['started'])}, finished {_when(row.get('finished'))}")
            print(f"  status     {row['status']}{_not_sent(row)}")
            if row.get("error"):
                print(f"  problem    {row['error']}")
            rep = row.get("report") or {}
            if rep.get("released_by"):
                print(f"  released   by {rep['released_by']} on {_when(rep.get('released'))}"
                      + (f", despite: {rep['released_after']}" if rep.get("released_after") else ""))
            print(f"  bibs/items {row.get('bibs')}/{row.get('items')}")
            for f in row.get("files") or []:
                print(f"  file       {f['name']}  {f['bytes']:,} bytes  sha256 {f['sha256'][:16]}…")
            for d in row.get("destinations") or []:
                print(f"  sent to    {d}")
            nxt = (row.get("report") or {}).get("next")
            if nxt:
                print(f"  next       {nxt}")
            return 0
        rows = hist.runs(args.feed, args.n)
        if not rows:
            print("No runs yet.")
        for r in reversed(rows):
            folder = Path(r["run_dir"]).name if r.get("run_dir") else "-"
            print(f"{r['id']:>5}  {_when(r['started'])}  {r['feed']:<14} {r['status']:<8} "
                  f"items {r.get('items') if r.get('items') is not None else '-':>8}  {folder:<22} "
                  f"{_short(r.get('error') or '', 80) if r.get('error') else _not_sent(r).strip()}")
        print("(Times are this computer's; run folder names are in UTC.)")
    return 0


ALERT_TARGETS = ("webhook", "heartbeat")       # secret targets that are site.yaml alerts settings


def _secret_target(site: Site, target: str) -> tuple[str, str, str | None, str]:
    """(service, account, reference, description) for `secret set` and `secret status`."""
    if target == "folio":
        f = site.folio
        return (credentials.FOLIO_SERVICE, credentials.folio_account(f["username"], f["tenant"]), f["secret"],
                "FOLIO password")
    if target == "webhook":
        return credentials.WEBHOOK_SERVICE, site.name, site.alerts.get("webhook"), "alert webhook URL"
    if target == "heartbeat":
        return credentials.HEARTBEAT_SERVICE, site.name, site.alerts.get("heartbeat"), "heartbeat URL"
    if target == "smtp":
        email = site.alerts.get("email") or {}
        if not email.get("username"):
            raise ConfigError("alerts.email.username is not set in site.yaml, so no SMTP password is needed.")
        return (credentials.SMTP_SERVICE, credentials.sftp_account(email["username"], email["smtp_host"]),
                email.get("secret"), "SMTP password")
    feed = _one(site, target)
    d = feed.delivery
    if d["transport"] != "sftp":
        raise ConfigError(f"Feed {target} delivers by {d['transport']}, which needs no password.")
    what = "SFTP key passphrase" if d.get("private_key") else "SFTP password"
    return credentials.SFTP_SERVICE, credentials.sftp_account(d["username"], d["host"]), d.get("secret"), what


def cmd_secret(args) -> int:
    site = _site(args)
    if args.action == "status":
        targets = ["folio"]
        targets += [t for t in ALERT_TARGETS if site.alerts.get(t)]
        if (site.alerts.get("email") or {}).get("username"):
            targets.append("smtp")
        feeds, _errors = _feeds(site)
        targets += [f.name for f in feeds if f.delivery["transport"] == "sftp"]
        missing = False
        for t in targets:
            service, account, ref, what = _secret_target(site, t)
            src = (site.folio_password_source() if t == "folio"
                   else credentials.source(ref, service, account))
            print(f"{t:<14} {what}: {src}")
            missing = missing or src.startswith("NOT FOUND")
        print(f"\n(Keyring entries belong to the Windows or system user who stored them; this is {getpass.getuser()}. "
              "A scheduled task must run as the same user.)")
        return 1 if missing else 0
    if not args.target:
        raise ConfigError("Say which secret: folio, smtp, webhook, heartbeat, or a feed name.")
    service, account, ref, what = _secret_target(site, args.target)
    if ref and str(ref).startswith("env:"):
        print(f"The {what} is read from the environment variable {ref[4:]} ({args.target}'s secret: {ref}). "
              "Set that variable instead, or change the setting to keyring.")
        return 1
    value = getpass.getpass(f"{what} for {account} (not shown): ")
    credentials.store(service, account, value)
    with History(site.history_path) as hist:
        hist.audit(None, None if args.target in ("folio", "smtp", *ALERT_TARGETS) else args.target,
                   "secret stored", {"what": what, "service": service, "account": account})
    print(f"Stored the {what} in the system keyring ({service}, {account}).")
    if args.target in ALERT_TARGETS and not ref:
        print(f"It is used once site.yaml says so: add `{args.target}: keyring` under alerts.")
    return 0


def cmd_schedule(args) -> int:
    if args.register:
        return _register_task(args)
    site = _site(args)
    feeds, _errors = _feeds(site)
    now = datetime.now(timezone.utc)
    zone = site.runs.get("timezone") or "this computer's local time"
    print(f"Schedules (times in {zone}):")
    for f in feeds:
        s = schedule.parse(f.schedule)
        nxt = schedule.next_slot(s, now, site.timezone)
        print(f"  {f.name:<16} {schedule.describe(s)}"
              + (f"; next {nxt.astimezone(site.timezone):%a %Y-%m-%d %H:%M}" if nxt and f.enabled else "")
              + ("" if f.enabled else " [paused]"))
    from .launcher import program_path

    exe = program_path()
    system = platform.system()
    user = None
    if system == "Windows":
        import os

        user = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")
    note = password_note(site)
    if note:
        print(f"\nWARNING  {note}")
    print("\nNothing runs until the computer's scheduler starts `run --due` every hour. Register it once with:\n")
    if system != "Windows":
        print("  " + schedule.os_command(site.root, exe, system, user))
    else:
        print("  In PowerShell:")
        print("  " + schedule.register_command(site.root, exe, "powershell"))
        print("\n  Or in Command Prompt (cmd.exe):")
        print("  " + schedule.register_command(site.root, exe, "cmd"))
        if any(c in exe + str(site.root) for c in "&^|<>%"):
            print("\n  WARNING  The program or site path contains one of & ^ | < > %, which Command Prompt treats "
                  "specially. Use the PowerShell form.")
        print("\n  Run it as the user who stored the secrets. It asks for that user's Windows password, so the task "
              "runs even when nobody is signed in. The task also runs on battery power and catches up after the "
              "computer was off or asleep.")
        print("\n  To register the same task by hand instead, paste these lines into PowerShell:\n")
        print("\n".join("  " + line for line in schedule.os_command(site.root, exe, system, user).splitlines()))
        print("\n  These work in PowerShell or Command Prompt:\n")
    for label, cmd in schedule.os_helpers(site.root, system).items():
        print(f"  {label + ':':<13} {cmd}")
    if system == "Windows":
        print("\n  In the last result, \"Task To Run\" should show the folio-exchange.exe path and --site folder, "
              "each in quotes.")
    return 0


def _register_task(args, run=None, root: Path | None = None) -> int:
    """Register the hourly `run --due` with the Windows Task Scheduler. schtasks asks for the Windows password
    itself; nothing here sees it. Works before the site is set up (the runs find nothing due until then)."""
    import getpass
    import os
    import subprocess

    from .launcher import program_path

    root = root or _new_site_root(args) or find_site(args.site).resolve()
    if not root.is_dir():
        raise ConfigError(f"No folder {root}")
    if platform.system() != "Windows":
        print("Add this line with `crontab -e`:\n")
        print("  " + schedule.os_command(root, program_path(), platform.system()))
        return 0
    import tempfile

    user = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")
    print(f"Registering \"{schedule.task_name(root)}\": every hour at minute 7, as {user}, also on battery power. "
          "Windows asks for that user's password, so the task can run while nobody is signed in.")
    with tempfile.TemporaryDirectory(prefix="fx-task-") as tmp:      # the task definition holds no secret
        xml_file = Path(tmp) / "task.xml"
        xml_file.write_text(schedule.windows_task_xml(root, program_path(), user), encoding="utf-16")
        done = (run or subprocess.run)(schedule.windows_task_args(root, xml_file, user))
    if done.returncode != 0:
        print(f"schtasks did not register the task (exit {done.returncode}). Nothing else was changed; run "
              "this command again to retry.")
        return 1
    print("Registered. `folio-exchange schedule` shows how to run it now, see its last result or remove it.")
    return 0


def cmd_ui(args) -> int:
    from .ui import serve

    root = _new_site_root(args)
    if root is not None:
        _make_site_folder(root)                        # a new site: the page starts with the FOLIO connection
        print(f"New site at {root}: the page starts with the FOLIO connection.")
    serve(root or find_site(args.site), port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_rename_site(args, run=None) -> int:
    """Rename the site folder (same parent). In a console the scheduled task can be registered again at once,
    since schtasks can ask for the Windows password here; the old task is removed once the new one is in."""
    import subprocess

    from . import siteops

    root = find_site(args.site).resolve()
    problem = siteops.name_problem(args.name.strip())
    if problem:
        raise ConfigError(problem)
    print(f"Rename the site folder {root} to {root.parent / args.name.strip()}?")
    if not args.yes and not _yes("Rename it now?"):
        print("Nothing was changed.")
        return 2
    res = siteops.rename_site(root, args.name, via="cli", run=run)
    print(f"Renamed. The site folder is now {res['root']}.")
    task = res["task"]
    for note in res["notes"]:
        if not (task and note.startswith("The scheduled task")):
            print("  " + note)
    if task is None:
        return 0
    print(f"The scheduled task \"{task['old']}\" still runs the old folder.")
    if args.yes or not _yes("Register the task for the new folder now? Windows asks for your password."):
        print(f"Register it later with: {task['register']}\nthen remove the old one with: {task['remove']}")
        return 0
    if _register_task(args, run=run, root=Path(res["root"])) != 0:
        print(f"The old task is still there; remove it once the new one is registered: {task['remove']}")
        return 1
    done = (run or subprocess.run)(["schtasks", "/Delete", "/TN", task["old"], "/F"])
    print("Removed the old task." if done.returncode == 0 else f"Remove the old task with: {task['remove']}")
    return 0


def cmd_launcher(args) -> int:
    from . import launcher

    root = _new_site_root(args)
    if root is not None:
        _make_site_folder(root)                        # set up later, on the page the launcher opens
    else:
        root = _site(args).root
    if args.folder:
        folder = Path(args.folder).expanduser().resolve()
        if not folder.is_dir():
            raise ConfigError(f"No folder {folder}")
    else:
        folder = launcher.desktop() or root
    path = launcher.write(root, folder, launcher.program_path(), platform.system())
    print(f"Made {path}")
    print(f"Double-click it to open the page for {root}. Its window keeps the page running; press Ctrl+C "
          "there to stop. If FOLIO-Exchange is installed somewhere else later, or the site folder moves, run "
          "this command again.")
    return 0


def cmd_init(args) -> int:
    import os

    from .wizard import ConsolePrompter, run_wizard

    if args.site and args.folder and Path(args.site).expanduser().resolve() != Path(args.folder).expanduser().resolve():
        raise ConfigError(f"Two different site folders were given ({args.site} and {args.folder}); give one.")
    chosen = args.folder or args.site or os.environ.get("FOLIO_EXCHANGE_SITE") or "."
    if not args.folder and not args.site and os.environ.get("FOLIO_EXCHANGE_SITE"):
        print(f"Using the site folder from FOLIO_EXCHANGE_SITE: {chosen}")
    return run_wizard(Path(chosen).expanduser().resolve(), ConsolePrompter())


# ------------------------------------------------------------------ main
def _version_text() -> str:
    """The version, and the git commit when pip recorded one (an install from git+https://...)."""
    try:
        from importlib.metadata import distribution

        info = json.loads(distribution("folio-exchange").read_text("direct_url.json") or "{}")
        commit = (info.get("vcs_info") or {}).get("commit_id")
    except Exception:  # noqa: BLE001 - the version is enough when the commit cannot be known
        commit = None
    return f"folio-exchange {__version__}" + (f" (commit {commit[:12]})" if commit else "")


class _Parser(argparse.ArgumentParser):
    """Usage mistakes exit 1: exit code 2 means a blocked or held run, and a scheduler must not mistake one
    for the other."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="folio-exchange",
                                description="Scheduled, recipe-driven exports from FOLIO to vendors.")
    p.add_argument("-s", "--site", help="the site folder (default: FOLIO_EXCHANGE_SITE, else the current folder)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=_version_text())
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    i = sub.add_parser("init", help="guided setup: connect to FOLIO, choose a recipe, preview, deliver, schedule")
    i.add_argument("folder", nargs="?", help="the site folder to create or update")
    c = sub.add_parser("check", help="test the FOLIO login, permissions and record counts")
    c.add_argument("feed", nargs="?")
    c.add_argument("--delivery", "--sftp", action="store_true",
                   help="test the vendor login and list the folder instead (uploads nothing)")
    c.add_argument("--alerts", action="store_true", help="send a test alert")
    pv = sub.add_parser("preview", help="build sample files from live data, without delivering")
    pv.add_argument("feed")
    pv.add_argument("-n", type=int, default=25, help="instances to sample (default 25)")
    pv.add_argument("--rows", type=int, default=3, help="item rows to print (default 3)")
    pv.add_argument("--columns", nargs="*", help="only print these column headers")
    r = sub.add_parser("run", help="sync, build, check and deliver")
    r.add_argument("feeds", nargs="*")
    r.add_argument("--all", action="store_true", help="every enabled feed")
    r.add_argument("--due", action="store_true", help="every feed whose scheduled time has passed (for the OS "
                                                      "scheduler)")
    r.add_argument("--full", action="store_true", help="refresh the whole cache from FOLIO")
    r.add_argument("--no-deliver", action="store_true", help="build and check files but do not send them")
    r.add_argument("--skip-sync", action="store_true", help="rebuild from the cache without contacting FOLIO")
    r.add_argument("--force", action="store_true", help="skip the item-count check (recorded in the audit log)")
    for name, helptext in (("release", "send a held, blocked or built run after checking it"),
                           ("deliver", "same as release")):
        rl = sub.add_parser(name, help=helptext)
        rl.add_argument("feed")
        rl.add_argument("run_dir", nargs="?", help="the run folder (default: the latest that can be released)")
        rl.add_argument("--yes", action="store_true", help="do not ask for confirmation")
        rl.add_argument("--force", action="store_true", help="skip the item-count check for a built run")
    s = sub.add_parser("sync", help="update the local cache from FOLIO only")
    s.add_argument("--full", action="store_true")
    b = sub.add_parser("build", help="write a feed's files from the cache, without contacting FOLIO")
    b.add_argument("feed")
    b.add_argument("--force", action="store_true", help="skip the item-count check")
    fd = sub.add_parser("feeds", help="list feeds, or pause or resume one")
    fd.add_argument("action", nargs="?", choices=["list", "pause", "resume"], default="list")
    fd.add_argument("feed", nargs="?")
    rc = sub.add_parser("recipes", help="list recipes, show one, validate a file, or print a layout summary")
    rc.add_argument("action", nargs="?", choices=["list", "show", "validate", "summary", "sources", "get"],
                    default="list")
    rc.add_argument("target", nargs="?", help="recipe id (id@range), feed name, a file to validate, or (for get) "
                                              "a source from `recipes sources`")
    rc.add_argument("--yes", action="store_true", help="get: install without asking")
    u = sub.add_parser("upgrade", help="move a feed to another recipe version, after showing what changes "
                       "(not an update of FOLIO-Exchange itself)")
    u.add_argument("feed")
    u.add_argument("--to", help="the new range, e.g. chq-academic@^2.0 (default: the newest version)")
    u.add_argument("--yes", action="store_true")
    sub.add_parser("fields", help="list the fields outputs can use")
    h = sub.add_parser("history", help="run history, one run's details, or the audit log")
    h.add_argument("feed", nargs="?")
    h.add_argument("-n", type=int, default=20)
    h.add_argument("--audit", action="store_true")
    h.add_argument("--show", metavar="RUN", help="a run id (from the list) or run folder")
    se = sub.add_parser("secret", help="store a secret in the system keyring, or show where each comes from")
    se.add_argument("action", choices=["set", "status"])
    se.add_argument("target", nargs="?", help="folio, smtp, webhook, heartbeat, or a feed name")
    sc = sub.add_parser("schedule", help="next runs, and the command that registers the hourly task")
    sc.add_argument("--register", action="store_true", help="register the hourly task now (Windows asks for "
                                                             "your Windows password)")
    ui = sub.add_parser("ui", help="open a simple page in your browser to look at feeds and run them")
    ui.add_argument("--port", type=int, help="port on this computer (default 8765, or a free one if it is taken)")
    ui.add_argument("--no-browser", action="store_true", help="do not open the browser")
    rn = sub.add_parser("rename-site", help="rename the site folder (its name shows on the page and in alerts)")
    rn.add_argument("name", help="the new folder name, in the same place")
    rn.add_argument("--yes", action="store_true", help="do not ask first (the scheduled task is then left to you)")
    la = sub.add_parser("launcher", help="make a file to double-click that opens the page for this site")
    la.add_argument("--folder", help="where to put it (default: your desktop)")
    return p


HANDLERS = {
    "init": cmd_init, "check": cmd_check, "preview": cmd_preview, "run": cmd_run, "release": cmd_release,
    "deliver": cmd_release, "sync": cmd_sync, "build": cmd_build, "feeds": cmd_feeds, "recipes": cmd_recipes,
    "upgrade": cmd_upgrade, "fields": cmd_fields, "history": cmd_history, "secret": cmd_secret,
    "schedule": cmd_schedule, "ui": cmd_ui, "launcher": cmd_launcher, "rename-site": cmd_rename_site,
}
NO_SITE = {"init", "fields", "recipes"}


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    logs_dir = None
    if args.command not in NO_SITE:
        try:
            logs_dir = load_site(find_site(args.site)).logs_dir
        except FxError:
            logs_dir = None
    _setup_logging(logs_dir, args.verbose)
    try:
        return HANDLERS[args.command](args)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except LockBusy as exc:
        log.error("%s", exc)
        return 1
    except FxError as exc:
        log.error("%s", exc)
        return 2 if exc.__class__.__name__ in ("SafetyCheckFailed", "HeldForRelease") else 1
    except OSError as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        return 1
    except sqlite3.Error as exc:
        log.error("A file in the site's data folder could not be read or written (%s: %s). Check that its drive "
                  "has free space and that no other program (a backup or an antivirus scan) holds it open, then "
                  "run again.", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
