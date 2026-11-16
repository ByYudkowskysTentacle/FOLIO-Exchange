"""Telling about a newer version (phase U0 of docs/updates-plan.md), run offscreen with pytest-qt: the Updates
section under Options, the tray's one notification per version, and opening Options from it. GitHub is faked
(test_updates' FakeGitHub); no real tray icon or process."""
from __future__ import annotations

import uuid

import pytest
from conftest import QT_LOADS

if not QT_LOADS:
    pytest.skip("needs the desktop extra (PySide6) and pytest-qt, and a Qt that loads", allow_module_level=True)

import requests  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QAbstractButton, QApplication  # noqa: E402
from test_desktop_setup import LONG, click, window  # noqa: E402
from test_desktop_tray import tick  # noqa: E402
from test_updates import FakeGitHub, answer, release  # noqa: E402

from folio_exchange import updates  # noqa: E402
from folio_exchange.desktop import _parser, options, places, single, startup  # noqa: E402
from folio_exchange.desktop import app as desktop_app  # noqa: E402
from folio_exchange.desktop.tray import Tray  # noqa: E402

NOTES = "Fixes a delivery bug.\n<b>Bold</b> & <img src='http://example.org/x.png'>"


@pytest.fixture
def github(monkeypatch):
    """Checking turned on (conftest turns it off), as version 0.2.0, against a GitHub that lists 0.3.0."""
    monkeypatch.delenv(updates.ENV, raising=False)
    monkeypatch.setattr(updates, "__version__", "0.2.0")
    gh = FakeGitHub(answer(data=[release("v0.3.0", prerelease=True, body=NOTES), release("v0.2.0")]))
    monkeypatch.setattr(updates, "_session", lambda: gh)
    return gh


def options_page(qtbot, site):
    w = window(qtbot, site)
    qtbot.waitUntil(lambda: not w.update_reader.busy, timeout=LONG)
    w.select("options")
    return w, w.options


def seen(widget) -> bool:
    return widget.isVisibleTo(widget.window())


def facts(page) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [label.text() for label in page.update_facts.findChildren(QLabel)]


# ------------------------------------------------------------------ Options
def test_options_tells_about_a_newer_version_and_how_to_get_it(qtbot, site, github):
    w, page = options_page(qtbot, site)
    assert len(github.calls) == 1                                  # the window checked when it opened
    assert any("0.2.0" in t for t in facts(page)) and not any("Not yet" in t for t in facts(page))
    assert seen(page.update_available) and page.update_available.text() == "FOLIO-Exchange 0.3.0 is available."
    assert page.update_notes.text() == NOTES                       # the release's text as written
    assert page.update_notes.textFormat() == Qt.TextFormat.PlainText    # never read as markup
    command = page.update_line.text()
    assert seen(page.update_line) and seen(page.update_how)
    assert command.endswith('-m pip install "folio-exchange[desktop] @ https://github.com/ByYudkowskysTentacle/'
                            'FOLIO-Exchange/archive/refs/tags/v0.3.0.zip"')
    assert "git+" not in command                                   # the tag's zip: the PC needs no Git
    assert page.update_how.text() == ("To update, close FOLIO-Exchange and its tray icon. Then run this command "
                                      "in Command Prompt:")        # PowerShell refuses a quoted path first
    assert "pythonw" not in command.lower() and "pip install folio-exchange" not in command
    click(qtbot, page.copy_button)
    assert QApplication.clipboard().text() == command and page.update_result.text() == "Copied the command."
    click(qtbot, page.release_button)
    assert w.opened == ["https://github.com/ByYudkowskysTentacle/FOLIO-Exchange/releases/tag/v0.3.0"]
    w.select("home")
    w.select("options")
    qtbot.waitUntil(lambda: not w.update_reader.busy, timeout=LONG)
    assert len(github.calls) == 1                                  # not again within the day
    assert not seen(page.install_button)                           # this test run isn't an install that can update


