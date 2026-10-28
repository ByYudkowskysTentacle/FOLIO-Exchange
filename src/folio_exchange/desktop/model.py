"""What the window shows about a site, read in one go and put into words. No Qt here.

``snapshot`` runs off the window's thread (reading history and feed files can take a moment on a slow drive),
and the window only lays out what it returns. The facts come from ``views`` and ``setupops``, as on the page,
and the words follow the page's (``web/app.js``), naming the app's own buttons where the page names its own.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import setupops, views
from ..errors import FxError
from ..history import History

STATUS_WORDS = {"success": "Delivered", "held": "Held for review", "blocked": "Blocked", "failed": "Failed",
                "built": "Built, not sent", "preview": "Preview"}
LEVEL_WORDS = {"problem": "Problem", "warning": "Warning", "note": "Note"}
FILE_CODES = ("feed-file", "recipe-file")       # listed with the feed files, not again with the warnings
RUNS_SHOWN = 50                                  # a feed's History tab, as on the page
ACTIVITY_SHOWN = 100                             # the Activity view's runs and changes (the page shows 100 changes)
KIND_WORDS = {"preview": "preview", "run": "run", "build": "build", "release": "send",
              "test-delivery": "delivery test", "check": "connection check"}
DOING_WORDS = {"preview": "Previewing", "run": "Running", "build": "Building", "release": "Sending"}
ERRAND_WORDS = {"release-summary": "Looking at what a release would send", "enabled": "Pausing or resuming a feed",
                "run": "Opening a run", "rows": "Reading a run's rows", "folder": "Opening a run's folder"}
DONE_WORDS = {"success": "Delivered.", "held": "Held for your review.", "blocked": "Blocked: see the summary.",
              "failed": "The run failed: see the summary.", "built": "Built. Nothing was sent.",
              "preview": "Preview ready."}
SETTING_NAMES = {
    "title": "Title", "schedule": "Schedule", "file_prefix": "File name prefix",
    "exclude_location_codes": "Locations left out", "exclude_item_statuses": "Item statuses left out",
    "include_location_codes": "Only these locations",
    "include_suppressed_instances": "Include instances suppressed from discovery",
    "include_suppressed_items": "Include items suppressed from discovery",
    "include_suppressed_holdings": "Include holdings suppressed from discovery",
    "include_items_without_barcode": "Include items without a barcode",
    "bibs_without_items": "Include bibs that have no items",
    "min_item_ratio": "Block a delivery if the item count falls below",
    "circ_count_note_type": "Loan count from the old system",
    "created_date_note_type": "Date added in the old system",
    "last_circ_date_note_type": "Last loan date in the old system",
}
DELIMITERS = {"|": "pipe |", ",": "comma", "\t": "tab"}
# A sentence that names a command the app has a button for (run, preview, build, release, history, pause and
# resume, the connection check) is left out, as the page leaves it out; the others are the only remedy.
APP_HAS_IT = re.compile(r"`?folio-exchange (?:run|preview|build|release|history|feeds|check(?! --))\b")


def snapshot(site_root: Path) -> dict:
    """{root, state, summary, feeds, runs, activity, settings}: ``state`` from setupops.site_state; while the
    site is ready, ``summary`` from views.site_summary, ``feeds`` (each loaded feed's views.feed_detail, by
    name), ``runs`` (each feed's latest runs, previews included, newest first), ``activity`` (the latest runs
    of every feed, and the audit log, newest first) and ``settings`` (each feed's setupops.feed_settings, which
    names where a secret comes from but never holds one; {error} when it does not load). Raises what they
    raise; the window shows it."""
    root = Path(site_root)
    state = setupops.site_state(root)
    out: dict = {"root": str(root), "state": state, "summary": None, "feeds": {}, "runs": {},
                 "activity": {"runs": [], "audit": []}, "settings": {}}
    if not state["ready"]:
        return out
    out["summary"] = views.site_summary(root)
    site, library, feeds, _errors = views.load(root)
    with History(site.history_path) as hist:
        out["feeds"] = {f.name: views.feed_detail(site, library, f, hist) for f in feeds}
        out["runs"] = {f.name: [views.run_row(r) for r in hist.runs(f.name, RUNS_SHOWN)] for f in feeds}
        out["activity"] = {"runs": [views.run_row(r) for r in hist.runs(None, ACTIVITY_SHOWN)],
                           "audit": hist.audit_log(ACTIVITY_SHOWN)}
    for f in feeds:
        try:
            out["settings"][f.name] = setupops.feed_settings(site, library, f.name)
        except (FxError, LookupError, OSError, ValueError) as exc:
            out["settings"][f.name] = {"error": str(exc)}
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
        newer = next((f.get("newer_recipe") for f in summary.get("feeds") or [] if f.get("name") == w.get("feed")),
                     None)
        out.append(f"{LEVEL_WORDS.get(w.get('level'), 'Note')}: {who}{warning_text(w, False, newer)}")
    return out


def file_problems(summary: dict) -> list[str]:
    """Feed files and recipe files that do not load."""
    out = [f"{name}: {err}" for name, err in (summary.get("problems") or {}).items()]
    return out + list(summary.get("recipe_problems") or [])


# ------------------------------------------------------------ the page's words, for the app
def page_words(text: str | None) -> str:
    """Text from the command-line side, less the sentences that name a command the app has a button for."""
    if not text:
        return ""
    kept = [x for x in re.split(r"(?<=\.)\s+", str(text)) if not APP_HAS_IT.search(x)]
    return " ".join(kept) if kept else str(text)


def job_words(kind: str, feed: str | None) -> str:
    """A running action as the page names it: "run of chq", "send of chq", "connection check"."""
    what = KIND_WORDS.get(kind, kind)
    return f"{what} of {feed}" if feed else what


def num(n: Any) -> str:
    return "–" if n is None else f"{n:,}" if isinstance(n, int) else str(n)


def size(n: int | None) -> str:
    if n is None:
        return ""
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.1f} KB" if n < 1048576 else f"{n / 1048576:.1f} MB"


def next_step(status: str | None, delivers: bool) -> str | None:
    """What to do about a run with this status."""
    if status == "blocked":
        return ("Look at the files (Open folder). If they are right, use Review and send…; if not, fix the records "
                "in FOLIO and Run again.")
    if status == "held" or (status == "built" and delivers):
        return "Look at the files (Open folder), then use Review and send… if they are right."
    if status == "built":
        return "This feed has nowhere to send files yet, so they stay on this computer."
    if status == "failed":
        return "Fix the problem above, then Run again."
    return None


def warning_text(w: dict, on_feed_page: bool = False, newer: str | None = None) -> str:
    """A site warning in the page's words."""
    code = w.get("code")
    if code == "no-alerts":
        return ("No alerts are set up, so nobody would hear about a failed scheduled run. Whoever looks after this "
                "site can add an alert webhook or email address in site.yaml.")
    if code == "paused":
        return ("This feed is paused: scheduled runs skip it. You can still preview and run it here; Resume starts "
                "the schedule again." if on_feed_page else
                "Paused: scheduled runs skip it. You can still preview and run it on its page, and resume it there.")
    if code == "recipe-update":
        return (f"A newer version of its recipe{f' ({newer})' if newer else ''} is available. It changes what the "
                "vendor receives, so the feed stays on its current version until someone reviews the change.")
    if code == "overdue":
        first = str(w.get("text") or "").split(";")[0].rstrip(".")
        return f"{first}. Check that the task that starts scheduled runs on this computer is still set up and running."
    if code == "alerts-never":
        return "Alerts are switched off (alerts.on is never in site.yaml), so a failed run sends no alert."
    if code == "ratio-off":
        return "The item count check is off, so a sudden drop in items would be sent without a hold for review."
    if code == "host-key-unconfirmed":
        return ("The vendor's SFTP server key is not confirmed yet, so nothing is sent there. Once the vendor gives "
                "you the fingerprint, enter it under Change delivery on the feed's page in the browser page.")
    return page_words(w.get("text"))


