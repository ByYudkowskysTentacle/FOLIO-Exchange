"""The pipeline for a feed: sync, build, check, hold or deliver, record, alert.

Every run gets a folder ``out/<feed>/<YYYYMMDD-HHMMSS>`` with its files and
``run-report.json``, a row in the run history, and (when it ends badly, or on the
site's alert setting) an alert. Nothing here ever reads, logs or reports a secret.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, checks, credentials, delivery, notify, schedule
from .build import BuildReport, build
from .cache import Cache
from .client import FolioClient, FolioError, LoginFailed, tls_verify
from .config import BUNDLED_RECIPES, Site, read_yaml
from .dates import now_utc
from .errors import ConfigError, DeliveryError, FxError, LockBusy, SafetyCheckFailed
from .history import History, current_user, file_digest
from .lock import RunLock
from .recipe import (
    Feed,
    collision_with,
    feed_paths,
    key_text,
    layout_changes,
    layout_signature,
    load_all,
    recipe_digest,
)
from .sync import Syncer, reads_to_sync

log = logging.getLogger(__name__)

UPGRADE_META = "upgrade:"            # history meta: upgrade:<feed> = {"to": version, "changes": [...]}
SETTINGS_AUDITED = "settings_audited"  # history meta: set once the first settings audit stored its snapshots
CODES = {"success": 0, "built": 0, "preview": 0, "blocked": 2, "held": 2, "failed": 1}
RELEASABLE = {"held", "blocked", "built"}


@dataclass
class RunOptions:
    full: bool = False
    no_deliver: bool = False
    skip_sync: bool = False
    force: bool = False
    trigger: str = "manual"


@dataclass
class RunResult:
    feed: str
    status: str
    code: int
    run_dir: Path | None
    report: dict = field(default_factory=dict)


def as_dict(obj) -> dict:
    if dataclasses.is_dataclass(obj):
        return {f.name: (dict(v) if isinstance(v := getattr(obj, f.name), dict) else v)
                for f in dataclasses.fields(obj)}
    return dict(obj)


def worst_code(results: list[RunResult]) -> int:
    codes = {r.code for r in results}
    return 1 if 1 in codes else 2 if 2 in codes else 0


def client_for(site: Site) -> FolioClient:
    f = site.folio
    return FolioClient(
        f["base_url"], f["tenant"], f["username"], site.folio_password() or "",
        timeout=float(f["timeout_seconds"]), max_retries=int(f["max_retries"]), verify_tls=tls_verify(f, site.root),
    )


def _new_run_dir(base: Path, prefix: str = "", hist: History | None = None) -> Path:
    """A new run folder named by the UTC time. Names are never reused, even after an old folder was pruned
    (history still names it), and runs within one second get -02, -03 ... so names sort oldest first."""
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    path = base / f"{prefix}{stamp}"
    n = 1
    while path.exists() or (hist is not None and hist.run_by_dir(path) is not None):
        n += 1
        path = base / f"{prefix}{stamp}-{n:02d}"
    return path


def _write_report(run_dir: Path | None, report: dict) -> None:
    if run_dir is not None and run_dir.exists():
        (run_dir / "run-report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def _major(version: str | None) -> str | None:
    return str(version).split(".", 1)[0] if version else None


def hold_reason(hist: History, feed: Feed) -> str | None:
    """Why the next delivery must wait for a person, or None. A first delivery to a destination, the first
    after a major recipe version change, and the first whose layout differs from the last delivery to that
    destination are held. A delivery recorded before layouts were kept has none: that alone never holds."""
    dest = delivery.destination_key(feed.delivery)
    last = hist.last_delivered(feed.name, dest)
    if last is None:
        return f"first delivery to {dest}"
    reasons = []
    if _major(last.get("recipe_version")) != _major(feed.recipe.version_str):
        reasons.append(f"first delivery on recipe {feed.recipe.ref} (the last delivery used version "
                       f"{last.get('recipe_version')})")
        reasons += upgrade_review(hist, feed)
    note = layout_note(last, layout_signature(feed))
    if note:
        reasons.append(note)
    return "; ".join(reasons) or None


def upgrade_review(hist: History, feed: Feed) -> list[str]:
    """What `upgrade` found the new major version changes in the feed's settings, when it moved to this major."""
    try:
        stored = json.loads(hist.get_meta(f"{UPGRADE_META}{feed.name}") or "{}")
    except ValueError:
        return []
    if _major(stored.get("to")) != _major(feed.recipe.version_str) or not stored.get("changes"):
        return []
    return ["the new version changes: " + "; ".join(map(str, stored["changes"]))]


