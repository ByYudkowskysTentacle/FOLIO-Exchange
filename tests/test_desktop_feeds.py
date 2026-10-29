"""The desktop app's feed pages, Home and Activity (D2): every action the page's feed and activity views allow,
run offscreen with pytest-qt against the mock FOLIO."""
from __future__ import annotations

import logging
import re
import threading

import pytest
from conftest import QT_LOADS

if not QT_LOADS:
    pytest.skip("needs the desktop extra (PySide6) and pytest-qt, and a Qt that loads", allow_module_level=True)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QAbstractButton, QApplication, QDialog, QLabel, QTableWidget  # noqa: E402
from sitekit import make_feed  # noqa: E402

from folio_exchange import client as client_mod  # noqa: E402
from folio_exchange import views  # noqa: E402
from folio_exchange.config import load_site  # noqa: E402
from folio_exchange.desktop import window as window_module  # noqa: E402
from folio_exchange.desktop.activity import ActivityPage  # noqa: E402
from folio_exchange.desktop.pages import HomePage  # noqa: E402
from folio_exchange.desktop.release import ReleaseDialog  # noqa: E402
from folio_exchange.desktop.runview import RunView  # noqa: E402
from folio_exchange.desktop.window import MainWindow  # noqa: E402
from folio_exchange.errors import FxError  # noqa: E402
from folio_exchange.history import History  # noqa: E402
from folio_exchange.jobs import Jobs  # noqa: E402

LONG = 60000                                   # ms: a run against the mock FOLIO takes a few seconds


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def window(qtbot, root, **kw):
    """A window on ``root`` that opens nothing in a real browser and never leaves a question open."""
    opened: list[str] = []
    w = MainWindow(root, open_url=opened.append, **kw)
    w.opened = opened
    w.ask_close_while_running = lambda job: "keep"
    w.ask_release = lambda summary: pytest.fail("a release question was asked that the test did not expect")
    qtbot.addWidget(w)
    w.show()
    qtbot.waitUntil(lambda: w.snap is not None and not w.reader.busy, timeout=15000)
    return w


def on_feed(qtbot, w, name="chq"):
    w.select(f"feed:{name}")
    assert w.stack.currentWidget() is w.feed and w.feed.name == name
    return w.feed


def settled(qtbot, w, timeout=LONG):
    """Wait until no action runs, the window has read the site again and no errand is out."""
    qtbot.waitUntil(lambda: w.jobs.running() is None and w.watcher is None and not w.reader.busy
                    and not w.errands.pending and w.feed.preview_button.isEnabled(), timeout=timeout)


def shown(widget) -> bool:
    """Not hidden by the page (a widget on a tab not chosen counts as shown)."""
    return not widget.isHidden()


def result(w, name="chq") -> dict:
    return w.feed.results.get(name) or {}


def sidebar(w) -> list[str]:
    return [w.sidebar.item(i).text() for i in range(w.sidebar.count())]


@pytest.fixture
def blocking_job():
    held: list[threading.Event] = []

    def start(jobs: Jobs, kind: str = "run", feed: str | None = "chq"):
        go = threading.Event()
        held.append(go)

        def work():
            if not go.wait(20):
                raise FxError("not let go")
            return {"done": True}

        return jobs.start(kind, feed, work), go

    yield start
    for go in held:
        go.set()


# ------------------------------------------------------------ what a feed's page shows
def test_a_feeds_page_shows_its_sections(qtbot, site):
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    assert [page.tabs.tabText(i) for i in range(page.tabs.count())] == [
        "Overview", "Layout", "Settings", "Delivery", "History (0)"]
    assert page.fields["uses"].text() == "chq-academic@^1.0"
    assert page.fields["file"].text().endswith("chq.yaml")
    assert "→" in page.sub.text() and "vendor-drop" in page.sub.text()
    titles = [x.text() for x in page.tabs.widget(1).findChildren(QLabel)]
    assert any("MARC 21 · test_bibs_YYYYMMDD.mrc" in t for t in titles)
    columns = [t for t in page.tabs.widget(1).findChildren(type(page.history)) if t.accessibleName() == "Columns of items"]
    assert columns and columns[0].item(0, 1).text().startswith("Bib record ID")
    assert "Locations left out (exclude_location_codes): ONLINE" in page.filters.text()
    assert page.checks_facts.values["Must be filled"].text()
    assert shown(page.legacy) and "Loan count from the old system" in page.legacy.text()
    assert page.where.values["Sends to"].text() == "A folder on this computer or the network"
    assert not shown(page.test_why) and shown(page.test_button)
    assert shown(page.no_runs) and not shown(page.waiting)
    assert "No result yet" in page.result.head.text()


def test_a_feed_with_nowhere_to_send_is_a_dry_run(qtbot, site):
    make_feed(site, "dry", file_prefix="dry", delivery={"transport": "none"})
    w = window(qtbot, site)
    page = on_feed(qtbot, w, "dry")
    assert "it is a dry run" in page.notices.text()
    assert page.where.values["Sends to"].text().startswith("Nowhere yet")
    assert not shown(page.test_button)


