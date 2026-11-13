"""The tray (D4), run offscreen with pytest-qt: what it tells and shows for the sites it watches, opening a site
from it, following its list of sites, `--tray`, and its two settings under Options. No real tray icon, process
or Startup entry: the tray's notifications, launches and quitting are replaced, and the registry is a fake
(conftest's ``registry``)."""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import QT_LOADS

if not QT_LOADS:
    pytest.skip("needs the desktop extra (PySide6) and pytest-qt, and a Qt that loads", allow_module_level=True)

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402
from sitekit import make_feed  # noqa: E402
from test_desktop_setup import LONG, click, settled, window  # noqa: E402
from test_desktop_tray_core import add_run  # noqa: E402

from folio_exchange import siteops  # noqa: E402
from folio_exchange.desktop import main, places, startup, tray, watch  # noqa: E402
from folio_exchange.desktop.tray import Tray  # noqa: E402


def make_tray(qtbot, roots) -> Tray:
    t = Tray(sites=lambda: list(roots), poll_ms=600_000)
    t.told, t.opened, t.quits, t.popped = [], [], [], []
    t.show_message = lambda title, text: t.told.append((title, text))
    t.launch = t.opened.append
    t.quit_app = lambda: t.quits.append(True)
    t.popup = t.popped.append
    return t


def read(qtbot, t: Tray) -> None:
    """One reading of every watched site, waited for."""
    before = t.readings
    t.refresh()
    qtbot.waitUntil(lambda: t.readings >= before + len(t.roots) and not any(r.busy for r in t.readers.values()),
                    timeout=LONG)


