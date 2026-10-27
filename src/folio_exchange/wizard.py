"""`folio-exchange init`: guided setup for library staff, one question at a time.

Work is saved as it goes: ``site.yaml`` right after a successful login, and a new feed
file (paused) as soon as a recipe is chosen, so a setup interrupted by a missing
permission or an unanswered vendor question can be picked up by running ``init``
again. Existing settings are offered as defaults and kept unless the person changes
them; a file that cannot be read stops setup instead of being overwritten, and a
hand-edited file is kept as ``.bak`` before it is rewritten. A password is stored only
after FOLIO accepts it. Secrets go to the system keyring (or stay in environment
variables), never into a file.
"""
from __future__ import annotations

import getpass
import os
import re
from collections.abc import Callable
from pathlib import Path

import requests

from . import credentials, delivery, runner, schedule
from .client import FolioClient, probe
from .config import SITE_FILE, load_site, read_yaml, write_yaml
from .errors import ConfigError, FxError
from .history import History
from .recipe import RecipeLibrary, load_feed
from .setupops import FEED_HEADER, SITE_HEADER, STATUSES
from .setupops import feed_name_problem as _feed_name_problem
from .setupops import own_comments as _own_comments
from .setupops import schedule_problem as _schedule_problem
from .setupops import url_problem as _url_problem


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
            except EOFError:                 # input closed: stop rather than ask the same question for ever
                raise KeyboardInterrupt from None
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
def _required(answer: str) -> str | None:
    return None if answer.strip() else "This is needed."


def _in_site(root: Path, folder: str) -> Path:
    """A folder as the feed will see it: relative paths are taken from the site folder."""
    p = Path(os.path.expanduser(folder))
    return p if p.is_absolute() else Path(root) / p


def _load_existing(path: Path, label: str) -> dict:
    """An existing settings file, or {} when there is none. A file that cannot be read stops setup: rewriting
    it would lose what the person had (the vendor host, the pinned key, the schedule)."""
    if not path.exists():
        return {}
    try:
        return read_yaml(path)
    except ConfigError as exc:
        raise ConfigError(f"{exc}\n{label} could not be read, so init stopped without changing it. Fix the "
                          "file (or move it aside to start afresh), then run init again.") from None


class _Writer:
    """Writes a settings file, first keeping a copy of a hand-edited one (comments and layout) as .bak."""

    def __init__(self, path: Path, header: str, p: Prompter) -> None:
        self.path, self.header, self.p = path, header, p
        self.backed_up = False

    def write(self, data: dict) -> None:
        if not self.backed_up and self.path.exists() and _own_comments(self.path, self.header):
            backup = self.path.with_name(self.path.name + ".bak")
            backup.write_text(self.path.read_text(encoding="utf-8-sig"), encoding="utf-8")
            self.p.say(f"  (Your copy of {self.path.name}, with its comments, is kept as {backup.name}.)")
        self.backed_up = True
        write_yaml(self.path, data, self.header)


def _audit(root: Path, feed: str | None, action: str, detail: dict) -> None:
    try:
        site = load_site(root)
    except FxError:
        return
    with History(site.history_path) as hist:
        hist.audit(None, feed, action, detail)
        runner.audit_settings(site, hist)


def _paused_by_person(root: Path, name: str) -> bool:
    """True when the feed's last pause or resume was a person's `feeds pause` (not init's own first save)."""
    try:
        site = load_site(root)
    except FxError:
        return False
    with History(site.history_path) as hist:
        for entry in hist.audit_log(200, name):
            if entry["action"] in ("feed paused", "feed resumed"):
                return entry["action"] == "feed paused"
    return False


def _env_name(answer: str) -> str | None:
    return None if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", answer) else "Letters, digits and underscores."


def _secret_ref(p: Prompter, service: str, account: str, what: str, env_default: str) -> str | None:
    """Ask for a secret and keep it in the system keyring, or (with no keyring) name the environment variable
    that will hold it. Returns the reference for the settings file, or None when the person leaves it for later."""
    if credentials.keyring_available():
        value = p.secret(f"{what} (press Enter to leave it for later)")
        if not value:
            return None
        credentials.store(service, account, value)
        p.say("  Stored in the system keyring.")
        return "keyring"
    env = p.ask(f"No keyring here. Name of the environment variable that will hold the {what[0].lower() + what[1:]}",
                env_default, _env_name)
    return f"env:{env}"


