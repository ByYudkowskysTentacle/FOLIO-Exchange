"""The desktop app's setup, recipes and options (D3), run offscreen with pytest-qt against the mock FOLIO: a new
site set up in the app alone, the new-feed wizard, a feed's settings and delivery (with the SFTP server key
check), its password and key file, recipes from a (fake) vendor source, renaming the site folder, registering the
scheduled task, and alerts. Secrets are looked for everywhere they must not be: the log, the jobs' lines, every
widget, and the site's files."""
from __future__ import annotations

import json
import logging
import sys
import threading
import types
from pathlib import Path

import pytest
from conftest import QT_LOADS

if not QT_LOADS:
    pytest.skip("needs the desktop extra (PySide6) and pytest-qt, and a Qt that loads", allow_module_level=True)

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QAccessible, QTextDocument  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QApplication,
    QComboBox,
    QDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
)
from sitekit import make_feed  # noqa: E402
from test_setup import KEYS, SFTP_PASSWORD, _ed25519_openssh, keyring  # noqa: E402, F401
from test_sources import SHA, FakeGitHub, newer  # noqa: E402

from folio_exchange import client as client_mod  # noqa: E402
from folio_exchange import delivery, runner, setupops, siteops, sources, views  # noqa: E402
from folio_exchange.config import load_site, read_yaml  # noqa: E402
from folio_exchange.desktop import forms, places  # noqa: E402
from folio_exchange.desktop import options as options_module  # noqa: E402
from folio_exchange.desktop.alerts import AlertsDialog  # noqa: E402
from folio_exchange.desktop.delivery import DeliveryDialog, LoginBox  # noqa: E402
from folio_exchange.desktop.settings import SettingsDialog  # noqa: E402
from folio_exchange.desktop.window import MainWindow  # noqa: E402
from folio_exchange.errors import FxError  # noqa: E402
from folio_exchange.history import History  # noqa: E402
from folio_exchange.jobs import Jobs  # noqa: E402

LONG = 60000
FOLIO_SECRET = "Fq-91distinctive-folio-pass"       # gitleaks:allow - made up; the mock FOLIO is told to take it
PASSPHRASE = "Kp-44distinctive-key-phrase"         # gitleaks:allow - made up
MAIL_SECRET = "Ml-63distinctive-mail-pass"         # gitleaks:allow - made up
HOOK = "https://hooks.example.org/services/zz-77distinctive-hook"     # gitleaks:allow - made up


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


@pytest.fixture
def heard(monkeypatch) -> list[str]:
    """What screen readers are told as it happens: the text of each label an alert event is raised on
    (forms.announce, which every view uses)."""
    texts: list[str] = []

    class Accessible:
        Event = QAccessible.Event

        @staticmethod
        def updateAccessibility(event):  # noqa: N802 - Qt's naming
            texts.append(event.object().text())

    monkeypatch.setattr(forms, "QAccessible", Accessible)
    return texts


@pytest.fixture
def server_keys(monkeypatch):
    """The vendor's SFTP server, as far as reading its key goes (nothing logs in)."""
    read = []
    monkeypatch.setattr(delivery, "host_key_fingerprints", lambda h, p, timeout=30: read.append((h, p)) or KEYS)
    return read


def window(qtbot, root, **kw):
    """A window that opens nothing in a real browser, starts no console and never leaves a question open."""
    opened: list[str] = []
    w = MainWindow(root, open_url=opened.append, **kw)
    w.opened = opened
    w.ask_close_while_running = lambda job: "keep"
    w.ask_release = lambda summary: pytest.fail("a release question was asked that the test did not expect")
    w.start_console = lambda exe, args, folder: pytest.fail("a console was started that the test did not expect")
    qtbot.addWidget(w, before_close_func=lambda w: drain(qtbot, w))
    w.show()
    qtbot.waitUntil(lambda: w.snap is not None and not w.reader.busy, timeout=15000)
    return w


def drain(qtbot, w, timeout: int = 30000) -> None:
    """Before a test's window closes: let its errands and jobs end, so no thread of this test reaches into the next
    (with the mock FOLIO stopped and sleep patched, a look at FOLIO still under way fails at once). One that does
    not end fails the test that left it, rather than going on quietly into the next."""
    qtbot.waitUntil(lambda: not w.errands.pending and w.jobs.running() is None, timeout=timeout)


def settled(qtbot, w, timeout=LONG):
    qtbot.waitUntil(lambda: w.jobs.running() is None and w.watcher is None and not w.reader.busy
                    and not w.errands.pending, timeout=timeout)


def click(qtbot, b) -> None:
    """Click ``b`` once it is shown and enabled: the window enables its actions on a half-second look after one
    ends, and Qt shows a widget added to a page on screen with the next turn of the event loop."""
    qtbot.waitUntil(lambda: b.isVisibleTo(b.window()) and b.isEnabled(), timeout=5000)
    # A button just shown (a box made visible a moment ago) is laid out on the next turn of the event loop: until
    # then a click can miss it. Regression (CI): the key file's "Choose" click was lost that way, now and then.
    QApplication.processEvents()
    qtbot.waitUntil(lambda: not b.rect().isEmpty(), timeout=5000)
    qtbot.mouseClick(b, Qt.MouseButton.LeftButton)


def sidebar(w) -> list[str]:
    return [w.sidebar.item(i).text() for i in range(w.sidebar.count())]


def shown_text(root) -> list[str]:
    """Everything a person (or a screen reader) could read in ``root`` and its dialogs."""
    out = []
    for label in root.findChildren(QLabel):
        if label.textFormat() == Qt.TextFormat.RichText:
            doc = QTextDocument()
            doc.setHtml(label.text())
            out.append(doc.toPlainText())
        else:
            out.append(label.text())
        out += [label.accessibleName(), label.accessibleDescription(), label.toolTip()]
    for box in root.findChildren(QLineEdit):
        out += [box.text(), box.displayText(), box.accessibleDescription()]
    for t in root.findChildren(QTableWidget):
        out += [t.item(r, c).text() for r in range(t.rowCount()) for c in range(t.columnCount()) if t.item(r, c)]
    for lst in root.findChildren(QListWidget):
        out += [lst.item(i).text() for i in range(lst.count())]
    out += [e.toPlainText() for e in root.findChildren(QPlainTextEdit)]
    return out


def nowhere(w, secret: str, caplog, root: Path | None = None) -> None:
    """``secret`` is in no log record, no job's lines or result, nothing shown, and (with ``root``) no file."""
    assert secret not in caplog.text
    for job in w.jobs.jobs.values():
        assert secret not in "\n".join(job.lines) and secret not in json.dumps(job.result, default=str)
        assert secret not in str(job.error or "")
    assert not [t for t in shown_text(w) if secret in (t or "")]
    if root is not None:
        for f in root.rglob("*"):
            if f.is_file():
                assert secret.encode() not in f.read_bytes(), f


def focused(widget) -> bool:
    """``widget`` has its window's focus (whether or not the offscreen platform made that window the active one)."""
    return widget.window().focusWidget() is widget


def audit(root) -> list[dict]:
    with History(load_site(root).history_path) as hist:
        return hist.audit_log(100)


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


# ------------------------------------------------------------ a new site, in the app alone
def test_a_new_library_sets_up_its_site_and_first_feed_in_the_app_alone(qtbot, tmp_path, folio, keyring, caplog,  # noqa: F811
                                                                         monkeypatch, heard):
    """D3's "done when": connect to FOLIO, add a feed in the wizard, give it somewhere to send, test it, resume."""
    caplog.set_level(logging.DEBUG, logger="folio_exchange")
    folio.password = FOLIO_SECRET
    root = tmp_path / "Lakeside Library"
    root.mkdir()
    w = window(qtbot, root)
    w.activateWindow()
    qtbot.waitUntil(w.isActiveWindow, timeout=5000)
    form = w.setup.form
    assert w.stack.currentWidget() is w.setup and sidebar(w) == ["Set up this site"]
    form.url.setText(folio.url)
    form.tenant.setText("diku")
    form.user.setText("fx_reader")
    form.password.setText("wrong")
    click(qtbot, form.save_button)
    assert form.password.text() == ""                                  # emptied the moment it was taken
    qtbot.waitUntil(lambda: form.problem.isVisible(), timeout=LONG)
    assert "Could not log in" in form.problem.text() and not (root / "site.yaml").exists() and keyring == {}
    assert focused(form.problem) and "Could not log in" in heard[-1]     # read out, not only shown (a11y)
    form.password.setText(FOLIO_SECRET)
    qtbot.keyClick(form.password, Qt.Key.Key_Return)
    assert form.password.text() == ""
    qtbot.waitUntil(lambda: w.stack.currentWidget() is w.wizard, timeout=LONG)    # on to the first feed
    assert keyring[("folio-exchange/folio", "fx_reader@diku")] == FOLIO_SECRET
    assert read_yaml(root / "site.yaml")["folio"]["secret"] == "keyring"
    assert w.wizard.notice.text() == "Connected to FOLIO. Next, add a vendor feed."
    assert focused(w.wizard.recipe_list) and heard[-1] == "Connected to FOLIO. Next, add a vendor feed."

    wz = w.wizard
    qtbot.waitUntil(lambda: wz.recipe_list.count() > 0, timeout=LONG)
    assert "collectionHQ Academic" in wz.recipe_list.item(0).text()
    click(qtbot, wz.next_button)
    assert wz.step() == 1 and wz.name_edit.text() == "chq" and wz.title_edit.text().startswith("collectionHQ Academic")
    qtbot.waitUntil(lambda: wz.perm_state.text().startswith("Yes"), timeout=LONG)
    click(qtbot, wz.next_button)
    qtbot.waitUntil(lambda: wz.loc.list.count() > 0, timeout=LONG)    # the tenant's own locations
    assert "ONLINE – Online" in [wz.loc.list.item(i).text() for i in range(wz.loc.list.count())]
    wz.loc.set_checked(["ONLINE"])
    wz.status.set_checked(["Withdrawn"])
    wz.legacy.boxes["circ_count_note_type"].setCurrentIndex(wz.legacy.boxes["circ_count_note_type"].findData(
        "Legacy circ count"))
    click(qtbot, wz.next_button)
    wz.schedule.radios["daily 03:30"].setChecked(True)
    click(qtbot, wz.next_button)
    facts = {k: v.text() for k, v in wz.summary.values.items()}
    title = facts["Title"]
    assert facts["Locations left out"] == "ONLINE" and facts["Schedule"] == "Daily at 03:30"
    assert facts["Permissions"] == "the account can read everything it needs"
    click(qtbot, wz.add_button)
    qtbot.waitUntil(lambda: w.stack.currentWidget() is w.feed and w.feed.name == "chq", timeout=LONG)
    raw = read_yaml(root / "feeds" / "chq.yaml")
    assert raw["enabled"] is False and raw["delivery"] == {"transport": "none"} and raw["schedule"] == "daily 03:30"
    assert raw["filters"] == {"exclude_location_codes": ["ONLINE"], "exclude_item_statuses": ["Withdrawn"]}
    assert raw["legacy"] == {"circ_count_note_type": "Legacy circ count"}
    page = w.feed
    assert page.tabs.currentIndex() == 3 and page.added.text().startswith(f"Added {title} as a paused dry run.")
    assert focused(page.added) and heard[-1] == page.added.text()       # the move is said, and focus follows
    assert "chq – Paused, no runs yet" in sidebar(w)

    # somewhere to send: a folder, tested, then the feed resumed
    click(qtbot, page.change_button)
    qtbot.waitUntil(lambda: w.delivery_dialog is not None, timeout=LONG)
    dialog = w.delivery_dialog
    dialog.radios["local"].setChecked(True)
    dialog.folder.setText(str(tmp_path / "drop"))
    click(qtbot, dialog.save_button)
    qtbot.waitUntil(lambda: w.delivery_dialog is None, timeout=LONG)
    settled(qtbot, w)
    assert (tmp_path / "drop").is_dir() and page.notice.text() == "Delivery saved. Use Test delivery to try it."
    assert page.added.isHidden()                                       # "nothing is sent until…" is over (the page's)
    click(qtbot, page.test_button)
    settled(qtbot, w)
    assert page.test_result.text().startswith("It works.")
    click(qtbot, page.resume_button)
    qtbot.waitUntil(lambda: "chq – On, no runs yet" in sidebar(w), timeout=LONG)
    froms = [a["detail"]["from"] for a in audit(root) if "from" in (a["detail"] or {})]
    assert len(froms) >= 5 and set(froms) == {"app"}                    # every change audited as made in the app
    actions = [a["action"] for a in audit(root)]
    assert {"site settings written from the app", "feed added from the app (paused)",
            "feed delivery set from the app"} <= set(actions)
    assert not [a for a in actions if "from the page" in a]             # the action says the app, too
    nowhere(w, FOLIO_SECRET, caplog, root)