def setting_name(key: str) -> str:
    if key in SETTING_NAMES:
        return SETTING_NAMES[key]
    s = str(key).replace("_", " ")
    return s[:1].upper() + s[1:]


def readable(v: Any) -> str:
    """A setting's value in words, as the page shows it."""
    if v is True:
        return "yes"
    if v is False:
        return "no"
    if v is None or v == "":
        return "not set"
    if isinstance(v, list):
        if not v:
            return "none"
        return "; ".join(readable(x) for x in v) if any(isinstance(x, dict) for x in v) else ", ".join(map(str, v))
    if isinstance(v, dict):
        return "; ".join(f"{setting_name(k)}: {readable(x)}" for k, x in v.items()) if v else "none"
    return str(v)


def setting_lines(values: dict) -> list[str]:
    """Settings as a person reads them: a plain name, the key as it is in the file, the value in words."""
    return [f"{setting_name(k)}{f' ({k})' if '_' in k else ''}: {readable(v)}" for k, v in values.items()]


def file_name(detail: dict, output: dict, ext: str | None) -> str:
    name = str(output.get("file_name") or output["name"])
    return f"{name.replace('{prefix}', str(detail.get('file_prefix') or '')).replace('{date}', 'YYYYMMDD')}.{ext}"


def layout_about(detail: dict, o: dict) -> tuple[str, str]:
    """(title, what it is) for one of a feed's files, as the page's Layout tab words it."""
    if o.get("format") != "delimited":
        itf = o.get("item_field") or {}
        subs = ", ".join(f"${c} {f}" for c, f in (itf.get("subfields") or {}).items())
        about = ("One record per instance; 001 = instance HRID; 999 ff $i = instance UUID"
                 + (f", a new file every {num(o['split_every'])} records." if o.get("split_every") else ".")
                 + (f" Items embedded in {itf.get('tag')}: {subs}." if o.get("embed_items") else
                    f" No item fields; any {itf.get('tag')} is removed."))
        return f"{o['name']} · MARC 21 · {file_name(detail, o, 'mrc')}", about
    delim = DELIMITERS.get(o.get("delimiter"), o.get("delimiter"))
    return (f"{o['name']} · {file_name(detail, o, o.get('extension'))}",
            f"{delim}-separated · header {'yes' if o.get('header') else 'no'} · empty values "
            f"“{o.get('empty_value') or ''}” · dates {o.get('date_format')}")