def layout_note(last: dict | None, layout: object) -> str | None:
    """What changed in the files' layout since the delivery ``last``, or None when nothing did, or when either
    side has no recorded layout (runs from before layouts were kept)."""
    before = ((last or {}).get("report") or {}).get("layout")
    if not isinstance(before, dict) or not isinstance(layout, dict):
        return None
    changes = layout_changes(before, layout)
    if not changes:
        return None
    when = _local((last or {}).get("started"))
    return f"the files' layout changed since the last delivery on {when}: " + "; ".join(changes)


def _local(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(iso)


def next_step(feed: Feed, status: str, run_dir: Path | None) -> str:
    where = f" {run_dir}" if run_dir else ""
    if status == "held":
        return f"Check the files in{where}, then run: folio-exchange release {feed.name}"
    if status == "blocked":
        return (f"Look at the files and the reason above. If the change is expected, run: "
                f"folio-exchange release {feed.name}")
    if status == "failed":
        return f"Fix the problem above, then run: folio-exchange run {feed.name}"
    if status in ("built", "success") and not feed.delivers:
        return (f"Dry run: nothing was sent. The files are in{where}; `folio-exchange history {feed.name}` "
                "lists the runs. This feed has no delivery set up yet: when the vendor's details are ready, run "
                "`folio-exchange init` again and choose how the files are delivered.")
    if status == "built":
        return f"To send these files, run: folio-exchange release {feed.name}"
    return ""


# ------------------------------------------------------------------ alerts
def alert(site: Site, feed: str | None, status: str, text: str, *, force: bool = False) -> list[str]:
    if not force and not notify.should_alert(site.alerts, status):
        return []
    subject = f"FOLIO-Exchange [{site.name}]{f' {feed}' if feed else ''}: {status.upper()}"
    return notify.send(site.alerts, subject, text, account=site.name)


def _alert_run(site: Site, feed: Feed, res: RunResult, previous: dict | None) -> None:
    b = res.report.get("build") or {}
    lines = [f"Feed {feed.name} ({feed.title}), recipe {feed.recipe.ref}: {res.status}."]
    if b:
        lines.append(f"Bibs {b.get('bibs_written', 0):,}, items {b.get('items_written', 0):,}.")
    if res.report.get("error"):
        lines.append(str(res.report["error"]))
    if res.report.get("held"):
        lines.append(f"Held: {res.report['held']}.")
    lines.extend(f"Warning: {w}" for w in res.report.get("warnings") or [])
    if res.run_dir:
        lines.append(f"Run folder: {res.run_dir}")
    step = next_step(feed, res.status, res.run_dir)
    if step:
        lines.append(step)
    text = "\n".join(lines)
    if notify.should_alert(site.alerts, res.status):
        alert(site, feed.name, res.status, text)
    elif res.status in ("success", "built") and previous and previous.get("status") in ("failed", "blocked"):
        alert(site, feed.name, "recovered", text, force=site.alerts.get("on") != "never")
    elif res.report.get("warnings") and res.report.get("warnings") != ((previous or {}).get("report") or {}).get(
            "warnings"):
        alert(site, feed.name, "warning", text)          # once, when a warning first appears or changes


# --------------------------------------------------------- settings audit
def redact(data, key: str = ""):
    """A copy of settings that is safe to store: secret-looking keys, and secret references that hold
    something other than a reference (someone typed the secret itself), are masked."""
    if isinstance(data, dict):
        return {k: redact(v, str(k)) for k, v in data.items()}
    if isinstance(data, list):
        return [redact(v, key) for v in data]
    if key.lower() in credentials.SECRET_KEYS:
        return "[removed: secrets never go in files]"
    if (key in ("private_key", "known_hosts") and key_text(data)) or (isinstance(data, str) and "PRIVATE KEY" in data):
        return "[removed: key text, not the path of a key file]"
    if key in ("secret", "webhook", "heartbeat") and data is not None and not credentials.is_secret_ref(data):
        return "[removed: not keyring or env:NAME]"
    return data


def _flatten(data, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(data, list) and any(isinstance(v, dict) for v in data):    # e.g. outputs, columns
        for i, v in enumerate(data):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = data
    return out


def site_recipe_paths(site: Site) -> list[Path]:
    """The recipe files in the site's own recipe folders (not the ones bundled with the program)."""
    bundled = BUNDLED_RECIPES.resolve()
    skip = {bundled, site.root.resolve()}
    return [p for d in site.recipe_dirs if d.is_dir() and d.resolve() not in skip
            for p in sorted(d.glob("*/*.yaml")) if p.is_file()]


def _site_rel(site: Site, path: Path) -> str:
    return path.relative_to(site.root).as_posix() if path.is_relative_to(site.root) else str(path)


def audit_settings(site: Site, hist: History) -> list[str]:
    """Audit hand edits to site.yaml, the feed files and the site's own recipe files since the last check (the
    files hold no secrets). A file added or removed is audited with its settings, except on a new site's first
    audit, which only records what is there."""
    changed: list[str] = []
    feeds = feed_paths(site)
    known = dict(hist.db.execute("SELECT key, value FROM meta WHERE key GLOB 'settings:*' AND value IS NOT NULL"))
    established = bool(known) or hist.get_meta(SETTINGS_AUDITED) is not None
    present: set[str] = set()
    for path in [site.root / "site.yaml", *feeds, *site_recipe_paths(site)]:
        rel = _site_rel(site, path)
        key = f"settings:{rel}"
        present.add(key)
        try:
            current = redact(read_yaml(path))
        except ConfigError:
            continue
        stored = known.get(key)
        snapshot = json.dumps(current, sort_keys=True, default=str)
        if stored == snapshot:
            continue
        hist.set_meta(key, snapshot)
        if stored is None:
            if established:
                hist.audit(None, path.stem if path in feeds else None, "settings file added",
                           {"file": rel, "settings": current})
                changed.append(rel)
            continue
        before, after = _flatten(redact(json.loads(stored))), _flatten(current)
        diff = {k: {"from": before.get(k), "to": after.get(k)}
                for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)}
        if not diff:
            continue                          # only masked in a snapshot stored before the mask
        hist.audit(None, path.stem if path in feeds else None, "settings changed",
                   {"file": rel, "changes": diff})
        changed.append(rel)
    feeds_rel = _site_rel(site, site.feeds_dir)
    for key in sorted(set(known) - present):
        rel = key.removeprefix("settings:")
        feed = Path(rel).stem if Path(rel).parent == Path(feeds_rel) else None
        hist.audit(None, feed, "settings file removed",
                   {"file": rel, "settings": redact(json.loads(known[key]))})
        hist.db.execute("DELETE FROM meta WHERE key=?", (key,))
        hist.db.commit()
        changed.append(rel)
    if not established:
        hist.set_meta(SETTINGS_AUDITED, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    for path in feed_paths(site):
        key = f"feed_seen:{path.stem}"
        if hist.get_meta(key) is None:
            hist.set_meta(key, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return changed


def feed_created(hist: History, feed: Feed) -> datetime:
    """When this site first saw the feed (its first scheduled slot comes after that)."""
    seen = hist.get_meta(f"feed_seen:{feed.name}")
    if seen:
        return datetime.fromisoformat(seen)
    return datetime.fromtimestamp(feed.path.stat().st_mtime, timezone.utc)


def delivery_clash(site: Site, feed: Feed) -> str | None:
    """For a paused feed run or released by hand: another feed of the site, paused or not, that sends one of
    its file names to the same place. Loading compares enabled feeds only (a clash there stops both), so an
    enabled feed owns its file names and keeps running; the paused copy is refused here."""
    return None if feed.enabled else collision_with(feed, load_all(site)[0])


# ------------------------------------------------------------------ runs
def run_feed(site: Site, feed: Feed, opts: RunOptions, *, hist: History,
             sync_report: dict | None = None) -> RunResult:
    """One feed, with the site lock already held by the caller (see run_many)."""
    out_base = site.out_dir / feed.name
    run_dir = _new_run_dir(out_base, hist=hist)
    dest = delivery.destination_key(feed.delivery)
    previous = hist.last_run(feed.name)
    run_id = hist.start_run(feed.name, feed.recipe.id, feed.recipe.version_str, opts.trigger, run_dir, dest)
    report: dict = {
        "version": __version__, "feed": feed.name, "recipe": feed.recipe.id,
        "recipe_version": feed.recipe.version_str, "trigger": opts.trigger, "started": now_utc().isoformat(),
        "run_dir": str(run_dir), "destination": delivery.describe_destination(feed.delivery),
        "recipe_sha256": recipe_digest(feed.recipe), "layout": layout_signature(feed),
    }
    if site.folio.get("verify_tls") is False:
        report["tls_unchecked"] = True                 # FOLIO's certificate was not checked (folio.verify_tls)
    status, code, error = "success", 0, None
    rep: BuildReport | None = None
    digests: list[dict] = []
    sent: list[str] = []
    try:
        if sync_report is not None:
            report["sync"] = sync_report
        elif not opts.skip_sync:
            report["sync"] = sync(site, set(feed.reads), opts.full)
        cache = Cache(site.cache_path)
        try:
            other = other_folio(cache, site)
            if other:
                raise FxError(f"The local cache holds records from {other}, not {folio_source(site)}. Run the feed "
                              "without --skip-sync (or `folio-exchange sync`) to download this FOLIO's records.")
            not_ready = cache_not_ready(cache, feed)
            if not_ready:
                raise FxError(f"The cache cannot be used for feed {feed.name} yet ({not_ready}). Run it without "
                              "--skip-sync, or run `folio-exchange sync` first.")
            rep = build(cache, feed, run_dir)
        except sqlite3.DatabaseError as exc:
            cache.close()
            moved = quarantine_cache(site)
            raise FxError(f"The local cache was damaged ({exc}); it was moved aside to {moved.name}. Run the feed "
                          "again without --skip-sync to download everything afresh.") from None
        finally:
            cache.close()
        report["build"] = as_dict(rep)
        warnings = marc_warnings(site, rep)
        if warnings:
            report["warnings"] = warnings
            for w in warnings:
                log.warning("%s", w)
        digests = [file_digest(run_dir / name) for name in rep.files]
        report["files"] = digests
        checks.required_fields(feed, rep)
        last = hist.last_delivered(feed.name, dest)
        report["last_delivered_items"] = last.get("items") if last else None
        if not opts.force:
            checks.item_ratio(feed, rep.items_written, last.get("items") if last else None)
        if opts.no_deliver:
            status = "built"
            log.info("--no-deliver: files left in %s", run_dir)
        elif not feed.delivers:
            log.info("Feed %s has transport none; files stay in %s", feed.name, run_dir)
        else:
            clash = delivery_clash(site, feed)
            if clash:
                raise FxError(f"Nothing was sent: {clash}")
            reason = hold_reason(hist, feed)
            if reason:
                status, code = "held", 2
                report["held"] = reason
                log.warning("Held for review (%s). %s", reason, next_step(feed, "held", run_dir))
            else:
                sent = delivery.deliver([run_dir / n for n in rep.files], feed.delivery, feed.name)
                report["delivered"] = sent
    except SafetyCheckFailed as exc:
        status, code = "blocked", 2
        error = (f"{exc} The files are in {run_dir}. If the drop is not expected, fix it in FOLIO and run "
                 f"`folio-exchange run {feed.name}` again; this run is then never sent.")
        log.error("%s", error)
    except DeliveryError as exc:
        status, code, error = "failed", 1, str(exc)
        report["partially_delivered"] = exc.sent
        hist.audit(None, feed.name, "partial delivery", {"run_dir": str(run_dir), "sent": exc.sent,
                                                          "destination": dest})
        log.error("Run failed: %s", exc)
    except FxError as exc:
        status, code, error = "failed", 1, str(exc)
        if isinstance(exc, LoginFailed):
            report["no_retry"] = True
        log.error("Run failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - top level: report every failure
        status, code, error = "failed", 1, f"{type(exc).__name__}: {exc}"
        log.error("Run failed: %s", error)
        log.debug("%s", traceback.format_exc())
    if error:
        report["error"] = error
    report["status"] = status
    report["finished"] = now_utc().isoformat()
    if status not in ("success",) or not feed.delivers:
        step = next_step(feed, status, run_dir if run_dir.exists() else None)
        if step:
            report["next"] = step
    _write_report(run_dir, report)
    hist.finish_run(
        run_id, status, items=rep.items_written if rep else None, bibs=rep.bibs_written if rep else None,
        files=digests, delivered=sent, error=error, report=report,
    )
    if opts.force and status in ("success", "built"):
        hist.audit(None, feed.name, "safety check skipped (--force)",
                   {"run_dir": str(run_dir), "items": rep.items_written if rep else None})
    _prune(site, feed, hist)
    res = RunResult(feed.name, status, code, run_dir if run_dir.exists() else None, report)
    _alert_run(site, feed, res, previous)
    return res


def marc_warnings(site: Site, rep: BuildReport) -> list[str]:
    """Plain warnings when bibs that should carry full MARC were sent as brief records."""
    out = []
    examples = ", ".join(rep.marc_problem_hrids[:5])
    if rep.bibs_marc_missing:
        text = (f"{rep.bibs_marc_missing:,} bibs whose source is MARC had no record in Source Record Storage and "
                f"were sent as brief records (for example {examples}).")
        if rep.bibs_marc_missing_consortium and not site.folio.get("srs_include_shared"):
            text += (" Some are shared consortium records: this looks like an ECS member tenant, so set "
                     "folio.srs_include_shared: true in site.yaml.")
        out.append(text)
    if rep.marc_unreadable:
        out.append(f"{rep.marc_unreadable:,} MARC records could not be read or written and were sent as brief records "
                   f"(for example {examples}); see the log.")
    return out


def cache_not_ready(cache: Cache, feed: Feed) -> str | None:
    """Why the cache cannot be built from for ``feed`` (a read never fully synced), else None."""
    for entity in ("instances", "holdings", "items", "loans"):
        if entity in feed.reads:
            state = cache.sync_state(entity)
            if state:
                return f"{entity}: {state}"
    if "marc" in feed.reads and cache.get_meta("instances.marc") != "1":
        return "MARC records have not been fetched"
    return None


def _prune(site: Site, feed: Feed, hist: History) -> None:
    last = hist.last_delivered(feed.name)
    # Runs waiting for release are kept; once a later delivery supersedes them they can be pruned.
    protect = {Path(r["run_dir"]) for r in hist.runs(feed.name, 200)
               if r.get("run_dir") and r.get("status") in ("held", "blocked")
               and (not last or r["id"] > last["id"])}
    if last and last.get("run_dir"):
        protect.add(Path(last["run_dir"]))
    delivery.prune_runs(site.out_dir / feed.name, int(site.runs["keep_local_runs"]), protect)


def quarantine_cache(site: Site, kind: str = "broken") -> Path:
    """Move a damaged cache, or one filled from another FOLIO (``kind="other-folio"``), and its -wal/-shm
    files aside; the next sync starts afresh with a full download. History is never touched."""
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    moved = site.cache_path.with_name(f"cache.{kind}-{stamp}.sqlite")
    for suffix in ("", "-wal", "-shm"):
        src = site.cache_path.with_name(site.cache_path.name + suffix)
        if src.exists():
            src.replace(moved.with_name(moved.name + suffix))
    return moved


def folio_source(site: Site) -> str:
    """Which FOLIO the cache's records come from: the API address and the tenant."""
    return f"{str(site.folio['base_url']).rstrip('/').lower()} tenant {site.folio['tenant']}"


def other_folio(cache: Cache, site: Site) -> str | None:
    """The FOLIO a cache was filled from, when it is not this site's (a changed address or tenant)."""
    have = cache.get_meta("folio_source")
    return have if have is not None and have != folio_source(site) else None


def sync(site: Site, reads: set[str], full: bool = False) -> dict:
    """Update the shared cache. A damaged cache is moved aside once and rebuilt with a full download, and so is
    one filled from another FOLIO: a delta on top of it would mix two catalogues."""
    for attempt in (1, 2):
        cache = None
        try:
            cache = Cache(site.cache_path)
            other = other_folio(cache, site)
            if other:
                cache.close()
                moved = quarantine_cache(site, "other-folio")
                log.warning("The local cache holds records from %s, not %s; moved aside to %s, downloading "
                            "everything from this FOLIO", other, folio_source(site), moved.name)
                cache, full = Cache(site.cache_path), True
            result = Syncer(client_for(site), cache, site).run(reads, force_full=full)
            cache.set_meta("folio_source", folio_source(site))
            return as_dict(result)
        except sqlite3.DatabaseError as exc:
            if cache is not None:
                cache.close()
                cache = None
            moved = quarantine_cache(site)
            if attempt == 2:
                raise FxError(f"The local cache is damaged ({exc}) and could not be rebuilt; it was moved aside to "
                              f"{moved.name}. Check the disk, then run again.") from None
            log.warning("The local cache was damaged (%s); moved aside to %s, downloading everything again",
                        exc, moved.name)
            full = True
        finally:
            if cache is not None:
                cache.close()
    raise AssertionError("unreachable")


def run_many(site: Site, feeds: list[Feed], opts: RunOptions, *, all_feeds: list[Feed] | None = None,
             bad_feeds: dict[str, str] | None = None) -> list[RunResult]:
    """Run ``feeds`` under one lock and one sync. The sync covers every enabled feed's reads (and the
    feeds being run), so the shared cache never goes stale for another feed."""
    results: list[RunResult] = []
    with RunLock(site.lock_path), History(site.history_path) as hist:
        stale = hist.fail_stale()
        if stale:
            log.warning("%d earlier run(s) never finished; marked as failed", stale)
            alert(site, None, "failed", f"{stale} earlier run(s) stopped without finishing and were marked "
                                        "failed. Check logs/folio-exchange.log.")
        audit_settings(site, hist)
        hist.prune(int(site.runs["history_months"]))
        for name, err in (bad_feeds or {}).items():
            log.error("Feed %s was not run: %s", name, err)
            if hist.get_meta(f"problem:{name}") != err:      # alert once per new problem, not every hour
                hist.set_meta(f"problem:{name}", err)
                alert(site, name, "failed", f"Feed {name} was not run because its file has a problem:\n{err}")
            results.append(RunResult(name, "failed", 1, None, {"error": err}))
        for f in all_feeds or feeds:
            if hist.get_meta(f"problem:{f.name}") is not None:
                hist.set_meta(f"problem:{f.name}", None)
        if not feeds:
            return results
        sync_report = None
        if not opts.skip_sync:
            reads = reads_to_sync(all_feeds or feeds) | reads_to_sync([dataclasses.replace(f, enabled=True)
                                                                         for f in feeds])
            try:
                sync_report = sync(site, reads, opts.full)
            except Exception as exc:  # noqa: BLE001 - every feed fails the same way; report it once per feed
                err = str(exc) if isinstance(exc, FxError) else f"{type(exc).__name__}: {exc}"
                log.error("Sync failed: %s", err)
                for f in feeds:
                    run_id = hist.start_run(f.name, f.recipe.id, f.recipe.version_str, opts.trigger, None,
                                            delivery.destination_key(f.delivery))
                    rep = {"error": f"Sync failed: {err}", "status": "failed",
                           "next": "Fix the problem above, then run: folio-exchange check"}
                    if isinstance(exc, LoginFailed):
                        rep["no_retry"] = True
                    hist.finish_run(run_id, "failed", error=f"Sync failed: {err}", report=rep)
                    results.append(RunResult(f.name, "failed", 1, None, rep))
                advice = ("Run `folio-exchange check` to test the connection." if isinstance(exc, FolioError)
                          else "See logs/folio-exchange.log for details.")
                alert(site, ", ".join(f.name for f in feeds), "failed",
                      f"Could not update the cache from FOLIO, so no feed ran.\n{err}\n{advice}")
                return results
        for f in feeds:
            results.append(run_feed(site, f, opts, hist=hist, sync_report=sync_report))
    return results


def active_problems(site: Site, errors: dict[str, str]) -> dict[str, str]:
    """Problems with feed files that are not paused (a paused file with a problem is shown by `feeds` and
    `check`, but does not fail scheduled runs)."""
    out = {}
    for name, err in errors.items():
        try:
            raw = read_yaml(site.feeds_dir / f"{name}.yaml")
        except ConfigError:
            raw = {}
        if raw.get("enabled") is not False:
            out[name] = err
    return out


def due_feeds(site: Site, feeds: list[Feed], now: datetime, hist: History) -> list[Feed]:
    out = []
    for f in feeds:
        if not f.enabled:
            continue
        s = schedule.parse(f.schedule)
        slot = schedule.last_slot(s, now, site.timezone)
        runs = hist.runs_since(f.name, slot) if slot else []
        if schedule.is_due(s, now, site.timezone, runs, int(site.runs["max_attempts_per_slot"]),
                           feed_created(hist, f)):
            out.append(f)
    return out


def overdue(site: Site, feed: Feed, hist: History, now: datetime) -> str | None:
    """A warning when a scheduled feed's last success is older than two slots (the scheduler may have stopped)."""
    s = schedule.parse(feed.schedule)
    if s.kind == "manual" or not feed.enabled:
        return None
    slot = schedule.last_slot(s, now, site.timezone)
    if slot is None:
        return None
    before = schedule.last_slot(s, slot - timedelta(seconds=1), site.timezone)
    if before is None or before <= feed_created(hist, feed):
        return None
    good = [r for r in hist.runs(feed.name, 500)
            if r.get("status") == "success" or (r.get("trigger") == "schedule" and r.get("status") in schedule.SETTLED)]
    last_ok = datetime.fromisoformat(good[0]["started"]) if good else None
    if last_ok is None or last_ok < before:
        when = f"last success {last_ok:%Y-%m-%d %H:%M} UTC" if last_ok else "no successful run yet"
        return f"overdue: {when}; check that the hourly `run --due` task is still running"
    return None


# ------------------------------------------------------------- release
def releasable_run(hist: History, feed: Feed, run_dir: Path | None, force: bool = False) -> dict:
    """The run to release: the newest held, blocked or built run that no later delivery has superseded, or
    the one named. A superseded run (older than the last delivery) needs ``force``: sending it would replace
    the vendor's newer files."""
    last = hist.last_delivered(feed.name)
    if run_dir is not None:
        row = hist.run_by_dir(Path(run_dir).resolve()) or hist.run_by_dir(Path(run_dir))
        if row is None or row.get("feed") != feed.name:
            raise FxError(f"{run_dir} is not a run of feed {feed.name} in this site's history.")
        if last and row["id"] < last["id"] and not force:
            raise FxError(f"{run_dir} is older than the delivery of {_local(last['started'])}; sending it would "
                          "replace the vendor's newer files. Add --force only if that is what you want.")
    else:
        rows = [r for r in hist.runs(feed.name, 200)
                if r.get("status") in RELEASABLE and r.get("run_dir") and (not last or r["id"] > last["id"])]
        if not rows:
            raise FxError(f"Feed {feed.name} has no held, blocked or built run newer than its last delivery. "
                          f"`folio-exchange history {feed.name}` lists its runs.")
        row = rows[0]
        newer_failed = [r for r in hist.runs(feed.name, 200) if r["id"] > row["id"] and r.get("status") == "failed"]
        if newer_failed:
            raise FxError(
                f"The newest run of {feed.name} ({_local(newer_failed[0]['started'])}) failed, and the run waiting "
                f"for release is older ({_local(row['started'])}). Run the feed again rather than send older files; "
                f"to send that older run anyway, name its folder: folio-exchange release {feed.name} {row['run_dir']}")
    if row["status"] not in RELEASABLE:
        raise FxError(
            f"Run {row['run_dir']} has status {row['status']!r}; only held, blocked or built runs can be released"
            + (" (failed runs may hold partial files)." if row["status"] == "failed" else ".")
        )
    return row


def release(site: Site, feed: Feed, run_dir: Path | None, *, force: bool = False, actor: str | None = None,
            confirm=None) -> RunResult:
    """Deliver a held, blocked or built run after a person has looked at it. ``confirm(summary) -> bool``
    is asked before anything is sent. Built runs still pass the checks unless ``force``; releasing a held
    or blocked run is itself the person's approval. A feed with no delivery (transport none) is refused."""
    if not feed.delivers:
        raise FxError(f"Feed {feed.name} has no delivery set up, so there is nothing to send; its files stay in "
                      "the run folder. Add a delivery with `folio-exchange init`.")
    with RunLock(site.lock_path), History(site.history_path) as hist:
        row = releasable_run(hist, feed, run_dir, force)
        rdir = Path(row["run_dir"])
        files = row.get("files") or []
        if not files or not rdir.is_dir():
            raise FxError(f"{rdir} has no recorded files; it cannot be released. Run the feed again.")
        for f in files:
            path = rdir / f["name"]
            if not path.is_file():
                raise FxError(f"{path} is missing; the run cannot be released. Run the feed again.")
            if file_digest(path)["sha256"] != f["sha256"]:
                raise FxError(f"{path} changed after it was built (was it opened and saved in another program?). "
                              "Run the feed again rather than sending an edited file.")
        dest = delivery.destination_key(feed.delivery)
        if row.get("destination") and row["destination"] != dest:
            raise FxError(f"The feed now delivers to {dest}, but this run was built for {row['destination']}. "
                          "Run the feed again.")
        clash = delivery_clash(site, feed)
        if clash:
            raise FxError(f"Not released: {clash}")
        last = hist.last_delivered(feed.name, dest)
        if row["status"] == "built" and not force:
            checks.item_ratio(feed, int(row.get("items") or 0), last.get("items") if last else None)
        summary = {
            "feed": feed.name, "run_dir": str(rdir), "status": row["status"], "built": row.get("started"),
            "recipe": f"{row.get('recipe')}@{row.get('recipe_version')}", "bibs": row.get("bibs"),
            "items": row.get("items"), "last_delivered_items": last.get("items") if last else None,
            "last_delivered": last.get("started") if last else None,
            "reason": row.get("error") or (row.get("report") or {}).get("held")
            or layout_note(last, (row.get("report") or {}).get("layout")),
            "files": [(f["name"], f["bytes"]) for f in files], "destination": delivery.describe_destination(
                feed.delivery),
            "newer_runs": [r["run_dir"] for r in hist.runs(feed.name, 20)
                           if r["id"] > row["id"] and r.get("status") in ("success",) and r.get("delivered")],
        }
        if confirm is not None and not confirm(summary):
            return RunResult(feed.name, row["status"], 2, rdir, {"cancelled": True, **summary})
        try:
            sent = delivery.deliver([rdir / f["name"] for f in files], feed.delivery, feed.name)
        except DeliveryError as exc:
            hist.audit(actor, feed.name, "partial delivery", {"run_dir": str(rdir), "sent": exc.sent,
                                                               "destination": dest})
            raise
        report = dict(row.get("report") or {})
        report.update({"status": "success", "delivered": sent, "released_by": actor or current_user(),
                       "released": now_utc().isoformat()})
        report.pop("next", None)
        if report.get("error"):
            report["released_after"] = report.pop("error")
        _write_report(rdir, report)
        hist.mark_delivered(row["id"], "success", sent, files, report)
        if not row.get("destination"):
            hist.db.execute("UPDATE runs SET destination=? WHERE id=?", (dest, row["id"]))
            hist.db.commit()
        hist.audit(actor, feed.name, f"released {row['status']} run",
                   {"run_dir": str(rdir), "items": row.get("items"), "previous_items": summary["last_delivered_items"],
                    "destination": dest, "files": files, "reason": summary["reason"], "force": force})
        return RunResult(feed.name, "success", 0, rdir, report)


# ------------------------------------------------------------- preview
def preview(site: Site, feed: Feed, instances: int = 10) -> RunResult:
    """Sample live data into a temporary cache and build PREVIEW_ files under out/<feed>/preview-…"""
    run_dir = _new_run_dir(site.out_dir / feed.name, "preview-")
    temp = site.cache_path.with_name("preview-cache.sqlite")
    for p in (temp, temp.with_name(temp.name + "-wal"), temp.with_name(temp.name + "-shm")):
        p.unlink(missing_ok=True)
    report: dict = {"version": __version__, "feed": feed.name, "recipe": feed.recipe.id,
                    "recipe_version": feed.recipe.version_str, "trigger": "preview", "run_dir": str(run_dir),
                    "started": now_utc().isoformat()}
    status, error = "preview", None
    cache = Cache(temp)
    try:
        report["sync"] = as_dict(Syncer(client_for(site), cache, site).sample(feed.reads, instances))
        rep = build(cache, feed, run_dir, limit_bibs=instances, name_prefix="PREVIEW_")
        report["build"] = as_dict(rep)
        report["files"] = [file_digest(run_dir / n) for n in rep.files]
        samples = checks.sample_files(feed)
        if samples:
            report["conformance"] = checks.conformance(feed, run_dir, samples, rep.outputs)
        try:
            checks.required_fields(feed, rep)
        except SafetyCheckFailed as exc:
            report["warnings"] = [str(exc)]
    except FxError as exc:
        status, error = "failed", str(exc)
    except Exception as exc:  # noqa: BLE001 - report every failure the same way
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        log.debug("%s", traceback.format_exc())
    finally:
        cache.close()
        for p in (temp, temp.with_name(temp.name + "-wal"), temp.with_name(temp.name + "-shm")):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    if error:
        report["error"] = error
    report["status"] = status
    report["finished"] = now_utc().isoformat()
    _write_report(run_dir, report)
    with History(site.history_path) as hist:
        rid = hist.start_run(feed.name, feed.recipe.id, feed.recipe.version_str, "preview", run_dir)
        b = report.get("build") or {}
        hist.finish_run(rid, status, items=b.get("items_written"), bibs=b.get("bibs_written"),
                        files=report.get("files"), error=error, report=report)
    return RunResult(feed.name, status, CODES[status], run_dir if run_dir.exists() else None, report)


__all__ = ["RunOptions", "RunResult", "run_feed", "run_many", "due_feeds", "release", "preview", "LockBusy",
           "worst_code", "overdue", "hold_reason", "audit_settings", "client_for", "sync", "alert", "CODES"]