def test_the_connection_form_checks_what_it_can_before_logging_in(qtbot, tmp_path, keyring):  # noqa: F811
    root = tmp_path / "new"
    root.mkdir()
    w = window(qtbot, root)
    form = w.setup.form
    for url, tenant, user, problem, field in (
            ("http://folio.example.org", "diku", "u", "starting with https://", form.url),
            ("https://u:p@folio.example.org", "diku", "u", "Leave the user name and password out", form.url),
            ("https://folio.example.org", "", "u", "Enter the tenant id.", form.tenant),
            ("https://folio.example.org", "diku", "", "Enter the user name.", form.user),
            ("https://folio.example.org", "diku", "u", "Enter the password for this account.", form.password)):
        form.url.setText(url)
        form.tenant.setText(tenant)
        form.user.setText(user)
        form.password.setText("typed" if field is not form.password else "")
        click(qtbot, form.save_button)
        assert problem in " ".join(form.rows.shown_problems()) and field.hasFocus()
        assert problem in field.accessibleDescription()                 # read out with the field
        assert form.password.text() == "" and w.jobs.current is None     # nothing went anywhere


def test_change_connection_keeps_the_stored_password_and_says_what_changed(qtbot, site, keyring, folio,  # noqa: F811
                                                                          monkeypatch):
    w = window(qtbot, site)
    w.select("connection")
    page = w.connection
    click(qtbot, page.change_button)
    form = w.connect_form
    assert form.isVisible() and form.url.text() == folio.url and form.password.text() == ""
    form.password.setText("not-what-folio-takes")
    w.select("home")                                                   # leaving the view drops what was typed
    assert form.password.text() == "" and not form.isVisible()
    w.select("connection")
    click(qtbot, page.change_button)
    form.tenant.setText("diku")
    click(qtbot, form.save_button)                                     # no password: the stored one (FOLIO_PASSWORD)
    qtbot.waitUntil(lambda: page.notice.isVisible(), timeout=LONG)
    assert page.notice.text().startswith("Logged in and saved. Password: From the environment variable")
    assert not form.isVisible() and page.change_button.isVisible()
    click(qtbot, page.change_button)                                   # regression: "Logging in to FOLIO…" stayed
    assert form.isVisible() and form.status.isHidden() and form.problem.isHidden()


# ------------------------------------------------------------ one action at a time
def test_setup_actions_are_refused_at_once_while_another_runs(qtbot, site, keyring, blocking_job, heard):  # noqa: F811
    w = window(qtbot, site)
    _job, go = blocking_job(w.jobs)
    qtbot.waitUntil(lambda: not w.connect_form.save_button.isEnabled(), timeout=5000)
    assert not w.setup.form.save_button.isEnabled() and not w.options.rename_button.isEnabled()
    assert not w.options.alerts_button.isEnabled() and not w.wizard.add_button.isEnabled()
    assert not w.feed.edit_button.isEnabled() and not w.feed.change_button.isEnabled()
    assert not w.feed.login.password_button.isEnabled()
    # started anyway (a button pressed as the action began): refused at once, in Jobs' words, nothing changed
    w.select("connection")
    w.open_connect_form()
    w.connect_form.password.setText("x")
    w.connect_folio(w.connect_form)
    assert "Another action is still running" in w.connect_form.problem.text() and w.connect_form.password.text() == ""
    w.select("options")
    w.rename_site("Elsewhere")
    assert "Wait for the run of chq to finish" in " ".join(w.options.rows.shown_problems())
    assert site.is_dir() and not (site.parent / "Elsewhere").exists()
    w.wizard.W.update(step=4, name="x", recipe={"default_uses": "chq-academic@^1.0"})
    w.add_feed()
    assert "The feed wasn't added. Another action is still running" in w.wizard.problem.text()
    assert w.wizard.problem.text() in heard
    w.feed.login.password.setText(SFTP_PASSWORD)
    w.save_password("chq")
    assert "Another action is still running" in w.feed.login.problem.text() and keyring == {}
    assert w.feed.login.password.text() == ""
    go.set()
    settled(qtbot, w)
    qtbot.waitUntil(lambda: w.options.rename_button.isEnabled() and w.feed.edit_button.isEnabled(), timeout=5000)


# ------------------------------------------------------------ the wizard
def test_the_wizard_checks_each_step_and_back_keeps_what_was_entered(qtbot, site, heard):
    w = window(qtbot, site)
    w.select("add")
    wz = w.wizard
    qtbot.waitUntil(lambda: wz.recipe_list.count() > 0, timeout=LONG)
    assert wz.where.text() == "Step 1 of 5: Recipe. Next: Name."
    wz.recipe_list.setCurrentItem(None)
    click(qtbot, wz.next_button)
    assert wz.step() == 0 and heard[-1] == "Choose a recipe first."        # announced, not only shown
    wz.recipe_list.setCurrentRow(0)
    assert "Already used by chq" in wz.recipe_list.item(0).text()
    click(qtbot, wz.next_button)
    assert wz.name_edit.text() == "chq-2"                              # chq is taken
    wz.name_edit.setText("chq")
    click(qtbot, wz.next_button)
    assert "There's already a feed called chq" in " ".join(wz.name_rows.shown_problems()) and wz.step() == 1
    wz.name_edit.setText("Bad Name")
    click(qtbot, wz.next_button)
    assert "lowercase" in " ".join(wz.name_rows.shown_problems())
    wz.name_edit.setText("branch")
    wz.title_edit.setText("Branch library")
    click(qtbot, wz.next_button)
    qtbot.waitUntil(lambda: wz.next_button.isEnabled(), timeout=LONG)  # the lists are read
    click(qtbot, wz.back_button)
    assert wz.step() == 1 and wz.name_edit.text() == "branch" and wz.title_edit.text() == "Branch library"
    click(qtbot, wz.next_button)
    click(qtbot, wz.next_button)
    wz.schedule.other.setChecked(True)
    wz.schedule.custom.setText("someday")
    qtbot.keyClick(wz.schedule.custom, Qt.Key.Key_Return)              # Enter where the focus already is
    assert wz.step() == 3 and "is not understood" in wz.schedule_problem.text()
    assert heard[-1] == wz.schedule_problem.text()                     # regression: nothing was read out
    assert wz.schedule.custom.accessibleDescription().startswith(wz.schedule_problem.text())
    assert forms.SCHEDULE_HELP in wz.schedule.custom.accessibleDescription()
    wz.schedule.custom.setText("")
    click(qtbot, wz.next_button)
    assert wz.schedule_problem.text() == "Enter a schedule, or choose one above."
    wz.schedule.custom.setText("Mon,Thu 06:00")
    click(qtbot, wz.next_button)
    assert wz.step() == 4 and wz.summary.values["Schedule"].text() == "Mon,Thu 06:00"
    click(qtbot, wz.cancel_button)
    assert w.stack.currentWidget() is w.feed and wz.step() == 0


def test_the_wizard_works_from_the_keyboard(qtbot, site):
    w = window(qtbot, site)
    w.activateWindow()
    qtbot.waitUntil(w.isActiveWindow, timeout=5000)                    # mnemonics work in the active window
    wz = w.wizard
    wz.say("Connected to FOLIO. Next, add a vendor feed.")
    w.pending_select = ("add", None)                                   # as after connecting: the window moves itself
    w.refresh()
    qtbot.waitUntil(lambda: w.stack.currentWidget() is wz and wz.recipe_list.count() > 0, timeout=LONG)
    assert focused(wz.recipe_list)                                     # the focus came along (no setFocus here)
    qtbot.keyClick(wz.recipe_list, Qt.Key.Key_Return)                 # Enter on a recipe chooses it
    assert wz.step() == 1 and wz.name_edit.hasFocus()
    wz.name_edit.setText("kb")
    qtbot.keyClick(wz.name_edit, Qt.Key.Key_Return)
    assert wz.step() == 2
    qtbot.waitUntil(lambda: wz.next_button.isEnabled(), timeout=LONG)
    qtbot.keyClick(QApplication.focusWidget(), Qt.Key.Key_N, Qt.KeyboardModifier.AltModifier)    # &Next
    qtbot.waitUntil(lambda: wz.step() == 3, timeout=5000)             # a mnemonic presses the button briefly
    qtbot.keyClick(QApplication.focusWidget(), Qt.Key.Key_N, Qt.KeyboardModifier.AltModifier)
    qtbot.waitUntil(lambda: wz.step() == 4, timeout=5000)
    assert wz.add_button.hasFocus()
    qtbot.keyClick(wz.add_button, Qt.Key.Key_Space)
    qtbot.waitUntil(lambda: w.stack.currentWidget() is w.feed and w.feed.name == "kb", timeout=LONG)
    assert read_yaml(site / "feeds" / "kb.yaml")["file_prefix"] == "kb"   # never chq's files at the same vendor


def test_each_wizard_step_fits_the_window_and_starts_at_its_top(qtbot, site):
    """Regression: every step was as tall as Records (a stack is as tall as its tallest page), so the schedule's
    choices sat below the window, and Check and add opened scrolled down to its buttons, past its own heading."""
    w = window(qtbot, site)
    w.resize(1100, 760)
    w.select("add")
    wz = w.wizard

    def in_view(widget) -> bool:
        top = widget.mapTo(wz.viewport(), QPoint(0, 0)).y()
        return 0 <= top and top + widget.height() <= wz.viewport().height()

    qtbot.waitUntil(lambda: wz.recipe_list.count() > 0, timeout=LONG)
    click(qtbot, wz.next_button)
    wz.name_edit.setText("fits")
    click(qtbot, wz.next_button)
    qtbot.waitUntil(lambda: wz.next_button.isEnabled(), timeout=LONG)
    bar = wz.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 0, timeout=5000)           # Records' long lists do scroll…
    bar.setValue(bar.maximum())                                        # …and the person went down to Next
    click(qtbot, wz.next_button)
    assert wz.step() == 3
    qtbot.waitUntil(lambda: bar.maximum() == 0, timeout=5000)          # the schedule fits: nothing to scroll to
    assert all(in_view(r) for r in wz.schedule.radios.values()) and in_view(wz.schedule.other)
    click(qtbot, wz.next_button)
    assert wz.step() == 4
    qtbot.waitUntil(lambda: bar.maximum() == 0, timeout=5000)
    assert bar.value() == 0 and in_view(wz.steps.currentWidget().heading) and in_view(wz.add_button)
    click(qtbot, wz.back_button)
    click(qtbot, wz.back_button)
    assert wz.step() == 2 and bar.value() == 0                          # back at the top of Records too


