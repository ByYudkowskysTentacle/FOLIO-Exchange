"""The desktop app's window, site picker and job watcher, run offscreen with pytest-qt."""
from __future__ import annotations

import logging
import sys
import threading
import uuid
from pathlib import Path

import pytest
from conftest import QT_LOADS

if not QT_LOADS:
    pytest.skip("needs the desktop extra (PySide6) and pytest-qt, and a Qt that loads", allow_module_level=True)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QMessageBox, QPushButton  # noqa: E402
from sitekit import make_feed  # noqa: E402

from folio_exchange import siteops, views  # noqa: E402
from folio_exchange.desktop import app as desktop_app  # noqa: E402
from folio_exchange.desktop import model, pages, places, single  # noqa: E402
from folio_exchange.desktop import picker as picker_module  # noqa: E402
from folio_exchange.desktop import window as window_module  # noqa: E402
from folio_exchange.desktop.feed import FeedPage  # noqa: E402
from folio_exchange.desktop.picker import NewSiteDialog, SitePicker  # noqa: E402
from folio_exchange.desktop.window import MainWindow, app_command  # noqa: E402
from folio_exchange.desktop.worker import JobWatcher  # noqa: E402
from folio_exchange.errors import FxError  # noqa: E402
from folio_exchange.jobs import Jobs  # noqa: E402

work_log = logging.getLogger("folio_exchange.test_desktop")


def window(qtbot, root, **kw):
    """A window on ``root`` that opens nothing in a real browser; its first reading of the site done. Closing it
    while an action runs keeps it open without asking, so a test that fails half way never leaves a question
    on screen that nobody answers."""
    opened: list[str] = []
    w = MainWindow(root, open_url=opened.append, **kw)
    w.opened = opened
    w.ask_close_while_running = lambda job: "keep"
    qtbot.addWidget(w)
    w.show()
    qtbot.waitUntil(lambda: w.snap is not None and not w.reader.busy, timeout=15000)
    return w


def sidebar(w) -> list[str]:
    return [w.sidebar.item(i).text() for i in range(w.sidebar.count())]


@pytest.fixture
def blocking_job(qtbot):
    """Starts a job that runs until the test lets it go. Any still held are let go when the test ends, before
    qtbot closes its windows."""
    held: list[threading.Event] = []

    def start(jobs: Jobs, kind: str = "run", feed: str | None = "chq"):
        go = threading.Event()
        held.append(go)

        def work():
            work_log.info("started")
            if not go.wait(20):
                raise FxError("not let go")
            work_log.info("finishing")
            return {"done": True}

        return jobs.start(kind, feed, work), go

    yield start
    for go in held:
        go.set()


# ------------------------------------------------------------ the window
def test_the_window_shows_the_sites_feeds(qtbot, site):
    w = window(qtbot, site)
    assert w.windowTitle() == "FOLIO-Exchange – site"
    assert sidebar(w) == ["Home", "chq – On, no runs yet", "Add a vendor feed", "Activity", "Recipes", "Connection",
                          "Options"]
    assert w.stack.currentWidget() is w.home
    assert w.home.table.rowCount() == 1 and w.home.table.item(0, 0).text() == "chq"
    assert w.home.table.item(0, 3).text() == "On"
    assert w.home.folio["tenant"].text() == "diku"
    assert w.home.folio["password"].text() == "From the environment variable FOLIO_PASSWORD"
    assert "secret" not in w.home.folio["password"].text()            # where it comes from, never its value
    w.select("feed:chq")
    assert w.stack.currentWidget() is w.feed
    assert "chq-academic@" in w.feed.fields["recipe"].text()
    assert w.feed.fields["schedule"].text() and w.feed.fields["last"].text() == "never"
    assert "vendor-drop" in w.feed.fields["delivery"].text()


