"""The main window for one site: a sidebar of views and feeds, and the page for the one chosen.

The window reads the site off its own thread (``worker.Reader``) every minute, when it is activated, after an
action and on F5, and keeps showing the last good reading if one fails. Actions run one at a time through the
``Jobs`` it shares with the browser page; closing while one runs waits for it rather than cut it off.
"""
from __future__ import annotations

import logging
import os
import sys
import sysconfig
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QProcess, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import views
from ..errors import FxError
from ..jobs import Job, Jobs
from . import model, places
from .page import PageFallback
from .pages import ConnectionPage, FeedPage, HomePage, LaterPage, NotReadyPage, alert
from .picker import NewSiteDialog, SitePicker
from .single import InstanceServer
from .worker import JobWatcher, Reader

log = logging.getLogger(__name__)

REFRESH_MS = 60_000
BUSY_MS = 500                     # how often the window looks whether an action (here or on the page) runs
ACTIVATE_GAP = 5.0                # seconds: activating the window refreshes at most this often
LATER = (("activity", "Activity", "Every run and every change to the site's settings, newest first.",
          "Open &Activity in the browser page"),
         ("recipes", "Recipes", "The vendors' recipes installed here, their new versions and what changed.",
          "Open &Recipes in the browser page"),
         ("options", "Options", "Renaming the site folder, and registering the scheduled task.",
          "Open &Options in the browser page"))