def test_without_folios_lists_the_wizard_takes_typed_codes(qtbot, site, monkeypatch):
    from folio_exchange import setupops

    def down(_site):
        raise FxError("FOLIO did not answer")

    monkeypatch.setattr(setupops, "tenant_choices", down)
    w = window(qtbot, site)
    w.select("add")
    wz = w.wizard
    qtbot.waitUntil(lambda: wz.recipe_list.count() > 0, timeout=LONG)
    click(qtbot, wz.next_button)
    wz.name_edit.setText("typed")
    click(qtbot, wz.next_button)
    qtbot.waitUntil(lambda: wz.choices is not None, timeout=LONG)
    assert "Couldn't read the lists from FOLIO (FOLIO did not answer)" in wz.choices_state.text()
    assert wz.loc.typed and wz.loc.codes.isVisibleTo(wz) and wz.choices_button.isVisibleTo(wz)
    # how to separate the codes is said, as the page's "Separate them with commas." (regression)
    assert wz.loc.codes.accessibleDescription().endswith("Separate them with commas.")
    assert wz.loc.commas.isVisibleTo(wz) and "commas" in wz.loc.codes.placeholderText()
    wz.loc.codes.setText("ONLINE, MAIN-REF")
    wz.legacy.boxes["created_date_note_type"].setEditText("Old created")
    click(qtbot, wz.next_button)
    click(qtbot, wz.next_button)
    click(qtbot, wz.add_button)
    qtbot.waitUntil(lambda: (site / "feeds" / "typed.yaml").exists() and w.jobs.running() is None, timeout=LONG)
    raw = read_yaml(site / "feeds" / "typed.yaml")
    assert raw["filters"]["exclude_location_codes"] == ["ONLINE", "MAIN-REF"]
    assert raw["legacy"] == {"created_date_note_type": "Old created"}


# ------------------------------------------------------------ a feed's settings
def open_settings(qtbot, w, name="chq") -> SettingsDialog:
    w.select(f"feed:{name}")
    w.feed.tabs.setCurrentIndex(2)
    click(qtbot, w.feed.edit_button)
    qtbot.waitUntil(lambda: w.settings_dialog is not None, timeout=LONG)
    return w.settings_dialog


def test_settings_are_reviewed_before_and_after_then_saved(qtbot, site):
    w = window(qtbot, site)
    d = open_settings(qtbot, w)
    old = d.title_edit.text()
    assert old.startswith("collectionHQ Academic") and d.loc.value() == ["ONLINE"]
    assert d.legacy.value()["circ_count_note_type"] == "Legacy circ count"
    d.title_edit.setText("Academic (cHQ)")
    d.ratio.setText("85")
    d.flags["include_suppressed_items"].setChecked(True)
    click(qtbot, d.review_button)
    review = QTextDocument()
    review.setHtml(d.review_text.text())
    assert f"Title: {old} → Academic (cHQ)" in review.toPlainText()
    assert "Block a delivery if the item count falls below: 90% of the last one → 85% of the last one" in         review.toPlainText()
    assert d.save_button.isVisible() and d.save_button.hasFocus()
    click(qtbot, d.back_button)
    assert d.pages.currentIndex() == 0 and d.title_edit.text() == "Academic (cHQ)"   # nothing lost
    click(qtbot, d.review_button)
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: w.settings_dialog is None, timeout=LONG)
    settled(qtbot, w)
    raw = read_yaml(site / "feeds" / "chq.yaml")
    assert raw["title"] == "Academic (cHQ)" and raw["checks"] == {"min_item_ratio": 0.85}
    assert raw["filters"]["include_suppressed_items"] is True
    assert w.feed.notice.text() == "Settings saved. Use Preview to see the effect."
    entry = next(a for a in audit(site) if a["action"] == "feed settings changed from the app")
    assert entry["detail"]["from"] == "app" and set(entry["detail"]["changed"]) == {"title", "filters", "checks"}
    assert w.feed.title.text() == "Academic (cHQ)"


def test_settings_that_cannot_be_saved_say_why_beside_the_field(qtbot, site, heard):
    w = window(qtbot, site)
    d = open_settings(qtbot, w)
    help_ = forms.SCHEDULE_HELP
    assert d.schedule.custom.accessibleDescription() == help_
    d.schedule.other.setChecked(True)
    d.schedule.custom.setText("someday")
    click(qtbot, d.review_button)
    assert d.schedule.custom.accessibleDescription().endswith(help_)   # the examples stay with the problem
    d.schedule.custom.setText("daily 06:00")
    click(qtbot, d.review_button)
    click(qtbot, d.back_button)
    assert d.schedule.custom.accessibleDescription() == help_          # regression: cleared to nothing
    d.schedule.set_value(read_yaml(site / "feeds" / "chq.yaml").get("schedule"))
    ratio = d.ratio.text()
    for value in ("150", "lots"):
        d.ratio.setText(value)
        click(qtbot, d.review_button)
        assert d.pages.currentIndex() == 0 and d.ratio.hasFocus()
        assert "percentage from 0 to 100" in " ".join(d.rows.shown_problems())
    d.ratio.setText(ratio)
    d.prefix.setText("bad prefix!")
    click(qtbot, d.review_button)
    click(qtbot, d.save_button)                                        # the core refuses it
    qtbot.waitUntil(lambda: d.problem.isVisible(), timeout=LONG)
    assert "letters, digits, hyphens and underscores" in d.problem.text() and d.pages.currentIndex() == 0
    assert heard[-1] == d.problem.text()
    assert d.prefix.text() == "bad prefix!"                             # what was typed stays
    d.prefix.setText("test")
    click(qtbot, d.review_button)
    assert d.review_title.text() == "Nothing has changed" and d.close_button.isVisible()
    click(qtbot, d.close_button)
    qtbot.waitUntil(lambda: w.settings_dialog is None, timeout=5000)


def test_a_file_changed_underneath_the_form_keeps_what_was_entered_and_offers_to_apply_it(qtbot, site):
    w = window(qtbot, site)
    d = open_settings(qtbot, w)
    d.title_edit.setText("Mine")
    path = site / "feeds" / "chq.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("Sun 02:15", "Sun 04:45"), encoding="utf-8")  # by hand
    click(qtbot, d.review_button)
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: d.review_title.text() == "Not saved because the file changed", timeout=LONG)
    assert "What you entered is still here." in d.review_text.text()
    assert d.apply_button.hasFocus() and read_yaml(path).get("title") is None
    click(qtbot, d.apply_button)
    qtbot.waitUntil(lambda: d.review_title.text() == "Review changes", timeout=LONG)
    assert not d.notice.isHidden() and d.notice.text().startswith("The form now shows the updated file")
    assert d.title_edit.text() == "Mine" and d.schedule.value() == "Sun 04:45"
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: w.settings_dialog is None, timeout=LONG)
    raw = read_yaml(path)
    assert raw["title"] == "Mine" and raw["schedule"] == "Sun 04:45"          # the hand edit kept, the change made


# ------------------------------------------------------------ a feed's delivery
def open_delivery(qtbot, w, name="chq") -> DeliveryDialog:
    w.select(f"feed:{name}")
    w.feed.tabs.setCurrentIndex(3)
    click(qtbot, w.feed.change_button)
    qtbot.waitUntil(lambda: w.delivery_dialog is not None, timeout=LONG)
    return w.delivery_dialog


def sftp(d: DeliveryDialog, fingerprint: str = "", later: bool = False) -> None:
    d.radios["sftp"].setChecked(True)
    d.host.setText("sftp.vendor.example")
    d.user.setText("lib1")
    d.fingerprint.setText(fingerprint)
    d.later.setChecked(later)


def test_without_a_keyring_the_login_help_says_where_each_secret_is_read(qtbot):
    """Regression: with no Credential Manager the SFTP password, key and passphrase help still said each was
    stored there."""
    login = LoginBox()
    qtbot.addWidget(login)
    fields = ((login.password_rows, login.password), (login.key_rows, login.choose_button),
              (login.key_rows, login.passphrase))

    def helps() -> list[str]:
        return [rows.help_labels[w].text() for rows, w in fields] + [w.accessibleDescription() for _rows, w in fields]

    login.show_login("chq", {"auth": "password"}, keyring=False)
    assert not login.no_keyring.isHidden()
    assert not [h for h in helps() if "Stored in Windows Credential Manager" in h or "kept in this site" in h]
    assert "secret: env:NAME" in helps()[0] and "private_key" in helps()[1] and "secret: env:NAME" in helps()[2]
    login.show_login("chq", {"auth": "password"}, keyring=True)          # a keyring again: its own help is back
    assert helps()[0].startswith("Stored in Windows Credential Manager") and "keys folder" in helps()[1]


def test_without_a_keyring_the_alert_help_says_where_each_secret_is_read(qtbot):
    """Regression: with no Credential Manager the mail password and webhook help said they were stored there."""
    email = {"to": ["a@x.org"], "from": "f@x.org", "smtp_host": "smtp.x.org", "smtp_port": 587, "username": "u",
             "password": "NOT FOUND (environment variable FX_SMTP_PASSWORD is not set)"}
    hook = {"format": "slack", "url": "NOT FOUND (environment variable FX_WEBHOOK_URL is not set)"}
    off = AlertsDialog({"on": "failure", "keyring": False, "email": email, "webhook": hook})
    qtbot.addWidget(off)
    said = [off.email_rows.helps[off.password], off.hook_rows.helps[off.url]]
    assert not [h for h in said if "Stored in Windows Credential Manager" in h or "It's stored in" in h], said
    assert "secret: env:NAME" in said[0] and "webhook: env:NAME" in said[1]
    on = AlertsDialog({"on": "failure", "keyring": True, "email": email, "webhook": hook})
    qtbot.addWidget(on)
    assert on.email_rows.helps[on.password].startswith("Stored in Windows Credential Manager")
    assert "stored in Windows Credential Manager" in on.hook_rows.helps[on.url]


def test_a_refused_delivery_says_nothing_was_saved_once(qtbot, site):
    """Regression: a refusal whose own words say nothing was saved read "Not saved. … nothing was saved"."""
    st = {"version": "v", "delivery": {"transport": "none"}}
    d = DeliveryDialog("chq", st)
    qtbot.addWidget(d)
    core = "A different key can mean the wrong server, so nothing was saved. Check the value with the vendor."
    d.failed(core)
    assert d.problem.text() == core
    d.failed("Nothing was saved, because site.yaml would not load.")
    assert d.problem.text() == "Nothing was saved, because site.yaml would not load."
    d.failed("Another action is still running (check).")             # it doesn't say so: the prefix does
    assert d.problem.text() == "Not saved. Another action is still running (check)."


