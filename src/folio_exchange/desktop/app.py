"""Starting the app: choose the site, log to it, take its one window (or hand over to the open one), run."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from .. import siteops
from ..config import find_site, load_site
from ..errors import FxError
from ..logsetup import setup_logging
from . import places, single, theme
from .pages import alert
from .picker import SitePicker
from .window import MainWindow
from .worker import MainThreadCollector

log = logging.getLogger(__name__)

BUSY = ("FOLIO-Exchange seems to be open for this site already, but its window isn't responding. Close it, or end "
        "it in Task Manager, and start it again.")
ELSEWHERE = ("FOLIO-Exchange is already open for this site in another of your Windows sessions, such as a Remote "
             "Desktop sign-in. Use it there, or close it there and start it again here.")
_reporting = False


def fail(message: str, console: bool) -> None:
    """An error before the window opens: in the console when there is one, else in a message box."""
    log.error("%s", message)
    if console:
        print(message, file=sys.stderr, flush=True)
    else:
        alert(None, message, QMessageBox.Icon.Critical)


def report_unexpected(exc_type, exc, tb) -> None:
    """sys.excepthook while the app runs: an exception in a slot is logged and shown, not lost."""
    global _reporting
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    log.error("Unexpected error", exc_info=(exc_type, exc, tb))
    if _reporting or QApplication.instance() is None:
        return
    _reporting = True
    try:
        alert(None, f"Something went wrong: {exc_type.__name__}: {exc}\n\nThe details are in the site's "
              "logs/folio-exchange.log. If the window stops working properly, close it and start it again.",
              QMessageBox.Icon.Critical)
    finally:
        _reporting = False


def site_from(named: str | None) -> Path | None:
    """The site folder to open: ``named`` (a folder with no site.yaml yet becomes a new site, as for
    `folio-exchange ui`), else the one chosen in the site picker, else None (the picker was closed)."""
    if named:
        new = siteops.new_site_root(named)
        if new is not None:
            siteops.make_site_folder(new)
            log.info("New site at %s: the app starts with the FOLIO connection", new)
            return new
        return find_site(named)
    picker = SitePicker()
    if picker.exec() and picker.chosen is not None:
        return picker.chosen
    return None


def logs_dir(root: Path) -> Path:
    """The site's log folder, as site.yaml sets it; ``logs`` while there is no site.yaml that loads."""
    try:
        return load_site(root).logs_dir
    except (FxError, OSError, ValueError):
        return root / "logs"


def run(named: str | None, *, verbose: bool = False, console: bool = True) -> int:
    qapp = QApplication.instance() or QApplication(["folio-exchange-app"])
    qapp.setApplicationName("FOLIO-Exchange")
    theme.apply(qapp)                                   # the page's look, with FOLIO's (see theme)
    collector = MainThreadCollector(qapp).start()       # garbage cycles freed on this thread only (see worker)
    try:
        return _run(qapp, named, verbose, console)
    except Exception as exc:  # noqa: BLE001 - before the window there is no excepthook, and pythonw no console
        log.exception("FOLIO-Exchange could not start")
        fail(f"FOLIO-Exchange couldn't start: {type(exc).__name__}: {exc}", console)
        return 1
    finally:
        collector.stop()
        collector.deleteLater()


def _run(qapp: QApplication, named: str | None, verbose: bool, console: bool) -> int:
    setup_logging(None, verbose, console=console)
    try:
        root = site_from(named)
    except FxError as exc:
        fail(str(exc), console)
        return 1
    if root is None:
        return 0
    setup_logging(logs_dir(root), verbose, console=console)
    kind, server = single.claim(places.instance_name(root))
    if kind == "shown":
        log.info("FOLIO-Exchange is already open for %s; brought its window to the front", root)
        return 0
    if kind in ("busy", "elsewhere"):
        fail(BUSY if kind == "busy" else ELSEWHERE, console)
        return 1
    places.remember_site(root)
    window = MainWindow(root, instance=server)
    server.shown.connect(window.bring_to_front)
    old_hook, sys.excepthook = sys.excepthook, report_unexpected
    log.info("Desktop app opened for %s", root)
    try:
        window.show()
        return qapp.exec()
    finally:
        sys.excepthook = old_hook
        window.shut_down()
