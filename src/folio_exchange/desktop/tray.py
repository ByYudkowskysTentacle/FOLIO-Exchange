"""The tray icon (D4): each watched site's feeds in a menu, and a notification when a scheduled run is held,
blocked or fails, or the scheduled task stops, with no window open.

``folio-exchange-app --tray`` starts it; the Startup entry (``startup.py``, opt-in under Options) starts it when
the person signs in to Windows. There is one tray per Windows user, whatever sites it watches (``tray.json`` in
``places.local_dir()``, changed under Options or from the tray's menu). It reads that list again at every poll,
and a second ``--tray`` makes it do so at once, through the same local socket the windows use (``single.claim``).
The tray closes when the list is empty.

The tray only reads. It never runs, sends or changes anything but its own list: opening a site starts that site's
window, where every action is. Each site is read every POLL_MS in a thread of its own (a ``worker.Reader`` per
site), and a site whose last reading hasn't ended is skipped, so a share that has stopped answering neither
freezes the menu nor holds up the other sites.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QProcess, QRectF, QTimer
from PySide6.QtGui import QColor, QCursor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ..config import find_site
from ..errors import FxError
from ..logsetup import setup_logging
from . import places, single, startup, theme, watch
from .worker import MainThreadCollector, Reader

log = logging.getLogger(__name__)

POLL_MS = 120_000             # each watched site is read this often: a held run shows within a few minutes
MESSAGE_MS = 15_000           # how long a notification asks to stay (Windows may keep it in the Action Center)
MOST_LINES = 4                # lines in one notification; the rest are counted
TASK_GRACE_S = 75 * 60        # awake this long before "Scheduled runs have stopped": the task starts hourly
BADGE = "#990000"             # FOLIO's error red (theme): it stands out from the orange symbol, with a white ring

NO_TRAY = ("This computer has no system tray that FOLIO-Exchange can use, so it can't show notifications. Open a "
           "site's window to see its feeds.")
BUSY = ("The FOLIO-Exchange tray seems to be running already, but it isn't responding. End it in Task Manager, then "
        "start it again.")
ELSEWHERE = "The FOLIO-Exchange tray is already running in another of your Windows sessions."
NO_SITES = ("No site is watched from the tray yet. Open a site, go to Options and turn on Watch this site from the "
            "tray.")


def badged(base: QIcon, size: int = 32) -> QIcon:
    """The app's symbol with a red dot in the corner: something needs the person. The tooltip says so in
    words too, so the dot is never the only sign."""
    pixmap = base.pixmap(size, size) if not base.isNull() else QPixmap(size, size)
    if base.isNull():
        pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("white"), max(1.5, size / 12)))
    painter.setBrush(QColor(BADGE))
    d = size * 0.55
    painter.drawEllipse(QRectF(size - d - 1, size - d - 1, d, d))
    painter.end()
    return QIcon(pixmap)


def _menu_text(text: str) -> str:
    """Text for a menu entry: an ampersand stays an ampersand rather than marking a shortcut letter."""
    return text.replace("&", "&&")


class Tray(QObject):
    """The tray icon, its menu and its notifications. ``show_message``, ``launch`` and ``quit_app`` are methods
    tests replace."""

    def __init__(self, sites=places.watched_sites, poll_ms: int = POLL_MS, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.sites_of = sites
        self.roots: list[Path] = list(sites())
        self.looks: dict[str, dict] = {}
        self.readers: dict[str, Reader] = {}
        self.task_told: dict[str, str | None] = {}
        self.readings = 0                            # readings delivered, for tests to wait on
        self.poll_s = poll_ms / 1000
        self.awake_since = self.last_tick = time.monotonic()
        self.last_told: Path | None = None
        self.closed = False
        self.plain = theme.logo_icon()
        self.marked = badged(self.plain)
        self.menu = QMenu()
        self.menu.setAccessibleName("FOLIO-Exchange tray menu")
        self.icon = QSystemTrayIcon(self.plain, self)
        self.icon.setToolTip("FOLIO-Exchange")
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.icon.messageClicked.connect(self._message_clicked)
        self.timer = QTimer(self)
        self.timer.setInterval(poll_ms)
        self.timer.timeout.connect(self.refresh)
        self._fill_menu()

    def start(self) -> Tray:
        self.icon.show()
        self.timer.start()
        self.refresh()
        return self

    def shut_down(self) -> None:
        self.closed = True                           # a reading still under way is dropped when it ends
        self.timer.stop()
        self.icon.hide()
        self.menu.deleteLater()

    # ------------------------------------------------------------ reading the sites
    def refresh(self) -> None:
        """Read the list of sites again, then start a reading of each site whose last one has ended."""
        if self.closed or not self._follow_list():
            return
        self._tick()
        for root in self.roots:
            key = str(root)
            reader = self.readers.get(key)
            if reader is None:
                reader = self.readers[key] = Reader(self)
                reader.done.connect(lambda result, error, r=root: self._looked(r, result, error))
            if not reader.busy:                      # a reading still under way (a share not answering) is left
                reader.read(lambda r=root: watch.look(r))

    def reload(self) -> None:
        """The list of sites changed (another ``--tray`` start asked): read it and the sites at once."""
        self.refresh()

    def _follow_list(self) -> bool:
        """Take the list of sites as saved now; close when it's empty. Returns whether there are sites."""
        roots = list(self.sites_of())
        if roots != self.roots:
            self.roots = roots
            keep = {str(r) for r in roots}
            for table in (self.looks, self.readers, self.task_told):
                for key in [k for k in table if k not in keep]:
                    del table[key]
            log.info("The tray watches %d site(s)", len(roots))
            self._fill_menu()
        if not roots:
            log.info("No site is watched any more, so the tray closes")
            self.quit_app()
        return bool(roots)

    def _tick(self) -> None:
        """Note the time of this poll. A gap much longer than the poll interval means the computer was asleep:
        "stopped" is held back again until the scheduled task has had time for its hourly start."""
        now = time.monotonic()
        if now - self.last_tick > 3 * self.poll_s:
            self.awake_since = now
        self.last_tick = now

    def quiet(self) -> bool:
        """Whether "Scheduled runs have stopped." waits: the tray hasn't been awake for TASK_GRACE_S."""
        return time.monotonic() - self.awake_since < TASK_GRACE_S

    def _looked(self, root: Path, snap, error) -> None:
        if self.closed or str(root) not in {str(r) for r in self.roots}:
            return                                   # closed, or taken off the list while it was read
        self.readings += 1
        if error is not None:                        # look() catches what a site can raise; this is unexpected
            log.warning("The tray couldn't read %s: %s: %s", root, type(error).__name__, error)
            return
        key = str(root)
        lines = watch.news(self.looks.get(key), snap)
        task_lines, self.task_told[key] = watch.task_news(snap, self.task_told.get(key), self.quiet())
        self.looks[key] = snap
        lines += task_lines
        if lines:
            log.info("Tray notification for %s: %s", root, " ".join(lines))
            self.tell(root, lines)
        self._fill_menu()

    def snaps(self) -> list[dict]:
        return [self.looks[str(r)] for r in self.roots if str(r) in self.looks]

    # ------------------------------------------------------------ telling
    def tell(self, root: Path, lines: list[str]) -> None:
        """One notification for one site. Clicking it opens that site."""
        name = (self.looks.get(str(root)) or {}).get("name") or root.name
        shown = lines[:MOST_LINES]
        if len(lines) > MOST_LINES:
            more = len(lines) - MOST_LINES
            shown.append(f"And {more} more thing{'s' if more != 1 else ''}.")
        self.last_told = root
        self.show_message(f"FOLIO-Exchange – {name}", "\n".join(shown))

    def show_message(self, title: str, text: str) -> None:
        self.icon.showMessage(title, text, QSystemTrayIcon.MessageIcon.Warning, MESSAGE_MS)

    def _message_clicked(self) -> None:
        if self.last_told is not None:
            self.launch(self.last_told)

    # ------------------------------------------------------------ the menu
    def _fill_menu(self) -> None:
        snaps, quiet = self.snaps(), self.quiet()
        self.icon.setIcon(self.marked if watch.count(snaps, quiet) else self.plain)
        self.icon.setToolTip(watch.tooltip(snaps, quiet))
        self.menu.clear()
        several = len(self.roots) > 1
        for root in self.roots:
            snap = self.looks.get(str(root))
            name = (snap or {}).get("name") or root.name
            if several:
                self.menu.addSection(_menu_text(name))
            if snap is None:
                self._line("Reading the site…", None)
            elif snap.get("error"):
                self._line(f"Couldn't read the site. {snap['error']}", None)
            else:
                for f in snap.get("feeds") or []:
                    self._line(watch.feed_line(f), root)
                if not snap.get("feeds"):
                    self._line("No feeds yet", root)
                problem = watch.task_problem(snap.get("task"))
                if problem:
                    self._line(watch.TASK_WORDS[problem], root)
            if snap is None or not snap.get("error"):
                opener = self.menu.addAction(f"Open {_menu_text(name)}" if several else "&Open FOLIO-Exchange")
                opener.triggered.connect(lambda _=False, r=root: self.launch(r))
            stop = self.menu.addAction(f"Stop watching {_menu_text(name)}" if several else "&Stop watching this site")
            stop.triggered.connect(lambda _=False, r=root: self.stop_watching(r))
        self.menu.addSeparator()
        self.menu.addAction("&Check now").triggered.connect(self.refresh)
        self.menu.addAction("&Quit the tray").triggered.connect(self.quit_app)

    def _line(self, text: str, root: Path | None) -> None:
        action = self.menu.addAction(_menu_text(text))
        if root is None:
            action.setEnabled(False)
        else:
            action.triggered.connect(lambda _=False, r=root: self.launch(r))

    def _activated(self, reason) -> None:
        """A click on the icon: with one site, open it; with several, show the menu (Windows shows it only on a
        right-click)."""
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        if len(self.roots) == 1:
            self.launch(self.roots[0])
        else:
            self.popup(QCursor.pos())

    def popup(self, where: QPoint) -> None:
        self.menu.popup(where)

    def stop_watching(self, root: Path) -> None:
        """Take a site off the list: one that was deleted or moved, say, which Options can't reach any more."""
        try:
            places.watch_site(root, False)
        except OSError as exc:
            log.warning("Couldn't take %s off the tray's list: %s", root, exc)
            self.show_message("FOLIO-Exchange", f"Couldn't take {root.name} off the tray's list. {exc}")
            return
        log.info("The tray no longer watches %s", root)
        self.refresh()

    # ------------------------------------------------------------ what tests replace
    def launch(self, root: Path) -> None:
        """Open the site's window: a process of its own, or the open window brought to the front."""
        exe, args, folder = startup.app_command(root)
        started = QProcess.startDetached(exe, args, folder)
        if not (started[0] if isinstance(started, tuple) else started):
            log.warning("Couldn't open %s: starting %s failed", root, exe)

    def quit_app(self) -> None:
        QApplication.quit()