def test_every_control_has_an_accessible_name(qtbot, site):
    from PySide6.QtWidgets import (
        QAbstractButton,
        QComboBox,
        QLineEdit,
        QListWidget,
        QPlainTextEdit,
        QSpinBox,
        QTableWidget,
    )

    w = window(qtbot, site)
    for kind in (QAbstractButton, QListWidget, QPlainTextEdit, QTableWidget, QLineEdit, QComboBox, QSpinBox):
        for control in w.findChildren(kind):
            if not control.objectName().startswith("qt_"):         # Qt's own (the menu bar's overflow button)
                assert control.accessibleName(), control
    shortcuts = {a.shortcut().toString() for a in w.actions()}
    assert {"F5", "Ctrl+O", "Ctrl+N"} <= shortcuts


def test_a_refresh_shows_changes(qtbot, site):
    w = window(qtbot, site)
    make_feed(site, "second", file_prefix="second",
              delivery={"transport": "local", "directory": str(site / "drop-2")})
    views.set_enabled(site, "chq", False, via="ui")
    qtbot.keyClick(w, Qt.Key.Key_F5)
    qtbot.waitUntil(lambda: len(sidebar(w)) == 8, timeout=15000)
    assert "chq – Paused, no runs yet" in sidebar(w) and "second – On, no runs yet" in sidebar(w)


def test_a_site_that_cannot_be_read_says_so_and_keeps_the_last_view(qtbot, site, monkeypatch):
    w = window(qtbot, site)
    monkeypatch.setattr(model, "snapshot", lambda root: (_ for _ in ()).throw(OSError("the drive is gone")))
    w.refresh()
    qtbot.waitUntil(lambda: "Couldn't load" in w.banner.text(), timeout=15000)
    assert w.banner.isVisible()
    assert "Couldn't load the site: the drive is gone" in w.banner.text()
    assert "chq – On, no runs yet" in sidebar(w)


def test_a_folder_not_set_up_yet_starts_with_the_connection_to_folio(qtbot, tmp_path):
    root = tmp_path / "new-site"
    root.mkdir()
    w = window(qtbot, root)
    assert sidebar(w) == ["Set up this site"]
    assert w.stack.currentWidget() is w.setup and w.setup.title.text() == "Connect to FOLIO"
    assert w.setup.form.isVisible() and not w.setup.what.isVisible()
    assert str(root) in w.setup.folder.text()
    w.open_page("home")                                                # the browser page stays a second way in
    assert len(w.opened) == 1 and "?t=" in w.opened[0]
    w.close()
    assert w.page.httpd is None                                        # the page stops with the window


def test_site_yaml_that_does_not_load_is_shown(qtbot, tmp_path):
    (tmp_path / "site.yaml").write_text("folio: [not, a, mapping]\n", encoding="utf-8")
    w = window(qtbot, tmp_path)                    # regression: site_state failed on a folio: that is a list
    assert w.stack.currentWidget() is w.setup
    assert w.setup.title.text() == "This site's settings don't load" and w.setup.what.isVisible()


def test_a_first_reading_that_fails_is_shown_in_place(qtbot, site, monkeypatch):
    monkeypatch.setattr(model, "snapshot", lambda root: (_ for _ in ()).throw(OSError("the drive is gone")))
    w = MainWindow(site, open_url=lambda url: None)
    qtbot.addWidget(w)
    qtbot.waitUntil(lambda: "the drive is gone" in w.reading.text(), timeout=15000)
    assert w.stack.currentWidget() is w.reading and "F5" in w.reading.text()
    assert w.reading.textFormat() == Qt.TextFormat.PlainText           # an error's text, shown as written


# ------------------------------------------------------------ actions
def test_the_job_watcher_streams_lines_then_the_result(qtbot, caplog, blocking_job):
    caplog.set_level(logging.INFO, logger="folio_exchange")            # as the app's log setup does
    jobs = Jobs()
    job, go = blocking_job(jobs)
    watcher = JobWatcher(job, interval_ms=20)
    lines: list[str] = []
    watcher.progress.connect(lines.extend)
    watcher.start()
    qtbot.waitUntil(lambda: any("started" in x for x in lines), timeout=5000)
    with qtbot.waitSignal(watcher.finished, timeout=5000) as done:
        go.set()
    assert done.args[0]["status"] == "done" and done.args[0]["result"] == {"done": True}
    assert any("finishing" in x for x in lines)
    assert not watcher.timer.isActive()