def test_an_install_the_installer_made_is_told_to_run_the_installer_again(qtbot, site, github, monkeypatch, tmp_path):
    """Regression (review of #37): every library PC is set up by installer/install.ps1 and has no Git, and the
    pip command offered there needed it."""
    program = tmp_path / "FOLIO-Exchange" / "program"
    program.mkdir(parents=True)
    monkeypatch.setattr(options.sys, "prefix", str(program))
    assert not options.installer_install()                         # a folder called program isn't enough
    (program / "pyvenv.cfg").write_text("home = C:\\Python312\n", encoding="utf-8")
    assert options.installer_install()
    w, page = options_page(qtbot, site)
    assert seen(page.update_available) and seen(page.update_notes) and seen(page.release_button)
    # Regression (re-review of #37): the installer run again follows main, not the release the notice names.
    assert seen(page.update_how) and page.update_how.text() == (
        "To update, close FOLIO-Exchange and its tray icon. Then run install.cmd -Ref v0.3.0 in the installer's "
        "folder, with the same install folder and site name.")
    assert not seen(page.update_line) and not seen(page.copy_button) and page.update_line.text() == ""
    monkeypatch.setattr(options.sys, "prefix", str(tmp_path / "venv"))
    assert not options.installer_install()


def test_check_for_updates_says_when_this_is_the_newest_version(qtbot, site, github):
    github.answers[:] = [answer(data=[release("v0.2.0"), release("v0.1.0")])]
    w, page = options_page(qtbot, site)
    assert not seen(page.update_available) and not seen(page.copy_button) and not seen(page.release_button)
    click(qtbot, page.check_button)
    qtbot.waitUntil(lambda: page.update_result.text() == "You have the newest version.", timeout=LONG)
    assert len(github.calls) == 2 and page.check_button.isEnabled()


def test_a_check_that_fails_is_said_in_place_and_a_later_one_tells(qtbot, site, github, monkeypatch):
    github.answers[:] = [requests.ConnectionError("no route")]
    asked: list = []
    monkeypatch.setattr(options, "tell", lambda label, message: (asked.append(message), label.setText(message),
                                                                 label.show()))
    monkeypatch.setattr("folio_exchange.desktop.pages.alert", lambda *a, **kw: pytest.fail("an error dialog"))
    w, page = options_page(qtbot, site)
    assert any("Not yet. The last check didn't work." in t for t in facts(page))
    assert asked == []                                             # a check nobody asked for fails quietly
    click(qtbot, page.check_button)
    qtbot.waitUntil(lambda: bool(asked), timeout=LONG)
    assert asked == ["Couldn't check for updates. Couldn't reach GitHub (ConnectionError). Check this computer's "
                     "internet connection or proxy settings."]
    github.answers[:] = [answer(data=[release("v0.3.0")])]
    click(qtbot, page.check_button)
    qtbot.waitUntil(lambda: len(asked) == 2, timeout=LONG)
    assert asked[1] == "FOLIO-Exchange 0.3.0 is available." and seen(page.update_available)
    assert not seen(page.update_notes)                             # a release with no text shows none


def test_with_updates_off_options_says_it_manages_them_and_asks_nothing(qtbot, site, github, monkeypatch):
    monkeypatch.setenv(updates.ENV, "off")
    w, page = options_page(qtbot, site)
    assert seen(page.update_managed) and page.update_managed.text() == "Updates on this computer are managed by IT."
    for hidden in (page.check_button, page.prerelease_box, page.automatic_box, page.updates_about,
                   page.update_available, page.copy_button, page.release_button):
        assert not seen(hidden)
    assert any("0.2.0" in t for t in facts(page)) and not any("Last checked" in t for t in facts(page))
    assert github.calls == []


def test_check_false_in_updates_json_is_managed_by_it_too(qtbot, site, github):
    updates._change(check=False)
    w, page = options_page(qtbot, site)
    assert seen(page.update_managed) and "managed by IT" in page.update_managed.text()
    assert not seen(page.check_button) and github.calls == []


