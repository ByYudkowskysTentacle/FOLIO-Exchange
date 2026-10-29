"""The main window for one site: a sidebar of views and feeds, and the page for the one chosen.

The window reads the site off its own thread (``worker.Reader``) every minute, when it is activated, after an
action and on F5, and keeps showing the last good reading if one fails. Long actions (check, preview, run, build,
release, delivery test, and setting up: see ``actions.py``) run one at a time through the ``Jobs`` it shares with
the browser page, each watched by a ``JobWatcher`` (one the page started too, so its feed shows its progress and
result here); short ones that touch the disk or only look (pause or resume, a run's details and rows, what a
release would send, opening a run's folder, the recipes, the tenant's lists) run as ``Errands``, and only the
newest answer for each place is shown. Closing while an action runs, or while a pause or resume is written, waits
for it rather than cut it off.
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
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenuBar,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import views
from ..errors import FxError
from ..jobs import Job, Jobs
from . import model, places, theme
from .actions import SETUP_DONE, SetupActions
from .activity import ActivityPage
from .feed import FeedPage
from .options import OptionsPage, start_console
from .page import PageFallback
from .pages import ConnectionPage, HomePage, alert
from .picker import NewSiteDialog, SitePicker
from .recipes import RecipesPage
from .release import ReleaseDialog
from .setup import ConnectForm, SetupPage
from .single import InstanceServer
from .wizard import FeedWizard
from .worker import Errands, JobWatcher, Reader

log = logging.getLogger(__name__)

REFRESH_MS = 60_000
BUSY_MS = 500                     # how often the window looks whether an action (here or on the page) runs
ACTIVATE_GAP = 5.0                # seconds: activating the window refreshes at most this often
PREVIEW_SAMPLE = 25                # instances a preview samples, as the page's Preview button asks for
FEED_KINDS = ("preview", "run", "build", "release", "test-delivery")    # the actions of a feed the page can start


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
    return model.job_words(job.kind, job.feed)


class MainWindow(QMainWindow, SetupActions):
    def __init__(self, site_root: Path, jobs: Jobs | None = None, open_url=None,
                 instance: InstanceServer | None = None) -> None:
        super().__init__()
        self.site_root = Path(site_root).resolve()
        self.jobs = jobs or Jobs()
        self.page = PageFallback(self.site_root, self.jobs, open_url or open_in_browser)
        self.instance = instance
        self.snap: dict | None = None
        self.watcher: JobWatcher | None = None
        self.watching: tuple[str, str | None] | None = None      # (kind, feed) of the action the watcher follows
        self.running_feed: str | None = None                     # the feed the sidebar says is running
        self.reviewing = False                                   # a release question is being prepared or asked
        self.review_root = self.site_root                        # the folder the release question was read from
        self.asked: dict[tuple, int] = {}                        # the newest request for each place (see _ask)
        self.toggling = False                                    # a pause or resume is being written
        self.job_about: object = None                            # what the running setup job is for (actions.py)
        self.choices: dict | None = None                         # the tenant's lists, once read: {data} or {error}
        self.opening_settings: str | None = None                 # the feed whose settings form is being read
        self.pending_select: tuple[str, str | None] | None = None   # a view to open once the site is read again
        self.renaming: str | None = None                         # the new name while the folder is renamed
        self.renaming_started = False
        self.registering = False                                 # a console registers the scheduled task
        self.start_console = start_console                       # how that console is started (tests replace it)
        self.settings_dialog = self.delivery_dialog = self.alerts_dialog = None
        self.shown_key: str | None = None
        self.closing_after_job = False
        self.quitting = False
        self.was_busy = False
        self.again = False
        self.last_refresh = float("-inf")
        self.reader = Reader(self)
        self.reader.done.connect(self._read_done)
        self.errands = Errands(self)
        self.errands.done.connect(self._errand_done)
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
        self.menu_bar = QMenuBar()
        self.menu_bar.setAccessibleName("Menu")
        menu = self.menu_bar.addMenu("&Site")
        self.open_action = self._action(menu, "&Open a site…", QKeySequence.StandardKey.Open, self.open_site)
        self.new_action = self._action(menu, "&New site…", QKeySequence.StandardKey.New, self.new_site)
        menu.addSeparator()
        self.browser_action = self._action(menu, "Open in the &browser page", QKeySequence("Ctrl+B"),
                                           lambda: self.open_page("home"))
        self.refresh_action = self._action(menu, "&Refresh", QKeySequence(Qt.Key.Key_F5), self.refresh)
        # Ctrl+Enter previews the open feed, as on the page: a preview never sends anything.
        self.preview_action = QAction("Preview the feed", self)
        self.preview_action.setShortcuts([QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter")])
        self.preview_action.triggered.connect(self._preview_shown)
        self.addAction(self.preview_action)
        menu.addSeparator()
        app = QApplication.instance()
        self.dark_action = QAction("&Dark mode", self)
        self.dark_action.setCheckable(True)
        self.dark_action.setChecked(theme.is_dark(app) if app.property("fx_dark") is not None
                                    else places.dark_mode())
        self.dark_action.toggled.connect(lambda on: theme.set_dark(QApplication.instance(), on))
        menu.addAction(self.dark_action)
        menu.addSeparator()
        self._action(menu, "&Quit", QKeySequence.StandardKey.Quit, self.close)

        self.banner = QLabel()
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.TextFormat.PlainText)
        self.banner.setAccessibleDescription("Notice")
        self.banner.setFrameShape(QLabel.Shape.StyledPanel)
        self.banner.setMargin(6)
        self.banner.hide()

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setAccessibleName("Views and feeds")
        self.sidebar.currentItemChanged.connect(lambda item, _old: self._show(item))
        self.stack = QStackedWidget()
        self.stack.setAccessibleName("Content")
        self.home = HomePage()
        self.home.open_feed.connect(lambda name: self.select(f"feed:{name}"))
        self.feed = FeedPage()
        self.feed.open_in_page.connect(lambda name: self.open_page(feed=name))
        self.feed.preview.connect(self.preview)
        self.feed.run.connect(self.run_feed)
        self.feed.set_enabled.connect(self.set_enabled)
        self.feed.review.connect(self.review)
        self.feed.check_delivery.connect(self.check_delivery)
        self.feed.open_folder.connect(lambda run_id: self.open_folder("feed", run_id))
        self.feed.open_run.connect(self.open_run)
        self.feed.rows_wanted.connect(lambda name, run_id, output: self.read_rows("feed", name, run_id, output))
        self.feed.edit_settings.connect(self.edit_settings)
        self.feed.change_delivery.connect(self.change_delivery)
        self.feed.save_password.connect(self.save_password)
        self.feed.save_key.connect(self.save_key)
        self.home.add_feed.connect(lambda: self.select("add"))
        self.activity = ActivityPage()
        self.activity.open_run.connect(lambda run_id: self.open_run(None, run_id))
        self.activity.open_folder.connect(lambda run_id: self.open_folder("activity", run_id))
        self.activity.rows_wanted.connect(lambda run_id, output: self.read_rows("activity", None, run_id, output))
        self.connection = ConnectionPage()
        self.connection.check.connect(self.check_connection)
        self.connection.change.connect(self.open_connect_form)
        self.connect_form = ConnectForm(first=False)
        self.connection.set_form(self.connect_form)
        self.connect_form.submitted.connect(lambda: self.connect_folio(self.connect_form))
        self.connect_form.cancelled.connect(self.connection.close_form)
        self.setup = SetupPage()
        self.setup.form.submitted.connect(lambda: self.connect_folio(self.setup.form))
        self.wizard = FeedWizard()
        self.wizard.want_recipes.connect(lambda: self.read_recipes("wizard"))
        self.wizard.want_permissions.connect(self.want_permissions)
        self.wizard.want_choices.connect(self.want_choices)
        self.wizard.add.connect(self.add_feed)
        self.wizard.get_recipes.connect(lambda: self.select("recipes"))
        self.wizard.cancelled.connect(self.cancel_wizard)
        self.recipes = RecipesPage()
        self.recipes.check.connect(self.check_source)
        self.recipes.install.connect(self.install_recipes)
        self.options = OptionsPage()
        self.options.rename.connect(self.rename_site)
        self.options.register.connect(self.register_task)
        self.options.change_alerts.connect(self.change_alerts)
        self.options.test_alert.connect(self.test_alert)
        self.reading = QLabel("Reading the site…")
        self.reading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reading.setWordWrap(True)
        self.reading.setTextFormat(Qt.TextFormat.PlainText)        # it may show an error's text
        for w in (self.reading, self.home, self.feed, self.activity, self.connection, self.recipes, self.options,
                  self.wizard, self.setup):
            self.stack.addWidget(w)

        split = QSplitter()
        split.addWidget(self.sidebar)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 1)
        split.setSizes([240, 760])
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)               # the sidebar runs to the window's edges, as the page's
        layout.setSpacing(0)
        layout.addWidget(self.banner)
        row = QHBoxLayout()
        row.addWidget(split)
        layout.addLayout(row, 1)
        self.setCentralWidget(body)
        self.setMenuWidget(self._topbar())
        self.setTabOrder(self.sidebar, self.stack)

    def _action(self, menu, caption: str, keys, slot) -> QAction:
        action = QAction(caption, self)
        action.setShortcut(QKeySequence(keys))
        action.triggered.connect(lambda _checked=False: slot())
        menu.addAction(action)
        self.addAction(action)
        return action

    def _topbar(self) -> QWidget:
        """The bar along the top, as FOLIO's main navigation and the page's: the symbol, the name, the menu, and
        the site's folder name at the right."""
        bar = QWidget()
        bar.setObjectName("topbar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(10)
        row.addWidget(theme.mark(22))
        brand = QLabel("FOLIO-Exchange")
        brand.setObjectName("brand")
        row.addWidget(brand)
        row.addWidget(self.menu_bar)
        row.addStretch(1)
        self.site_chip = QLabel()
        self.site_chip.setObjectName("sitechip")
        self.site_chip.setTextFormat(Qt.TextFormat.PlainText)        # a folder name, shown as written
        self.site_chip.setAccessibleDescription("Site")
        row.addWidget(self.site_chip)
        return bar

    def menuBar(self) -> QMenuBar:  # noqa: N802 - Qt's naming
        """The menu, which lives in the top bar: QMainWindow's own would make a new, empty one in its place."""
        return self.menu_bar

    def _set_title(self, name: str) -> None:
        self.setWindowTitle(f"FOLIO-Exchange – {name}")
        self.site_chip.setText(name)
        self.site_chip.setToolTip(str(self.site_root))

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
            items.append((f"feed:{f['name']}", self._feed_caption(f)))
        return items + [("add", "Add a vendor feed"), ("activity", "Activity"), ("recipes", "Recipes"),
                        ("connection", "Connection"), ("options", "Options")]

    def _feed_caption(self, f: dict) -> str:
        """A feed's sidebar line: its state in words, or "Running" while an action of it runs (the page's
        "Busy")."""
        job = self.jobs.running()
        if job is not None and job.feed == f["name"]:
            return f"{f['name']} – Running: {model.KIND_WORDS.get(job.kind, job.kind)}"
        return f"{f['name']} – {model.feed_state(f)}"

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
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else "home"
        if not snap["state"].get("ready"):
            key = "setup"
        moved = key != self.shown_key
        if moved:
            self._leave(self.shown_key)
            self.shown_key = key
        if key == "setup":
            self.setup.show_state(snap["state"])
            self.stack.setCurrentWidget(self.setup)
            return
        summary = snap["summary"]
        feed = key[len("feed:"):] if key.startswith("feed:") else None
        if feed in snap["feeds"]:
            self.feed.keyring = bool(snap["state"].get("keyring"))
            self.feed.show_feed(snap["feeds"][feed], (snap.get("runs") or {}).get(feed) or [],
                                (snap.get("settings") or {}).get(feed), summary.get("warnings") or [])
            self.stack.setCurrentWidget(self.feed)
        elif key == "activity":
            self.activity.show_activity(snap.get("activity") or {}, self._delivers())
            self.stack.setCurrentWidget(self.activity)
        elif key == "connection":
            self.connection.show_site(summary)
            self.stack.setCurrentWidget(self.connection)
        elif key == "add":
            taken = [f["name"] for f in summary.get("feeds") or []] + list(summary.get("problems") or {})
            if moved:
                self.wizard.start(taken)
            else:                                        # a refresh: what is being typed stays as it is
                self.wizard.taken = taken
            self.stack.setCurrentWidget(self.wizard)
        elif key == "recipes":
            if moved:                                    # read again on each visit, as the page does
                self.recipes.loading()
                self.read_recipes("page")
            self.stack.setCurrentWidget(self.recipes)
        elif key == "options":
            self.options.show_site(summary)
            if moved:
                self.read_alerts()
                self.look_for_task()
            self.stack.setCurrentWidget(self.options)
        else:
            self.home.show_site(summary, model.attention(snap))
            self.stack.setCurrentWidget(self.home)

    def _leave(self, key: str | None) -> None:
        """Nothing typed into a password box outlives the view it was typed in."""
        if key == "connection":
            self.connection.close_form()
        elif key == "setup":
            self.setup.form.password.clear()
        elif key and key.startswith("feed:"):
            self.feed.login.clear_secrets()

    def _delivers(self) -> dict[str, bool]:
        return {name: bool((d.get("delivery") or {}).get("delivers"))
                for name, d in ((self.snap or {}).get("feeds") or {}).items()}

    # ------------------------------------------------------------ reading the site
    def refresh(self) -> None:
        """Read the site again, off this thread. A refresh asked for during one runs once it ends."""
        if self.quitting:
            return
        self._follow_page()
        if self.reader.busy or self.renaming:            # no file of the folder is open while it is renamed
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
            self._select_pending()
        if self.renaming and not self.renaming_started:
            self._start_rename()                         # it waited for this reading to end
            return
        if self.again:
            self.again = False
            self.refresh()

    def _select_pending(self) -> None:
        """Open the view a setup step leads to (the wizard after connecting, a feed just added) once the site's
        new reading has it."""
        if self.pending_select is None:
            return
        key, title = self.pending_select
        if key not in [k for k, _caption in self._items()]:
            return
        self.pending_select = None
        self.select(key)
        if key.startswith("feed:"):
            self.feed.feed_added(key[len("feed:"):], title or key[len("feed:"):])
        elif key == "add":
            self.wizard.arrive()                         # the focus follows the window's own move, and is told why

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

    # ------------------------------------------------------------ long actions (jobs)
    def _start(self, kind: str, feed: str | None, work, refused) -> Job | None:
        """Start ``work`` as the one running action, watched to its end. While another runs (here or on the
        page) it is refused at once, as Jobs refuses it, and ``refused(message)`` says so."""
        try:
            job = self.jobs.start(kind, feed, work)
        except FxError as exc:
            refused(str(exc))
            return None
        if self.watcher is not None:
            # The last action has ended (Jobs started this one), but its watcher may not have looked since: the
            # half-second look can enable the buttons first. Let it report now, so its result is not lost.
            self.watcher.poll()
        self._watch(job)
        self.sync_busy()
        return job

    def _watch(self, job: Job) -> None:
        self.watching = (job.kind, job.feed)
        if job.feed and job.kind != "test-delivery":
            self._ask(("run", job.feed))           # a run asked for from History before this began is old news
        watcher = self.watcher = JobWatcher(job, self)
        watcher.progress.connect(lambda lines, w=watcher: self._job_lines(w, lines))
        watcher.finished.connect(lambda state, w=watcher: self._job_done(w, state))
        watcher.start()

    def _follow(self, job: Job) -> None:
        """Show an action of a feed that the browser page started, as if it had started here: its progress, its
        log from the start, and its result."""
        self._watch(job)
        if job.kind == "test-delivery":
            self.feed.delivery_check_started(job.feed)
        else:
            self.feed.job_started(job.feed, job.kind)

    def _job_lines(self, watcher: JobWatcher, lines: list) -> None:
        if watcher is not self.watcher or self.watching is None:
            return
        kind, feed = self.watching
        if kind == "check":
            self.connection.add_lines(lines)
        elif kind != "test-delivery" and feed:
            self.feed.job_lines(feed, lines)

    def _job_done(self, watcher: JobWatcher, state: dict) -> None:
        """A watcher has seen its job end. Only the newest action's result is shown: the busy timer can let the
        next action start before an older watcher's last look, and that one's news is stale by then."""
        watcher.deleteLater()
        if watcher is not self.watcher or self.watching is None:
            return
        kind, feed = self.watching
        self.watcher = self.watching = None
        if kind in SETUP_DONE:
            self._setup_done(kind, state)
        elif kind == "check":
            self.connection.finished(state)
        elif kind == "test-delivery" and feed:
            self.feed.delivery_check_done(feed, state)
        elif feed:
            run_id = self.feed.job_done(feed, state)
            if run_id is not None:
                self.read_rows("feed", feed, run_id, None)

    def check_connection(self) -> None:
        self._follow_page()
        root = self.site_root

        def work() -> dict:
            log.info("Checking the connection to FOLIO for the site %s", root.name)
            res = views.check_connection(root)
            for r in res["endpoints"]:
                log.info("%-6s %s (%s)", "OK" if r["status"] == "ok" else r["status"], r["name"], r["path"])
            return res

        if self._start("check", None, work, self.connection.refused):
            self.connection.started()

    def _feed_job(self, kind: str, name: str, work) -> None:
        if self._start(kind, name, work, self.feed.say):
            self.feed.job_started(name, kind)

    def preview(self, name: str) -> None:
        """Build sample files from live data; nothing is sent."""
        self._follow_page()
        root = self.site_root
        self._feed_job("preview", name, lambda: views.preview(root, name, PREVIEW_SAMPLE))

    def run_feed(self, name: str, deliver: bool) -> None:
        """Run now: with ``deliver``, update, build, check and send (or hold for review); else build only."""
        self._follow_page()
        root = self.site_root
        self._feed_job("run" if deliver else "build", name, lambda: views.run(root, name, deliver=deliver))

    def check_delivery(self, name: str) -> None:
        """Log in to the feed's destination and look at the folder; nothing is sent."""
        self._follow_page()
        root = self.site_root
        if self._start("test-delivery", name, lambda: views.check_delivery(root, name), self.feed.say):
            self.feed.delivery_check_started(name)

    def _preview_shown(self, _checked: bool = False) -> None:
        if (self.stack.currentWidget() is self.feed and self.feed.name and self.jobs.running() is None
                and not self.closing_after_job and not self.reviewing):
            self.preview(self.feed.name)

    # ------------------------------------------------------------ releasing a held run
    def review(self, name: str) -> None:
        """Look at what a release would send (off this thread), then ask; only "Send files" sends."""
        if self.reviewing:
            return                                           # one question at a time
        if self.jobs.running() is not None:
            self.feed.say("Another action is running; wait for it to finish.")
            return
        self._follow_page()
        root = self.review_root = self.site_root
        self.reviewing = True
        self.sync_busy()
        self.errands.run(("release-summary", name), lambda: views.release_summary(root, name))

    def ask_release(self, summary: dict) -> bool:
        """True only when the person chose "Send files" (Cancel is the default). Tests replace this."""
        dialog = ReleaseDialog(summary, self)
        try:
            return dialog.exec() == ReleaseDialog.DialogCode.Accepted
        finally:
            dialog.deleteLater()                             # not kept in the window, one per question

    def _reviewed(self) -> None:
        self.reviewing = False
        self.sync_busy()

    def _release_asked(self, name: str, summary: dict) -> None:
        if self.quitting:
            self._reviewed()
            return                                           # the window closed while the look ran: no question
        if self.stack.currentWidget() is not self.feed or self.feed.name != name:
            # The person moved on while the look ran: a question now would open over another page.
            self._reviewed()
            self.say(f"Nothing was sent: the page of {name} was left before its files could be shown. Open it "
                     "and choose Review and send… again.")
            return
        if self.jobs.running() is not None:                 # started (on the page) while the look ran
            self._reviewed()
            self.feed.say("Another action is running; nothing was sent.")
            return
        try:
            send = self.ask_release(summary)
        finally:
            self._reviewed()
        if self.quitting:
            return                                           # the window closed while the question was open
        self._follow_page()
        if not send:
            self.feed.say("Nothing was sent.")
        elif os.path.normcase(str(self.site_root)) != os.path.normcase(str(self.review_root)):
            self.feed.say("The site folder was renamed while you decided, so nothing was sent. Choose Review and "
                          "send… again.")
        elif self.jobs.running() is not None:
            self.feed.say("Another action is running; nothing was sent.")
        else:
            # Exactly what was shown: the core looks again and sends nothing if any of it changed meanwhile.
            root, run_dir = self.site_root, summary["run_dir"]
            self._feed_job("release", name, lambda: views.release(root, name, run_dir, expected=summary))

    # ------------------------------------------------------------ short errands
    def set_enabled(self, name: str, enabled: bool) -> None:
        """Pause or resume a feed (audited as done in the app)."""
        if self.toggling:
            return                                           # a second press does nothing
        if self.renaming:
            self.feed.say("The site folder is being renamed; pause or resume the feed once it is done.")
            return
        if self.jobs.running() is not None:
            self.feed.say("Another action is running; wait for it to finish.")
            self.sync_busy()
            return
        self._follow_page()
        root = self.site_root
        self.toggling = True
        self.sync_busy()
        self.errands.run(("enabled", name, enabled), lambda: views.set_enabled(root, name, enabled, via="app"))

    def _ask(self, place: tuple) -> int:
        """Number a request for ``place`` (a run for a feed's result or Activity's, rows for one of them). Errands
        answer in the order they finish, so an answer that is not the newest request's is dropped."""
        self.asked[place] = self.asked.get(place, 0) + 1
        return self.asked[place]

    def open_run(self, feed: str | None, run_id: int) -> None:
        """Show a run (from a feed's History, or from Activity when ``feed`` is None) with its first rows."""
        if feed is not None and feed == self.feed.name and self.feed.running_here():
            self.feed.say("Wait for the running action to finish, then open the run again.")
            return
        self._follow_page()
        root = self.site_root
        n = self._ask(("run", feed))
        self._ask(("rows", "feed" if feed else "activity", feed))         # rows of the run shown before are old

        def look() -> dict:
            detail = views.run_detail(root, run_id)
            try:
                return {"detail": detail, "rows": views.run_rows(root, run_id)}
            except (FxError, LookupError, OSError, ValueError) as exc:
                return {"detail": detail, "rows": None, "rows_error": str(exc)}

        self.errands.run(("run", feed, run_id, n), look)

    def read_rows(self, where: str, feed: str | None, run_id: int, output: str | None) -> None:
        self._follow_page()
        root = self.site_root
        n = self._ask(("rows", where, feed))
        self.errands.run(("rows", where, feed, run_id, n), lambda: views.run_rows(root, run_id, output))

    def open_folder(self, where: str, run_id: int) -> None:
        """Show a run's folder in the file browser, only when it is inside the site's output folder."""
        self._follow_page()
        root = self.site_root
        self.errands.run(("folder", where), lambda: views.open_run_folder(root, run_id))

    def _errand_done(self, key: tuple, result, error: Exception | None) -> None:
        what = key[0]
        message = None if error is None else (str(error) if isinstance(error, (FxError, LookupError)) else
                                              f"{type(error).__name__}: {error}")
        if message:
            log.warning("%s did not work: %s", model.ERRAND_WORDS.get(what, what), message)
        if what == "release-summary":
            if message:
                self.reviewing = False
                self.feed.say(message)
                self.sync_busy()
            else:
                self._release_asked(key[1], result)
        elif what == "enabled":
            _, name, enabled = key
            self.toggling = False
            if self.closing_after_job and self.jobs.running() is None and not self.renaming:
                self.close_now()                             # it was waited for: the window closes now
                return
            if not message:
                self.feed.show_enabled(name, result["enabled"])
            self.feed.say(message or (f"{name} resumed. Its schedule runs it from now on." if enabled else
                                      f"{name} paused."))
            self.sync_busy()
            self.refresh()
        elif what == "run":
            _, feed, _run_id, n = key
            page = self.feed if feed else self.activity
            if feed and (self.feed.results.get(feed) or {}).get("state") == "running":
                if feed == self.feed.name:                   # an action began while the run was read: it stays
                    self.feed.say("Wait for the running action to finish, then open the run again.")
            elif self.asked.get(("run", feed)) != n:
                pass                                         # another run was asked for since
            elif message:
                page.say(message)
            elif feed:
                self.feed.show_run(feed, result["detail"], result["rows"], result.get("rows_error"))
            else:
                self.activity.show_run(result["detail"], result["rows"], result.get("rows_error"))
        elif what == "rows":
            _, where, feed, run_id, n = key
            if self.asked.get(("rows", where, feed)) != n:
                return                                       # another file or run was asked for since
            if where == "feed":
                self.feed.show_rows(feed, run_id, result, message)
            else:
                self.activity.show_rows(run_id, result, message)
        elif what == "folder":
            (self.feed if key[1] == "feed" else self.activity).say(
                message or "The run's folder is open in a new window.")
        else:
            self._setup_errand(key, result, message)

    def sync_busy(self) -> None:
        """Enable actions only while nothing runs (here or on the page); refresh when an action ends; close
        when one was waited for."""
        job = self.jobs.running()
        busy = job is not None
        # A rename asked for waits for a reading of the site before its job starts: nothing else starts meanwhile
        # (a pause begun then would be cut off by the move).
        idle = not busy and not self.closing_after_job and not self.renaming
        self.connection.check_button.setEnabled(idle)
        for page in (self.setup.form, self.connect_form, self.wizard, self.recipes):
            page.set_idle(idle)
        self.options.set_idle(idle and not self.toggling and not self.reviewing)    # a rename waits for both
        self.feed.set_idle(idle, reviewing=self.reviewing, toggling=self.toggling)
        self.preview_action.setEnabled(idle and not self.reviewing)
        if (busy and self.watcher is None and job.feed and job.kind in FEED_KINDS and not self.closing_after_job
                and not self.quitting):
            self._follow(job)                                # started on the browser page
        feed = job.feed if job is not None else None
        if feed != self.running_feed:
            self.running_feed = feed
            self._recaption()
        for action in (self.open_action, self.new_action):
            action.setEnabled(not self.closing_after_job)
        if busy and not self.closing_after_job:
            self.say(f"Running: {_what(job)}. Other actions wait until it finishes.")
        if self.was_busy and not busy:
            # A rename's job ends in its errand's thread a moment before the window hears how it went: the window
            # closes from _renamed, once it has followed the folder (and remembered its new name).
            if self.closing_after_job and not self.toggling and not self.renaming:
                self.close_now()
                return
            self.say(None)
            self.refresh()
        self.was_busy = busy

    def _recaption(self) -> None:
        """Say in the sidebar which feed's action runs, without rebuilding it (the focus stays where it is)."""
        feeds = {f["name"]: f for f in ((self.snap or {}).get("summary") or {}).get("feeds") or []}
        for i in range(self.sidebar.count()):
            item = self.sidebar.item(i)
            name = str(item.data(Qt.ItemDataRole.UserRole)).removeprefix("feed:")
            if name in feeds and item.data(Qt.ItemDataRole.UserRole) == f"feed:{name}":
                caption = self._feed_caption(feeds[name])
                item.setText(caption)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, caption)

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
        if self.quitting or (job is None and not self.toggling and not self.renaming):
            self.shut_down()
            event.accept()
            return
        event.ignore()
        if self.closing_after_job:
            return
        if job is None:
            # A pause or resume is being written, in a thread that quitting would cut off between rewriting the
            # feed file and recording it in the activity log; or a rename is starting or ending, and the window
            # must follow the folder before it closes. Both are short: close as soon as they are done.
            self.closing_after_job = True
            self.say("This window closes once the pause or resume is written." if self.toggling else
                     "This window closes once the site folder is renamed.")
            self.sync_busy()
            return
        if self.ask_close_while_running(job) == "later":
            if self.jobs.running() is None and not self.toggling and not self.renaming:   # it ended meanwhile
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