def test_an_sftp_delivery_shows_where_the_password_comes_from_never_the_password(qtbot, site, monkeypatch):
    """Regression: the test had no password at all, so it could not see one being shown."""
    secret = "Zq-7very-distinctive-sftp-secret"
    monkeypatch.setenv("FX_TEST_SFTP_PASSWORD", secret)
    make_feed(site, "vendor", file_prefix="vendor", enabled=False, delivery={
        "transport": "sftp", "host": "sftp.example.invalid", "username": "lib", "remote_dir": "in",
        "secret": "env:FX_TEST_SFTP_PASSWORD", "host_key_fingerprint": "SHA256:" + "r" * 43,
        "host_key_confirmed": False})
    w = window(qtbot, site)
    page = on_feed(qtbot, w, "vendor")
    assert page.where.values["Server"].text() == "sftp.example.invalid:22"
    assert "Not confirmed yet" in page.where.values["Server key"].text()
    assert page.where.values["Login"].text() == ("With a password. From the environment variable "
                                                 "FX_TEST_SFTP_PASSWORD.")
    assert shown(page.test_why) and not page.test_button.isEnabled()     # the test waits for the key
    texts = [x.text() for x in w.findChildren(QLabel)]
    texts += [t.item(r, c).text() for t in w.findChildren(QTableWidget) for r in range(t.rowCount())
              for c in range(t.columnCount()) if t.item(r, c) is not None]
    assert not [t for t in texts if secret in t]                          # named by where it is, never shown
    # Regression (D2): both texts sent people to a Change delivery button the app did not have; D3 brings it.
    assert page.where.values["Server key"].text().endswith("under Change delivery… on this tab.")
    assert shown(page.change_button)
    assert shown(page.paused_text) and page.paused_text.text().endswith(
        "Enter the vendor's fingerprint under Change delivery… first.")


# ------------------------------------------------------------ preview, run, build only
def test_preview_streams_its_log_then_shows_the_rows_and_sends_nothing(qtbot, site, caplog):
    caplog.set_level(logging.INFO, logger="folio_exchange")
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.preview_button, Qt.MouseButton.LeftButton)
    assert not page.run_button.isEnabled() and not page.preview_button.isEnabled()     # one action at a time
    assert result(w)["state"] == "running" and page.result.head.text().startswith("Previewing…")
    assert "chq – Running: preview" in sidebar(w)
    settled(qtbot, w)
    qtbot.waitUntil(lambda: bool(result(w).get("rows")), timeout=15000)
    assert result(w)["status"] == "preview" and page.notice.text() == "Preview ready."
    assert page.result.log.toPlainText()                                    # the job's own log lines
    assert page.result.rows.rowCount() > 0 and page.result.rows.horizontalHeaderItem(0).text() == "Bib record ID"
    assert "rows" in page.result.rows_line.text()
    assert page.result.folder_button.isVisible()
    assert page.fields["last"].text() == "never"                           # previews are not runs
    assert not (site / "vendor-drop").exists()
    assert page.tabs.tabText(4) == "History (1)"
    assert "chq – On, no runs yet" in sidebar(w)


def test_run_and_build_only_ask_the_core_to_send_or_not(qtbot, site, monkeypatch):
    """Run sends (deliver=True), Build only never does (deliver=False): the flag the page passes."""
    asked = []
    monkeypatch.setattr(views, "run", lambda root, name, *, deliver: asked.append((name, deliver)) or {
        "status": "built", "code": 0, "run": None, "report": {}})
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.build_button, Qt.MouseButton.LeftButton)
    assert w.jobs.current.kind == "build"
    settled(qtbot, w)
    qtbot.mouseClick(page.run_button, Qt.MouseButton.LeftButton)
    assert w.jobs.current.kind == "run"
    settled(qtbot, w)
    assert asked == [("chq", False), ("chq", True)]


def test_build_only_builds_and_sends_nothing(qtbot, site):
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.build_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    assert result(w)["status"] == "built" and page.notice.text() == "Built. Nothing was sent."
    assert not (site / "vendor-drop").exists()
    assert shown(page.waiting) and "Built, not sent" in page.waiting_text.text()


def test_a_first_run_is_held_then_reviewed_and_sent(qtbot, site):
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.run_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    assert result(w)["status"] == "held" and page.notice.text() == "Held for your review."
    assert not (site / "vendor-drop").exists()                              # the first delivery is held
    assert shown(page.waiting) and "is waiting for you" in page.waiting_text.text()
    # Regression: the reason followed a full stop in lower case ("… is waiting for you. first delivery …").
    assert re.fullmatch(r"Held for review\. The run of .+ is waiting for you: first delivery to .+",
                        page.waiting_text.text()), page.waiting_text.text()
    assert "chq – On, held for review" in sidebar(w)

    summaries = []
    w.ask_release = lambda s: summaries.append(s) or False                  # Cancel
    qtbot.mouseClick(page.review_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(summaries) and page.notice.text() == "Nothing was sent.", timeout=15000)
    assert summaries[0]["reason"].startswith("first delivery") and summaries[0]["items"] == 5
    assert not (site / "vendor-drop").exists() and w.jobs.running() is None

    w.ask_release = lambda s: summaries.append(s) or True                   # Send files
    qtbot.mouseClick(page.review_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(summaries) == 2, timeout=15000)
    settled(qtbot, w)
    assert result(w)["status"] == "success" and page.notice.text() == "Delivered."
    sent = sorted(p.name for p in (site / "vendor-drop").iterdir())
    assert sent == sorted(f["name"] for f in result(w)["run"]["files"])
    qtbot.waitUntil(lambda: not shown(page.waiting), timeout=15000)
    assert "chq – On, delivered" in sidebar(w)


def test_a_release_that_cannot_be_looked_at_says_why(qtbot, site):
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    w.review("chq")                                                        # nothing waits
    qtbot.waitUntil(lambda: "no held, blocked or built run" in page.notice.text(), timeout=15000)
    assert page.notice.textFormat() == Qt.TextFormat.PlainText and not w.reviewing


