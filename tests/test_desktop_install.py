"""Installing an update from the app, run offscreen with pytest-qt: "Install X.Y.Z…" under Options and when it
isn't offered, the question before it, the update as the window's running action with its progress and refusals
in place, "quit for update" over the local sockets for windows and the tray, and what the next start says and
records. GitHub, the engine's install folder and the sockets' other ends are fakes or temporary; the key is a
throwaway one made in memory, put in place only by these tests. Nothing runs against a real install or site."""
# Fixtures are imported from the modules that define them, and test functions take them by name.
# ruff: noqa: F811
from __future__ import annotations

import functools
import json
import logging
import threading
import uuid

import pytest
from conftest import QT_LOADS

if not QT_LOADS:
    pytest.skip("needs the desktop extra (PySide6) and pytest-qt, and a Qt that loads", allow_module_level=True)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QAbstractButton  # noqa: E402
from sitekit import make_site  # noqa: E402
from test_desktop_setup import LONG, click, mnemonics, window  # noqa: E402
from test_desktop_tray import make_tray, trays  # noqa: E402, F401
from test_desktop_updates import NOTES, github, options_page, seen  # noqa: E402, F401
from test_installing import Engine  # noqa: E402
from test_selfupdate import CALM, OLD, Builder, FakeGitHub, Runner, install, key, make_key, release  # noqa: E402, F401

from folio_exchange import __version__, selfupdate, updates  # noqa: E402
from folio_exchange.config import load_site  # noqa: E402
from folio_exchange.desktop import installing, options, places, single, startup  # noqa: E402
from folio_exchange.history import History  # noqa: E402

NEW = "0.3.0"                       # what the faked GitHub lists (test_desktop_updates' github fixture)
UPDATABLE = {"updatable": True, "root": "x", "program": "x/program", "python": "py", "reason": None, "command": None}


@pytest.fixture
def keyed(monkeypatch):
    """A throwaway release key in the code, and an install the engine can update (looked up, never real)."""
    _private, public = make_key()
    monkeypatch.setattr(updates, "RELEASE_KEY", public)
    monkeypatch.setattr(selfupdate, "find_install", lambda **kw: dict(UPDATABLE))
    return public


@pytest.fixture
def name():
    return f"fx-test-{uuid.uuid4().hex[:16]}"


def installing_window(qtbot, site, engine, *, asked=None, yes=True, http=None, **kw):
    """Options, with the question answered ``yes`` and the update run by ``engine``, sockets that find nothing
    open, and no wait or launch for real."""
    w, page = options_page(qtbot, site)
    asked = [] if asked is None else asked
    w.ask_install = lambda release: asked.append(release) or yes
    w.updater = functools.partial(installing.install, engine=engine, probe=lambda n: None, ask_quit=pytest.fail,
                                  parents=dict,
                                  launch=pytest.fail, sleep=lambda s: None, now=lambda: CALM,
                                  http=http or FakeGitHub({}), **kw)
    return w, page


def ended(qtbot, w):
    qtbot.waitUntil(lambda: w.jobs.running() is None and w.watcher is None, timeout=LONG)


# ------------------------------------------------------------------ the offer
def test_an_install_it_can_update_gets_install_instead_of_the_command(qtbot, site, github, keyed):
    w, page = options_page(qtbot, site)
    assert seen(page.install_button) and page.install_button.text() == f"Insta&ll {NEW}…"
    assert page.install_button.accessibleName() == f"Install {NEW}"
    assert seen(page.release_button) and seen(page.update_notes)
    assert not seen(page.update_line) and not seen(page.copy_button) and not seen(page.install_why)


def test_no_release_key_means_no_install_button_and_no_way_to_the_engine(qtbot, site, github, monkeypatch):
    monkeypatch.setattr(selfupdate, "find_install", lambda **kw: dict(UPDATABLE))
    assert updates.RELEASE_KEY is None                             # as shipped, until the maintainer makes the key
    w, page = options_page(qtbot, site)
    assert not seen(page.install_button)
    assert seen(page.install_why) and page.install_why.text() == installing.NO_KEY
    assert seen(page.update_line) and seen(page.copy_button)       # U0's command stays
    w.ask_install = lambda release: pytest.fail("asked to install with no key")
    w.updater = lambda *a, **kw: pytest.fail("the engine was reached with no key")
    page.offered = True                                            # even a stray signal goes nowhere
    w.install_update(NEW)
    assert w.jobs.running() is None