def test_check_the_connection_streams_the_log_and_the_result(qtbot, site, caplog):
    caplog.set_level(logging.INFO, logger="folio_exchange")
    w = window(qtbot, site)
    w.select("connection")
    qtbot.mouseClick(w.connection.check_button, Qt.MouseButton.LeftButton)
    assert not w.connection.check_button.isEnabled()                  # one action at a time
    qtbot.waitUntil(lambda: w.connection.state.text().startswith("Logged in"), timeout=30000)
    assert w.connection.log.toPlainText()                              # the job's own log lines
    assert "OK –" in w.connection.result.text()
    qtbot.waitUntil(lambda: w.connection.check_button.isEnabled(), timeout=5000)


def test_a_second_action_waits_for_the_first(qtbot, site, blocking_job):
    w = window(qtbot, site)
    job, go = blocking_job(w.jobs)
    w.sync_busy()
    assert not w.connection.check_button.isEnabled()
    assert "Running: run of chq" in w.banner.text()
    w.check_connection()                                               # refused, as Jobs refuses it
    assert "Another action is still running" in w.connection.state.text()
    go.set()
    qtbot.waitUntil(lambda: w.connection.check_button.isEnabled(), timeout=5000)


def test_closing_while_an_action_runs_asks_and_waits(qtbot, site, blocking_job):
    w = window(qtbot, site)
    w.show()
    job, go = blocking_job(w.jobs)
    asked = []
    w.ask_close_while_running = lambda j: asked.append(j) or "keep"
    w.close()
    assert asked == [job] and w.isVisible() and not w.closing_after_job
    w.ask_close_while_running = lambda j: asked.append(j) or "later"
    w.close()
    assert w.isVisible() and w.closing_after_job                        # not quitting yet
    assert "closes when the run of chq finishes" in w.banner.text()
    assert not w.open_action.isEnabled() and not w.connection.check_button.isEnabled()
    w.close()
    assert len(asked) == 2                                             # asked once, then it just waits
    go.set()
    qtbot.waitUntil(lambda: not w.isVisible(), timeout=5000)           # closed once the run ended
    assert job.status == "done"


def test_the_browser_page_shares_the_windows_job_runner(qtbot, site, blocking_job):
    jobs = Jobs()
    w = window(qtbot, site, jobs=jobs)
    w.open_page("activity")
    assert w.opened[0].endswith("#activity") and "?t=" in w.opened[0]
    w.open_page(feed="chq")
    assert w.opened[1].endswith("#feed/chq")
    assert w.page.app.jobs is jobs
    job, go = blocking_job(jobs)
    with pytest.raises(FxError, match="Another action is still running"):
        w.page.app.jobs.start("check", None, lambda: None)
    go.set()
    job.thread.join(5)
    httpd = w.page.httpd
    w.close()
    assert w.page.httpd is None and httpd.socket.fileno() == -1


def test_the_window_follows_a_rename_made_on_the_page(qtbot, site):
    make_feed(site, delivery={"transport": "local", "directory": "vendor-drop"})     # nothing by full path
    name = places.instance_name(site)
    server = single.InstanceServer(name)
    assert server.listen()
    w = window(qtbot, site, instance=server)
    w.open_page("options")
    res = siteops.rename_site(site, "renamed", via="ui")
    w.page.app.site_root = type(site)(res["root"])                    # as the page's rename does
    w.refresh()
    qtbot.waitUntil(lambda: w.windowTitle() == "FOLIO-Exchange – renamed", timeout=15000)
    assert w.site_root.name == "renamed"
    assert server.name == places.instance_name(w.site_root)
    assert places.recent_sites()[0].name == "renamed"
    w.close()