def test_notify_tells_but_offers_no_way_to_install(qtbot, site, github, monkeypatch):
    monkeypatch.setenv(updates.ENV, "notify")
    w, page = options_page(qtbot, site)
    assert seen(page.update_available) and seen(page.update_notes) and seen(page.release_button)
    assert seen(page.update_managed) and page.update_managed.text() == "Your IT staff install updates on this computer."
    assert not seen(page.update_line) and not seen(page.copy_button) and not seen(page.update_how)


def test_the_two_choices_are_kept_per_person(qtbot, site, github):
    w, page = options_page(qtbot, site)
    assert page.prerelease_box.isChecked() and page.automatic_box.isChecked()      # below 1.0, on by default
    assert seen(page.prerelease_about)
    tick(qtbot, page.prerelease_box)
    assert updates.load()["prereleases"] is False and not page.prerelease_box.isChecked()
    assert not seen(page.update_available)                         # 0.3.0 is a pre-release
    tick(qtbot, page.prerelease_box)
    assert updates.load()["prereleases"] is True and seen(page.update_available)
    tick(qtbot, page.automatic_box)
    assert updates.load()["automatic"] is False and not updates.due()
    assert len(github.calls) == 1                                  # neither choice asked GitHub again


def test_a_choice_that_cannot_be_saved_is_said_and_stays_as_it_was(qtbot, site, github, monkeypatch):
    w, page = options_page(qtbot, site)

    def refuse(key, on):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(updates, "set_choice", refuse)
    tick(qtbot, page.automatic_box)
    assert page.automatic_box.isChecked() and page.update_result.text().startswith("Couldn't save the setting")


def test_the_updates_section_follows_the_wording_rules(qtbot, site, github):
    w, page = options_page(qtbot, site)
    controls = [page.check_button, page.copy_button, page.release_button, page.prerelease_box, page.automatic_box]
    assert [c.text() for c in controls] == ["Check for &updates", "Cop&y the command", "&Open the release page",
                                            "Get &pre-releases", "Check for updates automati&cally"]
    for c in controls:
        assert c.text().replace("&", "") in c.accessibleName()         # label in name

    def letter(caption: str) -> str | None:
        at = caption.find("&")
        return caption[at + 1].lower() if at >= 0 else None

    letters = [x for x in (letter(b.text()) for b in page.findChildren(QAbstractButton)) if x]
    assert len(letters) == len(set(letters)) and "s" not in letters, letters        # unique, and not the menu's
    for words in (options.UPDATES_ABOUT, options.UPDATES_MANAGED, options.UPDATES_BY_IT, options.UPDATE_HOW,
                  options.UPDATE_INSTALLER,
                  options.PRERELEASE_ABOUT, page.update_available.text()):
        assert "—" not in words and words[0].isupper() and words.rstrip()[-1] in ".:"
    assert "sends nothing about your library" in options.UPDATES_ABOUT          # the privacy fact, said once


def test_long_release_notes_point_to_the_release_page(qtbot, site, github):
    github.answers[:] = [answer(data=[release("v0.3.0", body="word " * 2000)])]
    w, page = options_page(qtbot, site)
    assert len(page.update_notes.text()) < options.NOTES_MOST + 50
    assert page.update_notes.text().endswith("…\n\nThe release page has the rest.")


# ------------------------------------------------------------------ the tray
def make_tray(roots) -> Tray:
    t = Tray(sites=lambda: list(roots), poll_ms=600_000)
    t.told, t.noticed, t.opened, t.quits = [], [], [], []
    t.show_message = lambda title, text: t.told.append((title, text))
    t.show_notice = lambda title, text: t.noticed.append((title, text))
    t.launch = lambda root, view=None: t.opened.append((root, view))
    t.quit_app = lambda: t.quits.append(True)
    return t


def poll(qtbot, t: Tray) -> None:
    before = t.update_looks
    t.refresh()
    qtbot.waitUntil(lambda: t.update_looks > before and not t.update_reader.busy
                    and not any(r.busy for r in t.readers.values()), timeout=LONG)