def test_sftp_delivery_is_saved_only_with_the_vendors_fingerprint(qtbot, site, server_keys, heard):
    w = window(qtbot, site)
    path = site / "feeds" / "chq.yaml"
    before = path.read_bytes()
    d = open_delivery(qtbot, w)
    assert d.radios["local"].isChecked() and d.local_box.isVisibleTo(d) and not d.sftp_box.isVisibleTo(d)
    sftp(d)
    click(qtbot, d.save_button)
    assert "Enter the fingerprint the vendor gave you, or check the box" in " ".join(d.rows.shown_problems())
    assert server_keys == []                                           # nothing was asked of the server
    d.fingerprint.setText("not a fingerprint")
    click(qtbot, d.save_button)
    assert "doesn't look like a server key fingerprint" in " ".join(d.rows.shown_problems())
    d.fingerprint.setText("SHA256:" + "Z" * 43)                        # a key, but not this server's
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: d.problem.isVisible(), timeout=LONG)
    assert "which is not the fingerprint you entered" in d.problem.text() and "nothing was saved" in d.problem.text()
    assert not d.problem.text().startswith("Not saved.")              # said once, in the core's words
    assert heard[-1] == d.problem.text() and focused(d.problem)       # read out, not only the button's name
    assert path.read_bytes() == before and server_keys == [("sftp.vendor.example", 22)]
    d.fingerprint.setText(KEYS["sha256"])
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: w.delivery_dialog is None, timeout=LONG)
    settled(qtbot, w)
    dl = read_yaml(path)["delivery"]
    assert dl["host_key_fingerprint"] == KEYS["sha256"] and "host_key_confirmed" not in dl
    page = w.feed
    assert page.notice.text() == "Delivery saved. Use Test delivery to try it."
    assert KEYS["sha256"] in page.server_key.text() and page.server_key.isVisibleTo(page)
    assert "Confirmed" in page.where.values["Server key"].text()
    assert page.login.isVisibleTo(page) and page.login.use_password.isChecked()
    assert page.where.values["Login"].text() == "No password or key is stored yet, so nothing can be sent."


def test_without_the_fingerprint_yet_the_feed_is_paused_and_nothing_logs_in(qtbot, site, server_keys):
    w = window(qtbot, site)
    d = open_delivery(qtbot, w)
    sftp(d, later=True)
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: w.delivery_dialog is None, timeout=LONG)
    settled(qtbot, w)
    raw = read_yaml(site / "feeds" / "chq.yaml")
    assert raw["enabled"] is False and raw["delivery"]["host_key_confirmed"] is False
    page = w.feed
    assert page.notice.text() == ("Delivery saved. The server key isn't confirmed yet, so the feed is paused and "
                                  "nothing logs in.")
    assert not page.test_button.isEnabled() and page.test_why.isVisibleTo(page)
    assert page.resume_row.isVisibleTo(page) and "Not confirmed yet" in page.where.values["Server key"].text()


def test_a_local_folder_must_be_given_in_full_and_can_be_browsed_for(qtbot, site, tmp_path):
    w = window(qtbot, site)
    d = open_delivery(qtbot, w)
    d.folder.setText("drop")
    click(qtbot, d.save_button)
    assert "full path" in " ".join(d.local_rows.shown_problems()) and d.folder.hasFocus()
    d.ask_folder = lambda: str(tmp_path / "picked")
    click(qtbot, d.findChildren(QAbstractButton, None)[[b.text() for b in d.findChildren(QAbstractButton)]
                                                       .index("&Browse…")])
    assert d.folder.text() == str(tmp_path / "picked")
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: w.delivery_dialog is None, timeout=LONG)
    assert (tmp_path / "picked").is_dir()


def with_sftp(qtbot, w, site) -> None:
    d = open_delivery(qtbot, w)
    sftp(d, KEYS["sha256"])
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: w.delivery_dialog is None, timeout=LONG)
    settled(qtbot, w)


def test_the_sftp_password_goes_to_credential_manager_and_nowhere_else(qtbot, site, keyring, server_keys,  # noqa: F811
                                                                      caplog):
    caplog.set_level(logging.DEBUG, logger="folio_exchange")
    w = window(qtbot, site)
    with_sftp(qtbot, w, site)
    login = w.feed.login
    click(qtbot, login.password_button)
    assert "Enter the password the vendor gave you." in " ".join(login.password_rows.shown_problems())
    login.password.setText(SFTP_PASSWORD)
    assert login.password.echoMode() == QLineEdit.EchoMode.Password and login.password.displayText() != SFTP_PASSWORD
    click(qtbot, login.password_button)
    assert login.password.text() == ""
    settled(qtbot, w)
    assert keyring[("folio-exchange/sftp", "lib1@sftp.vendor.example")] == SFTP_PASSWORD
    assert w.feed.notice.text() == "Password saved in Credential Manager. chq logs in with it from now on."
    assert w.feed.where.values["Login"].text().startswith("With a password. Stored in Windows Credential Manager")
    entry = next(a for a in audit(site) if a["action"] == "secret stored")
    assert entry["detail"]["what"] == "SFTP password" and entry["detail"]["from"] == "app"
    nowhere(w, SFTP_PASSWORD, caplog, site)
    assert SFTP_PASSWORD not in json.dumps(audit(site))


def test_a_typed_password_does_not_outlive_the_feed_it_was_typed_for(qtbot, site, server_keys):
    make_feed(site, "other", file_prefix="other", delivery={"transport": "local", "directory": str(site / "o")})
    w = window(qtbot, site)
    with_sftp(qtbot, w, site)
    w.feed.login.password.setText(SFTP_PASSWORD)
    w.feed.login.passphrase.setText(PASSPHRASE)
    w.select("feed:other")
    assert w.feed.login.password.text() == "" and w.feed.login.passphrase.text() == ""
    w.select("feed:chq")
    w.feed.login.password.setText(SFTP_PASSWORD)
    w.select("activity")
    assert w.feed.login.password.text() == ""
    w.select("feed:chq")
    w.feed.tabs.setCurrentIndex(3)
    w.feed.login.passphrase.setText(PASSPHRASE)
    w.feed.tabs.setCurrentIndex(4)                                     # leaving the Delivery tab, as on the page
    assert w.feed.login.passphrase.text() == ""


def test_a_key_file_too_large_is_refused_before_it_is_read(qtbot, site, keyring, server_keys, tmp_path,  # noqa: F811
                                                            heard):
    big = tmp_path / "huge.key"
    big.write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\n" + b"A" * 20000)
    w = window(qtbot, site)
    with_sftp(qtbot, w, site)
    login = w.feed.login
    login.use_key.setChecked(True)
    login.ask_key_file = lambda: str(big)
    click(qtbot, [b for b in login.findChildren(QAbstractButton) if b.text() == "C&hoose the key file…"][0])
    assert login.key_file.text() == str(big)                           # chosen, before it is saved
    click(qtbot, login.key_button)
    qtbot.waitUntil(lambda: login.problem.isVisibleTo(login), timeout=LONG)
    assert login.problem.text() == "That file is too large to be a private key. Choose the file again to retry."
    assert heard[-1] == login.problem.text() and focused(login.problem)
    assert not (site / "keys").exists()


def test_a_delivery_saved_on_a_changed_file_keeps_the_choices_and_saves_them_again(qtbot, site, server_keys, heard):
    w = window(qtbot, site)
    d = open_delivery(qtbot, w)
    sftp(d, KEYS["sha256"])
    path = site / "feeds" / "chq.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("Sun 02:15", "Sun 05:00"), encoding="utf-8")
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: d.again_button.isVisibleTo(d), timeout=LONG)
    assert d.problem.text().startswith("Not saved.") and "What you entered is still here." in d.problem.text()
    assert heard[-1] == d.problem.text() and focused(d.again_button)
    assert d.again_button.accessibleDescription() == d.problem.text()
    assert d.host.text() == "sftp.vendor.example" and d.fingerprint.text() == KEYS["sha256"]
    click(qtbot, d.again_button)
    qtbot.waitUntil(lambda: w.delivery_dialog is None, timeout=LONG)
    raw = read_yaml(path)
    assert raw["schedule"] == "Sun 05:00" and raw["delivery"]["host"] == "sftp.vendor.example"


def test_a_server_offering_several_keys_lists_them_all(qtbot, site, monkeypatch):
    rsa = {"type": "ssh-rsa", "sha256": "SHA256:" + "R" * 43, "md5": "aa:" * 15 + "aa"}
    ed = {"type": "ssh-ed25519", **KEYS}
    monkeypatch.setattr(delivery, "host_key_fingerprints", lambda h, p, timeout=30: {**ed, "keys": [ed, rsa]})
    w = window(qtbot, site)
    d = open_delivery(qtbot, w)
    sftp(d, rsa["sha256"])
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: w.delivery_dialog is None, timeout=LONG)
    text_ = w.feed.server_key.text()
    assert "The server offered several keys" in text_ and "The ssh-rsa key is the one kept for this feed." in text_
    assert rsa["sha256"] in text_ and KEYS["sha256"] in text_
    assert read_yaml(site / "feeds" / "chq.yaml")["delivery"]["host_key_type"] == "ssh-rsa"


def test_a_run_whose_rows_cannot_be_read_still_opens(qtbot, site):
    """Regression (D2): csv.Error on a value over the csv module's limit stopped the run opening at all."""
    from folio_exchange import views

    res = views.run(site, "chq", deliver=False)
    run = res["run"]
    items = next(f for f in run["files"] if not f["name"].endswith(".mrc"))
    with open(load_site(site).out_dir / "chq" / run["folder"] / items["name"], "a", encoding="utf-8") as fh:
        fh.write("y" * 200_000 + "\n")
    w = window(qtbot, site)
    w.select("feed:chq")
    w.open_run("chq", run["id"])
    qtbot.waitUntil(lambda: (w.feed.results.get("chq") or {}).get("run", {}).get("id") == run["id"], timeout=LONG)
    assert "could not be read" in w.feed.result.rows_line.text()
    assert w.feed.notice.text().startswith("Showing the run of")


