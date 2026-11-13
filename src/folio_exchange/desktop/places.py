"""The app's own per-user things, outside any site: the recent sites list, light or dark, the sites the tray
watches, and the one-window-per-site and one-tray-per-user names.

Two folders: ``config_dir`` for preferences that may follow the person to another PC (Windows' roaming
``%APPDATA%``), and ``local_dir`` for what belongs to this PC alone (``%LOCALAPPDATA%``): the tray's list of
sites, which names folders on this PC, and the tray's log. With roaming profiles, a list of another PC's folders
would make the tray there say it can't read them, every time.

No Qt here, so it is quick to import and to test. Nothing kept here is secret: the recent list holds folder
paths only.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from ..config import SITE_FILE
from ..logsetup import LOG_FILE
from ..siteops import name_problem, new_site_root
from ..ui import site_id

log = logging.getLogger(__name__)

RECENT_FILE = "recent-sites.json"
RECENT_MOST = 10
LOOK_FILE = "look.json"
TRAY_FILE = "tray.json"           # the sites the tray watches (in local_dir: they name this PC's folders)
LOCAL_NAME = ".app"               # local_dir's own folder on Windows; no site can be called this
UNFINISHED = {"logs", "data"}     # what the app and its page write into a new site before site.yaml exists


def config_dir() -> Path:
    """The per-user folder for the app's settings. Tests redirect this one function."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / "FOLIO-Exchange"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FOLIO-Exchange"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "folio-exchange"


def local_dir() -> Path:
    """The per-user folder for what belongs to this PC alone: ``%LOCALAPPDATA%\\FOLIO-Exchange\\.app`` on
    Windows, which never roams. It sits in the folder the installer suggests for sites, under a name no site can
    have (``siteops.name_problem`` refuses names that start with a period). Elsewhere ``$XDG_STATE_HOME`` (or
    ``~/.local/state``)/folio-exchange, and on macOS the same folder as ``config_dir``. Tests redirect this one
    function."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "FOLIO-Exchange" / LOCAL_NAME
    if sys.platform == "darwin":
        return config_dir()
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "folio-exchange"


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def recent_sites() -> list[Path]:
    """Site folders opened before, newest first, at most RECENT_MOST. Read from the list alone: the window's
    thread never looks at the folders themselves, since one on a share that has stopped answering would hold it
    for a minute or more. A folder that has gone is found when it is opened."""
    try:
        raw = json.loads((config_dir() / RECENT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[Path] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str) or not item:
            continue
        path = Path(item)
        if not any(_same(path, p) for p in out):
            out.append(path)
    return out[:RECENT_MOST]


def remember_site(root: Path, replaces: Path | None = None) -> list[Path]:
    """Put ``root`` first in the recent list (dropping ``replaces``, the folder's old name after a rename) and
    save it. Only the app's own settings folder is touched (``root`` is made absolute without looking at the
    disk). A list that cannot be saved is only a convenience lost, so that is logged, not raised."""
    root = Path(os.path.abspath(root))
    keep = [p for p in recent_sites() if not _same(p, root) and not (replaces and _same(p, Path(replaces)))]
    sites = [root, *keep][:RECENT_MOST]
    folder = config_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".recent.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([str(p) for p in sites], fh, indent=1)
        os.replace(tmp, folder / RECENT_FILE)
    except OSError as exc:
        log.warning("Could not save the list of recent sites in %s: %s", folder, exc)
    return sites


def _save(name: str, data: object, folder: Path | None = None) -> None:
    """Write one of the app's settings files whole (a temporary file, then a rename), in ``folder`` (else
    config_dir). Raises OSError."""
    folder = folder or config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{Path(name).stem}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, folder / name)


def watched_sites() -> list[Path]:
    """The site folders the tray watches (``tray.json``), in the order they were added. Read from the list
    alone, as ``recent_sites`` is: the tray finds a folder that has gone when it looks at it."""
    try:
        raw = json.loads((local_dir() / TRAY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = raw.get("sites") if isinstance(raw, dict) else None
    out: list[Path] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str) and item and not any(_same(Path(item), p) for p in out):
            out.append(Path(item))
    return out


def is_watched(root: Path) -> bool:
    root = Path(os.path.abspath(root))
    return any(_same(p, root) for p in watched_sites())


def watch_site(root: Path, on: bool) -> list[Path]:
    """Add ``root`` to the tray's sites (``on``) or take it off, and save the list. Raises OSError when the list
    cannot be saved: unlike the recent list, the person asked for this and must hear that it didn't happen."""
    root = Path(os.path.abspath(root))
    keep = [p for p in watched_sites() if not _same(p, root)]
    sites = [*keep, root] if on else keep
    _save(TRAY_FILE, {"sites": [str(p) for p in sites]}, local_dir())
    return sites