@pytest.mark.parametrize("mode", ["notify", "off"])
def test_notify_and_off_never_offer_to_install(qtbot, site, github, keyed, monkeypatch, mode):
    monkeypatch.setenv(updates.ENV, mode)
    w, page = options_page(qtbot, site)
    assert not seen(page.install_button) and not seen(page.install_why)
    assert seen(page.update_managed)


def test_a_pip_install_gets_the_command_and_why(qtbot, site, github, monkeypatch, tmp_path):
    _private, public = make_key()
    monkeypatch.setattr(updates, "RELEASE_KEY", public)
    pip = selfupdate.find_install(prefix=tmp_path, base_prefix=tmp_path, version=NEW)
    monkeypatch.setattr(selfupdate, "find_install", lambda **kw: pip)
    w, page = options_page(qtbot, site)
    assert not seen(page.install_button)
    assert page.install_why.text() == "FOLIO-Exchange was installed with pip, so it can't update itself."
    assert seen(page.update_how) and seen(page.update_line) and seen(page.copy_button)


def test_the_test_run_itself_is_never_offered_an_install(qtbot, site, github, monkeypatch):
    _private, public = make_key()
    monkeypatch.setattr(updates, "RELEASE_KEY", public)
    w, page = options_page(qtbot, site)                            # the real find_install: this venv isn't one
    assert not seen(page.install_button) and seen(page.install_why)


def test_the_section_follows_the_wording_rules(qtbot, site, github, keyed):
    w, page = options_page(qtbot, site)
    letters = mnemonics(page)
    assert len(letters) == len(set(letters)) and "s" not in letters, letters
    for b in page.findChildren(QAbstractButton):
        if b.text():
            assert b.text().replace("&", "").rstrip("…") in b.accessibleName(), b.text()     # label in name
    for words in (installing.NO_KEY, installing.WHAT_HAPPENS):
        assert "—" not in words and words[0].isupper() and words.rstrip()[-1] == "."


# ------------------------------------------------------------------ the question
def test_the_question_shows_the_version_notes_and_what_happens_with_cancel_the_default(qtbot):
    d = options.UpdateDialog({"version": NEW, "notes": NOTES})
    qtbot.addWidget(d)
    d.show()
    assert d.windowTitle() == f"FOLIO-Exchange – install {NEW}?"
    assert d.notes.toPlainText() == NOTES and d.notes.isReadOnly()        # plain text: never read as markup
    assert d.happens.text() == ("FOLIO-Exchange closes, updates and opens again. Your sites, feeds and passwords "
                                "stay as they are.")
    assert d.cancel_button.isDefault() and not d.install_button.isDefault()
    assert d.install_button.text() == "&Install" and d.install_button.accessibleName() == f"Install {NEW}"
    qtbot.waitUntil(lambda: d.cancel_button.hasFocus(), timeout=5000)
    qtbot.keyClick(d, Qt.Key.Key_Return)
    assert d.result() == options.UpdateDialog.DialogCode.Rejected


def test_cancel_installs_nothing(qtbot, site, github, keyed):
    engine = Engine()
    asked = []
    w, page = installing_window(qtbot, site, engine, asked=asked, yes=False)
    click(qtbot, page.install_button)
    assert [a["version"] for a in asked] == [NEW] and asked[0]["notes"] == NOTES
    assert page.install_result.text() == "Nothing was installed." and engine.calls == []
    assert w.jobs.running() is None


# ------------------------------------------------------------------ installing
def test_a_yes_reaches_the_engine_with_the_tag_and_the_key_then_the_window_closes(qtbot, site, github, keyed,
                                                                                    caplog):
    caplog.set_level(logging.INFO, logger="folio_exchange")         # as the app's log setup does
    engine = Engine()
    w, page = installing_window(qtbot, site, engine)
    click(qtbot, page.install_button)
    qtbot.waitUntil(lambda: w.quitting, timeout=LONG)
    (_, tag, kw), = engine.asked("prepare")
    assert tag == f"v{NEW}" and kw["key"] == keyed                 # updates.RELEASE_KEY, nothing else
    (_, _plan, restart), = engine.asked("start_swap")
    exe, args, _folder = startup.app_command(w.site_root, "options")
    assert restart[-1] == [exe, *args]                             # this window comes back on Options
    assert page.install_result.text() == f"FOLIO-Exchange closes now and opens again on version {NEW}."
    progress = page.install_progress.text()
    assert f"Getting FOLIO-Exchange {NEW} ready" in progress and f"FOLIO-Exchange {NEW} is ready." in progress
    assert "INFO" not in progress


