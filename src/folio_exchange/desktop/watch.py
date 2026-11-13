"""What the tray shows and tells: a light reading of each watched site, and what is new since the last one.

No Qt here, so it is quick to test. ``look`` reads a site the way the window's Home does (each feed's last run,
a run waiting for review, an overdue schedule, the scheduled task's state) without the settings and history the
window also shows. ``news`` compares two readings: the tray notifies about scheduled runs, which happen while
nobody is watching. A run started by hand is seen where it was started, so it only counts in the first reading,
which lists everything waiting when the tray starts. The scheduled task's state is told apart (``task_news``):
it reads as stopped whenever the computer was off or asleep past an hourly start, so the tray says so only once
it has itself been awake long enough for the task to have run.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import feedops, runner, views
from ..errors import FxError
from ..history import History
from . import model

WAITING = ("held", "blocked")      # a run waiting for a person; a build waits too, but someone asked for it
TASK_WORDS = {"stopped": "Scheduled runs have stopped.", "failing": "Scheduled runs are failing.",
              "stuck": "A run has been going for hours."}


def _run(row: dict | None) -> dict | None:
    if not row:
        return None
    r = views.run_row(row)
    return {k: r.get(k) for k in ("id", "status", "trigger", "started", "error", "not_sent")}


def _last_two(hist: History, feed: str) -> tuple[dict | None, dict | None]:
    rows = [r for r in hist.runs(feed, 20) if r.get("status") != "preview"]
    return (rows[0] if rows else None), (rows[1] if len(rows) > 1 else None)


def look(site_root: Path, now: datetime | None = None) -> dict:
    """{root, name, error, feeds, task, broken}: each feed's name, title, enabled, last real run (with
    ``repeat``: a scheduled retry that failed as the slot's attempt before it did, as ``runner.repeat_failure``
    decides for alerts), a held or blocked run waiting for release, and its overdue warning; the scheduled task's
    state; the feed files that don't load. A site that can't be read has ``error`` (its words) and nothing else."""
    root = Path(site_root)
    out: dict = {"root": str(root), "name": root.name, "error": None, "feeds": [], "task": None, "broken": []}
    try:
        site, _library, feeds, errors = views.load(root)
        now = now or datetime.now(timezone.utc)
        with History(site.history_path) as hist:
            for f in feeds:
                try:
                    waiting = runner.releasable_run(hist, f, None)
                except FxError:
                    waiting = None
                last_row, before = _last_two(hist, f.name)
                last = _run(last_row)
                if last:
                    last["repeat"] = runner.repeat_failure(site, f, last["status"], last["trigger"], last["error"],
                                                           before)
                out["feeds"].append({
                    "name": f.name, "title": f.title, "enabled": f.enabled,
                    "last": last,
                    "waiting": _run(waiting) if waiting and waiting.get("status") in WAITING else None,
                    "overdue": runner.overdue(site, f, hist, now),
                })
            task = feedops.task_status(hist, feeds, now, lock_path=site.lock_path)
        out["task"] = {"state": task.get("state"), "failing": bool(task.get("failing"))}
        out["broken"] = sorted(errors)
        out["name"] = site.name
    except (FxError, OSError, sqlite3.Error, ValueError) as exc:
        out["error"] = model.first_sentence(str(exc)) or type(exc).__name__
    return out


def task_problem(task: dict | None) -> str | None:
    """"stopped", "failing" or "stuck" when the scheduled task needs a person, else None."""
    task = task or {}
    if task.get("state") == "stuck":
        return "stuck"
    if task.get("state") == "stale":
        return "failing" if task.get("failing") else "stopped"
    return None


def _waiting_words(name: str, run: dict) -> str:
    return f"{name} is held for your review." if run.get("status") == "held" else f"{name} was blocked. Nothing was sent."


def needs(snap: dict, *, task: bool = True, quiet: bool = False) -> list[str]:
    """Everything at a site that needs a person now, one sentence each. ``task``: include the scheduled task's
    problem; ``quiet``: but not "stopped" (see ``task_news``)."""
    if snap.get("error"):
        return [f"Couldn't read the site. {snap['error']}"]
    out = []
    for f in snap.get("feeds") or []:
        if f.get("waiting"):
            out.append(_waiting_words(f["name"], f["waiting"]))
        elif (f.get("last") or {}).get("status") == "failed":
            out.append(f"{f['name']}'s last run failed.")
        if f.get("overdue"):
            out.append(f"{f['name']} hasn't run on schedule.")
    problem = task_problem(snap.get("task"))
    if task and problem and not (quiet and problem == "stopped"):
        out.append(TASK_WORDS[problem])
    broken = snap.get("broken") or []
    if broken:
        out.append(f"{len(broken)} feed file{'s' if len(broken) != 1 else ''} can't be loaded.")
    return out


def news(before: dict | None, after: dict) -> list[str]:
    """What to tell the person about one site's feeds: everything that needs them on the first reading
    (``before`` is None) or when the site can be read again; afterwards only what is new. New means a scheduled
    run held, blocked or failed (a retry that failed as the slot's attempt before it did is not new, as with
    alerts), a feed that has just become overdue, and the site just becoming unreadable. The scheduled task's
    state is ``task_news``'s."""
    if before is None or (before.get("error") and not after.get("error")):
        return needs(after, task=False)
    if after.get("error"):
        return [] if before.get("error") else needs(after, task=False)
    out = []
    old = {f["name"]: f for f in before.get("feeds") or []}
    for f in after.get("feeds") or []:
        was = old.get(f["name"]) or {}
        waiting, last, was_last = f.get("waiting"), f.get("last") or {}, was.get("last") or {}
        if waiting and waiting.get("trigger") == "schedule" and waiting.get("id") != (was.get("waiting") or {}).get("id"):
            out.append(_waiting_words(f["name"], waiting))
        if (last.get("status") == "failed" and last.get("trigger") == "schedule" and last.get("id") != was_last.get("id")
                and not last.get("repeat")):
            out.append(f"{f['name']}'s scheduled run failed.")
        if f.get("overdue") and not was.get("overdue"):
            out.append(f"{f['name']} hasn't run on schedule.")
    return out


