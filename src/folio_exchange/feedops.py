"""Feed operations shared by the command line and the local page, so the two never drift apart.

Pausing and resuming work on the feed file itself, so even a feed whose file has a problem (the one most
likely to need pausing) can be paused. Site and feed warnings are returned as data; each front end only
words and shows them.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import credentials, runner, schedule, sources
from .config import read_yaml, rewrite_key
from .errors import ConfigError
from .history import History

if TYPE_CHECKING:
    from .config import Site
    from .recipe import Feed, RecipeLibrary


def set_enabled(site: Site, name: str, enabled: bool, *, via: str = "cli") -> dict:
    """Pause (``enabled=False``) or resume a feed by rewriting its ``enabled:`` line, keeping comments and
    layout. Works when the file does not load. Returns {name, enabled, changed}."""
    path = site.feeds_dir / f"{name}.yaml"
    if not path.is_file():
        raise ConfigError(f"No feed file {path}")
    try:
        raw = read_yaml(path)
    except ConfigError:
        raw = None                             # the file has a problem: set the line anyway
    current = raw.get("enabled", True) if raw is not None else None
    d = (raw or {}).get("delivery") or {}
    if enabled and isinstance(d, dict) and d.get("transport") == "sftp" and d.get("host_key_confirmed") is False:
        raise ConfigError(f"The key of {d.get('host')} is not confirmed with the vendor yet, so {name} stays paused: "
                          "nothing would log in to the server. Confirm the key first (the page's Delivery tab, or "
                          "`folio-exchange init`).")
    if current is enabled:
        return {"name": name, "enabled": enabled, "changed": False}
    if enabled:
        _resume_checked(site, name, path)
    else:
        rewrite_key(path, "enabled", "false")
    action = "feed resumed" if enabled else "feed paused"
    with History(site.history_path) as hist:
        hist.audit(None, name, action, {"file": str(path), "from": via})
        runner.audit_settings(site, hist)
    return {"name": name, "enabled": enabled, "changed": True}


def _resume_checked(site: Site, name: str, path) -> None:
    """Resume, unless that makes another feed fail to load (a paused feed is not checked for sending the same
    file names to the same place as an enabled one, so the clash only shows once it runs)."""
    from .recipe import RecipeLibrary, load_all

    library = RecipeLibrary(site.recipe_dirs)
    before = set(load_all(site, library)[1])
    old = path.read_bytes()
    rewrite_key(path, "enabled", "true")
    after = load_all(site, library)[1]
    newly = {n: e for n, e in after.items() if n not in before and n != name}
    if newly:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(old)
        tmp.replace(path)
        other, problem = next(iter(newly.items()))
        raise ConfigError(f"{name} stays paused: resuming it would stop feed {other} from loading.\n{problem}")


LAST_DUE_START = "last_due_start"          # history meta: when `run --due` last started (UTC, ISO)
TASK_STALE_AFTER = timedelta(hours=2)
LOCK_STUCK_AFTER = timedelta(hours=4)      # a run holding the site's lock longer than this may be stuck
LAST_PASS = "last-pass.json"              # beside the run lock: when `run --due` last started, and how it ended
LOW_DISK = 1_000_000_000                   # bytes free on the data folder's drive below which check warns


def record_due_start(hist: History, now: datetime) -> None:
    """Note that the scheduled task started a pass (SPEC-6), so a stopped task shows up."""
    hist.set_meta(LAST_DUE_START, now.astimezone(timezone.utc).isoformat(timespec="seconds"))


def last_due_start(hist: History) -> datetime | None:
    """When the scheduled task last started: the recorded start, or else the newest scheduled run (a site
    from before starts were recorded)."""
    raw = hist.get_meta(LAST_DUE_START)
    if not raw:
        raw = next((r["started"] for r in hist.runs(None, 200) if r.get("trigger") == "schedule"), None)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def last_pass_path(lock_path: Path) -> Path:
    return Path(lock_path).with_name(LAST_PASS)


def mark_pass_start(lock_path: Path, now: datetime) -> dict:
    """Note that a scheduled pass started, in a small file beside the run lock, so a pass that cannot write the
    history (a full disk, a file held open) still shows: setting a file's time needs no free space. Returns how
    the previous pass ended ({error, alerted}, or {})."""
    path = last_pass_path(lock_path)
    previous = read_pass(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    os.utime(path, (now.timestamp(), now.timestamp()))
    return previous


def mark_pass_end(lock_path: Path, started: datetime, error: str | None, alerted: bool = False) -> None:
    """Record how the pass that began at ``started`` ended (``error`` None: it did not fail)."""
    path = last_pass_path(lock_path)
    path.write_text(json.dumps({"error": error, "alerted": alerted}), encoding="utf-8")
    os.utime(path, (started.timestamp(), started.timestamp()))


def read_pass(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def last_pass(lock_path: Path) -> tuple[datetime | None, dict]:
    """When the scheduled task last started a pass, from the file beside the run lock, and how that pass ended."""
    path = last_pass_path(lock_path)
    try:
        when = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except (OSError, ValueError):
        return None, {}
    return when, read_pass(path)


def free_bytes(folder: Path) -> int | None:
    """Free space on the drive of ``folder`` (or of its nearest existing parent), or None when unknown."""
    path = Path(folder)
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def ago(delta: timedelta) -> str:
    """'25 minutes', '3 hours', '2 days'."""
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 120:
        n, unit = minutes, "minute"
    elif minutes < 48 * 60:
        n, unit = minutes // 60, "hour"
    else:
        n, unit = minutes // (24 * 60), "day"
    return f"{n} {unit}{'' if n == 1 else 's'}"


def run_started(lock_path: Path) -> datetime | None:
    """When the run that holds the site's lock took it, or None when no run holds it.

    It only looks: taking the lock, even for a moment, could turn away a run starting at that instant. On
    Windows a held lock's byte cannot be read through another handle; on Linux the kernel lists held locks in
    /proc/locks. Elsewhere it cannot tell and says None. A run writes its process ID when it takes the lock, so
    the file's modified time is when it started."""
    path = Path(lock_path)
    try:
        started = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except (OSError, ValueError):
        return None
    return started if _lock_held(path) else None