def test_the_release_question_shows_what_would_be_sent_and_cancel_is_the_default(qtbot):
    summary = {"feed": "chq", "run_dir": "/x/run", "status": "held", "built": "2026-10-27T10:00:00+00:00",
               "recipe": "chq-academic@1.0.0", "bibs": 4, "items": 5, "last_delivered_items": 900,
               "last_delivered": "2026-10-20T10:00:00+00:00", "reason": "first delivery to <b>here</b>",
               "files": [["test_items_20261027.txt", 2048], ["test_bibs_20261027.mrc", 100]],
               "destination": "folder D:\\drop", "newer_runs": ["/x/newer"]}
    dialog = ReleaseDialog(summary)
    qtbot.addWidget(dialog)
    facts = {k: v.text() for k, v in dialog.facts.values.items()}
    assert facts["Waiting because"] == "first delivery to <b>here</b>"     # shown as written
    assert dialog.facts.values["Waiting because"].textFormat() == Qt.TextFormat.PlainText
    assert facts["Items"].startswith("5 (last delivery 900,") and facts["Bibs"] == "4"
    assert facts["Sends to"] == "folder D:\\drop"
    assert "test_items_20261027.txt  2.0 KB" in dialog.files.text()
    assert shown(dialog.newer)
    assert dialog.cancel_button.isDefault() and not dialog.send_button.isDefault()
    dialog.open()
    qtbot.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
    assert dialog.result() == QDialog.DialogCode.Rejected
    dialog.open()
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert dialog.result() == QDialog.DialogCode.Rejected
    dialog.open()
    qtbot.mouseClick(dialog.send_button, Qt.MouseButton.LeftButton)
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_a_release_is_not_started_when_another_action_began_meanwhile(qtbot, site, blocking_job):
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.run_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)

    def ask(summary):
        blocking_job(w.jobs, "check", None)                                # started on the page meanwhile
        return True

    w.ask_release = ask
    w.review("chq")
    qtbot.waitUntil(lambda: page.notice.text() == "Another action is running; nothing was sent.", timeout=15000)
    assert not (site / "vendor-drop").exists()


# ------------------------------------------------------------ one action at a time
def test_feed_actions_are_refused_while_another_runs(qtbot, site, blocking_job):
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    job, go = blocking_job(w.jobs, "check", None)
    w.sync_busy()
    for b in (page.preview_button, page.run_button, page.build_button, page.pause_button, page.test_button):
        assert not b.isEnabled(), b.text()
    assert "Running: connection check" in w.banner.text()
    w.preview("chq")                                                       # refused at once, as Jobs refuses
    assert "Another action is still running (check)" in page.notice.text()
    w.run_feed("chq", True)
    assert w.jobs.current is job and "chq" not in page.results
    w.review("chq")
    assert page.notice.text() == "Another action is running; wait for it to finish." and not w.reviewing
    w.set_enabled("chq", False)
    assert not w.toggling and not w.errands.pending
    # Regression: this only checked that Jobs refused a preview, which it does with or without the window's guard.
    assert not w.preview_action.isEnabled()
    page.say(None)
    qtbot.keyClick(w, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert w.jobs.current is job and page.notice.text() == ""              # no preview was even tried
    go.set()
    qtbot.waitUntil(lambda: page.preview_button.isEnabled(), timeout=5000)
    assert load_site(site) and views.feed_view(site, "chq")["enabled"]      # nothing was paused


def test_the_sidebar_says_which_feed_runs(qtbot, site, blocking_job):
    w = window(qtbot, site)
    job, go = blocking_job(w.jobs, "release", "chq")
    w.sync_busy()
    assert "chq – Running: send" in sidebar(w) and "Running: send of chq" in w.banner.text()
    go.set()
    qtbot.waitUntil(lambda: "chq – On, no runs yet" in sidebar(w), timeout=5000)


def test_ctrl_enter_previews_the_open_feed(qtbot, site, monkeypatch):
    previews = []
    monkeypatch.setattr(views, "preview", lambda root, name, n: previews.append((name, n)) or {
        "status": "preview", "code": 0, "run": None, "report": {}})
    w = window(qtbot, site)
    qtbot.keyClick(w, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)   # on Home: nothing
    assert w.jobs.current is None
    on_feed(qtbot, w)
    qtbot.keyClick(w, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    settled(qtbot, w)
    assert previews == [("chq", window_module.PREVIEW_SAMPLE)]


# ------------------------------------------------------------ pause and resume
def test_pause_and_resume(qtbot, site):
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    assert page.pause_button.text() == "Pa&use"
    qtbot.mouseClick(page.pause_button, Qt.MouseButton.LeftButton)
    assert not page.pause_button.isEnabled()                               # at once: a second press does nothing
    w.set_enabled("chq", False)
    qtbot.waitUntil(lambda: page.notice.text() == "chq paused.", timeout=15000)
    qtbot.waitUntil(lambda: "chq – Paused, no runs yet" in sidebar(w), timeout=15000)
    assert page.pause_button.text() == "Res&ume" and page.pause_button.accessibleName() == "Resume this feed"
    assert page.fields["state"].text() == "Paused: scheduled runs skip it"
    assert shown(page.paused_title) and "Preview and Run it by hand" in page.paused_text.text()
    assert "This feed is paused: scheduled runs skip it" in page.notices.text()
    with History(load_site(site).history_path) as hist:
        paused = [a for a in hist.audit_log(10) if a["action"] == "feed paused"]
    assert len(paused) == 1 and paused[0]["detail"]["from"] == "app"
    settled(qtbot, w)
    qtbot.mouseClick(page.pause_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: page.notice.text().startswith("chq resumed."), timeout=15000)
    qtbot.waitUntil(lambda: "chq – On, no runs yet" in sidebar(w), timeout=15000)
    assert page.pause_button.text() == "Pa&use"


def test_a_pause_that_fails_says_why_in_plain_text(qtbot, site, monkeypatch):
    def refuse(root, name, enabled, *, via):
        raise FxError("The <i>feed</i> file cannot be written")

    monkeypatch.setattr(views, "set_enabled", refuse)
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.pause_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: page.notice.text() == "The <i>feed</i> file cannot be written", timeout=15000)
    assert page.notice.textFormat() == Qt.TextFormat.PlainText
    settled(qtbot, w)
    assert page.pause_button.isEnabled() and page.pause_button.text() == "Pa&use"


# ------------------------------------------------------------ errors, delivery tests and folders
def test_an_action_that_fails_is_shown_as_written(qtbot, site, monkeypatch):
    def fail(root, name, n):
        raise FxError('FOLIO refused <img src="file://example.invalid/x.png">. Run `folio-exchange preview chq` again.')

    monkeypatch.setattr(views, "preview", fail)
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.preview_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    assert result(w)["state"] == "error"
    head = page.result.head.text()
    assert head == 'Did not run: FOLIO refused <img src="file://example.invalid/x.png">.'   # no command the app has
    assert page.result.head.textFormat() == Qt.TextFormat.PlainText
    assert page.notice.text() == head and page.notice.textFormat() == Qt.TextFormat.PlainText


def test_test_delivery_reports_in_the_delivery_tab(qtbot, site):
    (site / "vendor-drop").mkdir()
    (site / "vendor-drop" / "old.txt").write_text("x", encoding="utf-8")
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.test_button, Qt.MouseButton.LeftButton)
    assert w.jobs.current.kind == "test-delivery" and "sends nothing" in page.test_result.text()
    settled(qtbot, w)
    assert page.test_result.text().startswith("It works. Reached") and "holds 1 entry" in page.test_result.text()
    assert page.notice.text() == "The delivery test worked."
    assert "chq" not in page.results                                       # not a run: no result panel


