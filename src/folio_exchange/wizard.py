"""`folio-exchange init`: guided setup for library staff, one question at a time.

Work is saved as it goes: ``site.yaml`` right after a successful login, and the feed
file (paused) as soon as a recipe is chosen, so a setup interrupted by a missing
permission or an unanswered vendor question can be picked up by running ``init``
again. Existing settings are shown and kept unless the person changes them. Secrets
go to the system keyring (or stay in environment variables), never into a file.
"""
from __future__ import annotations

import getpass
import os
import re
from collections.abc import Callable
from pathlib import Path

import requests

from . import credentials, delivery, reads, runner, schedule
from .client import FolioClient, FolioError
from .config import SITE_FILE, load_site, read_yaml, write_yaml
from .errors import ConfigError, FxError
from .history import History
from .recipe import ID_RE, RESERVED_FEED_NAMES, RecipeLibrary, load_feed

SITE_HEADER = """FOLIO-Exchange site settings, written by `folio-exchange init`.
No passwords live here: see `folio-exchange secret status`."""
FEED_HEADER = """FOLIO-Exchange feed: this library's settings over a shared recipe.
Edit by hand or re-run `folio-exchange init`; `folio-exchange check` validates it."""
STATUSES = ["Withdrawn", "Lost and paid", "Declared lost", "Missing", "Long missing", "Claimed returned",
            "Unavailable", "Unknown", "In process", "On order", "Order closed"]


class Prompter:
    """Console questions. Every method returns the answer; tests use ScriptedPrompter."""

    def say(self, text: str) -> None:
        print(text)

    def ask(self, prompt: str, default: str | None = None, validate: Callable[[str], str | None] | None = None
            ) -> str:
        while True:
            suffix = f" [{default}]" if default not in (None, "") else ""
            try:
                answer = input(f"{prompt}{suffix}: ").strip()
            except EOFError:
                answer = ""
            answer = answer or (default or "")
            problem = validate(answer) if validate else None
            if not problem:
                return answer
            self.say(f"  {problem}")

    def choose(self, prompt: str, options: list[tuple[str, str]], default: str | None = None) -> str:
        self.say(prompt)
        for n, (_key, label) in enumerate(options, 1):
            self.say(f"  {n}. {label}")
        keys = [k for k, _label in options]
        dflt = str(keys.index(default) + 1) if default in keys else None

        def check(a: str) -> str | None:
            return None if a.isdigit() and 1 <= int(a) <= len(options) else f"Enter a number from 1 to {len(options)}."

        return keys[int(self.ask("Choose", dflt, check)) - 1]

    def choose_many(self, prompt: str, options: list[tuple[str, str]], default: list[str] | None = None
                    ) -> list[str]:
        self.say(prompt)
        for n, (_key, label) in enumerate(options, 1):
            self.say(f"  {n}. {label}")
        keys = [k for k, _label in options]
        dflt = ",".join(str(keys.index(k) + 1) for k in (default or []) if k in keys) or "none"

        def check(a: str) -> str | None:
            if a.lower() in ("", "none"):
                return None
            parts = [p.strip() for p in a.split(",")]
            ok = all(p.isdigit() and 1 <= int(p) <= len(options) for p in parts)
            return None if ok else "Enter numbers separated by commas, or none."

        answer = self.ask("Numbers, separated by commas (or none)", dflt, check)
        if answer.lower() in ("", "none"):
            return []
        return [keys[int(p) - 1] for p in answer.split(",")]

    def confirm(self, prompt: str, default: bool = True) -> bool:
        answer = self.ask(f"{prompt} (y/n)", "y" if default else "n",
                          lambda a: None if a.lower() in ("y", "yes", "n", "no") else "Answer y or n.")
        return answer.lower() in ("y", "yes")

    def secret(self, prompt: str) -> str:
        return getpass.getpass(f"{prompt} (not shown): ")


ConsolePrompter = Prompter


class ScriptedPrompter(Prompter):
    """Answers questions from a list, in order; None (or "") takes the default. Records a transcript."""

    def __init__(self, answers: list) -> None:
        self.answers = list(answers)
        self.transcript: list[str] = []

    def say(self, text: str) -> None:
        self.transcript.append(text)

    def _next(self, prompt: str):
        if not self.answers:
            raise AssertionError(f"No scripted answer left for: {prompt}")
        self.transcript.append(f"? {prompt}")
        return self.answers.pop(0)

    def ask(self, prompt, default=None, validate=None):
        answer = self._next(prompt)
        answer = (default or "") if answer in (None, "") else str(answer)
        problem = validate(answer) if validate else None
        if problem:
            raise AssertionError(f"Scripted answer {answer!r} rejected for {prompt}: {problem}")
        return answer

    def choose(self, prompt, options, default=None):
        self.say(prompt)
        answer = self._next(prompt)
        keys = [k for k, _ in options]
        answer = default if answer in (None, "") else answer
        if answer not in keys:
            raise AssertionError(f"Scripted choice {answer!r} is not one of {keys} for {prompt}")
        return answer

    def choose_many(self, prompt, options, default=None):
        self.say(prompt)
        answer = self._next(prompt)
        return list(default or []) if answer is None else list(answer)

    def confirm(self, prompt, default=True):
        answer = self._next(prompt)
        return default if answer is None else bool(answer)

    def secret(self, prompt):
        return str(self._next(prompt) or "")


# ------------------------------------------------------------------ helpers
def _url_problem(url: str) -> str | None:
    host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    if url.startswith("https://") and len(url) > 9:
        return None
    if url.startswith("http://") and host in ("localhost", "127.0.0.1"):
        return None
    return "Enter the full address, starting with https:// (on Eureka, the Kong gateway URL)."


def _feed_name_problem(name: str) -> str | None:
    if not ID_RE.match(name):
        return "Use lowercase letters, digits and hyphens, e.g. chq."
    if name in RESERVED_FEED_NAMES:
        return f"{name!r} is reserved; choose another name."
    return None


def _required(answer: str) -> str | None:
    return None if answer.strip() else "This is needed."


def _schedule_problem(text: str) -> str | None:
    try:
        schedule.parse(text)
    except ConfigError as exc:
        return str(exc)
    return None


def _in_site(root: Path, folder: str) -> Path:
    """A folder as the feed will see it: relative paths are taken from the site folder."""
    p = Path(os.path.expanduser(folder))
    return p if p.is_absolute() else Path(root) / p


def _load_raw(path: Path) -> dict:
    try:
        return read_yaml(path)
    except ConfigError:
        return {}


def _audit(root: Path, feed: str | None, action: str, detail: dict) -> None:
    try:
        site = load_site(root)
    except FxError:
        return
    with History(site.history_path) as hist:
        hist.audit(None, feed, action, detail)
        runner.audit_settings(site, hist)


# ------------------------------------------------------------------ wizard
def run_wizard(root: Path, prompter: Prompter, *, library: RecipeLibrary | None = None,
               read_host_key: Callable[[str, int], str] | None = None) -> int:
    p = prompter
    root = Path(root)
    read_host_key = read_host_key or delivery.host_key_fingerprint
    p.say("FOLIO-Exchange setup. Answers in [brackets] are the defaults; press Enter to keep them.")
    p.say(f"Site folder: {root}")
    if "onedrive" in str(root).lower() or "dropbox" in str(root).lower():
        p.say("WARNING  This folder looks synced to the cloud. The cache and lock files break in synced "
              "folders; a plain local folder such as C:\\FOLIO-Exchange\\<tenant> is safer.")
        if not p.confirm("Use it anyway?", False):
            return 1
    root.mkdir(parents=True, exist_ok=True)

    # 1. FOLIO connection
    site_path = root / SITE_FILE
    raw_site = _load_raw(site_path) if site_path.exists() else {}
    folio = dict(raw_site.get("folio") or {})
    if raw_site:
        p.say(f"Found site.yaml: {folio.get('base_url')} (tenant {folio.get('tenant')}, user {folio.get('username')}).")
    if not raw_site or p.confirm("Change the FOLIO connection?", False):
        p.say("\nFOLIO connection. Ask your FOLIO host for the API address (on Eureka, the Kong gateway), the "
              "tenant id, and a read-only service account.")
        folio["base_url"] = p.ask("FOLIO API address", folio.get("base_url"), _url_problem).rstrip("/")
        folio["tenant"] = p.ask("Tenant id", folio.get("tenant"), _required)
        folio["username"] = p.ask("Service account user name", folio.get("username"), _required)
        folio.setdefault("secret", "keyring")
    account = credentials.folio_account(folio["username"], folio["tenant"])
    if os.environ.get("FOLIO_PASSWORD"):
        p.say("Using the password in the FOLIO_PASSWORD environment variable.")
        password = os.environ["FOLIO_PASSWORD"]
    else:
        password = credentials.resolve(folio.get("secret"), credentials.FOLIO_SERVICE, account)
        if password and not p.confirm("A FOLIO password is already stored. Keep it?", True):
            password = None
        if not password:
            if not credentials.keyring_available():
                p.say("No system keyring is available here. Set the FOLIO_PASSWORD environment variable for this "
                      "account, then run `folio-exchange init` again.")
                return 1
            password = p.secret(f"FOLIO password for {folio['username']}")
            credentials.store(credentials.FOLIO_SERVICE, account, password)
            p.say("Stored in the system keyring.")
    client = FolioClient(folio["base_url"], folio["tenant"], folio["username"], password or "",
                         verify_tls=bool(folio.get("verify_tls", True)))
    try:
        client.login()
    except (FxError, requests.RequestException) as exc:
        p.say(f"Could not log in: {exc}\nNothing was saved. Check the address, tenant and account, then run init "
              "again.")
        return 1
    p.say(f"Logged in to {client.base_url} as {client.username}.")
    if os.environ.get("FOLIO_PASSWORD") and credentials.keyring_available() and p.confirm(
            "Also store this password in the system keyring, so scheduled runs have it?", True):
        credentials.store(credentials.FOLIO_SERVICE, account, os.environ["FOLIO_PASSWORD"])
    elif os.environ.get("FOLIO_PASSWORD"):
        folio["secret"] = "env:FOLIO_PASSWORD"       # say where the password really comes from
        p.say("Note: scheduled runs need FOLIO_PASSWORD set for the user that runs them.")
    raw_site["folio"] = folio
    write_yaml(site_path, raw_site, SITE_HEADER)
    _audit(root, None, "site settings written by init", {"file": SITE_FILE})
    site = load_site(root)
    library = library or RecipeLibrary(site.recipe_dirs)

    # 2. Recipe and feed name
    latest = [library.latest(rid) for rid in library.ids()]
    if not latest:
        p.say("No recipes found.")
        return 1
    recipe_id = p.choose("\nWhich vendor feed do you want to set up?",
                         [(r.id, f"{r.title} ({r.data.get('vendor')}, {r.ref})") for r in latest], latest[0].id)
    recipe = library.latest(recipe_id)
    uses = f"{recipe.id}@^{recipe.version[0]}.{recipe.version[1]}"
    default_name = (recipe.data.get("file_prefix") or recipe.id.split("-")[0]).lower().replace("_", "-")
    name = p.ask("A short name for this feed", default_name, _feed_name_problem)
    feed_path = site.feeds_dir / f"{name}.yaml"
    raw = _load_raw(feed_path) if feed_path.exists() else {}
    if raw:
        p.say(f"Found {feed_path.name}; its settings are offered as defaults.")
    raw["uses"] = raw.get("uses") if str(raw.get("uses", "")).split("@")[0] == recipe.id else uses
    raw.setdefault("enabled", False)
    raw.setdefault("delivery", {"transport": "none"})

    def save_feed(note: str) -> None:
        write_yaml(feed_path, raw, FEED_HEADER)
        _audit(root, name, note, {"file": str(feed_path.relative_to(root))})

    save_feed("feed written by init (paused)")

    # 3. Permissions
    p.say("\nChecking that the account can read what this recipe needs ...")
    missing = []
    for rname in [r for r in reads.ENTITY_ORDER + ("marc",) if r in recipe.data["reads"]]:
        r = reads.READS[rname]
        try:
            if rname == "marc":
                client.get(r.path, {"limit": 0})
            else:
                client.count(r.path)
        except FolioError:
            missing.append(r.permission)
    for _kind, (path, _key, perm) in reads.REFDATA.items():
        try:
            client.count(path)
        except FolioError:
            missing.append(perm)
    if missing:
        p.say("The account cannot read everything yet. Ask your FOLIO administrator to add these permissions "
              "(on Eureka, the matching capabilities) to its role:")
        for perm in sorted(set(missing)):
            p.say(f"  {perm}")
        p.say(f"Your progress is saved; run `folio-exchange init {root}` again once they are added.")
        return 1
    p.say("All the records this recipe needs can be read.")

    # 4. Filters from the tenant's own codes
    locations = sorted((loc.get("code") or "", loc.get("name") or "") for loc in client.get_all("/locations", "locations"))
    filters = dict(raw.get("filters") or {})
    if locations:
        filters["exclude_location_codes"] = p.choose_many(
            "\nLeave out any locations? (for example online resources or staff-only shelves)",
            [(code, f"{code}: {label}") for code, label in locations if code],
            filters.get("exclude_location_codes") or [])
    filters["exclude_item_statuses"] = p.choose_many(
        "Leave out items with any of these statuses? (most vendors want every status)",
        [(s, s) for s in STATUSES], filters.get("exclude_item_statuses") or [])
    raw["filters"] = {k: v for k, v in filters.items() if v}
    if not raw["filters"]:
        raw.pop("filters")

    note_types = sorted(t.get("name") for t in client.get_all("/item-note-types", "itemNoteTypes") if t.get("name"))
    if note_types and p.confirm("\nWere items migrated from an older system with counts or dates kept in item "
                                "notes?", bool(raw.get("legacy"))):
        legacy = dict(raw.get("legacy") or {})
        options = [("", "none")] + [(t, t) for t in note_types]
        for key, label in (("circ_count_note_type", "loan count from the old system"),
                           ("created_date_note_type", "original item created date"),
                           ("last_circ_date_note_type", "last loan date from the old system")):
            choice = p.choose(f"Which item note type holds the {label}?", options, legacy.get(key) or "")
            legacy[key] = choice or None
        raw["legacy"] = {k: v for k, v in legacy.items() if v}
    save_feed("feed filters set by init")

    # 5. Preview
    if p.confirm("\nBuild a preview from a sample of live records now?", True):
        try:
            res = runner.preview(site, load_feed(feed_path, library, site.root), 10)
        except FxError as exc:
            p.say(f"Preview failed: {exc}")
            res = None
        if res is not None:
            b = res.report.get("build") or {}
            if res.status == "failed":
                p.say(f"Preview failed: {res.report.get('error')}")
            else:
                p.say(f"Preview: {b.get('bibs_written', 0)} bibs and {b.get('items_written', 0)} item rows in "
                      f"{res.run_dir}")
                for w in res.report.get("warnings") or []:
                    p.say(f"WARNING  {w}")
                p.say(f"Open the files there, or run `folio-exchange preview {name}` for a closer look. "
                      f"`folio-exchange recipes summary {name}` prints the layout to send the vendor.")

    # 6. Delivery
    d = dict(raw.get("delivery") or {})
    transport = p.choose("\nHow are the files delivered?", [
        ("sftp", "upload to the vendor's SFTP server"),
        ("local", "copy into a folder (for testing, or a shared drive another tool picks up)"),
        ("none", "not yet: build the files and leave them in the run folder"),
    ], d.get("transport") if d.get("transport") in ("sftp", "local") else "sftp")
    d["transport"] = transport
    keep_paused = False
    if transport == "local":
        def folder_ok(answer: str) -> str | None:
            if not answer.strip():
                return "This is needed."
            try:
                _in_site(root, answer).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return f"That folder cannot be used ({exc.strerror or exc}). Enter another."
            return None

        d = {"transport": "local", "directory": p.ask("Folder to copy the files into", d.get("directory"),
                                                      folder_ok)}
    elif transport == "sftp":
        d["host"] = p.ask("SFTP server name (from the vendor)", d.get("host"), _required)
        d["port"] = int(p.ask("Port", str(d.get("port") or 22),
                              lambda a: None if a.isdigit() else "Enter a number, usually 22."))
        d["username"] = p.ask("SFTP user name (from the vendor)", d.get("username"), _required)
        d["remote_dir"] = p.ask("Folder on the server (the vendor says which; . is the login folder)",
                                d.get("remote_dir") or ".")
        try:
            key = read_host_key(d["host"], d["port"])
        except Exception as exc:  # noqa: BLE001 - network errors of every kind
            p.say(f"Could not reach {d['host']}:{d['port']} ({type(exc).__name__}: {exc}).")
            key = None
        if key:
            p.say(f"The server's key fingerprint is {key}")
            pin = p.choose("Does it match the fingerprint the vendor gave you?", [
                ("yes", "yes, it matches"),
                ("unconfirmed", "I don't have the vendor's value yet: keep it, but leave the feed paused"),
                ("no", "no, it differs: don't trust this server"),
            ], "yes" if d.get("host_key_fingerprint") == key else "unconfirmed")
            if pin == "no":
                p.say("Stopped: a different key can mean the wrong server. Ask the vendor before going on.")
                d.pop("host_key_fingerprint", None)
                d["transport"] = "none"
                raw["delivery"] = d
                save_feed("feed delivery left off by init (host key mismatch)")
                return 1
            d["host_key_fingerprint"] = key
            keep_paused = pin == "unconfirmed"
        else:
            d["transport"] = "none"
            keep_paused = True
            p.say("Delivery stays off until the server can be reached; run init again later.")
        if d["transport"] == "sftp":
            sacct = credentials.sftp_account(d["username"], d["host"])
            d.setdefault("secret", "keyring")
            if credentials.resolve(d["secret"], credentials.SFTP_SERVICE, sacct) and p.confirm(
                    "An SFTP password is already stored. Keep it?", True):
                pass
            elif credentials.keyring_available():
                credentials.store(credentials.SFTP_SERVICE, sacct, p.secret(f"SFTP password for {sacct}"))
            else:
                env = p.ask("No keyring here. Name of the environment variable that will hold the SFTP password",
                            "FX_SFTP_PASSWORD", lambda a: None if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", a)
                            else "Letters, digits and underscores.")
                d["secret"] = f"env:{env}"
    if d.get("transport") in ("sftp", "local"):
        if d["transport"] == "sftp" and not d.get("upload_mode"):
            d.pop("upload_mode", None)
        p.say("Testing the destination (nothing is uploaded) ...")
        try:
            test = load_feed_defaults(d)
            if test.get("directory"):
                test["directory"] = str(_in_site(root, test["directory"]))
            listing = delivery.check_delivery(test)
            p.say("  OK" + ("" if listing is None else f": {len(listing)} entries in the folder"))
        except Exception as exc:  # noqa: BLE001 - report every failure the same way
            p.say(f"  FAILED  {type(exc).__name__}: {str(exc)[:200]}")
            keep_paused = True
    raw["delivery"] = d

    # 7. Schedule
    current = raw.get("schedule") or recipe.data.get("default_schedule") or "weekly"
    raw["schedule"] = p.ask("\nWhen should it run? (e.g. weekly, daily 03:30, Sun 03:30, monthly 1 03:30, manual)",
                            current, _schedule_problem)
    p.say(f"  It will run {schedule.describe(schedule.parse(raw['schedule']))}.")
    raw["enabled"] = not keep_paused
    save_feed("feed delivery and schedule set by init")
    try:
        load_feed(feed_path, library, site.root)
    except ConfigError as exc:
        p.say(f"The feed file still has a problem:\n{exc}")
        return 1

    # 8. Alerts
    alerts = dict(raw_site.get("alerts") or {})
    if not alerts.get("webhook") and not alerts.get("email"):
        how = p.choose("\nHow should FOLIO-Exchange tell you when a run fails?", [
            ("email", "email"), ("webhook", "a chat webhook (Slack, or a Teams workflow)"), ("none", "skip for now"),
        ], "email")
        if how == "email":
            email = {"to": [p.ask("Send alerts to (email address)", None, lambda a: None if "@" in a else
                                  "Enter an email address.")],
                     "from": p.ask("From address", None, lambda a: None if "@" in a else "Enter an email address."),
                     "smtp_host": p.ask("Mail server (SMTP)", None, _required)}
            email["smtp_port"] = int(p.ask("Port", "587", lambda a: None if a.isdigit() else "A number."))
            user = p.ask("Mail server user name (blank if none)", "")
            if user:
                email["username"] = user
                credentials.store(credentials.SMTP_SERVICE, credentials.sftp_account(user, email["smtp_host"]),
                                  p.secret(f"Mail password for {user}"))
            alerts["email"] = email
        elif how == "webhook":
            alerts["webhook_format"] = p.choose("Which kind?", [("slack", "Slack (or another {text} webhook)"),
                                                                ("teams", "Microsoft Teams workflow")], "slack")
            credentials.store(credentials.WEBHOOK_SERVICE, root.resolve().name, p.secret("Webhook URL"))
            alerts["webhook"] = "keyring"
        if how != "none":
            raw_site["alerts"] = alerts
            write_yaml(site_path, raw_site, SITE_HEADER)
            _audit(root, None, "alert settings written by init", {"file": SITE_FILE})
            site = load_site(root)
            if p.confirm("Send a test alert now?", True):
                sent = runner.alert(site, None, "test", "Test alert from `folio-exchange init`.", force=True)
                p.say(f"  Sent by: {', '.join(sent)}" if sent else "  The test alert failed; see the log.")

    # 9. Done
    p.say(f"\nSaved {SITE_FILE} and feeds/{name}.yaml in {root}.")
    if keep_paused:
        p.say(f"The feed is paused until the delivery is confirmed. When it is, run `folio-exchange feeds resume "
              f"{name}`.")
    p.say("Next steps:")
    p.say(f"  1. folio-exchange -s \"{root}\" run {name}     (the first delivery is held for you to check)")
    p.say(f"  2. folio-exchange -s \"{root}\" release {name} (sends it once you are happy)")
    p.say(f"  3. folio-exchange -s \"{root}\" schedule      (shows the one command that schedules it)")
    if os.name == "nt":
        p.say(f"  Tip: setx FOLIO_EXCHANGE_SITE \"{root}\" lets you leave out -s in new windows.")
    return 0


def load_feed_defaults(d: dict) -> dict:
    """A delivery block with the defaults filled in, for testing it before the feed file is complete."""
    from .recipe import DELIVERY_DEFAULTS

    return {**DELIVERY_DEFAULTS, **d}