def test_an_action_that_ends_while_asked_lets_the_window_close(qtbot, site, blocking_job):
    """Regression: when the action ended while the question was open, "Close when it finishes" left the window
    open for good, with its actions disabled, until some later action closed it by surprise."""
    w = window(qtbot, site)
    job, go = blocking_job(w.jobs)
    w.sync_busy()

    def ask(_job):
        go.set()
        job.thread.join(5)
        w.sync_busy()                             # the busy timer ticks on inside the question's event loop
        return "later"

    w.ask_close_while_running = ask
    w.close()
    assert not w.isVisible() and not w.closing_after_job and w.quitting


def test_a_check_right_after_a_rename_on_the_page_uses_the_new_folder(qtbot, site):
    """Regression: only a refresh followed a rename made on the page, so a check started before the next one
    ran against the old folder, which no longer exists."""
    make_feed(site, delivery={"transport": "local", "directory": "vendor-drop"})     # nothing by full path
    w = window(qtbot, site)
    w.open_page("options")
    res = siteops.rename_site(site, "renamed", via="ui")
    w.page.app.site_root = type(site)(res["root"])                    # as the page's rename does
    w.check_connection()                                               # no refresh in between
    # Wait for the check itself to end: just after it starts, the button can still be enabled for a moment
    # (the window marks itself busy on its next look), which once let this read the "Logging in…" line.
    qtbot.waitUntil(lambda: w.jobs.running() is None and w.watcher is None
                    and w.connection.check_button.isEnabled(), timeout=30000)
    assert w.connection.state.text().startswith("Logged in"), w.connection.state.text()
    assert w.site_root.name == "renamed"


def test_following_the_page_does_not_touch_the_disk(qtbot, site, monkeypatch):
    """Regression: every refresh resolved the page's folder on the window's thread, which froze the window
    while a network share was not answering."""
    w = window(qtbot, site)
    w.open_page("home")

    def stuck(self, strict=False):
        raise AssertionError("the window's thread touched the disk")

    monkeypatch.setattr(Path, "resolve", stuck)
    w._follow_page()                                                   # the same folder: nothing to do
    monkeypatch.undo()
    assert w.site_root == site.resolve()


def test_titles_and_errors_are_shown_as_written_never_as_markup(qtbot, monkeypatch):
    """Regression: a feed or recipe title holding <img src=...> was read as markup, and Qt fetched the file,
    even from another computer, which the page's own content policy blocks."""
    sneaky = 'Academic <img src="file://example.invalid/share/x.png"> <b>bold</b>'
    page = FeedPage()
    qtbot.addWidget(page)
    page.show_feed({"name": "chq", "title": sneaky}, [])
    assert page.title.textFormat() == Qt.TextFormat.PlainText and page.title.text() == sneaky
    boxes = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: boxes.append((box.textFormat(), box.text())) or 0)
    pages.alert(None, f"Could not open {sneaky}")
    assert boxes == [(Qt.TextFormat.PlainText, f"Could not open {sneaky}")]
    dialog = NewSiteDialog()
    qtbot.addWidget(dialog)
    assert dialog.full.textFormat() == Qt.TextFormat.PlainText


def test_screen_readers_hear_what_each_label_shows(qtbot, site):
    """Regression: labels carried a fixed accessible name ("Check status", "Notice"), which screen readers read
    instead of the message itself."""
    from PySide6.QtGui import QAccessible, QTextDocument
    from PySide6.QtWidgets import QLabel

    def shown(label: QLabel) -> str:
        if label.buddy() is not None:                                  # a form's caption: its mnemonic is not read
            return label.text().replace("&&", "\0").replace("&", "").replace("\0", "&")
        if label.textFormat() != Qt.TextFormat.RichText:
            return label.text()
        doc = QTextDocument()
        doc.setHtml(label.text())
        return doc.toPlainText()

    w = window(qtbot, site)
    w.connection.refused("Another action is still running")
    w.say("Running: run of chq.")
    heard = 0
    for key in ("home", "feed:chq", "connection", "activity", "recipes", "options", "add"):
        w.select(key)
        for label in [w.banner, *w.stack.currentWidget().findChildren(QLabel)]:
            if label.isVisible() and label.text() and not label.objectName().startswith("qt_"):
                name = QAccessible.queryAccessibleInterface(label).text(QAccessible.Text.Name)
                assert name == shown(label), (name, label.text())
                heard += 1
    assert heard > 20


