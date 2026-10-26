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
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, credentials, delivery, reads, runner, schedule
from .client import FolioError
from .config import BUNDLED_RECIPES, Site, find_site, load_site, read_yaml
from .errors import ConfigError, FxError, LockBusy
from .fields import FIELDS
from .history import History
from .recipe import (
    Feed,
    RecipeLibrary,
    load_all,
    load_feed_data,
    load_recipe_file,
    parse_uses,
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


def _when(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _rewrite_key(path: Path, key: str, value: str) -> None:
    """Set a top-level ``key: value`` line in a feed file, keeping its comments and layout."""
    text = path.read_text(encoding="utf-8-sig")
    line = f"{key}: {value}"
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    text = pattern.sub(line, text, count=1) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ------------------------------------------------------------------ check
def _probe(client, label: str, path: str, permission: str) -> tuple[bool, str]:
    try:
        if path == reads.READS["marc"].path:
            client.get(path, {"limit": 0})
            return True, f"  OK       {label}"
        return True, f"  OK       {label}: {client.count(path):,} records"
    except FolioError as exc:
        text = str(exc)
        if " 401" in text or " 403" in text or "-> 401" in text or "-> 403" in text:
            return False, f"  DENIED   {label}: the account lacks {permission}"
        return False, f"  FAILED   {label}: {text[:160]}"


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
    chosen = [f for f in feeds if (f.name == args.feed if args.feed else f.enabled)]
    if args.feed and not chosen and args.feed not in errors:
        raise ConfigError(f"No feed called {args.feed!r}")
    ok = True
    with History(site.history_path) as hist:
        changed = runner.audit_settings(site, hist)
        if changed:
            print(f"Settings changed since the last check (recorded in the audit log): {', '.join(changed)}")
        print(f"Site: {site.root}")
        for name, err in errors.items():
            if not args.feed or name == args.feed:
                ok = False
                print(f"Feed {name}: PROBLEM\n  " + err.replace("\n", "\n  "))
        for p in library.problems():
            print("Recipe file problem:\n  " + p.replace("\n", "\n  "))
        now = datetime.now(timezone.utc)
        for f in chosen:
            newer = library.newer_major(f.uses)
            line = f"Feed {f.name}: {f.recipe.ref} ({f.uses}), {schedule.describe(schedule.parse(f.schedule))}"
            print(line + ("" if f.enabled else " [paused]"))
            if newer:
                print(f"  NOTE     {newer.ref} is available and waits for review: "
                      f"folio-exchange upgrade {f.name} --to {newer.id}@^{newer.version[0]}.{newer.version[1]}")
            if f.delivers and not f.checks.get("min_item_ratio"):
                print("  WARNING  checks.min_item_ratio is 0, so a sudden drop in items would be delivered")
            late = runner.overdue(site, f, hist, now)
            if late:
                print(f"  WARNING  {late}")
    if not site.alerts.get("webhook") and not site.alerts.get("email"):
        print("WARNING  No alert channel is set up, so a failed scheduled run would go unnoticed. "
              "Add alerts.webhook or alerts.email to site.yaml, then run `folio-exchange check --alerts`.")
    elif site.alerts.get("on") == "never":
        print("WARNING  alerts.on is never: failed runs send no alert.")

    print(f"FOLIO password: {site.folio_password_source()}")
    client = runner.client_for(site)
    client.login()
    print(f"Logged in to {client.base_url} (tenant {client.tenant}) as {client.username}")
    needed = set().union(*(set(f.reads) for f in chosen)) if chosen else set(reads.READS)
    for name in reads.ENTITY_ORDER + ("marc",):
        if name in needed:
            r = reads.READS[name]
            good, line = _probe(client, f"{name} ({r.path})", r.path, r.permission)
            ok &= good
            print(line)
    for kind, (path, _key, perm) in reads.REFDATA.items():
        good, line = _probe(client, f"{kind} ({path})", path, perm)
        ok &= good
        print(line)
    if not ok:
        print("\nSome checks failed. The FOLIO account needs these permissions (on Eureka, the matching "
              "capabilities in its role):")
        for perm in reads.permissions_for(needed):
            print(f"  {perm}")
    return 0 if ok else 1


def cmd_check_delivery(args) -> int:
    site = _site(args)
    feeds, errors = _feeds(site)
    chosen = [f for f in feeds if (f.name == args.feed if args.feed else f.enabled)]
    if args.feed and args.feed in errors:
        raise ConfigError(errors[args.feed])
    if not chosen:
        print("No feed to test." if not args.feed else f"No feed called {args.feed!r}")
        return 1
    worst = 0
    for f in chosen:
        d = f.delivery
        print(f"Feed {f.name}: {delivery.describe_destination(d)}")
        if d["transport"] == "sftp":
            pin = d.get("host_key_fingerprint") or f"known_hosts ({d.get('known_hosts')})"
            if (d.get("private_key") or "").strip():
                secret = "private key file " + d["private_key"]
            else:
                secret = "password: " + credentials.source(
                    d.get("secret"), credentials.SFTP_SERVICE, credentials.sftp_account(d["username"], d["host"]))
            print(f"  Connecting to {d['host']}:{d.get('port') or 22} as {d['username']}")
            print(f"  host key {pin}; {secret}")
        elif d["transport"] == "none":
            print("  Nothing to test.")
            continue
        try:
            names = delivery.check_delivery(d)
        except Exception as exc:  # noqa: BLE001 - paramiko raises many types; report them all the same way
            print(f"  FAILED   {type(exc).__name__}: {str(exc)[:200]}")
            worst = 1
            continue
        folder = d.get("remote_dir") or d.get("directory") or "."
        if names is None:
            print(f"  OK       logged in; listing {folder} is not permitted (normal for upload-only accounts). "
                  "Nothing was uploaded.")
            continue
        print(f"  OK       {'logged in; ' if d['transport'] == 'sftp' else ''}{folder} has {len(names)} entries. "
              "Nothing was uploaded.")
        for name in names[:20]:
            print(f"           {name}")
    return worst


# ------------------------------------------------------------------ runs
def cmd_run(args) -> int:
    site = _site(args)
    feeds, errors = _feeds(site)
    opts = runner.RunOptions(full=args.full, no_deliver=args.no_deliver, skip_sync=args.skip_sync,
                             force=args.force, trigger="schedule" if args.due else "manual")
    bad: dict[str, str] = {}
    if args.due:
        bad = runner.active_problems(site, errors)
        with History(site.history_path) as hist:
            runner.audit_settings(site, hist)
            now = datetime.now(timezone.utc)
            chosen = runner.due_feeds(site, feeds, now, hist)
            for f in feeds:
                late = runner.overdue(site, f, hist, now)
                if late and f not in chosen:
                    log.warning("Feed %s is %s", f.name, late)
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
            log.info("%s Skipping this hour; the next start will catch up.", exc)
            return 0
        raise
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
    wanted = reads_to_sync(feeds) or set(reads.READS)
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
    print(json.dumps({"run_dir": str(res.run_dir), "delivered": res.report.get("delivered")}, indent=2))
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
        path = site.feeds_dir / f"{args.feed}.yaml"
        if not path.is_file():
            raise ConfigError(f"No feed file {path}")
        _rewrite_key(path, "enabled", "false" if args.action == "pause" else "true")
        with History(site.history_path) as hist:
            hist.audit(None, args.feed, f"feed {args.action}d", {"file": str(path)})
            runner.audit_settings(site, hist)
        print(f"Feed {args.feed} {args.action}d.")
        return 0
    library = _library(site)
    feeds, errors = _feeds(site, library)
    if not feeds and not errors:
        print("No feeds yet. Run `folio-exchange init` to set one up.")
        return 0
    now = datetime.now(timezone.utc)
    with History(site.history_path) as hist:
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
                print(f"  last run  {_when(last['started'])}: {last['status']}"
                      + (f" ({last['error'][:120]})" if last.get("error") else ""))
                if last["status"] in ("held", "blocked", "built"):
                    print(f"            {runner.next_step(f, last['status'], Path(last['run_dir']))}")
            else:
                print("  last run  never")
            newer = library.newer_major(f.uses)
            if newer:
                print(f"  update    {newer.ref} is available (held for review; see `folio-exchange upgrade`)")
            late = runner.overdue(site, f, hist, now)
            if late:
                print(f"  WARNING   {late}")
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
        return 1 if library.problems() else 0
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
        target = args.to
    else:
        newer = library.newer_major(feed.uses) or library.latest(feed.recipe.id)
        target = f"{newer.id}@^{newer.version[0]}.{newer.version[1]}"
    rid, _spec = parse_uses(target)
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
    if new.recipe.version[0] != feed.recipe.version[0] and feed.delivers:
        print("  This is a new major version: the next delivery is held until you release it.")
    if not (args.yes or _yes(f"Change {feed.path.name} to uses: {target}?")):
        print("Nothing changed.")
        return 2
    _rewrite_key(feed.path, "uses", target)
    with History(site.history_path) as hist:
        hist.audit(None, feed.name, "recipe upgraded", {"from": feed.recipe.ref, "to": new.recipe.ref,
                                                         "uses": target})
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
            print(f"  status     {row['status']}")
            if row.get("error"):
                print(f"  problem    {row['error']}")
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
            print(f"{r['id']:>5}  {_when(r['started'])}  {r['feed']:<14} {r['status']:<8} "
                  f"items {r.get('items') if r.get('items') is not None else '-':>8}  "
                  f"{(r.get('error') or '')[:80]}")
    return 0


def _secret_target(site: Site, target: str) -> tuple[str, str, str | None, str]:
    """(service, account, reference, description) for `secret set` and `secret status`."""
    if target == "folio":
        f = site.folio
        return (credentials.FOLIO_SERVICE, credentials.folio_account(f["username"], f["tenant"]), f["secret"],
                "FOLIO password")
    if target == "webhook":
        return credentials.WEBHOOK_SERVICE, site.name, site.alerts.get("webhook"), "alert webhook URL"
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
        if site.alerts.get("webhook"):
            targets.append("webhook")
        if (site.alerts.get("email") or {}).get("username"):
            targets.append("smtp")
        feeds, _errors = _feeds(site)
        targets += [f.name for f in feeds if f.delivery["transport"] == "sftp"]
        for t in targets:
            service, account, ref, what = _secret_target(site, t)
            src = (site.folio_password_source() if t == "folio"
                   else credentials.source(ref, service, account))
            print(f"{t:<14} {what}: {src}")
        print(f"\n(Keyring entries belong to the Windows or system user who stored them; this is {getpass.getuser()}. "
              "A scheduled task must run as the same user.)")
        return 0
    if not args.target:
        raise ConfigError("Say which secret: folio, smtp, webhook, or a feed name.")
    service, account, ref, what = _secret_target(site, args.target)
    if ref and str(ref).startswith("env:"):
        print(f"The {what} is read from the environment variable {ref[4:]} ({args.target}'s secret: {ref}). "
              "Set that variable instead, or change the setting to keyring.")
        return 1
    value = getpass.getpass(f"{what} for {account} (not shown): ")
    credentials.store(service, account, value)
    with History(site.history_path) as hist:
        hist.audit(None, None if args.target in ("folio", "smtp", "webhook") else args.target,
                   "secret stored", {"what": what, "service": service, "account": account})
    print(f"Stored the {what} in the system keyring ({service}, {account}).")
    return 0


def cmd_schedule(args) -> int:
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
    exe = shutil.which("folio-exchange") or str(Path(sys.executable).with_name(
        "folio-exchange.exe" if platform.system() == "Windows" else "folio-exchange"))
    system = platform.system()
    user = None
    if system == "Windows":
        import os

        user = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")
    print("\nNothing runs until the computer's scheduler starts `run --due` every hour. Register it once with:\n")
    print("  " + schedule.os_command(site.root, exe, system, user))
    if system == "Windows":
        print("\n  Run this in a Command Prompt as the user who stored the secrets. It asks for that user's "
              "Windows password, so the task runs even when nobody is logged on.")
    for label, cmd in schedule.os_helpers(site.root, system).items():
        print(f"  {label + ':':<13} {cmd}")
    return 0


def cmd_init(args) -> int:
    from .wizard import ConsolePrompter, run_wizard

    root = Path(args.site or args.folder or ".").expanduser()
    return run_wizard(root.resolve(), ConsolePrompter())


# ------------------------------------------------------------------ main
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
    p.add_argument("--version", action="version", version=f"folio-exchange {__version__}")
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
    rc.add_argument("action", nargs="?", choices=["list", "show", "validate", "summary"], default="list")
    rc.add_argument("target", nargs="?", help="recipe id (id@range), feed name, or a file to validate")
    u = sub.add_parser("upgrade", help="move a feed to another recipe version, after showing what changes")
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
    se.add_argument("target", nargs="?", help="folio, smtp, webhook, or a feed name")
    sub.add_parser("schedule", help="next runs, and the command that registers the hourly task")
    return p


HANDLERS = {
    "init": cmd_init, "check": cmd_check, "preview": cmd_preview, "run": cmd_run, "release": cmd_release,
    "deliver": cmd_release, "sync": cmd_sync, "build": cmd_build, "feeds": cmd_feeds, "recipes": cmd_recipes,
    "upgrade": cmd_upgrade, "fields": cmd_fields, "history": cmd_history, "secret": cmd_secret,
    "schedule": cmd_schedule,
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


if __name__ == "__main__":
    sys.exit(main())
