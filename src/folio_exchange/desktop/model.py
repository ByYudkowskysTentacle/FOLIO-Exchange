"""What the window shows about a site, read in one go and put into words. No Qt here.

``snapshot`` runs off the window's thread (reading history and feed files can take a moment on a slow drive),
and the window only lays out what it returns. The facts come from ``views`` and ``setupops``, as on the page,
and the words follow the page's (``web/app.js``), naming the app's own buttons where the page names its own.
"""
from __future__ import annotations

import json
import platform
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
              "test-delivery": "delivery test", "check": "connection check",
              "connect": "save of the FOLIO connection", "add-feed": "feed setup", "settings": "save of a feed's settings",
              "delivery": "save of a feed's delivery", "password": "save of an SFTP password",
              "private-key": "save of an SFTP key", "alerts": "save of the alert settings",
              "test-alert": "test alert", "recipe-install": "recipe install"}
DOING_WORDS = {"preview": "Previewing", "run": "Running", "build": "Building", "release": "Sending"}
ERRAND_WORDS = {"release-summary": "Looking at what a release would send", "enabled": "Pausing or resuming a feed",
                "run": "Opening a run", "rows": "Reading a run's rows", "folder": "Opening a run's folder",
                "recipes": "Reading the recipes", "choices": "Reading FOLIO's locations and note types",
                "permissions": "Checking the FOLIO account's permissions", "feed-settings": "Reading a feed's settings",
                "recipe-check": "Checking a recipe source", "rename": "Renaming the site folder",
                "register": "Registering the scheduled task", "task": "Looking for the scheduled task",
                "alert-settings": "Reading the alert settings"}
# The new-feed wizard's and settings form's schedules (the page's SCHEDULES): the value, and how it is offered.
SCHEDULES = (("Sun 03:30", "Weekly, on Sunday at 03:30"), ("daily 03:30", "Daily at 03:30"),
             ("monthly 1 03:30", "Monthly, on the 1st at 03:30"), ("manual", "Only when run by hand"))
FILTER_LISTS = ("exclude_location_codes", "exclude_item_statuses", "include_location_codes")
FILTER_FLAGS = ("include_suppressed_instances", "include_suppressed_items", "include_suppressed_holdings",
                "include_items_without_barcode", "bibs_without_items")
LEGACY_KEYS = ("circ_count_note_type", "created_date_note_type", "last_circ_date_note_type")
LEGACY_HELP = {
    "circ_count_note_type": "The item note type holding each item's loan count from the old system. It's added to "
                            "FOLIO's own count.",
    "created_date_note_type": "The item note type holding the date each item was added in the old system.",
    "last_circ_date_note_type": "The item note type holding each item's last loan date in the old system.",
}
MISSING_LOCATION = "not a location in this FOLIO"
MISSING_STATUS = "not a FOLIO item status"
ALERT_ON = (("failure", "When a run fails, is blocked or is held for review"),
            ("always", "After every run"), ("never", "Never"))
