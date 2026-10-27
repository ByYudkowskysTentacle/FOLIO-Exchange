"""Setting up a site and its feeds from the local page: the operations behind its forms.

Each function checks what it is given, changes one settings file, records the change in the audit log and
returns what the page shows next. The rules are init's: a FOLIO password is stored only after FOLIO accepts
it; a settings file that cannot be read is never overwritten; a hand-edited one (with comments of its own) is
first kept as ``<name>.bak``. A feed is saved only if it still loads, together with every other feed, so a
form can never leave a feed (or its neighbour) broken.

Passwords go straight to the system keyring (Windows Credential Manager) and are never written, logged or
returned. An SFTP private key is too large for Credential Manager (2,560 bytes), so it is kept as a file in
the site's ``keys`` folder that only this Windows user can read, with its passphrase in the keyring.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import io
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from . import credentials, delivery, runner, schedule
from .client import FolioClient, probe
from .config import SITE_DEFAULTS, SITE_FILE, Site, load_site, merge, read_yaml, write_yaml
from .errors import ConfigError, FxError, RecipeError
from .history import History
from .recipe import ID_RE, RESERVED_FEED_NAMES, Recipe, RecipeLibrary, load_all

SITE_HEADER = """FOLIO-Exchange site settings, written by `folio-exchange init`.
No passwords live here: see `folio-exchange secret status`."""
FEED_HEADER = """FOLIO-Exchange feed: this library's settings over a shared recipe.
Edit by hand or re-run `folio-exchange init`; `folio-exchange check` validates it."""
# Every item status FOLIO defines (the item schema's enum), the ones libraries usually leave out first.
STATUSES = ["Withdrawn", "Lost and paid", "Declared lost", "Aged to lost", "Missing", "Long missing",
            "Claimed returned", "Unavailable", "Unknown", "In process", "In process (non-requestable)", "On order",
            "Order closed", "Restricted", "Intellectual item", "In transit", "Awaiting pickup", "Awaiting delivery",
            "Paged", "Checked out", "Available"]
KEYS_DIR = "keys"
MAX_KEY_BYTES = 16 * 1024
MAX_KDF_ROUNDS = 1000          # ssh-keygen's default is 16; far more only stalls the page
_SAVE = threading.Lock()       # one settings write at a time from this process

FILTER_KEYS: dict[str, type] = {
    "include_suppressed_instances": bool, "include_suppressed_items": bool, "include_suppressed_holdings": bool,
    "include_items_without_barcode": bool, "bibs_without_items": bool,
    "exclude_item_statuses": list, "include_location_codes": list, "exclude_location_codes": list,
}
LEGACY_KEYS = ("circ_count_note_type", "created_date_note_type", "last_circ_date_note_type")
EDITABLE = ("title", "schedule", "file_prefix", "filters", "checks", "legacy")


# ------------------------------------------------------------------ checks shared with init
def url_problem(url: str) -> str | None:
    if "@" in url.split("//", 1)[-1].split("/", 1)[0]:
        return "Leave the user name and password out of the address; they have fields of their own."
    host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    if url.startswith("https://") and len(url) > 9:
        return None
    if url.startswith("http://") and host in ("localhost", "127.0.0.1"):
        return None
    return "Enter the full address, starting with https:// (on Eureka, the Kong gateway URL)."


def feed_name_problem(name: str) -> str | None:
    if not ID_RE.match(name):
        return "Use lowercase letters, digits and hyphens, e.g. chq."
    if name in RESERVED_FEED_NAMES:
        return f"{name!r} is reserved; choose another name."
    return None


def schedule_problem(text: str) -> str | None:
    try:
        schedule.parse(text)
    except ConfigError as exc:
        return str(exc)
    return None


def own_comments(path: Path, header: str) -> bool:
    """True when the file holds comments that init did not write (they would be lost on rewrite)."""
    ours = {f"# {line}".rstrip() if line else "#" for line in header.splitlines()}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return False
    return any("#" in line and line.rstrip() not in ours for line in lines)


def write_settings(path: Path, data: dict, header: str) -> Path | None:
    """Write a settings file, first keeping a hand-edited one as ``<name>.bak``. Returns the backup, if made."""
    backup = None
    if path.exists() and own_comments(path, header):
        backup = path.with_name(path.name + ".bak")
        backup.write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
    write_yaml(path, data, header)
    return backup


def _audit(site: Site, feed: str | None, action: str, detail: dict) -> None:
    with History(site.history_path) as hist:
        hist.audit(None, feed, action, detail)
        runner.audit_settings(site, hist)          # a change made here is not a hand edit


# ------------------------------------------------------------------ the FOLIO connection
def site_state(root: Path) -> dict:
    """Whether the folder holds a usable site, and its FOLIO connection (never the password)."""
    root = Path(root)
    state: dict[str, Any] = {"root": str(root), "ready": False, "problem": None, "folio": {},
                             "keyring": credentials.keyring_available(),
                             "synced_folder": any(w in str(root).lower() for w in ("onedrive", "dropbox"))}
    if not (root / SITE_FILE).is_file():
        return state
    try:
        site = load_site(root)
    except ConfigError as exc:
        state["problem"] = str(exc)
        try:
            raw = read_yaml(root / SITE_FILE).get("folio") or {}
        except ConfigError:
            raw = {}
        state["folio"] = {k: raw[k] for k in ("base_url", "tenant", "username") if isinstance(raw.get(k), str)}
        return state
    state["ready"] = True
    state["folio"] = {"base_url": site.folio["base_url"], "tenant": site.folio["tenant"],
                      "username": site.folio["username"], "password": site.folio_password_source()}
    return state


def connect(root: Path, base_url: str, tenant: str, username: str, password: str | None = None, *,
            via: str = "ui") -> dict:
    """Log in to FOLIO with these details and, only if FOLIO accepts them, store a typed password in the
    keyring and write ``site.yaml`` (creating the site folder). No password means the stored one."""
    root = Path(root)
    base_url = (base_url or "").strip().rstrip("/")
    tenant, username = (tenant or "").strip(), (username or "").strip()
    for problem in (url_problem(base_url), None if tenant else "Enter the tenant id.",
                    None if username else "Enter the user name."):
        if problem:
            raise ConfigError(problem)
    path = root / SITE_FILE
    raw: dict = {}
    if path.exists():
        try:
            raw = read_yaml(path)
        except ConfigError as exc:
            raise ConfigError(f"{exc}\nsite.yaml could not be read, so nothing was changed. Fix the file (or move "
                              "it aside to start afresh), then try again.") from None
    folio = dict(raw.get("folio") or {})
    folio.update(base_url=base_url, tenant=tenant, username=username)
    if password:
        folio["secret"] = "keyring"
    folio.setdefault("secret", "keyring")
    _site_would_load({**raw, "folio": folio})
    account = credentials.folio_account(username, tenant)
    if password:
        if not credentials.keyring_available():
            raise ConfigError("No system keyring is available on this computer, so the password cannot be stored. "
                              "Set the FOLIO_PASSWORD environment variable for this account instead.")
        use = password
    else:
        use = credentials.resolve(folio.get("secret"), credentials.FOLIO_SERVICE, account, env_first="FOLIO_PASSWORD")
        if not use:
            raise ConfigError("Enter the FOLIO password for this account.")
    eff = merge(SITE_DEFAULTS["folio"], folio)
    client = FolioClient(base_url, tenant, username, use, timeout=60.0, max_retries=2,
                         verify_tls=eff.get("verify_tls") is not False)
    try:
        client.connect()
    except (FxError, requests.RequestException) as exc:
        raise FxError(f"Could not log in: {exc}\nNothing was saved.") from None
    if password:
        credentials.store(credentials.FOLIO_SERVICE, account, password)
    changed = not path.exists() or raw.get("folio") != folio
    if changed:
        raw["folio"] = folio
        root.mkdir(parents=True, exist_ok=True)
        write_settings(path, raw, SITE_HEADER)
    site = load_site(root)
    if changed:
        _audit(site, None, "site settings written from the page", {"file": SITE_FILE, "from": via})
    if password:
        _audit(site, None, "secret stored", {"what": "FOLIO password", "service": credentials.FOLIO_SERVICE,
                                             "account": account, "from": via})
    return site_state(root)


def _site_would_load(raw: dict) -> None:
    """Refuse, before anything is stored or written, a site.yaml that would not load."""
    with tempfile.TemporaryDirectory(prefix="fx-site-") as tmp:
        try:
            write_yaml(Path(tmp) / SITE_FILE, raw, SITE_HEADER)
            load_site(tmp)
        except ConfigError as exc:
            text = str(exc).replace(str(Path(tmp).resolve() / SITE_FILE), SITE_FILE).replace(str(tmp), "")
            raise ConfigError(f"Nothing was saved, because site.yaml would not load:\n{text}") from None


def tenant_choices(site: Site) -> dict:
    """The tenant's own location codes and item note types, for the feed form's pick lists."""
    client = runner.client_for(site)
    client.connect()
    locations = sorted(({"code": loc["code"], "name": loc.get("name") or ""}
                        for loc in client.get_all("/locations", "locations") if loc.get("code")),
                       key=lambda loc: loc["code"])
    note_types = sorted({t["name"] for t in client.get_all("/item-note-types", "itemNoteTypes") if t.get("name")})
    return {"locations": locations, "statuses": STATUSES, "note_types": note_types}


def recipe_permissions(site: Site, library: RecipeLibrary, uses: str) -> dict:
    """Whether the FOLIO account can read everything a recipe needs: missing permissions, other failures."""
    recipe = library.resolve(uses)
    client = runner.client_for(site)
    client.connect()
    results = probe(client, recipe.data["reads"])
    denied = sorted({r["permission"] for r in results if r["status"] == "denied"})
    failed = [{"name": r["name"], "path": r["path"], "error": r["error"]} for r in results if r["status"] == "failed"]
    return {"recipe": recipe.ref, "ok": not denied and not failed, "denied": denied, "failed": failed}


# ------------------------------------------------------------------ feed settings
def file_version(path: Path) -> str:
    """A fingerprint of the file as it is now, so a form can tell whether it changed underneath it."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _feed_path(site: Site, name: str) -> Path:
    if feed_name_problem(name):
        raise LookupError(f"No feed called {name!r}")
    return site.feeds_dir / f"{name}.yaml"


def _open_feed(site: Site, name: str, version: str | None) -> tuple[Path, dict, bytes]:
    path = _feed_path(site, name)
    if not path.is_file():
        raise LookupError(f"No feed called {name!r}")
    old = path.read_bytes()
    if version is not None and hashlib.sha256(old).hexdigest() != version:
        raise ConfigError(f"feeds/{name}.yaml changed since this form was opened (by hand, or in another window). "
                          "Nothing was saved: reload the feed and make the change again.")
    return path, read_yaml(path), old


def _quiet(path: Path) -> dict:
    try:
        return read_yaml(path)
    except ConfigError:
        return {}


def _recipe_for(library: RecipeLibrary, uses: Any) -> Recipe | None:
    try:
        return library.resolve(str(uses or ""))
    except RecipeError:
        return None


def feed_settings(site: Site, library: RecipeLibrary, name: str) -> dict:
    """What the feed form shows: the effective settings (the recipe's defaults where the feed sets nothing), the
    delivery without secrets, and the file's version for saving."""
    path = _feed_path(site, name)
    if not path.is_file():
        raise LookupError(f"No feed called {name!r}")
    raw = read_yaml(path)
    feeds, errors = load_all(site, library)
    feed = next((f for f in feeds if f.name == name), None)
    recipe = feed.recipe if feed else _recipe_for(library, raw.get("uses"))
    rdata = recipe.data if recipe else {}
    defaults = {k: rdata.get("selection", {}).get(k) for k in FILTER_KEYS}
    filters = dict(feed.selection) if feed else {**defaults, **(raw.get("filters") or {})}
    d = dict(raw.get("delivery") or {})
    out_d: dict[str, Any] = {k: d.get(k) for k in ("transport", "host", "port", "username", "remote_dir",
                                                     "directory", "host_key_fingerprint")}
    out_d["transport"] = out_d["transport"] or "none"
    out_d["host_key_confirmed"] = d.get("host_key_confirmed", True) is not False
    out_d["auth"] = "key" if d.get("private_key") else "password"
    out_d["password"] = (credentials.source(d.get("secret"), credentials.SFTP_SERVICE,
                                            credentials.sftp_account(d.get("username") or "", d.get("host") or ""))
                         if d.get("transport") == "sftp" and d.get("host") and d.get("username") else None)
    return {
        "name": name, "version": file_version(path), "problem": errors.get(name),
        "uses": raw.get("uses"), "enabled": raw.get("enabled", True) is not False,
        "recipe": {"ref": recipe.ref, "title": recipe.title, "vendor": rdata.get("vendor")} if recipe else None,
        "title": feed.title if feed else (raw.get("title") or name),
        "schedule": feed.schedule if feed else raw.get("schedule"),
        "file_prefix": feed.file_prefix if feed else raw.get("file_prefix"),
        "filters": {k: filters.get(k) for k in FILTER_KEYS}, "filter_defaults": defaults,
        "checks": {"min_item_ratio": (feed.checks if feed else raw.get("checks") or {}).get("min_item_ratio")},
        "legacy": {k: (raw.get("legacy") or {}).get(k) for k in LEGACY_KEYS},
        "delivery": out_d,
    }


def _clean_list(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"{what}: expected a list of text values.")
    return list(dict.fromkeys(x.strip() for x in value if x.strip()))


def _apply(raw: dict, values: dict, recipe: Recipe | None) -> list[str]:
    """Apply a form's values to a feed file's settings. A value equal to the recipe's default is dropped, so the
    feed keeps following the recipe. Returns the names of the settings that changed."""
    unknown = sorted(set(values) - set(EDITABLE))
    if unknown:
        raise ConfigError(f"These settings cannot be changed here: {', '.join(unknown)}.")
    before = {k: repr(raw.get(k)) for k in EDITABLE}
    rdata = recipe.data if recipe else {}

    def put(key: str, value: Any, section: dict | None = None) -> None:
        target = raw if section is None else section
        if value in (None, "", [], {}):
            target.pop(key, None)
        else:
            target[key] = value

    if "title" in values:
        title = str(values["title"] or "").strip()
        if len(title) > 200:
            raise ConfigError("The title is too long (200 characters at most).")
        put("title", title)
    if "schedule" in values:
        text = str(values["schedule"] or "").strip()
        problem = schedule_problem(text)
        if problem:
            raise ConfigError(problem)
        put("schedule", text)
    if "file_prefix" in values:
        prefix = str(values["file_prefix"] or "").strip()
        if prefix and not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
            raise ConfigError("The file name prefix may use letters, digits, hyphens and underscores.")
        put("file_prefix", None if prefix == rdata.get("file_prefix") else prefix)
    if "filters" in values:
        if not isinstance(values["filters"], dict):
            raise ConfigError("filters: expected a set of settings.")
        filters = dict(raw.get("filters") or {})
        defaults = rdata.get("selection", {})
        for key, value in values["filters"].items():
            kind = FILTER_KEYS.get(key)
            if kind is None:
                raise ConfigError(f"filters.{key}: not a filter this form can set.")
            if kind is bool:
                if not isinstance(value, bool):
                    raise ConfigError(f"filters.{key}: expected yes or no.")
            else:
                value = _clean_list(value, f"filters.{key}")
            if value == defaults.get(key) or (kind is list and not value and not defaults.get(key)):
                filters.pop(key, None)
            else:
                filters[key] = value
        put("filters", filters)
    if "checks" in values:
        given = values["checks"] if isinstance(values["checks"], dict) else {}
        if set(given) - {"min_item_ratio"}:
            raise ConfigError("Only checks.min_item_ratio can be changed here.")
        checks = dict(raw.get("checks") or {})
        if "min_item_ratio" in given:
            ratio = given["min_item_ratio"]
            if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
                raise ConfigError("checks.min_item_ratio: a number from 0 to 1, e.g. 0.9 for 90%.")
            default = rdata.get("checks", {}).get("min_item_ratio")
            put("min_item_ratio", None if ratio == default else ratio, checks)
        put("checks", checks)
    if "legacy" in values:
        given = values["legacy"] if isinstance(values["legacy"], dict) else {}
        if set(given) - set(LEGACY_KEYS):
            raise ConfigError(f"legacy: only {', '.join(LEGACY_KEYS)}.")
        legacy = dict(raw.get("legacy") or {})
        for key, value in given.items():
            if value is not None and not isinstance(value, str):
                raise ConfigError(f"legacy.{key}: expected a note type name.")
            put(key, (value or "").strip() or None, legacy)
        put("legacy", legacy)
    return [k for k in EDITABLE if repr(raw.get(k)) != before[k]]


def _save_checked(site: Site, library: RecipeLibrary, name: str, path: Path, raw: dict, old: bytes | None) -> None:
    """Write the feed, then load every feed; if this one (or a neighbour it now clashes with) does not load,
    put the old file back and refuse. The file must still be as it was read (``old``; None: not there), so a
    save that waited on the network never overwrites a change made meanwhile."""
    with _SAVE:
        now = path.read_bytes() if path.exists() else None
        if now != old:
            if old is None:
                raise ConfigError(f"There is already a feed called {name}; choose another name.")
            raise ConfigError(f"feeds/{name}.yaml changed while this was being saved (by hand, or in another "
                              "window). Nothing was saved: reload the feed and make the change again.")
        before = {n for n in load_all(site, library)[1] if n != name}   # feeds that were already broken
        backup = write_settings(path, raw, FEED_HEADER)
        try:
            _feeds, errors = load_all(site, library)
        except Exception:
            _restore(path, old, backup)
            raise
        broken = {n: e for n, e in errors.items() if n == name or n not in before}
        if broken:
            _restore(path, old, backup)
            problem = broken.get(name) or next(iter(broken.values()))
            raise ConfigError(f"Not saved, because the feed would not load:\n{problem}")


def _restore(path: Path, old: bytes | None, backup: Path | None) -> None:
    if old is None:
        path.unlink(missing_ok=True)
    else:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(old)
        os.replace(tmp, path)
    if backup is not None:
        backup.unlink(missing_ok=True)


