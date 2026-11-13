"""Starting the desktop app as a process of its own: another site's window, the tray, and the tray's Startup entry.

No Qt here. The Startup entry is a value under this Windows user's Run key
(``HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``), so it needs no administrator rights
and starts the tray only for this user. It is opt-in: the app writes it only when the person ticks the box
under Options, and removes it when they untick it. Off Windows there is no Startup entry.
"""
from __future__ import annotations

import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "FOLIO-Exchange tray"


def app_program() -> tuple[str, list[str], str]:
    """The desktop app's program, the arguments before its own, and the folder to start it in. Never this
    process's current folder: ``python -m`` imports from there first, and that may be a shared folder others can
    write to. The installed ``folio-exchange-app`` is used when there is one; else ``-m`` with ``-P`` (Python 3.11
    and later), which leaves the current folder off the path, run by pythonw on Windows so no console opens."""
    exe = Path(sys.executable)
    here = str(exe.parent)
    script = Path(sysconfig.get_path("scripts")) / ("folio-exchange-app.exe" if sys.platform == "win32"
                                                    else "folio-exchange-app")
    if script.is_file():
        return str(script), [], here
    if sys.platform == "win32" and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").is_file():
        exe = exe.parent / "pythonw.exe"
    safe = ["-P"] if sys.version_info >= (3, 11) else []
    return str(exe), [*safe, "-m", "folio_exchange.desktop"], here


def app_command(site_root: Path) -> tuple[str, list[str], str]:
    """How to open the app for a site, in a process of its own (see ``app_program``)."""
    exe, args, folder = app_program()
    return exe, [*args, "--site", str(site_root)], folder


def tray_command() -> tuple[str, list[str], str]:
    """How to start the tray (or, when one runs, ask it to read its list of sites again)."""
    exe, args, folder = app_program()
    return exe, [*args, "--tray"], folder


def startup_line() -> str:
    """The command the Startup entry runs: the tray, by this install's full path, quoted for Windows."""
    exe, args, _folder = tray_command()
    return subprocess.list2cmdline([exe, *args])


def _winreg() -> Any:
    """The winreg module, or None off Windows. Tests replace this with a stand-in."""
    if sys.platform != "win32":
        return None
    import winreg

    return winreg


def startup_state() -> str | None:
    """Whether the tray starts when this user signs in to Windows: "on" (the entry runs this install's tray),
    "other" (it runs another copy of FOLIO-Exchange, such as one installed before), "off", or None off Windows
    or when the entry cannot be read."""
    reg = _winreg()
    if reg is None:
        return None
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_READ) as key:
            value, _kind = reg.QueryValueEx(key, RUN_VALUE)
    except FileNotFoundError:
        return "off"
    except OSError:
        return None
    return "on" if str(value).strip().lower() == startup_line().lower() else "other"


def set_startup(on: bool) -> None:
    """Add the Startup entry for this install's tray, or remove it. Raises OSError when Windows refuses, and
    RuntimeError off Windows."""
    reg = _winreg()
    if reg is None:
        raise RuntimeError("Starting the tray when you sign in works on Windows only.")
    if on:
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.SetValueEx(key, RUN_VALUE, 0, reg.REG_SZ, startup_line())
        return
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass
