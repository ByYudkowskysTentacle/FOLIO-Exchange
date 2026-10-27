"""A double-click launcher for the local page: a small script that runs `folio-exchange ui` for one site.

On Windows it is a ``.cmd`` file, by default on the desktop; on macOS a ``.command`` file (Finder opens it in
Terminal) and elsewhere a ``.sh`` file. It names this install's program and the site folder in full, so it
works without activating anything or setting FOLIO_EXCHANGE_SITE. It holds no secrets.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from .errors import ConfigError
from .schedule import task_name, win_quote

MARKER = "Made by folio-exchange launcher"
KEEP_OPEN = ("Keep this window open while you use the page. To stop, press Ctrl+C here; "
             "a run or release still going finishes first.")


def program_path() -> str:
    """The folio-exchange program of the install running now, else the one on PATH."""
    name = "folio-exchange.exe" if os.name == "nt" else "folio-exchange"
    here = Path(sys.executable).with_name(name)
    if here.is_file():
        return str(here)
    return shutil.which("folio-exchange") or str(here)


def desktop() -> Path | None:
    """The user's desktop folder (Windows may move it, for example into OneDrive), or None."""
    if os.name == "nt":
        try:
            import winreg

            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                value, _type = winreg.QueryValueEx(k, "Desktop")
            path = Path(os.path.expandvars(value))
            if path.is_dir():
                return path
        except OSError:
            pass
    path = Path.home() / "Desktop"
    return path if path.is_dir() else None


def _label(site_root: Path) -> str:
    """"FOLIO-Exchange (<site folder>)" without characters that Command Prompt treats specially (& % ^ ! ...),
    for the file name and the window title."""
    return " ".join(re.sub(r"[^\w .()-]", "", task_name(site_root)).split())


def file_name(site_root: Path, system: str) -> str:
    return _label(site_root) + {"Windows": ".cmd", "Darwin": ".command"}.get(system, ".sh")


def script(site_root: Path, executable: str, system: str) -> str:
    """The launcher's text. Paths are quoted for the shell that runs it; nothing else is substituted."""
    site = str(site_root)
    if system == "Windows":
        title = _label(site_root)
        # % is expanded even inside quotes in a batch file; & ^ | < > are safe inside them
        cmd = f"{win_quote(executable)} --site {win_quote(site)} ui".replace("%", "%%")
        lines = ["@echo off"]
        if not cmd.isascii():
            lines.append("chcp 65001 >nul")       # read the paths below as UTF-8
        lines += [f"rem {MARKER}: opens the FOLIO-Exchange page for one site.",
                  f"title {title}", f"echo {KEEP_OPEN}", "echo.", cmd,
                  "if errorlevel 1 pause"]        # keep an error on screen instead of closing the window
        return "\r\n".join(lines) + "\r\n"
    return "\n".join([
        "#!/bin/sh", f"# {MARKER}: opens the FOLIO-Exchange page for one site.",
        f"echo {shlex.quote(KEEP_OPEN)}",
        f"exec {shlex.quote(executable)} --site {shlex.quote(site)} ui",
    ]) + "\n"


def write(site_root: Path, folder: Path, executable: str, system: str) -> Path:
    """Write the launcher into ``folder``. Replaces an earlier launcher, never any other file."""
    path = folder / file_name(site_root, system)
    if path.exists():
        try:
            ours = MARKER in path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            ours = False
        if not ours:
            raise ConfigError(f"{path} already exists and was not made by folio-exchange; nothing was changed. "
                              "Move it, or choose another folder with --folder.")
    text = script(site_root, executable, system)
    path.write_bytes(text.encode("utf-8"))
    if system != "Windows":
        path.chmod(0o755)
    return path