def add_feed(site: Site, library: RecipeLibrary, name: str, uses: str, values: dict | None = None, *,
             via: str = "ui") -> dict:
    """A new feed on a recipe, saved paused and with no delivery: a dry run that can send nothing."""
    name = (name or "").strip()
    problem = feed_name_problem(name)
    if problem:
        raise ConfigError(problem)
    path = site.feeds_dir / f"{name}.yaml"
    if path.exists():
        raise ConfigError(f"There is already a feed called {name}; choose another name.")
    uses = (uses or "").strip()
    if "@" in uses:
        recipe = library.resolve(uses)
    else:                                          # a bare id: its newest version, taking minor updates
        found = library.latest(uses)
        if found is None:
            raise ConfigError(f"No recipe called {uses!r}.")
        recipe, uses = found, f"{found.id}@^{found.version[0]}.{found.version[1]}"
    raw: dict[str, Any] = {"uses": uses, "enabled": False}
    others = sorted(site.feeds_dir.glob("*.yaml")) if site.feeds_dir.is_dir() else []
    if any(str(_quiet(q).get("uses", "")).split("@")[0] == recipe.id for q in others):
        raw["file_prefix"] = name                  # never overwrite another feed's files at the same vendor
    _apply(raw, dict(values or {}), recipe)
    raw["delivery"] = {"transport": "none"}
    site.feeds_dir.mkdir(parents=True, exist_ok=True)
    _save_checked(site, library, name, path, raw, None)
    _audit(site, name, "feed added from the page (paused)", {"file": f"feeds/{name}.yaml", "uses": raw["uses"],
                                                            "from": via})
    return feed_settings(site, library, name)


def save_feed(site: Site, library: RecipeLibrary, name: str, values: dict, *, version: str | None,
              via: str = "ui") -> dict:
    """Change a feed's everyday settings (title, schedule, file prefix, filters, item-count check, legacy note
    types). The layout and the delivery are not changed here."""
    path, raw, old = _open_feed(site, name, version)
    changed = _apply(raw, values, _recipe_for(library, raw.get("uses")))
    if changed:
        _save_checked(site, library, name, path, raw, old)
        _audit(site, name, "feed settings changed from the page", {"file": f"feeds/{name}.yaml",
                                                                    "changed": changed, "from": via})
    return {**feed_settings(site, library, name), "changed": changed}


