"""The desktop app, ``folio-exchange-app``: one window per site, built on PySide6 (Qt).

Qt comes with the ``desktop`` extra, and nothing else in FOLIO-Exchange imports it, so the command line and the
scheduled task run without it. This module imports no Qt itself, so it can say plainly how to add it. Started
as a GUI script (pythonw on Windows) there is no console: sys.stdout and sys.stderr are None until ``main``
gives them somewhere to go, and messages go to a message box instead.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..config import SITE_ENV

QT = "PySide6-Essentials==6.12.0"          # the Qt the tests pass with (constraints.txt pins the same)
NOT_INSTALLED = ("The FOLIO-Exchange desktop app needs Qt (PySide6), which isn't installed. Add it with:\n"
                 "  {install}\n"
                 "Until then, use the command line or the browser page (folio-exchange ui).")
NOT_LOADED = ("The FOLIO-Exchange desktop app couldn't load Qt (PySide6): {error}\n"
              "Some Python builds, such as Anaconda's, include an older Microsoft Visual C++ runtime than Qt needs. "
              "Python from python.org works, and reinstalling Qt with {install} can also help. The browser page "
              "(folio-exchange ui) works without Qt.")


def install_command() -> str:
    """The command that adds Qt to the Python running FOLIO-Exchange, by that Python's full path. Only Qt itself
    is named, never FOLIO-Exchange: it is not on PyPI, and a package someone else put there under its name
    would install over this one. Under pythonw, the console Python beside it, so pip's progress shows."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        exe = exe.parent / "python.exe"
    return f'"{exe}" -m pip install "{QT}"'


MB_ICONERROR, MB_ICONINFORMATION = 0x10, 0x40


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    console = True

    def error(self, message: str):
        raise _Usage(f"{self.format_usage()}{self.prog}: error: {message}")

    def print_help(self, file=None) -> None:
        """Without a console (pythonw), --help would print into nothing: show it in a message box instead."""
        if self.console or file is not None:
            super().print_help(file)
        else:
            tell(self.format_help(), False, icon=MB_ICONINFORMATION)


def _parser(console: bool = True) -> argparse.ArgumentParser:
    p = _Parser(prog="folio-exchange-app", description="The FOLIO-Exchange desktop app.")
    p.console = console
    p.add_argument("-s", "--site", help=f"the site folder (default: {SITE_ENV}, or choose one in a window)")
    p.add_argument("-v", "--verbose", action="store_true", help="write more detail to the log")
    return p


def safe_streams() -> bool:
    """Give printing somewhere to go when there is no console (pythonw). Returns whether there is one."""
    console = sys.stderr is not None and sys.stdout is not None
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))   # noqa: SIM115 - for the whole run
    return console


def tell(message: str, console: bool, *, icon: int = MB_ICONERROR) -> None:
    """Put a message in front of the person before (or without) Qt: stderr in a console, else a Windows
    message box."""
    if console or sys.platform != "win32":
        print(message, file=sys.stderr, flush=True)
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "FOLIO-Exchange", icon)
    except (OSError, AttributeError):
        pass


def qt_problem() -> str | None:
    """Why Qt cannot be used here, in words a person can act on, or None."""
    try:
        import PySide6.QtNetwork  # noqa: F401
        import PySide6.QtWidgets  # noqa: F401
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] in ("PySide6", "shiboken6"):
            return NOT_INSTALLED.format(install=install_command())
        return NOT_LOADED.format(error=exc, install=install_command())
    except (ImportError, OSError) as exc:
        return NOT_LOADED.format(error=exc, install=install_command())
    return None


def main(argv: list[str] | None = None) -> int:
    console = safe_streams()
    try:
        args = _parser(console).parse_args(argv)
    except _Usage as exc:
        tell(str(exc), console)
        return 1
    problem = qt_problem()
    if problem:
        tell(problem, console)
        return 1
    from .app import run

    return run(args.site or os.environ.get(SITE_ENV), verbose=args.verbose, console=console)