# ------------------------------------------------------------------ wizard
def run_wizard(root: Path, prompter: Prompter, *, library: RecipeLibrary | None = None,
               read_host_key: Callable[[str, int], str] | None = None) -> int:
    try:
        return _run_wizard(root, prompter, library=library, read_host_key=read_host_key)
    except ConfigError as exc:
        prompter.say(str(exc))
        return 1


def _run_wizard(root: Path, p: Prompter, *, library: RecipeLibrary | None,
                read_host_key: Callable[[str, int], str] | None) -> int:
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

    # 1. FOLIO connection. An existing site.yaml is read the normal way (${ENV} values, FOLIO_URL and the like).
    site_path = root / SITE_FILE
    raw_site = _load_existing(site_path, SITE_FILE)
    site_writer = _Writer(site_path, SITE_HEADER, p)
    folio = dict(raw_site.get("folio") or {})
    effective: dict | None = None
    if raw_site:
        try:
            effective = dict(load_site(root).folio)
        except ConfigError as exc:
            p.say(f"site.yaml has a problem, so its FOLIO connection is asked again:\n{exc}")
    if effective:
        p.say(f"Found site.yaml: {effective['base_url']} (tenant {effective['tenant']}, user {effective['username']}).")
    if not effective or p.confirm("Change the FOLIO connection?", False):
        p.say("\nFOLIO connection. Ask your FOLIO host for the API address (on Eureka, the Kong gateway), the "
              "tenant id, and a read-only service account.")
        folio["base_url"] = p.ask("FOLIO API address", folio.get("base_url"), _url_problem).rstrip("/")
        folio["tenant"] = p.ask("Tenant id", folio.get("tenant"), _required)
        folio["username"] = p.ask("Service account user name", folio.get("username"), _required)
        folio.setdefault("secret", "keyring")
        effective = {**(effective or {}), **folio}
        site_changed = True
    else:
        site_changed = False
    account = credentials.folio_account(effective["username"], effective["tenant"])
    typed = stored = None
    if os.environ.get("FOLIO_PASSWORD"):
        p.say("Using the password in the FOLIO_PASSWORD environment variable.")
        password = os.environ["FOLIO_PASSWORD"]
    else:
        stored = credentials.resolve(effective.get("secret"), credentials.FOLIO_SERVICE, account)
        password = stored
        if stored and not p.confirm("A FOLIO password is already stored. Keep it?", True):
            password = None
        if not password:
            if not credentials.keyring_available():
                p.say("No system keyring is available here. Set the FOLIO_PASSWORD environment variable for this "
                      "account, then run `folio-exchange init` again.")
                return 1
            typed = p.secret(f"FOLIO password for {effective['username']}")
            if not typed:
                p.say("Nothing was entered. Run init again when you have the password.")
                return 1
            password = typed
    client = FolioClient(effective["base_url"], effective["tenant"], effective["username"], password,
                         timeout=float(effective.get("timeout_seconds", 120)),
                         max_retries=int(effective.get("max_retries", 5)),
                         verify_tls=bool(effective.get("verify_tls", True)))
    try:
        client.connect()
    except (FxError, requests.RequestException) as exc:
        p.say(f"Could not log in: {exc}")
        if password == stored:
            p.say("The stored password may be the problem: run init again and answer n to \"Keep it?\" to type "
                  "it again.")
        p.say("Nothing was saved: no settings were written and no password was stored. Check the address, "
              "tenant and account, then run init again.")
        return 1
    p.say(f"Logged in to {client.base_url} as {client.username}.")
    if typed:                                          # only a password FOLIO accepted is kept
        credentials.store(credentials.FOLIO_SERVICE, account, typed)
        p.say("  Stored in the system keyring.")
    elif os.environ.get("FOLIO_PASSWORD"):
        if credentials.keyring_available() and p.confirm(
                "Also store this password in the system keyring, so scheduled runs have it?", True):
            credentials.store(credentials.FOLIO_SERVICE, account, os.environ["FOLIO_PASSWORD"])
            if folio.get("secret") != "keyring" and str(folio.get("secret", "")).startswith("env:"):
                folio["secret"], site_changed = "keyring", True
        elif folio.get("secret") != "env:FOLIO_PASSWORD" and credentials.resolve(
                folio.get("secret"), credentials.FOLIO_SERVICE, account) is None:
            folio["secret"], site_changed = "env:FOLIO_PASSWORD", True   # say where the password really comes from
            p.say("Note: scheduled runs need FOLIO_PASSWORD set for the user that runs them.")
    if site_changed or not site_path.exists():
        raw_site["folio"] = folio
        site_writer.write(raw_site)
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
    existing = feed_path.exists()
    raw = _load_existing(feed_path, f"feeds/{name}.yaml")
    feed_writer = _Writer(feed_path, FEED_HEADER, p)
    was_enabled = raw.get("enabled", True) if existing else None
    if existing:
        p.say(f"Found {feed_path.name}; its settings are offered as defaults.")
    raw["uses"] = raw.get("uses") if str(raw.get("uses", "")).split("@")[0] == recipe.id else uses
    raw.setdefault("delivery", {"transport": "none"})
    if not existing:
        raw["enabled"] = False
        others = [q for q in site.feeds_dir.glob("*.yaml") if q != feed_path] if site.feeds_dir.is_dir() else []
        same = [q.stem for q in others if str(_load_quiet(q).get("uses", "")).split("@")[0] == recipe.id]
        if same and "file_prefix" not in raw:
            raw["file_prefix"] = name
            p.say(f"  Files will be named {name}_..., so they never overwrite feed {same[0]}'s files at the vendor.")

    def save_feed(note: str) -> None:
        feed_writer.write(raw)
        _audit(root, name, note, {"file": str(feed_path.relative_to(root))})

    if not existing:
        save_feed("feed written by init (paused)")

    # 3. Permissions: only refusals are missing permissions; other failures are named as they are.
    p.say("\nChecking that the account can read what this recipe needs ...")
    results = probe(client, recipe.data["reads"])
    denied = sorted({r["permission"] for r in results if r["status"] == "denied"})
    failed = [r for r in results if r["status"] == "failed"]
    if denied:
        p.say("The account cannot read everything yet. Ask your FOLIO administrator to add these permissions "
              "(on Eureka, the matching capabilities) to its role:")
        for perm in denied:
            p.say(f"  {perm}")
    for r in failed:
        p.say(f"FOLIO could not answer for {r['name']} ({r['path']}): {r['error']}")
    if denied or failed:
        if failed and not denied:
            p.say("That is not a permission problem. Check the API address, or run `folio-exchange check` later.")
        p.say(f"Your progress is saved; run `folio-exchange init {root}` again once this is sorted out.")
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
            confirmed_before = d.get("host_key_fingerprint") == key and d.get("host_key_confirmed", True) is not False
            pin = p.choose("Does it match the fingerprint the vendor gave you?", [
                ("yes", "yes, it matches"),
                ("unconfirmed", "I don't have the vendor's value yet: keep it, but don't connect until I confirm it"),
                ("no", "no, it differs: don't trust this server"),
            ], "yes" if confirmed_before else "unconfirmed")
            if pin == "no":
                p.say("Stopped: a different key can mean the wrong server. Ask the vendor before going on.")
                d.pop("host_key_fingerprint", None)
                d.pop("host_key_confirmed", None)
                d["transport"] = "none"
                raw["delivery"] = d
                save_feed("feed delivery left off by init (host key mismatch)")
                return 1
            d["host_key_fingerprint"] = key
            if pin == "unconfirmed":
                d["host_key_confirmed"] = False
                keep_paused = True
                p.say("  Nothing will log in to that server until you confirm the key: run init again once the "
                      "vendor has given you their fingerprint.")
            else:
                d.pop("host_key_confirmed", None)
        else:
            d["transport"] = "none"
            keep_paused = True
            p.say("Delivery stays off until the server can be reached; run init again later.")
        raw["delivery"] = d
        save_feed("feed delivery set by init")
        if d["transport"] == "sftp":
            sacct = credentials.sftp_account(d["username"], d["host"])
            d.setdefault("secret", "keyring")
            if credentials.resolve(d["secret"], credentials.SFTP_SERVICE, sacct) and p.confirm(
                    "An SFTP password is already stored. Keep it?", True):
                pass
            else:
                ref = _secret_ref(p, credentials.SFTP_SERVICE, sacct, f"SFTP password for {sacct}",
                                  "FX_SFTP_PASSWORD")
                if ref is None:
                    keep_paused = True
                    p.say(f"  Store it later with `folio-exchange secret set {name}`; the feed stays paused until "
                          f"then (`folio-exchange feeds resume {name}`).")
                else:
                    d["secret"] = ref
    if d.get("transport") in ("sftp", "local") and d.get("host_key_confirmed") is not False:
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

    # 7. Schedule, and whether the feed runs
    current = raw.get("schedule") or recipe.data.get("default_schedule") or "weekly"
    raw["schedule"] = p.ask("\nWhen should it run? (e.g. weekly, daily 03:30, Sun 03:30, monthly 1 03:30, manual)",
                            current, _schedule_problem)
    p.say(f"  It will run {schedule.describe(schedule.parse(raw['schedule']))}.")
    if keep_paused:
        raw["enabled"] = False
    elif existing and was_enabled is False and _paused_by_person(root, name):
        raw["enabled"] = p.confirm("This feed was paused with `feeds pause`. Resume it now?", False)
        if not raw["enabled"]:
            p.say(f"  It stays paused; `folio-exchange feeds resume {name}` resumes it.")
    else:
        raw["enabled"] = True
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
                ref = _secret_ref(p, credentials.SMTP_SERVICE, credentials.sftp_account(user, email["smtp_host"]),
                                  f"Mail password for {user}", "FX_SMTP_PASSWORD")
                email["secret"] = ref or "keyring"
                if ref is None:
                    p.say("  Store it later with `folio-exchange secret set smtp`.")
            alerts["email"] = email
        elif how == "webhook":
            alerts["webhook_format"] = p.choose("Which kind?", [("slack", "Slack (or another {text} webhook)"),
                                                                ("teams", "Microsoft Teams workflow")], "slack")
            ref = _secret_ref(p, credentials.WEBHOOK_SERVICE, root.resolve().name, "Webhook URL", "FX_WEBHOOK_URL")
            if ref is None:
                p.say("  No webhook was set up. Run init again, or `folio-exchange secret set webhook` after adding "
                      "`webhook: keyring` under alerts in site.yaml.")
                how = "none"
            else:
                alerts["webhook"] = ref
        if how != "none":
            raw_site["alerts"] = alerts
            raw_site.setdefault("folio", folio)
            site_writer.write(raw_site)
            _audit(root, None, "alert settings written by init", {"file": SITE_FILE})
            site = load_site(root)
            if p.confirm("Send a test alert now?", True):
                sent = runner.alert(site, None, "test", "Test alert from `folio-exchange init`.", force=True)
                p.say(f"  Sent by: {', '.join(sent)}" if sent else "  The test alert failed; see the log.")

    # 9. Done
    p.say(f"\nSaved {SITE_FILE} and feeds/{name}.yaml in {root}.")
    fx = f"folio-exchange -s \"{root}\""
    if d.get("transport") in ("sftp", "local"):
        if not raw["enabled"]:
            first = ("Once the vendor has given you the key fingerprint, run init again to confirm it. Then t"
                     if d.get("host_key_confirmed") is False else "T")
            p.say(f"The feed is paused. {first}est the delivery with `{fx} check --delivery {name}`, then resume it "
                  f"with `{fx} feeds resume {name}`.")
        p.say("Next steps:")
        p.say(f"  1. {fx} run {name}   (the first delivery is held for you to check)")
        p.say(f"  2. {fx} release {name}   (sends it once you are happy)")
        p.say(f"  3. {fx} schedule   (shows the command that schedules it)")
    else:
        if not raw["enabled"]:
            p.say(f"The feed is paused. When it is ready, run `{fx} feeds resume {name}`.")
        p.say("Next steps for a dry run (nothing is sent anywhere):")
        p.say(f"  1. {fx} check   (login, permissions and record counts)")
        p.say(f"  2. {fx} preview {name}   (a few rows from a sample)")
        p.say(f"  3. {fx} run {name} --no-deliver   (the full files, in a dated folder under "
              f"{site.out_dir / name})")
        p.say(f"  4. {fx} history   (the runs; history --show <number> for one)")
        p.say("When the vendor's delivery details are ready, run init again and choose how the files are "
              "delivered. The first real delivery is then held for you to check and release.")
    p.say(f"  Tip: `{fx} launcher` puts a file on your desktop that opens the page for this site.")
    if os.name == "nt":
        p.say(f"  Tip: setx FOLIO_EXCHANGE_SITE \"{root}\" lets you leave out -s in new windows.")
    return 0


def _load_quiet(path: Path) -> dict:
    try:
        return read_yaml(path)
    except ConfigError:
        return {}


def load_feed_defaults(d: dict) -> dict:
    """A delivery block with the defaults filled in, for testing it before the feed file is complete."""
    from .recipe import DELIVERY_DEFAULTS

    return {**DELIVERY_DEFAULTS, **d}