def open_in_browser(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


def app_command(site_root: Path) -> tuple[str, list[str], str]:
    """How to start the app for another site, in a process of its own (no console window on Windows), and the
    folder to start it in. Never this process's current folder: ``python -m`` imports from there first, and
    that may be a shared folder others can write to. The installed ``folio-exchange-app`` is used when there
    is one; else ``-m`` with ``-P`` (Python 3.11 and later), which leaves the current folder off the path."""
    exe = Path(sys.executable)
    here = str(exe.parent)
    script = Path(sysconfig.get_path("scripts")) / ("folio-exchange-app.exe" if sys.platform == "win32"
                                                    else "folio-exchange-app")
    if script.is_file():
        return str(script), ["--site", str(site_root)], here
    if sys.platform == "win32" and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").is_file():
        exe = exe.parent / "pythonw.exe"
    safe = ["-P"] if sys.version_info >= (3, 11) else []
    return str(exe), [*safe, "-m", "folio_exchange.desktop", "--site", str(site_root)], here


def _what(job: Job) -> str:
    return f"{job.kind} of {job.feed}" if job.feed else job.kind


class MainWindow(QMainWindow):
    def __init__(self, site_root: Path, jobs: Jobs | None = None, open_url=None,
                 instance: InstanceServer | None = None) -> None:
        super().__init__()
        self.site_root = Path(site_root).resolve()
        self.jobs = jobs or Jobs()
        self.page = PageFallback(self.site_root, self.jobs, open_url or open_in_browser)
        self.instance = instance
        self.snap: dict | None = None
        self.watcher: JobWatcher | None = None
        self.closing_after_job = False
        self.quitting = False
        self.was_busy = False
        self.again = False
        self.last_refresh = float("-inf")
        self.reader = Reader(self)
        self.reader.done.connect(self._read_done)
        self._build()
        self._set_title(self.site_root.name)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(REFRESH_MS)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()
        self.busy_timer = QTimer(self)
        self.busy_timer.setInterval(BUSY_MS)
        self.busy_timer.timeout.connect(self.sync_busy)
        self.busy_timer.start()
        self.resize(1000, 680)
        self.refresh()

    # ------------------------------------------------------------ layout
    def _build(self) -> None:
        menu = self.menuBar().addMenu("&Site")
        self.open_action = self._action(menu, "&Open a site…", QKeySequence.StandardKey.Open, self.open_site)
        self.new_action = self._action(menu, "&New site…", QKeySequence.StandardKey.New, self.new_site)
        menu.addSeparator()
        self.browser_action = self._action(menu, "Open in the &browser page", QKeySequence("Ctrl+B"),
                                           lambda: self.open_page("home"))
        self.refresh_action = self._action(menu, "&Refresh", QKeySequence(Qt.Key.Key_F5), self.refresh)
        menu.addSeparator()
        self._action(menu, "&Quit", QKeySequence.StandardKey.Quit, self.close)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.TextFormat.PlainText)
        self.banner.setAccessibleDescription("Notice")
        self.banner.setFrameShape(QLabel.Shape.StyledPanel)
        self.banner.setMargin(6)
        self.banner.hide()

        self.sidebar = QListWidget()
        self.sidebar.setAccessibleName("Views and feeds")
        self.sidebar.currentItemChanged.connect(lambda item, _old: self._show(item))
        self.stack = QStackedWidget()
        self.stack.setAccessibleName("Content")
        self.home = HomePage()
        self.home.open_feed.connect(lambda name: self.select(f"feed:{name}"))
        self.feed = FeedPage()
        self.feed.open_in_page.connect(lambda name: self.open_page(feed=name))
        self.connection = ConnectionPage()
        self.connection.check.connect(self.check_connection)
        self.later: dict[str, LaterPage] = {}
        for view, title, about, caption in LATER:
            self.later[view] = LaterPage(view, title, about, caption)
            self.later[view].open_in_page.connect(lambda v: self.open_page(v))
        self.not_ready = NotReadyPage()
        self.not_ready.set_up.connect(lambda: self.open_page("home"))
        self.reading = QLabel("Reading the site…")
        self.reading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reading.setWordWrap(True)
        self.reading.setTextFormat(Qt.TextFormat.PlainText)        # it may show an error's text
        for w in (self.reading, self.home, self.feed, self.connection, *self.later.values(), self.not_ready):
            self.stack.addWidget(w)

        split = QSplitter()
        split.addWidget(self.sidebar)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 1)
        split.setSizes([240, 760])
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.addWidget(self.banner)
        row = QHBoxLayout()
        row.addWidget(split)
        layout.addLayout(row, 1)
        self.setCentralWidget(body)
        self.setTabOrder(self.sidebar, self.stack)

    def _action(self, menu, caption: str, keys, slot) -> QAction:
        action = QAction(caption, self)
        action.setShortcut(QKeySequence(keys))
        action.triggered.connect(lambda _checked=False: slot())
        menu.addAction(action)
        self.addAction(action)
        return action

    def _set_title(self, name: str) -> None:
        self.setWindowTitle(f"FOLIO-Exchange – {name}")

    def say(self, message: str | None) -> None:
        """A notice above the pages (an error reading the site, an action running, closing after it)."""
        self.banner.setText(message or "")
        self.banner.setVisible(bool(message))

    # ------------------------------------------------------------ the sidebar and pages
    def _items(self) -> list[tuple[str, str]]:
        """(key, caption) for each sidebar entry; a feed's key is "feed:<name>"."""
        snap = self.snap or {}
        if not (snap.get("state") or {}).get("ready"):
            return [("home", "Set up this site")]
        items = [("home", "Home")]
        for f in (snap.get("summary") or {}).get("feeds") or []:
            items.append((f"feed:{f['name']}", f"{f['name']} – {model.feed_state(f)}"))
        return items + [("activity", "Activity"), ("recipes", "Recipes"), ("connection", "Connection"),
                        ("options", "Options")]

    def current_key(self) -> str:
        item = self.sidebar.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else "home"

    def _fill_sidebar(self) -> None:
        keep = self.current_key()
        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        chosen = 0
        for i, (key, caption) in enumerate(self._items()):
            item = QListWidgetItem(caption)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, caption)
            self.sidebar.addItem(item)
            if key == keep:
                chosen = i
        self.sidebar.setCurrentRow(chosen)
        self.sidebar.blockSignals(False)
        self._show(self.sidebar.currentItem())

    def select(self, key: str) -> None:
        for i in range(self.sidebar.count()):
            if self.sidebar.item(i).data(Qt.ItemDataRole.UserRole) == key:
                self.sidebar.setCurrentRow(i)
                return

    def _show(self, item: QListWidgetItem | None) -> None:
        snap = self.snap
        if snap is None:
            self.stack.setCurrentWidget(self.reading)
            return
        if not snap["state"].get("ready"):
            self.not_ready.show_state(snap["state"])
            self.stack.setCurrentWidget(self.not_ready)
            return
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else "home"
        summary = snap["summary"]
        feed = key[len("feed:"):] if key.startswith("feed:") else None
        if feed in snap["feeds"]:
            self.feed.show_feed(snap["feeds"][feed])
            self.stack.setCurrentWidget(self.feed)
        elif key == "connection":
            self.connection.show_site(summary)
            self.stack.setCurrentWidget(self.connection)
        elif key in self.later:
            self.stack.setCurrentWidget(self.later[key])
        else:
            self.home.show_site(summary)
            self.stack.setCurrentWidget(self.home)

    # ------------------------------------------------------------ reading the site
    def refresh(self) -> None:
        """Read the site again, off this thread. A refresh asked for during one runs once it ends."""
        if self.quitting:
            return
        self._follow_page()
        if self.reader.busy:
            self.again = True
            return
        self.last_refresh = time.monotonic()
        root = self.site_root
        self.reader.read(lambda: model.snapshot(root))

    def _read_done(self, snap: dict | None, error: Exception | None) -> None:
        if error is not None:
            log.warning("Could not read the site %s: %s", self.site_root, error)
            self.say(f"Could not read the site: {error}")
            if self.snap is None:
                self.reading.setText(f"Could not read the site: {error}\n\nPress F5 to try again.")
        else:
            self.snap = snap
            if not self.closing_after_job and not self.jobs.running():
                self.say(None)
            summary = snap.get("summary") or {}
            self._set_title(summary.get("name") or self.site_root.name)
            self._fill_sidebar()
        if self.again:
            self.again = False
            self.refresh()

    def _follow_page(self) -> None:
        """When the browser page renamed the site folder, carry on with the new folder. Called before anything
        reads ``site_root``. It compares names only: the page's folder is already resolved, and touching the
        disk here could freeze the window on a share that has stopped answering."""
        new = Path(self.page.site_root)
        if os.path.normcase(str(new)) == os.path.normcase(str(self.site_root)):
            return
        old, self.site_root = self.site_root, new
        log.info("The site folder is now %s (renamed from %s)", new, old)
        places.remember_site(new, replaces=old)
        if self.instance is not None and not self.instance.rename(places.instance_name(new)):
            log.warning("Another window already uses the renamed folder %s", new)
        self._set_title(new.name)

    def event(self, ev: QEvent) -> bool:
        if (ev.type() == QEvent.Type.WindowActivate
                and time.monotonic() - self.last_refresh > ACTIVATE_GAP):
            QTimer.singleShot(0, self.refresh)
        return super().event(ev)

    # ------------------------------------------------------------ actions
    def check_connection(self) -> None:
        self._follow_page()
        root = self.site_root

        def work() -> dict:
            log.info("Checking the connection to FOLIO for the site %s", root.name)
            res = views.check_connection(root)
            for r in res["endpoints"]:
                log.info("%-6s %s (%s)", "OK" if r["status"] == "ok" else r["status"], r["name"], r["path"])
            return res

        try:
            job = self.jobs.start("check", None, work)
        except FxError as exc:
            self.connection.refused(str(exc))
            return
        self.connection.started()
        watcher = self.watcher = JobWatcher(job, self)
        watcher.progress.connect(lambda lines, w=watcher: self._check_lines(w, lines))
        watcher.finished.connect(lambda state, w=watcher: self._check_done(w, state))
        watcher.start()
        self.sync_busy()

    def _check_lines(self, watcher: JobWatcher, lines: list) -> None:
        if watcher is self.watcher:
            self.connection.add_lines(lines)

    def _check_done(self, watcher: JobWatcher, state: dict) -> None:
        """A check's watcher has seen its job end. Only the newest check's result is shown: the busy timer can
        let the next check start before an older watcher's last look, and that one's news is stale by then."""
        watcher.deleteLater()
        if watcher is not self.watcher:
            return
        self.watcher = None
        self.connection.finished(state)

    def sync_busy(self) -> None:
        """Enable actions only while nothing runs (here or on the page); refresh when an action ends; close
        when one was waited for."""
        job = self.jobs.running()
        busy = job is not None
        idle = not busy and not self.closing_after_job
        self.connection.check_button.setEnabled(idle)
        for action in (self.open_action, self.new_action):
            action.setEnabled(not self.closing_after_job)
        if busy and not self.closing_after_job:
            self.say(f"Running: {_what(job)}. Other actions wait until it finishes.")
        if self.was_busy and not busy:
            if self.closing_after_job:
                self.close_now()
                return
            self.say(None)
            self.refresh()
        self.was_busy = busy

    def open_page(self, view: str = "home", feed: str | None = None) -> None:
        try:
            self.page.open(view, feed)
        except OSError as exc:
            alert(self, f"The browser page could not be started: {exc}")

    def open_site(self) -> None:
        picker = SitePicker(self)
        if picker.exec() and picker.chosen is not None:
            self.launch_site(picker.chosen)

    def new_site(self) -> None:
        dialog = NewSiteDialog(self)
        if dialog.exec() and dialog.chosen is not None:
            self.launch_site(dialog.chosen)

    def launch_site(self, root: Path) -> None:
        """Open another site in a window (and process) of its own; this site's is already open here."""
        if os.path.normcase(str(Path(root).resolve())) == os.path.normcase(str(self.site_root)):
            self.bring_to_front()
            return
        exe, args, folder = app_command(root)
        started = QProcess.startDetached(exe, args, folder)
        if not (started[0] if isinstance(started, tuple) else started):
            alert(self, f"Could not open {root}: starting {exe} failed.")

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------ closing
    def ask_close_while_running(self, job: Job) -> str:
        """"later" (close once the action ends) or "keep" (keep the window open). Tests replace this."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("FOLIO-Exchange")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"A {_what(job)} is still running.")
        box.setInformativeText("Closing now would cut it off half way, so the window can close once it "
                               "finishes instead.")
        later = box.addButton("&Close when it finishes", QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton("&Keep the window open", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        return "later" if box.clickedButton() is later else "keep"

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt's naming
        job = self.jobs.running()
        if self.quitting or job is None:
            self.shut_down()
            event.accept()
            return
        event.ignore()
        if self.closing_after_job:
            return
        if self.ask_close_while_running(job) == "later":
            if self.jobs.running() is None:           # it ended while the question was open
                self.shut_down()
                event.accept()
                return
            self.closing_after_job = True
            self.say(f"This window closes when the {_what(job)} finishes.")
            self.sync_busy()

    def close_now(self) -> None:
        self.quitting = True
        self.close()

    def shut_down(self) -> None:
        self.quitting = True
        self.refresh_timer.stop()
        self.busy_timer.stop()
        self.page.stop()
        if self.instance is not None:
            self.instance.close()