def _lock_held(path: Path) -> bool:
    if sys.platform == "win32":
        try:
            with open(path, "rb") as fh:
                fh.read(1)
        except PermissionError:          # the locked byte: another handle holds it
            return True
        except OSError:
            return False
        return False
    try:
        st = path.stat()
        with open("/proc/locks", encoding="ascii", errors="replace") as fh:
            locks = fh.read().splitlines()
    except OSError:
        return False
    # "1: FLOCK  ADVISORY  WRITE 2785 08:01:1311759 0 EOF": device major and minor in hex, then the inode.
    me = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    return any(len(parts) > 5 and "->" not in parts and parts[5] == me
               for parts in (line.split() for line in locks))


def task_status(hist: History, feeds: list[Feed], now: datetime, *, lock_path: Path | None = None) -> dict:
    """How the hourly scheduled task is doing: {state: running | stuck | ok | stale | never | unneeded, last,
    text} (and ``since`` while running or stuck).

    It matters only while an enabled feed runs on a schedule; otherwise a stopped task is no warning. While a
    pass holds the site's lock (``lock_path``), Task Scheduler does not start the task again, so a long pass (a
    first full sync can take hours) is "running", not "stale"; after LOCK_STUCK_AFTER it is "stuck", a warning
    worded as the stuck-lock alert. A run that took the lock more than two hours after the task last started is
    not that pass (a manual run while the task is stopped), so it hides nothing."""
    scheduled = any(f.enabled and schedule.parse(f.schedule).kind != "manual" for f in feeds)
    recorded = last_due_start(hist)
    marked, ended = last_pass(lock_path) if lock_path is not None else (None, {})
    last = max((t for t in (recorded, marked) if t is not None), default=None)
    since = run_started(lock_path) if lock_path is not None else None
    if since is not None and (last is None or since - last <= TASK_STALE_AFTER):
        stamp = last.astimezone(timezone.utc).isoformat(timespec="seconds") if last else None
        if scheduled and now - since > LOCK_STUCK_AFTER:
            return {"state": "stuck", "last": stamp, "since": since.isoformat(timespec="seconds"),
                    "text": stuck_text(since, now)}
        text = (f"A run has been going since {since:%Y-%m-%d %H:%M} UTC ({ago(now - since)}); the hourly task "
                "starts again once it finishes. A first sync of a large catalogue can take several hours.")
        return {"state": "running", "last": stamp, "since": since.isoformat(timespec="seconds"), "text": text}
    if last is None:
        state = "never" if scheduled else "unneeded"
        text = ("Scheduled runs have not started yet: no feed runs on its own until the hourly task is "
                "registered. Register it with `folio-exchange schedule --register`.")
        return {"state": state, "last": None, "text": text}
    age = now - last
    stamp = last.astimezone(timezone.utc).isoformat(timespec="seconds")
    if scheduled and age > TASK_STALE_AFTER:
        text = (f"The scheduled task last ran {ago(age)} ago, so no feed has run on its schedule and no alert "
                "could be sent since. Check that this computer is on and awake, that it is not running on battery "
                "(a task registered by an older version never starts on battery power), and that the task is "
                "enabled in Task Scheduler. After a change of Windows password, or to let it run on battery, "
                "register it again with `folio-exchange schedule --register`.")
        return {"state": "stale", "last": stamp, "text": text}
    failing = _failing(recorded, marked, ended)
    if scheduled and failing:
        text = (f"The scheduled task runs (it last started {ago(age)} ago), but its passes fail, so no feed runs on "
                f"its schedule: {failing}. Check that the drive of the site's data folder has free space and that "
                "no other program (a backup or an antivirus scan) holds its files open; logs/folio-exchange.log "
                "has the details.")
        return {"state": "stale", "failing": True, "last": stamp, "text": text}
    return {"state": "ok", "last": stamp, "text": f"The scheduled task last ran {ago(age)} ago."}