def test_a_private_key_file_is_read_kept_owner_only_and_its_passphrase_stored(qtbot, site, keyring, server_keys,  # noqa: F811
                                                                             tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="folio_exchange")
    key_text = _ed25519_openssh(PASSPHRASE.encode())
    chosen = tmp_path / "vendor_key"
    chosen.write_text(key_text, encoding="utf-8")
    pub = tmp_path / "vendor_key.pub"
    pub.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI example@pc\n", encoding="utf-8")
    w = window(qtbot, site)
    with_sftp(qtbot, w, site)
    login = w.feed.login
    login.use_key.setChecked(True)
    assert login.key_box.isVisibleTo(login) and not login.password_box.isVisibleTo(login)
    click(qtbot, login.key_button)
    assert "Choose the private key file." in " ".join(login.key_rows.shown_problems())
    login.ask_key_file = lambda: str(pub)                              # the public half: refused, and why
    click(qtbot, [b for b in login.findChildren(QAbstractButton) if b.text() == "C&hoose the key file…"][0])
    assert login.key_file.text() == str(pub)
    login.passphrase.setText(PASSPHRASE)
    click(qtbot, login.key_button)
    assert login.key_file.text() == "" and login.passphrase.text() == ""
    settled(qtbot, w)
    assert "not a private key" in login.problem.text() and login.problem.text().endswith(
        "Choose the file again to retry.")
    assert not (site / "keys" / "chq.key").exists()
    login.ask_key_file = lambda: str(chosen)
    click(qtbot, [b for b in login.findChildren(QAbstractButton) if b.text() == "C&hoose the key file…"][0])
    assert login.key_file.text() == str(chosen)
    login.passphrase.setText(PASSPHRASE)
    click(qtbot, login.key_button)
    settled(qtbot, w)
    stored = site / "keys" / "chq.key"
    assert stored.read_text(encoding="utf-8").strip() == key_text.strip()
    assert chosen.read_text(encoding="utf-8") == key_text                 # the original is left as it was
    assert keyring[("folio-exchange/sftp", "lib1@sftp.vendor.example")] == PASSPHRASE
    assert login.key_saved.text().startswith("Key saved. Its public fingerprint is ssh-ed25519 SHA256:")
    assert read_yaml(site / "feeds" / "chq.yaml")["delivery"]["private_key"] == "keys/chq.key"
    assert w.feed.where.values["Login"].text().startswith("With a private key")
    if sys.platform != "win32":
        assert stored.stat().st_mode & 0o077 == 0
    nowhere(w, PASSPHRASE, caplog, site)                               # the site's files too (the key is encrypted)
    assert PASSPHRASE not in json.dumps(audit(site))
    assert "PRIVATE KEY" not in caplog.text and key_text.splitlines()[1] not in caplog.text
    for job in w.jobs.jobs.values():
        assert "PRIVATE KEY" not in "\n".join(job.lines)