def last_run_words(last: dict | None) -> str:
    if not last:
        return "never"
    return (f"{status_words(last['status'])}, {when(last.get('started'))} · {num(last.get('items'))} items, "
            f"{num(last.get('bibs'))} bibs")


def run_note(r: dict) -> str:
    """The History table's last column: who released the run, else why it failed or was held (shortened)."""
    if r.get("released_by"):
        return f"released by {r['released_by']}"
    return page_words(r.get("error") or r.get("held"))[:90]


def run_summary(run: dict, report: dict, delivers: bool) -> list[tuple[str, str]]:
    """The facts of one run (the page's Summary tab), each a (label, value)."""
    b = report.get("build") or {}
    skipped = ", ".join(f"{k}: {num(v)}" for k, v in (b.get("items_skipped") or {}).items())
    step = next_step(run.get("status"), delivers)
    rows: list[tuple[str, str] | None] = [
        ("Status", status_words(run.get("status"))),
        ("Started", when(run.get("started"))),
        ("Recipe", str(run.get("recipe") or "")),
        ("Held because", page_words(report["held"])) if report.get("held") else None,
        ("Problem", page_words(run["error"])) if run.get("error") else None,
        ("Released despite", page_words(run["released_after"])) if run.get("released_after") else None,
        ("Released by", str(run["released_by"])) if run.get("released_by") else None,
        ("Next step", step) if step else None,
        ("Items left out", skipped or "none"),
        ("Items without barcode", num(b["items_without_barcode"])) if b.get("items_without_barcode") else None,
        ("Duplicate barcodes", num(b["duplicate_barcodes"])) if b.get("duplicate_barcodes") else None,
        ("Bibs", f"{num(b.get('bibs_written'))} ({num(b.get('bibs_from_srs'))} from MARC, "
                 f"{num(b.get('bibs_generated'))} generated)") if "bibs_written" in b else None,
        ("Folder", str(run.get("folder") or "")),
    ]
    return [r for r in rows if r]