def test_a_failed_delivery_test_says_why(qtbot, site):
    w = window(qtbot, site)                                                # vendor-drop does not exist
    page = on_feed(qtbot, w)
    w.check_delivery("chq")
    settled(qtbot, w)
    assert page.test_result.text().startswith("The test failed.") and "does not exist" in page.test_result.text()
    assert page.notice.text() == "The delivery test failed."


def test_open_a_runs_folder(qtbot, site, monkeypatch):
    opened = []
    monkeypatch.setattr(views, "open_folder", opened.append)
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.preview_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    qtbot.mouseClick(page.result.folder_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: page.notice.text() == "The run's folder is open in a new window.", timeout=15000)
    assert len(opened) == 1 and opened[0].name == result(w)["run"]["folder"]
    w.open_folder("feed", 999)
    qtbot.waitUntil(lambda: page.notice.text() == "No such run", timeout=15000)
    assert len(opened) == 1


# ------------------------------------------------------------ history and activity
def test_a_run_opens_from_the_history_with_its_rows(qtbot, site):
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.build_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    page.results.clear()
    page.result.show_result(None)
    assert page.history.rowCount() == 1 and page.history.item(0, 0).text() == "Built, not sent"
    page.history.setCurrentCell(0, 0)
    page.history.cellActivated.emit(0, 0)                                 # Enter or a double click
    qtbot.waitUntil(lambda: bool(result(w).get("rows")), timeout=15000)
    assert page.notice.text().startswith("Showing the run of")
    assert page.result.rows.rowCount() == 5
    summary = {k: v.text() for k, v in page.result.summary.values.items()}
    assert summary["Status"] == "Built, not sent" and summary["Next step"].startswith("Look at the files")
    assert "test_items_" in page.result.files.text()


def test_a_refresh_that_changes_nothing_keeps_the_history_selection_and_the_result(qtbot, site):
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.preview_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    qtbot.waitUntil(lambda: bool(result(w).get("rows")), timeout=15000)
    page.history.setCurrentCell(0, 2)
    first = page.history.item(0, 0)
    page.result.rows.setCurrentCell(3, 1)
    w.refresh()
    qtbot.waitUntil(lambda: not w.reader.busy, timeout=15000)
    assert page.history.item(0, 0) is first and page.history.currentRow() == 0
    assert page.result.rows.currentRow() == 3


def test_a_run_does_not_open_while_this_feed_runs(qtbot, site, blocking_job):
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    page.job_started("chq", "run")
    w.open_run("chq", 1)
    assert page.notice.text() == "Wait for the running action to finish, then open the run again."
    assert not w.errands.pending


def test_activity_lists_runs_and_changes_newest_first(qtbot, site):
    views.set_enabled(site, "chq", False, via="app")
    views.set_enabled(site, "chq", True, via="app")
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.preview_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    w.select("activity")
    act = w.stack.currentWidget()
    assert isinstance(act, ActivityPage) and act is w.activity
    assert act.runs.rowCount() == 1 and act.runs.item(0, 0).text() == "Preview" and act.runs.item(0, 1).text() == "chq"
    actions = [act.changes.item(r, 3).text() for r in range(act.changes.rowCount())]
    assert actions.index("feed resumed") < actions.index("feed paused")    # newest first
    assert "From: app" in act.changes.item(actions.index("feed resumed"), 4).text()
    assert act.tabs.tabText(1) == f"Changes ({act.changes.rowCount()})"
    act.runs.cellActivated.emit(0, 0)
    qtbot.waitUntil(lambda: act.result.rows.rowCount() > 0, timeout=15000)
    assert act.notice.text().startswith("Showing the run of chq")