def task_news(snap: dict, told: str | None, quiet: bool) -> tuple[list[str], str | None]:
    """What to tell about a site's scheduled task, and the problem told so far (keep it for the next call).
    Each problem is told once, until it clears. "stopped" waits while ``quiet``: the tray hasn't been awake long
    enough for the task to have had its hourly start, since a computer that was off or asleep looks the same."""
    problem = None if snap.get("error") else task_problem(snap.get("task"))
    if problem is None:
        return [], None
    if problem == told or (quiet and problem == "stopped"):
        return [], told
    return [TASK_WORDS[problem]], problem


def feed_line(f: dict) -> str:
    """A feed's line in the tray's menu: its name and state in words."""
    last = f.get("last") or {}
    if f.get("waiting"):
        state = model.status_words(f["waiting"]["status"])
    elif last.get("status") == "failed":
        state = "Last run failed"
    elif not f.get("enabled"):
        state = "Paused"
    elif last:
        state = f"{model.status_words(last.get('status'), last.get('not_sent'))}, {model.when(last.get('started'))}"
    else:
        state = "No runs yet"
    if f.get("overdue"):
        state += ", overdue"
    return f"{f['name']}: {state}"


def count(snaps: list[dict], quiet: bool = False) -> int:
    return sum(len(needs(s, quiet=quiet)) for s in snaps)


def tooltip(snaps: list[dict], quiet: bool = False) -> str:
    """The tray icon's tooltip, short enough for Windows (at most 127 characters)."""
    if not snaps:
        return "FOLIO-Exchange"
    n = count(snaps, quiet)
    text = (f"FOLIO-Exchange: {n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} you" if n else
            "FOLIO-Exchange: nothing needs you right now")
    return text[:127]