DONE_WORDS = {"success": "Delivered.", "held": "Held for your review.", "blocked": "Blocked. See the Summary tab.",
              "failed": "The run failed. See the Summary tab.", "built": "Built. Nothing was sent.",
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
    site, library, feeds, errors = views.load(root)
    with History(site.history_path) as hist:
        out["feeds"] = {f.name: views.feed_detail(site, library, f, hist) for f in feeds}
        out["runs"] = {f.name: [views.run_row(r) for r in hist.runs(f.name, RUNS_SHOWN)] for f in feeds}
        out["activity"] = {"runs": [views.run_row(r) for r in hist.runs(None, ACTIVITY_SHOWN)],
                           "audit": hist.audit_log(ACTIVITY_SHOWN)}
    for f in feeds:
        try:     # the feeds loaded once above: loading them all again for each feed made a refresh O(n²)
            out["settings"][f.name] = setupops.feed_settings(site, library, f.name, loaded=(feeds, errors))
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
        who = f" for {w['feed']}" if w.get("feed") else ""
        newer = next((f.get("newer_recipe") for f in summary.get("feeds") or [] if f.get("name") == w.get("feed")),
                     None)
        out.append(f"{LEVEL_WORDS.get(w.get('level'), 'Note')}{who}: {warning_text(w, False, newer)}")
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
        return ("Look at the files (Open folder). Send them with Review and send… if they're right. If not, fix the "
                "records in FOLIO and Run again.")
    if status == "held" or (status == "built" and delivers):
        return "Look at the files (Open folder). Send them with Review and send… if they're right."
    if status == "built":
        return "The files stay on this computer. This feed has no delivery yet."
    if status == "failed":
        return "Fix the problem above, then Run again."
    return None


def warning_text(w: dict, on_feed_page: bool = False, newer: str | None = None) -> str:
    """A site warning in the page's words."""
    code = w.get("code")
    if code == "no-alerts":
        return ("No alerts are set up, so nobody hears about a failed scheduled run. Add an email address or chat "
                "webhook with Change alerts… under Options.")
    if code == "paused":
        return ("This feed is paused. Resume it to run on its schedule." if on_feed_page else
                "This feed is paused. Scheduled runs skip it until you resume it on the feed's page.")
    if code == "recipe-update":
        return (f"A newer version of its recipe{f' ({newer})' if newer else ''} is available. It changes what the "
                "vendor gets, so the feed keeps its current version until someone reviews the change.")
    if code == "overdue":
        first = str(w.get("text") or "").split(";")[0].rstrip(".")
        return f"{first}. Check that the scheduled task on this computer is still set up and running."
    if code == "alerts-never":
        return "Alerts are turned off. Turn them on with Change alerts… under Options."
    if code == "ratio-off":
        return "The item count check is off. A sudden drop in items would be sent without being blocked for review."
    if code == "host-key-unconfirmed":
        return ("The vendor's SFTP server key isn't confirmed yet, so nothing is sent. Enter the vendor's fingerprint "
                "under Change delivery… on the feed's Delivery tab.")
    if code == "task-never":
        return task_words({"state": "never", "text": w.get("text")})
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
                "Confirmed. It matches the vendor's fingerprint." if d.get("host_key_confirmed") else
                "Not confirmed yet. Enter the vendor's fingerprint under Change delivery… before the feed can "
                "connect.")
        else:
            key = "not read yet"
        login = ("With a private key in this site's keys folder. Only this Windows user can read it."
                 if d.get("auth") == "key" else
                 "No password or key is stored yet, so nothing can be sent." if where == "Not stored yet" else
                 f"With a password. {where}.")
        return [("Sends to", "The vendor's SFTP server"), ("Server", f"{d.get('host')}:{d.get('port') or 22}"),
                ("Folder on the server", d.get("remote_dir") or "."), ("User name", d.get("username") or ""),
                ("Server key", key), ("Login", login)]
    if d.get("transport") == "local":
        return [("Sends to", "A folder on this computer or the network"), ("Folder", d.get("directory") or "")]
    return [("Sends to", "Nowhere yet. This feed is a dry run. Preview and Run build its files on this computer "
                         "and send nothing.")]


def delivery_check_words(state: dict) -> str:
    """A delivery test's outcome (its job's final as_dict), as the page words it."""
    if state.get("status") != "done":
        return f"The test failed. {page_words(state.get('error')) or 'No reason was given.'}"
    r = state.get("result") or {}
    n = r.get("entries")
    listing = ("The server doesn't let this account list the folder. That's normal for upload-only accounts."
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
                              f"{when(waiting.get('started'))} is waiting for you{f' ({why}).' if why else '.'}"))
        elif last and last.get("status") in ("failed", "blocked"):
            what = "failed" if last["status"] == "failed" else "was blocked"
            out.append((name, f"{name}: The last run {what} ({when(last.get('started'))}). "
                              f"{page_words(last.get('error'))}".rstrip()))
        if d.get("overdue"):                       # as the site's warning words it, without the command it names
            late = str(d["overdue"])
            out.append((name, f"{name}: {warning_text({'code': 'overdue', 'text': late[:1].upper() + late[1:]})}"))
    return out