def test_activity_recipes_and_options_are_in_the_app(qtbot, site):
    """D3: Recipes and Options were the browser page's until now."""
    w = window(qtbot, site)
    for key, page in (("activity", w.activity), ("recipes", w.recipes), ("options", w.options),
                      ("add", w.wizard)):
        w.select(key)
        assert w.stack.currentWidget() is page


# ------------------------------------------------------------ Home
def test_home_lists_the_feeds_that_need_you(qtbot, site):
    w = window(qtbot, site)
    assert not shown(w.home.attention) and shown(w.home.calm)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.run_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    w.select("home")
    assert w.home.attention.count() == 1
    line = w.home.attention.item(0).text()
    assert line.startswith("chq: Held for review. The run of") and "is waiting for you" in line
    w.home.attention.itemActivated.emit(w.home.attention.item(0))
    assert w.stack.currentWidget() is w.feed and w.feed.name == "chq"


# ------------------------------------------------------------ keyboard and screen readers
def test_the_feed_page_and_activity_are_named_for_screen_readers(qtbot, site):
    from PySide6.QtWidgets import QComboBox, QPlainTextEdit, QTableWidget, QTabWidget

    w = window(qtbot, site)
    for key in ("feed:chq", "activity", "home"):
        w.select(key)
        for kind in (QAbstractButton, QTableWidget, QTabWidget, QPlainTextEdit, QComboBox):
            for control in w.stack.currentWidget().findChildren(kind):
                if not control.objectName().startswith("qt_"):
                    assert control.accessibleName(), (key, control)
        for label in w.stack.currentWidget().findChildren(QLabel):
            assert label.textFormat() in (Qt.TextFormat.PlainText, Qt.TextFormat.RichText), label
    assert {"Ctrl+Return", "F5"} <= {s.toString() for a in w.actions() for s in a.shortcuts()}


# ------------------------------------------------------------ review findings (regressions)
def gated(monkeypatch, target, attr, block=lambda *a, **k: True):
    """Replace ``target.attr`` with one that waits for the returned Event when ``block(*args)`` says so."""
    real = getattr(target, attr)
    entered, gate = threading.Event(), threading.Event()

    def slow(*args, **kw):
        if block(*args, **kw):
            entered.set()
            gate.wait(20)
        return real(*args, **kw)

    monkeypatch.setattr(target, attr, slow)
    return entered, gate


def rows_of(output: str, outputs=("a", "b")) -> dict:
    return {"file": f"{output}.txt", "output": output, "outputs": list(outputs), "header": ["Barcode"],
            "rows": [[f"{output}-1"]], "total": 1}


def a_run(run_id: int) -> dict:
    return {"id": run_id, "feed": "chq", "started": None, "status": "built", "report": {}, "folder": "x"}


def test_a_run_opened_before_an_action_started_does_not_replace_its_result(qtbot, site, monkeypatch, caplog):
    """Regression (parity-1, qt-1, safety-2): a History run read slowly answered after Run started, replaced the
    running result, and the finished run's log was lost."""
    caplog.set_level(logging.INFO, logger="folio_exchange")
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.build_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    entered, gate = gated(monkeypatch, views, "run_detail")
    w.open_run("chq", 1)
    qtbot.waitUntil(entered.is_set, timeout=5000)
    go = threading.Event()

    def work():
        logging.getLogger("folio_exchange.test").info("Reading items")
        go.wait(20)
        return {"status": "built", "code": 0, "run": None, "report": {}}

    w._feed_job("run", "chq", work)
    assert result(w)["state"] == "running"
    gate.set()
    qtbot.waitUntil(lambda: not w.errands.pending, timeout=10000)
    assert result(w)["state"] == "running"
    assert page.notice.text() == "Wait for the running action to finish, then open the run again."
    go.set()
    settled(qtbot, w)
    assert result(w)["state"] == "done" and any("Reading items" in x for x in result(w)["lines"])


def test_only_the_newest_run_asked_for_is_shown(qtbot, site, monkeypatch):
    """Regression (qt-2): answers came in the order they finished, so an older, slower read replaced a newer one."""
    monkeypatch.setattr(views, "run_detail", lambda root, run_id: a_run(run_id))
    monkeypatch.setattr(views, "run_rows", lambda root, run_id, output=None: rows_of(output or "a"))
    entered, gate = gated(monkeypatch, views, "run_detail", lambda root, run_id: run_id == 5)
    w = window(qtbot, site)
    w.select("activity")
    w.open_run(None, 5)
    qtbot.waitUntil(entered.is_set, timeout=5000)
    w.open_run(None, 6)
    qtbot.waitUntil(lambda: w.errands.pending == 1, timeout=10000)
    assert w.activity.shown["run"]["id"] == 6
    gate.set()
    qtbot.waitUntil(lambda: not w.errands.pending, timeout=10000)
    assert w.activity.shown["run"]["id"] == 6                               # run 5's late answer is dropped

    page = on_feed(qtbot, w)                                                # the same in a feed's History
    entered.clear()
    gate.clear()
    w.open_run("chq", 5)
    qtbot.waitUntil(entered.is_set, timeout=5000)
    w.open_run("chq", 6)
    qtbot.waitUntil(lambda: w.errands.pending == 1, timeout=10000)
    gate.set()
    qtbot.waitUntil(lambda: not w.errands.pending, timeout=10000)
    assert result(w)["run"]["id"] == 6 and page.notice.text().startswith("Showing the run of")