def test_button_mnemonics_never_clash_with_the_menu(qtbot, site):
    """Regression: "&Set it up in the browser page" and the "&Site" menu both took Alt+S, so the key did
    neither."""
    from PySide6.QtWidgets import QAbstractButton

    def letter(caption: str) -> str | None:
        at = caption.replace("&&", "").find("&")
        return caption.replace("&&", "")[at + 1].lower() if at >= 0 else None

    w = window(qtbot, site)
    menu = {letter(a.text()) for a in w.menuBar().actions()} - {None}
    assert menu == {"s"}
    for i in range(w.stack.count()):
        letters = [x for x in (letter(b.text()) for b in w.stack.widget(i).findChildren(QAbstractButton)) if x]
        assert len(letters) == len(set(letters)) and not set(letters) & menu, letters


def test_another_site_never_starts_from_the_current_folder(tmp_path, monkeypatch):
    """Regression: ``python -m`` started in the app's current folder imports from it first, so a yaml.py left in
    a shared folder would have run as the person when they opened another site."""
    other = tmp_path / "other"
    exe, args, folder = app_command(other)
    assert args[-2:] == ["--site", str(other)] and Path(folder) == Path(sys.executable).parent
    assert "-m" not in args or "-P" in args
    monkeypatch.setattr(window_module.sysconfig, "get_path", lambda _name: str(tmp_path))   # no app script
    exe, args, folder = app_command(other)
    assert args == [*(["-P"] if sys.version_info >= (3, 11) else []), "-m", "folio_exchange.desktop",
                    "--site", str(other)]
    assert Path(folder) == Path(sys.executable).parent