# ------------------------------------------------------------ setup, recipes and options (D3), as the page words them
def task_words(task: dict | None, windows: bool | None = None) -> str:
    """The scheduled task's state (feedops.task_status) in the app's words: the command it names is the Options
    page's "Register the scheduled task…" button on Windows, and elsewhere (where there is no such button) the
    crontab line Options shows. ``windows``: this computer's, unless given. A task that has never run ("never",
    or "unneeded" while no feed has a schedule) is worded here rather than in the core's words."""
    text = str((task or {}).get("text") or "")
    if not text:
        return "No feed runs on a schedule yet."
    if windows is None:
        windows = platform.system() == "Windows"
    how = ("Use Register the scheduled task… under Options." if windows else
           "Add the line shown under Options to this computer's scheduler, with crontab -e.")
    if (task or {}).get("state") in ("never", "unneeded"):
        return f"Scheduled runs haven't started yet. No feed runs on its own until the hourly task is registered. {how}"
    parts = re.split(r"(?<=\.)\s+", text)
    kept = [x for x in parts if "schedule --register" not in x]
    if len(kept) == len(parts):
        return text
    return " ".join([*kept, how])


def free_name(base: str | None, taken: list[str]) -> str:
    """A feed name that is free: the recipe's own (chq), else chq-2, chq-3 and so on (the page's freeName)."""
    clean = re.sub(r"[^a-z0-9-]+", "-", str(base or "feed").lower()).strip("-") or "feed"
    if clean not in taken and not setupops.feed_name_problem(clean):
        return clean
    n = 2
    while f"{clean}-{n}" in taken:
        n += 1
    return f"{clean}-{n}"


def feed_name_problem(name: str, taken: list[str]) -> str | None:
    if not name:
        return "Enter a name for the feed."
    if name in taken:
        return f"There's already a feed called {name}. Choose another name."
    return setupops.feed_name_problem(name)


def folder_problem(text: str) -> str | None:
    """A local delivery folder must be given in full (the page's folderProblem)."""
    t = str(text or "").strip()
    if not t:
        return "Enter the folder to copy the files into."
    if re.match(r"^[A-Za-z]:[\\/]", t) or re.match(r"^\\\\[^\\]+\\[^\\]+", t) or t.startswith(("/", "~")):
        return None
    return "Enter the folder's full path, such as D:\\Exports\\chq or \\\\server\\share\\chq."


def fingerprint_problem(text: str) -> str | None:
    """Whether a value could be a server key fingerprint at all (the server's own key is compared on saving)."""
    t = re.sub(r"\s+", "", str(text or ""))
    if not t:
        return None
    if re.fullmatch(r"(?i)(?:MD5:)?(?:[0-9a-f]{2}:){15}[0-9a-f]{2}", t):
        return None
    if re.fullmatch(r"(?i)(?:SHA256:)?[A-Za-z0-9+/]{20,}={0,2}", t):
        return None
    return ("That doesn't look like a server key fingerprint. It starts with SHA256: or looks like 3f:a2:…:9c. Copy it "
            "exactly as the vendor gave it.")


def schedule_words(text: str) -> str:
    return next((words for value, words in SCHEDULES if value == text), text)


def schedule_preset(text: str | None) -> str | None:
    t = " ".join(str(text or "").split()).lower()
    return next((value for value, _words in SCHEDULES if value.lower() == t), None)


def list_text(values: list | None) -> str:
    return ", ".join(values) if values else "none"


def recipe_lists(selection: dict | None) -> dict[str, list[str]]:
    """The three list filters as a recipe sets them by default."""
    s = selection or {}
    return {k: list(s.get(k) or []) for k in FILTER_LISTS}


def left_out_text(values: list[str], default: list[str] | None) -> str:
    """A list for the wizard's summary, saying when it is simply the recipe's default."""
    same = bool(default) and sorted(values) == sorted(default or [])
    return list_text(values) + (" (the recipe's default)" if same else "")


