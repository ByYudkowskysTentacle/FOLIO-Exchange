"""Site-wide operations shared by `cli.py`, `ui.py` and the desktop app: starting a new site's folder, and
renaming the site folder.

The folder's name is the site's name: the page and alerts show it, and the scheduled task, the desktop
launcher and the stored alert addresses are named after it. Renaming keeps the folder where it is (same
parent) and carries those along where it safely can; what it cannot do (register the task again, which needs
the Windows password) it says, with the commands.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import credentials, schedule
from .config import SITE_FILE, load_site, read_yaml
from .errors import ConfigError, FxError
from .history import History
from .lock import RunLock
from .wintools import system_tool

log = logging.getLogger(__name__)

RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_NAME = 80
BUSY = ("Windows would not rename the folder because a file in it is open: another FOLIO-Exchange window or "
        "scheduled run, a Command Prompt whose current folder is inside it, or a file from it open in another "
        "program (a spreadsheet, say). Close it and try again. Nothing was changed.")


def name_problem(name: str) -> str | None:
    """Why ``name`` cannot be a site folder's name, or None. Kept to names that work on Windows, in Command
    Prompt and in a launcher's file name."""
    if not name:
        return "Enter a name for the folder."
    if len(name) > MAX_NAME:
        return f"Keep the name to {MAX_NAME} characters or fewer."
    if BAD_CHARS.search(name):
        return 'A folder name cannot contain < > : " / \\ | ? * or control characters.'
    if name != name.strip() or name.endswith("."):
        return "A folder name cannot start or end with a space, or end with a full stop."
    if name in (".", "..") or name.split(".")[0].upper() in RESERVED:
        return f"Windows keeps {name!r} for itself; choose another name."
    if any(c in name for c in "%^!&"):
        return "Leave out % ^ ! and &: Command Prompt treats them specially, which breaks the launcher and the task."
    return None


def new_site_root(named: str | os.PathLike | None) -> Path | None:
    """The folder ``named`` (from --site or FOLIO_EXCHANGE_SITE) when it has no site.yaml yet (a site still to be
    set up), else None. Nothing named never counts, so a mistyped command cannot start a site anywhere."""
    if not named:
        return None
    root = Path(named).expanduser()
    if root.name == SITE_FILE:
        root = root.parent
    if (root / SITE_FILE).is_file() or (root / "feeds").is_dir() or (root / "data" / "history.sqlite").is_file():
        return None              # a site (or one whose site.yaml has gone missing: that stays an error)
    return root.resolve()


def make_site_folder(root: Path) -> None:
    """Create a new site's folder, but only inside a folder that exists, so a mistyped path is noticed."""
    try:
        Path(root).mkdir(exist_ok=True)
    except FileNotFoundError:
        raise ConfigError(f"{Path(root).parent} does not exist, so {root} was not created. Check the path.") from None


def _inside(path_text: object, root: Path) -> str | None:
    """``path_text`` when it is a full path into ``root`` (so it would still name the old folder after a
    rename), else None. Relative paths move with the folder."""
    if not isinstance(path_text, str) or not path_text.strip() or path_text.strip().lower() == "windows":
        return None
    p = Path(os.path.expanduser(path_text.strip()))
    if not p.is_absolute():
        return None
    try:
        p.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return path_text


def absolute_paths(root: Path) -> list[str]:
    """Settings that name a place inside the site folder by its full path: "file: key = value". A rename would
    leave them pointing at the old folder (and a data folder there would be made afresh), so they come first."""
    root = Path(root)
    found = []
    raw = read_yaml(root / SITE_FILE) or {}
    paths = raw.get("paths") or {}
    for key, value in paths.items() if isinstance(paths, dict) else ():
        for v in value if isinstance(value, list) else [value]:
            if _inside(v, root):
                found.append(f"site.yaml: paths.{key} = {v}")
    if _inside((raw.get("folio") or {}).get("ca_bundle"), root):
        found.append(f"site.yaml: folio.ca_bundle = {raw['folio']['ca_bundle']}")
    feeds_dir = load_site(root).feeds_dir
    for path in sorted(feeds_dir.glob("*.yaml")) if feeds_dir.is_dir() else []:
        try:
            d = (read_yaml(path) or {}).get("delivery") or {}
        except (ConfigError, OSError):
            continue
        for key in ("directory", "private_key", "known_hosts"):
            if isinstance(d, dict) and _inside(d.get(key), root):
                found.append(f"feeds/{path.name}: delivery.{key} = {d[key]}")
    return found


def _log_files(root: Path) -> list[logging.FileHandler]:
    """This process's log files kept inside the site folder (the page's own log): Windows will not rename a
    folder while a file in it is open, so they are closed for the rename and opened again after."""
    out = []
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.FileHandler):
            try:
                Path(h.baseFilename).resolve().relative_to(root.resolve())
            except ValueError:
                continue
            out.append(h)
    return out


def _move_logs(handlers: list[logging.FileHandler], old: Path, new: Path) -> None:
    """Point the closed log files at the renamed folder; each opens again with its next record."""
    for h in handlers:
        h.baseFilename = os.path.abspath(new / Path(h.baseFilename).resolve().relative_to(old))