def test_choosing_another_file_of_a_run_shows_its_rows_newest_choice_last(qtbot, site, monkeypatch):
    """Regression (a11y_tests-7, qt-2): the file chooser was never tested, and its answers were not ordered."""
    monkeypatch.setattr(views, "run_rows", lambda root, run_id, output=None: rows_of(output or "a"))
    entered, gate = gated(monkeypatch, views, "run_rows", lambda root, run_id, output=None: output == "a")
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    page.show_run("chq", a_run(7), rows_of("b"))
    combo = page.result.output
    assert shown(combo) and combo.accessibleName() == "Which file" and combo.currentText() == "b"
    assert page.result.rows_line.text().startswith("b.txt")
    with qtbot.waitSignal(page.result.rows_wanted, timeout=5000) as asked:
        combo.setCurrentText("a")
    assert asked.args == [7, "a"]
    qtbot.waitUntil(entered.is_set, timeout=5000)
    combo.setCurrentText("b")                                               # a second choice before the first answers
    qtbot.waitUntil(lambda: w.errands.pending == 1, timeout=10000)
    assert page.result.rows_line.text().startswith("b.txt")
    gate.set()
    qtbot.waitUntil(lambda: not w.errands.pending, timeout=10000)
    assert page.result.rows_line.text().startswith("b.txt") and page.result.rows.item(0, 0).text() == "b-1"

    monkeypatch.setattr(views, "run_detail", lambda root, run_id: a_run(run_id))
    w.select("activity")                                                    # Activity's chooser
    w.open_run(None, 8)
    qtbot.waitUntil(lambda: w.activity.shown is not None and bool(w.activity.shown.get("rows")), timeout=10000)
    assert shown(w.activity.result.output)
    w.activity.result.output.setCurrentText("b")
    qtbot.waitUntil(lambda: w.activity.result.rows_line.text().startswith("b.txt"), timeout=10000)


def test_closing_while_a_pause_is_written_waits_for_it(qtbot, site, monkeypatch):
    """Regression (qt-3, safety-4): closing quit at once and cut a pause or resume off in its daemon thread."""
    entered, gate = gated(monkeypatch, views, "set_enabled")
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.pause_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(entered.is_set, timeout=5000)
    w.close()
    assert w.isVisible() and not w.quitting
    assert "closes once the pause or resume is written" in w.banner.text()
    gate.set()
    qtbot.waitUntil(lambda: w.quitting and not w.isVisible(), timeout=15000)
    assert not views.feed_view(site, "chq")["enabled"]                     # the pause was written in full
    with History(load_site(site).history_path) as hist:
        assert [a for a in hist.audit_log(10) if a["action"] == "feed paused"]


def test_an_action_that_ended_before_its_watcher_looked_still_shows_its_result(qtbot, site, monkeypatch):
    """Regression (qt-4): a new action replaced the watcher of one that had ended but not been seen, and that
    feed's result stayed "running" for good."""
    monkeypatch.setattr(views, "check_connection", lambda root: {"endpoints": [], "ok": True})
    w = window(qtbot, site)
    on_feed(qtbot, w)
    w._feed_job("build", "chq", lambda: {"status": "built", "code": 0, "run": None, "report": {}})
    w.watcher.timer.stop()                                                  # its last look has not come yet
    qtbot.waitUntil(lambda: w.jobs.running() is None, timeout=10000)
    w.check_connection()
    qtbot.waitUntil(lambda: w.jobs.running() is None and w.watcher is None, timeout=10000)
    assert result(w)["state"] == "done" and result(w)["status"] == "built"
    assert not w.feed.running_here()


def test_the_release_question_names_the_feed(qtbot):
    """Regression (qt-5, safety-1): nothing in the question said which feed's files it would send."""
    summary = {"feed": "chq", "run_dir": "/x/run", "status": "held", "built": None, "recipe": "r@1",
               "items": 5, "bibs": 4, "files": [], "destination": "folder D:\\drop"}
    dialog = ReleaseDialog(summary)
    qtbot.addWidget(dialog)
    assert "chq" in dialog.windowTitle()
    assert list(dialog.facts.values)[0] == "Feed" and dialog.facts.values["Feed"].text() == "chq"