def rename_watched(old: Path, new: Path) -> None:
    """After the site folder ``old`` was renamed to ``new``: the new name takes the old one's place in the tray's
    list. Nothing changes when the tray doesn't watch ``old``; a list that cannot be saved is logged."""
    old, new = Path(os.path.abspath(old)), Path(os.path.abspath(new))
    sites = watched_sites()
    if not any(_same(p, old) for p in sites):
        return
    out: list[Path] = []
    for p in sites:
        q = new if _same(p, old) else p
        if not any(_same(q, r) for r in out):
            out.append(q)
    try:
        _save(TRAY_FILE, {"sites": [str(p) for p in out]}, local_dir())
    except OSError as exc:
        log.warning("Could not save the tray's sites in %s: %s", local_dir(), exc)


def tray_instance_name() -> str:
    """The local socket name of this Windows user's tray: one tray per user, whatever sites it watches."""
    user = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}"
    return f"folio-exchange-tray-{hashlib.sha256(user.encode()).hexdigest()[:24]}"


def dark_mode() -> bool:
    """Whether the app is dark: it is unless the person turned it off (Site, Dark mode)."""
    try:
        raw = json.loads((config_dir() / LOOK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (isinstance(raw, dict) and raw.get("dark") is False)


def set_dark_mode(on: bool) -> None:
    """Remember light or dark for the next start. Only a convenience: a failure is logged, not raised."""
    folder = config_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".look.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"dark": bool(on)}, fh)
        os.replace(tmp, folder / LOOK_FILE)
    except OSError as exc:
        log.warning("Could not save light or dark in %s: %s", folder, exc)


def unfinished_site(folder: Path) -> bool:
    """Whether ``folder`` is a new site the app opened but whose setup was not finished: no site.yaml yet, the
    app's own log in it, and nothing else but the folders the app and its page make (logs, data)."""
    folder = Path(folder)
    if _same(Path(os.path.abspath(folder)), Path(os.path.abspath(local_dir()))):
        return False                    # the tray's own log looks like an unfinished site's, but it isn't one
    try:
        return (new_site_root(folder) is not None and (folder / "logs" / LOG_FILE).is_file()
                and all(p.name in UNFINISHED and p.is_dir() for p in folder.iterdir()))
    except OSError:
        return False


def folder_problem(folder: Path) -> str | None:
    """Why ``folder`` cannot be opened as a site, or None when it holds a site.yaml or is a new site whose
    setup was not finished (it opens on "Set up this site", as with --site)."""
    folder = Path(folder)
    if not folder.is_dir():
        return f"{folder} isn't a folder that exists. Choose the site folder again."
    if _same(Path(os.path.abspath(folder)), Path(os.path.abspath(local_dir()))):
        return f"{folder} is FOLIO-Exchange's own folder, not a site. Choose the site folder."
    if not (folder / SITE_FILE).is_file() and not unfinished_site(folder):
        return (f"There's no {SITE_FILE} in {folder}, so it isn't a FOLIO-Exchange site. Choose the folder "
                f"that holds {SITE_FILE}, or use \"Set up a new site…\" to start one.")
    return None


def new_site_problem(parent: Path | str, name: str) -> str | None:
    """Why a new site called ``name`` cannot be made in ``parent``, or None."""
    if not str(parent).strip():
        return "Choose the folder to put the new site in."
    parent = Path(parent).expanduser()
    if not parent.is_dir():
        return f"{parent} isn't a folder that exists. Choose another folder."
    problem = name_problem(name)
    if problem:
        return problem
    target = parent / name
    if (target / SITE_FILE).is_file():
        return f"{target} is already a site. Open it instead."
    if target.exists() and not target.is_dir():
        return f"{target} is a file. Choose another name."
    if target.is_dir() and any(target.iterdir()) and not unfinished_site(target):
        return f"{target} isn't empty. Choose an empty folder or another name."
    return None


def instance_name(site_root: Path) -> str:
    """The local socket name for one site's window: the same for every start by this user for this folder, and
    different for another user or folder. It names neither (it is a hash), since other users can list it."""
    user = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}"
    digest = hashlib.sha256(f"{user}\n{site_id(site_root)}".encode()).hexdigest()
    return f"folio-exchange-{digest[:24]}"