def _task_exists(name: str, run: Callable[..., Any]) -> bool:
    """Whether the scheduled task ``name`` is registered. No console window: the desktop app has none of its
    own, so Windows would flash one up for schtasks."""
    try:
        done = run([system_tool("schtasks"), "/Query", "/TN", name], capture_output=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def task_registered(root: Path, run: Callable[..., Any] | None = None) -> bool | None:
    """Whether this site's scheduled task is registered with the Windows Task Scheduler (None elsewhere, where
    the hourly start is a crontab line FOLIO-Exchange cannot see). Asks schtasks, so it can take a moment."""
    if platform.system() != "Windows":
        return None
    return _task_exists(schedule.task_name(Path(root)), run or subprocess.run)


def _carry_alert_secrets(site_alerts: dict, old: str, new: str) -> list[str]:
    """Copy the alert addresses stored in Credential Manager under the old folder name to the new one. The old
    entries stay (another site with the same folder name elsewhere may share them); nothing is overwritten."""
    carried = []
    for key, service, what in (("webhook", credentials.WEBHOOK_SERVICE, "alert webhook address"),
                               ("heartbeat", credentials.HEARTBEAT_SERVICE, "heartbeat address")):
        if site_alerts.get(key) != "keyring":
            continue
        value = credentials.resolve("keyring", service, old)
        if value and not credentials.resolve("keyring", service, new):
            credentials.store(service, new, value)
            carried.append(what)
    return carried


def _carry_launcher(old: Path, new: Path, folder: Path | None) -> str | None:
    """Replace a launcher on the desktop made for the old folder with one for the new folder."""
    from . import launcher

    if folder is None:
        return None
    system = platform.system()
    path = folder / launcher.file_name(old, system)
    try:
        if not path.is_file() or launcher.MARKER not in path.read_text(encoding="utf-8", errors="replace"):
            return None
        made = launcher.write(new, folder, launcher.program_path(), system)
        if made != path:
            path.unlink()
    except (OSError, ConfigError) as exc:
        log.warning("Could not replace the launcher %s: %s", path, exc)
        return None
    return str(made)


def rename_site(root: Path, name: str, *, via: str, desktop: Path | None = None,
                run: Callable[..., Any] | None = None) -> dict:
    """Rename the site folder ``root`` to ``name`` in the same parent folder. Refuses while a run holds the
    site, when a setting names the folder by its full path, or when the name is taken. Returns the new root
    and what else was done or is left to do (``notes``, with commands)."""
    root = Path(root).resolve()
    name = (name or "").strip()
    problem = name_problem(name)
    if problem:
        raise ConfigError(problem)
    new = root.parent / name
    if name == root.name:
        raise ConfigError(f"The site folder is already called {name}.")
    if new.exists() and not (name.lower() == root.name.lower() and new.samefile(root)):
        raise ConfigError(f"{new} already exists. Choose another name.")
    if not (root / SITE_FILE).is_file():
        raise ConfigError(f"{root} has no {SITE_FILE}; connect it to FOLIO before renaming it.")
    fixed = absolute_paths(root)
    if fixed:
        raise ConfigError("These settings name a place in the site folder by its full path, so they would still "
                          "point at the old folder after the rename: " + "; ".join(fixed) + ". Change each to a "
                          "path relative to the site folder (for example data instead of the full path), then "
                          "rename. Nothing was changed.")
    site = load_site(root)
    old_task = schedule.task_name(root)
    with RunLock(site.lock_path):            # no run is going now; one starting after this finds no folder
        pass
    handlers = _log_files(root)
    for h in handlers:
        h.close()
    try:
        os.rename(root, new)
    except PermissionError:
        raise FxError(BUSY) from None
    except OSError as exc:
        raise FxError(f"The folder could not be renamed ({exc.strerror or exc}). Nothing was changed.") from None
    _move_logs(handlers, root, new)

    site = load_site(new)
    notes: list[str] = []
    carried = _carry_alert_secrets(site.alerts, root.name, new.name)
    if carried:
        notes.append(f"The {' and '.join(carried)} stored in Windows Credential Manager now also sit under the "
                     "new name.")
    from . import launcher

    made = _carry_launcher(root, new, desktop if desktop is not None else launcher.desktop())
    if made:
        notes.append(f"The launcher on the desktop now opens the new folder: {made}.")
    task = None
    if platform.system() == "Windows" and _task_exists(old_task, run or subprocess.run):
        exe = launcher.program_path()
        task = {"old": old_task, "new": schedule.task_name(new),
                "register": schedule.register_command(new, exe, "cmd"),
                "remove": f'schtasks /Delete /TN "{old_task}" /F'}
        notes.append(f"The scheduled task \"{old_task}\" still runs the old folder, so scheduled runs stop until "
                     f"it is registered again. In a Command Prompt, run {task['register']} (it asks for your "
                     f"Windows password), then remove the old task with {task['remove']}.")
    if os.path.normcase(os.environ.get("FOLIO_EXCHANGE_SITE", "")) == os.path.normcase(str(root)):
        notes.append(f'FOLIO_EXCHANGE_SITE still names the old folder. Set it again with: setx FOLIO_EXCHANGE_SITE '
                     f'"{new}"')
    with History(site.history_path) as hist:
        hist.audit(None, None, "site folder renamed", {"from": str(root), "to": str(new), "via": via})
    log.info("Site folder renamed from %s to %s", root, new)
    return {"old": str(root), "root": str(new), "name": new.name, "notes": notes, "task": task}
