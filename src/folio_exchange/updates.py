"""Whether a newer FOLIO-Exchange is out (phase U0 of docs/updates-plan.md): the check, and what is kept about it.

No Qt here. The desktop app's tray and its window ask (``check``); the command line and the scheduled task never
import this module, so ``run --due`` makes no update request.

The check is one anonymous GET of this project's releases on GitHub. The repository is fixed here, not read from
a setting, so a changed file can't point updates elsewhere. The request carries a ``User-Agent`` of
``FOLIO-Exchange/<version>`` and nothing else: no site, tenant, user or counts, no account from ``.netrc``, no
cookie. Redirects are followed by hand, and only within ``api.github.com``.

The newest release whose tag is ``vX.Y.Z`` and higher than the running version is the one offered. Drafts are
never used, pre-releases only when chosen, and never a lower or equal version. Nothing is downloaded or
installed: this phase only tells (the tray's notification, the Updates section under Options).

``updates.json`` in ``places.config_dir()`` keeps the last check and the person's choices, per Windows user:
``check`` (false: IT turned checking off), ``automatic``, ``prereleases``, ``last_ok``, ``failed`` (the checks
that didn't work since), ``wait_until`` (GitHub asked to wait), ``checking`` (a check under way, so the tray and
a window don't both ask), ``found`` (the newest release, and the newest that isn't a pre-release) and ``told``
(the version the tray has notified about).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from . import __version__
from .desktop import places

log = logging.getLogger(__name__)

REPO = "ByYudkowskysTentacle/FOLIO-Exchange"       # fixed: never read from a setting
HOST = "api.github.com"
RELEASES_URL = f"https://{HOST}/repos/{REPO}/releases"
RELEASE_PAGE = f"https://github.com/{REPO}/releases/tag/"
PIP_SOURCE = f"git+https://github.com/{REPO}.git"

# The public half of the project's Ed25519 release key, which ``SHA256SUMS.sig`` is checked against before anything
# downloaded is used: by the in-place update the app installs (desktop/updating.py passes this and no other key to
# selfupdate), and by U1's installer. Not set yet: the maintainer makes the key offline (docs/updates-plan.md, open
# question 6). While it is None, Options offers no Install. Only ever the 32 public bytes; the private key never
# comes near this repository.
RELEASE_KEY: bytes | None = None

ENV = "FOLIO_EXCHANGE_UPDATES"                     # off: never check; notify: check and tell, never offer to install
FILE = "updates.json"
DAY = timedelta(days=1)                            # between checks, after one that worked
RETRY = timedelta(hours=2)                         # after a check that didn't
MOST_FAILED = 4                                    # checks that didn't work, in any 24 hours
CHECKING = timedelta(minutes=2)                    # how long another process's check under way is waited for
TIMEOUT = 30
MAX_BYTES = 4 * 1024 * 1024
MAX_NOTES = 20_000                                 # characters of a release's text kept
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_RUNNING = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

Version = tuple[int, int, int]


class UpdateError(Exception):
    """A check that didn't work, in words for the person. ``wait_until``: when GitHub said to ask again."""

    def __init__(self, message: str, wait_until: datetime | None = None) -> None:
        super().__init__(message)
        self.wait_until = wait_until


# ------------------------------------------------------------------ versions
def tag_version(tag: object) -> Version | None:
    """The version a release tag names: ``v0.3.0`` only, nothing before or after it."""
    m = _TAG.fullmatch(tag) if isinstance(tag, str) else None
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def running_version(text: str | None = None) -> Version | None:
    """This install's version as numbers (``0.3.0.dev1`` counts as 0.3.0), or None when it can't be read."""
    m = _RUNNING.match(__version__ if text is None else text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def version_text(version: Version) -> str:
    return ".".join(str(n) for n in version)


def newest(releases: object, running: Version | None, prereleases: bool) -> dict | None:
    """The release to offer from GitHub's list: the highest ``vX.Y.Z`` above ``running``. Drafts never,
    pre-releases only when ``prereleases``. None when there is none, or the running version isn't known (so a
    lower version is never offered)."""
    best: tuple[Version, dict] | None = None
    for r in releases if isinstance(releases, list) else []:
        if not isinstance(r, dict) or r.get("draft") is not False:
            continue
        pre = r.get("prerelease") is not False
        version = tag_version(r.get("tag_name"))
        if version is None or running is None or version <= running or (pre and not prereleases):
            continue
        if best is None or version > best[0]:
            best = (version, {"version": version_text(version), "prerelease": pre,
                              "notes": str(r.get("body") or "").replace("\r\n", "\n").strip()[:MAX_NOTES],
                              "published": str(r.get("published_at") or "") or None})
    return best[1] if best else None


def release_page(version: str) -> str:
    """The release's page on GitHub, built from the version, never taken from GitHub's answer."""
    return f"{RELEASE_PAGE}v{version}"


def pip_command(version: str, python: str | None = None) -> str:
    """The command that installs ``version`` over a pip install. Always from this project's repository at the
    release's tag: FOLIO-Exchange is never installed by its bare name, which isn't on PyPI. ``python``: the
    full path of the Python to run pip with (the app passes its own)."""
    pip = f'"{python}" -m pip' if python else "pip"
    return f'{pip} install "folio-exchange[desktop] @ {PIP_SOURCE}@v{version}"'


# ------------------------------------------------------------------ updates.json
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: object) -> datetime | None:
    try:
        t = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return t if t is None or t.tzinfo else t.replace(tzinfo=timezone.utc)


