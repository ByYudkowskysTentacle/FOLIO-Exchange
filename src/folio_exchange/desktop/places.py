"""The app's own per-user things, outside any site: the recent sites list, light or dark, and the one-window-per-site
name.

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