def settings_base(st: dict) -> dict:
    """What a settings form starts from: a feed's setupops.feed_settings, in the form's shape."""
    return {"title": st.get("title") or "", "schedule": st.get("schedule") or "",
            "file_prefix": st.get("file_prefix") or "", "filters": dict(st.get("filters") or {}),
            "checks": dict(st.get("checks") or {}), "legacy": dict(st.get("legacy") or {})}


def overlaid(base: dict, values: dict | None) -> dict:
    """A form's changes laid over the settings as they are now (after the file changed underneath the form)."""
    out = json.loads(json.dumps(base))
    for k in ("title", "schedule", "file_prefix"):
        if values and k in values:
            out[k] = values[k]
    for k in ("filters", "checks", "legacy"):
        out[k].update((values or {}).get(k) or {})
    return out


def percent(ratio: float | None) -> str:
    return f"{float(ratio) * 100:.1f}".rstrip("0").rstrip(".") + "% of the last one" if ratio else "off"


def settings_changes(base: dict, form: dict) -> tuple[dict, list[tuple[str, str, str]]]:
    """What a settings form changes (the page's settingsChanges): the values to send, only what changed, and
    each change as (what, before, after) for the person to review."""
    values: dict[str, Any] = {}
    changes: list[tuple[str, str, str]] = []
    for k in ("title", "schedule", "file_prefix"):
        a, b = base.get(k) or "", form.get(k) or ""
        if a != b:
            values[k] = b
            changes.append((setting_name(k), a or "not set", b or "not set"))
    filters: dict[str, Any] = {}
    for k in FILTER_LISTS:
        a, b = base["filters"].get(k) or [], form["filters"].get(k) or []
        if sorted(a) != sorted(b):
            filters[k] = list(b)
            changes.append((setting_name(k), list_text(a), list_text(b)))
    for k in FILTER_FLAGS:
        a, b = bool(base["filters"].get(k)), bool(form["filters"].get(k))
        if a != b:
            filters[k] = b
            changes.append((setting_name(k), readable(a), readable(b)))
    if filters:
        values["filters"] = filters
    ra, rb = base["checks"].get("min_item_ratio") or 0, form["checks"].get("min_item_ratio") or 0
    if abs(ra - rb) > 1e-9:
        values["checks"] = {"min_item_ratio": rb}
        changes.append((setting_name("min_item_ratio"), percent(ra), percent(rb)))
    legacy: dict[str, Any] = {}
    for k in LEGACY_KEYS:
        a, b = base["legacy"].get(k) or None, form["legacy"].get(k) or None
        if a != b:
            legacy[k] = b
            changes.append((setting_name(k), a or "none", b or "none"))
    if legacy:
        values["legacy"] = legacy
    return values, changes


def first_sentence(text: str | None) -> str:
    m = re.match(r"^.*?\.(?=\s|$)", str(text or ""), re.S)
    return m.group(0) if m else str(text or "")


def permission_words(result: dict) -> tuple[str, list[str]]:
    """A recipe permission check (setupops.recipe_permissions): what it found, and the lines to list."""
    if result.get("ok"):
        return "Yes. The account can read everything this recipe uses.", []
    lines = [f"Permission missing: {p}" for p in result.get("denied") or []]
    lines += [f"Couldn't read {x['name']} ({x['path']}): {x['error']}" for x in result.get("failed") or []]
    words = "Not yet. You can continue, but the feed's runs will fail until this is fixed."
    if result.get("denied"):
        words += " Ask your FOLIO administrator to add the permissions listed."
    return words, lines


def from_words(source: str, sources: list[dict]) -> str:
    """Where an installed recipe version came from (the page's fromText)."""
    if source == "bundled":
        return "comes with FOLIO-Exchange"
    if source == "this site":
        return "added to this site by hand"
    title = next((s.get("title") for s in sources if s.get("id") == source), None)
    return f"downloaded from {title or source}"