# ------------------------------------------------------------------ delivery
def _in_site(root: Path, folder: str) -> Path:
    p = Path(os.path.expanduser(folder))
    return p if p.is_absolute() else Path(root) / p


def fingerprint_matches(given: str, keys: dict[str, str]) -> bool:
    """Whether a fingerprint the vendor gave (SHA256:..., with or without padding or the prefix, or an older
    MD5 aa:bb:... value) is the server's key."""
    text = re.sub(r"\s+", "", given or "")
    md5 = re.fullmatch(r"(?i)(?:MD5:)?((?:[0-9a-f]{2}:){15}[0-9a-f]{2})", text)
    if md5:
        return md5.group(1).lower() == keys["md5"].lower()
    if text.upper().startswith("SHA256:"):
        text = text[7:]
    return bool(text) and text.rstrip("=") == keys["sha256"][7:]


def save_delivery(site: Site, library: RecipeLibrary, name: str, form: dict, *, version: str | None,
                  read_keys: Callable[[str, int], dict[str, str]] | None = None, via: str = "ui") -> dict:
    """Set where a feed's files go. For SFTP the server's key is read now and compared with the fingerprint
    the vendor gave; with no vendor value yet it is kept unconfirmed (nothing logs in, the feed is paused);
    a different key refuses the change. Passwords and keys are stored separately."""
    path, raw, old = _open_feed(site, name, version)
    old_d = dict(raw.get("delivery") or {})
    transport = form.get("transport")
    keys: dict[str, str] | None = None
    pause = False
    if transport == "none":
        d: dict[str, Any] = {"transport": "none"}
    elif transport == "local":
        directory = str(form.get("directory") or "").strip()
        if not directory:
            raise ConfigError("Enter the folder to copy the files into.")
        try:
            _in_site(site.root, directory).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"That folder cannot be used ({exc.strerror or exc}).") from None
        d = {"transport": "local", "directory": directory}
    elif transport == "sftp":
        host = str(form.get("host") or "").strip().lower()
        username = str(form.get("username") or "").strip()
        remote_dir = str(form.get("remote_dir") or "").strip() or "."
        try:
            port = int(form.get("port") or 22)
        except (TypeError, ValueError):
            port = 0
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", host):
            raise ConfigError("Enter the SFTP server name the vendor gave you, e.g. sftp.vendor.com.")
        if not 0 < port < 65536:
            raise ConfigError("Enter the port, usually 22.")
        if not username:
            raise ConfigError("Enter the SFTP user name the vendor gave you.")
        try:
            keys = (read_keys or delivery.host_key_fingerprints)(host, port)
        except Exception as exc:  # noqa: BLE001 - network errors of every kind
            raise ConfigError(f"Could not reach {host}:{port} ({type(exc).__name__}: {exc}). Nothing was saved: "
                              "check the server name and port.") from None
        vendor_value = str(form.get("vendor_fingerprint") or "").strip()
        same_server = old_d.get("host") == host and int(old_d.get("port") or 22) == port
        known = (same_server and old_d.get("host_key_fingerprint") == keys["sha256"]
                 and old_d.get("host_key_confirmed", True) is not False)
        if vendor_value:
            if not fingerprint_matches(vendor_value, keys):
                kind = f"{keys['type']} " if keys.get("type") else ""
                raise ConfigError(f"The server's {kind}key is {keys['sha256']} (MD5 {keys['md5']}), which is not the "
                                  "fingerprint you entered. A different key can mean the wrong server, so nothing "
                                  "was saved. Check the value with the vendor (if they gave a fingerprint for "
                                  f"another kind of key, ask them for the {kind or 'matching '}one).")
            confirmed = True
        elif known:
            confirmed = True
        elif form.get("confirm_later"):
            confirmed = False
        else:
            raise ConfigError("Enter the server key fingerprint the vendor gave you, or choose to confirm it later.")
        d = {k: old_d[k] for k in ("upload_mode", "secret", "private_key") if old_d.get(k) is not None}
        d.update(transport="sftp", host=host, port=port, username=username, remote_dir=remote_dir,
                 host_key_fingerprint=keys["sha256"])
        d.setdefault("secret", "keyring")
        if not confirmed:
            d["host_key_confirmed"] = False
            pause = True
    else:
        raise ConfigError("Choose how the files are delivered: sftp, local or none.")
    raw["delivery"] = d
    if pause:
        raw["enabled"] = False
    _save_checked(site, library, name, path, raw, old)
    if old_d.get("private_key") and not d.get("private_key"):
        _drop_our_key(site, name, old_d["private_key"])
    host_key = None if keys is None else ("not confirmed" if pause else "confirmed")
    _audit(site, name, "feed delivery set from the page", {"file": f"feeds/{name}.yaml", "transport": transport,
                                                          "host_key": host_key, "from": via})
    return {**feed_settings(site, library, name), "server_key": keys}