def _stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    """``updates.json`` as saved; empty when there is none or it can't be read."""
    try:
        data = json.loads((places.config_dir() / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    folder = places.config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".updates.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, folder / FILE)


def _change(**values: Any) -> dict:
    """Read the file, set ``values`` (None removes a key) and save it. A file that can't be saved is logged:
    the check then runs again sooner than it should, which the limits at GitHub's end still bound."""
    data = load()
    for key, value in values.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    try:
        _save(data)
    except OSError as exc:
        log.warning("Could not save %s in %s: %s", FILE, places.config_dir(), exc)
    return data


def mode(data: dict | None = None, environ: Any = None) -> str:
    """"off" (``FOLIO_EXCHANGE_UPDATES=off``, or ``"check": false`` in updates.json: IT manages updates, and
    nothing is asked), "notify" (check and tell, never offer to install) or "on"."""
    said = str((os.environ if environ is None else environ).get(ENV) or "").strip().lower()
    if said == "off" or (load() if data is None else data).get("check") is False:
        return "off"
    return "notify" if said == "notify" else "on"


def gets_prereleases(data: dict) -> bool:
    """The person's choice; until they make one, pre-releases count while this version is below 1.0, since
    every release until then is one."""
    chosen = data.get("prereleases")
    if isinstance(chosen, bool):
        return chosen
    running = running_version()
    return running is not None and running < (1, 0, 0)


def set_choice(key: str, on: bool) -> None:
    """Save one of the person's choices: ``automatic`` or ``prereleases``. Raises OSError when it can't be
    saved: they asked for this, and must hear that it didn't happen."""
    if key not in ("automatic", "prereleases"):
        raise ValueError(key)
    data = load()
    data[key] = bool(on)
    _save(data)


# ------------------------------------------------------------------ when
def next_check(data: dict, now: datetime) -> datetime:
    """The earliest time for the next check made without being asked: a day after one that worked; two hours
    after one that didn't, with at most MOST_FAILED of those in 24 hours; not before GitHub said to ask again;
    not while another process's check is under way. Times in the future (a clock set back) and waits longer than
    a day (a header gone wrong) don't count."""
    def past(key: str) -> datetime | None:
        t = _time(data.get(key))
        return t if t is not None and t <= now else None

    at = datetime.min.replace(tzinfo=timezone.utc)
    ok = past("last_ok")
    if ok is not None:
        at = ok + DAY
    failed = sorted(t for t in map(_time, data.get("failed") if isinstance(data.get("failed"), list) else [])
                    if t is not None and now - DAY < t <= now and (ok is None or t > ok))
    if failed:
        at = max(at, failed[-1] + RETRY)
    if len(failed) >= MOST_FAILED:
        at = max(at, failed[-MOST_FAILED] + DAY)
    wait = _time(data.get("wait_until"))
    if wait is not None and wait <= now + DAY:
        at = max(at, wait)
    checking = past("checking")
    if checking is not None:
        at = max(at, checking + CHECKING)
    return at


def due(now: datetime | None = None, data: dict | None = None) -> bool:
    """Whether to check now without being asked."""
    data = load() if data is None else data
    now = now or _now()
    if mode(data) == "off" or data.get("automatic") is False:
        return False
    return now >= next_check(data, now)


# ------------------------------------------------------------------ GitHub
def _no_account(request: Any) -> Any:
    """An ``auth`` that adds nothing. With none at all, requests would look in the person's ``.netrc`` for a
    GitHub account and send it."""
    return request


def _session() -> requests.Session:
    """A session that sends nothing of its own: no default headers, no cookies kept, no ``.netrc`` account. The
    system's proxy settings still apply."""
    s = requests.Session()
    s.headers.clear()
    s.auth = _no_account
    return s


def _wait_until(headers: Any, now: datetime) -> datetime | None:
    """When GitHub says to ask again: ``Retry-After`` (seconds, or a date), else ``X-RateLimit-Reset`` (a Unix
    time) once nothing remains of the limit."""
    headers = headers or {}
    after = str(headers.get("Retry-After") or "").strip()
    if after.isdigit():
        return now + timedelta(seconds=int(after))
    if after:
        try:
            t = parsedate_to_datetime(after)
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    reset = str(headers.get("X-RateLimit-Reset") or "").strip()
    if reset.isdigit() and str(headers.get("X-RateLimit-Remaining") or "").strip() == "0":
        try:
            return datetime.fromtimestamp(int(reset), timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return None


def fetch_releases(http: Any = None, now: datetime | None = None) -> list:
    """This project's releases, as GitHub lists them (newest first, one page). Raises UpdateError."""
    http = http or _session()
    now = now or _now()
    url = RELEASES_URL
    for _hop in range(4):
        parts = urlparse(url)
        if parts.scheme != "https" or parts.hostname != HOST:
            raise UpdateError(f"GitHub sent the check to another site ({parts.hostname}), so it was stopped.")
        try:
            r = http.get(url, timeout=TIMEOUT, allow_redirects=False,
                         headers={"User-Agent": f"FOLIO-Exchange/{__version__}"})
        except requests.RequestException as exc:
            raise UpdateError(f"Couldn't reach GitHub ({type(exc).__name__}). Check this computer's internet "
                              "connection or proxy settings.") from None
        if r.status_code not in (301, 302, 303, 307, 308):
            break
        url = urljoin(url, (getattr(r, "headers", None) or {}).get("Location") or "")
    else:
        raise UpdateError("GitHub redirected the check too many times.")
    if r.status_code != 200:
        wait = _wait_until(getattr(r, "headers", None), now)
        if r.status_code in (403, 429):
            raise UpdateError("GitHub is limiting requests from this network.", wait)
        raise UpdateError(f"GitHub answered {r.status_code}.", wait)
    if len(r.content or b"") > MAX_BYTES:
        raise UpdateError("GitHub's answer was too large.")
    try:
        releases = r.json()
    except ValueError:
        releases = None
    if not isinstance(releases, list):
        raise UpdateError("GitHub's answer wasn't a list of releases.")
    return releases


# ------------------------------------------------------------------ the check
def check(*, asked: bool = False, http: Any = None, now: datetime | None = None) -> dict:
    """Ask GitHub for the releases when a check is due, or whenever the person ``asked`` (unless GitHub said to
    wait). Returns {checked, problem}: whether a request was made, and why it didn't work (None when it did, or
    when nothing was asked). Never raises for a check that fails: it is logged and tried again later
    (``next_check``)."""
    now = now or _now()
    data = load()
    if mode(data) == "off":
        return {"checked": False, "problem": None}
    if not asked and not due(now, data):
        return {"checked": False, "problem": None}
    wait = _time(data.get("wait_until"))
    if asked and wait is not None and now < wait <= now + DAY:
        minutes = max(1, round((wait - now).total_seconds() / 60))
        return {"checked": False, "problem": "GitHub is limiting requests from this network. Try again in about "
                                             f"{minutes} minute{'s' if minutes != 1 else ''}."}
    _change(checking=_stamp(now))
    try:
        releases = fetch_releases(http, now)
    except UpdateError as exc:
        log.warning("The check for updates didn't work: %s", exc)
        failed = [s for s in (load().get("failed") or []) if isinstance(s, str)][-(MOST_FAILED - 1):]
        _change(checking=None, failed=[*failed, _stamp(now)],
                wait_until=_stamp(exc.wait_until) if exc.wait_until else None)
        return {"checked": True, "problem": str(exc)}
    running = running_version()
    found = {"any": newest(releases, running, True), "stable": newest(releases, running, False)}
    _change(checking=None, failed=None, wait_until=None, last_ok=_stamp(now), found=found)
    log.info("Checked for updates: %s", ", ".join(f"{k} {v['version']}" for k, v in found.items() if v)
             or "nothing newer")
    return {"checked": True, "problem": None}


def _available(data: dict) -> dict | None:
    """The newer release to tell about, from the last check that worked: higher than the version running now
    (which may itself have been updated since), and a pre-release only when those are chosen."""
    found = data.get("found") if isinstance(data.get("found"), dict) else {}
    release = found.get("any" if gets_prereleases(data) else "stable")
    if not isinstance(release, dict):
        return None
    version, running = tag_version(f"v{release.get('version')}"), running_version()
    if version is None or running is None or version <= running:
        return None
    return {"version": version_text(version), "prerelease": bool(release.get("prerelease")),
            "notes": str(release.get("notes") or ""), "page": release_page(version_text(version))}


def status(now: datetime | None = None) -> dict:
    """What the Updates section shows: {version, mode, automatic, prereleases, checked, failed, available}.
    ``checked``: when a check last worked; ``failed``: whether one has failed since; ``available``: None or
    {version, prerelease, notes, page}."""
    data = load()
    now = now or _now()
    ok = _time(data.get("last_ok"))
    failed = [t for t in map(_time, data.get("failed") if isinstance(data.get("failed"), list) else [])
              if t is not None and (ok is None or t > ok)]
    return {"version": __version__, "mode": mode(data), "automatic": data.get("automatic") is not False,
            "prereleases": gets_prereleases(data), "checked": _stamp(ok) if ok else None, "failed": bool(failed),
            "available": None if mode(data) == "off" else _available(data)}


def to_tell(data: dict | None = None) -> str | None:
    """The version the tray has yet to notify about, or None: each new version is told once."""
    data = load() if data is None else data
    if mode(data) == "off":
        return None
    release = _available(data)
    return release["version"] if release and release["version"] != data.get("told") else None


def told(version: str) -> None:
    """The tray notified about ``version``."""
    _change(told=version)


def look(*, http: Any = None, now: datetime | None = None) -> str | None:
    """The tray's poll: check when one is due, then say which version to notify about, if any."""
    check(http=http, now=now)
    return to_tell()