def recipe_rows(r: dict, sources: list[dict]) -> list[tuple[str, str]]:
    """One installed recipe (views.recipes_overview), as the page's Recipes view lists it."""
    versions = "; ".join(f"{v['version']} – {from_words(v['from'], sources)}" for v in r.get("versions") or [])
    return [("Versions", versions), ("Used by", ", ".join(r.get("feeds") or []) or "no feed yet"),
            ("Reads from FOLIO", ", ".join(r.get("reads") or []) or "not stated")]


def source_rows(s: dict) -> list[tuple[str, str]]:
    """A vendor's recipe source, as the page's Recipes view lists it."""
    repo = f"https://github.com/{s.get('repo')}"
    rows = [("Vendor", s.get("vendor") or ""), ("Repository", repo)]
    if s.get("homepage") and s["homepage"] != repo:
        rows.append(("Home page", s["homepage"]))
    rows.append(("Provides", ", ".join(s.get("recipes") or [])))
    rows.append(("Last downloaded", f"{when(s.get('installed'))}, commit {str(s['commit'])[:7]}" if s.get("commit")
                 else "never"))
    if s.get("origin") == "site":
        rows.append(("Listed by", "this site's own settings"))
    return rows


REVIEW_WORDS = {"new": ("New", "not installed yet"), "installed": ("Installed", "already installed"),
                "conflict": ("Refused", "refused"), "invalid": ("Refused", "refused")}


def review_lines(result: dict) -> list[str]:
    """What a recipe source offers now (sources.check), one line per recipe file, as the page reviews it."""
    out = []
    for item in result.get("recipes") or []:
        badge, status = REVIEW_WORDS.get(item.get("status"), (item.get("status"), item.get("status")))
        about = " · ".join(str(x) for x in (item.get("vendor"), status) if x)
        parts = [f"{badge}: {item.get('title') or item.get('id')} {item.get('version')} · {about}"]
        if item.get("problem"):
            parts.append(f"Refused because: {item['problem']}")
        if item.get("status") == "new":
            changes = item.get("changes")
            changes = "; ".join(map(str, changes)) if isinstance(changes, list) else changes
            parts.append(f"What changed: {changes or 'not described'}")
            parts.append(f"Reads from FOLIO: {', '.join(item.get('reads') or []) or 'not stated'}")
            parts.append(f"Patron data: {'none' if item.get('patron_data') in (None, 'none', False) else 'yes'}")
            takers = item.get("picked_up_by") or []
            parts.append(f"From their next run, these feeds use it: {', '.join(takers)}" if takers else
                         "No feed uses it until one is set to this version")
        out.append(" ".join(p if p.endswith(".") else p + "." for p in parts))
    return out


def check_words(result: dict) -> str:
    """The line above a recipe source's review: the commit, when, and what was downloaded before."""
    commit, was = str(result.get("commit") or ""), result.get("installed_commit")
    before = ("This is the commit last downloaded." if was == commit else
              f"Last downloaded: commit {str(was)[:7]}." if was else "Nothing has been downloaded from this source yet.")
    return f"Commit {commit[:7]}, checked {when(result.get('checked'))}. {before}"


def _lower_first(words: str) -> str:
    return words[:1].lower() + words[1:]


def alert_rows(a: dict) -> list[tuple[str, str]]:
    """The site's alert settings (setupops.alert_settings), never a secret: where each comes from."""
    on = dict(ALERT_ON).get(a.get("on") or "failure", str(a.get("on")))
    email = a.get("email")
    if email:
        login = (f"; logs in as {email['username']} ({_lower_first(password_words(email.get('password')))})"
                 if email.get("username") else "")
        mail = (f"To {', '.join(email.get('to') or [])}, from {email.get('from')}, through "
                f"{email.get('smtp_host')}:{email.get('smtp_port')}{login}")
    else:
        mail = "not set up"
    hook = a.get("webhook")
    chat = (f"{'Microsoft Teams workflow' if hook.get('format') == 'teams' else 'Slack-style'}, address "
            f"{_lower_first(password_words(hook.get('url')))}" if hook else "not set up")
    return [("When", on), ("Email", mail), ("Chat webhook", chat)]