def _sftp_delivery(raw: dict, name: str) -> dict:
    d = dict(raw.get("delivery") or {})
    if d.get("transport") != "sftp" or not d.get("host") or not d.get("username"):
        raise ConfigError(f"Set up the SFTP delivery of {name} first.")
    if not credentials.keyring_available():
        raise ConfigError("No system keyring is available on this computer, so nothing can be stored. Use an "
                          "environment variable instead (see `folio-exchange secret status`).")
    return d


def _drop_our_key(site: Site, name: str, key: str) -> None:
    """Remove the key file the page saved for this feed, once nothing uses it."""
    if Path(key).as_posix() == f"{KEYS_DIR}/{name}.key":
        (site.root / KEYS_DIR / f"{name}.key").unlink(missing_ok=True)


def _account_clash(site: Site, name: str, d: dict, key: bool) -> None:
    """Credential Manager keeps one secret per SFTP account (user@host): the password, or a key's passphrase.
    Two feeds on one account must therefore log in the same way."""
    for q in sorted(site.feeds_dir.glob("*.yaml")):
        other = _quiet(q).get("delivery") if q.stem != name else None
        if not isinstance(other, dict) or other.get("transport") != "sftp":
            continue
        same = (str(other.get("host") or "").lower() == str(d["host"]).lower()
                and other.get("username") == d["username"])
        if same and bool(other.get("private_key")) != key:
            how = "a private key" if other.get("private_key") else "a password"
            raise ConfigError(f"Feed {q.stem} logs in to the same account ({d['username']}@{d['host']}) with {how}. "
                              "Credential Manager keeps one secret per account, so both feeds must log in the same "
                              f"way: change {q.stem} too, or use {how} here.")


def store_sftp_password(site: Site, library: RecipeLibrary, name: str, password: str, *, via: str = "ui") -> dict:
    """Store the vendor's SFTP password in the keyring, and log in with it from now on (instead of a key). The
    feed is saved first, so a feed that would not load leaves the keyring as it was."""
    if not password:
        raise ConfigError("Enter the password.")
    path, raw, old = _open_feed(site, name, None)
    d = _sftp_delivery(raw, name)
    _account_clash(site, name, d, key=False)
    account = credentials.sftp_account(d["username"], d["host"])
    key = d.get("private_key")
    if d.get("secret") != "keyring" or key:
        d.pop("private_key", None)
        d["secret"] = "keyring"
        raw["delivery"] = d
        _save_checked(site, library, name, path, raw, old)
        try:
            credentials.store(credentials.SFTP_SERVICE, account, password)
        except Exception:
            _restore(path, old, None)
            raise
        if key:
            _drop_our_key(site, name, key)
    else:
        credentials.store(credentials.SFTP_SERVICE, account, password)
    _audit(site, name, "secret stored", {"what": "SFTP password", "service": credentials.SFTP_SERVICE,
                                         "account": account, "from": via})
    return feed_settings(site, library, name)


def _read_private_key(text: str, passphrase: str | None):
    import paramiko

    if "PuTTY-User-Key-File" in text:
        raise ConfigError("This is a PuTTY .ppk key. Open it in PuTTYgen, choose Conversions > Export OpenSSH key, "
                          "and use that file.")
    if "PRIVATE KEY" not in text:
        raise ConfigError("That file is not a private key. (The public key, ending .pub, is the one the vendor "
                          "gets.)")
    rounds = _kdf_rounds(text)
    if rounds is not None and rounds > MAX_KDF_ROUNDS:
        raise ConfigError(f"This key's passphrase is stretched with {rounds:,} rounds, which would take too long to "
                          f"open. Save it again with fewer (ssh-keygen -p -a 100 -f <file>), at most {MAX_KDF_ROUNDS}.")
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except paramiko.PasswordRequiredException:
            raise ConfigError("This key is protected by a passphrase: enter it too.") from None
        except Exception:  # noqa: BLE001 - a damaged key fails in many ways inside paramiko
            continue
    raise ConfigError("The key could not be read: the passphrase is wrong, or it is a kind of key FOLIO-Exchange "
                      "cannot use (it reads OpenSSH and PEM keys: Ed25519, ECDSA and RSA).")


def _kdf_rounds(text: str) -> int | None:
    """The bcrypt round count of an encrypted OpenSSH key (None for anything else)."""
    m = re.search(r"-----BEGIN OPENSSH PRIVATE KEY-----(.*?)-----END OPENSSH PRIVATE KEY-----", text, re.S)
    if not m:
        return None
    try:
        blob = base64.b64decode("".join(m.group(1).split()), validate=True)
    except ValueError:
        return None
    magic = b"openssh-key-v1\0"
    if not blob.startswith(magic):
        return None
    pos = len(magic)
    fields = []
    for _ in range(3):                             # cipher name, KDF name, KDF options
        n = int.from_bytes(blob[pos:pos + 4], "big")
        fields.append(blob[pos + 4:pos + 4 + n])
        pos += 4 + n
    kdf, opts = fields[1], fields[2]
    if kdf != b"bcrypt" or len(opts) < 8:
        return None
    salt = int.from_bytes(opts[:4], "big")
    return int.from_bytes(opts[4 + salt:8 + salt], "big")


def _owner_only(path: Path, *, folder: bool = False) -> None:
    """Let only this user open the file (or, for a folder, anything made in it): no inherited access on
    Windows, mode 600 (700) elsewhere."""
    if os.name != "nt":
        os.chmod(path, 0o700 if folder else 0o600)
        return
    domain = os.environ.get("USERDOMAIN")
    who = f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()
    grant = f"{who}:(OI)(CI)F" if folder else f"{who}:F"
    done = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant], capture_output=True,
                          encoding="oem", errors="replace", stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise FxError(f"Could not limit who can read {path.name} ({(done.stdout or done.stderr or '').strip()}).")


def store_private_key(site: Site, library: RecipeLibrary, name: str, key_text: str, passphrase: str | None = None,
                      *, via: str = "ui") -> dict:
    """Keep the vendor login's private key as ``keys/<feed>.key`` (only this user can read it) and its
    passphrase in the keyring, and log in with the key from now on."""
    key_text = (key_text or "").replace("\r\n", "\n").strip() + "\n"
    if len(key_text.encode("utf-8")) > MAX_KEY_BYTES:
        raise ConfigError("That file is too large to be a private key.")
    pkey = _read_private_key(key_text, passphrase)
    path, raw, old = _open_feed(site, name, None)
    d = _sftp_delivery(raw, name)
    _account_clash(site, name, d, key=True)
    folder = site.root / KEYS_DIR
    folder.mkdir(exist_ok=True)
    _owner_only(folder, folder=True)               # before any key is written into it
    final = folder / f"{name}.key"
    fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=f".{name}.", suffix=".tmp")   # a new file of its own (O_EXCL)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(key_text)
        _owner_only(tmp)
        d.update(private_key=f"{KEYS_DIR}/{name}.key", secret="keyring")
        raw["delivery"] = d
        _save_checked(site, library, name, path, raw, old)       # refused (and put back) if the feed would not load
        if passphrase:
            try:
                credentials.store(credentials.SFTP_SERVICE, credentials.sftp_account(d["username"], d["host"]),
                                  passphrase)
            except Exception:
                _restore(path, old, None)
                raise
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    public = f"{pkey.get_name()} {delivery.sha256_fingerprint(pkey)}"
    _audit(site, name, "SFTP private key stored", {"file": f"{KEYS_DIR}/{name}.key", "key": public,
                                                   "passphrase": bool(passphrase), "from": via})
    return {**feed_settings(site, library, name), "key": public}