def tick(qtbot, box) -> None:
    """Click a check box on its box: a click past the end of its label doesn't count, as in any Qt app."""
    qtbot.waitUntil(lambda: box.isVisibleTo(box.window()) and box.isEnabled(), timeout=5000)
    QApplication.processEvents()
    qtbot.mouseClick(box, Qt.MouseButton.LeftButton, pos=QPoint(8, box.height() // 2))


def menu_texts(t: Tray) -> list[str]:
    return [a.text() for a in t.menu.actions() if not a.isSeparator()]


@pytest.fixture
def trays():
    made: list[Tray] = []
    yield made
    for t in made:
        t.shut_down()


def test_a_held_scheduled_run_is_told_once_and_opens_its_site(qtbot, site, trays):
    t = make_tray(qtbot, [site])
    trays.append(t)
    read(qtbot, t)
    assert t.told == []                                            # nothing needs anyone yet
    assert t.icon.toolTip() == "FOLIO-Exchange: nothing needs you right now"
    assert "chq: No runs yet" in menu_texts(t)
    add_run(site, "held")
    read(qtbot, t)
    assert t.told == [(f"FOLIO-Exchange – {site.name}", "chq is held for your review.")]
    assert "chq: Held for review" in menu_texts(t)
    assert t.icon.toolTip() == "FOLIO-Exchange: 1 thing needs you"
    assert t.icon.icon().cacheKey() == t.marked.cacheKey()         # the dot, and the words in the tooltip
    read(qtbot, t)
    assert len(t.told) == 1                                        # told once
    t.icon.messageClicked.emit()
    assert t.opened == [site]
    opener = next(a for a in t.menu.actions() if a.text() == "&Open FOLIO-Exchange")
    opener.trigger()
    feed_line = next(a for a in t.menu.actions() if a.text() == "chq: Held for review")
    feed_line.trigger()
    assert t.opened == [site, site, site]
    t._activated(QSystemTrayIcon.ActivationReason.Trigger)        # a click on the icon opens the one site
    assert t.opened[-1] == site and t.popped == []


def test_what_already_waits_is_told_when_the_tray_starts(qtbot, site, trays):
    add_run(site, "failed", trigger="manual", error="Cannot reach FOLIO.")
    t = make_tray(qtbot, [site])
    trays.append(t)
    read(qtbot, t)
    assert t.told == [(f"FOLIO-Exchange – {site.name}", "chq's last run failed.")]


def test_several_sites_get_a_section_each_and_a_click_shows_the_menu(qtbot, site, tmp_path, trays):
    gone = tmp_path / "gone"
    t = make_tray(qtbot, [site, gone])
    trays.append(t)
    assert "Reading the site…" in menu_texts(t)
    read(qtbot, t)
    texts = menu_texts(t)
    assert f"Open {site.name}" in texts and "Open gone" not in texts     # nothing there to open
    assert f"Stop watching {site.name}" in texts and "Stop watching gone" in texts
    assert any(x.startswith("Couldn't read the site.") for x in texts)
    assert [title for title, _text in t.told] == ["FOLIO-Exchange – gone"]
    t._activated(QSystemTrayIcon.ActivationReason.Trigger)
    assert t.opened == [] and len(t.popped) == 1 and isinstance(t.popped[0], QPoint)
    assert "&Check now" in texts and "&Quit the tray" in texts


def test_a_long_list_is_cut_and_counted(qtbot, site, trays):
    t = make_tray(qtbot, [site])
    trays.append(t)
    t.tell(site, [f"line {n}." for n in range(7)])
    title, text = t.told[-1]
    assert text.splitlines() == ["line 0.", "line 1.", "line 2.", "line 3.", "And 3 more things."]
    assert t.last_told == site


def test_the_tray_follows_its_list_and_closes_when_it_is_empty(qtbot, site, tmp_path, trays):
    places.watch_site(site, True)
    t = Tray(poll_ms=600_000)
    trays.append(t)
    t.quits = []
    t.quit_app = lambda: t.quits.append(True)
    assert t.roots == [Path(site).absolute()]
    other = tmp_path / "other"
    places.watch_site(other, True)
    t.reload()
    assert t.roots[-1] == other.absolute() and t.quits == []
    places.watch_site(site, False)
    places.watch_site(other, False)
    t.reload()
    assert t.quits == [True]


def test_a_site_that_has_gone_can_be_taken_off_the_list_from_the_menu(qtbot, tmp_path, trays):
    """Regression (review of 12 Nov): a deleted or moved site stayed on the list for good, with a red dot."""
    gone = tmp_path / "gone"
    places.watch_site(gone, True)
    t = Tray(poll_ms=600_000)
    trays.append(t)
    t.quits = []
    t.quit_app = lambda: t.quits.append(True)
    stop = next(a for a in t.menu.actions() if a.text() == "&Stop watching this site")
    stop.trigger()
    assert places.watched_sites() == [] and t.quits == [True]          # the last site: the tray closes


def test_the_list_is_read_again_at_every_poll(qtbot, site, tmp_path, trays):
    roots = [site]
    t = Tray(sites=lambda: list(roots), poll_ms=600_000)
    trays.append(t)
    t.show_message = lambda title, text: None
    read(qtbot, t)
    roots.append(tmp_path / "renamed-elsewhere")                      # as a rename from the command line leaves it
    read(qtbot, t)
    assert t.roots == roots and str(tmp_path / "renamed-elsewhere") in t.looks


def test_a_site_that_doesnt_answer_holds_up_no_other(qtbot, site, tmp_path, trays, monkeypatch):
    """Regression (review of 12 Nov): one reading of every site in turn, newest only, so a share that stopped
    answering stopped every site's readings, with one more stuck thread every poll."""
    import threading

    stalled = tmp_path / "stalled"
    release, calls = threading.Event(), []
    real = watch.look

    def look(root, now=None):
        calls.append(str(root))
        if root == stalled:
            release.wait(20)
        return real(root, now)

    monkeypatch.setattr(watch, "look", look)
    t = make_tray(qtbot, [stalled, site])
    trays.append(t)
    t.refresh()
    qtbot.waitUntil(lambda: str(site) in t.looks, timeout=LONG)        # the other site is read
    t.refresh()
    qtbot.waitUntil(lambda: calls.count(str(site)) == 2, timeout=LONG)
    assert calls.count(str(stalled)) == 1                              # still under way: not started again
    release.set()
    qtbot.waitUntil(lambda: not any(r.busy for r in t.readers.values()), timeout=LONG)


def test_scheduled_runs_stopping_waits_until_the_tray_has_been_awake_an_hour_and_more(qtbot, site, trays):
    """Regression (review of 12 Nov): after a night off or asleep, the task looks stopped until its next hourly
    start, so the tray said so every morning."""
    from datetime import datetime, timedelta, timezone

    from folio_exchange import feedops
    from folio_exchange.history import History

    with History(site / "data" / "history.sqlite") as h:
        feedops.record_due_start(h, datetime.now(timezone.utc) - timedelta(hours=10))
    t = make_tray(qtbot, [site])
    trays.append(t)
    read(qtbot, t)
    assert not any("stopped" in text for _title, text in t.told)
    assert t.icon.toolTip() == "FOLIO-Exchange: nothing needs you right now"
    assert "Scheduled runs have stopped." in menu_texts(t)             # the menu shows it, quietly
    t.awake_since -= tray.TASK_GRACE_S + 1                             # awake long enough for an hourly start
    read(qtbot, t)
    assert [text for _title, text in t.told] == ["Scheduled runs have stopped."]
    read(qtbot, t)
    assert len(t.told) == 1                                            # told once
    t.last_tick -= 3 * t.poll_s + 1                                    # the computer slept: quiet again
    t._tick()
    assert t.quiet()


def test_the_badge_is_drawn_on_the_symbol(qapp):
    plain = tray.theme.logo_icon()
    marked = tray.badged(plain)
    assert not marked.isNull() and marked.cacheKey() != plain.cacheKey()


# ------------------------------------------------------------------ folio-exchange-app --tray
@pytest.fixture
def quiet_tray_start(monkeypatch, qapp):
    """--tray in the test process: no logging set up for the whole run, no look applied to every test's widgets,
    a tray name of the test's own, and the app's quit setting put back."""
    monkeypatch.setattr(tray, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(tray.theme, "apply", lambda app: None)
    monkeypatch.setattr(places, "tray_instance_name", lambda: f"folio-exchange-test-tray-{id(monkeypatch)}")
    quits = qapp.quitOnLastWindowClosed()
    yield
    qapp.setQuitOnLastWindowClosed(quits)


def test_tray_with_no_sites_says_how_to_add_one(quiet_tray_start, capsys):
    assert main(["--tray"]) == 0
    assert "turn on Watch this site from the tray" in capsys.readouterr().err


def test_tray_with_a_site_watches_it(quiet_tray_start, site, monkeypatch):
    seen = []
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    assert main(["--tray", "--site", str(site)]) == 1               # no tray here, which it says
    assert places.is_watched(site)
    monkeypatch.setattr(tray.single, "claim", lambda name: seen.append(name) or ("shown", None))
    assert main(["--tray"]) == 0                                    # one runs: it was asked to read its list again
    assert seen


def test_tray_without_a_system_tray_says_so(quiet_tray_start, site, monkeypatch, capsys):
    places.watch_site(site, True)
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    assert main(["--tray"]) == 1
    assert "no system tray" in capsys.readouterr().err


# ------------------------------------------------------------------ Options
def options_window(qtbot, site, started):
    w = window(qtbot, site)
    w.start_tray = lambda: started.append(True) or True
    w.options.set_platform(windows=True)                           # what Windows shows, wherever this runs
    w.select("options")
    return w


def test_watching_this_site_from_options_starts_the_tray(qtbot, site):
    started: list = []
    w = options_window(qtbot, site, started)
    page = w.options
    assert not page.watch_box.isChecked()
    tick(qtbot, page.watch_box)
    assert places.is_watched(w.site_root) and started == [True]
    assert page.watch_box.isChecked()
    assert page.tray_result.text().startswith("The tray watches this site.")
    tick(qtbot, page.watch_box)
    assert not places.is_watched(w.site_root) and started == [True, True]   # the running tray reads its list again
    assert page.tray_result.text() == "The tray doesn't watch this site any more."


def test_a_watch_list_that_cannot_be_saved_is_said_and_changes_nothing(qtbot, site, monkeypatch):
    started: list = []
    w = options_window(qtbot, site, started)

    def refuse(root, on):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(places, "watch_site", refuse)
    tick(qtbot, w.options.watch_box)
    assert started == [] and not w.options.watch_box.isChecked()
    assert w.options.tray_result.text().startswith("Couldn't save the tray's list of sites")


def test_starting_the_tray_at_sign_in_is_opt_in(qtbot, site, registry):
    started: list = []
    w = options_window(qtbot, site, started)
    page = w.options
    key = (startup.RUN_KEY, startup.RUN_VALUE)
    assert not page.startup_box.isChecked() and key not in registry.values
    tick(qtbot, page.startup_box)
    assert registry.values[key] == startup.startup_line() and page.startup_box.isChecked()
    assert page.tray_result.text() == "The tray starts when you sign in to Windows."
    tick(qtbot, page.startup_box)
    assert key not in registry.values and not page.startup_box.isChecked()
    registry.values[key] = r'"C:\Old\folio-exchange-app.exe" --tray'
    w.show_tray_settings()
    assert page.startup_box.isChecked() and page.startup_note.isVisibleTo(page)
    assert page.startup_note.text().startswith("When you sign in, Windows starts the tray from another copy")
    registry.refuse = PermissionError(5, "Access is denied")
    tick(qtbot, page.startup_box)
    assert page.tray_result.text().startswith("Couldn't change the Startup entry")
    assert page.startup_box.isChecked()                            # unchanged, as the registry is


def test_off_windows_there_is_no_startup_box(qtbot, site):
    started: list = []
    w = options_window(qtbot, site, started)
    w.options.set_platform(windows=False)
    assert not w.options.startup_box.isVisibleTo(w.options) and w.options.watch_box.isVisibleTo(w.options)


def test_a_watched_site_renamed_in_the_app_stays_watched(qtbot, site, monkeypatch):
    make_feed(site, delivery={"transport": "local", "directory": str(site.parent / "vendor-drop")})
    monkeypatch.setattr(siteops.platform, "system", lambda: "Linux")      # no scheduled task to look for
    started: list = []
    w = options_window(qtbot, site, started)
    places.watch_site(w.site_root, True)
    w.options.name.setText("Lakeside Library")
    click(qtbot, w.options.rename_button)
    new = site.parent / "Lakeside Library"
    qtbot.waitUntil(lambda: w.options.done.isVisible(), timeout=LONG)
    settled(qtbot, w)
    assert places.watched_sites() == [new.resolve()] and started == [True]


def test_the_tray_settings_are_named_by_their_labels(qtbot, site):
    started: list = []
    w = options_window(qtbot, site, started)
    labels = [b.text() for b in (w.options.watch_box, w.options.startup_box)]
    assert labels == ["&Watch this site from the tray", "Start the tray when I sign &in to Windows"]
    for box in (w.options.watch_box, w.options.startup_box):
        assert box.text().replace("&", "") in box.accessibleName()          # label in name