# ------------------------------------------------------------ the site picker
def test_the_picker_refuses_a_folder_without_site_yaml(qtbot, tmp_path, site):
    places.remember_site(site)
    picker = SitePicker()
    qtbot.addWidget(picker)
    assert picker.recent.count() == 1 and str(site) in picker.recent.item(0).text()
    picker.ask_folder = lambda: str(tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    picker.open_folder()
    assert picker.chosen is None and "There's no site.yaml" in picker.error.text()
    picker.ask_folder = lambda: str(site)
    picker.open_folder()
    assert picker.chosen == site.resolve() and not picker.is_new


def test_a_first_start_offers_to_set_up_a_site_outside_synced_folders(qtbot, tmp_path, monkeypatch):
    """With no recent sites, Enter sets up a new site, which starts in Documents, or in the home folder when
    Documents is in OneDrive (a site must not live in a synced folder)."""
    picker = SitePicker()
    qtbot.addWidget(picker)
    new = next(b for b in picker.findChildren(QPushButton) if b.accessibleName() == "Set up a new site")
    assert new.isDefault() and not picker.open_button.isEnabled()
    docs, home = tmp_path / "OneDrive" / "Documents", tmp_path / "home"
    docs.mkdir(parents=True)
    home.mkdir()

    class Paths:
        StandardLocation = picker_module.QStandardPaths.StandardLocation

        @staticmethod
        def writableLocation(where):  # noqa: N802 - Qt's naming
            return str(docs if where == Paths.StandardLocation.DocumentsLocation else home)

    monkeypatch.setattr(picker_module, "QStandardPaths", Paths)
    dialog = NewSiteDialog()
    qtbot.addWidget(dialog)
    assert dialog.parent_edit.text() == str(home)
    dialog.name_edit.setText("Lakeside Library")
    assert str(home / "Lakeside Library") in dialog.full.text()
    plain = tmp_path / "Documents"
    plain.mkdir()
    docs = plain
    assert NewSiteDialog().parent_edit.text() == str(plain)             # Documents, when it is not synced


def test_a_new_site_needs_a_good_name(qtbot, tmp_path):
    dialog = NewSiteDialog()
    qtbot.addWidget(dialog)
    dialog.parent_edit.setText(str(tmp_path))
    dialog.name_edit.setText("Main|Library")
    dialog.accept()
    assert dialog.chosen is None and "cannot contain" in dialog.error.text()
    assert not (tmp_path / "Main|Library").exists()
    dialog.parent_edit.setText(str(tmp_path / "missing"))
    dialog.name_edit.setText("Main Library")
    dialog.accept()
    assert "isn't a folder that exists" in dialog.error.text() and not (tmp_path / "missing").exists()
    dialog.parent_edit.setText(str(tmp_path))
    assert str(tmp_path / "Main Library") in dialog.full.text()
    dialog.accept()
    assert dialog.chosen == (tmp_path / "Main Library").resolve() and dialog.chosen.is_dir()


def test_a_named_folder_that_is_new_becomes_a_new_site(tmp_path):
    root = desktop_app.site_from(str(tmp_path / "fresh"))
    assert root == (tmp_path / "fresh").resolve() and root.is_dir()
    (tmp_path / "half" / "feeds").mkdir(parents=True)
    with pytest.raises(FxError, match="No site.yaml"):
        desktop_app.site_from(str(tmp_path / "half"))


# ------------------------------------------------------------ errors in slots
def test_an_unexpected_error_is_logged_and_shown(qtbot, monkeypatch, caplog):
    shown = []
    monkeypatch.setattr(desktop_app, "alert", lambda parent, message, icon=None: shown.append(message))
    try:
        raise ValueError("boom")
    except ValueError as exc:
        with caplog.at_level(logging.ERROR):
            desktop_app.report_unexpected(type(exc), exc, exc.__traceback__)
    assert "Unexpected error" in caplog.text and "boom" in caplog.text
    assert shown and "ValueError: boom" in shown[0]


@pytest.mark.parametrize("where", ["the site's log folder", "a new site's folder"])
def test_a_failure_before_the_window_opens_is_shown_and_logged(qtbot, tmp_path, monkeypatch, caplog, where):
    """Regression: only FxError was caught before the window opened, so without a console (pythonw) an offline
    log folder or a folder that cannot be made closed the app with nothing shown and nothing logged."""
    shown = []
    monkeypatch.setattr(desktop_app, "alert", lambda parent, message, icon=None: shown.append(message))

    def setup_logging(folder, verbose, console=True):
        if folder is not None and where == "the site's log folder":
            raise FileNotFoundError(53, "The network path was not found", str(folder))

    def make_site_folder(root):
        raise PermissionError(13, "Access is denied", str(root))

    monkeypatch.setattr(desktop_app, "setup_logging", setup_logging)                 # leave the test's logging be
    if where == "a new site's folder":
        monkeypatch.setattr(desktop_app.siteops, "make_site_folder", make_site_folder)
    with caplog.at_level(logging.ERROR):
        assert desktop_app.run(str(tmp_path / "fresh"), console=False) == 1
    assert len(shown) == 1 and shown[0].startswith("FOLIO-Exchange couldn't start:")
    assert ("FileNotFoundError" if where == "the site's log folder" else "PermissionError") in shown[0]
    assert "could not start" in caplog.text and "Traceback" in caplog.text


def test_the_app_hands_over_to_the_open_window(qtbot, site, monkeypatch):
    name = f"fx-test-{uuid.uuid4().hex[:16]}"
    monkeypatch.setattr(places, "instance_name", lambda root: name)
    monkeypatch.setattr(desktop_app, "setup_logging", lambda *a, **kw: None)     # leave the test's logging be
    first = single.InstanceServer(name)
    assert first.listen()
    shown = []
    first.shown.connect(lambda: shown.append(1))
    assert desktop_app.run(str(site)) == 0                             # a second start: no second window
    assert shown == [1]
    first.close()


# ------------------------------------------------------------ regressions from the second review
def test_a_new_site_left_unfinished_reopens_from_the_recent_list(qtbot, tmp_path):
    """Regression: a site made in the picker and closed before its setup was done could not be opened from the
    picker again (no site.yaml), nor set up again under its name (its log folder made it a folder in use)."""
    dialog = NewSiteDialog()
    qtbot.addWidget(dialog)
    dialog.parent_edit.setText(str(tmp_path))
    dialog.name_edit.setText("Main")
    dialog.accept()
    root = dialog.chosen
    assert root == (tmp_path / "Main").resolve()
    logs = desktop_app.logs_dir(root)                       # what the app writes as it opens the window
    logs.mkdir(parents=True)
    (logs / "folio-exchange.log").write_text("Desktop app opened\n", encoding="utf-8")
    places.remember_site(root)
    picker = SitePicker()                                   # the next start
    qtbot.addWidget(picker)
    assert picker.recent.count() == 1
    picker.open_recent()
    assert picker.chosen == root and not picker.error.isVisible()
    again = NewSiteDialog()
    qtbot.addWidget(again)
    again.parent_edit.setText(str(tmp_path))
    again.name_edit.setText("Main")
    again.accept()
    assert again.chosen == root
    w = window(qtbot, root)
    assert w.stack.currentWidget() is w.setup               # it opens on "Set up this site"


def test_a_late_finish_from_an_older_check_leaves_the_newer_one_alone(qtbot, site, monkeypatch):
    """Regression: the busy timer could enable Check before the last check's watcher had looked a final time;
    a new check then started, and the old watcher's late finish showed the old result and deleted the new
    check's watcher, so its log and result never appeared."""
    gates = []

    def check(root):
        go = threading.Event()
        gates.append(go)
        assert go.wait(20)
        return {"endpoints": [], "n": len(gates)}

    monkeypatch.setattr(views, "check_connection", check)
    w = window(qtbot, site)
    finished = []
    monkeypatch.setattr(w.connection, "finished", finished.append)
    w.check_connection()
    first = w.watcher
    first.timer.stop()                                     # its last look has not come yet
    qtbot.waitUntil(lambda: len(gates) == 1, timeout=5000)
    gates[0].set()
    w.jobs.current.thread.join(5)
    w.sync_busy()
    assert w.connection.check_button.isEnabled()
    w.check_connection()                                   # the person presses Check again
    second = w.watcher
    assert second is not first
    # The older one reported its end before the newer began (D2 review, qt-4: else a feed's result stayed
    # "running"), and only once.
    assert [f["result"]["n"] for f in finished] == [1]
    first.poll()                                           # the older watcher's late finish
    assert w.watcher is second and len(finished) == 1      # neither shown again nor taking the newer one's place
    qtbot.waitUntil(lambda: len(gates) == 2, timeout=5000)  # the newer check's thread has reached it
    gates[1].set()
    qtbot.waitUntil(lambda: len(finished) == 2, timeout=5000)
    assert finished[1]["result"]["n"] == 2 and w.watcher is None


def test_the_picker_lists_recent_sites_without_looking_at_them(qtbot, tmp_path, monkeypatch):
    """Regression: the picker checked each recent folder on the window's thread before it appeared, which took
    a minute or more when a share in the list had stopped answering."""
    gone = tmp_path / "gone"
    places.remember_site(gone)
    settings = places.config_dir()

    def guard(name):
        real = getattr(Path, name)

        def look(self, *a, **k):
            if self == settings or settings in self.parents:
                return real(self, *a, **k)
            raise AssertionError(f"the picker touched {self}")

        return look

    with monkeypatch.context() as m:
        for name in ("is_dir", "exists", "resolve", "stat"):
            m.setattr(Path, name, guard(name))
        picker = SitePicker()
        qtbot.addWidget(picker)
    assert picker.recent.count() == 1
    picker.open_recent()                                   # only opening it looks, and says it has gone
    assert picker.chosen is None and "isn't a folder that exists" in picker.error.text()


def test_a_check_during_a_rename_on_the_page_is_refused_at_once(qtbot, site, monkeypatch):
    """Regression: the page held the jobs' lock for the whole rename, so pressing Check in the window froze it
    until the rename ended, then checked the old folder."""
    make_feed(site, delivery={"transport": "local", "directory": "vendor-drop"})     # nothing by full path
    w = window(qtbot, site)
    with w.jobs.holding("rename"):                         # as the page's rename does
        w.sync_busy()
        assert not w.connection.check_button.isEnabled() and "Running: rename" in w.banner.text()
        w.check_connection()
        assert "Another action is still running (rename)" in w.connection.state.text()
        assert w.watcher is None
    qtbot.waitUntil(lambda: w.connection.check_button.isEnabled(), timeout=5000)


def test_a_window_open_in_another_windows_session_is_reported(qtbot, site, monkeypatch):
    shown = []
    monkeypatch.setattr(desktop_app, "alert", lambda parent, message, icon=None: shown.append(message))
    monkeypatch.setattr(desktop_app, "setup_logging", lambda *a, **kw: None)     # leave the test's logging be
    monkeypatch.setattr(desktop_app.single, "claim", lambda name: ("elsewhere", None))
    assert desktop_app.run(str(site), console=False) == 1
    assert shown == [desktop_app.ELSEWHERE] and "another of your Windows sessions" in shown[0]


# ------------------------------------------------------------ garbage collection off the window's thread
class _ReadFailed(Exception):
    """An error a weak reference can follow (built-in exceptions take none)."""


def test_a_failed_read_leaves_no_cycle_for_another_thread_to_free(qtbot):
    """Regression: a failed read's error held its traceback, which held the reading thread's frame, which held the
    error and the Reader. Only a garbage collection freed that cycle, and on Linux CI one ran in a reading thread
    and freed Qt objects there, which crashed the run."""
    import gc
    import weakref

    from folio_exchange.desktop.worker import Reader

    reader = Reader()
    got = []
    reader.done.connect(lambda result, error: got.append(weakref.ref(error)))

    def fail():
        raise _ReadFailed("the drive is gone")

    gc.disable()                        # as while the app runs; the conftest fixture has it off already
    reader.read(fail)
    qtbot.waitUntil(lambda: bool(got) and not any(t.name == "fx-app-read" for t in threading.enumerate()),
                    timeout=5000)
    assert got[0]() is None             # freed when the last reference went, with no collection


def test_cyclic_garbage_is_collected_on_the_windows_thread_only(qtbot):
    """Regression: with automatic collection on, a reading or job thread could free a Qt object kept alive by a
    cycle, off the window's thread, and crash the process."""
    import gc

    from folio_exchange.desktop.worker import MainThreadCollector

    class Node:
        def __init__(self):
            self.me = self

    threads: list[str] = []

    def seen(phase, info):
        if phase == "start":
            threads.append(threading.current_thread().name)

    collector = MainThreadCollector(interval_ms=10).start()
    gc.callbacks.append(seen)
    try:
        assert not gc.isenabled()

        def litter():
            for _ in range(gc.get_threshold()[0] * 5):
                Node()                  # cycles enough to start several collections, were it on

        worker = threading.Thread(target=litter, name="fx-test-litter")
        worker.start()
        worker.join()
        assert "fx-test-litter" not in threads
        qtbot.waitUntil(lambda: bool(threads), timeout=5000)
        assert set(threads) == {threading.main_thread().name}
    finally:
        gc.callbacks.remove(seen)
        collector.stop()
    assert not gc.isenabled()           # back as it was: the conftest fixture's collector has it off


def test_the_app_collects_garbage_on_its_own_thread(qtbot, site, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from folio_exchange.desktop.worker import MainThreadCollector

    running = []

    def run(qapp, named, verbose, console):
        running.extend(c for c in qapp.findChildren(MainThreadCollector) if c.timer.isActive())
        return 0

    monkeypatch.setattr(desktop_app, "_run", run)
    assert desktop_app.run(str(site)) == 0
    assert len(running) == 1
    assert not running[0].timer.isActive()                             # stopped when the app ends
    assert not [c for c in QApplication.instance().findChildren(MainThreadCollector) if c.timer.isActive()]