# ------------------------------------------------------------ recipes
def test_recipes_are_checked_and_installed_from_a_vendors_source(qtbot, site, monkeypatch):
    gh = FakeGitHub({"recipes/chq-academic/1.0.0.yaml": (sources.BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml")
                     .read_bytes().replace(b"\r\n", b"\n"), "recipes/chq-academic/1.1.0.yaml": newer()})
    monkeypatch.setattr(sources, "_session", lambda: gh)
    w = window(qtbot, site)
    w.select("recipes")
    page = w.recipes
    qtbot.waitUntil(lambda: page.data is not None, timeout=LONG)
    texts = shown_text(page)
    assert any(t.startswith("collectionHQ Academic") for t in texts)
    assert any(t.startswith("1.0.0 – comes with FOLIO-Exchange") for t in texts)
    assert "chq" in texts                                               # used by
    check = page.buttons["foliohq"]["check"]
    assert check.text() == "Check for updates"
    click(qtbot, check)
    qtbot.waitUntil(lambda: "install" in page.buttons.get("foliohq", {}), timeout=LONG)
    texts = " ".join(shown_text(page))
    assert "New: collectionHQ Academic" in texts and "Installed: collectionHQ Academic" in texts
    assert "1.1.0 · collectionHQ · not installed yet. What changed: Adds a column." in texts
    assert "From their next run, these feeds use it: chq." in texts
    assert f"Commit {SHA[:7]}" in texts and not (site / "recipes" / "chq-academic" / "1.1.0.yaml").exists()
    install = page.buttons["foliohq"]["install"]
    assert install.text() == "Install 1 new version"
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    checks = [b for b in page.findChildren(QAbstractButton) if b.text() in ("Check for updates", "Check again")]
    assert len(checks) == 1                                            # regression: a redraw left the old button
    click(qtbot, install)
    qtbot.waitUntil(lambda: (page.checks.get("foliohq") or {}).get("installed") is not None, timeout=LONG)
    settled(qtbot, w)
    assert (site / "recipes" / "chq-academic" / "1.1.0.yaml").read_bytes() == newer()
    assert "Installed chq-academic@1.1.0." in " ".join(shown_text(page))
    entry = next(a for a in audit(site) if a["action"] == "recipes downloaded")
    assert entry["detail"]["from"] == "app" and entry["detail"]["commit"] == SHA
    qtbot.waitUntil(lambda: any("downloaded from collectionHQ" in t for t in shown_text(page)), timeout=LONG)


def test_a_failed_check_and_a_refused_install_say_why(qtbot, site, monkeypatch, blocking_job):
    class Down:
        def get(self, *a, **k):
            import requests

            raise requests.ConnectionError("offline")

    monkeypatch.setattr(sources, "_session", lambda: Down())
    w = window(qtbot, site)
    w.select("recipes")
    qtbot.waitUntil(lambda: w.recipes.data is not None, timeout=LONG)
    click(qtbot, w.recipes.buttons["foliohq"]["check"])
    qtbot.waitUntil(lambda: bool((w.recipes.checks.get("foliohq") or {}).get("error")), timeout=LONG)
    assert "Could not reach GitHub" in w.recipes.buttons["foliohq"]["review"].text()
    _job, go = blocking_job(w.jobs)
    w.install_recipes("foliohq", SHA)
    assert "Another action is still running" in w.recipes.checks["foliohq"]["install_error"]
    go.set()


# ------------------------------------------------------------ options: the folder
def test_the_site_folder_is_renamed_in_the_app_and_the_window_follows_it(qtbot, site, monkeypatch):
    make_feed(site, delivery={"transport": "local", "directory": str(site.parent / "vendor-drop")})
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")      # no scheduled task to look for
    w = window(qtbot, site)
    w.select("options")
    page = w.options
    assert page.folder.values["Site folder"].text() == str(site) and page.name.text() == "site"
    page.name.setText("Lakeside Library")
    assert page.becomes.text() == f"The folder becomes: {site.parent / 'Lakeside Library'}"
    page.name.setText("bad:name")
    click(qtbot, page.rename_button)
    assert "cannot contain" in " ".join(page.rows.shown_problems()) and site.is_dir()
    page.name.setText("Lakeside Library")
    click(qtbot, page.rename_button)
    new = site.parent / "Lakeside Library"
    qtbot.waitUntil(lambda: page.done.isVisible(), timeout=LONG)
    settled(qtbot, w)
    assert new.is_dir() and not site.exists()
    assert w.site_root == new.resolve() and w.page.site_root == new.resolve()
    assert w.windowTitle() == "FOLIO-Exchange – Lakeside Library"
    assert page.done.text() == "Renamed to Lakeside Library. Nothing else needs doing."
    assert places.recent_sites()[0] == new.resolve()
    entry = next(a for a in audit(new) if a["action"] == "site folder renamed")
    assert entry["detail"]["via"] == "app"
    qtbot.waitUntil(lambda: page.folder.values["Site folder"].text() == str(new.resolve()), timeout=LONG)


def test_a_rename_waits_for_a_reading_of_the_site_and_is_the_running_action(qtbot, site, monkeypatch):
    make_feed(site, delivery={"transport": "local", "directory": str(site.parent / "vendor-drop")})
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")
    seen = []
    real = siteops.rename_site

    def rename(root, name, **kw):
        seen.append(w.jobs.running().kind if w.jobs.running() else None)
        return real(root, name, **kw)

    monkeypatch.setattr(siteops, "rename_site", rename)
    w = window(qtbot, site)
    w.select("options")
    w.refresh()
    assert w.reader.busy                                               # a reading is under way
    w.rename_site("Renamed")
    assert w.renaming == "Renamed" and not w.renaming_started and seen == []
    qtbot.waitUntil(lambda: w.renaming is None, timeout=LONG)          # it started once the reading ended
    assert seen == ["rename"] and (site.parent / "Renamed").is_dir()


# ------------------------------------------------------------ options: the scheduled task
def test_registering_the_task_runs_the_command_line_in_a_console_of_its_own(qtbot, site, monkeypatch):
    started = []
    looked = []
    monkeypatch.setattr(siteops, "task_registered", lambda root, run=None: looked.append(root) or bool(started))
    w = window(qtbot, site)
    w.options.set_platform(windows=True)                               # what Windows shows, wherever this runs
    w.start_console = lambda exe, args, folder: started.append((exe, args, folder)) or 0
    w.select("options")
    qtbot.waitUntil(lambda: looked and not w.errands.pending, timeout=LONG)
    assert w.options.registered.text().startswith("Task Scheduler doesn't have this site's task yet")
    click(qtbot, w.options.register_button)
    qtbot.waitUntil(lambda: started and not w.errands.pending and not w.registering, timeout=LONG)
    (exe, args, folder), = started
    assert args[-4:] == ["--site", str(site.resolve()), "schedule", "--register"]
    assert Path(folder) == Path(sys.executable).parent                 # never the current folder
    assert Path(exe).name.lower() in ("folio-exchange.exe", "folio-exchange", "python.exe", "python")
    assert w.options.register_result.text() == "Registered. Task Scheduler starts the runs every hour from now on."
    qtbot.waitUntil(lambda: w.options.registered.text().startswith("Task Scheduler has the task"), timeout=LONG)
    w.start_console = lambda exe, args, folder: 1                       # a mistyped password
    click(qtbot, w.options.register_button)
    qtbot.waitUntil(lambda: "exit code 1" in w.options.register_result.text(), timeout=LONG)


def test_the_register_command_never_comes_from_the_current_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(options_module.sysconfig, "get_path", lambda _name: str(tmp_path))   # no installed program
    exe, args, folder = options_module.task_command(tmp_path / "Lib")
    assert args == [*(["-P"] if sys.version_info >= (3, 11) else []), "-m", "folio_exchange", "--site",
                    str(tmp_path / "Lib"), "schedule", "--register"]
    assert Path(folder) == Path(sys.executable).parent and not Path(exe).name.lower().startswith("pythonw")
    (tmp_path / ("folio-exchange.exe" if sys.platform == "win32" else "folio-exchange")).write_text("")
    exe, args, _folder = options_module.task_command(tmp_path / "Lib")
    assert Path(exe).parent == tmp_path and args[0] == "--site"
    calls = []
    monkeypatch.setattr(options_module.subprocess, "run",
                        lambda argv, **kw: calls.append((argv, kw)) or type("R", (), {"returncode": 0})())
    assert options_module.start_console("prog", ["a"], "here") == 0
    argv, kw = calls[0]
    assert argv == ["prog", "a"] and kw["cwd"] == "here" and kw["stdin"] is None and kw["stdout"] is None
    assert kw["creationflags"] == getattr(options_module.subprocess, "CREATE_NEW_CONSOLE", 0)   # a console of its own


def test_elsewhere_the_task_is_a_crontab_line_to_add(qtbot, site, monkeypatch):
    w = window(qtbot, site)
    w.options.set_platform(windows=False)
    w.options.show_site(w.snap["summary"])
    assert not w.options.register_button.isVisibleTo(w.options) and w.options.cron.isVisibleTo(w.options)
    assert "crontab -e" in w.options.cron.text() and "run --due" in w.options.cron.text()
    # regression: the task's state pointed to a Register button that is not there off Windows
    assert "crontab -e" in w.options.task.text() and "Register the scheduled task" not in w.options.task.text()
    w.register_task()                                                # nothing to start here
    assert not w.registering


def test_a_task_that_never_ran_is_worded_by_the_app_on_home_and_options(qtbot, site):
    """Regression: Home and Options showed the core's words for a task that never ran ("Scheduled runs have not
    started yet: no feed runs on its own …"), a clause joined by a colon."""
    w = window(qtbot, site)
    assert (w.snap["summary"].get("task") or {}).get("state") in ("never", "unneeded")
    w.select("options")
    for label in (w.home.task, w.options.task):
        assert label.text().startswith("Scheduled runs haven't started yet. No feed runs on its own"), label.text()
        assert "have not" not in label.text() and ": no feed" not in label.text()
    assert "have not started" not in w.home.warnings.text()             # the site's task-never warning too


# ------------------------------------------------------------ options: alerts
def test_alerts_are_set_up_with_their_secrets_in_credential_manager(qtbot, site, keyring, caplog, monkeypatch):  # noqa: F811
    caplog.set_level(logging.DEBUG, logger="folio_exchange")
    w = window(qtbot, site)
    w.select("options")
    page = w.options
    qtbot.waitUntil(lambda: "When" in page.alerts.values, timeout=LONG)
    assert page.alerts.values["Email"].text() == "not set up"
    click(qtbot, page.alerts_button)
    qtbot.waitUntil(lambda: w.alerts_dialog is not None, timeout=LONG)
    d: AlertsDialog = w.alerts_dialog
    d.on.setCurrentIndex(d.on.findData("failure"))
    d.use_email.setChecked(True)
    d.to.setText("head@lib.example")
    d.sender.setText("not an address")
    click(qtbot, d.save_button)
    assert "Enter the address the alerts come from." in " ".join(d.email_rows.shown_problems())
    d.sender.setText("fx@lib.example")
    d.host.setText("smtp.lib.example")
    d.user.setText("mailer")
    d.password.setText(MAIL_SECRET)
    d.use_hook.setChecked(True)
    d.kind.setCurrentIndex(d.kind.findData("teams"))
    d.url.setText("http://hooks.example.org/plain")
    click(qtbot, d.save_button)
    assert "isn't allowed" in " ".join(d.hook_rows.shown_problems()) and d.url.text() == "" and d.password.text() == ""
    d.password.setText(MAIL_SECRET)
    d.url.setText(HOOK)
    click(qtbot, d.save_button)
    assert d.password.text() == "" and d.url.text() == ""
    qtbot.waitUntil(lambda: w.alerts_dialog is None, timeout=LONG)
    settled(qtbot, w)
    assert keyring[("folio-exchange/smtp", "mailer@smtp.lib.example")] == MAIL_SECRET
    assert keyring[("folio-exchange/webhook", "site")] == HOOK
    raw = read_yaml(site / "site.yaml")["alerts"]
    assert raw["on"] == "failure" and raw["webhook"] == "keyring" and raw["email"]["secret"] == "keyring"
    assert page.alerts_state.text() == "Alert settings saved. Send a test alert to try them."
    assert "stored in Windows Credential Manager" in page.alerts.values["Chat webhook"].text()
    for secret in (MAIL_SECRET, HOOK):
        nowhere(w, secret, caplog, site)
    sent = []
    monkeypatch.setattr(runner, "alert", lambda s, feed, status, text, force=False: sent.append((status, force)) or
                        ["email", "webhook"])
    click(qtbot, page.test_button)
    settled(qtbot, w)
    assert sent == [("test", True)] and page.test_result.text() == "Sent by email and webhook. Check that it arrived."


# ------------------------------------------------------------ the setup views, for keyboards and screen readers
def mnemonics(widget) -> list[str]:
    """The letters of every mnemonic in ``widget``: its buttons' and its captions' (labels with a buddy)."""
    out = []
    for text_ in [b.text() for b in widget.findChildren(QAbstractButton)] + [
            label.text() for label in widget.findChildren(QLabel) if label.buddy() is not None]:
        t = text_.replace("&&", "")
        if "&" in t:
            out.append(t[t.index("&") + 1].lower())
    return out


def test_the_setup_views_and_dialogs_are_named_and_their_keys_never_clash(qtbot, site, server_keys):
    w = window(qtbot, site)
    st = {"version": "v", "delivery": {"transport": "none"}, "filters": {}, "checks": {}, "legacy": {}}
    dialogs = [SettingsDialog("chq", st, None, w), DeliveryDialog("chq", st, w),
               AlertsDialog({"on": "failure", "keyring": True}, w)]
    for d in dialogs:
        qtbot.addWidget(d)
    for widget in [w.setup, w.connection, w.wizard, w.recipes, w.options, w.feed, w.home, *dialogs]:
        letters = mnemonics(widget)
        assert len(letters) == len(set(letters)), (type(widget).__name__, sorted(letters))
        if not isinstance(widget, QDialog):
            assert "s" not in letters, type(widget).__name__               # the Site menu's
        for kind in (QAbstractButton, QListWidget, QLineEdit, QComboBox, QSpinBox, QTableWidget, QPlainTextEdit):
            for control in widget.findChildren(kind):
                if not control.objectName().startswith("qt_"):
                    assert control.accessibleName(), (type(widget).__name__, control)
        for box in widget.findChildren(QLineEdit):
            if box.echoMode() == QLineEdit.EchoMode.Password:
                assert box.contextMenuPolicy() == Qt.ContextMenuPolicy.NoContextMenu and not box.dragEnabled()
        for label in widget.findChildren(QLabel):
            if label.textFormat() != Qt.TextFormat.RichText:
                assert label.textFormat() == Qt.TextFormat.PlainText, label.text()   # nothing read as markup
    for d in dialogs:
        d.close()


def test_every_buttons_accessible_name_holds_its_label(qtbot, site, server_keys):
    """Label in name (WCAG 2.5.3). Regression: names such as "Change where this feed's files go" for "Change
    delivery…" did not hold the words on the button, so voice control could not find it by what it shows."""
    from folio_exchange.desktop.picker import NewSiteDialog, SitePicker
    from folio_exchange.desktop.release import ReleaseDialog

    w = window(qtbot, site)
    w.select("recipes")
    qtbot.waitUntil(lambda: bool(w.recipes.buttons), timeout=LONG)        # each source's check button is drawn
    st = {"version": "v", "delivery": {"transport": "none"}, "filters": {}, "checks": {}, "legacy": {}}
    dialogs = [SettingsDialog("chq", st, None, w), DeliveryDialog("chq", st, w),
               AlertsDialog({"on": "failure", "keyring": True}, w),
               ReleaseDialog({"feed": "chq", "files": [["a.txt", 1]], "items": 1, "bibs": 1}, w),
               SitePicker(w), NewSiteDialog(w)]
    for d in dialogs:
        qtbot.addWidget(d)
    checked = 0
    for widget in [w, *dialogs]:
        for b in widget.findChildren(QPushButton):
            if b.objectName().startswith("qt_") or not b.text():
                continue
            label = b.text().replace("&&", "\0").replace("&", "").replace("\0", "&").rstrip("…").strip()
            name = QAccessible.queryAccessibleInterface(b).text(QAccessible.Text.Name)
            assert label.lower() in name.lower(), (type(widget).__name__, b.text(), name)
            checked += 1
    assert checked > 40
    for d in dialogs:
        d.close()


def test_a_refresh_keeps_what_is_being_typed_in_the_wizard(qtbot, site):
    """Regression: the minute's refresh started the wizard's step again, putting back the name as it was."""
    w = window(qtbot, site)
    w.select("add")
    wz = w.wizard
    qtbot.waitUntil(lambda: wz.recipe_list.count() > 0, timeout=LONG)
    click(qtbot, wz.next_button)
    wz.name_edit.setText("half-typed")
    w.refresh()
    qtbot.waitUntil(lambda: not w.reader.busy, timeout=LONG)
    assert w.stack.currentWidget() is wz and wz.step() == 1 and wz.name_edit.text() == "half-typed"


def test_a_folder_in_onedrive_is_warned_about_and_has_no_password_row_without_a_keyring(qtbot, tmp_path):
    root = tmp_path / "OneDrive - Lakeside" / "lib"
    root.mkdir(parents=True)
    w = window(qtbot, root)
    page = w.setup
    assert page.synced.isVisibleTo(page) and "OneDrive or Dropbox" in page.synced.text()
    assert page.form.no_keyring.isVisibleTo(page) and "FOLIO_PASSWORD" in page.form.no_keyring.text()
    assert not page.form.password.isVisibleTo(page) and not page.form.password_caption.isVisibleTo(page)


def test_the_change_connection_form_follows_the_buttons_in_the_tab_order(qtbot, site):
    """The form is made after the Connection page; its fields still come straight after the buttons for Tab."""
    w = window(qtbot, site)
    w.activateWindow()
    qtbot.waitUntil(w.isActiveWindow, timeout=5000)
    w.select("connection")
    click(qtbot, w.connection.change_button)
    w.connection.refused("Another action is still running.")         # the check's line, below the form, has text
    w.connection.check_button.setFocus()
    qtbot.waitUntil(w.connection.check_button.hasFocus, timeout=5000)
    qtbot.keyClick(w.connection.check_button, Qt.Key.Key_Tab)
    assert w.connect_form.url.hasFocus()


# ------------------------------------------------------------ regressions from the D3 review
@pytest.fixture
def hold(monkeypatch):
    """``hold(owner, "name")`` makes owner.name wait (in the thread that calls it) until the returned event is set,
    then do as before. Every event is set when the test ends, so no thread waits on into the next test."""
    events: list[threading.Event] = []

    def make(owner, name: str) -> threading.Event:
        go = threading.Event()
        events.append(go)
        real = getattr(owner, name)

        def wait_then(*a, **k):
            if not go.wait(20):
                raise FxError("not let go")
            return real(*a, **k)

        monkeypatch.setattr(owner, name, wait_then)
        return go

    yield make
    for go in events:
        go.set()


def folio_down(*_a, **_k):
    raise FxError("FOLIO did not answer")


def outside_drop(site) -> None:
    """The feed delivers outside the site folder, so the folder can be renamed (no setting names it in full)."""
    make_feed(site, delivery={"transport": "local", "directory": str(site.parent / "vendor-drop")})


def test_reading_folios_lists_again_keeps_the_codes_typed(qtbot, site, monkeypatch, hold):
    """Regression: "Read the lists again" drew the step again from what it held before, so codes and a note type
    typed while FOLIO's lists were missing were lost without a word, whatever the answer (the page keeps them)."""
    real = setupops.tenant_choices
    monkeypatch.setattr(setupops, "tenant_choices", folio_down)
    w = window(qtbot, site)
    w.select("add")
    wz = w.wizard
    qtbot.waitUntil(lambda: wz.recipe_list.count() > 0, timeout=LONG)
    click(qtbot, wz.next_button)
    wz.name_edit.setText("again")
    click(qtbot, wz.next_button)
    qtbot.waitUntil(lambda: wz.choices is not None and wz.choices_button.isVisibleTo(wz), timeout=LONG)
    wz.loc.codes.setText("ONLINE, MAIN-REF")
    wz.legacy.boxes["created_date_note_type"].setEditText("Old created")
    click(qtbot, wz.choices_button)                                    # FOLIO still does not answer
    qtbot.waitUntil(lambda: not wz.reading and wz.choices_button.isVisibleTo(wz), timeout=LONG)
    assert wz.loc.codes.text() == "ONLINE, MAIN-REF" and wz.legacy.value()["created_date_note_type"] == "Old created"
    monkeypatch.setattr(setupops, "tenant_choices", real)
    go = hold(setupops, "tenant_choices")
    click(qtbot, wz.choices_button)                                    # now it does, after a moment
    assert wz.reading and wz.choices_state.text() == "Reading locations and note types from FOLIO…"
    assert not wz.next_button.isEnabled() and not wz.choices_button.isVisibleTo(wz)
    wz.status.codes.setText("Withdrawn")                               # typed while the lists are read: kept too
    go.set()
    qtbot.waitUntil(lambda: not wz.loc.typed, timeout=LONG)
    assert sorted(wz.loc.value()) == ["MAIN-REF", "ONLINE"] and wz.status.value() == ["Withdrawn"]
    assert wz.legacy.value()["created_date_note_type"] == "Old created"
    click(qtbot, wz.next_button)
    assert sorted(wz.W["exclude_location_codes"]) == ["MAIN-REF", "ONLINE"]


def test_a_changed_connection_reads_folios_lists_again(qtbot, site, monkeypatch):
    """Regression: the window kept the first tenant's locations and statuses for its whole life, so after the
    connection was changed the wizard and the settings form still offered the old tenant's codes."""
    calls = []
    real = setupops.tenant_choices
    monkeypatch.setattr(setupops, "tenant_choices", lambda s: calls.append(1) or real(s))
    w = window(qtbot, site)
    d = open_settings(qtbot, w)
    d.reject()
    qtbot.waitUntil(lambda: w.settings_dialog is None, timeout=5000)
    d = open_settings(qtbot, w)                                        # read once while the window is open
    d.reject()
    qtbot.waitUntil(lambda: w.settings_dialog is None, timeout=5000)
    assert len(calls) == 1
    w.select("connection")
    click(qtbot, w.connection.change_button)
    w.connect_form.tenant.setText("diku")
    click(qtbot, w.connect_form.save_button)
    qtbot.waitUntil(lambda: w.connection.notice.isVisible(), timeout=LONG)
    assert w.choices is None and w.wizard.choices is None
    open_settings(qtbot, w)
    assert len(calls) == 2                                             # asked of FOLIO again


def test_a_refused_change_of_connection_is_said_after_leaving_the_view(qtbot, site, keyring, hold):  # noqa: F811
    """Regression: a refusal that came back after the person left Connection went to the hidden form, and the
    next opening of the form wiped it: they never learned the new password was not saved."""
    before = (site / "site.yaml").read_bytes()
    go = hold(setupops, "connect")
    w = window(qtbot, site)
    w.select("connection")
    page, form = w.connection, w.connect_form
    click(qtbot, page.change_button)
    form.password.setText("not-what-folio-takes")
    click(qtbot, form.save_button)
    assert form.password.text() == "" and w.jobs.running() is not None
    w.select("home")                                                   # FOLIO has not answered yet
    go.set()
    settled(qtbot, w)
    assert (site / "site.yaml").read_bytes() == before and keyring == {}    # the stored password stays as it was
    w.select("connection")
    assert page.notice.isVisibleTo(page)
    assert page.notice.text().startswith("The connection wasn't changed. Could not log in")
    click(qtbot, page.change_button)
    assert page.notice.isHidden() and form.status.isHidden() and form.problem.isHidden()


def test_a_check_keeps_the_focus_and_cannot_undo_an_install_under_way(qtbot, site, monkeypatch, hold, heard):
    """Regressions: pressing Check for updates left the focus on nothing (the button pressed was redrawn away, its
    new one disabled); and Check again stayed enabled during an install, whose outcome the check then replaced."""
    gh = FakeGitHub({"recipes/chq-academic/1.0.0.yaml": (sources.BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml")
                     .read_bytes().replace(b"\r\n", b"\n"), "recipes/chq-academic/1.1.0.yaml": newer()})
    monkeypatch.setattr(sources, "_session", lambda: gh)
    w = window(qtbot, site)
    w.activateWindow()
    qtbot.waitUntil(w.isActiveWindow, timeout=5000)
    w.select("recipes")
    page = w.recipes
    qtbot.waitUntil(lambda: page.data is not None, timeout=LONG)
    go_check = hold(sources, "check")
    check = page.buttons["foliohq"]["check"]
    check.setFocus()
    qtbot.keyClick(check, Qt.Key.Key_Space)
    checking = page.buttons["foliohq"]["review"]
    assert checking.text() == "Checking GitHub…" and focused(checking) and heard[-1] == "Checking GitHub…"
    go_check.set()
    qtbot.waitUntil(lambda: "install" in page.buttons.get("foliohq", {}), timeout=LONG)
    go_install = hold(sources, "install")
    click(qtbot, page.buttons["foliohq"]["install"])
    assert page.checks["foliohq"].get("installing") and not page.buttons["foliohq"]["check"].isEnabled()
    page._check("foliohq")                                             # pressed anyway: nothing happens
    assert page.checks["foliohq"].get("installing") and "running" not in page.checks["foliohq"]
    go_install.set()
    qtbot.waitUntil(lambda: (page.checks.get("foliohq") or {}).get("installed") is not None, timeout=LONG)
    settled(qtbot, w)
    assert page.checks["foliohq"]["installed"] and (site / "recipes" / "chq-academic" / "1.1.0.yaml").exists()


def test_the_settings_form_cannot_be_changed_while_the_file_is_read_again(qtbot, site, hold):
    """Regression: while "Apply my changes to the updated file" read the file, the form could be edited, and
    the answer then laid the earlier changes over the file, throwing the newer edits away."""
    from folio_exchange.desktop import actions

    w = window(qtbot, site)
    d = open_settings(qtbot, w)
    d.title_edit.setText("Mine")
    path = site / "feeds" / "chq.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("Sun 02:15", "Sun 04:45"), encoding="utf-8")
    click(qtbot, d.review_button)
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: d.review_title.text() == "Not saved because the file changed", timeout=LONG)
    go = hold(actions, "_load")
    click(qtbot, d.apply_button)
    assert not d.back_button.isEnabled() and not d.apply_button.isEnabled()
    assert d.review_note.text() == "Reading the updated file…"
    d.reject()                                                         # Escape waits for it too
    assert w.settings_dialog is d and d.isVisible()
    go.set()
    qtbot.waitUntil(lambda: d.review_title.text() == "Review changes", timeout=LONG)
    assert d.back_button.isEnabled() and d.save_button.isEnabled() and d.title_edit.text() == "Mine"


def test_a_key_saved_after_moving_to_another_feed_is_not_shown_on_that_feed(qtbot, site, keyring, server_keys,  # noqa: F811
                                                                          tmp_path, hold):
    """Regression: the answer went to the one login box whatever feed it showed by then, so another feed's
    Delivery tab said "Key saved. Its public fingerprint is …" for a key it does not have."""
    make_feed(site, "other", file_prefix="other", delivery={"transport": "local", "directory": str(site / "o")})
    chosen = tmp_path / "vendor_key"
    chosen.write_text(_ed25519_openssh(PASSPHRASE.encode()), encoding="utf-8")
    w = window(qtbot, site)
    with_sftp(qtbot, w, site)
    login = w.feed.login
    login.use_key.setChecked(True)
    login.ask_key_file = lambda: str(chosen)
    click(qtbot, login.choose_button)
    login.passphrase.setText(PASSPHRASE)
    go = hold(setupops, "store_private_key")
    click(qtbot, login.key_button)
    w.select("feed:other")
    go.set()
    settled(qtbot, w)
    assert w.feed.name == "other" and login.key_saved.isHidden() and login.problem.isHidden()
    assert w.feed.notice.text().startswith("Key saved for chq, which logs in with it from now on.")
    assert (site / "keys" / "chq.key").exists()


def test_a_rename_waits_for_a_pause_being_written(qtbot, site, monkeypatch, hold):
    """Regression: a rename could start between a pause rewriting the feed file and recording it in the activity
    log, which then failed on the old folder (the window's closing waits for it; the rename did not)."""
    outside_drop(site)
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")
    w = window(qtbot, site)
    w.select("options")
    go = hold(views, "set_enabled")
    w.set_enabled("chq", False)
    assert w.toggling and not w.options.rename_button.isEnabled()
    w.rename_site("Elsewhere")
    assert "Wait for the pause or resume to be saved" in " ".join(w.options.rows.shown_problems())
    assert w.renaming is None and site.is_dir() and not (site.parent / "Elsewhere").exists()
    go.set()
    qtbot.waitUntil(lambda: not w.toggling and w.options.rename_button.isEnabled(), timeout=LONG)
    assert read_yaml(site / "feeds" / "chq.yaml")["enabled"] is False


def test_a_rename_that_fails_after_the_move_still_follows_the_folder(qtbot, site, monkeypatch):
    """Regression: when a step after the move failed (here the alert secrets' copy), the window stayed on the old
    folder, which no longer exists, until the app was started again."""
    outside_drop(site)
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")

    def broken(*_a, **_k):
        raise RuntimeError("the keyring backend stopped answering")

    monkeypatch.setattr(siteops, "_carry_alert_secrets", broken)
    w = window(qtbot, site)
    w.select("options")
    w.rename_site("Moved")
    new = site.parent / "Moved"
    qtbot.waitUntil(lambda: w.renaming is None, timeout=LONG)
    settled(qtbot, w)
    assert new.is_dir() and not site.exists()
    assert w.site_root == new.resolve() and w.page.site_root == new.resolve()
    assert places.recent_sites()[0] == new.resolve() and w.windowTitle() == "FOLIO-Exchange – Moved"
    assert w.options.done.text() == ("Renamed to Moved, but a later step failed: RuntimeError: the keyring backend "
                                     "stopped answering")
    assert w.snap["summary"]["name"] == "Moved"                        # the site is read from its new folder


def test_closing_during_a_rename_waits_until_the_window_has_followed_the_folder(qtbot, site, monkeypatch, hold):
    """Regression: the window closed as soon as the rename's job ended, which is a moment before it hears how the
    rename went, so the recent sites kept the old folder."""
    import contextlib

    outside_drop(site)
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")
    w = window(qtbot, site)
    w.ask_close_while_running = lambda job: "later"
    released = threading.Event()
    real_holding = w.jobs.holding

    @contextlib.contextmanager
    def holding(kind, refuse=None):
        with real_holding(kind, refuse) as job:
            yield job
        released.set()

    monkeypatch.setattr(w.jobs, "holding", holding)
    go = hold(siteops, "rename_site")
    w.rename_site("Moved")
    qtbot.waitUntil(lambda: w.jobs.running() is not None, timeout=LONG)
    w.close()
    assert w.closing_after_job and not w.quitting
    go.set()
    assert released.wait(20)                       # the job has ended; the window has not heard back (no events run)
    w.sync_busy()                                  # the half-second look, in that gap
    assert not w.quitting
    qtbot.waitUntil(lambda: w.quitting, timeout=LONG)
    assert places.recent_sites()[0] == (site.parent / "Moved").resolve()


def test_after_a_rename_task_scheduler_is_asked_again(qtbot, site, monkeypatch):
    """Regression: Options kept saying Task Scheduler has the task named after the old folder."""
    outside_drop(site)
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")          # rename_site asks no schtasks
    monkeypatch.setattr(siteops, "task_registered", lambda root, run=None: Path(root).name == "site")
    w = window(qtbot, site)
    w.options.set_platform(windows=True)
    w.select("options")
    qtbot.waitUntil(lambda: w.options.registered.text().startswith("Task Scheduler has the task"), timeout=LONG)
    w.rename_site("Renamed")
    qtbot.waitUntil(lambda: w.options.registered.text().startswith("Task Scheduler doesn't have this site's task"),
                    timeout=LONG)


def test_a_recipes_codes_are_never_read_as_markup_in_a_tooltip(qtbot, site):
    """Regression: the wizard's caption tooltips held the recipe's default codes as they were, and Qt reads a
    tooltip that looks like markup as markup (an image from a share, say)."""
    w = window(qtbot, site)
    wz = w.wizard
    code = '<img src="file://attacker.example/share/x.png">'
    wz.W.update(recipe={"selection": {"exclude_location_codes": [code]}}, step=2)
    wz.choices = {"data": {"locations": [], "statuses": [], "note_types": []}}
    wz._fill_records()
    doc = QTextDocument()
    doc.setHtml(wz.loc_caption.toolTip())
    assert code in doc.toPlainText()                                   # shown as written, not as an image


def test_without_folios_lists_the_settings_form_says_how_to_type_codes(qtbot, site, monkeypatch):
    """Regression: the settings form said nothing about the lists being unavailable, nor how to separate codes."""
    monkeypatch.setattr(setupops, "tenant_choices", folio_down)
    w = window(qtbot, site)
    d = open_settings(qtbot, w)
    assert d.lists_note.isVisibleTo(d) and d.lists_note.text().endswith("Separate them with commas.")
    assert "FOLIO did not answer" in d.lists_note.text() and d.loc.typed and d.loc.commas.isVisibleTo(d)
    assert d.loc.codes.accessibleDescription() == "Items in these locations aren't sent. Separate them with commas."


# ------------------------------------------------------------ failures and refusals, each said and changing nothing
def test_a_rename_the_core_refuses_changes_nothing_and_says_why(qtbot, site, monkeypatch):
    outside_drop(site)
    (site.parent / "Taken").mkdir()
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")
    w = window(qtbot, site)
    w.select("options")
    w.options.name.setText("Taken")
    click(qtbot, w.options.rename_button)
    qtbot.waitUntil(lambda: w.renaming is None and bool(w.options.rows.shown_problems()), timeout=LONG)
    assert "already exists. Choose another name." in " ".join(w.options.rows.shown_problems())
    assert site.is_dir() and w.site_root == site.resolve() and w.options.done.isHidden()


def test_a_console_that_cannot_start_is_said(qtbot, site, monkeypatch):
    monkeypatch.setattr(siteops, "task_registered", lambda root, run=None: False)
    w = window(qtbot, site)
    w.options.set_platform(windows=True)

    def cannot(exe, args, folder):
        raise OSError("no console here")

    w.start_console = cannot
    w.select("options")
    click(qtbot, w.options.register_button)
    qtbot.waitUntil(lambda: not w.registering and bool(w.options.register_result.text()), timeout=LONG)
    assert w.options.register_result.text() == "Couldn't start the console window: OSError: no console here"


def test_a_test_alert_that_is_not_sent_says_why(qtbot, site, monkeypatch):
    w = window(qtbot, site)
    w.select("options")
    monkeypatch.setattr(runner, "alert", lambda *a, **k: [])
    click(qtbot, w.options.test_button)
    settled(qtbot, w)
    assert w.options.test_result.text().startswith("The test alert wasn't sent. No alert channel is set up")

    def broken(*_a, **_k):
        raise FxError("the mail server refused the login")

    monkeypatch.setattr(runner, "alert", broken)
    click(qtbot, w.options.test_button)
    settled(qtbot, w)
    assert w.options.test_result.text() == "Couldn't send the test alert: the mail server refused the login"


def test_an_install_that_fails_installs_nothing_and_says_why(qtbot, site, monkeypatch):
    gh = FakeGitHub({"recipes/chq-academic/1.0.0.yaml": (sources.BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml")
                     .read_bytes().replace(b"\r\n", b"\n"), "recipes/chq-academic/1.1.0.yaml": newer()})
    monkeypatch.setattr(sources, "_session", lambda: gh)
    w = window(qtbot, site)
    w.select("recipes")
    page = w.recipes
    qtbot.waitUntil(lambda: page.data is not None, timeout=LONG)
    click(qtbot, page.buttons["foliohq"]["check"])
    qtbot.waitUntil(lambda: "install" in page.buttons.get("foliohq", {}), timeout=LONG)

    def broken(*_a, **_k):
        raise FxError("The download was cut off.")

    monkeypatch.setattr(sources, "install", broken)
    click(qtbot, page.buttons["foliohq"]["install"])
    settled(qtbot, w)
    assert "Nothing was installed. The download was cut off." in " ".join(shown_text(page))
    assert not (site / "recipes" / "chq-academic" / "1.1.0.yaml").exists()
    qtbot.waitUntil(lambda: page.buttons["foliohq"]["install"].isEnabled(), timeout=5000)     # it can be tried again


def test_alerts_the_core_refuses_store_nothing_and_say_why(qtbot, site, keyring, heard):  # noqa: F811
    w = window(qtbot, site)
    w.select("options")
    qtbot.waitUntil(lambda: "When" in w.options.alerts.values, timeout=LONG)
    click(qtbot, w.options.alerts_button)
    qtbot.waitUntil(lambda: w.alerts_dialog is not None, timeout=LONG)
    d = w.alerts_dialog
    d.use_hook.setChecked(True)
    d.url.setText(HOOK)
    path = site / "site.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\n# edited by hand meanwhile\n", encoding="utf-8")
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: d.problem.isVisible(), timeout=LONG)
    assert d.problem.text().startswith("Not saved. site.yaml changed since this form was opened")
    assert heard[-1] == d.problem.text() and keyring == {} and d.url.text() == ""
    assert "webhook" not in (read_yaml(path).get("alerts") or {})