def test_the_tray_tells_about_each_new_version_once_and_opens_options(qtbot, site, github):
    t = make_tray([site])
    try:
        poll(qtbot, t)
        assert t.noticed == [("FOLIO-Exchange", "FOLIO-Exchange 0.3.0 is available.")]
        t._message_clicked()
        assert t.opened == [(site, "options")]
        poll(qtbot, t)
        assert len(t.noticed) == 1 and len(github.calls) == 1      # told once, asked once
        github.answers[:] = [answer(data=[release("v0.3.1"), release("v0.3.0")])]
        updates._change(last_ok="2026-01-01T00:00:00+00:00")       # a day and more has passed
        poll(qtbot, t)
        assert t.noticed[-1] == ("FOLIO-Exchange", "FOLIO-Exchange 0.3.1 is available.") and len(t.noticed) == 2
        t.tell(site, ["chq is held for your review."])             # a site's news opens the site as before
        t._message_clicked()
        assert t.opened[-1] == (site, None)
    finally:
        t.shut_down()


def test_a_tray_started_later_does_not_tell_again(qtbot, site, github):
    first = make_tray([site])
    second = make_tray([site])
    try:
        poll(qtbot, first)
        poll(qtbot, second)
        assert len(first.noticed) == 1 and second.noticed == []
    finally:
        first.shut_down()
        second.shut_down()


def test_the_tray_tells_what_a_window_found(qtbot, site, github):
    updates.check()                                                # as a window does when it opens
    t = make_tray([site])
    try:
        poll(qtbot, t)
        assert t.noticed == [("FOLIO-Exchange", "FOLIO-Exchange 0.3.0 is available.")] and len(github.calls) == 1
    finally:
        t.shut_down()


def test_the_tray_asks_and_tells_nothing_with_updates_off(qtbot, site, github, monkeypatch):
    monkeypatch.setenv(updates.ENV, "off")
    t = make_tray([site])
    try:
        poll(qtbot, t)
        assert t.noticed == [] and github.calls == []
    finally:
        t.shut_down()


# ------------------------------------------------------------------ opening Options from the notice
def test_the_app_can_be_started_on_options():
    assert _parser().parse_args(["--site", "x", "--view", "options"]).view == "options"
    assert _parser().parse_args(["--site", "x"]).view is None
    exe, args, _folder = startup.app_command(places.config_dir(), "options")
    assert args[-4:] == ["--site", str(places.config_dir()), "--view", "options"]
    assert startup.app_command(places.config_dir())[1][-2:] == ["--site", str(places.config_dir())]


def test_an_open_window_is_asked_for_options(qtbot, site, monkeypatch):
    name = f"fx-test-{uuid.uuid4().hex[:16]}"
    monkeypatch.setattr(places, "instance_name", lambda root: name)
    monkeypatch.setattr(desktop_app, "setup_logging", lambda *a, **kw: None)
    server = single.InstanceServer(name)
    assert server.listen()
    w = window(qtbot, site, instance=server)
    server.shown.connect(w.bring_to_front)
    server.asked.connect(w.open_view)
    assert w.current_key() == "home"
    assert desktop_app.run(str(site), view="options") == 0         # a second start, as the tray's notice makes
    assert w.current_key() == "options"
    assert single.ask_to_show(name, view="nonsense") is False      # only views the app knows
    w.select("home")
    assert single.claim(name, "nonsense") == ("shown", None)       # then just brought to the front
    assert w.current_key() == "home"


def test_a_window_still_reading_its_site_opens_options_once_it_has(qtbot, site):
    from folio_exchange.desktop.window import MainWindow

    w = MainWindow(site, open_url=lambda url: None)
    qtbot.addWidget(w)
    w.open_view("options")                                         # before the first reading has ended
    qtbot.waitUntil(lambda: w.snap is not None and w.current_key() == "options", timeout=LONG)