def stuck_text(since: datetime, now: datetime) -> str:
    """What the stuck-lock alert, `check`, `feeds` and the page say about a run that has held the lock for hours."""
    return (f"One run has held this site's lock since {since.astimezone():%Y-%m-%d %H:%M} ({ago(now - since)}), "
            "so no other run can start until it ends, and a feed that falls due meanwhile is skipped. "
            "A first full download of a large catalogue can take this long; otherwise the run may be stuck (a "
            "stalled upload, say). Look at logs/folio-exchange.log; if nothing is moving, end that FOLIO-Exchange "
            "process or restart the computer, and the next hourly start catches up.")


def _failing(recorded: datetime | None, marked: datetime | None, ended: dict) -> str | None:
    """Why the task's passes fail, from the file beside the lock: the history has not been written for a while
    (each pass records its start there too), or the last pass ended with an error. None when neither."""
    if marked is None:
        return None
    reasons = []
    if recorded is None or marked - recorded > TASK_STALE_AFTER:
        when = f"since {recorded:%Y-%m-%d %H:%M} UTC" if recorded else "at all"
        reasons.append(f"the run history (data/history.sqlite) has not been written {when}")
    if ended.get("error"):
        reasons.append(f"the last pass ended with {ended['error']}")
    return "; ".join(reasons) or None


def _w(level: str, code: str, text: str, feed: str | None = None) -> dict:
    return {"level": level, "code": code, "text": text, "feed": feed}


def alert_secret_warnings(site: Site) -> list[dict]:
    """A warning for alerts.webhook or alerts.heartbeat whose URL cannot be found. Keyring entries are named
    after the site folder, so renaming or copying the folder loses them, and every alert is then dropped."""
    out = []
    for what, service, lost in (("webhook", credentials.WEBHOOK_SERVICE, "failure alerts are not sent"),
                                ("heartbeat", credentials.HEARTBEAT_SERVICE, "the heartbeat monitor hears nothing")):
        ref = site.alerts.get(what)
        if ref and not credentials.resolve(ref, service, site.name):
            out.append(_w("warning", "alert-secret-missing",
                          f"alerts.{what} is set, but its URL was not found "
                          f"({credentials.source(ref, service, site.name)}), so {lost}. Store it with "
                          f"`folio-exchange secret set {what}`. A URL in the keyring belongs to the site folder's "
                          f"name ({site.name}): after renaming or copying the folder, store it again."))
    return out