def test_a_delivery_set_back_to_not_yet_keeps_the_feed_a_dry_run(qtbot, site):
    w = window(qtbot, site)
    d = open_delivery(qtbot, w)
    d.radios["none"].setChecked(True)
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: w.delivery_dialog is None, timeout=LONG)
    settled(qtbot, w)
    assert w.feed.notice.text() == "Saved. The feed is a dry run."
    assert read_yaml(site / "feeds" / "chq.yaml")["delivery"]["transport"] == "none"


def test_a_permission_check_that_cannot_run_says_why(qtbot, site, monkeypatch):
    monkeypatch.setattr(setupops, "recipe_permissions", folio_down)
    w = window(qtbot, site)
    w.select("add")
    wz = w.wizard
    qtbot.waitUntil(lambda: wz.recipe_list.count() > 0, timeout=LONG)
    click(qtbot, wz.next_button)
    qtbot.waitUntil(lambda: wz.perm_state.text().startswith("The check couldn't run"), timeout=LONG)
    assert wz.perm_state.text() == "The check couldn't run: FOLIO did not answer" and wz.perm_button.isEnabled()


def test_saving_the_delivery_again_says_when_the_file_cannot_be_read(qtbot, site, server_keys, monkeypatch, heard):
    from folio_exchange.desktop import actions

    w = window(qtbot, site)
    d = open_delivery(qtbot, w)
    sftp(d, KEYS["sha256"])
    path = site / "feeds" / "chq.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("Sun 02:15", "Sun 05:00"), encoding="utf-8")
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: d.again_button.isVisibleTo(d), timeout=LONG)

    def broken(_root):
        raise FxError("feeds/chq.yaml could not be read")

    monkeypatch.setattr(actions, "_load", broken)
    click(qtbot, d.again_button)
    qtbot.waitUntil(lambda: d.problem.text().startswith("Not saved. feeds/chq.yaml could not be read"), timeout=LONG)
    assert heard[-1] == d.problem.text() and w.delivery_dialog is d and d.host.text() == "sftp.vendor.example"
    assert read_yaml(path)["delivery"]["transport"] == "local"


