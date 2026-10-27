"""The site folder and its ``site.yaml``: FOLIO connection, folders, sync, runs and alerts.

``${NAME}`` and ``${NAME:-default}`` in any string are read from the environment, and
FOLIO_URL, FOLIO_TENANT and FOLIO_USERNAME override the ``folio`` block, so containers
need no file edits. Secrets never live here: see credentials.py.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from . import credentials
from .errors import ConfigError

SITE_FILE = "site.yaml"
SITE_ENV = "FOLIO_EXCHANGE_SITE"
BUNDLED_RECIPES = Path(__file__).parent / "recipes"

SITE_DEFAULTS: dict[str, Any] = {
    "folio": {
        "base_url": "",
        "tenant": "",
        "username": "",
        "secret": "keyring",
        "page_size": 500,
        "srs_batch_size": 200,
        "timeout_seconds": 120,
        "max_retries": 5,
        "verify_tls": True,
        "srs_include_shared": False,
    },
    "sync": {"mode": "auto", "full_refresh_days": 30},
    "paths": {"data": "data", "out": "out", "logs": "logs", "feeds": "feeds", "recipes": ["recipes"]},
    "runs": {"keep_local_runs": 5, "history_months": 12, "max_attempts_per_slot": 3, "timezone": None},
    "alerts": {"on": "failure", "webhook": None, "webhook_format": "slack", "email": None},
    "recipe_sources": [],
}

EMAIL_DEFAULTS: dict[str, Any] = {
    "to": [], "from": "", "smtp_host": "", "smtp_port": 587, "starttls": True, "username": None, "secret": "keyring",
}

class _Loader(yaml.SafeLoader):
    """YAML with only true and false as booleans (as in YAML 1.2), so a setting called ``on`` and codes
    such as NO, ON, Y or OFF stay text."""


_Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
                              list("tTfF"))


def load_yaml_text(text: str):
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - a SafeLoader subclass


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass
class Site:
    root: Path
    folio: dict
    sync: dict
    paths: dict
    runs: dict
    alerts: dict
    recipe_sources: list
    cache_path: Path = field(init=False)
    history_path: Path = field(init=False)
    lock_path: Path = field(init=False)
    out_dir: Path = field(init=False)
    logs_dir: Path = field(init=False)
    feeds_dir: Path = field(init=False)
    recipe_dirs: list[Path] = field(init=False)

    def __post_init__(self) -> None:
        def at(p: str) -> Path:
            path = Path(os.path.expanduser(str(p)))
            return path if path.is_absolute() else self.root / path

        data = at(self.paths["data"])
        self.cache_path = data / "cache.sqlite"
        self.history_path = data / "history.sqlite"
        self.lock_path = data / "run.lock"
        self.out_dir = at(self.paths["out"])
        self.logs_dir = at(self.paths["logs"])
        self.feeds_dir = at(self.paths["feeds"])
        recipes = self.paths["recipes"]
        self.recipe_dirs = [at(p) for p in ([recipes] if isinstance(recipes, str) else recipes)] + [BUNDLED_RECIPES]

    @property
    def name(self) -> str:
        return self.root.name

    @property
    def timezone(self) -> ZoneInfo | None:
        tz = self.runs.get("timezone")
        return ZoneInfo(tz) if tz else None

    def folio_password(self) -> str | None:
        f = self.folio
        return credentials.resolve(
            f["secret"], credentials.FOLIO_SERVICE, credentials.folio_account(f["username"], f["tenant"]),
            env_first="FOLIO_PASSWORD",
        )

    def folio_password_source(self) -> str:
        f = self.folio
        return credentials.source(
            f["secret"], credentials.FOLIO_SERVICE, credentials.folio_account(f["username"], f["tenant"]),
            env_first="FOLIO_PASSWORD",
        )


# ------------------------------------------------------------------ helpers
def _subst(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _subst(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v) for v in value]
    return value


def merge(base: dict, override: dict | None) -> dict:
    """Deep-merge ``override`` into a copy of ``base``; lists and scalars replace."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def reject_secrets(data: object, where: str, _path: str = "") -> None:
    """Refuse any file that holds a secret-looking key, whatever its value."""
    if isinstance(data, dict):
        for k, v in data.items():
            path = f"{_path}.{k}" if _path else str(k)
            if str(k).lower() in credentials.SECRET_KEYS:
                raise ConfigError(
                    f"{where}: {path}: secrets never go in files. Remove it, then store the secret with "
                    "`folio-exchange secret set ...` or in an environment variable, and point `secret:` "
                    "at it (keyring or env:NAME)."
                )
            reject_secrets(v, where, path)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            reject_secrets(v, where, f"{_path}[{i}]")