def test_the_update_is_the_running_action(qtbot, site, github, keyed):
    engine = Engine()
    w, page = installing_window(qtbot, site, engine)
    go = threading.Event()

    def prepare(tag, **kw):
        go.wait(20)
        return {"ok": False, "stage": "busy", "program": "old", "done": [], "left": [], "reason": "Stop here."}

    engine.prepare = prepare
    click(qtbot, page.install_button)
    try:
        qtbot.waitUntil(lambda: w.jobs.running() is not None and not page.install_button.isEnabled(), timeout=5000)
        assert w.jobs.running().kind == "update" and not w.may_quit_for_update()
        assert w.banner.text() == "Running: program update. Other actions wait until it finishes."
        assert not page.check_button.isEnabled() and not page.rename_button.isEnabled()
    finally:
        go.set()
    ended(qtbot, w)
    assert page.install_result.text() == "Stop here."
    qtbot.waitUntil(lambda: page.install_button.isEnabled(), timeout=5000)        # it can be tried again


def test_an_action_running_holds_the_update_and_says_so(qtbot, site, github, keyed):
    w, page = installing_window(qtbot, site, Engine(), yes=True)
    w.ask_install = lambda release: pytest.fail("asked while an action was running")
    with w.jobs.holding("check"):
        w.install_update(NEW)
    assert page.install_result.text() == "Wait for the connection check to finish, then install the update."


def real_engine(install, tmp_path, *, processes=lambda folder: [], now=CALM, sites=()):
    class Real(Engine):
        build_environment = Builder()

        def known_sites(self):
            return list(sites)

        def prepare(self, tag, **kw):
            return selfupdate.prepare(tag, install=install, current=OLD, run=Runner(), now=now, processes=processes,
                                      updates_dir=tmp_path / "updates", protect=lambda *a, **k: None, **kw)

    return Real()


def broken_site(tmp_path):
    root = make_site(tmp_path / "broken", "https://folio.example.org")
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "history.sqlite").write_bytes(b"this is not a database at all" * 10)
    return root


@pytest.mark.parametrize("case", ["process", "quiet", "copy", "signature"])
def test_each_refusal_is_shown_in_place_and_nothing_closes(qtbot, site, github, install, key,
                                                         tmp_path, monkeypatch, case):
    # The temporary install (PR 36's fixture) is found first; then the window is told this install can update.
    monkeypatch.setattr(selfupdate, "find_install", lambda **kw: dict(UPDATABLE))
    monkeypatch.setattr(updates, "RELEASE_KEY", key[1])            # the window passes it; the release is signed so
    program = install["program"]
    busy = [(99999, f"{program}\\Scripts\\folio-exchange.exe")]
    engine = real_engine(install, tmp_path, processes=lambda f: busy if case == "process" else [],
                         now=CALM.replace(minute=7) if case == "quiet" else CALM,
                         sites=[broken_site(tmp_path)] if case == "copy" else ())
    signer = make_key()[0] if case == "signature" else key[0]
    w, page = installing_window(qtbot, site, engine, http=FakeGitHub(release(signer)))
    click(qtbot, page.install_button)
    ended(qtbot, w)
    said = page.install_result.text()
    expected = {"process": f"FOLIO-Exchange is running from {program}: a run, the page or another window.",
                "quiet": "Scheduled runs start between minutes 5 and 15 of each hour.",
                "copy": f"The site {tmp_path / 'broken'} couldn't be copied",
                "signature": "The download isn't signed with FOLIO-Exchange's release key."}[case]
    assert seen(page.install_result) and said.startswith(expected), said
    assert f"FOLIO-Exchange {OLD} is still installed." in page.install_notes.text()
    assert not w.quitting and engine.asked("start_swap") == []
    qtbot.waitUntil(lambda: page.install_button.isEnabled(), timeout=5000)        # it can be tried again