def release_rows(s: dict) -> list[tuple[str, str]]:
    """What a release would send, for the person to confirm (the page's "Send these files?"), first naming the
    feed: the question can come a moment after it was asked for, so it says whose files it sends."""
    last = s.get("last_delivered_items")
    items = (f"{num(s.get('items'))} (last delivery {num(last)}, {when(s.get('last_delivered'))})" if last
             else f"{num(s.get('items'))} (nothing delivered there before)")
    return [("Feed", str(s.get("feed") or "")), ("Run", f"{when(s.get('built'))}, {s.get('recipe')}"),
            ("Waiting because", page_words(s.get("reason")) or "it was built without sending"),
            ("Items", items), ("Bibs", num(s.get("bibs"))), ("Sends to", str(s.get("destination") or ""))]


def delivery_rows(d: dict) -> list[tuple[str, str]]:
    """Where a feed's files go (its Delivery tab), from setupops.feed_settings' delivery, which holds no secret."""
    if d.get("transport") == "sftp":
        where = password_words(d.get("password"))
        if d.get("host_key_fingerprint"):
            key = f"{d['host_key_fingerprint']}. " + (
                "Confirmed: it matches the fingerprint the vendor gave." if d.get("host_key_confirmed") else
                "Not confirmed yet: nothing logs in or sends until you enter the vendor's fingerprint under Change "
                "delivery on the feed's page in the browser page.")
        else:
            key = "not read yet"
        login = ("With a private key, kept in this site's keys folder (only this Windows user can read it)"
                 if d.get("auth") == "key" else
                 "No password or key is stored yet, so nothing can be sent." if where == "Not stored yet" else
                 f"With a password. {where}.")
        return [("Sends to", "The vendor's SFTP server"), ("Server", f"{d.get('host')}:{d.get('port') or 22}"),
                ("Folder on the server", d.get("remote_dir") or "."), ("User name", d.get("username") or ""),
                ("Server key", key), ("Login", login)]
    if d.get("transport") == "local":
        return [("Sends to", "A folder on this computer or the network"), ("Folder", d.get("directory") or "")]
    return [("Sends to", "Nowhere yet. This feed is a dry run: Preview and Run build its files on this computer "
                         "and send nothing.")]


def delivery_check_words(state: dict) -> str:
    """A delivery test's outcome (its job's final as_dict), as the page words it."""
    if state.get("status") != "done":
        return f"The test failed. {page_words(state.get('error')) or 'No reason was given.'}"
    r = state.get("result") or {}
    n = r.get("entries")
    listing = ("The server does not let this account list the folder, which is usual for upload-only accounts."
               if n is None else f"The folder holds {num(n)} {'entry' if n == 1 else 'entries'}.")
    return f"It works. Reached {r.get('where')}. {listing}"


def detail_text(detail: Any) -> str:
    """An audit entry's details in one line."""
    if not isinstance(detail, dict):
        return str(detail or "")
    parts = []
    for k, v in detail.items():
        val = ", ".join(map(str, v)) if isinstance(v, list) else json.dumps(v) if isinstance(v, dict) else str(v)
        parts.append(f"{setting_name(k)}: {val}")
    return " · ".join(parts)


def attention(snap: dict) -> list[tuple[str, str]]:
    """(feed, what) for each feed that needs a person: a run waiting to be released, a last run that failed or
    was blocked, or a schedule that has not run it."""
    out = []
    for name, d in (snap.get("feeds") or {}).items():
        waiting, last = d.get("waiting"), d.get("last_run")
        if waiting:
            why = page_words(waiting.get("held") or waiting.get("error")) or (
                "built without sending" if waiting.get("status") == "built" else "")
            out.append((name, f"{name}: {status_words(waiting['status'])}. The run of "
                              f"{when(waiting.get('started'))} is waiting for you{f': {why}' if why else '.'}"))
        elif last and last.get("status") in ("failed", "blocked"):
            what = "failed" if last["status"] == "failed" else "was blocked"
            out.append((name, f"{name}: the last run {what} ({when(last.get('started'))}). "
                              f"{page_words(last.get('error'))}".rstrip()))
        if d.get("overdue"):                       # as the site's warning words it, without the command it names
            late = str(d["overdue"])
            out.append((name, f"{name}: {warning_text({'code': 'overdue', 'text': late[:1].upper() + late[1:]})}"))
    return out