def site_warnings(site: Site, library: RecipeLibrary, feeds: list[Feed], errors: dict[str, str], hist: History,
                  now: datetime, *, only: str | None = None) -> list[dict]:
    """Everything a person should know before trusting the site to run on its own, most serious first.

    Each item is {level: problem | warning | note, code, text, feed}. ``only`` limits feed items to one feed."""
    out: list[dict] = []
    for name, err in errors.items():
        if not only or name == only:
            out.append(_w("problem", "feed-file", err, name))
    for problem in library.problems():
        out.append(_w("problem", "recipe-file", problem))
    for text in library.shadow_warnings():
        out.append(_w("warning", "recipe-shadow", text))
    from .permissions import problems as permission_problems

    for text in permission_problems(site):
        out.append(_w("warning", "folder-access", text))
    if not site.alerts.get("webhook") and not site.alerts.get("email"):
        out.append(_w("warning", "no-alerts", "No alert channel is set up, so a failed scheduled run would go "
                      "unnoticed. Add alerts.webhook or alerts.email to site.yaml, then run "
                      "`folio-exchange check --alerts`."))
    elif site.alerts.get("on") == "never":
        out.append(_w("warning", "alerts-never", "alerts.on is never: failed runs send no alert."))
    out.extend(alert_secret_warnings(site))
    task = task_status(hist, feeds, now, lock_path=site.lock_path)
    if task["state"] == "stale":
        out.append(_w("warning", "task-failing" if task.get("failing") else "task-stale", task["text"]))
    elif task["state"] == "stuck":
        out.append(_w("warning", "task-stuck", task["text"]))
    elif task["state"] == "never":
        out.append(_w("note", "task-never", task["text"]))
    free = free_bytes(site.history_path.parent)
    if free is not None and free < LOW_DISK:
        out.append(_w("warning", "low-disk", f"Only {free / 1e9:.1f} GB is free on the drive of "
                      f"{site.history_path.parent}. When it fills up, runs fail and their history cannot be "
                      "written. Free some space, or move the site to a larger drive."))
    if site.folio.get("verify_tls") is False:
        out.append(_w("warning", "tls-off", "folio.verify_tls is false, so FOLIO's certificate is not checked: "
                      "anyone between this computer and FOLIO could read the service account's password and the "
                      "data. If FOLIO's certificate comes from your campus or a network proxy, set folio.ca_bundle "
                      "to that certificate's PEM file (or to windows, to trust the Windows certificate store) and "
                      "set verify_tls back to true."))
    for f in feeds:
        if only and f.name != only:
            continue
        if f.delivery.get("host_key_confirmed") is False:
            out.append(_w("warning", "host-key-unconfirmed",
                          f"The SFTP server key of {f.delivery.get('host')} is not confirmed with the vendor yet, "
                          "so nothing logs in to it. Run `folio-exchange init` again once the vendor confirms it.",
                          f.name))
        if f.delivers and not f.checks.get("min_item_ratio"):
            out.append(_w("warning", "ratio-off", "checks.min_item_ratio is 0, so a sudden drop in items would be "
                          "delivered.", f.name))
        if f.enabled:
            late = runner.overdue(site, f, hist, now)
            if late:
                out.append(_w("warning", "overdue", late[0].upper() + late[1:] + ".", f.name))
        else:
            test = f"Test it with `folio-exchange check --delivery {f.name}`, then" if f.delivers else "Resume it with"
            out.append(_w("note", "paused", f"Paused: scheduled runs skip it. {test} "
                          f"`folio-exchange feeds resume {f.name}`.", f.name))
        local = sources.local_minor_warning(site, library, f)
        if local:
            out.append(_w("warning", "recipe-local-substance", local, f.name))
        newer = library.newer_major(f.uses)
        if newer:
            out.append(_w("note", "recipe-update", f"{newer.ref} is available and waits for review: "
                          f"folio-exchange upgrade {f.name} --to {newer.id}@^{newer.version[0]}.{newer.version[1]}",
                          f.name))
    order = {"problem": 0, "warning": 1, "note": 2}
    return sorted(out, key=lambda w: order[w["level"]])