# ------------------------------------------------------------------ quit for update
def test_an_idle_window_quits_for_an_update(qtbot, site, name):
    server = single.InstanceServer(name)
    assert server.listen()
    w = window(qtbot, site, instance=server)
    assert single.probe(name) is not None
    assert single.ask_to_quit(name) == "quit"
    qtbot.waitUntil(lambda: w.quitting, timeout=5000)


def test_a_window_with_an_action_running_refuses_to_quit(qtbot, site, name):
    server = single.InstanceServer(name)
    assert server.listen()
    w = window(qtbot, site, instance=server)
    with w.jobs.holding("check"):
        assert single.ask_to_quit(name) == "busy"
    assert not w.quitting
    w.toggling = True                                              # a pause or resume being written counts too
    assert single.ask_to_quit(name) == "busy"
    w.toggling = False
    assert single.ask_to_quit(name) == "quit"
    qtbot.waitUntil(lambda: w.quitting, timeout=5000)


def test_nothing_there_and_a_server_that_was_not_told_are_never_quit(qtbot, name):
    assert single.ask_to_quit(name) == "gone" and single.probe(name) is None
    server = single.InstanceServer(name)
    assert server.listen()
    quit_ = []
    server.quit_asked.connect(lambda: quit_.append(1))
    assert single.ask_to_quit(name) == "busy"                      # may_quit unset: a no
    qtbot.wait(single.QUIT_DELAY_MS + 100)
    assert quit_ == []
    server.close()


def test_the_tray_quits_for_an_update(qtbot, site, name, trays):
    server = single.InstanceServer(name)
    assert server.listen()
    t = make_tray(qtbot, [site])
    trays.append(t)
    t.listen(server)
    assert single.ask_to_quit(name) == "quit"
    qtbot.waitUntil(lambda: t.quits == [True], timeout=5000)
    server.close()


# ------------------------------------------------------------------ afterwards
def write_result(**result) -> None:
    folder = selfupdate.updates_folder()
    folder.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    result.setdefault("finished", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    (folder / selfupdate.RESULT).write_text(json.dumps(result), encoding="utf-8")


def audit_entries(root):
    with History(load_site(root).history_path) as hist:
        return [e for e in hist.audit_log() if e["action"] == "program updated"]


def test_the_next_start_says_updated_to_and_records_it_once(qtbot, site):
    places.remember_site(site)
    write_result(ok=True, version=__version__, **{"from": "0.1.0"})
    w = window(qtbot, site)
    qtbot.waitUntil(lambda: not w.errands.pending, timeout=LONG)
    w.select("options")
    assert seen(w.options.last_update) and w.options.last_update.text() == f"Updated to {__version__}."
    (entry,) = audit_entries(site)
    assert entry["detail"] == {"from": "0.1.0", "to": __version__, "via": "app"}
    w2 = window(qtbot, site)                                       # another start, the same day
    qtbot.waitUntil(lambda: not w2.errands.pending, timeout=LONG)
    assert len(audit_entries(site)) == 1


def test_with_no_update_behind_it_the_window_starts_no_errand_for_one(qtbot, site):
    w = window(qtbot, site)
    assert w.after_update_asked and not w.errands.pending
    w.select("options")
    assert not seen(w.options.last_update)


def test_the_next_start_says_when_the_update_did_not_finish(qtbot, site):
    reason = "FOLIO-Exchange is still running from C:\\FX\\program. Nothing was changed. Close it and try again."
    write_result(ok=False, version="9.9.9", **{"from": __version__}, reason=reason)
    w = window(qtbot, site)
    qtbot.waitUntil(lambda: not w.errands.pending, timeout=LONG)
    w.select("options")
    assert w.options.last_update.text() == (f"The update to 9.9.9 didn't finish. FOLIO-Exchange {__version__} is "
                                            f"still installed. {reason}")
    assert audit_entries(site) == []


def test_a_tray_started_again_records_the_update_when_no_window_has(qtbot, site, trays):
    places.watch_site(site, True)
    write_result(ok=True, version=__version__, **{"from": "0.1.0"})
    t = make_tray(qtbot, [site])
    trays.append(t)
    t.look_for_update = lambda: None
    t.start()
    qtbot.waitUntil(lambda: t.after_asked and not t.after_reader.busy, timeout=LONG)
    assert t.readings >= 1                                         # after the first reading, not alongside it
    assert len(audit_entries(site)) == 1