def test_enter_acts_on_the_focused_button_of_the_release_question(qtbot):
    """Regression (parity-2): Enter on a focused "Send files" cancelled, unlike the page."""
    summary = {"feed": "chq", "run_dir": "/x/run", "status": "held", "files": [], "destination": "x"}
    dialog = ReleaseDialog(summary)
    qtbot.addWidget(dialog)
    assert dialog.cancel_button.isDefault()                                 # where Enter starts: send nothing
    dialog.open()
    qtbot.keyClick(dialog.send_button, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.open()
    qtbot.keyClick(dialog.cancel_button, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_a_release_question_for_a_feed_no_longer_shown_is_not_asked(qtbot, site, monkeypatch):
    """Regression (qt-5, safety-1): the question opened over another feed's page when the look was slow."""
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.run_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    asked = []
    w.ask_release = lambda s: asked.append(s) or True
    entered, gate = gated(monkeypatch, views, "release_summary")
    w.review("chq")
    qtbot.waitUntil(entered.is_set, timeout=5000)
    w.select("home")
    gate.set()
    qtbot.waitUntil(lambda: not w.reviewing, timeout=10000)
    assert not asked and w.jobs.running() is None and not (site / "vendor-drop").exists()
    assert "Nothing was sent" in w.banner.text() and "chq" in w.banner.text()


def test_actions_wait_while_a_release_question_is_on_the_way(qtbot, site, monkeypatch, blocking_job):
    """Regression (qt-6a): Preview, Run and Ctrl+Enter stayed on while the question was prepared, and the question
    was asked although an action had started meanwhile."""
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.run_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    asked = []
    w.ask_release = lambda s: asked.append(s) or True
    entered, gate = gated(monkeypatch, views, "release_summary")
    w.review("chq")
    qtbot.waitUntil(entered.is_set, timeout=5000)
    for b in (page.preview_button, page.run_button, page.build_button, page.test_button):
        assert not b.isEnabled(), b.text()
    assert not w.preview_action.isEnabled()
    qtbot.keyClick(w, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert w.jobs.running() is None
    blocking_job(w.jobs, "check", None)                                     # started on the page meanwhile
    gate.set()
    qtbot.waitUntil(lambda: not w.reviewing, timeout=10000)
    assert not asked and page.notice.text() == "Another action is running; nothing was sent."


def test_a_release_is_not_sent_when_the_site_folder_was_renamed_during_the_question(qtbot, site, tmp_path):
    """Regression (qt-6b): the release ran against the old folder after a rename on the page."""
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.run_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    renamed = tmp_path / "renamed-site"

    def ask(summary):
        w.page.start_root = renamed                                         # the page renamed the folder meanwhile
        return True

    w.ask_release = ask
    w.review("chq")
    qtbot.waitUntil(lambda: not w.reviewing and "renamed" in page.notice.text(), timeout=15000)
    assert w.jobs.current.kind != "release" and w.site_root == renamed
    assert not (site / "vendor-drop").exists()


def test_pause_shows_its_new_state_at_once(qtbot, site, monkeypatch):
    """Regression (qt-7, safety-6): the button kept its old caption until the next reading, so a second press
    paused again."""
    calls = []
    monkeypatch.setattr(views, "set_enabled", lambda root, name, enabled, *, via: calls.append((name, enabled))
                        or {"name": name, "enabled": enabled})
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    w.refresh = lambda: None                                                # the next reading never lands
    qtbot.mouseClick(page.pause_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: page.notice.text() == "chq paused.", timeout=10000)
    assert page.pause_button.isEnabled() and page.pause_button.text() == "Res&ume"
    assert page.pause_button.accessibleName() == "Resume this feed"
    assert page.fields["state"].text() == "Paused: scheduled runs skip it"
    qtbot.mouseClick(page.pause_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: page.notice.text().startswith("chq resumed."), timeout=10000)
    assert calls == [("chq", False), ("chq", True)]


def test_each_release_question_is_deleted_after_it_is_answered(qtbot, site, monkeypatch):
    """Regression (qt-8): every question left a hidden dialog in the window until it closed."""
    monkeypatch.setattr(ReleaseDialog, "exec", lambda self: 0)
    w = window(qtbot, site)
    w.ask_release = MainWindow.ask_release.__get__(w)
    summary = {"feed": "chq", "run_dir": "/x/run", "status": "held", "files": [], "destination": "x"}
    assert w.ask_release(summary) is False and w.ask_release(summary) is False
    qtbot.waitUntil(lambda: not w.findChildren(ReleaseDialog), timeout=5000)


def test_a_release_is_refused_when_a_newer_run_failed_during_the_question(qtbot, site):
    """Regression (safety-3): the run named in the question was sent without the core's check for a newer failed
    run, which the release it showed would have refused."""
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.run_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)

    def ask(summary):
        with History(load_site(site).history_path) as hist:                  # a scheduled run fails meanwhile
            rid = hist.start_run("chq", "chq-academic", "1.0.0", "schedule", None)
            hist.finish_run(rid, "failed", error="FOLIO did not answer.")
        return True

    w.ask_release = ask
    w.review("chq")
    qtbot.waitUntil(lambda: w.jobs.current is not None and w.jobs.current.kind == "release", timeout=15000)
    settled(qtbot, w)
    assert result(w)["state"] == "error" and "failed" in result(w)["error"]
    assert not (site / "vendor-drop").exists()


def test_errand_failures_are_logged(qtbot, site, monkeypatch, caplog):
    """Regression (safety-5): a failed pause, folder or run read was shown but left nothing in the log."""
    caplog.set_level(logging.INFO, logger="folio_exchange")

    def refuse(root, name, enabled, *, via):
        raise FxError("The feed file cannot be written")

    def broken(root, run_id):
        raise RuntimeError("the share went away")

    monkeypatch.setattr(views, "set_enabled", refuse)
    monkeypatch.setattr(views, "open_run_folder", broken)
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    w.set_enabled("chq", False)
    qtbot.waitUntil(lambda: not w.errands.pending, timeout=10000)
    w.open_folder("feed", 1)
    qtbot.waitUntil(lambda: not w.errands.pending, timeout=10000)
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("The feed file cannot be written" in r.getMessage() for r in warned)
    assert any("the share went away" in r.getMessage() for r in warned)
    assert any(r.exc_info and r.exc_info[0] is RuntimeError for r in warned)    # the unexpected one, in full
    assert page.notice.text() == "RuntimeError: the share went away"


def test_the_layout_tables_follow_the_section_tabs_in_the_tab_order(qtbot, site):
    """Regression (a11y_tests-1): rebuilt tables went to the end of the window's Tab order, after the result."""
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.preview_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    page.tabs.setCurrentIndex(1)
    columns = [t for t in page.tabs.widget(1).findChildren(QTableWidget) if t.accessibleName() == "Columns of items"]
    order, x = [], page.tabs.tabBar().nextInFocusChain()
    while x is not page.tabs.tabBar() and len(order) < 500:
        if x.isVisible() and x.isEnabled() and x.focusPolicy() & Qt.FocusPolicy.TabFocus:
            order.append(x)
        x = x.nextInFocusChain()
    assert columns[0] in order and page.result.folder_button in order
    assert order.index(columns[0]) < order.index(page.result.folder_button)


def test_starting_an_action_from_the_keyboard_keeps_the_focus_on_this_page(qtbot, site, monkeypatch):
    """Regression (a11y_tests-2): the pressed button was disabled and the focus jumped to "Open this feed in the
    browser page", which the next Enter would press."""
    go = threading.Event()
    monkeypatch.setattr(views, "run", lambda root, name, *, deliver: go.wait(20) and {
        "status": "built", "code": 0, "run": None, "report": {}})
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    w.activateWindow()
    qtbot.waitUntil(w.isActiveWindow, timeout=5000)
    page.run_button.setFocus()
    qtbot.waitUntil(page.run_button.hasFocus, timeout=5000)
    qtbot.keyClick(page.run_button, Qt.Key.Key_Space)
    assert w.jobs.running() is not None
    assert QApplication.focusWidget() is page.result.head
    go.set()
    settled(qtbot, w)


def test_a_refresh_that_changes_nothing_keeps_the_place_in_needs_you(qtbot):
    """Regression (a11y_tests-3): every refresh emptied the list, so the arrow keys' place was lost."""
    home = HomePage()
    qtbot.addWidget(home)
    need = [("a", "a: Held for review."), ("b", "b: the last run failed (never).")]
    home.show_site({"name": "x", "feeds": []}, need)
    home.attention.setCurrentRow(1)
    home.show_site({"name": "x", "feeds": []}, list(need))
    assert home.attention.currentRow() == 1


def test_the_rows_table_is_named_for_any_run(qtbot):
    """Regression (a11y_tests-5): screen readers heard "Rows sent" for a preview, which sends nothing."""
    view = RunView("Nothing yet.")
    qtbot.addWidget(view)
    assert view.rows.accessibleName() == "Rows of the run's file"


def test_one_release_question_at_a_time_and_none_after_the_window_closed(qtbot, site, monkeypatch):
    """Regression (a11y_tests-8): the two guards had no test."""
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.run_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    looks = []
    real = views.release_summary
    monkeypatch.setattr(views, "release_summary", lambda root, name: looks.append(name) or real(root, name))
    asked = []

    def ask(summary):
        asked.append(summary)
        w.shut_down()                                                       # the window closed meanwhile
        return True

    w.ask_release = ask
    w.review("chq")
    w.review("chq")                                                         # a second press while the look runs
    qtbot.waitUntil(lambda: not w.reviewing and not w.errands.pending, timeout=15000)
    assert looks == ["chq"] and len(asked) == 1
    assert w.jobs.current.kind != "release" and not (site / "vendor-drop").exists()


def test_no_release_question_opens_after_the_window_closed_during_the_look(qtbot, site, monkeypatch):
    """Regression (final D2 check): closing while the look ran (nothing to wait for) still opened "Send these
    files?" when the look answered, over a window that had gone."""
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.run_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    asked = []
    w.ask_release = lambda s: asked.append(s) or True
    entered, gate = gated(monkeypatch, views, "release_summary")
    w.review("chq")
    qtbot.waitUntil(entered.is_set, timeout=5000)
    w.close()
    assert w.quitting and not w.isVisible()
    gate.set()
    qtbot.waitUntil(lambda: not w.reviewing and not w.errands.pending, timeout=10000)
    assert not asked and w.jobs.current.kind != "release" and not (site / "vendor-drop").exists()


def test_activitys_folder_a_missing_run_and_the_waiting_runs_folder(qtbot, site, monkeypatch):
    """Regression (a11y_tests-9): Activity's Open folder, a run that does not open there, and the waiting panel's
    "Open its folder" had no test."""
    opened = []
    monkeypatch.setattr(views, "open_folder", opened.append)
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    qtbot.mouseClick(page.build_button, Qt.MouseButton.LeftButton)
    settled(qtbot, w)
    assert shown(page.waiting) and shown(page.waiting_folder)
    qtbot.mouseClick(page.waiting_folder, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: page.notice.text() == "The run's folder is open in a new window.", timeout=15000)
    assert opened[-1].name == views.run_detail(site, page.waiting_id)["folder"]
    w.select("activity")
    act = w.activity
    w.open_run(None, 999)
    qtbot.waitUntil(lambda: act.notice.text() == "No such run", timeout=15000)
    act.runs.cellActivated.emit(0, 0)
    qtbot.waitUntil(lambda: act.result.folder_button.isVisible(), timeout=15000)
    page.say(None)
    qtbot.mouseClick(act.result.folder_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: act.notice.text() == "The run's folder is open in a new window.", timeout=15000)
    assert len(opened) == 2 and page.notice.text() == ""


def test_an_action_started_on_the_page_is_followed_in_the_app(qtbot, site, caplog):
    """Regression (parity-5): a run started on the browser page showed in the banner, but never in the feed's
    result: no progress, no log and no outcome."""
    caplog.set_level(logging.INFO, logger="folio_exchange")
    w = window(qtbot, site)
    page = on_feed(qtbot, w)
    go = threading.Event()

    def work():
        logging.getLogger("folio_exchange.test").info("Reading items")
        go.wait(20)
        return {"status": "held", "code": 2, "run": None, "report": {}}

    w.jobs.start("run", "chq", work)                                        # as the page's Run does
    qtbot.waitUntil(lambda: result(w).get("state") == "running", timeout=5000)
    qtbot.waitUntil(lambda: "Reading items" in page.result.log.toPlainText(), timeout=5000)
    assert page.result.head.text().startswith("Running…")
    go.set()
    qtbot.waitUntil(lambda: result(w).get("state") == "done", timeout=10000)
    assert page.notice.text() == "Held for your review."
    assert any("Reading items" in x for x in result(w)["lines"])
    settled(qtbot, w)