def test_a_thread_left_running_fails_the_test_that_left_it(qtbot):
    """Regression: drain gave up quietly after 30 s and let the window close with an errand still running, which
    then reached into the next test."""
    stuck = types.SimpleNamespace(errands=types.SimpleNamespace(pending=1),
                                  jobs=types.SimpleNamespace(running=lambda: None))
    with pytest.raises(qtbot.TimeoutError):
        drain(qtbot, stuck, timeout=200)


def test_codes_typed_while_folios_lists_are_first_read_are_kept(qtbot, site, hold):
    """Regression: while the lists were read the first time, step 3 showed fields for codes (focused, so a
    keyboard user typed into them), and the answer drew the step again from what it held before, losing them."""
    go = hold(setupops, "tenant_choices")
    w = window(qtbot, site)
    w.select("add")
    wz = w.wizard
    qtbot.waitUntil(lambda: wz.recipe_list.count() > 0, timeout=LONG)
    click(qtbot, wz.next_button)
    wz.name_edit.setText("first")
    click(qtbot, wz.next_button)
    assert wz.step() == 2 and wz.choices is None and wz.loc.typed and wz.loc.codes.isVisibleTo(wz)
    wz.loc.codes.setText("ONLINE")
    go.set()
    qtbot.waitUntil(lambda: not wz.loc.typed, timeout=LONG)
    assert wz.loc.value() == ["ONLINE"]


def test_lists_the_window_has_already_never_replace_the_recipes_defaults(qtbot, site, monkeypatch):
    """Lists read before (for a settings form) come back at once when step 3 opens: the step is drawn from the
    recipe's defaults first, so keeping it before drawing the lists keeps those defaults (not an empty form)."""
    asked = []
    monkeypatch.setattr(setupops, "tenant_choices", lambda s: asked.append(s) or {})
    w = window(qtbot, site)
    w.choices = {"data": {"locations": [{"code": "ONLINE", "name": "Online"}], "statuses": ["Withdrawn", "Lost"],
                          "note_types": []}}
    w.select("add")
    wz = w.wizard
    qtbot.waitUntil(lambda: wz.recipe_list.count() > 0, timeout=LONG)
    click(qtbot, wz.next_button)
    wz.W["exclude_item_statuses"] = ["Withdrawn"]                     # as a recipe's own default would set it
    wz.name_edit.setText("cached")
    click(qtbot, wz.next_button)
    assert wz.step() == 2 and not wz.status.typed and wz.status.value() == ["Withdrawn"]
    assert asked == []                                                 # FOLIO was not asked again


def test_nothing_else_starts_while_a_rename_waits_for_a_reading(qtbot, site, monkeypatch):
    """Regression: while a rename waited for a reading of the site, Pause stayed enabled, so a pause could begin
    and the rename then start in the middle of it (cutting off its entry in the activity log)."""
    outside_drop(site)
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")
    w = window(qtbot, site)
    w.select("feed:chq")
    w.refresh()
    assert w.reader.busy
    w.rename_site("Waited")
    assert w.renaming == "Waited" and not w.renaming_started
    assert not w.feed.pause_button.isEnabled() and not w.feed.preview_button.isEnabled()
    w.set_enabled("chq", False)                                        # the keyboard, or a click already queued
    assert not w.toggling and "being renamed" in w.feed.notice.text()
    qtbot.waitUntil(lambda: w.renaming is None, timeout=LONG)
    settled(qtbot, w)
    new = site.parent / "Waited"
    assert new.is_dir() and read_yaml(new / "feeds" / "chq.yaml").get("enabled", True) is not False
    qtbot.waitUntil(lambda: w.feed.pause_button.isEnabled(), timeout=5000)