def fail(message: str, console: bool) -> None:
    log.error("%s", message)
    if console:
        print(message, file=sys.stderr, flush=True)
    else:
        from .pages import alert

        alert(None, message)


def run_tray(named: str | None, *, verbose: bool = False, console: bool = True) -> int:
    """``folio-exchange-app --tray`` (with ``--site``: watch that site too)."""
    qapp = QApplication.instance() or QApplication(["folio-exchange-app"])
    qapp.setApplicationName("FOLIO-Exchange")
    qapp.setQuitOnLastWindowClosed(False)             # the tray has no window; quitting is in its menu
    theme.apply(qapp)
    collector = MainThreadCollector(qapp).start()
    try:
        return _run_tray(qapp, named, verbose, console)
    except Exception as exc:  # noqa: BLE001 - started at sign-in with no console: say it, and log it
        log.exception("The FOLIO-Exchange tray could not start")
        fail(f"The FOLIO-Exchange tray couldn't start: {type(exc).__name__}: {exc}", console)
        return 1
    finally:
        collector.stop()
        collector.deleteLater()


def _run_tray(qapp: QApplication, named: str | None, verbose: bool, console: bool) -> int:
    setup_logging(places.local_dir() / "logs", verbose, console=console)
    if named:
        try:
            root = find_site(named)
            places.watch_site(root, True)
        except (FxError, OSError) as exc:
            fail(str(exc), console)
            return 1
    kind, server = single.claim(places.tray_instance_name())
    if kind == "shown":
        log.info("The tray was already running; it reads its list of sites again")
        return 0
    if kind in ("busy", "elsewhere"):
        fail(BUSY if kind == "busy" else ELSEWHERE, console)
        return 1
    try:
        if not places.watched_sites():
            log.info("No site is watched from the tray, so it doesn't start")
            if console:
                print(NO_SITES, file=sys.stderr, flush=True)
            return 0
        if not QSystemTrayIcon.isSystemTrayAvailable():
            fail(NO_TRAY, console)
            return 1
        tray = Tray().start()
        server.shown.connect(tray.reload)
        log.info("The tray started for %d site(s)", len(tray.roots))
        try:
            return qapp.exec()
        finally:
            tray.shut_down()
    finally:
        server.close()