def read_yaml(path: Path, where: str | None = None) -> dict:
    where = where or str(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(f"{where}: file not found") from None
    try:
        data = load_yaml_text(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        at = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        line = f"line {mark.line + 1}" if mark else "the line named"
        raise ConfigError(
            f"{where}: the file's layout is broken{at}. Check the indentation on {line} and the line above "
            "(settings under a heading are indented by the same spaces), that a list in [ ] has its closing ], "
            f"and that text containing : or # is in quotes. (YAML says: {getattr(exc, 'problem', exc)})"
        ) from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping of settings at the top level")
    return data


def write_yaml(path: Path, data: dict, header: str = "") -> None:
    """Write settings as UTF-8 YAML, keeping key order. Refuses to write anything secret-looking."""
    reject_secrets(data, str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    lead = "".join(f"# {line}\n" if line else "#\n" for line in header.splitlines()) if header else ""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(lead + body, encoding="utf-8")
    os.replace(tmp, path)


def unknown_keys(data: dict, allowed: dict | set, where: str) -> list[str]:
    return [f"{where}.{k}: unknown setting" for k in data if k not in allowed]


# ------------------------------------------------------------------ site
def find_site(explicit: str | os.PathLike | None = None) -> Path:
    """The site folder: ``explicit``, else FOLIO_EXCHANGE_SITE, else the current directory."""
    if explicit:
        candidates, how = [Path(explicit)], "the --site option"
    elif os.environ.get(SITE_ENV):
        candidates, how = [Path(os.environ[SITE_ENV])], f"the {SITE_ENV} environment variable"
    else:
        candidates, how = [Path.cwd()], "the current folder"
    root = candidates[0].expanduser()
    if root.is_file() and root.name == SITE_FILE:
        root = root.parent
    if not (root / SITE_FILE).is_file():
        raise ConfigError(
            f"No {SITE_FILE} in {root} (from {how}). Run `folio-exchange init` to set up a site there, "
            "or point --site at the folder that holds site.yaml."
        )
    return root.resolve()


def load_site(root: str | os.PathLike) -> Site:
    root = Path(root).resolve()
    where = str(root / SITE_FILE)
    raw = read_yaml(root / SITE_FILE, where)
    reject_secrets(raw, where)
    problems = unknown_keys(raw, SITE_DEFAULTS, "site")
    for section, defaults in SITE_DEFAULTS.items():
        if isinstance(defaults, list):
            if section in raw and raw[section] is None:
                raw[section] = []                      # every entry commented out
            if raw.get(section) is not None and not isinstance(raw[section], list):
                problems.append(f"{section}: expected a list")
        elif isinstance(raw.get(section), dict):
            problems += unknown_keys(raw[section], defaults, section)
        elif raw.get(section) is not None:
            problems.append(f"{section}: expected a mapping of settings")
    if problems:
        raise ConfigError(f"{where}:\n  " + "\n  ".join(problems))
    cfg = _subst(merge(SITE_DEFAULTS, raw))
    for key, env in (("base_url", "FOLIO_URL"), ("tenant", "FOLIO_TENANT"), ("username", "FOLIO_USERNAME")):
        if os.environ.get(env):
            cfg["folio"][key] = os.environ[env]
    if isinstance(cfg["alerts"].get("email"), dict):
        cfg["alerts"]["email"] = merge(EMAIL_DEFAULTS, cfg["alerts"]["email"])
    problems = validate_site(cfg)
    if problems:
        raise ConfigError(f"{where}:\n  " + "\n  ".join(problems))
    return Site(root=root, **cfg)


def _int(value: Any, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def validate_site(cfg: dict) -> list[str]:
    p: list[str] = []
    f = cfg["folio"]
    for key in ("base_url", "tenant", "username"):
        if not str(f.get(key) or "").strip():
            p.append(f"folio.{key}: required (run `folio-exchange init`, or set FOLIO_{key.split('_')[-1].upper()})")
    url = str(f.get("base_url") or "")
    host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    if url and not url.startswith("https://") and not (
        url.startswith("http://") and host in ("localhost", "127.0.0.1", "[::1]")
    ):
        p.append("folio.base_url: must start with https:// (the password would otherwise cross the network "
                 "unencrypted)")
    if "@" in url.split("//", 1)[-1].split("/", 1)[0]:
        p.append("folio.base_url: must not contain a user name or password")
    if not credentials.is_secret_ref(f.get("secret")):
        p.append("folio.secret: must be keyring or env:NAME")
    for key, minimum in (("page_size", 1), ("srs_batch_size", 1), ("max_retries", 0)):
        if not _int(f.get(key), minimum):
            p.append(f"folio.{key}: must be a whole number of at least {minimum}")
    if not isinstance(f.get("timeout_seconds"), (int, float)) or f["timeout_seconds"] <= 0:
        p.append("folio.timeout_seconds: must be a positive number")
    for key in ("verify_tls", "srs_include_shared"):
        if not isinstance(f.get(key), bool):
            p.append(f"folio.{key}: must be true or false")

    s = cfg["sync"]
    if s.get("mode") not in {"auto", "full", "delta"}:
        p.append("sync.mode: must be auto, full or delta")
    if not _int(s.get("full_refresh_days"), 0):
        p.append("sync.full_refresh_days: must be a whole number (0 = never)")

    paths = cfg["paths"]
    for key in ("data", "out", "logs", "feeds"):
        if not isinstance(paths.get(key), str) or not paths[key].strip():
            p.append(f"paths.{key}: must be a folder path")
    rec = paths.get("recipes")
    if not (isinstance(rec, str) or (isinstance(rec, list) and all(isinstance(x, str) for x in rec))):
        p.append("paths.recipes: must be a folder path or a list of them")

    from .sources import source_problems

    for n, entry in enumerate(cfg["recipe_sources"]):
        p += source_problems(entry, f"recipe_sources[{n}]")

    r = cfg["runs"]
    for key, minimum in (("keep_local_runs", 0), ("history_months", 1), ("max_attempts_per_slot", 1)):
        if not _int(r.get(key), minimum):
            p.append(f"runs.{key}: must be a whole number of at least {minimum}")
    if r.get("timezone"):
        try:
            ZoneInfo(str(r["timezone"]))
        except (ZoneInfoNotFoundError, ValueError):
            p.append(f"runs.timezone: unknown time zone {r['timezone']!r} (use a name like America/New_York)")

    a = cfg["alerts"]
    if a.get("on") not in {"failure", "always", "never"}:
        p.append("alerts.on: must be failure, always or never")
    if a.get("webhook_format") not in {"slack", "teams"}:
        p.append("alerts.webhook_format: must be slack (Slack, Mattermost, Google Chat) or teams (Teams Workflows)")
    if a.get("webhook") is not None and not credentials.is_secret_ref(a["webhook"]):
        p.append("alerts.webhook: must be keyring or env:NAME (the URL itself is a secret)")
    email = a.get("email")
    if email is not None:
        if not isinstance(email, dict):
            p.append("alerts.email: expected a mapping of settings")
        else:
            p += unknown_keys(email, EMAIL_DEFAULTS, "alerts.email")
            to = email.get("to")
            if isinstance(to, str):
                email["to"] = to = [to]
            if not to or not all(isinstance(x, str) and "@" in x for x in to):
                p.append("alerts.email.to: one or more email addresses")
            if not isinstance(email.get("from"), str) or "@" not in email.get("from", ""):
                p.append("alerts.email.from: an email address")
            if not str(email.get("smtp_host") or "").strip():
                p.append("alerts.email.smtp_host: required")
            if not _int(email.get("smtp_port"), 1):
                p.append("alerts.email.smtp_port: a port number")
            if not isinstance(email.get("starttls"), bool):
                p.append("alerts.email.starttls: must be true or false")
            if email.get("username") and not email.get("starttls") and int(email.get("smtp_port") or 0) != 465:
                p.append("alerts.email.starttls: must be true when logging in (or use port 465, SMTP over TLS), "
                         "so the SMTP password is never sent unencrypted")
            if email.get("username") and not credentials.is_secret_ref(email.get("secret")):
                p.append("alerts.email.secret: must be keyring or env:NAME")
    return p


def rewrite_key(path: Path, key: str, value: str) -> None:
    """Set a top-level ``key: value`` line in a settings file, keeping its comments and layout."""
    text = path.read_text(encoding="utf-8-sig")
    line = f"{key}: {value}"
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    text = pattern.sub(line, text, count=1) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def password_note(site: Site) -> str | None:
    """When the FOLIO password comes from an environment variable, what the scheduled task needs to have it."""
    source = site.folio_password_source()
    if not source.startswith("environment ("):
        return None
    name = source[len("environment ("):-1]
    if str(site.folio.get("secret") or "").startswith("env:") and name != "FOLIO_PASSWORD":
        return (f"The FOLIO password is read from the environment variable {name}. Set {name} for the user the "
                "scheduled task runs as, too.")
    if site.folio.get("secret") == "env:FOLIO_PASSWORD":
        return ("The FOLIO password is read from the environment variable FOLIO_PASSWORD. Set it for the user the "
                "scheduled task runs as, too.")
    return ("The FOLIO password comes from this window's FOLIO_PASSWORD, not from the keyring that site.yaml "
            "names. The scheduled task will not have it: run `folio-exchange secret set folio` (as the user the "
            "task runs as), or set FOLIO_PASSWORD for that user.")
