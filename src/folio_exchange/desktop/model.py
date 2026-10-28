"""What the window shows about a site, read in one go and put into words. No Qt here.

``snapshot`` runs off the window's thread (reading history and feed files can take a moment on a slow drive),
and the window only lays out what it returns. The facts come from ``views`` and ``setupops``, as on the page.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .. import setupops, views
from ..history import History

STATUS_WORDS = {"success": "Delivered", "held": "Held for review", "blocked": "Blocked", "failed": "Failed",
                "built": "Built, not sent", "preview": "Preview"}
LEVEL_WORDS = {"problem": "Problem", "warning": "Warning", "note": "Note"}
FILE_CODES = ("feed-file", "recipe-file")       # listed with the feed files, not again with the warnings


def snapshot(site_root: Path) -> dict:
    """{root, state, summary, feeds}: ``state`` from setupops.site_state; while the site is ready, ``summary``
    from views.site_summary and ``feeds`` (each loaded feed's views.feed_detail, by name). Raises what they
    raise; the window shows it."""
    root = Path(site_root)
    state = setupops.site_state(root)
    out: dict = {"root": str(root), "state": state, "summary": None, "feeds": {}}
    if not state["ready"]:
        return out
    out["summary"] = views.site_summary(root)
    site, library, feeds, _errors = views.load(root)
    with History(site.history_path) as hist:
        out["feeds"] = {f.name: views.feed_detail(site, library, f, hist) for f in feeds}
    return out


def when(iso: str | None) -> str:
    """A stored time in this computer's time zone, as people read it."""
    if not iso:
        return "never"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%a %d %b %Y, %H:%M")
    except ValueError:
        return iso


def status_words(status: str | None) -> str:
    return STATUS_WORDS.get(status or "", status or "No runs yet")


def password_words(source: str | None) -> str:
    """Where the FOLIO password comes from, never its value (as the page words it)."""
    s = str(source or "")
    if not s or s.startswith("NOT FOUND"):
        return "Not stored yet"
    if s.startswith("system keyring"):
        return "Stored in Windows Credential Manager" + s[len("system keyring"):]
    if s.startswith("environment (") and s.endswith(")"):
        return f"From the environment variable {s[len('environment ('):-1]}"
    return s


def feed_state(item: dict) -> str:
    """A feed's line in the sidebar: on or paused, and its last real run. In words, not only colour. "Running"
    is kept for an action in progress, as the page keeps "Busy"."""
    state = "On" if item.get("enabled") else "Paused"
    last = item.get("last_status") or (item.get("last_run") or {}).get("status")
    return f"{state}, {status_words(last).lower() if last else 'no runs yet'}"


def warnings(summary: dict) -> list[str]:
    """The site's warnings as lines that say their level in words."""
    out = []
    for w in summary.get("warnings") or []:
        if w.get("code") in FILE_CODES:
            continue
        who = f"{w['feed']}: " if w.get("feed") else ""
        out.append(f"{LEVEL_WORDS.get(w.get('level'), 'Note')}: {who}{w.get('text', '')}")
    return out


def file_problems(summary: dict) -> list[str]:
    """Feed files and recipe files that do not load."""
    out = [f"{name}: {err}" for name, err in (summary.get("problems") or {}).items()]
    return out + list(summary.get("recipe_problems") or [])
